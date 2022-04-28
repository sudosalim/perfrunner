import os
import signal
import sys
import time
from itertools import cycle
from multiprocessing import set_start_method
from typing import Callable, Iterable

from celery import Celery, group
from kombu.serialization import registry
from sqlalchemy import create_engine

from logger import logger
from perfrunner import celerylocal, celeryremote
from perfrunner.helpers import local, misc
from perfrunner.helpers.remote import RemoteHelper
from perfrunner.settings import (
    ClusterSpec,
    PhaseSettings,
    TargetSettings,
    TestConfig,
)
from perfrunner.workloads import spring_workload
from perfrunner.workloads.blackholepuller import (
    blackholepuller_runtest,
    newdocpusher_runtest,
)
from perfrunner.workloads.dcp import java_dcp_client
from perfrunner.workloads.jts import jts_run, jts_warmup
from perfrunner.workloads.pillowfight import (
    pillowfight_data_load,
    pillowfight_workload,
)
from perfrunner.workloads.syncgateway import (
    syncgateway_delta_sync_load_docs,
    syncgateway_delta_sync_run_test,
    syncgateway_e2e_cbl_load_docs,
    syncgateway_e2e_cbl_run_test,
    syncgateway_e2e_multi_cb_load_docs,
    syncgateway_e2e_multi_cb_run_test,
    syncgateway_e2e_multi_cbl_load_docs,
    syncgateway_e2e_multi_cbl_run_test,
    syncgateway_grant_access,
    syncgateway_init_users,
    syncgateway_load_docs,
    syncgateway_load_users,
    syncgateway_run_test,
    syncgateway_start_memcached,
)
from perfrunner.workloads.tpcds import (
    tpcds_initial_data_load,
    tpcds_remaining_data_load,
)
from perfrunner.workloads.ycsb import ycsb_data_load, ycsb_workload

try:
    set_start_method("fork")
except Exception as ex:
    print(ex)

celery = Celery('workers')

try:
    registry.enable('json')
    registry.enable('application/json')
    registry.enable('application/data')
    registry.enable('application/text')
except Exception as ex:
    print(ex)

if 'env/bin/perfrunner' in sys.argv:
    if '--remote' in sys.argv:
        # -C flag is a hack to distinguish local and remote workers!
        celery.config_from_object(celeryremote)
    else:
        celery.config_from_object(celerylocal)
elif 'env/bin/nosetests' in sys.argv:
    pass
else:
    worker_type = os.getenv('WORKER_TYPE')
    broker_url = os.getenv('BROKER_URL')
    if worker_type == 'local':
        celery.conf.update(
            broker_url='sqla+sqlite:///perfrunner.db',
            result_backend='database',
            database_url='sqlite:///results.db',
            task_serializer='pickle',
            result_serializer='pickle',
            accept_content={'pickle',
                            'json',
                            'application/json',
                            'application/data',
                            'application/text'},
            task_protocol=1)
    elif worker_type == 'remote':
        celery.conf.update(
            broker_url=broker_url,
            broker_pool_limit=None,
            worker_hijack_root_logger=False,
            result_backend="rpc://",
            result_persistent=False,
            result_exchange="perf_results",
            accept_content=['pickle',
                            'json',
                            'application/json',
                            'application/data',
                            'application/text'],
            result_serializer='pickle',
            task_serializer='pickle',
            task_protocol=1,
            broker_connection_timeout=30,
            broker_connection_retry=True,
            broker_connection_max_retries=10)
    else:
        raise Exception('invalid worker type: {}'.format(worker_type))

try:
    registry.enable('json')
    registry.enable('application/json')
    registry.enable('application/data')
    registry.enable('application/text')
except Exception as ex:
    print(ex)


@celery.task
def spring_task(*args):
    spring_workload(*args)


@celery.task
def pillowfight_data_load_task(*args):
    pillowfight_data_load(*args)


@celery.task
def pillowfight_task(*args):
    pillowfight_workload(*args)


@celery.task
def ycsb_data_load_task(*args):
    ycsb_data_load(*args)


@celery.task
def ycsb_task(*args):
    ycsb_workload(*args)


@celery.task
def jts_run_task(*args):
    jts_run(*args)


@celery.task
def jts_warmup_task(*args):
    jts_warmup(*args)


@celery.task
def tpcds_initial_data_load_task(*args):
    tpcds_initial_data_load(*args)


@celery.task
def tpcds_remaining_data_load_task(*args):
    tpcds_remaining_data_load(*args)


@celery.task
def java_dcp_client_task(*args):
    java_dcp_client(*args)


@celery.task
def syncgateway_task_load_users(*args):
    syncgateway_load_users(*args)


@celery.task
def syncgateway_task_init_users(*args):
    syncgateway_init_users(*args)


