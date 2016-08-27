import logging
import os
import time
from multiprocessing import Event, Lock, Process, Value

from couchbase.exceptions import ValueFormatError
from couchbase.n1ql import MutationState, N1QLQuery
from decorator import decorator
from logger import logger
from psutil import cpu_count
from numpy import random
from twisted.internet import reactor

from spring.cbgen import (CBAsyncGen, CBGen, ElasticGen, FtsGen, N1QLGen,
                          SubDocGen)
from spring.docgen import (ExistingKey, KeyForCASUpdate,
                           KeyForRemoval, MergeDocument,
                           NewDocument, NewKey, NewLargeDocument,
                           NewNestedDocument, ReverseLookupDocument,
                           ReverseLookupDocumentArrayIndexing,
                           SequentialHotKey)
from spring.querygen import N1QLQueryGen, ViewQueryGen, ViewQueryGenByType


@decorator
def with_sleep(method, *args):
    self = args[0]
    if self.target_time is None:
        return method(self)
    else:
        t0 = time.time()
        method(self)
        actual_time = time.time() - t0
        delta = self.target_time - actual_time
        if delta > 0:
            time.sleep(self.CORRECTION_FACTOR * delta)
        else:
            self.fallingBehindCount += 1


def set_cpu_afinity(sid):
    os.system('taskset -p -c {} {}'.format(sid % cpu_count(), os.getpid()))


class Worker(object):

    CORRECTION_FACTOR = 0.975  # empiric!

    BATCH_SIZE = 100

    def __init__(self, workload_settings, target_settings,
                 shutdown_event=None):
        self.ws = workload_settings
        self.ts = target_settings
        self.shutdown_event = shutdown_event
        logger.setLevel(logging.INFO)

        self.existing_keys = ExistingKey(self.ws.working_set,
                                         self.ws.working_set_access,
                                         self.ts.prefix)
        self.new_keys = NewKey(self.ts.prefix, self.ws.expiration)
        self.keys_for_removal = KeyForRemoval(self.ts.prefix)

        if not hasattr(self.ws, 'doc_gen') or self.ws.doc_gen == 'old':
            self.docs = NewDocument(self.ws.size)
        elif self.ws.doc_gen == 'new':
            self.docs = NewNestedDocument(self.ws.size)
        elif self.ws.doc_gen == 'merge':
            is_random = True
            if self.ts.prefix == 'n1ql':
                is_random = False
            self.docs = MergeDocument(self.ws.size,
                                      self.ws.doc_partitions,
                                      is_random)
        elif self.ws.doc_gen == 'reverse_lookup':
            is_random = True
            if self.ts.prefix == 'n1ql':
                is_random = False
            self.docs = ReverseLookupDocument(self.ws.size,
                                              self.ws.doc_partitions,
                                              is_random)
        elif self.ws.doc_gen == 'reverse_lookup_array_indexing':
            is_random = True
            if self.ts.prefix == 'n1ql':
                is_random = False
            if self.ws.updates:
                # plus 10 to all values in array when updating doc
                self.docs = ReverseLookupDocumentArrayIndexing(
                    self.ws.size, self.ws.doc_partitions, self.ws.items,
                    delta=random.randint(0, 10))
            else:
                self.docs = ReverseLookupDocumentArrayIndexing(
                    self.ws.size, self.ws.doc_partitions, self.ws.items)
        elif self.ws.doc_gen == 'large_subdoc':
            self.docs = NewLargeDocument(self.ws.size)

        self.next_report = 0.05  # report after every 5% of completion

        host, port = self.ts.node.split(':')
        # Only FTS uses proxyPort and authless bucket right now.
        # Instead of jumping hoops to specify proxyPort in target
        # iterator/settings, which only passes down very specific attributes,
        # just detect fts instead. The following does not work with
        # authless bucket. FTS's worker does its own Couchbase.connect
        if not (hasattr(self.ws, "fts") and hasattr(
                self.ws.fts, "doc_database_url")):
            # default sasl bucket
            self.init_db({'bucket': self.ts.bucket, 'host': host, 'port': port,
                          'username': self.ts.bucket,
                          'password': self.ts.password})

        self.fallingBehindCount = 0

    def init_db(self, params):
        try:
            self.cb = CBGen(**params)
        except Exception as e:
            raise SystemExit(e)

    def report_progress(self, curr_ops):  # only first worker
        if not self.sid and self.ws.ops < float('inf') and \
                curr_ops > self.next_report * self.ws.ops:
            progress = 100.0 * curr_ops / self.ws.ops
            self.next_report += 0.05
            logger.info('Current progress: {:.2f} %'.format(progress))

    def time_to_stop(self):
        return (self.shutdown_event is not None and
                self.shutdown_event.is_set())


