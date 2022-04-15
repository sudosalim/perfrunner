import copy
import glob
import multiprocessing
import os
import re
import shutil
from multiprocessing import Pool
from time import sleep, time
from typing import Callable

from decorator import decorator

from logger import logger
from perfrunner.helpers import local
from perfrunner.helpers.cbmonitor import with_stats
from perfrunner.helpers.cluster import ClusterManager
from perfrunner.helpers.memcached import MemcachedHelper
from perfrunner.helpers.metrics import MetricHelper
from perfrunner.helpers.misc import pretty_dict, target_hash
from perfrunner.helpers.monitor import Monitor
from perfrunner.helpers.profiler import Profiler, with_profiles
from perfrunner.helpers.remote import RemoteHelper
from perfrunner.helpers.reporter import ShowFastReporter
from perfrunner.helpers.rest import RestHelper
from perfrunner.helpers.worker import (
    WorkerManager,
    pillowfight_data_load_task,
    syncgateway_bh_puller_task,
    syncgateway_delta_sync_task_load_docs,
    syncgateway_delta_sync_task_run_test,
    syncgateway_e2e_cbl_task_load_docs,
    syncgateway_e2e_cbl_task_run_test,
    syncgateway_e2e_multi_cb_task_load_docs,
    syncgateway_e2e_multi_cb_task_run_test,
    syncgateway_e2e_multi_cbl_task_load_docs,
    syncgateway_e2e_multi_cbl_task_run_test,
    syncgateway_new_docpush_task,
    syncgateway_task_grant_access,
    syncgateway_task_init_users,
    syncgateway_task_load_docs,
    syncgateway_task_load_users,
    syncgateway_task_run_test,
    syncgateway_task_start_memcached,
    ycsb_data_load_task,
    ycsb_task,
)
from perfrunner.settings import (
    ClusterSpec,
    PhaseSettings,
    TargetIterator,
    TargetSettings,
    TestConfig,
)
from perfrunner.tests import PerfTest


@decorator
def with_timer(cblite_replicate, *args, **kwargs):
    test = args[0]

    t0 = time.time()

    cblite_replicate(*args, **kwargs)

    test.replicate_time = time.time() - t0  # Delta Sync time in seconds


class SGPerfTest(PerfTest):

    COLLECTORS = {'disk': False, 'ns_server': False, 'ns_server_overview': False,
                  'active_tasks': False, 'syncgateway_stats': True}

    ALL_HOSTNAMES = True
    LOCAL_DIR = "YCSB"
    CBLITE_LOG_DIR = "cblite_logs"

    def __init__(self,
                 cluster_spec: ClusterSpec,
                 test_config: TestConfig,
                 verbose: bool):
        self.dynamic_infra = False
        self.cluster_spec = cluster_spec
        self.test_config = test_config
        self.memcached = MemcachedHelper(test_config)
        self.remote = RemoteHelper(cluster_spec, verbose)
        self.rest = RestHelper(cluster_spec, test_config)
        self.master_node = next(self.cluster_spec.masters)
        self.sgw_master_node = next(cluster_spec.sgw_masters)
        self.build = self.rest.get_sgversion(self.sgw_master_node)
        self.metrics = MetricHelper(self)
        self.reporter = ShowFastReporter(cluster_spec, test_config, self.build, sgw=True)
        if self.test_config.test_case.use_workers:
            self.worker_manager = WorkerManager(cluster_spec, test_config, verbose)
        self.settings = self.test_config.access_settings
        self.settings.syncgateway_settings = self.test_config.syncgateway_settings
        self.profiler = Profiler(cluster_spec, test_config)
        self.cluster = ClusterManager(cluster_spec, test_config)
        self.target_iterator = TargetIterator(cluster_spec, test_config)
        self.monitor = Monitor(cluster_spec, test_config, verbose)
        self.sg_settings = self.test_config.syncgateway_settings

    def download_ycsb(self):
        if self.worker_manager.is_remote:
            self.remote.clone_ycsb(repo=self.test_config.syncgateway_settings.repo,
                                   branch=self.test_config.syncgateway_settings.branch,
                                   worker_home=self.worker_manager.WORKER_HOME,
                                   ycsb_instances=int(self.test_config.syncgateway_settings.
                                                      instances_per_client))
        else:
            local.clone_ycsb(repo=self.test_config.syncgateway_settings.repo,
                             branch=self.test_config.syncgateway_settings.branch)

    def collect_execution_logs(self):
        if self.worker_manager.is_remote:
            if os.path.exists(self.LOCAL_DIR):
                shutil.rmtree(self.LOCAL_DIR, ignore_errors=True)
            os.makedirs(self.LOCAL_DIR)
            self.remote.get_syncgateway_ycsb_logs(self.worker_manager.WORKER_HOME,
                                                  self.test_config.syncgateway_settings,
                                                  self.LOCAL_DIR)

    def collect_cblite_logs(self):
        if self.worker_manager.is_remote:
            if os.path.exists(self.CBLITE_LOG_DIR):
                shutil.rmtree(self.CBLITE_LOG_DIR, ignore_errors=True)
            os.makedirs(self.CBLITE_LOG_DIR)
            for client in self.cluster_spec.workers[:int(
                                                     self.settings.syncgateway_settings.clients)]:
                for instance_id in range(int(
                                         self.settings.syncgateway_settings.instances_per_client)):
                    self.remote.get_cblite_logs(client, instance_id, self.CBLITE_LOG_DIR)

    def run_sg_phase(self,
                     phase: str,
                     task: Callable,
                     settings: PhaseSettings,
                     timer: int = None,
                     distribute: bool = False,
                     wait: bool = True) -> None:
        logger.info('Running {}: {}'.format(phase, pretty_dict(settings)))
        self.worker_manager.run_sg_tasks(task, settings, timer, distribute, phase)
        if wait:
            self.worker_manager.wait_for_workers()

    def start_memcached(self):
        self.run_sg_phase("start memcached", syncgateway_task_start_memcached,
                          self.settings, self.settings.time, False)

    def load_users(self):
        self.run_sg_phase("load users", syncgateway_task_load_users,
                          self.settings, self.settings.time, False)

    def init_users(self):
        if self.test_config.syncgateway_settings.auth == 'true':
            self.run_sg_phase("init users", syncgateway_task_init_users,
                              self.settings, self.settings.time, False)

    def grant_access(self):
        if self.test_config.syncgateway_settings.grant_access == 'true':
            self.run_sg_phase("grant access to  users", syncgateway_task_grant_access,
                              self.settings, self.settings.time, False)

    def load_docs(self):
        self.run_sg_phase("load docs", syncgateway_task_load_docs,
                          self.settings, self.settings.time, False)

    @with_stats
    @with_profiles
    def run_test(self):
        self.run_sg_phase("run test", syncgateway_task_run_test,
                          self.settings, self.settings.time, True)

    def compress_sg_logs(self):
        try:
            self.remote.compress_sg_logs_new()
            return
        except Exception as ex:
            print(str(ex))
        try:
            self.remote.compress_sg_logs()
        except Exception as ex:
            print(str(ex))

    def get_sg_logs(self):
        ssh_user, ssh_pass = self.cluster_spec.ssh_credentials
        if self.cluster_spec.sgw_servers:
            server = self.cluster_spec.sgw_servers[0]
        else:
            server = self.cluster_spec.servers[0]
        try:
            local.get_sg_logs_new(host=server, ssh_user=ssh_user, ssh_pass=ssh_pass)
            local.get_sg_console(host=server, ssh_user=ssh_user, ssh_pass=ssh_pass)
        except Exception as ex:
            print(str(ex))
        if self.settings.syncgateway_settings.troublemaker:
            for server in self.cluster_spec.sgw_servers:
                try:
                    local.get_troublemaker_logs(host=server, ssh_user=ssh_user, ssh_pass=ssh_pass)
                    local.rename_troublemaker_logs(from_host=server)
                except Exception as ex:
                    print(str(ex))
                try:
                    local.get_default_troublemaker_logs(host=server, ssh_user=ssh_user,
                                                        ssh_pass=ssh_pass)
                except Exception as ex:
                    print(str(ex))

    def channel_list(self, number_of_channels: int):
        channels = []
        for number in range(1, number_of_channels + 1):
            channel = "channel-" + str(number)
            channels.append(channel)
        return channels

    def run(self):
        self.remote.remove_sglogs()
        self.download_ycsb()
        self.start_memcached()
        self.load_users()
        self.load_docs()
        self.init_users()
        self.grant_access()
        self.run_test()
        self.report_kpi()

    def __exit__(self, exc_type, exc_val, exc_tb):
        if self.settings.syncgateway_settings.cbl_per_worker:
            self.remote.kill_cblite()
        if self.settings.syncgateway_settings.collect_cbl_logs:
            self.collect_cblite_logs()
        if self.settings.syncgateway_settings.ramdisk_size:
            self.remote.destroy_cblite_ramdisk()
        if self.settings.syncgateway_settings.troublemaker:
            self.remote.kill_troublemaker()

        if self.test_config.test_case.use_workers:
            self.worker_manager.download_celery_logs()
            self.worker_manager.terminate()

        if self.test_config.cluster.online_cores:
            self.remote.enable_cpu()

        if self.test_config.cluster.kernel_mem_limit:
            self.remote.reset_memory_settings()
            self.monitor.wait_for_servers()

        if self.settings.syncgateway_settings.collect_sgw_logs:
            self.compress_sg_logs()
            self.get_sg_logs()


class SGRead(SGPerfTest):
    def _report_kpi(self):
        self.collect_execution_logs()
        for f in glob.glob('{}/*runtest*.result'.format(self.LOCAL_DIR)):
            with open(f, 'r') as fout:
                logger.info(f)
                logger.info(fout.read())

        self.reporter.post(
            *self.metrics.sg_throughput("Throughput (req/sec), GET doc by id")
        )


class SGAuthThroughput(SGPerfTest):
    def _report_kpi(self):
        self.collect_execution_logs()
        for f in glob.glob('{}/*runtest*.result'.format(self.LOCAL_DIR)):
            with open(f, 'r') as fout:
                logger.info(f)
                logger.info(fout.read())

        self.reporter.post(
            *self.metrics.sg_throughput("Throughput (req/sec), POST auth")
        )


