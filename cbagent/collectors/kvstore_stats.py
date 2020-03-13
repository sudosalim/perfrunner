import json

from cbagent.collectors import Collector
from perfrunner.helpers.local import extract_cb_any, get_cbstats


class KVStoreStats(Collector):
    COLLECTOR = "kvstore_stats"
    CB_STATS_PORT = 11209
    METRICS_ACROSS_SHARDS = (
        "BlockCacheQuota",
        "WriteCacheQuota",
        "BlockCacheMemUsed",
        "BlockCacheHits",
        "BlockCacheMisses",
        "BloomFilterMemUsed",
        "BytesIncoming",
        "BytesOutgoing",
        "BytesPerRead",
        "IndexBlocksSize",
        "MemoryQuota",
        "NCommitBatches",
        "NDeletes",
        "NGets",
        "NReadBytes",
        "NReadBytesCompact",
        "NReadBytesGet",
        "NReadIOs",
        "NReadIOsGet",
        "NSets",
        "NSyncs",
        "NTablesCreated",
        "NTablesDeleted",
        "NWriteBytes",
        "NWriteBytesCompact",
        "NWriteIOs",
        "TotalMemUsed",
        "WriteCacheMemUsed",
        "NCompacts",
        "ReadAmp",
        "ReadAmpGet",
        "ReadIOAmp",
        "WriteAmp"
    )
    METRICS_AVERAGE_PER_NODE_PER_SHARD = (
        "ReadAmp",
        "ReadAmpGet",
        "ReadIOAmp",
        "WriteAmp"
    )

    def __init__(self, settings, test):
        super().__init__(settings)
        extract_cb_any(filename='couchbase')
        self.collect_per_server_stats = test.collect_per_server_stats
        self.cluster_spec = test.cluster_spec

    def _get_stats_from_server(self, bucket: str, server: str):
        stats = {}
        try:
            result = get_cbstats(server, self.CB_STATS_PORT, "kvstore", self.cluster_spec)
            buckets_data = list(filter(lambda a: a != "", result.split("*")))
            for data in buckets_data:
                data = data.strip()
                if data.startswith(bucket):
                    data = data.split("\n", 1)[1]
                    data = data.replace("\"{", "{")
                    data = data.replace("}\"", "}")
                    data = data.replace("\\", "")
                    data = json.loads(data)
                    for (shard, metrics) in data.items():
                        if not shard.endswith(":magma"):
                            continue
                        for metric in self.METRICS_ACROSS_SHARDS:
                            if metric in metrics.keys():
                                if metric in stats:
                                    stats[metric] += metrics[metric]
                                else:
                                    stats[metric] = metrics[metric]
                    break
        except Exception:
            pass

        return stats

    def _get_kvstore_stats(self, bucket: str, server: str):
        node_stats = self._get_stats_from_server(bucket, server=server)
        return node_stats

    def _get_num_shards(self, bucket: str, server: str):
        result = get_cbstats(server, self.CB_STATS_PORT, "workload", self.cluster_spec)
        buckets_data = list(filter(lambda a: a != "", result.split("*")))
        for data in buckets_data:
            data = data.strip()
            if data.startswith(bucket):
                data = data.split("\n", 1)[1]
                data = data.replace("\"{", "{")
                data = data.replace("}\"", "}")
                data = data.replace("\\", "")
                data = json.loads(data)
                return data["ep_workload:num_shards"]
        return 1

    def sample(self):
        if self.collect_per_server_stats:
            for node in self.nodes:
                for bucket in self.get_buckets():
                    num_shards = self._get_num_shards(bucket, self.master_node)
                    stats = self._get_kvstore_stats(bucket, node)
                    for metric in self.METRICS_AVERAGE_PER_NODE_PER_SHARD:
                        if metric in stats:
                            if stats[metric] / num_shards >= 50:
                                stats[metric] = 50
                            else:
                                stats[metric] /= num_shards

                    if stats:
                        self.update_metric_metadata(stats.keys(), server=node, bucket=bucket)
                        self.store.append(stats, cluster=self.cluster,
                                          bucket=bucket, server=node,
                                          collector=self.COLLECTOR)

        for bucket in self.get_buckets():
            stats = {}
            num_shards = self._get_num_shards(bucket, self.master_node)
            num_nodes = len(self.nodes)
            for node in self.nodes:
                temp_stats = self._get_kvstore_stats(bucket, node)
                for st in temp_stats:
                    if st in stats:
                        stats[st] += temp_stats[st]
                    else:
                        stats[st] = temp_stats[st]

            for metric in self.METRICS_AVERAGE_PER_NODE_PER_SHARD:
                if metric in stats:
                    if stats[metric]/(num_shards * num_nodes) >= 50:
                        stats[metric] = 50
                    else:
                        stats[metric] /= (num_shards * num_nodes)

            if stats:
                self.update_metric_metadata(stats.keys(), bucket=bucket)
                self.store.append(stats, cluster=self.cluster,
                                  bucket=bucket,
                                  collector=self.COLLECTOR)

    def update_metadata(self):
        self.mc.add_cluster()

        for bucket in self.get_buckets():
            self.mc.add_bucket(bucket)
        for node in self.nodes:
            self.mc.add_server(node)
