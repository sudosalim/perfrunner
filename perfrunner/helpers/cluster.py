import random
import re
import time
from typing import Iterable, List
from uuid import uuid4

from logger import logger
from perfrunner.helpers.memcached import MemcachedHelper
from perfrunner.helpers.misc import (
    SGPortRange,
    maybe_atoi,
    pretty_dict,
    run_local_shell_command,
    set_azure_capella_subscription,
    set_azure_perf_subscription,
)
from perfrunner.helpers.monitor import Monitor
from perfrunner.helpers.remote import RemoteHelper
from perfrunner.helpers.rest import RestHelper
from perfrunner.settings import ClusterSpec, TestConfig


class ClusterManager:

    def __init__(self, cluster_spec: ClusterSpec, test_config: TestConfig,
                 verbose: bool = False):
        self.cluster_spec = cluster_spec
        self.test_config = test_config
        self.dynamic_infra = self.cluster_spec.dynamic_infrastructure
        self.capella_infra = self.cluster_spec.capella_infrastructure
        self.rest = RestHelper(cluster_spec, test_config)
        self.remote = RemoteHelper(cluster_spec, verbose)
        self.monitor = Monitor(cluster_spec, test_config, verbose)
        self.memcached = MemcachedHelper(cluster_spec, test_config)
        self.master_node = next(self.cluster_spec.masters)
        if self.dynamic_infra:
            self.initial_nodes = None
            self.build = ''  # During cluster creation, we dont have cb server yet,
            # and we dont have a usecase for this value in k8s
        else:
            self.initial_nodes = test_config.cluster.initial_nodes
            self.build = self.rest.get_version(self.master_node)
            version, build_number = self.build.split('-')
            self.build_tuple = tuple(map(int, version.split('.'))) + (int(build_number),)

    def is_compatible(self, min_release: str) -> bool:
        for master in self.cluster_spec.masters:
            version = self.rest.get_version(master)
            return version >= min_release

    def set_data_path(self):
        if self.dynamic_infra or self.capella_infra:
            return
        for server in self.cluster_spec.servers:
            self.remote.change_owner(server, self.cluster_spec.data_path)
            self.rest.set_data_path(server, self.cluster_spec.data_path)

    def set_index_path(self):
        if self.dynamic_infra or self.capella_infra:
            return
        for server in self.cluster_spec.servers:
            self.remote.change_owner(server, self.cluster_spec.index_path)
            self.rest.set_index_path(server, self.cluster_spec.index_path)

    def set_analytics_path(self):
        if self.dynamic_infra or self.capella_infra:
            return
        paths = []
        for path in self.cluster_spec.analytics_paths:
            for i in range(self.test_config.analytics_settings.num_io_devices):
                io_device = '{}/dev{}'.format(path, i)
                paths.append(io_device)
        for server in self.cluster_spec.servers_by_role('cbas'):
            for path in self.cluster_spec.analytics_paths:
                self.remote.change_owner(server, path)
            self.rest.set_analytics_paths(server, paths)

    def rename(self):
        if self.dynamic_infra or self.capella_infra:
            return
        elif self.cluster_spec.using_private_cluster_ips:
            for public_ip, private_ip in self.cluster_spec.servers_public_to_private_ip.items():
                if private_ip:
                    self.rest.rename(public_ip, private_ip)
        else:
            for server in self.cluster_spec.servers:
                self.rest.rename(server)

    def set_auth(self):
        if self.dynamic_infra or self.capella_infra:
            return
        else:
            for server in self.cluster_spec.servers:
                self.rest.set_auth(server)

    def set_mem_quotas(self):
        if self.capella_infra:
            return
        elif self.dynamic_infra:
            logger.info("Setting Memory Quotas")
            cluster = self.remote.get_cluster_config()
            cluster['spec']['cluster']['dataServiceMemoryQuota'] = \
                '{}Mi'.format(self.test_config.cluster.mem_quota)
            cluster['spec']['cluster']['indexServiceMemoryQuota'] = \
                '{}Mi'.format(self.test_config.cluster.index_mem_quota)
            if self.test_config.cluster.fts_index_mem_quota:
                cluster['spec']['cluster']['searchServiceMemoryQuota'] = \
                    '{}Mi'.format(self.test_config.cluster.fts_index_mem_quota)
            if self.test_config.cluster.analytics_mem_quota:
                cluster['spec']['cluster']['analyticsServiceMemoryQuota'] = \
                    '{}Mi'.format(self.test_config.cluster.analytics_mem_quota)
            if self.test_config.cluster.eventing_mem_quota:
                cluster['spec']['cluster']['eventingServiceMemoryQuota'] = \
                    '{}Mi'.format(self.test_config.cluster.eventing_mem_quota)
            self.remote.update_cluster_config(cluster)
        else:
            for master in self.cluster_spec.masters:
                self.rest.set_mem_quota(master,
                                        self.test_config.cluster.mem_quota)
                self.rest.set_index_mem_quota(master,
                                              self.test_config.cluster.index_mem_quota)
                if self.test_config.cluster.fts_index_mem_quota:
                    self.rest.set_fts_index_mem_quota(master,
                                                      self.test_config.cluster.fts_index_mem_quota)
                if self.test_config.cluster.analytics_mem_quota:
                    self.rest.set_analytics_mem_quota(master,
                                                      self.test_config.cluster.analytics_mem_quota)
                if self.test_config.cluster.eventing_mem_quota:
                    self.rest.set_eventing_mem_quota(master,
                                                     self.test_config.cluster.eventing_mem_quota)

    def set_query_settings(self):
        logger.info('Setting query settings')
        if self.dynamic_infra or self.capella_infra:
            return
        query_nodes = self.cluster_spec.servers_by_role('n1ql')
        if query_nodes:
            settings = self.test_config.n1ql_settings.cbq_settings
            if settings:
                self.rest.set_query_settings(query_nodes[0], settings)
            settings = self.rest.get_query_settings(query_nodes[0])
            settings = pretty_dict(settings)
            logger.info('Query settings: {}'.format(settings))

    def set_index_settings(self):
        if self.capella_infra:
            return
        logger.info('Setting index settings')
        index_nodes = self.cluster_spec.servers_by_role('index')
        if index_nodes:
            settings = self.test_config.gsi_settings.settings
            if settings:
                if self.dynamic_infra:
                    cluster = self.remote.get_cluster_config()
                    cluster['spec']['cluster']['indexStorageSetting'] = \
                        settings['indexer.settings.storage_mode']
                    self.remote.update_cluster_config(cluster)
                else:
                    for cluster_index_servers in \
                            self.cluster_spec.servers_by_cluster_and_role('index'):
                        index_node = cluster_index_servers[0]
                        self.rest.set_index_settings(index_node,
                                                     self.test_config.gsi_settings.settings)
                        cluster_settings = self.rest.get_index_settings(index_node)
                        cluster_settings = pretty_dict(self.rest.get_index_settings(index_node))
                        logger.info('Index settings: {}'.format(cluster_settings))

    def set_analytics_settings(self):
        replica_analytics = self.test_config.analytics_settings.replica_analytics
        if replica_analytics:
            if self.dynamic_infra:
                return

            self.rest.set_analytics_replica(self.master_node, replica_analytics)
            self.rebalance()
            check_replica = self.rest.get_analytics_replica(self.master_node)
            logger.info("Analytics replica setting: {}".format(check_replica))

    def set_services(self):
        if self.capella_infra:
            return
        elif self.dynamic_infra:
            logger.info("Setting services")
            cluster = self.remote.get_cluster_config()
            server_types = dict()
            server_roles = self.cluster_spec.roles
            for server, role in server_roles.items():
                role = role\
                    .replace('kv', 'data')\
                    .replace('n1ql', 'query')
                server_type_count = server_types.get(role, 0)
                server_types[role] = server_type_count + 1

            istio = 'false'
            if self.cluster_spec.istio_enabled(cluster_name='k8s_cluster_1'):
                istio = 'true'

            cluster_servers = []
            volume_claims = []
            operator_version = self.remote.get_operator_version()
            operator_major = int(operator_version.split(".")[0])
            operator_minor = int(operator_version.split(".")[1])
            for server_role, server_role_count in server_types.items():
                node_selector = {
                    '{}_enabled'.format(service
                                        .replace('data', 'kv')
                                        .replace('query', 'n1ql')): 'true'
                    for service in server_role.split(",")
                }
                node_selector['NodeRoles'] = 'couchbase1'
                spec = {
                    'imagePullSecrets': [{'name': 'regcred'}],
                    'nodeSelector': node_selector,
                }
                if (operator_major, operator_minor) <= (2, 1):
                    spec['containers'] = []

                sg_name = server_role.replace(",", "-")
                sg_def = self.test_config.get_sever_group_definition(sg_name)
                sg_nodes = int(sg_def.get("nodes", server_role_count))
                volume_size = sg_def.get("volume_size", "1000GB")
                volume_size = volume_size.replace("GB", "Gi")
                volume_size = volume_size.replace("MB", "Mi")
                pod_def =\
                    {
                        'spec': spec,
                        'metadata':
                            {
                                'annotations': {'sidecar.istio.io/inject': istio}
                            }
                    }
                server_def = \
                    {
                        'name': sg_name,
                        'services': server_role.split(","),
                        'pod': pod_def,
                        'size': sg_nodes,
                        'volumeMounts': {'default': sg_name}
                    }

                volume_claim_def = \
                    {
                        'metadata': {'name': sg_name},
                        'spec':
                            {
                                'resources':
                                    {
                                        'requests': {'storage': volume_size}
                                    }
                            }
                    }
                cluster_servers.append(server_def)
                volume_claims.append(volume_claim_def)

            cluster['spec']['servers'] = cluster_servers
            cluster['spec']['volumeClaimTemplates'] = volume_claims
            self.remote.update_cluster_config(cluster)
        else:
            if not self.is_compatible(min_release='4.0.0'):
                return

            for master in self.cluster_spec.masters:
                roles = self.cluster_spec.roles[master]
                self.rest.set_services(master, roles)

    def add_nodes(self):
        if self.dynamic_infra or self.capella_infra:
            return

        for (cluster, servers), initial_nodes in zip(self.cluster_spec.clusters,
                                                     self.initial_nodes):

            if initial_nodes < 2:  # Single-node cluster
                continue

            if using_private_ips := self.cluster_spec.using_private_cluster_ips:
                private_ips = dict(self.cluster_spec.clusters_private)[cluster]

            master = servers[0]
            for i, node in enumerate(servers[1:initial_nodes]):
                roles = self.cluster_spec.roles[node]
                new_host = private_ips[1:initial_nodes][i] if using_private_ips else node
                self.rest.add_node(master, new_host, roles)

    def rebalance(self):
        if self.dynamic_infra or self.capella_infra:
            return
        for (_, servers), initial_nodes \
                in zip(self.cluster_spec.clusters, self.initial_nodes):
            master = servers[0]
            known_nodes = servers[:initial_nodes]
            ejected_nodes = []
            self.rest.rebalance(master, known_nodes, ejected_nodes)
            self.monitor.monitor_rebalance(master)
        self.wait_until_healthy()

    def increase_bucket_limit(self, num_buckets: int):
        if self.dynamic_infra or self.capella_infra:
            return
        for master in self.cluster_spec.masters:
            self.rest.increase_bucket_limit(master, num_buckets)

    def flush_buckets(self):
        for master in self.cluster_spec.masters:
            for bucket_name in self.test_config.buckets:
                self.rest.flush_bucket(host=master,
                                       bucket=bucket_name)

    def delete_buckets(self):
        for master in self.cluster_spec.masters:
            for bucket_name in self.test_config.buckets:
                self.rest.delete_bucket(host=master,
                                        name=bucket_name)

    def create_buckets(self):
        mem_quota = self.test_config.cluster.mem_quota

        if mem_quota == 0 and self.capella_infra:
            logger.info('No memory quota provided for buckets on provisioned Capella cluster. '
                        'Getting free memory available for buckets...')
            mem_info = self.rest.get_bucket_mem_available(next(self.cluster_spec.masters))
            mem_quota = mem_info['free']
            logger.info('Free memory for buckets (per node): {}MB'.format(mem_quota))

        if self.test_config.cluster.num_buckets > 7:
            self.increase_bucket_limit(self.test_config.cluster.num_buckets + 3)

        if self.test_config.cluster.eventing_metadata_bucket_mem_quota:
            mem_quota -= (self.test_config.cluster.eventing_metadata_bucket_mem_quota +
                          self.test_config.cluster.eventing_bucket_mem_quota)

        per_bucket_quota = mem_quota // self.test_config.cluster.num_buckets

        if self.dynamic_infra:
            self.remote.delete_all_buckets()
            for bucket_name in self.test_config.buckets:
                self.remote.create_bucket(bucket_name, per_bucket_quota, self.test_config.bucket)
        else:
            for master in self.cluster_spec.masters:
                for bucket_name in self.test_config.buckets:
                    bucket_params = {
                        'host': master,
                        'name': bucket_name,
                        'ram_quota': per_bucket_quota,
                        'replica_number': self.test_config.bucket.replica_number,
                        'conflict_resolution_type':
                            self.test_config.bucket.conflict_resolution_type,
                        'backend_storage': self.test_config.bucket.backend_storage,
                        'eviction_policy': self.test_config.bucket.eviction_policy
                    }
                    if self.capella_infra:
                        bucket_params.update({
                            'flush': self.test_config.bucket.flush,
                            'durability': self.test_config.bucket.min_durability,
                            'ttl_value': self.test_config.bucket.doc_ttl_value,
                            'ttl_unit': self.test_config.bucket.doc_ttl_unit
                        })
                    else:
                        bucket_params.update({
                            'password': self.test_config.bucket.password,
                            'replica_index': self.test_config.bucket.replica_index,
                            'bucket_type': self.test_config.bucket.bucket_type,
                            'compression_mode': self.test_config.bucket.compression_mode,
                            'magma_seq_tree_data_block_size':
                                self.test_config.bucket.magma_seq_tree_data_block_size,
                            'history_seconds': self.test_config.bucket.history_seconds,
                            'history_bytes': self.test_config.bucket.history_bytes,
                            'max_ttl': self.test_config.bucket.max_ttl
                        })

                    self.rest.create_bucket(**bucket_params)

    def create_collections(self):
        if self.dynamic_infra:
            return
        collection_map = self.test_config.collection.collection_map
        for master in self.cluster_spec.masters:
            if collection_map is not None:
                if self.test_config.collection.use_bulk_api:
                    for bucket, scopes in collection_map.items():
                        create_scopes = []
                        for scope, collections in scopes.items():
                            create_collections = []
                            for collection, options in collections.items():
                                create_collection = {'name': collection}
                                if 'history' in options:
                                    create_collection['history'] = bool(options['history'])
                                create_collections.append(create_collection)
                            create_scope = {'name': scope, 'collections': create_collections}
                            create_scopes.append(create_scope)

                        self.rest.set_collection_map(master, bucket, {'scopes': create_scopes})
                else:
                    for bucket, scopes in collection_map.items():
                        # If transactions are enabled, we need to keep the default collection
                        # Otherwise, we can delete it if it is not specified in the collection map
                        delete_default = (
                            (not self.test_config.access_settings.transactionsenabled) and
                            '_default' not in scopes.get('_default', {})
                        )

                        if delete_default:
                            self.rest.delete_collection(master, bucket, '_default', '_default')

                        for scope, collections in scopes.items():
                            if scope != '_default':
                                self.rest.create_scope(master, bucket, scope)
                            for collection, options in collections.items():
                                if collection != '_default':
                                    history = bool(options['history']) \
                                              if 'history' in options else None
                                    self.rest.create_collection(master, bucket, scope, collection,
                                                                history)

    def create_eventing_buckets(self):
        if not self.test_config.cluster.eventing_bucket_mem_quota:
            return
        if self.dynamic_infra:
            return
        per_bucket_quota = \
            self.test_config.cluster.eventing_bucket_mem_quota \
            // self.test_config.cluster.eventing_buckets

        for master in self.cluster_spec.masters:
            for bucket_name in self.test_config.eventing_buckets:
                bucket_params = {
                    'host': master,
                    'name': bucket_name,
                    'ram_quota': per_bucket_quota,
                    'replica_number': self.test_config.bucket.replica_number,
                    'conflict_resolution_type': self.test_config.bucket.conflict_resolution_type,
                    'backend_storage': self.test_config.bucket.backend_storage
                }
                if self.capella_infra:
                    bucket_params.update({
                        'flush': self.test_config.bucket.flush,
                        'durability': self.test_config.bucket.min_durability,
                        'ttl_value': 0,
                        'ttl_unit': None
                    })
                else:
                    bucket_params.update({
                        'password': self.test_config.bucket.password,
                        'replica_index': self.test_config.bucket.replica_index,
                        'eviction_policy': self.test_config.bucket.eviction_policy,
                        'bucket_type': self.test_config.bucket.bucket_type,
                        'compression_mode': self.test_config.bucket.compression_mode
                    })
                self.rest.create_bucket(**bucket_params)

    def create_eventing_metadata_bucket(self):
        if not self.test_config.cluster.eventing_metadata_bucket_mem_quota:
            return
        if self.dynamic_infra:
            return
        for master in self.cluster_spec.masters:
            bucket_params = {
                'host': master,
                'name': self.test_config.cluster.EVENTING_METADATA_BUCKET_NAME,
                'ram_quota': self.test_config.cluster.eventing_metadata_bucket_mem_quota,
                'replica_number': self.test_config.bucket.replica_number,
                'conflict_resolution_type': self.test_config.bucket.conflict_resolution_type,
                'backend_storage': self.test_config.bucket.backend_storage
            }
            if self.capella_infra:
                bucket_params.update({
                    'flush': self.test_config.bucket.flush,
                    'durability': self.test_config.bucket.min_durability,
                    'ttl_value': 0,
                    'ttl_unit': None
                })
            else:
                bucket_params.update({
                    'password': self.test_config.bucket.password,
                    'replica_index': self.test_config.bucket.replica_index,
                    'eviction_policy': self.test_config.bucket.eviction_policy,
                    'bucket_type': self.test_config.bucket.bucket_type,
                    'compression_mode': self.test_config.bucket.compression_mode
                })
            self.rest.create_bucket(**bucket_params)

    def configure_auto_compaction(self):
        compaction_settings = self.test_config.compaction
        if self.capella_infra:
            return
        elif self.dynamic_infra:
            logger.info("Configuring auto-compaction")
            cluster = self.remote.get_cluster_config()
            db = int(compaction_settings.db_percentage)
            view = int(compaction_settings.view_percentage)
            para = bool(str(compaction_settings.parallel).lower())

            auto_compaction = cluster['spec']['cluster']\
                .get('autoCompaction',
                     {'databaseFragmentationThreshold': {'percent': 30},
                      'viewFragmentationThreshold': {'percent': 30},
                      'parallelCompaction': False})

            db_percent = auto_compaction.get(
                'databaseFragmentationThreshold', {'percent': 30})
            db_percent['percent'] = db
            auto_compaction['databaseFragmentationThreshold'] = db_percent

            views_percent = auto_compaction.get(
                'viewFragmentationThreshold', {'percent': 30})
            views_percent['percent'] = view
            auto_compaction['viewFragmentationThreshold'] = views_percent
            auto_compaction['parallelCompaction'] = para

            self.remote.update_cluster_config(cluster)
        else:
            for master in self.cluster_spec.masters:
                self.rest.configure_auto_compaction(master, compaction_settings)
                settings = self.rest.get_auto_compaction_settings(master)
                logger.info('Auto-compaction settings: {}'.format(pretty_dict(settings)))

    def configure_internal_settings(self):
        internal_settings = self.test_config.internal_settings
        for master in self.cluster_spec.masters:
            for parameter, value in internal_settings.items():
                if self.dynamic_infra or self.capella_infra:
                    raise Exception('not supported for dynamic or capella infrastructure yet')
                else:
                    self.rest.set_internal_settings(
                        master,
                        {parameter: maybe_atoi(value)})

    def configure_xdcr_settings(self):
        xdcr_cluster_settings = self.test_config.xdcr_cluster_settings
        if self.dynamic_infra or self.capella_infra:
            return
        for master in self.cluster_spec.masters:
            for parameter, value in xdcr_cluster_settings.items():
                self.rest.set_xdcr_cluster_settings(
                    master,
                    {parameter: maybe_atoi(value)})

    def tweak_memory(self):
        if self.dynamic_infra or self.capella_infra:
            return
        self.remote.reset_swap()
        self.remote.drop_caches()
        self.remote.set_swappiness()
        self.remote.disable_thp()

    def enable_n2n_encryption(self):
        if self.dynamic_infra or self.capella_infra:
            return
        if self.test_config.cluster.enable_n2n_encryption:
            for master in self.cluster_spec.masters:
                self.remote.enable_n2n_encryption(
                    master,
                    self.test_config.cluster.enable_n2n_encryption)

    def disable_ui_http(self):
        if self.capella_infra:
            return
        if self.test_config.cluster.ui_http == 'disabled':
            self.remote.ui_http_off(self.cluster_spec.servers[0])

    def serverless_mode(self):
        if self.test_config.cluster.serverless_mode == 'enabled':
            self.remote.enable_serverless_mode()
        else:
            self.remote.disable_serverless_mode()

    def serverless_throttle(self):
        if not all(value == 0 for value in self.test_config.cluster.serverless_throttle.values()):
            self.rest.set_serverless_throttle(self.master_node,
                                              self.test_config.cluster.serverless_throttle)

    def restart_with_alternative_num_vbuckets(self):
        if self.capella_infra:
            return
        num_vbuckets = self.test_config.cluster.num_vbuckets
        if num_vbuckets is not None:
            if self.dynamic_infra:
                raise Exception('not supported for dynamic infrastructure yet')
            else:
                self.remote.restart_with_alternative_num_vbuckets(num_vbuckets)

    def restart_with_alternative_bucket_options(self):
        """Apply custom buckets settings.

        Tune bucket settings (e.g., max_num_shards or max_num_auxio) using
        "/diag/eval" and restart the entire cluster.
        """
        if self.dynamic_infra or self.capella_infra:
            return
        if self.test_config.bucket_extras:
            self.remote.enable_nonlocal_diag_eval()

        cmd = 'ns_bucket:update_bucket_props("{}", ' \
              '[{{extra_config_string, "{}"}}]).'

        params = ''
        for option, value in self.test_config.bucket_extras.items():
            if re.search("^num_.*_threads$", option):
                self.rest.set_num_threads(self.master_node, option, value)
            else:
                params = params + option+"="+value+";"

        if params:
            for master in self.cluster_spec.masters:
                for bucket in (self.test_config.buckets + self.test_config.eventing_buckets +
                               self.test_config.eventing_metadata_bucket):
                    logger.info('Changing {} to {}'.format(bucket, params))
                    diag_eval = cmd.format(bucket, params[:len(params) - 1])
                    self.rest.run_diag_eval(master, diag_eval)

        if self.test_config.bucket_extras:
            self._restart_clusters()

        if self.test_config.magma_settings.storage_quota_percentage:
            magma_quota = self.test_config.magma_settings.storage_quota_percentage
            for bucket in self.test_config.buckets:
                self.remote.set_magma_quota(bucket, magma_quota)

    def _restart_clusters(self):
        self.disable_auto_failover()
        self.remote.restart()
        self.wait_until_healthy()
        self.enable_auto_failover()

    def configure_ns_server(self):
        """Configure ns_server using diag/eval.

        Tune ns_server settings with specified Erlang code using  "/diag/eval"
        and restart the cluster after a specified delay.
        """
        diag_eval_settings = self.test_config.diag_eval
        if self.dynamic_infra or self.capella_infra or not diag_eval_settings.payloads:
            return

        if diag_eval_settings.enable_nonlocal_diag_eval:
            self.remote.enable_nonlocal_diag_eval()

        for master in self.cluster_spec.masters:
            for payload in diag_eval_settings.payloads:
                payload = payload.strip('\'')
                logger.info("Running diag/eval: '{}' on {}".format(payload, master))
                self.rest.run_diag_eval(master, '{}'.format(payload))

        # Some config may be replicated to other nodes asynchronously.
        # Allow configurable delay before restart
        time.sleep(diag_eval_settings.restart_delay)
        self._restart_clusters()

    def tune_logging(self):
        if self.dynamic_infra or self.capella_infra:
            return
        self.remote.tune_log_rotation()
        self.remote.restart()

    def enable_auto_failover(self):
        enabled = self.test_config.bucket.autofailover_enabled
        failover_timeouts = self.test_config.bucket.failover_timeouts
        if self.capella_infra:
            return
        if self.dynamic_infra:
            logger.info("Setting auto-failover settings")
            cluster = self.remote.get_cluster_config()
            cluster['spec']['cluster']['autoFailoverMaxCount'] = 1
            cluster['spec']['cluster']['autoFailoverServerGroup'] = bool(enabled)
            cluster['spec']['cluster']['autoFailoverOnDataDiskIssues'] = bool(enabled)
            cluster['spec']['cluster']['autoFailoverOnDataDiskIssuesTimePeriod'] = \
                '{}s'.format(10)
            cluster['spec']['cluster']['autoFailoverTimeout'] = \
                '{}s'.format(failover_timeouts[-1])
            self.remote.update_cluster_config(cluster)
        else:
            for master in self.cluster_spec.masters:
                self.rest.set_auto_failover(master, enabled, failover_timeouts)

    def disable_auto_failover(self):
        enabled = 'false'
        failover_timeouts = self.test_config.bucket.failover_timeouts
        if self.capella_infra:
            return
        if self.dynamic_infra:
            cluster = self.remote.get_cluster_config()
            cluster['spec']['cluster']['autoFailoverMaxCount'] = 1
            cluster['spec']['cluster']['autoFailoverServerGroup'] = bool(enabled)
            cluster['spec']['cluster']['autoFailoverOnDataDiskIssues'] = bool(enabled)
            cluster['spec']['cluster']['autoFailoverOnDataDiskIssuesTimePeriod'] = \
                '{}s'.format(10)
            cluster['spec']['cluster']['autoFailoverTimeout'] = \
                '{}s'.format(failover_timeouts[-1])
            self.remote.update_cluster_config(cluster)
        else:
            for master in self.cluster_spec.masters:
                self.rest.set_auto_failover(master, enabled, failover_timeouts)

    def wait_until_warmed_up(self):
        if self.test_config.bucket.bucket_type in ('ephemeral', 'memcached'):
            return
        if self.dynamic_infra:
            self.remote.wait_for_cluster_ready()
        else:
            for master in self.cluster_spec.masters:
                for bucket in self.test_config.buckets:
                    self.monitor.monitor_warmup(self.memcached, master, bucket)

    def wait_until_healthy(self):
        if self.dynamic_infra:
            self.remote.wait_for_cluster_ready()
        else:
            for master in self.cluster_spec.masters:
                self.monitor.monitor_node_health(master)

                for analytics_node in self.rest.get_active_nodes_by_role(master,
                                                                         'cbas'):
                    self.monitor.monitor_analytics_node_active(analytics_node)

    def gen_disabled_audit_events(self, master: str) -> List[str]:
        curr_settings = self.rest.get_audit_settings(master)
        curr_disabled = {str(event) for event in curr_settings['disabled']}
        disabled = curr_disabled - self.test_config.audit_settings.extra_events
        return list(disabled)

    def enable_audit(self):
        if self.dynamic_infra:
            return
        if not self.is_compatible(min_release='4.0.0') or \
                self.rest.is_community(self.master_node):
            return

        if not self.test_config.audit_settings.enabled:
            return

        for master in self.cluster_spec.masters:
            disabled = []
            if self.test_config.audit_settings.extra_events:
                disabled = self.gen_disabled_audit_events(master)
            self.rest.enable_audit(master, disabled)

    def add_server_groups(self):
        logger.info("Server group map: {}".format(self.cluster_spec.server_group_map))
        if self.cluster_spec.server_group_map:
            server_group_info = self.rest.get_server_group_info(self.master_node)["groups"]
            existing_server_groups = [group_info["name"] for group_info in server_group_info]
            groups = set(self.cluster_spec.server_group_map.values())
            for group in groups:
                if group not in existing_server_groups:
                    self.rest.create_server_group(self.master_node, group)

    def change_group_membership(self):
        if self.cluster_spec.server_group_map:
            server_group_info = self.rest.get_server_group_info(self.master_node)
            server_groups = set(self.cluster_spec.server_group_map.values())

            node_group_json = {
                "groups": []
            }

            for i, group_info in enumerate(server_group_info["groups"]):
                node_group_json["groups"].append(dict((k, group_info[k])
                                                      for k in ["name", "uri"]))
                node_group_json["groups"][i]["nodes"] = []
            nodes_initialised = 1
            for server, group in self.cluster_spec.server_group_map.items():
                for server_info in node_group_json["groups"]:
                    if server_info["name"] == group and nodes_initialised <= self.initial_nodes[0]:
                        server_info["nodes"].append({"otpNode": "ns_1@{}".format(server)})
                        nodes_initialised += 1
                        break

            logger.info("node json {}".format(node_group_json))
            self.rest.change_group_membership(self.master_node,
                                              server_group_info["uri"],
                                              node_group_json)
            logger.info("group membership updated")
            for server_grp in server_group_info["groups"]:
                if server_grp["name"] not in server_groups:
                    self.delete_server_group(server_grp["name"])

    def delete_server_group(self, server_group):
        logger.info("Deleting Server Group {}".format(server_group))
        server_group_info = self.rest.get_server_group_info(self.master_node)["groups"]
        for server_grp in server_group_info:
            if server_grp["name"] == server_group:
                uri = server_grp["uri"]
                break
        self.rest.delete_server_group(self.master_node, uri)

    def generate_ce_roles(self) -> List[str]:
        return ['admin']

    def generate_ee_roles(self) -> List[str]:
        existing_roles = {r['role']
                          for r in self.rest.get_rbac_roles(self.master_node)}

        roles = []
        for role in (
                'bucket_admin',
                'data_dcp_reader',
                'data_monitoring',
                'data_reader_writer',
                'data_reader',
                'data_writer',
                'fts_admin',
                'fts_searcher',
                'query_delete',
                'query_insert',
                'query_select',
                'query_update',
                'views_admin',
        ):
            if role in existing_roles:
                roles.append(role + '[{bucket}]')

        return roles

    def delete_rbac_users(self):
        if not self.is_compatible(min_release='5.0'):
            return

        for master in self.cluster_spec.masters:
            for bucket in self.test_config.buckets:
                self.rest.delete_rbac_user(
                    host=master,
                    bucket=bucket
                )

    def add_rbac_users(self):
        if self.dynamic_infra:
            self.remote.create_from_file("cloud/operator/2/1/user-password-secret.yaml")
            # self.remote.create_from_file("cloud/operator/2/1/admin-user.yaml")
            self.remote.create_from_file("cloud/operator/2/1/bucket-user.yaml")
            self.remote.create_from_file("cloud/operator/2/1/rbac-admin-group.yaml")
            self.remote.create_from_file("cloud/operator/2/1/rbac-admin-role-binding.yaml")
        else:
            if not self.rest.supports_rbac(self.master_node):
                logger.info('RBAC not supported - skipping adding RBAC users')
                return

            if self.rest.is_community(self.master_node):
                roles = self.generate_ce_roles()
            else:
                roles = self.generate_ee_roles()

            for master in self.cluster_spec.masters:
                admin_user, admin_password = self.cluster_spec.rest_credentials
                self.rest.add_rbac_user(
                    host=master,
                    user=admin_user,
                    password=admin_password,
                    roles=['admin'],
                )

                buckets = self.test_config.buckets + self.test_config.eventing_buckets

                for bucket in buckets:
                    bucket_roles = [role.format(bucket=bucket) for role in roles]
                    bucket_roles.append("admin")
                    self.rest.add_rbac_user(
                        host=master,
                        user=bucket,  # Backward compatibility
                        password=self.test_config.bucket.password,
                        roles=bucket_roles,
                    )

    def add_extra_rbac_users(self, num_users):
        if not self.rest.supports_rbac(self.master_node):
            logger.info('RBAC not supported - skipping adding RBAC users')
            return

        if self.rest.is_community(self.master_node):
            roles = self.generate_ce_roles()
        else:
            roles = self.generate_ee_roles()

        for master in self.cluster_spec.masters:
            admin_user, admin_password = self.cluster_spec.rest_credentials
            self.rest.add_rbac_user(
                host=master,
                user=admin_user,
                password=admin_password,
                roles=['admin'],
            )

            for bucket in self.test_config.buckets:
                bucket_roles = [role.format(bucket=bucket) for role in roles]
                bucket_roles.append("admin")
                for i in range(1, num_users+1):
                    user = 'user{user_number}'.format(user_number=str(i))
                    self.rest.add_rbac_user(
                        host=master,
                        user=user,
                        password=self.test_config.bucket.password,
                        roles=bucket_roles,
                    )

    def throttle_cpu(self):
        if self.capella_infra:
            return
        if self.dynamic_infra:
            if self.test_config.cluster.online_cores:
                logger.info("Throttling cpu")
                cluster = self.remote.get_cluster_config()
                server_groups = cluster['spec']['servers']
                updated_server_groups = []
                online_vcpus = self.test_config.cluster.online_cores * 2
                for server_group in server_groups:
                    resources = server_group.get('resources', {})
                    limits = resources.get('limits', {})
                    limits['cpu'] = online_vcpus
                    resources['limits'] = limits
                    server_group['resources'] = resources
                    updated_server_groups.append(server_group)
                cluster['spec']['servers'] = updated_server_groups
                self.remote.update_cluster_config(cluster)
        else:
            if self.remote.PLATFORM == 'cygwin':
                return

            if self.test_config.cluster.enable_cpu_cores:
                self.remote.enable_cpu()

            if self.test_config.cluster.online_cores:
                self.remote.disable_cpu(self.test_config.cluster.online_cores)

            if self.test_config.cluster.sgw_online_cores:
                self.remote.disable_cpu_sgw(self.test_config.cluster.sgw_online_cores)

    def tune_memory_settings(self):
        if self.capella_infra:
            return
        kernel_memory = self.test_config.cluster.kernel_mem_limit
        kv_kernel_memory = self.test_config.cluster.kv_kernel_mem_limit
        if kernel_memory or kv_kernel_memory:
            if self.dynamic_infra:
                logger.info("Tuning memory settings")
                cluster = self.remote.get_cluster_config()
                server_groups = cluster['spec']['servers']
                tune_services = set()
                # CAO uses different service names than perfrunner
                for service in self.test_config.cluster.kernel_mem_limit_services:
                    if service == 'kv':
                        service = 'data'
                    elif service == 'n1ql':
                        service = 'query'
                    elif service == 'fts':
                        service = 'search'
                    elif service == 'cbas':
                        service = 'analytics'
                    tune_services.add(service)

                updated_server_groups = []
                default_mem = '128Gi'
                for server_group in server_groups:
                    services_in_group = set(server_group['services'])
                    resources = server_group.get('resources', {})
                    limits = resources.get('limits', {})
                    mem_limit = limits.get('memory', default_mem)
                    if services_in_group.intersection(tune_services) and kernel_memory != 0:
                        mem_limit = '{}Mi'.format(kernel_memory)
                    limits['memory'] = mem_limit
                    resources['limits'] = limits
                    server_group['resources'] = resources
                    updated_server_groups.append(server_group)

                cluster['spec']['servers'] = updated_server_groups
                self.remote.update_cluster_config(cluster)
            else:
                for service in self.test_config.cluster.kernel_mem_limit_services:
                    for server in self.cluster_spec.servers_by_role(service):
                        if service == 'kv' and kv_kernel_memory:
                            self.remote.tune_memory_settings(host_string=server,
                                                             size=kv_kernel_memory)
                        else:
                            self.remote.tune_memory_settings(host_string=server,
                                                             size=kernel_memory)
                self.monitor.wait_for_servers()

    def reset_memory_settings(self):
        if self.dynamic_infra or self.capella_infra:
            return
        for service in self.test_config.cluster.kernel_mem_limit_services:
            for server in self.cluster_spec.servers_by_role(service):
                self.remote.reset_memory_settings(host_string=server)
        self.monitor.wait_for_servers()

    def flush_iptables(self):
        if self.dynamic_infra or self.capella_infra:
            return
        self.remote.flush_iptables()

    def clear_login_history(self):
        if self.dynamic_infra or self.capella_infra:
            return
        self.remote.clear_wtmp()

    def disable_wan(self):
        if self.dynamic_infra or self.capella_infra:
            return
        self.remote.disable_wan()

    def enable_ipv6(self):
        if self.dynamic_infra or self.capella_infra:
            return
        if self.test_config.cluster.ipv6:
            if self.build_tuple < (6, 5, 0, 0):
                self.remote.update_ip_family_rest()
            else:
                self.remote.update_ip_family_cli()
            self.remote.enable_ipv6()

    def set_x509_certificates(self):
        if self.dynamic_infra or self.capella_infra:
            return
        if self.test_config.access_settings.ssl_mode == "auth":
            self.remote.allow_non_local_ca_upload()
            self.remote.setup_x509()
            self.rest.upload_cluster_certificate(self.cluster_spec.servers[0])
            for i in range(self.initial_nodes[0]):
                self.rest.reload_cluster_certificate(self.cluster_spec.servers[i])
                self.rest.enable_certificate_auth(self.cluster_spec.servers[i])

    def set_cipher_suite(self):
        if self.dynamic_infra or self.capella_infra:
            return
        if self.test_config.access_settings.cipher_list:
            check_cipher_suit = self.rest.get_cipher_suite(self.master_node)
            logger.info('current cipher suit: {}'.format(check_cipher_suit))
            self.rest.set_cipher_suite(
                self.master_node, self.test_config.access_settings.cipher_list)
            check_cipher_suit = self.rest.get_cipher_suite(self.master_node)
            logger.info('new cipher suit: {}'.format(check_cipher_suit))

    def set_min_tls_version(self):
        if self.dynamic_infra or self.capella_infra:
            return
        if self.test_config.access_settings.min_tls_version or \
           self.test_config.backup_settings.min_tls_version or \
           self.test_config.restore_settings.min_tls_version:
            check_tls_version = self.rest.get_minimum_tls_version(self.master_node)
            logger.info('current tls version: {}'.format(check_tls_version))
            self.rest.set_minimum_tls_version(
                self.master_node,
                self.test_config.access_settings.min_tls_version
            )
            check_tls_version = self.rest.get_minimum_tls_version(self.master_node)
            logger.info('new tls version: {}'.format(check_tls_version))

    def get_debug_package_url(self):
        release, build_number = self.build.split('-')
        if self.build_tuple > (8, 0, 0, 0):
            release = 'morpheus'
        elif self.build_tuple > (7, 6, 0, 0):
            release = 'trinity'
        elif (7, 2, 0, 0) < self.build_tuple <= (7, 2, 0, 2228) or self.build_tuple > (7, 5, 0, 0):
            release = 'elixir'
        elif self.build_tuple > (7, 1, 0, 0):
            release = 'neo'
        elif self.build_tuple > (7, 0, 0, 0):
            release = 'cheshire-cat'
        elif self.build_tuple > (6, 5, 0, 0) and self.build_tuple < (7, 0, 0, 0):
            release = 'mad-hatter'
        elif self.build_tuple < (6, 5, 0, 0):
            release = 'alice'

        if self.remote.distro.upper() in ['UBUNTU', 'DEBIAN']:
            package_name = 'couchbase-server-enterprise-dbg_{{}}-{{}}{{}}_amd64.deb'
        else:
            package_name = 'couchbase-server-enterprise-debuginfo-{{}}-{{}}{{}}.x86_64.rpm'

        package_name = package_name.format(self.build, self.remote.distro,
                                           self.remote.distro_version)
        return (
            'http://latestbuilds.service.couchbase.com/'
            'builds/latestbuilds/couchbase-server/{}/{}/{}'
        ).format(release, build_number, package_name)

    def install_cb_debug_package(self):
        self.remote.install_cb_debug_package(url=self.get_debug_package_url())

    def enable_developer_preview(self):
        if self.build_tuple > (7, 0, 0, 4698) or self.build_tuple < (1, 0, 0, 0):
            self.remote.enable_developer_preview()

    def configure_autoscaling(self):
        autoscaling_settings = self.test_config.autoscaling_setting
        if self.dynamic_infra and autoscaling_settings.enabled:
            logger.info("Configuring auto-scaling")
            cluster = self.remote.get_cluster_config()
            server_groups = cluster['spec']['servers']
            updated_server_groups = []
            for server_group in server_groups:
                if server_group['name'] == autoscaling_settings.server_group:
                    server_group['autoscaleEnabled'] = True
                updated_server_groups.append(server_group)
            cluster['spec']['servers'] = updated_server_groups
            self.remote.update_cluster_config(cluster)
            self.remote.create_horizontal_pod_autoscaler(autoscaling_settings.server_group,
                                                         autoscaling_settings.min_nodes,
                                                         autoscaling_settings.max_nodes,
                                                         autoscaling_settings.target_metric,
                                                         autoscaling_settings.target_type,
                                                         autoscaling_settings.target_value)
        else:
            return

    def set_magma_min_quota(self):
        if self.test_config.magma_settings.magma_min_memory_quota:
            magma_min_quota = self.test_config.magma_settings.magma_min_memory_quota
            self.remote.set_magma_min_memory_quota(magma_min_quota)

    def capella_allow_client_ips(self):
        if not self.capella_infra:
            return

        if self.cluster_spec.infrastructure_settings.get('peering_connection', None) is None:
            client_ips = self.cluster_spec.clients
            if self.cluster_spec.capella_backend == 'aws':
                client_ips = [
                    dns.split('.')[0].removeprefix('ec2-').replace('-', '.') for dns in client_ips
                ]
            self.rest.add_allowed_ips_all_clusters(client_ips)

    def allow_ips_for_serverless_dbs(self):
        for db_id in self.test_config.buckets:
            self.rest.allow_my_ip(db_id)
            client_ips = self.cluster_spec.clients
            if self.cluster_spec.capella_backend == 'aws':
                client_ips = [
                    dns.split('.')[0].removeprefix('ec2-').replace('-', '.') for dns in client_ips
                ]
            self.rest.add_allowed_ips(db_id, client_ips)

    def bypass_nebula_for_clients(self):
        client_ips = self.cluster_spec.clients
        if self.cluster_spec.capella_backend == 'aws':
            client_ips = [
                dns.split('.')[0].removeprefix('ec2-').replace('-', '.') for dns in client_ips
            ]
        for ip in client_ips:
            self.rest.bypass_nebula(ip)

    def provision_serverless_db_keys(self):
        dbs = self.test_config.serverless_db.db_map
        for db_id in dbs.keys():
            resp = self.rest.get_db_api_key(db_id)
            dbs[db_id]['access'] = resp.json()['access']
            dbs[db_id]['secret'] = resp.json()['secret']

        self.test_config.serverless_db.update_db_map(dbs)

    def init_nebula_ssh(self):
        if self.cluster_spec.serverless_infrastructure and \
           self.cluster_spec.capella_backend == 'aws':
            self.cluster_spec.set_nebula_instance_ids()
            self.remote.nebula_init_ssh()

    def get_capella_cluster_admin_creds(self):
        if self.cluster_spec.capella_infrastructure:
            logger.info('Getting cluster admin credentials.')
            user = 'couchbase-cloud-admin'
            pwds = []

            command_template = (
                'env/bin/aws secretsmanager get-secret-value --region us-east-1 '
                '--secret-id {}_dp-admin '
                '--query "SecretString" '
                '--output text'
            )

            for cluster_id in self.rest.cluster_ids:
                command = command_template.format(cluster_id)

                stdout, _, returncode = run_local_shell_command(command)

                if returncode == 0:
                    pwds.append(stdout.strip())

            creds = '\n'.join('{}:{}'.format(user, pwd) for pwd in pwds).replace('%', '%%')
            self.cluster_spec.config.set('credentials', 'admin', creds)
            self.cluster_spec.update_spec_file()

    def open_capella_cluster_ports(self, port_ranges: Iterable[SGPortRange]):
        if self.cluster_spec.capella_infrastructure:
            logger.info('Opening port ranges for Capella cluster:\n{}'
                        .format('\n'.join(str(pr) for pr in port_ranges)))

            if self.cluster_spec.capella_backend == 'aws':
                self._open_capella_aws_cluster_ports(port_ranges)
            elif self.cluster_spec.capella_backend == 'azure':
                self._open_capella_azure_cluster_ports(port_ranges)

    def _open_capella_aws_cluster_ports(self, port_ranges: Iterable[SGPortRange]):
        command_template = (
            'env/bin/aws ec2 authorize-security-group-ingress --region $AWS_REGION '
            '--group-id {} '
            '--ip-permissions {}'
        )

        for cluster_id, hostname in zip(self.rest.cluster_ids, self.cluster_spec.masters):
            if not (vpc_id := self.get_capella_aws_cluster_vpc_id(hostname)):
                logger.error(
                    'Failed to get Capella cluster VPC ID in order to get Security Group ID. '
                    'Cannot open desired ports for cluster {}.'.format(cluster_id)
                )
                continue

            if not (sg_id := self.get_capella_aws_cluster_security_group_id(vpc_id)):
                logger.error(
                    'Failed to get Security Group ID for Capella cluster VPC. '
                    'Cannot open desired ports for cluster {}.'.format(cluster_id)
                )
                continue

            ip_perms_template = \
                'IpProtocol={},FromPort={},ToPort={},IpRanges=[{{CidrIp=0.0.0.0/0}}]'
            ip_perms_list = [
                ip_perms_template.format(pr.protocol, pr.min_port, pr.max_port)
                for pr in port_ranges
            ]

            command = command_template.format(sg_id, ' '.join(ip_perms_list))

            run_local_shell_command(
                command=command,
                success_msg='Successfully opened ports for Capella cluster {}.'.format(cluster_id),
                err_msg='Failed to open ports for Capella cluster {}.'.format(cluster_id)
            )

    def _open_capella_azure_cluster_ports(self, port_ranges: Iterable[SGPortRange]):
        command_template = (
            'az network nsg rule create '
            '--resource-group rg-{0} '
            '--nsg-name cc-{0} '
            '--name {1} '
            '--priority {2} '
            '--destination-address-prefixes \'VirtualNetwork\' '
            '--destination-port-ranges {3} '
            '--access Allow '
            '--direction Inbound'
        )

        # NSG rule priorities need to be unique within an NSG, so choose one at random from a
        # large enough range to minimize collisions
        rule_priority = random.randint(1000, 2000)
        rule_name = 'AllowPerfrunnerInbound-{}'.format(uuid4().hex[:6])

        err = set_azure_capella_subscription(
            self.cluster_spec.infrastructure_settings.get('cbc_env', 'sandbox')
        )
        if not err:
            for cluster_id in self.rest.cluster_ids:
                command = command_template.format(
                    cluster_id,
                    rule_name,
                    rule_priority,
                    ' '.join(pr.port_range_str() for pr in port_ranges)
                )

                run_local_shell_command(
                    command=command,
                    success_msg='Successfully opened ports for Capella cluster {}.'
                                .format(cluster_id),
                    err_msg='Failed to create NSG rule for cluster {}'.format(cluster_id)
                )

            set_azure_perf_subscription()

    def get_capella_aws_cluster_vpc_id(self, cluster_node_hostname):
        command_template = (
            'env/bin/aws ec2 describe-instances --region $AWS_REGION '
            '--filter Name=ip-address,Values=$(dig +short {}) '
            '--query "Reservations[].Instances[].VpcId" '
            '--output text'
        )

        command = command_template.format(cluster_node_hostname)

        vpc_id = None
        stdout, _, returncode = run_local_shell_command(command)

        if returncode == 0:
            vpc_id = stdout.strip()
            logger.info('Found VPC ID: {}'.format(vpc_id))

        return vpc_id

    def get_capella_aws_cluster_security_group_id(self, vpc_id):
        command_template = (
            'env/bin/aws ec2 describe-security-groups --region $AWS_REGION '
            '--filters Name=vpc-id,Values={} Name=group-name,Values=default '
            '--query "SecurityGroups[*].GroupId" '
            '--output text'
        )

        command = command_template.format(vpc_id)

        sg_id = None
        stdout, _, returncode = run_local_shell_command(command)

        if returncode == 0:
            sg_id = stdout.strip()
            logger.info('Found Security Group ID: {}'.format(sg_id))

        return sg_id

    def set_nebula_log_levels(self):
        if dn_log_level := self.test_config.direct_nebula.log_level:
            self.remote.set_dn_log_level(dn_log_level)

        if dapi_log_level := self.test_config.data_api.log_level:
            self.remote.set_dapi_log_level(dapi_log_level)

    def deploy_couchbase_cluster(self):
        logger.info('Creating couchbase cluster')
        self.remote.create_couchbase_cluster()
        logger.info('Waiting for cluster')
        self.remote.wait_for_cluster_ready(timeout=1200)