class SGAuthLatency(SGPerfTest):
    def _report_kpi(self):
        self.collect_execution_logs()
        for f in glob.glob('{}/*runtest*.result'.format(self.LOCAL_DIR)):
            with open(f, 'r') as fout:
                logger.info(f)
                logger.info(fout.read())

        self.reporter.post(
            *self.metrics.sg_latency('[SCAN], 95thPercentileLatency(us)',
                                     'Latency (ms), POST auth, 95 percentile')
        )


class SGSync(SGPerfTest):
    def _report_kpi(self):
        self.collect_execution_logs()
        for f in glob.glob('{}/*runtest*.result'.format(self.LOCAL_DIR)):
            with open(f, 'r') as fout:
                logger.info(f)
                logger.info(fout.read())

        self.reporter.post(
            *self.metrics.sg_throughput('Throughput (req/sec), GET docs via _changes')
        )

        self.reporter.post(
            *self.metrics.sg_latency('[INSERT], 95thPercentileLatency(us)',
                                     'Latency, round-trip write, 95 percentile (ms)')
        )


class SGSyncQueryThroughput(SGPerfTest):
    def _report_kpi(self):
        self.collect_execution_logs()
        for f in glob.glob('{}/*runtest*.result'.format(self.LOCAL_DIR)):
            with open(f, 'r') as fout:
                logger.info(f)
                logger.info(fout.read())

        self.reporter.post(
            *self.metrics.sg_throughput('Throughput (req/sec), GET docs via _changes')
        )


class SGSyncQueryLatency(SGPerfTest):
    def _report_kpi(self):
        self.collect_execution_logs()
        for f in glob.glob('{}/*runtest*.result'.format(self.LOCAL_DIR)):
            with open(f, 'r') as fout:
                logger.info(f)
                logger.info(fout.read())

        self.reporter.post(
            *self.metrics.sg_latency('[READ], 95thPercentileLatency(us)',
                                     'Latency (ms), GET docs via _changes, 95 percentile')
        )


class SGWrite(SGPerfTest):
    def _report_kpi(self):
        self.collect_execution_logs()
        for f in glob.glob('{}/*runtest*.result'.format(self.LOCAL_DIR)):
            with open(f, 'r') as fout:
                logger.info(f)
                logger.info(fout.read())

        self.reporter.post(
            *self.metrics.sg_throughput("Throughput (req/sec), POST doc")
        )


class SGMixQueryThroughput(SGPerfTest):
    def _report_kpi(self):
        self.collect_execution_logs()
        for f in glob.glob('{}/*runtest*.result'.format(self.LOCAL_DIR)):
            with open(f, 'r') as fout:
                logger.info(f)
                logger.info(fout.read())

        self.reporter.post(
            *self.metrics.sg_throughput("Throughput (req/sec)")
        )


class SGTargetIterator(TargetIterator):

    def __iter__(self):
        password = self.test_config.bucket.password
        prefix = self.prefix
        src_master = next(self.cluster_spec.masters)
        for bucket in self.test_config.buckets:
            if self.prefix is None:
                prefix = target_hash(src_master, bucket)
            yield TargetSettings(src_master, bucket, password, prefix)


class CBTargetIterator(TargetIterator):

    def __iter__(self):
        password = self.test_config.bucket.password
        prefix = self.prefix
        # masters = self.cluster_spec.masters
        cb_master = self.cluster_spec.servers[0]
        # src_master = next(masters)
        # dest_master = next(masters)
        for bucket in self.test_config.buckets:
            if self.prefix is None:
                prefix = target_hash(cb_master, bucket)
            yield TargetSettings(cb_master, bucket, password, prefix)


class SGImportLoad(PerfTest):

    def load(self, *args):
        PerfTest.load(self, task=pillowfight_data_load_task)

    def run(self):
        self.remote.remove_sglogs()
        self.load()


class SGImportThroughputTest(SGPerfTest):

    def download_ycsb(self):
        if self.worker_manager.is_remote:
            self.remote.init_ycsb(
                    repo=self.test_config.ycsb_settings.repo,
                    branch=self.test_config.ycsb_settings.branch,
                    worker_home=self.worker_manager.WORKER_HOME,
                    sdk_version=self.test_config.ycsb_settings.sdk_version)
        else:
            local.clone_git_repo(repo=self.test_config.ycsb_settings.repo,
                                 branch=self.test_config.ycsb_settings.branch)

    def load(self, *args, **kwargs):
        PerfTest.load(self, task=ycsb_data_load_task)

    def access(self, *args, **kwargs):
        PerfTest.access(self, task=ycsb_task)

    def access_bg(self, *args, **kwargs):
        PerfTest.access_bg(self, task=ycsb_task)

    COLLECTORS = {'disk': False, 'ns_server': False, 'ns_server_overview': False,
                  'active_tasks': False, 'syncgateway_stats': True}

    def _report_kpi(self, time_elapsed_load, items_in_range_load, time_elapsed_access,
                    items_in_range_access):
        self.reporter.post(
            *self.metrics.sgimport_items_per_sec(time_elapsed=time_elapsed_load,
                                                 items_in_range=items_in_range_load,
                                                 operation="INSERT")
        )
        self.reporter.post(
            *self.metrics.sgimport_items_per_sec(time_elapsed=time_elapsed_access,
                                                 items_in_range=items_in_range_access,
                                                 operation="UPDATE")
        )

    @with_stats
    @with_profiles
    def monitor_sg_import(self, phase):
        host = self.cluster_spec.sgw_servers[0]
        expected_docs = self.test_config.load_settings.items
        if phase == 'access':
            expected_docs = expected_docs * 2
        logger.info("expected docs :{}".format(expected_docs))

        initial_docs = self.initial_import_count()
        logger.info("initial docs imported :{}".format(initial_docs))

        remaining_docs = expected_docs - initial_docs
        logger.info("remaining_docs :{}".format(remaining_docs))

        time_elapsed, items_in_range = self.monitor.monitor_sgimport_queues(host, expected_docs)
        return time_elapsed, items_in_range

    def initial_import_count(self):
        total_initial_docs = 0
        for i in range(self.test_config.syncgateway_settings.import_nodes):
            server = self.cluster_spec.sgw_servers[i]
            import_count = self.monitor.get_import_count(host=server)
            total_initial_docs += import_count
        return total_initial_docs

    @with_stats
    @with_profiles
    def monitor_sg_import_multinode(self, phase: str):
        expected_docs = self.test_config.load_settings.items
        if phase == 'access':
            expected_docs = expected_docs * 2
        logger.info("expected docs :{}".format(expected_docs))

        initial_docs = self.initial_import_count()
        logger.info("initial docs imported :{}".format(initial_docs))

        remaining_docs = expected_docs - initial_docs
        logger.info("remaining_docs :{}".format(remaining_docs))

        importing = True

        start_time = time()

        while importing:
            total_count = 0
            for i in range(self.test_config.syncgateway_settings.import_nodes):
                server = self.cluster_spec.sgw_servers[i]
                import_count = self.monitor.get_import_count(host=server)
                logger.info('import count : {} , host : {}'.format(import_count, server))
                total_count += import_count
            if total_count >= expected_docs:
                importing = False
            if time() - start_time > 1200:
                raise Exception("timeout of 1200 exceeded")

        end_time = time()

        time_elapsed = end_time - start_time

        return time_elapsed, remaining_docs

    def run(self):
        self.remote.remove_sglogs()

        self.download_ycsb()

        self.load()

        if self.test_config.syncgateway_settings.import_nodes > 1:
            time_elapsed_load, items_in_range_load = \
                self.monitor_sg_import_multinode(phase='load')
        else:
            time_elapsed_load, items_in_range_load = \
                self.monitor_sg_import(phase='load')

        # self.report_kpi(time_elapsed, items_in_range)

        self.access_bg()

        if self.test_config.syncgateway_settings.import_nodes > 1:
            time_elapsed_access, items_in_range_access = \
                self.monitor_sg_import_multinode(phase='access')
        else:
            time_elapsed_access, items_in_range_access = \
                self.monitor_sg_import(phase='access')

        self.report_kpi(time_elapsed_load, items_in_range_load,
                        time_elapsed_access, items_in_range_access)


class SGImportLatencyTest(SGPerfTest):

    def download_ycsb(self):
        if self.worker_manager.is_remote:
            self.remote.clone_ycsb(repo=self.test_config.ycsb_settings.repo,
                                   branch=self.test_config.ycsb_settings.branch,
                                   worker_home=self.worker_manager.WORKER_HOME,
                                   ycsb_instances=1)
        else:
            local.clone_ycsb(repo=self.test_config.ycsb_settings.repo,
                             branch=self.test_config.ycsb_settings.branch)

    COLLECTORS = {'disk': False, 'ns_server': False, 'ns_server_overview': False,
                  'active_tasks': False, 'syncgateway_stats': True,
                  'sgimport_latency': True}

    def _report_kpi(self, *args):
        self.reporter.post(
            *self.metrics.sgimport_latency()
        )

    def monitor_sg_import(self):
        host = self.cluster_spec.sgw_servers[0]
        # expected_docs = self.test_config.load_settings.items + 360
        expected_docs = self.test_config.load_settings.items
        self.monitor.monitor_sgimport_queues(host, expected_docs)

    @with_stats
    @with_profiles
    def load(self, *args):
        cb_target_iterator = CBTargetIterator(self.cluster_spec,
                                              self.test_config,
                                              prefix='symmetric')
        super().load(task=ycsb_data_load_task, target_iterator=cb_target_iterator)
        self.monitor_sg_import()

    @with_stats
    @with_profiles
    def access(self, *args):
        cb_target_iterator = CBTargetIterator(self.cluster_spec,
                                              self.test_config,
                                              prefix='symmetric')
        super().access_bg(task=ycsb_task, target_iterator=cb_target_iterator)

    def run(self):
        self.remote.remove_sglogs()
        self.download_ycsb()
        self.load()
        self.report_kpi()