class KVWorker(Worker):

    def gen_cmd_sequence(self, cb=None, cases="cas"):
        ops = \
            ['c'] * self.ws.creates + \
            ['r'] * self.ws.reads + \
            ['u'] * self.ws.updates + \
            ['d'] * self.ws.deletes + \
            [cases] * self.ws.cases
        random.shuffle(ops)

        curr_items_tmp = curr_items_spot = self.curr_items.value
        if self.ws.creates:
            with self.lock:
                self.curr_items.value += self.ws.creates
                curr_items_tmp = self.curr_items.value - self.ws.creates
            curr_items_spot = (curr_items_tmp -
                               self.ws.creates * self.ws.workers)

        deleted_items_tmp = deleted_spot = 0
        if self.ws.deletes:
            with self.lock:
                self.deleted_items.value += self.ws.deletes
                deleted_items_tmp = self.deleted_items.value - self.ws.deletes
            deleted_spot = (deleted_items_tmp +
                            self.ws.deletes * self.ws.workers)

        if not cb:
            cb = self.cb

        cmds = []
        for op in ops:
            if op == 'c':
                curr_items_tmp += 1
                key, ttl = self.new_keys.next(curr_items_tmp)
                doc = self.docs.next(key)
                cmds.append((cb.create, (key, doc, ttl)))
            elif op == 'r':
                key = self.existing_keys.next(curr_items_spot, deleted_spot)
                if cases == 'counter':
                    cmds.append((cb.read, (key, self.ws.subdoc_fields)))
                else:
                    cmds.append((cb.read, (key, )))
            elif op == 'u':
                if cases == 'counter':
                    key = self.existing_keys.next(curr_items_spot, deleted_spot)
                    cmds.append((cb.update, (key, self.ws.subdoc_fields, self.ws.size)))
                else:
                    key = self.existing_keys.next(curr_items_spot, deleted_spot)
                    doc = self.docs.next(key)
                    cmds.append((cb.update, (key, doc)))
            elif op == 'd':
                deleted_items_tmp += 1
                key = self.keys_for_removal.next(deleted_items_tmp)
                cmds.append((cb.delete, (key, )))
            elif op == 'cas':
                key = self.existing_keys.next(curr_items_spot, deleted_spot)
                doc = self.docs.next(key)
                cmds.append((cb.cas, (key, doc)))
            elif op == 'counter':
                key = self.existing_keys.next(curr_items_spot, deleted_spot)
                cmds.append((cb.cas, (key, self.ws.subdoc_counter_fields)))
        return cmds

    @with_sleep
    def do_batch(self, *args, **kwargs):
        for cmd, args in self.gen_cmd_sequence():
            cmd(*args)

    def run_condition(self, curr_ops):
        if self.ws.operations:
            return curr_ops.value < self.ws.items
        else:
            return curr_ops.value < self.ws.ops and not self.time_to_stop()

    def run(self, sid, lock, curr_ops, curr_items, deleted_items):
        if self.ws.throughput < float('inf'):
            self.target_time = float(self.BATCH_SIZE) * self.ws.workers / \
                self.ws.throughput
        else:
            self.target_time = None
        self.sid = sid
        self.lock = lock
        self.curr_items = curr_items
        self.deleted_items = deleted_items

        logger.info('Started: worker-{}'.format(self.sid))
        try:
            while self.run_condition(curr_ops):
                with lock:
                    curr_ops.value += self.BATCH_SIZE
                self.do_batch()
                self.report_progress(curr_ops.value)
        except (KeyboardInterrupt, ValueFormatError):
            logger.info('Interrupted: worker-{}'.format(self.sid))
        else:
            logger.info('Finished: worker-{}'.format(self.sid))