@celery.task
def syncgateway_task_grant_access(*args):
    syncgateway_grant_access(*args)


@celery.task
def syncgateway_task_load_docs(*args):
    syncgateway_load_docs(*args)


@celery.task
def syncgateway_task_run_test(*args):
    syncgateway_run_test(*args)


@celery.task
def syncgateway_task_start_memcached(*args):
    syncgateway_start_memcached(*args)


@celery.task
def syncgateway_bh_puller_task(*args):
    blackholepuller_runtest(*args)


@celery.task
def syncgateway_new_docpush_task(*args):
    newdocpusher_runtest(*args)


@celery.task
def syncgateway_delta_sync_task_load_docs(*args):
    syncgateway_delta_sync_load_docs(*args)


@celery.task
def syncgateway_delta_sync_task_run_test(*args):
    syncgateway_delta_sync_run_test(*args)


@celery.task
def syncgateway_e2e_cbl_task_load_docs(*args):
    syncgateway_e2e_cbl_load_docs(*args)


@celery.task
def syncgateway_e2e_cbl_task_run_test(*args):
    syncgateway_e2e_cbl_run_test(*args)


@celery.task
def syncgateway_e2e_multi_cbl_task_load_docs(*args):
    syncgateway_e2e_multi_cbl_load_docs(*args)


@celery.task
def syncgateway_e2e_multi_cbl_task_run_test(*args):
    syncgateway_e2e_multi_cbl_run_test(*args)


@celery.task
def syncgateway_e2e_multi_cb_task_load_docs(*args):
    syncgateway_e2e_multi_cb_load_docs(*args)


@celery.task
def syncgateway_e2e_multi_cb_task_run_test(*args):
    syncgateway_e2e_multi_cb_run_test(*args)


class WorkloadPhase:

    def __init__(self,
                 task: Callable,
                 target_iterator: Iterable[TargetSettings],
                 base_settings: PhaseSettings,
                 override_settings: dict = {},
                 timer: int = None):
        self.task = task
        self.target_iterator = target_iterator
        self.targets = list(target_iterator)
        self.task_settings = base_settings
        for option, value in override_settings.items():
            if hasattr(self.task_settings, option):
                setattr(self.task_settings, option, value)
        self.task_settings.bucket_list = [t.bucket for t in self.targets]
        self.timer = timer

    def task_sigs(self, workers):
        sigs, sig_workers = [], []

        for target in self.targets:
            for instance in range(self.task_settings.workload_instances):
                sig_workers.append(worker := next(workers))
                sigs.append(
                    self.task.si(self.task_settings, target, self.timer, instance).set(
                        queue=worker, expires=self.timer
                    )
                )

        return sigs, sig_workers


class WorkerManager:

    def __new__(cls, *args, **kwargs):
        if '--remote' in sys.argv:
            return RemoteWorkerManager(*args, **kwargs)
        else:
            return LocalWorkerManager(*args, **kwargs)