class SGSyncByUserWithAuth(SGSync):

    def _report_kpi(self):
        self.collect_execution_logs()
        for f in glob.glob('{}/*runtest*.result'.format(self.LOCAL_DIR)):
            with open(f, 'r') as fout:
                logger.info(f)
                logger.info(fout.read())

        self.reporter.post(
            *self.metrics.sg_throughput('Throughput (req/sec), SYNC docs via _changes')
        )

        self.reporter.post(
            *self.metrics.sg_latency('[INSERT], 95thPercentileLatency(us)',
                                     'Latency, round-trip write, 95 percentile (ms)')
        )

    def run(self):
        self.remote.remove_sglogs()
        self.download_ycsb()
        self.start_memcached()
        self.load_users()
        self.init_users()
        self.run_test()
        self.report_kpi()


class SGSyncByKeyNoAuth(SGSyncByUserWithAuth):

    def run(self):
        self.remote.remove_sglogs()
        self.download_ycsb()
        self.start_memcached()
        self.run_test()
        self.report_kpi()


class SGSyncInitialLoad(SGSyncByUserWithAuth):

    def run(self):
        self.remote.remove_sglogs()
        self.download_ycsb()
        self.start_memcached()
        self.load_users()
        self.load_docs()
        self.init_users()
        self.run_test()
        self.report_kpi()


class SGReplicateThroughputTest1(SGPerfTest):

    def _report_kpi(self, time_elapsed, items_in_range):
        self.reporter.post(
            *self.metrics.sgreplicate_items_per_sec(time_elapsed=time_elapsed,
                                                    items_in_range=items_in_range)
        )

    def start_replication(self, sg1_master, sg2_master):
        sg1 = 'http://{}:4985/db'.format(sg1_master)
        sg2 = 'http://{}:4985/db'.format(sg2_master)

        channels = self.channel_list(int(self.sg_settings.channels))

        data = {
            "replication_id": "sgr1_push",
            "source": sg1,
            "target": sg2,
            "filter": "sync_gateway/bychannel",
            "query_params": {
                "channels": channels
            },
            "continuous": True,
            "changes_feed_limit": 10000
        }

        if self.sg_settings.sg_replication_type == 'push':
            self.rest.start_sg_replication(sg1_master, data)
        elif self.sg_settings.sg_replication_type == 'pull':
            data["replication_id"] = "sgr1_pull"
            self.rest.start_sg_replication(sg2_master, data)

    @with_stats
    @with_profiles
    def monitor_sg_replicate(self, replication_id, sg_master):
        expected_docs = self.test_config.load_settings.items
        time_elapsed, items_in_range = self.monitor.monitor_sgreplicate(sg_master,
                                                                        expected_docs,
                                                                        replication_id,
                                                                        1)
        return time_elapsed, items_in_range

    def run(self):
        masters = self.cluster_spec.masters
        sg1_master = next(masters)
        sg2_master = next(masters)
        self.remote.remove_sglogs()
        self.download_ycsb()
        self.start_memcached()
        self.load_docs()
        self.start_replication(sg1_master, sg2_master)
        if self.sg_settings.sg_replication_type == 'push':
            time_elapsed, items_in_range = self.monitor_sg_replicate("sgr1_push", sg1_master)
        elif self.sg_settings.sg_replication_type == 'pull':
            time_elapsed, items_in_range = self.monitor_sg_replicate("sgr1_pull", sg2_master)
        self.report_kpi(time_elapsed, items_in_range)


class SGReplicateThroughputMultiChannelTest1(SGReplicateThroughputTest1):

    def start_replication(self, sg1_master, sg2_master):
        sg1 = 'http://{}:4985/db'.format(sg1_master)
        sg2 = 'http://{}:4985/db'.format(sg2_master)

        channels = self.channel_list(int(self.sg_settings.channels))

        data = {
            "replication_id": "sgr1_push",
            "source": sg1,
            "target": sg2,
            "filter": "sync_gateway/bychannel",
            "query_params": {
                "channels": channels
            },
            "continuous": True,
            "changes_feed_limit": 10000
        }
        if self.sg_settings.sg_replication_type == 'push':
            self.rest.start_sg_replication(sg1_master, data)
        elif self.sg_settings.sg_replication_type == 'pull':
            data["replication_id"] = "sgr1_pull"
            self.rest.start_sg_replication(sg2_master, data)

    def run(self):
        masters = self.cluster_spec.masters
        sg1_master = next(masters)
        sg2_master = next(masters)
        self.remote.remove_sglogs()
        self.download_ycsb()
        self.start_memcached()
        self.load_docs()
        self.start_replication(sg1_master, sg2_master)
        if self.sg_settings.sg_replication_type == 'push':
            time_elapsed, items_in_range = self.monitor_sg_replicate("sgr1_push", sg1_master)
        elif self.sg_settings.sg_replication_type == 'pull':
            time_elapsed, items_in_range = self.monitor_sg_replicate("sgr1_pull", sg2_master)
        self.report_kpi(time_elapsed, items_in_range)


class SGReplicateThroughputTest2(SGPerfTest):

    def _report_kpi(self, time_elapsed, items_in_range):
        self.reporter.post(
            *self.metrics.sgreplicate_items_per_sec(time_elapsed=time_elapsed,
                                                    items_in_range=items_in_range)
        )

    def start_replication(self, sg1_master, sg2_master):
        sg1 = 'http://{}:4985/db'.format(sg1_master)
        sg2 = 'http://{}:4985/db'.format(sg2_master)

        channels = self.channel_list(int(self.sg_settings.channels))

        data = {
            "replication_id": "sgr2_push",
            "remote": sg2,
            "direction": "push",
            "filter": "sync_gateway/bychannel",
            "query_params": {
                "channels": channels
            },
            "continuous": True
        }
        if self.sg_settings.sg_replication_type == 'push':
            self.rest.start_sg_replication2(sg1_master, data)
        elif self.sg_settings.sg_replication_type == 'pull':
            data["replication_id"] = "sgr2_pull"
            data["direction"] = "pull"
            data["remote"] = sg1
            self.rest.start_sg_replication2(sg2_master, data)

    @with_stats
    @with_profiles
    def monitor_sg_replicate(self, replication_id, sg_master):
        expected_docs = self.test_config.load_settings.items
        time_elapsed, items_in_range = self.monitor.monitor_sgreplicate(sg_master,
                                                                        expected_docs,
                                                                        replication_id,
                                                                        2)
        return time_elapsed, items_in_range

    def run(self):
        masters = self.cluster_spec.masters
        sg1_master = next(masters)
        sg2_master = next(masters)
        self.remote.remove_sglogs()
        self.download_ycsb()
        self.start_memcached()
        self.load_docs()
        self.start_replication(sg1_master, sg2_master)
        if self.sg_settings.sg_replication_type == 'push':
            time_elapsed, items_in_range = self.monitor_sg_replicate("sgr2_push", sg1_master)
        elif self.sg_settings.sg_replication_type == 'pull':
            time_elapsed, items_in_range = self.monitor_sg_replicate("sgr2_pull", sg2_master)
        self.report_kpi(time_elapsed, items_in_range)


class SGReplicateThroughputMultiChannelTest2(SGReplicateThroughputTest2):

    def start_replication(self, sg1_master, sg2_master):
        sg1 = 'http://{}:4985/db'.format(sg1_master)
        sg2 = 'http://{}:4985/db'.format(sg2_master)

        channels = self.channel_list(int(self.sg_settings.channels))

        data = {
            "replication_id": "sgr2_push",
            "remote": sg2,
            "direction": "push",
            "filter": "sync_gateway/bychannel",
            "query_params": {
                "channels": channels
            },
            "continuous": True
        }
        if self.sg_settings.sg_replication_type == 'push':
            self.rest.start_sg_replication2(sg1_master, data)
        elif self.sg_settings.sg_replication_type == 'pull':
            data["replication_id"] = "sgr2_pull"
            data["direction"] = "pull"
            data["remote"] = sg1
            self.rest.start_sg_replication2(sg2_master, data)

    def run(self):
        masters = self.cluster_spec.masters
        sg1_master = next(masters)
        sg2_master = next(masters)
        self.remote.remove_sglogs()
        self.download_ycsb()
        self.start_memcached()
        self.load_docs()
        self.start_replication(sg1_master, sg2_master)
        if self.sg_settings.sg_replication_type == 'push':
            time_elapsed, items_in_range = self.monitor_sg_replicate("sgr2_push", sg1_master)
        elif self.sg_settings.sg_replication_type == 'pull':
            time_elapsed, items_in_range = self.monitor_sg_replicate("sgr2_pull", sg2_master)
        self.report_kpi(time_elapsed, items_in_range)


class SGReplicateThroughputConflictResolutionTest2(SGReplicateThroughputTest2):

    def start_replication(self, sg1_master, sg2_master):
        sg2 = 'http://{}:4985/db'.format(sg2_master)

        channels = self.channel_list(int(self.sg_settings.channels))

        data = {
            "replication_id": "sgr2_conflict_resolution",
            "remote": sg2,
            "direction": "pushAndPull",
            "filter": "sync_gateway/bychannel",
            "query_params": {
                "channels": channels
            },
            "continuous": True,
            "conflict_resolution_type": "default",
        }

        if self.sg_settings.sg_conflict_resolution == 'custom':
            data["conflict_resolution_type"] = "custom"
            data["custom_conflict_resolver"] = \
                "function(conflict) { return defaultPolicy(conflict);}"

        self.rest.start_sg_replication2(sg1_master, data)

    @with_stats
    @with_profiles
    def monitor_sg_replicate(self, replication_id, sg_master):
        expected_docs = self.test_config.load_settings.items * 2
        time_elapsed, items_in_range = self.monitor.monitor_sgreplicate(sg_master,
                                                                        expected_docs,
                                                                        replication_id,
                                                                        2)
        return time_elapsed, items_in_range

    def run(self):
        masters = self.cluster_spec.masters
        sg1_master = next(masters)
        sg2_master = next(masters)
        self.start_replication(sg1_master, sg2_master)
        time_elapsed, items_in_range = \
            self.monitor_sg_replicate("sgr2_conflict_resolution", sg1_master)
        self.report_kpi(time_elapsed, items_in_range)


class SGReplicateLoad(SGPerfTest):

    def run(self):
        self.remote.remove_sglogs()
        self.download_ycsb()
        self.start_memcached()
        self.load_docs()