class SubDocWorker(KVWorker):

    def __init__(self, workload_settings, target_settings, shutdown_event):
        super(SubDocWorker, self).__init__(workload_settings, target_settings,
                                           shutdown_event)
        host, port = self.ts.node.split(':')
        params = {'bucket': self.ts.bucket, 'host': host, 'port': port,
                  'username': self.ts.bucket, 'password': self.ts.password}
        self.cb = SubDocGen(**params)

    def gen_cmd_sequence(self, cb=None):
        return super(SubDocWorker, self).gen_cmd_sequence(cb, cases='counter')


class AsyncKVWorker(KVWorker):

    NUM_CONNECTIONS = 8

    def init_db(self, params):
        self.cbs = [CBAsyncGen(**params) for _ in range(self.NUM_CONNECTIONS)]
        self.counter = range(self.NUM_CONNECTIONS)

    def restart(self, _, cb, i):
        self.counter[i] += 1
        if self.counter[i] == self.BATCH_SIZE:
            actual_time = time.time() - self.time_started
            if self.target_time is not None:
                delta = self.target_time - actual_time
                if delta > 0:
                    time.sleep(self.CORRECTION_FACTOR * delta)

            self.report_progress(self.curr_ops.value)
            if not self.done and (
                    self.curr_ops.value >= self.ws.ops or self.time_to_stop()):
                with self.lock:
                    self.done = True
                logger.info('Finished: worker-{}'.format(self.sid))
                reactor.stop()
            else:
                self.do_batch(_, cb, i)

    def do_batch(self, _, cb, i):
        self.counter[i] = 0
        self.time_started = time.time()

        with self.lock:
            self.curr_ops.value += self.BATCH_SIZE

        for cmd, args in self.gen_cmd_sequence(cb):
            d = cmd(*args)
            d.addCallback(self.restart, cb, i)
            d.addErrback(self.log_and_restart, cb, i)

    def log_and_restart(self, err, cb, i):
        logger.warn('Request problem with worker-{} thread-{}: {}'.format(
            self.sid, i, err.value)
        )
        self.restart(None, cb, i)

    def error(self, err, cb, i):
        logger.warn('Connection problem with worker-{} thread-{}: {}'.format(
            self.sid, i, err)
        )

        cb.client._close()
        time.sleep(15)
        d = cb.client.connect()
        d.addCallback(self.do_batch, cb, i)
        d.addErrback(self.error, cb, i)

    def run(self, sid, lock, curr_ops, curr_items, deleted_items):
        set_cpu_afinity(sid)

        if self.ws.throughput < float('inf'):
            self.target_time = (self.BATCH_SIZE * self.ws.workers /
                                float(self.ws.throughput))
        else:
            self.target_time = None
        self.sid = sid
        self.lock = lock
        self.curr_items = curr_items
        self.deleted_items = deleted_items
        self.curr_ops = curr_ops

        self.done = False
        for i, cb in enumerate(self.cbs):
            d = cb.client.connect()
            d.addCallback(self.do_batch, cb, i)
            d.addErrback(self.error, cb, i)
        logger.info('Started: worker-{}'.format(self.sid))
        reactor.run()


class SeqReadsWorker(Worker):

    def run(self, sid, *args, **kwargs):
        set_cpu_afinity(sid)

        for key in SequentialHotKey(sid, self.ws, self.ts.prefix):
            self.cb.read(key)


class SeqUpdatesWorker(Worker):

    def run(self, sid, *args, **kwargs):
        for key in SequentialHotKey(sid, self.ws, self.ts.prefix):
            doc = self.docs.next(key)
            self.cb.update(key, doc)


class WorkerFactory(object):

    def __new__(cls, workload_settings):
        if getattr(workload_settings, 'async', False):
            worker = AsyncKVWorker
        elif getattr(workload_settings, 'seq_updates', False):
            worker = SeqUpdatesWorker
        elif getattr(workload_settings, 'seq_reads', False):
            worker = SeqReadsWorker
        elif not (getattr(workload_settings, 'seq_updates', False) or
                  getattr(workload_settings, 'seq_reads', False)):
            worker = KVWorker
        return worker, workload_settings.workers


class SubdocWorkerFactory(object):

    def __new__(cls, workload_settings):
        return SubDocWorker, workload_settings.subdoc_workers


class ViewWorkerFactory(object):

    def __new__(cls, workload_settings):
        return ViewWorker, workload_settings.query_workers


