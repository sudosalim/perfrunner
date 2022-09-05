from time import sleep, time

import numpy
from couchbase.bucket import Bucket

from cbagent.collectors.latency import Latency
from logger import logger
from spring.docgen import Document, Key


def new_client(host, bucket, password, timeout):
    connection_string = 'couchbase://{}/{}?password={}'
    connection_string = connection_string.format(host,
                                                 bucket,
                                                 password)
    client = Bucket(connection_string=connection_string)
    client.timeout = timeout
    return client


class XdcrLag(Latency):

    COLLECTOR = "xdcr_lag"

    METRICS = "xdcr_lag",

    INITIAL_POLLING_INTERVAL = 0.001  # 1 ms

    TIMEOUT = 600  # 10 minutes

    MAX_SAMPLING_INTERVAL = 0.25  # 250 ms

    def __init__(self, settings, workload):
        super().__init__(settings)

        self.interval = self.MAX_SAMPLING_INTERVAL

        self.clients = []
        for bucket in self.get_buckets():
            src_client = new_client(host=settings.master_node,
                                    bucket=bucket,
                                    password=settings.bucket_password,
                                    timeout=self.TIMEOUT)
            dst_client = new_client(host=settings.dest_master_node,
                                    bucket=bucket,
                                    password=settings.bucket_password,
                                    timeout=self.TIMEOUT)
            self.clients.append((bucket, src_client, dst_client))

        self.new_docs = Document(workload.size)

    @staticmethod
    def gen_key() -> Key:
        return Key(number=numpy.random.random_integers(0, 10 ** 9),
                   prefix='xdcr',
                   fmtr='hex')

    def measure(self, src_client, dst_client):
        key = self.gen_key()
        doc = self.new_docs.next(key)

        polling_interval = self.INITIAL_POLLING_INTERVAL

        src_client.upsert(key.string, doc)

        t0 = time()
        while time() - t0 < self.TIMEOUT:
            if dst_client.get(key.string, quiet=True).success:
                break
            sleep(polling_interval)
            polling_interval *= 1.05  # increase interval by 5%
        else:
            logger.warn('XDCR sampling timed out after {} seconds'
                        .format(self.TIMEOUT))
        t1 = time()

        src_client.remove(key.string, quiet=True)
        dst_client.remove(key.string, quiet=True)

        return {'xdcr_lag': (t1 - t0) * 1000}  # s -> ms

    def sample(self):
        for bucket, src_client, dst_client in self.clients:
            lags = self.measure(src_client, dst_client)
            self.append_to_store(lags,
                                 cluster=self.cluster,
                                 bucket=bucket,
                                 collector=self.COLLECTOR)