class SGReplicateThroughputBidirectionalTest1(SGReplicateThroughputTest1):

    def start_replication(self, sg1_master, sg2_master):
        sg1 = 'http://{}:4985/db'.format(sg1_master)
        sg2 = 'http://{}:4985/db'.format(sg2_master)

        channels = self.channel_list(int(self.sg_settings.channels))

        data = {
            "replication_id": "sgr1_push",
            "source": sg1,
            "target": sg2,
            "filter": "sync_gateway/bychannel",
            "query_params": {
                "channels": channels
            },
            "continuous": True,
            "changes_feed_limit": 10000
        }
        self.rest.start_sg_replication(sg1_master, data)

        data["replication_id"] = "sgr1_pull"
        data["source"] = sg2
        data["target"] = sg1
        self.rest.start_sg_replication(sg1_master, data)

    @with_stats
    @with_profiles
    def monitor_sg_replicate(self, replication_id, sg_master):
        expected_docs = self.test_config.load_settings.items * 2
        time_elapsed, items_in_range = self.monitor.monitor_sgreplicate(sg_master,
                                                                        expected_docs,
                                                                        replication_id,
                                                                        1)
        return time_elapsed, items_in_range

    def run(self):
        masters = self.cluster_spec.masters
        sg1_master = next(masters)
        sg2_master = next(masters)
        self.start_replication(sg1_master, sg2_master)
        time_elapsed, items_in_range = self.monitor_sg_replicate("sgr1_pushAndPull", sg1_master)
        self.report_kpi(time_elapsed, items_in_range)


class SGReplicateThroughputBidirectionalTest2(SGReplicateThroughputTest2):

    def start_replication(self, sg1_master, sg2_master):
        sg2 = 'http://{}:4985/db'.format(sg2_master)

        channels = self.channel_list(int(self.sg_settings.channels))

        data = {
            "replication_id": "sgr2_pushAndPull",
            "remote": sg2,
            "direction": "pushAndPull",
            "filter": "sync_gateway/bychannel",
            "query_params": {
                "channels": channels
            },
            "continuous": True
        }
        self.rest.start_sg_replication2(sg1_master, data)

    @with_stats
    @with_profiles
    def monitor_sg_replicate(self, replication_id, sg_master):
        expected_docs = self.test_config.load_settings.items * 2
        time_elapsed, items_in_range = self.monitor.monitor_sgreplicate(sg_master,
                                                                        expected_docs,
                                                                        replication_id,
                                                                        2)
        return time_elapsed, items_in_range

    def run(self):
        masters = self.cluster_spec.masters
        sg1_master = next(masters)
        sg2_master = next(masters)
        self.start_replication(sg1_master, sg2_master)
        time_elapsed, items_in_range = self.monitor_sg_replicate("sgr2_pushAndPull", sg1_master)
        self.report_kpi(time_elapsed, items_in_range)


class SGReplicateThroughputMultiChannelMultiSgTest1(SGReplicateThroughputTest1):

    def start_replication(self, sg1_master, sg2_master, channel):
        sg1 = 'http://{}:4985/db'.format(sg1_master)
        sg2 = 'http://{}:4985/db'.format(sg2_master)

        channels = [("channel-" + str(channel + 1))]
        replication_id = "sgr1_" + self.sg_settings.sg_replication_type + str(channel+1)
        data = {
            "replication_id": replication_id,
            "source": sg1,
            "target": sg2,
            "filter": "sync_gateway/bychannel",
            "query_params": {
                "channels": channels
            },
            "continuous": True,
            "changes_feed_limit": 10000
        }

        if self.sg_settings.sg_replication_type == 'push':
            self.rest.start_sg_replication(sg1_master, data)
        elif self.sg_settings.sg_replication_type == 'pull':
            self.rest.start_sg_replication(sg2_master, data)
        return data["replication_id"]

    @with_stats
    @with_profiles
    def monitor_sg_replicate(self, replication_id, sg_master):
        expected_docs = self.test_config.load_settings.items
        time_elapsed, items_in_range = self.monitor.monitor_sgreplicate(sg_master,
                                                                        expected_docs,
                                                                        replication_id,
                                                                        1)
        return time_elapsed, items_in_range

    def run(self):
        sg1_node = []
        sg2_node = []
        replication_ids = []
        self.remote.remove_sglogs()
        self.download_ycsb()
        self.start_memcached()
        self.load_docs()
        for node in range(int(self.sg_settings.nodes)):
            sg1 = self.cluster_spec.sgw_servers[node]
            nodes_per_cluster = int(self.sg_settings.nodes) + \
                int(self.test_config.cluster.initial_nodes[0])
            sg2 = self.cluster_spec.sgw_servers[node+nodes_per_cluster]
            replication_id = self.start_replication(sg1, sg2, node)
            sg1_node.append(sg1)
            sg2_node.append(sg2)
            replication_ids.append(replication_id)
        if self.sg_settings.sg_replication_type == 'push':
            time_elapsed, items_in_range = self.monitor_sg_replicate(replication_ids, sg1_node)
        elif self.sg_settings.sg_replication_type == 'pull':
            time_elapsed, items_in_range = self.monitor_sg_replicate(replication_ids, sg2_node)
        self.report_kpi(time_elapsed, items_in_range)


class SGReplicateThroughputMultiChannelMultiSgTest2(SGReplicateThroughputTest2):

    def start_replication(self, sg1_master, sg2_master, channel):
        sg1 = 'http://{}:4985/db'.format(sg1_master)
        sg2 = 'http://{}:4985/db'.format(sg2_master)

        channels = [("channel-" + str(channel+1))]
        replication_id = "sgr2_" + self.sg_settings.sg_replication_type + str(channel+1)
        data = {
            "replication_id": replication_id,
            "remote": sg2,
            "direction": "push",
            "filter": "sync_gateway/bychannel",
            "query_params": {
                "channels": channels
            },
            "continuous": True
        }

        if self.sg_settings.sg_replication_type == 'push':
            self.rest.start_sg_replication2(sg1_master, data)
        elif self.sg_settings.sg_replication_type == 'pull':
            data["direction"] = "pull"
            data["remote"] = sg1
            self.rest.start_sg_replication2(sg2_master, data)
        return data["replication_id"]

    def monitor_sg_replicate(self, replication_id, sg_master):
        expected_docs = self.test_config.load_settings.items
        time_elapsed, items_in_range = self.monitor.monitor_sgreplicate(sg_master,
                                                                        expected_docs,
                                                                        replication_id,
                                                                        2)
        return time_elapsed, items_in_range

    @with_stats
    @with_profiles
    def run_replicate(self):
        sleep(30)
        sg1_nodes = []
        sg2_nodes = []
        replication_ids = []
        for node in range(int(self.sg_settings.nodes)):
            sg1 = self.cluster_spec.sgw_servers[node]
            logger.info("sg1 stats: {}".format(self.rest.get_sgreplicate_stats(sg1, 2)))
            nodes_per_cluster = int(self.sg_settings.nodes) + \
                int(self.test_config.cluster.initial_nodes[0])
            sg2 = self.cluster_spec.sgw_servers[node+nodes_per_cluster]
            replication_id = self.start_replication(sg1, sg2, node)
            sg1_nodes.append(sg1)
            sg2_nodes.append(sg2)
            replication_ids.append(replication_id)
        if self.sg_settings.sg_replication_type == 'push':
            time_elapsed, items_in_range = self.monitor_sg_replicate(replication_ids, sg1_nodes)
        elif self.sg_settings.sg_replication_type == 'pull':
            time_elapsed, items_in_range = self.monitor_sg_replicate(replication_ids, sg2_nodes)
        return time_elapsed, items_in_range

    def run(self):
        self.remote.remove_sglogs()
        self.download_ycsb()
        self.start_memcached()
        self.load_docs()
        time_elapsed, items_in_range = self.run_replicate()
        self.report_kpi(time_elapsed, items_in_range)


class SGReplicateMultiCluster(SGPerfTest):

    def download_blockholepuller_tool(self):
        if self.worker_manager.is_remote:
            logger.info('printing elf.worker_manager.WORKER_HOME{}'.format(
                self.worker_manager.WORKER_HOME))
            self.remote.download_blackholepuller(worker_home=self.worker_manager.WORKER_HOME)
        else:
            local.download_blockholepuller()

    def execute_multicluster_pull(self, clinets, timeout):
        result_path = "/tmp/perfrunner/perfrunner/results.json"
        self.remote.execute_blockholepuller(clients=clinets,
                                            timeout=timeout,
                                            result_path=result_path)

    def collect_execution_logs(self):
        if self.worker_manager.is_remote:

            logger.info('removing existing log & stderr files')
            local.remove_sg_bp_logs()
            self.remote.get_sg_blackholepuller_logs(self.worker_manager.WORKER_HOME,
                                                    self.test_config.syncgateway_settings)

    def run_sg_bp_phase(self,
                        phase: str,
                        task: Callable, settings: PhaseSettings,
                        timer: int = None,
                        distribute: bool = False) -> None:
        self.worker_manager.run_sg_bp_tasks(task, settings, timer, distribute, phase)
        self.worker_manager.wait_for_workers()

    @with_stats
    @with_profiles
    def run_bp_test(self):
        self.run_sg_bp_phase(" blackholepuller test", syncgateway_bh_puller_task,
                             self.settings, self.settings.time, True)

    def _report_kpi(self):
        self.collect_execution_logs()
        for f in glob.glob('*sg_stats_blackholepuller_*.json'):
            with open(f, 'r') as fout:
                logger.info(f)
                logger.info(fout.read())

        self.reporter.post(
            *self.metrics.sg_bp_throughput("Average docs/sec per client")
        )

        duration = int(self.test_config.syncgateway_settings.sg_blackholepuller_timeout)

        self.reporter.post(
            *self.metrics.sg_bp_total_docs_pulled(title="Total docs pulled per second",
                                                  duration=duration)
        )

    def run(self):

        self.remote.remove_sglogs()
        self.download_ycsb()
        self.start_memcached()
        self.load_users()
        self.load_docs()
        self.init_users()
        self.grant_access()
        self.download_blockholepuller_tool()
        self.run_bp_test()
        self.report_kpi()