class QueryWorker(Worker):

    def __init__(self, workload_settings, target_settings, shutdown_event):
        super(QueryWorker, self).__init__(workload_settings, target_settings,
                                          shutdown_event)

    @with_sleep
    def do_batch(self):
        curr_items_spot = \
            self.curr_items.value - self.ws.creates * self.ws.workers
        deleted_spot = \
            self.deleted_items.value + self.ws.deletes * self.ws.workers

        for _ in xrange(self.BATCH_SIZE):
            key = self.existing_keys.next(curr_items_spot, deleted_spot)
            doc = self.docs.next(key)
            doc['key'] = key
            doc['bucket'] = self.ts.bucket
            ddoc_name, view_name, query = self.new_queries.next(doc)
            self.cb.query(ddoc_name, view_name, query=query)

    def run(self, sid, lock, curr_queries, curr_items, deleted_items):
        self.cb.start_updater()

        if self.throughput < float('inf'):
            self.target_time = float(self.BATCH_SIZE) * self.total_workers / \
                self.throughput
        else:
            self.target_time = None
        self.sid = sid
        self.curr_items = curr_items
        self.deleted_items = deleted_items
        self.curr_queries = curr_queries

        try:
            logger.info('Started: {}-{}'.format(self.name, self.sid))
            while curr_queries.value < self.ws.ops and not self.time_to_stop():
                with lock:
                    curr_queries.value += self.BATCH_SIZE
                self.do_batch()
                self.report_progress(curr_queries.value)
        except (KeyboardInterrupt, ValueFormatError, AttributeError) as e:
            logger.info('Interrupted: {}-{}-{}'.format(self.name, self.sid, e))
        else:
            if self.fallingBehindCount > 0:
                logger.info('Worker {0} fell behind {1} times.'
                            .format(self.name, self.fallingBehindCount))
            logger.info('Finished: {}-{}'.format(self.name, self.sid))


class ViewWorker(QueryWorker):

    def __init__(self, workload_settings, target_settings, shutdown_event):
        super(ViewWorker, self).__init__(workload_settings, target_settings,
                                         shutdown_event)
        self.total_workers = self.ws.query_workers
        self.throughput = self.ws.query_throughput
        self.name = 'query-worker'

        if workload_settings.index_type is None:
            self.new_queries = ViewQueryGen(workload_settings.ddocs,
                                            workload_settings.qparams)
        else:
            self.new_queries = ViewQueryGenByType(workload_settings.index_type,
                                                  workload_settings.qparams)


class N1QLWorkerFactory(object):

    def __new__(cls, workload_settings):
        return N1QLWorker, workload_settings.n1ql_workers