class RemoteWorkerManager:

    WORKER_HOME = '/tmp/perfrunner'

    PING_INTERVAL = 1

    def __init__(self, cluster_spec: ClusterSpec, test_config: TestConfig,
                 verbose: bool):
        self.cluster_spec = cluster_spec
        self.test_config = test_config
        self.broker_url = 'amqp://couchbase:couchbase@172.23.97.73:5672/broker'
        self.remote = RemoteHelper(cluster_spec, verbose)
        if self.cluster_spec.cloud_infrastructure:
            if self.cluster_spec.kubernetes_infrastructure:
                self.WORKER_HOME = '/opt/perfrunner'
                self.broker_url = self.remote.get_broker_urls()[0]
                self.worker_template_path = "cloud/worker/worker_template.yaml"
                self.worker_path = "cloud/worker/worker.yaml"
            else:
                self.broker_url = 'amqp://couchbase:couchbase@{}:5672/broker'\
                    .format(self.cluster_spec.brokers[0])
        celery.conf.update(
            broker_url=self.broker_url,
            broker_pool_limit=None,
            worker_hijack_root_logger=False,
            result_backend="rpc://",
            result_persistent=False,
            result_exchange="perf_results",
            accept_content=['pickle',
                            'json',
                            'application/json',
                            'application/data',
                            'application/text'],
            result_serializer='pickle',
            task_serializer='pickle',
            task_protocol=1,
            broker_connection_timeout=5,
            broker_connection_retry=True,
            broker_connection_max_retries=2)
        self.workers = cycle(self.cluster_spec.workers)
        self.terminate()
        self.start()
        self.wait_until_workers_are_ready()
        self.async_results = []

    @property
    def is_remote(self) -> bool:
        return True

    def next_worker(self) -> str:
        return next(self.workers)

    def reset_workers(self):
        self.workers = cycle(self.cluster_spec.workers)

    def start(self):
        logger.info('Initializing remote worker environment')
        if self.cluster_spec.kubernetes_infrastructure:
            self.start_kubernetes_workers()
        else:
            self.start_remote_workers()

    def start_remote_workers(self):
        perfrunner_home = os.path.join(self.WORKER_HOME, 'perfrunner')
        self.remote.init_repo(self.WORKER_HOME)
        self.remote.install_clients(perfrunner_home, self.test_config)
        if '--remote-copy' in sys.argv:
            self.remote.remote_copy(self.WORKER_HOME)
        for worker in self.cluster_spec.workers:
            logger.info('Starting remote Celery worker, host={}'.format(worker))
            self.remote.start_celery_worker(worker, perfrunner_home, self.broker_url)

    def start_kubernetes_workers(self):
        num_workers = len(self.cluster_spec.workers)
        k8s = self.cluster_spec.infrastructure_section('k8s')
        mem_limit = "{}Gi".format(k8s.get('worker_mem_limit', '128Gi'))
        cpu_limit = k8s.get('worker_cpu_limit', 80)  # Gb

        misc.inject_workers_spec(num_workers,
                                 mem_limit,
                                 cpu_limit,
                                 self.worker_template_path,
                                 self.worker_path)
        self.remote.create_from_file(self.worker_path)
        self.remote.wait_for_pods_ready("worker", num_workers)
        worker_idx = 0
        for pod in self.remote.get_pods():
            worker_name = pod.get("metadata", {}).get("name", "")
            if "worker" in worker_name:
                self.remote.start_celery_worker(worker_name,
                                                self.cluster_spec.workers[worker_idx],
                                                self.broker_url)
                worker_idx += 1

    def wait_until_workers_are_ready(self):
        workers = ['celery@{}'.format(worker)
                   for worker in self.cluster_spec.workers]
        while True:
            responses = celery.control.ping(workers)
            if len(responses) == len(workers):
                break
            time.sleep(self.PING_INTERVAL)
        logger.info('All remote Celery workers are ready')

    def run_tasks(self, phase):
        if self.test_config.test_case.reset_workers:
            self.reset_workers()
        self.async_results = []
        sigs, sig_workers = phase.task_sigs(self.workers)
        for sig, worker in zip(sigs, sig_workers):
            logger.info('Running task on {}'.format(worker))
            async_result = sig.apply_async()
            self.async_results.append(async_result)
            time.sleep(5)

    def wait_for_workers(self):
        logger.info('Waiting for all tasks to finish')
        for async_result in self.async_results:
            async_result.get()
        logger.info('All tasks are done')

    def download_celery_logs(self):
        if not os.path.exists('celery'):
            os.mkdir('celery')
        self.remote.get_celery_logs(self.WORKER_HOME)

    def abort(self):
        pass

    def terminate(self):
        logger.info('Terminating Celery workers')
        if self.cluster_spec.kubernetes_infrastructure:
            self.remote.terminate_client_pods(self.worker_path)
        else:
            self.remote.terminate_client_processes()

    def run_sg_tasks(self,
                     task: Callable,
                     task_settings: PhaseSettings,
                     target_iterator: Iterable[TargetSettings],
                     timer: int = None,
                     distribute: bool = False,
                     phase: str = ""):
        self.async_results = []
        self.reset_workers()
        for target in target_iterator:
            if distribute:
                total_threads = int(task_settings.syncgateway_settings.threads)
                total_clients = int(task_settings.syncgateway_settings.clients)
                instances_per_client = int(task_settings.syncgateway_settings.instances_per_client)
                total_instances = total_clients * instances_per_client
                threads_per_instance = int(total_threads/total_instances) or 1
                worker_id = 0

                group_tasks = []

                for instance in range(instances_per_client):
                    for client in self.cluster_spec.workers[:total_clients]:
                        worker_id += 1
                        logger.info('Running the \'{}\' by worker #{} on client {}'
                                    .format(phase, worker_id, client))
                        task_settings.syncgateway_settings.threads_per_instance = \
                            str(threads_per_instance)

                        group_tasks.append(
                            task.s(task_settings, target, timer, worker_id, self.cluster_spec).set(
                                queue=client
                            ).set(
                                expires=timer
                            ))

                g = group(group_tasks)
                self.async_results.append(g())
                time.sleep(15)
            else:
                client = self.cluster_spec.workers[0]
                logger.info('Running single-instance task \'{}\' on client {}'
                            .format(phase, client))
                task_settings.syncgateway_settings.threads_per_instance = \
                    task_settings.syncgateway_settings.threads
                async_result = task.apply_async(
                    args=(task_settings, target, timer, 0, self.cluster_spec),
                    queue=client,
                    expires=timer,
                )
                self.async_results.append(async_result)
                time.sleep(15)
                if task is syncgateway_task_start_memcached:
                    break

    def run_sg_bp_tasks(self,
                        task: Callable,
                        task_settings: PhaseSettings,
                        target_iterator: Iterable[TargetSettings],
                        timer: int = None,
                        distribute: bool = False,
                        phase: str = ""):
        self.async_results = []
        self.reset_workers()
        for target in target_iterator:
            if distribute:
                worker_id = 0
                total_clients = int(task_settings.syncgateway_settings.clients)
                for client in self.cluster_spec.workers[:total_clients]:
                    worker_id += 1
                    logger.info('Running the \'{}\' by worker #{} on'
                                ' client {}'.format(phase, worker_id, client))
                    async_result = task.apply_async(
                        args=(task_settings, target, timer, worker_id, self.cluster_spec),
                        queue=client, expires=timer,)
                    self.async_results.append(async_result)
                time.sleep(15)
            else:
                client = self.cluster_spec.workers[0]
                logger.info('Running sigle-instance task \'{}\' on client {}'.format(phase, client))
                async_result = task.apply_async(
                    args=(task_settings, target, timer, 0, self.cluster_spec),
                    queue=client, expires=timer)
                self.async_results.append(async_result)
                time.sleep(15)