class SGReplicateMultiClusterPush(SGPerfTest):

    def download_newdocpusher_tool(self):
        if self.worker_manager.is_remote:
            logger.info('printing elf.worker_manager.WORKER_HOME{}'.format(
                self.worker_manager.WORKER_HOME))
            self.remote.download_newdocpusher(worker_home=self.worker_manager.WORKER_HOME)
        else:
            local.download_newdocpusher()

    def execute_newdocpush(self, clinets, timeout):
        result_path = "/tmp/perfrunner/perfrunner/results.json"
        self.remote.execute_blockholepuller(clients=clinets,
                                            timeout=timeout,
                                            result_path=result_path)

    def collect_execution_logs(self):
        if self.worker_manager.is_remote:

            logger.info('removing existing log & stderr files')
            local.remove_sg_newdocpusher_logs()
            self.remote.get_sg_newdocpusher_logs(self.worker_manager.WORKER_HOME,
                                                 self.test_config.syncgateway_settings)

    def run_sg_docpush_phase(self,
                             phase: str,
                             task: Callable, settings: PhaseSettings,
                             timer: int = None,
                             distribute: bool = False) -> None:
        self.worker_manager.run_sg_bp_tasks(task, settings, timer, distribute, phase)
        self.worker_manager.wait_for_workers()

    @with_stats
    @with_profiles
    def run_bp_test(self):
        self.run_sg_docpush_phase(" newDocpusher test", syncgateway_new_docpush_task,
                                  self.settings, self.settings.time, True)

    def _report_kpi(self):
        self.collect_execution_logs()
        for f in glob.glob('*sg_stats_newdocpusher_*.json'):
            with open(f, 'r') as fout:
                logger.info(f)
                logger.info(fout.read())

        self.reporter.post(
            *self.metrics.sg_newdocpush_throughput("Average docs/sec per client")
        )

        duration = int(self.test_config.syncgateway_settings.sg_blackholepuller_timeout)

        self.reporter.post(
            *self.metrics.sg_bp_total_docs_pushed(title="Total docs pushed per second",
                                                  duration=duration)
        )

    def run(self):
        self.remote.remove_sglogs()
        self.download_ycsb()
        self.start_memcached()
        self.load_users()
        self.download_newdocpusher_tool()
        self.run_bp_test()
        self.report_kpi()


class DeltaSync(SGPerfTest):

    def load_docs(self):
        self.run_sg_phase(
            "load docs",
            syncgateway_delta_sync_task_load_docs,
            self.settings,
            self.settings.time,
            distribute=False
        )

    @with_stats
    def run_test(self):
        self.run_sg_phase(
            "run test",
            syncgateway_delta_sync_task_run_test,
            self.settings,
            self.settings.time,
            distribute=True
        )

    def start_cblite(self, port: str, db_name: str):
        local.start_cblitedb(port=port, db_name=db_name)

    @with_stats
    @with_profiles
    def cblite_replicate(self, cblite_db: str):
        replication_type = self.test_config.syncgateway_settings.replication_type
        sgw_ip = list(self.cluster_spec.sgw_masters)[0]
        if replication_type == 'PUSH':
            ret_str = local.replicate_push(cblite_db, sgw_ip)
        elif replication_type == 'PULL':
            ret_str = local.replicate_pull(cblite_db, sgw_ip)
        else:
            raise Exception("incorrect replication type: {}".format(replication_type))

        if ret_str.find('Completed'):
            logger.info('cblite message:{}'.format(ret_str))
            replication_time = float((re.search('docs in (.*) secs;', ret_str)).group(1))
            docs_replicated = int((re.search('Completed (.*) docs in', ret_str)).group(1))
            if docs_replicated == int(self.test_config.syncgateway_settings.documents):
                success_code = 'SUCCESS'
            else:
                success_code = 'FAILED'
                logger.info(
                    "Replication failed due to partial replication. Number of docs replicated: {}".
                    format(docs_replicated)
                )
        else:
            logger.info('Replication failed with error message:{}'.format(ret_str))
            replication_time = 0
            docs_replicated = 0
            success_code = 'FAILED'
        return replication_time, docs_replicated, success_code

    def _report_kpi(self,
                    replication_time: float,
                    throughput: int,
                    bandwidth: float,
                    synced_bytes: float,
                    field_length: str):
        self.collect_execution_logs()
        for f in glob.glob('{}/*runtest*.result'.format(self.LOCAL_DIR)):
            with open(f, 'r') as fout:
                logger.info(f)
                logger.info(fout.read())
        self.reporter.post(
            *self.metrics.deltasync_time(
                replication_time=replication_time,
                field_length=field_length
            )
        )

        self.reporter.post(
            *self.metrics.deltasync_throughput(throughput=throughput, field_length=field_length)
        )

        self.reporter.post(
            *self.metrics.deltasync_bytes(bytes=synced_bytes, field_length=field_length)
        )

    def db_cleanup(self):
        local.cleanup_cblite_db()

    def post_deltastats(self):
        sg_server = self.cluster_spec.sgw_servers[0]
        stats = self.monitor.deltasync_stats(host=sg_server)
        logger.info('Sync-gateway Stats:{}'.format(stats))

    def calc_bandwidth_usage(self, synced_bytes: float, time_taken: float):
        # in Mb
        bandwidth = round((((synced_bytes/time_taken)/1024)/1024), 2)
        return bandwidth

    def get_bytes_transfer(self):
        sg_server = self.cluster_spec.sgw_servers[0]
        bytes_transferred = self.monitor.deltasync_bytes_transfer(host=sg_server)
        return bytes_transferred

    def run(self):
        self.download_ycsb()

        if self.test_config.syncgateway_settings.deltasync_cachehit_ratio == '100':
            self.start_cblite(port='4985', db_name='db1')
            self.start_cblite(port='4986', db_name='db2')
        else:
            self.start_cblite(port='4985', db_name='db')
        self.start_memcached()
        self.load_docs()
        sleep(20)
        if self.test_config.syncgateway_settings.deltasync_cachehit_ratio == '100':
            self.cblite_replicate(cblite_db='db1')
            self.cblite_replicate(cblite_db='db2')
        else:
            self.cblite_replicate(cblite_db='db')
        self.post_deltastats()
        self.run_test()

        if self.test_config.syncgateway_settings.deltasync_cachehit_ratio == '100':
            self.cblite_replicate(cblite_db='db1')
            bytes_transferred_1 = self.get_bytes_transfer()
            replication_time, docs_replicated, success_code = self.cblite_replicate(cblite_db='db2')
        else:
            bytes_transferred_1 = self.get_bytes_transfer()
            replication_time, docs_replicated, success_code = self.cblite_replicate(cblite_db='db')

        if success_code == 'SUCCESS':
            self.post_deltastats()
            bytes_transferred_2 = self.get_bytes_transfer()
            byte_transfer = bytes_transferred_2 - bytes_transferred_1
            bandwidth = self.calc_bandwidth_usage(
                synced_bytes=byte_transfer,
                time_taken=replication_time
            )
            throughput = int(docs_replicated/replication_time)
            field_length = str(self.test_config.syncgateway_settings.fieldlength)
            self.report_kpi(replication_time, throughput, bandwidth, byte_transfer, field_length)

            self.db_cleanup()
        else:
            self.db_cleanup()
        self.compress_sg_logs()
        self.get_sg_logs()


class DeltaSyncParallel(DeltaSync):

    def generate_dbmap(self, num_dbs: int):
        db_map = {}
        for i in range(num_dbs):
            port = str(4985 + i)
            db_name = 'db' + str(i)
            db_map.update({port: db_name})
        return db_map

    def start_multiplecblite(self, db_map: map):
        cblite_dbs = []
        for key in db_map:
            local.start_cblitedb(port=key, db_name=db_map[key])
            cblite_dbs.append(db_map[key])
        return cblite_dbs

    @with_stats
    @with_profiles
    def multiple_replicate(self, num_agents: int, cblite_dbs: list):
        with Pool(processes=num_agents) as pool:
            logger.info('starting cb replicate parallel with {}'.format(num_agents))
            results = pool.map(func=self.cblite_replicate, iterable=cblite_dbs)
        logger.info('end of multiple replicate: {}'.format(results))
        return results

    def cblite_replicate(self, cblite_db: str):
        replication_type = self.test_config.syncgateway_settings.replication_type
        sgw_ip = list(self.cluster_spec.sgw_masters)[0]
        if replication_type == 'PUSH':
            ret_str = local.replicate_push(cblite_db, sgw_ip)
        elif replication_type == 'PULL':
            ret_str = local.replicate_pull(cblite_db, sgw_ip)
        else:
            raise Exception("incorrect replication type: {}".format(replication_type))

        if ret_str.find('Completed'):
            logger.info('cblite message: {}'.format(ret_str))
            replication_time = float((re.search('docs in (.*) secs;', ret_str)).group(1))
            docs_replicated = int((re.search('Completed (.*) docs in', ret_str)).group(1))
            if docs_replicated == int(self.test_config.syncgateway_settings.documents):
                success_code = 'SUCCESS'
            else:
                success_code = 'FAILED'
                logger.info(
                    "Replication failed due to partial replication. Number of docs replicated: {}".
                    format(docs_replicated)
                )
        else:
            logger.info('Replication failed with error message:{}'.format(ret_str))
            replication_time = 0
            docs_replicated = 0
            success_code = 'FAILED'
        return replication_time, docs_replicated, success_code

    def get_average_time(self, results: list):
        tmp_sum = 0
        for result in results:
            tmp_sum = tmp_sum + result[0]
        average = tmp_sum/len(results)
        return average

    def get_docs_replicated(self, results: list):
        doc_count = 0
        for result in results:
            doc_count = doc_count + result[1]
        return doc_count

    def check_success(self, results: list):
        success_count = 0
        for result in results:
            if result[2] == 'SUCCESS':
                success_count += 1
        logger.info('success_count :{}'.format(success_count))
        if success_count == len(results):
            return 1
        else:
            return 0

    def run(self):
        self.download_ycsb()
        self.start_memcached()
        num_dbs = int(self.test_config.syncgateway_settings.replication_concurrency)
        db_map = self.generate_dbmap(num_dbs)
        cblite_dbs = self.start_multiplecblite(db_map)

        if self.test_config.syncgateway_settings.deltasync_cachehit_ratio == '100':
            self.start_cblite(port='4983', db_name='db')

        self.load_docs()

        if self.test_config.syncgateway_settings.deltasync_cachehit_ratio == '100':
            self.cblite_replicate(cblite_db='db')

        num_agents = len(cblite_dbs)

        self.multiple_replicate(num_agents, cblite_dbs)

        bytes_transferred_1 = self.get_bytes_transfer()
        self.post_deltastats()

        self.run_test()

        if self.test_config.syncgateway_settings.deltasync_cachehit_ratio == '100':
            self.cblite_replicate(cblite_db='db')
            bytes_transferred_1 = self.get_bytes_transfer()

        results = self.multiple_replicate(num_agents, cblite_dbs)

        if self.check_success(results) == 1:
            self.post_deltastats()
            bytes_transferred_2 = self.get_bytes_transfer()
            byte_transfer = bytes_transferred_2 - bytes_transferred_1
            average_time = self.get_average_time(results)

            docs_replicated = self.get_docs_replicated(results)

            field_length = str(self.test_config.syncgateway_settings.fieldlength)

            throughput = int(docs_replicated/average_time)
            bandwidth = self.calc_bandwidth_usage(
                synced_bytes=byte_transfer,
                time_taken=average_time
            )

            self.report_kpi(average_time, throughput, bandwidth, byte_transfer, field_length)

            self.db_cleanup()
        else:
            self.db_cleanup()
        self.compress_sg_logs()
        self.get_sg_logs()