class N1QLWorker(Worker):

    def __init__(self, workload_settings, target_settings, shutdown_event):
        super(N1QLWorker, self).__init__(workload_settings, target_settings,
                                         shutdown_event)
        self.new_queries = N1QLQueryGen(workload_settings.n1ql_queries)
        self.total_workers = self.ws.n1ql_workers
        self.throughput = self.ws.n1ql_throughput
        self.name = 'n1ql-worker'

        host, port = self.ts.node.split(':')
        bucket = self.ts.bucket
        if workload_settings.n1ql_op == 'ryow':
            bucket += '?fetch_mutation_tokens=true'

        params = {'bucket': bucket, 'host': host, 'port': port,
                  'username': self.ts.bucket, 'password': self.ts.password}

        self.existing_keys = ExistingKey(self.ws.working_set,
                                         self.ws.working_set_access,
                                         'n1ql')
        self.new_keys = NewKey('n1ql', self.ws.expiration)
        self.keys_for_removal = KeyForRemoval('n1ql')
        self.keys_for_casupdate = KeyForCASUpdate(self.total_workers, self.ws.working_set,
                                                  self.ws.working_set_access,
                                                  'n1ql')

        if self.ws.doc_gen == 'merge':
            self.docs = MergeDocument(self.ws.size,
                                      self.ws.doc_partitions,
                                      False)
        elif self.ws.doc_gen == 'reverse_lookup':
            self.docs = ReverseLookupDocument(self.ws.size,
                                              self.ws.doc_partitions,
                                              False)
        elif self.ws.doc_gen == 'reverse_lookup_array_indexing':
            if self.ws.updates:
                self.docs = ReverseLookupDocumentArrayIndexing(
                    self.ws.size, self.ws.doc_partitions, self.ws.items,
                    delta=random.randint(0, 10))
            else:
                self.docs = ReverseLookupDocumentArrayIndexing(
                    self.ws.size, self.ws.doc_partitions, self.ws.items)
        self.cb = N1QLGen(**params)

    @with_sleep
    def do_batch(self):
        if self.ws.n1ql_op == 'read':
            curr_items_spot = \
                self.curr_items.value - self.ws.creates * self.ws.workers
            deleted_spot = \
                self.deleted_items.value + self.ws.deletes * self.ws.workers
            for _ in xrange(self.BATCH_SIZE):
                key = self.existing_keys.next(curr_items_spot, deleted_spot)
                doc = self.docs.next(key)
                doc['key'] = key
                doc['bucket'] = self.ts.bucket
                ddoc_name, view_name, query = self.new_queries.next(doc)
                self.cb.query(ddoc_name, view_name, query=query)
            return

        curr_items_tmp = curr_items_spot = self.curr_items.value
        if self.ws.n1ql_op == 'create':
            with self.lock:
                self.curr_items.value += self.BATCH_SIZE
                curr_items_tmp = self.curr_items.value - self.BATCH_SIZE
            curr_items_spot = (curr_items_tmp -
                               self.BATCH_SIZE * self.total_workers)

        deleted_items_tmp = deleted_spot = 0
        if self.ws.n1ql_op == 'delete':
            with self.lock:
                self.deleted_items.value += self.BATCH_SIZE
                deleted_items_tmp = self.deleted_items.value - self.BATCH_SIZE
            deleted_spot = (deleted_items_tmp +
                            self.BATCH_SIZE * self.total_workers)

        deleted_capped_items_tmp = 0
        if self.ws.n1ql_op == 'rangedelete':
            with self.lock:
                self.deleted_capped_items.value += self.BATCH_SIZE
                deleted_capped_items_tmp = self.deleted_capped_items.value - self.BATCH_SIZE

        if self.ws.n1ql_op == 'update':
            with self.lock:
                self.casupdated_items.value += self.BATCH_SIZE

        if self.ws.n1ql_op == 'create':
            for _ in xrange(self.BATCH_SIZE):
                curr_items_tmp += 1
                key, ttl = self.new_keys.next(curr_items_tmp)
                doc = self.docs.next(key)
                doc['key'] = key
                doc['bucket'] = self.ts.bucket
                ddoc_name, view_name, query = self.new_queries.next(doc)
                self.cb.query(ddoc_name, view_name, query=query)

        elif self.ws.n1ql_op == 'delete':
            for _ in xrange(self.BATCH_SIZE):
                deleted_items_tmp += 1
                key = self.keys_for_removal.next(deleted_items_tmp)
                doc = self.docs.next(key)
                doc['key'] = key
                doc['bucket'] = self.ts.bucket
                ddoc_name, view_name, query = self.new_queries.next(doc)
                self.cb.query(ddoc_name, view_name, query=query)

        elif self.ws.n1ql_op == 'update' or self.ws.n1ql_op == 'lookupupdate':
            for _ in xrange(self.BATCH_SIZE):
                key = self.keys_for_casupdate.next(self.sid, curr_items_spot, deleted_spot)
                doc = self.docs.next(key)
                doc['key'] = key
                doc['bucket'] = self.ts.bucket
                ddoc_name, view_name, query = self.new_queries.next(doc)
                self.cb.query(ddoc_name, view_name, query=query)

        elif self.ws.n1ql_op == 'ryow':
            for _ in xrange(self.BATCH_SIZE):
                query = self.ws.n1ql_queries[0]['statement'][1:-1]
                if self.ws.n1ql_queries[0]['prepared'] == "singleton_unique_lookup":
                    by_key = 'email'
                elif self.ws.n1ql_queries[0]['prepared'] == "range_scan":
                    by_key = 'capped_small'
                else:
                    logger.error('n1ql_queries {} not defined'.format(self.ws.n1ql_queries))
                key1 = self.keys_for_casupdate.next(self.sid, curr_items_spot, deleted_spot)
                doc1 = self.docs.next(key1)
                key2 = self.keys_for_casupdate.next(self.sid, curr_items_spot, deleted_spot)
                doc2 = self.docs.next(key2)
                rvs = self.cb.client.upsert_multi({key1: doc2, key2: doc1})
                # This is a part of requirements:
                # Each n1ql worker sleeps for 1 seconds.
                time.sleep(float(self.ws.n1ql_queries[0]['time_sleep']))
                ms = MutationState()
                ms.add_results(*rvs.values())
                nq = N1QLQuery(query.format(doc2[by_key]))
                nq.consistent_with(ms)
                len(list(self.cb.client.n1ql_query(nq)))

        elif self.ws.n1ql_op == 'rangeupdate':
            for _ in xrange(self.BATCH_SIZE):
                key = self.keys_for_casupdate.next(self.sid, curr_items_spot, deleted_spot)
                doc = self.docs.next(key)
                doc['key'] = key
                doc['bucket'] = self.ts.bucket
                ddoc_name, view_name, query = self.new_queries.next(doc)
                self.cb.query(ddoc_name, view_name, query=query)

        elif self.ws.n1ql_op == 'rangedelete':
            for _ in xrange(self.BATCH_SIZE):
                doc = {}
                doc['capped_small'] = "n1ql-_100_" + str(deleted_capped_items_tmp)
                ddoc_name, view_name, query = self.new_queries.next(doc)
                self.cb.query(ddoc_name, view_name, query=query)
                deleted_capped_items_tmp += 1

        elif self.ws.n1ql_op == 'merge':  # run select * workload for merge
            for _ in xrange(self.BATCH_SIZE):
                key = self.existing_keys.next(curr_items_spot, deleted_spot)
                doc = self.docs.next(key)
                doc['key'] = key
                doc['bucket'] = self.ts.bucket
                ddoc_name, view_name, query = self.new_queries.next(doc)
                query['statement'] = "SELECT * FROM `bucket-1` USE KEYS[$1];"
                query['args'] = "[\"{key}\"]".format(**doc)
                del query['prepared']
                self.cb.query(ddoc_name, view_name, query=query)

    def run(self, sid, lock, curr_queries, curr_items, deleted_items,
            casupdated_items, deleted_capped_items):
        self.cb.start_updater()

        if self.throughput < float('inf'):
            self.target_time = float(self.BATCH_SIZE) * self.total_workers / \
                self.throughput
        else:
            self.target_time = None
        self.lock = lock
        self.sid = sid
        self.curr_items = curr_items
        self.deleted_items = deleted_items
        self.deleted_capped_items = deleted_capped_items
        self.casupdated_items = casupdated_items
        self.curr_queries = curr_queries

        try:
            logger.info('Started: {}-{}'.format(self.name, self.sid))
            while curr_queries.value < self.ws.ops and not self.time_to_stop():
                with self.lock:
                    curr_queries.value += self.BATCH_SIZE
                self.do_batch()
                self.report_progress(curr_queries.value)
        except (KeyboardInterrupt, ValueFormatError, AttributeError) as e:
            logger.info('Interrupted: {}-{}-{}'.format(self.name, self.sid, e))
        else:
            if self.fallingBehindCount > 0:
                logger.info('Worker {0} fell behind {1} times.'.
                            format(self.name, self.fallingBehindCount))
            logger.info('Finished: {}-{}'.format(self.name, self.sid))