class LocalWorkerManager(RemoteWorkerManager):

    BROKER_DB = 'perfrunner.db'
    RESULTS_DB = 'results.db'

    def __init__(self, cluster_spec: ClusterSpec, test_config: TestConfig,
                 verbose: bool):
        self.cluster_spec = cluster_spec
        self.test_config = test_config
        self.workers = cycle(['localhost'])
        self.terminate()
        self.tune_sqlite()
        self.start()
        self.wait_until_workers_are_ready()

    @property
    def is_remote(self) -> bool:
        return False

    def tune_sqlite(self):
        for db in self.BROKER_DB, self.RESULTS_DB:
            engine = create_engine('sqlite:///{}'.format(db))
            engine.execute('PRAGMA synchronous=OFF;')

    def wait_until_workers_are_ready(self):
        engine = create_engine('sqlite:///{}'.format(self.BROKER_DB))
        query = 'SELECT COUNT(*) FROM kombu_queue WHERE name = "{}"'\
            .format(self.next_worker())

        while True:
            if 'kombu_queue' not in engine.table_names():
                continue

            for count, in engine.execute(query):
                if count:
                    logger.info('Local Celery worker is ready')
                    return

    def start(self):
        logger.info('Starting local Celery worker')
        local.start_celery_worker(queue=self.next_worker())

    def download_celery_logs(self):
        pass

    @property
    def pid(self) -> int:
        with open('worker.pid') as f:
            pid = f.read()
        return int(pid)

    def abort(self):
        logger.info('Interrupting Celery workers')
        os.kill(self.pid, signal.SIGTERM)
        self.wait_for_workers()

    def terminate(self):
        logger.info('Terminating Celery workers')
        local.kill_process('celery')

    def run_sg_tasks(self,
                     task: Callable,
                     task_settings: PhaseSettings,
                     target_iterator: Iterable[TargetSettings],
                     timer: int = None,
                     distribute: bool = False,
                     phase: str = ""):
        self.async_results = []
        self.reset_workers()
        for target in target_iterator:
            if distribute:
                total_threads = int(task_settings.syncgateway_settings.threads)
                total_clients = int(task_settings.syncgateway_settings.clients)
                instances_per_client = int(task_settings.syncgateway_settings.instances_per_client)
                total_instances = total_clients * instances_per_client
                threads_per_instance = int(total_threads/total_instances) or 1
                worker_id = 0
                for instance in range(instances_per_client):
                    for _ in range(0, total_clients):
                        client = self.next_worker()
                        worker_id += 1
                        logger.info('Running the \'{}\' by worker #{} on client {}'
                                    .format(phase, worker_id, client))
                        task_settings.syncgateway_settings.threads_per_instance = \
                            str(threads_per_instance)
                        async_result = task.apply_async(
                            args=(task_settings, timer, worker_id, self.cluster_spec),
                            queue=client,
                            expires=timer,
                        )
                        self.async_results.append(async_result)
                time.sleep(15)
            else:
                client = self.next_worker()
                logger.info('Running single-instance task \'{}\' on client {}'
                            .format(phase, client))
                task_settings.syncgateway_settings.threads_per_instance = \
                    task_settings.syncgateway_settings.threads
                async_result = task.apply_async(
                    args=(task_settings, timer, 0, self.cluster_spec),
                    queue=client,
                    expires=timer,
                )
                self.async_results.append(async_result)
                time.sleep(15)