class EndToEndTest(SGPerfTest):

    def start_continuous_replication(self, db):
        replication_type = self.test_config.syncgateway_settings.replication_type
        sgw_ip = list(self.cluster_spec.sgw_masters)[0]
        if replication_type == 'E2E_PUSH':
            local.replicate_push_continuous(db, sgw_ip)
        elif replication_type == 'E2E_PULL':
            local.replicate_pull_continuous(db, sgw_ip)
        else:
            raise Exception(
                "replication type must be either E2E_PUSH or E2E_PULL: {}".format(replication_type)
            )

    def wait_for_docs_pushed(self, initial_docs, target_docs):
        sgw_servers = self.settings.syncgateway_settings.nodes
        sg_servers = self.cluster_spec.sgw_servers[0:sgw_servers]
        sgw_t0, start_push_count = self.monitor.wait_sgw_push_start(sg_servers, initial_docs)
        logger.info("waiting for push complete...")
        sgw_t1, end_push_count = self.monitor.wait_sgw_push_docs(sg_servers,
                                                                 initial_docs+target_docs)
        sgw_time = sgw_t1 - sgw_t0
        observed_pushed = end_push_count-start_push_count
        logger.info("sgw_time: {}, observed_pushed: {}, throughput: {}"
                    .format(sgw_time, observed_pushed, observed_pushed/sgw_time))
        return sgw_time, observed_pushed

    def wait_for_docs_pulled(self, initial_docs, target_docs):
        sgw_servers = self.settings.syncgateway_settings.nodes
        sg_servers = self.cluster_spec.sgw_servers[0:sgw_servers]
        sgw_t0, start_pull_count = self.monitor.wait_sgw_pull_start(sg_servers, initial_docs)
        logger.info("waiting for pull complete...")
        sgw_t1, end_pull_count = self.monitor.wait_sgw_pull_docs(sg_servers,
                                                                 initial_docs+target_docs)
        sgw_time = sgw_t1 - sgw_t0
        observed_pulled = end_pull_count - start_pull_count
        logger.info("sgw_time: {}, observed_pulled: {}, throughput: {}"
                    .format(sgw_time, observed_pulled, observed_pulled/sgw_time))
        return sgw_time, observed_pulled

    def post_delta_stats(self):
        sg_server = self.cluster_spec.sgw_servers[0]
        stats = self.monitor.deltasync_stats(host=sg_server)
        logger.info('Sync-gateway Stats: {}'.format(pretty_dict(stats)))
        return stats

    def print_ycsb_logs(self):
        for f in glob.glob('{}/*runtest*.result'.format(self.LOCAL_DIR)):
            with open(f, 'r') as f_out:
                logger.info(f)
                logger.info(f_out.read())

    def _report_kpi(self, sgw_load_tp: int, sgw_access_tp: int):
        field_length = str(self.test_config.syncgateway_settings.fieldlength)
        self.reporter.post(
            *self.metrics.sgw_e2e_throughput(
                throughput=sgw_load_tp,
                field_length=field_length,
                operation="INSERT",
                replication=self.test_config.syncgateway_settings.replication_type
            )
        )
        self.reporter.post(
            *self.metrics.sgw_e2e_throughput(
                throughput=sgw_access_tp,
                field_length=field_length,
                operation="UPDATE",
                replication=self.test_config.syncgateway_settings.replication_type
            )
        )

    @with_stats
    def e2e_cbl_load_bg(self, pre_load_writes, load_docs):
        settings = copy.deepcopy(self.settings)
        settings.syncgateway_settings.clients = \
            self.settings.syncgateway_settings.load_clients
        settings.syncgateway_settings.threads = \
            self.settings.syncgateway_settings.load_threads
        settings.syncgateway_settings.instances_per_client = \
            self.settings.syncgateway_settings.load_instances_per_client
        self.run_sg_phase(
            phase="load phase",
            task=syncgateway_e2e_cbl_task_load_docs,
            settings=self.settings,
            timer=self.settings.time,
            distribute=True,
            wait=False
        )
        sgw_load_time, observed_pushed = \
            self.wait_for_docs_pushed(
                initial_docs=pre_load_writes,
                target_docs=load_docs
            )
        return sgw_load_time, observed_pushed

    @with_stats
    def e2e_cbl_update_bg(self, post_load_writes, load_docs):
        self.run_sg_phase(
            phase="access phase",
            task=syncgateway_e2e_cbl_task_run_test,
            settings=self.settings,
            timer=self.settings.time,
            distribute=True,
            wait=False
        )
        sgw_access_time, observed_pushed = \
            self.wait_for_docs_pushed(
                initial_docs=post_load_writes,
                target_docs=int(load_docs*0.9)
            )
        self.worker_manager.wait_for_workers()
        return sgw_access_time, observed_pushed

    @with_stats
    def e2e_cb_load_bg(self, pre_load_writes, load_docs):
        cb_target_iterator = CBTargetIterator(self.cluster_spec,
                                              self.test_config,
                                              prefix='symmetric')
        super().load_bg(task=ycsb_data_load_task, target_iterator=cb_target_iterator)
        sgw_load_time = \
            self.wait_for_docs_pulled(
                initial_docs=pre_load_writes,
                target_docs=load_docs
            )
        return sgw_load_time

    @with_stats
    def e2e_cb_update_bg(self, post_load_writes, load_docs):
        cb_target_iterator = CBTargetIterator(self.cluster_spec,
                                              self.test_config,
                                              prefix='symmetric')
        super().access_bg(task=ycsb_task, target_iterator=cb_target_iterator)
        sgw_access_time = \
            self.wait_for_docs_pulled(
                initial_docs=post_load_writes,
                target_docs=load_docs
            )
        return sgw_access_time


class EndToEndSingleCBLTest(EndToEndTest):

    def run_push(self):
        pass

    def run_pull(self):
        pass

    def run_bidi(self):
        pass

    def run(self):
        try:
            local.kill_cblite()
        except Exception as e:
            print(str(e))
        self.download_ycsb()
        local.clone_cblite()
        local.build_cblite()
        local.cleanup_cblite_db()
        local.start_cblitedb_continuous(port='4985', db_name='db')
        self.start_continuous_replication('db')
        self.start_memcached()
        replication_type = self.test_config.syncgateway_settings.replication_type
        if replication_type == 'E2E_PUSH':
            self.run_push()
        elif replication_type == 'E2E_PULL':
            self.run_pull()
        elif replication_type == "E2E_BIDI":
            self.run_bidi()
        else:
            raise Exception(
                "Replication type must be "
                "E2E_PUSH, E2E_PULL or E2E_BIDI: "
                "{}".format(replication_type)
            )


class EndToEndSingleCBLPushTest(EndToEndSingleCBLTest):

    def run_push(self):
        load_docs = int(self.settings.syncgateway_settings.documents)
        pre_load_stats = self.post_delta_stats()
        pre_load_writes = int(pre_load_stats['db']['database']['num_doc_writes'])
        logger.info("initial writes: {}".format(pre_load_writes))

        sgw_load_time, observed_pushed_load = self.e2e_cbl_load_bg(pre_load_writes, load_docs)

        post_load_stats = self.post_delta_stats()
        post_load_writes = int(post_load_stats['db']['database']['num_doc_writes'])
        logger.info("post load writes: {}".format(post_load_writes))

        sgw_access_time, observed_pushed_access = self.e2e_cbl_update_bg(post_load_writes,
                                                                         load_docs)

        post_access_stats = self.post_delta_stats()
        post_access_writes = int(post_access_stats['db']['database']['num_doc_writes'])
        logger.info("post access writes: {}".format(post_access_writes))

        sgw_load_tp = observed_pushed_load / sgw_load_time
        sgw_access_tp = observed_pushed_access / sgw_access_time

        self.collect_execution_logs()
        self.print_ycsb_logs()

        self.report_kpi(sgw_load_tp, sgw_access_tp)
        local.cleanup_cblite_db_coninuous()
        self.compress_sg_logs()
        self.get_sg_logs()


class EndToEndSingleCBLPullTest(EndToEndSingleCBLTest):

    def download_ycsb(self):
        if self.worker_manager.is_remote:
            self.remote.clone_ycsb(repo=self.test_config.ycsb_settings.repo,
                                   branch=self.test_config.ycsb_settings.branch,
                                   worker_home=self.worker_manager.WORKER_HOME,
                                   ycsb_instances=1)
        else:
            local.clone_ycsb(repo=self.test_config.ycsb_settings.repo,
                             branch=self.test_config.ycsb_settings.branch)

    def run_pull(self):
        load_docs = int(self.settings.syncgateway_settings.documents)
        pre_load_stats = self.post_delta_stats()
        pre_load_writes = int(pre_load_stats['db']['database']['num_doc_writes'])
        logger.info("initial writes: {}".format(pre_load_writes))

        sgw_load_time = self.e2e_cb_load_bg(pre_load_writes, load_docs)

        post_load_stats = self.post_delta_stats()
        post_load_writes = int(post_load_stats['db']['database']['num_doc_writes'])
        logger.info("post load writes: {}".format(post_load_writes))

        sgw_access_time = self.e2e_cb_update_bg(post_load_writes)

        post_access_stats = self.post_delta_stats()
        post_access_writes = int(post_access_stats['db']['database']['num_doc_writes'])
        logger.info("post access writes: {}".format(post_access_writes))

        docs_accessed = post_access_writes - post_load_writes
        sgw_load_tp = load_docs / sgw_load_time
        sgw_access_tp = docs_accessed / sgw_access_time

        self.collect_execution_logs()
        self.print_ycsb_logs()

        self.report_kpi(sgw_load_tp, sgw_access_tp)
        local.cleanup_cblite_db_coninuous()
        self.compress_sg_logs()
        self.get_sg_logs()