class FtsWorkerFactory(object):

    def __new__(cls, workload_settings):
        '''
         For FTS worker one extra worker is added,
         this worker will do the log collection
        '''
        if workload_settings.fts_config:
            return FtsWorker, workload_settings.fts_config.worker
        return FtsWorker, 0


class FtsWorker(Worker):
    BATCH_SIZE = 100

    def __init__(self, workload_settings, target_settings, shutdown_event=None):
        super(FtsWorker, self).__init__(workload_settings, target_settings,
                                        shutdown_event)
        host, port = self.ts.node.split(':')
        if self.ws.fts_config.elastic:
            instance = ElasticGen(host, self.ws.fts_config)
            self.name = "ElasticWorker"
        else:
            instance = FtsGen(host, self.ws.fts_config)
            self.name = "FtsWorker"

        self.fts_es_query = instance
        self.fts_es_query.prepare_query()
        self.count = 0

    def do_check_result(self, r):
        '''
        Check whether the query returned 0 hits
        '''
        if self.ws.fts_config.elastic:
            return r.json()["hits"]["total"] == 0
        return r.json()['total_hits'] == 0

    def do_batch(self):
        '''
        need to store the info if any zero hit, no results
        we cant do for all workers. one worker will do this,
        all inputs are from iterator, so it will have a fair
        amount to proper result. Last worker is scheduled to
        do the logging stuff.
        '''
        for i in range(self.BATCH_SIZE):
            if not self.time_to_stop():
                cmd, args = self.fts_es_query.next()
                if self.sid == self.ws.fts_config.worker - 1:
                    '''
                    collect stats for last worker
                    '''
                    try:
                        r = cmd(**args)
                        '''
                        increment in sinle thread, so llock needed
                        '''
                        self.count += 1
                        if self.count % 10000 == 0:
                            '''
                             Dump a sample of queries
                             '''
                            logger.info(args)
                            logger.info(r.text)
                        if not self.ws.fts_config.logfile:
                            '''
                             Error Checking if logfile is missing test file
                            '''
                            continue
                        f = open(self.ws.fts_config.logfile, 'a')
                        if r.status_code not in range(200, 203) \
                                or self.do_check_result(r):
                            f.write(str(args))
                            f.write(str(r.status_code))
                            f.write(str(r.text))
                        f.close()
                    except IOError as e:
                        logger.info("I/O error({0}): {1}".format(e.errno, e.strerror))
                else:
                    '''
                     Only running the rest API no error checking
                    '''
                    cmd(**args)

    def run(self, sid, lock):
        logger.info("Started {}".format(self.name))
        self.sid = sid
        try:
            logger.info('Started: {}-{}'.format(self.name, self.sid))
            while not self.time_to_stop():
                self.do_batch()
        except (KeyboardInterrupt, ValueFormatError, AttributeError) as e:
            logger.info('Interrupted: {}-{}-{}'.format(self.name, self.sid, e))
        else:
            logger.info('Finished: {}-{}'.format(self.name, self.sid))


class WorkloadGen(object):

    def __init__(self, workload_settings, target_settings, timer=None):
        self.ws = workload_settings
        self.ts = target_settings
        self.timer = timer
        self.shutdown_event = timer and Event() or None
        self.workers = {}

    def start_workers(self, worker_factory, name, curr_items=None,
                      deleted_items=None, casupdated_items=None,
                      deleted_capped_items=None):
        curr_ops = Value('L', 0)
        lock = Lock()
        worker_type, total_workers = worker_factory(self.ws)
        self.workers[name] = list()
        for sid in range(total_workers):
            if curr_items is None and deleted_items is None:
                args = (sid, lock)
            elif casupdated_items is not None or deleted_capped_items is not None:
                args = (sid, lock, curr_ops, curr_items, deleted_items,
                        casupdated_items, deleted_capped_items)
            else:
                args = (sid, lock, curr_ops, curr_items, deleted_items)
            worker = worker_type(self.ws, self.ts, self.shutdown_event)
            worker_process = Process(target=worker.run, args=tuple(args))
            worker_process.start()
            self.workers[name].append(worker_process)
            if getattr(self.ws, 'async', False):
                time.sleep(2)

    def wait_for_all_workers(self):
        for workers in self.workers.values():
            for worker in workers:
                worker.join()
                if worker.exitcode:
                    logger.interrupt('Worker finished with non-zero exit code')

    def run(self):
        curr_items = Value('L', self.ws.items)
        deleted_items = Value('L', 0)
        deleted_capped_items = Value('L', 0)
        casupdated_items = Value('L', 0)

        logger.info('Starting all workers')

        self.start_workers(WorkerFactory, 'kv', curr_items, deleted_items)
        self.start_workers(SubdocWorkerFactory, 'subdoc', curr_items, deleted_items)
        self.start_workers(ViewWorkerFactory, 'view', curr_items, deleted_items)
        self.start_workers(N1QLWorkerFactory, 'n1ql', curr_items, deleted_items,
                           casupdated_items, deleted_capped_items)
        self.start_workers(FtsWorkerFactory, 'fts')

        if self.timer:
            time.sleep(self.timer)
            self.shutdown_event.set()
        self.wait_for_all_workers()