class EndToEndSingleCBLBidiTest(EndToEndSingleCBLTest):

    def run_bidi(self):
        pass


class EndToEndMultiCBLTest(EndToEndTest):

    def get_cblite_info(self):
        for client in self.cluster_spec.workers[:int(self.settings.syncgateway_settings.clients)]:
            for instance_id in range(int(self.settings.syncgateway_settings.instances_per_client)):
                db_name = "db_{}".format(instance_id)
                port = 4985+instance_id
                info = self.rest.get_cblite_info(
                    client, port, db_name)
                logger.info("client: {}, port: {}, db: {}, \n info: {}".format(
                    client, port, db_name, info))

    def push_monitor(self, queue, pre_load_writes, load_docs):
        try:
            sgw_load_time, observed_pushed = \
                self.wait_for_docs_pushed(
                    initial_docs=pre_load_writes,
                    target_docs=load_docs
                )
            queue.put((sgw_load_time, observed_pushed, "push"))
        except Exception as ex:
            logger.info(str(ex))
            queue.put((None, None, "push"))

    def pull_monitor(self, queue, pre_load_writes, load_docs):
        try:
            sgw_load_time, observed_pulled = \
                self.wait_for_docs_pulled(
                    initial_docs=pre_load_writes,
                    target_docs=load_docs
                )
            queue.put((sgw_load_time, observed_pulled, "pull"))
        except Exception as ex:
            logger.info(str(ex))
            queue.put((None, None, "pull"))

    @with_stats
    def push_load(self, pre_load_writes, load_docs):
        # settings = copy.deepcopy(self.settings)
        queue = multiprocessing.Queue()
        p = multiprocessing.Process(target=self.push_monitor, args=(queue, pre_load_writes,
                                                                    load_docs))
        p.daemon = True
        p.start()
        try:
            self.run_sg_phase(
                phase="load phase",
                task=syncgateway_e2e_multi_cbl_task_load_docs,
                settings=self.settings,
                timer=self.settings.time,
                distribute=True,
                wait=True
            )
            ret = queue.get(timeout=600)
        except Exception as ex:
            logger.info(str(ex))
            p.terminate()
            p.join()
            self.get_cblite_info()
            self.collect_execution_logs()
            raise ex
        else:
            p.join()
            sgw_load_time = ret[0]
            observed_pushed = ret[1]
            if sgw_load_time and observed_pushed:
                return sgw_load_time, observed_pushed
            else:
                self.get_cblite_info()
                self.collect_execution_logs()
                # self.print_ycsb_logs()
                raise Exception("failed to load docs")

    @with_stats
    def push_update(self, post_load_writes, load_docs):
        queue = multiprocessing.Queue()
        p = multiprocessing.Process(target=self.push_monitor, args=(queue, post_load_writes,
                                    int(load_docs)))
        p.daemon = True
        p.start()
        try:
            self.run_sg_phase(
                phase="access phase",
                task=syncgateway_e2e_multi_cbl_task_run_test,
                settings=self.settings,
                timer=self.settings.time,
                distribute=True,
                wait=True
            )
            ret = queue.get(timeout=600)
        except Exception as ex:
            logger.info(str(ex))
            p.terminate()
            p.join()
            self.get_cblite_info()
            self.collect_execution_logs()
            raise ex
        else:
            p.join()
            sgw_load_time = ret[0]
            observed_pushed = ret[1]
            if sgw_load_time and observed_pushed:
                return sgw_load_time, observed_pushed
            else:
                self.get_cblite_info()
                self.collect_execution_logs()
                # self.print_ycsb_logs()
                raise Exception("failed to update docs")

    @with_stats
    def pull_load(self, pre_load_writes, load_docs):
        # settings = copy.deepcopy(self.settings)
        queue = multiprocessing.Queue()
        p = multiprocessing.Process(target=self.pull_monitor, args=(queue, pre_load_writes,
                                    load_docs))
        p.daemon = True
        p.start()
        try:
            self.run_sg_phase(
                phase="load phase",
                task=syncgateway_e2e_multi_cb_task_load_docs,
                settings=self.settings,
                timer=self.settings.time,
                distribute=True,
                wait=True
            )
            ret = queue.get(timeout=600)
        except Exception as ex:
            logger.info(str(ex))
            p.terminate()
            p.join()
            self.get_cblite_info()
            self.collect_execution_logs()
            raise ex
        else:
            p.join()
            sgw_load_time = ret[0]
            observed_pushed = ret[1]
            if sgw_load_time and observed_pushed:
                return sgw_load_time, observed_pushed
            else:
                self.get_cblite_info()
                self.collect_execution_logs()
                # self.print_ycsb_logs()
                raise Exception("failed to load docs")

    @with_stats
    def pull_update(self, post_load_writes, load_docs):
        queue = multiprocessing.Queue()
        p = multiprocessing.Process(target=self.pull_monitor, args=(queue, post_load_writes,
                                    int(load_docs)))
        p.daemon = True
        p.start()
        try:
            self.run_sg_phase(
                phase="access phase",
                task=syncgateway_e2e_multi_cb_task_run_test,
                settings=self.settings,
                timer=self.settings.time,
                distribute=True,
                wait=True
            )
            ret = queue.get(timeout=600)
        except Exception as ex:
            logger.info(str(ex))
            p.terminate()
            p.join()
            self.get_cblite_info()
            self.collect_execution_logs()
            raise ex
        else:
            p.join()
            sgw_load_time = ret[0]
            observed_pushed = ret[1]
            if sgw_load_time and observed_pushed:
                return sgw_load_time, observed_pushed
            else:
                self.get_cblite_info()
                self.collect_execution_logs()
                # self.print_ycsb_logs()
                raise Exception("failed to update docs")

    @with_stats
    def bidi_load(self, pre_load_writes, pre_load_reads, load_docs):
        settings = copy.deepcopy(self.settings)
        settings.syncgateway_settings.documents = load_docs // 2
        queue = multiprocessing.Queue()
        p = multiprocessing.Process(target=self.push_monitor, args=(queue, pre_load_writes,
                                                                    load_docs // 2))
        p.daemon = True
        p.start()
        q = multiprocessing.Process(target=self.pull_monitor, args=(queue, pre_load_reads,
                                                                    load_docs // 2))
        q.daemon = True
        q.start()
        try:
            self.run_sg_phase(
                phase="load phase",
                task=syncgateway_e2e_multi_cbl_task_load_docs,
                settings=settings,
                timer=self.settings.time,
                distribute=True,
                wait=False
            )
            self.run_sg_phase(
                phase="load phase",
                task=syncgateway_e2e_multi_cb_task_load_docs,
                settings=settings,
                timer=self.settings.time,
                distribute=True,
                wait=True
            )

            ret1 = queue.get(timeout=600)
            ret2 = queue.get(timeout=600)
        except Exception as ex:
            logger.info(str(ex))
            p.terminate()
            p.join()
            q.terminate()
            q.join()
            self.get_cblite_info()
            self.collect_execution_logs()
            raise ex
        else:
            p.join()
            q.join()
            if ret1[2] == "push":
                sgw_push_load_time = ret1[0]
                sgw_push_load_docs = ret1[1]
                sgw_pull_load_time = ret2[0]
                sgw_pull_load_docs = ret2[1]
            else:
                sgw_pull_load_time = ret1[0]
                sgw_pull_load_docs = ret1[1]
                sgw_push_load_time = ret2[0]
                sgw_push_load_docs = ret2[1]
            if sgw_push_load_time and sgw_push_load_docs and sgw_pull_load_time and \
                    sgw_pull_load_docs:
                return sgw_push_load_time, sgw_push_load_docs, sgw_pull_load_time, \
                       sgw_pull_load_docs
            else:
                logger.info("PUSH time: {}, PUSH docs: {}, PULL time: {}, PULL docs: {}".format(
                    sgw_push_load_time, sgw_push_load_docs, sgw_pull_load_time, sgw_pull_load_docs
                ))
                self.get_cblite_info()
                self.collect_execution_logs()
                # self.print_ycsb_logs()
                raise Exception("failed to load docs")

    @with_stats
    def bidi_update(self, post_load_writes, post_load_reads, load_docs):
        settings = copy.deepcopy(self.settings)
        settings.syncgateway_settings.documents = load_docs // 2
        queue = multiprocessing.Queue()
        p = multiprocessing.Process(target=self.push_monitor, args=(queue, post_load_writes,
                                                                    load_docs // 2))
        p.daemon = True
        p.start()
        q = multiprocessing.Process(target=self.pull_monitor, args=(queue, post_load_reads,
                                                                    load_docs // 2))
        q.daemon = True
        q.start()
        try:
            self.run_sg_phase(
                phase="access phase",
                task=syncgateway_e2e_multi_cbl_task_run_test,
                settings=settings,
                timer=self.settings.time,
                distribute=True,
                wait=False
            )
            self.run_sg_phase(
                phase="access phase",
                task=syncgateway_e2e_multi_cb_task_run_test,
                settings=settings,
                timer=self.settings.time,
                distribute=True,
                wait=True
            )

            ret1 = queue.get(timeout=600)
            ret2 = queue.get(timeout=600)
        except Exception as ex:
            logger.info(str(ex))
            p.terminate()
            p.join()
            q.terminate()
            q.join()
            self.get_cblite_info()
            self.collect_execution_logs()
            raise ex
        else:
            p.join()
            q.join()
            if ret1[2] == "push":
                sgw_push_load_time = ret1[0]
                sgw_push_load_docs = ret1[1]
                sgw_pull_load_time = ret2[0]
                sgw_pull_load_docs = ret2[1]
            else:
                sgw_pull_load_time = ret1[0]
                sgw_pull_load_docs = ret1[1]
                sgw_push_load_time = ret2[0]
                sgw_push_load_docs = ret2[1]
            if sgw_push_load_time and sgw_push_load_docs and sgw_pull_load_time and \
                    sgw_pull_load_docs:
                return sgw_push_load_time, sgw_push_load_docs, sgw_pull_load_time, \
                    sgw_pull_load_docs
            else:
                logger.info("PUSH time: {}, PUSH docs: {}, PULL time: {}, PULL docs: {}".format(
                    sgw_push_load_time, sgw_push_load_docs, sgw_pull_load_time, sgw_pull_load_docs
                ))
                self.get_cblite_info()
                self.collect_execution_logs()
                # self.print_ycsb_logs()
                raise Exception("failed to update docs")

    def start_multi_cblitedb_continuous(self):
        verbose = self.settings.syncgateway_settings.cbl_verbose_logging
        for instance_id in range(int(self.settings.syncgateway_settings.instances_per_client)):
            for client in self.cluster_spec.workers[:int(
                                                    self.settings.syncgateway_settings.clients)]:
                port = 4985 + instance_id
                db_name = "db_{}".format(instance_id)
                self.remote.start_cblitedb_continuous(client, db_name, port, verbose)

    def start_multi_continuous_replication(self):
        replication_type = self.test_config.syncgateway_settings.replication_type
        sgw_ip = list(self.cluster_spec.sgw_masters)[0]

        logger.info("The sgw_ip is: {}".format(sgw_ip))
        total_users = int(self.settings.syncgateway_settings.users)
        user_id = 0
        logger.info("The number of users is: {}".format(total_users))
        for instance_id in range(int(self.settings.syncgateway_settings.instances_per_client)):
            for client in self.cluster_spec.workers[:int(
                                                    self.settings.syncgateway_settings.clients)]:
                db_name = "db_{}".format(instance_id)
                user_num = user_id % total_users
                user_id += 1
                if self.settings.syncgateway_settings.replication_auth:
                    username = "sg-user-{}".format(user_num)
                    password = "password" if total_users > 1 else "guest"
                else:
                    username = None
                    password = None
                port = 4985+instance_id
                if replication_type == 'E2E_PUSH':
                    sgw_port = 4900 if self.test_config.syncgateway_settings.troublemaker else 4984
                    self.rest.start_cblite_replication_push(
                        client, port, db_name, sgw_ip, sgw_port, username, password)
                elif replication_type == 'E2E_PULL':
                    self.rest.start_cblite_replication_pull(
                        client, port, db_name, sgw_ip, username, password)
                elif replication_type == 'E2E_BIDI':
                    self.rest.start_cblite_replication_bidi(
                        client, port, db_name, sgw_ip, username, password)
                else:
                    raise Exception(
                        "replication type must be either E2E_PUSH or E2E_PULL: {}".
                        format(replication_type)
                    )

    def setup_cblite(self):
        try:
            self.remote.create_cblite_directory()
        except Exception as ex:
            logger.info("{}".format(ex))
        try:
            self.remote.create_cblite_ramdisk(self.settings.syncgateway_settings.ramdisk_size)
        except Exception as ex:
            logger.info("{}".format(ex))
        try:
            self.remote.clone_cblite()
        except Exception as ex:
            logger.info("{}".format(ex))
        self.remote.build_cblite()

    def run_push(self):
        pass

    def run_pull(self):
        pass

    def run_bidi(self):
        pass

    def run(self):
        try:
            self.remote.kill_cblite()
        except Exception as e:
            print(str(e))
        self.remote.modify_tcp_settings()
        self.download_ycsb()
        self.remote.build_syncgateway_ycsb(
            worker_home=self.worker_manager.WORKER_HOME,
            ycsb_instances=int(self.test_config.syncgateway_settings.instances_per_client))
        self.setup_cblite()
        self.start_memcached()
        self.load_users()
        self.init_users()
        self.grant_access()
        self.start_multi_cblitedb_continuous()
        logger.info("CBLites are started")
        if self.test_config.syncgateway_settings.troublemaker:
            logger.info("Troublemaker is building")
            self.remote.build_troublemaker()
            logger.info("Troublemaker is successfully built")
        self.start_multi_continuous_replication()
        logger.info("Replication started")
        replication_type = self.test_config.syncgateway_settings.replication_type
        if replication_type == 'E2E_PUSH':
            self.run_push()
        elif replication_type == 'E2E_PULL':
            self.run_pull()
        elif replication_type == "E2E_BIDI":
            self.run_bidi()
        else:
            raise Exception(
                "Replication type must be "
                "E2E_PUSH, E2E_PULL or E2E_BIDI: "
                "{}".format(replication_type)
            )


class EndToEndMultiCBLPushTest(EndToEndMultiCBLTest):

    def run_push(self):
        logger.info("Loading Docs")
        load_docs = int(self.settings.syncgateway_settings.documents)
        pre_load_stats = self.post_delta_stats()
        pre_load_writes = \
            int(pre_load_stats['db']['cbl_replication_push']['doc_push_count'])
        logger.info("initial pushed: {}".format(pre_load_writes))

        sgw_load_time, observed_pushed_load = self.push_load(pre_load_writes, load_docs)

        post_load_stats = self.post_delta_stats()
        post_load_writes = \
            int(post_load_stats['db']['cbl_replication_push']['doc_push_count'])
        logger.info("post load pushed: {}".format(post_load_writes))

        sgw_access_time, observed_pushed_access = self.push_update(post_load_writes, load_docs)

        post_access_stats = self.post_delta_stats()
        post_access_writes = \
            int(post_access_stats['db']['cbl_replication_push']['doc_push_count'])
        logger.info("post access pushed: {}".format(post_access_writes))

        sgw_load_tp = observed_pushed_load / sgw_load_time
        sgw_access_tp = observed_pushed_access / sgw_access_time

        self.collect_execution_logs()
        # self.print_ycsb_logs()
        self.report_kpi(sgw_load_tp, sgw_access_tp)


class EndToEndMultiCBLPullTest(EndToEndMultiCBLTest):

    def run_pull(self):
        logger.info("Loading Docs")
        load_docs = int(self.settings.syncgateway_settings.documents)
        pre_load_stats = self.post_delta_stats()
        pre_load_reads = \
            int(pre_load_stats['db']['cbl_replication_pull']['rev_send_count'])
        logger.info("initial pulled: {}".format(pre_load_reads))

        sgw_load_time, observed_pulled_load = self.pull_load(pre_load_reads, load_docs)

        post_load_stats = self.post_delta_stats()
        post_load_reads = \
            int(post_load_stats['db']['cbl_replication_pull']['rev_send_count'])
        logger.info("post load pulled: {}".format(post_load_reads))

        sgw_access_time, observed_pulled_access = self.pull_update(post_load_reads, load_docs)

        post_access_stats = self.post_delta_stats()
        post_access_reads = \
            int(post_access_stats['db']['cbl_replication_pull']['rev_send_count'])
        logger.info("post access pulled: {}".format(post_access_reads))

        sgw_load_tp = observed_pulled_load / sgw_load_time
        sgw_access_tp = observed_pulled_access / sgw_access_time

        self.collect_execution_logs()
        # self.print_ycsb_logs()
        self.report_kpi(sgw_load_tp, sgw_access_tp)


class EndToEndMultiCBLBidiTest(EndToEndMultiCBLTest):

    def _report_kpi(self, sgw_load_tp: int, operation: str):
        field_length = str(self.test_config.syncgateway_settings.fieldlength)
        self.reporter.post(
            *self.metrics.sgw_e2e_throughput(
                throughput=sgw_load_tp,
                field_length=field_length,
                operation=operation,
                replication=self.test_config.syncgateway_settings.replication_type
            )
        )

    def run_bidi(self):
        load_docs = int(self.settings.syncgateway_settings.documents)
        pre_load_stats = self.post_delta_stats()

        pre_load_writes = \
            int(pre_load_stats['db']['cbl_replication_push']['doc_push_count'])
        logger.info("initial pushed: {}".format(pre_load_writes))

        pre_load_reads = \
            int(pre_load_stats['db']['cbl_replication_pull']['rev_send_count'])
        logger.info("initial pulled: {}".format(pre_load_reads))

        sgw_push_load_time, sgw_push_load_docs, sgw_pull_load_time, sgw_pull_load_docs = \
            self.bidi_load(pre_load_writes, pre_load_reads, load_docs)

        post_load_stats = self.post_delta_stats()
        post_load_writes = \
            int(post_load_stats['db']['cbl_replication_push']['doc_push_count'])
        logger.info("post load pushed: {}".format(post_load_writes))
        post_load_reads = \
            int(post_load_stats['db']['cbl_replication_pull']['rev_send_count'])
        logger.info("post load pulled: {}".format(post_load_reads))

        sgw_push_access_time, sgw_push_access_docs, sgw_pull_access_time, sgw_pull_access_docs = \
            self.bidi_update(post_load_writes, post_load_reads, load_docs)

        post_access_stats = self.post_delta_stats()
        post_access_writes = \
            int(post_access_stats['db']['cbl_replication_push']['doc_push_count'])
        logger.info("post access pushed: {}".format(post_access_writes))
        post_access_reads = \
            int(post_access_stats['db']['cbl_replication_pull']['rev_send_count'])
        logger.info("post access pulled: {}".format(post_access_reads))

        sgw_push_load_tp = sgw_push_load_docs / sgw_push_load_time
        sgw_push_access_tp = sgw_push_access_docs / sgw_push_access_time
        sgw_pull_load_tp = sgw_pull_load_docs / sgw_pull_load_time
        sgw_pull_access_tp = sgw_pull_access_docs / sgw_pull_access_time

        self.collect_execution_logs()
        # self.print_ycsb_logs()
        self.report_kpi(sgw_push_load_tp, "PUSH_INSERT")
        self.report_kpi(sgw_pull_load_tp, "PULL_INSERT")
        self.report_kpi(sgw_push_access_tp, "PUSH_UPDATE")
        self.report_kpi(sgw_pull_access_tp, "PULL_UPDATE")
