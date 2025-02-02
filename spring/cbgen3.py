import time
from ctypes import CDLL
from datetime import timedelta
from urllib import parse

import requests
from couchbase.cluster import (
    Cluster,
    ClusterOptions,
    ClusterTimeoutOptions,
    QueryOptions,
)
from couchbase.management.collections import CollectionSpec
from couchbase.management.users import User
from couchbase_core.cluster import PasswordAuthenticator
from couchbase_core.views.params import ViewQuery
from requests.auth import HTTPBasicAuth
from txcouchbase.cluster import TxCluster

from spring.cbgen_helpers import backoff, quiet, time_all, timeit


class CBAsyncGen3:

    TIMEOUT = 120  # seconds

    def __init__(self, **kwargs):
        connection_string = 'couchbase://{host}'
        connection_string = connection_string.format(host=kwargs['host'])
        pass_auth = PasswordAuthenticator(kwargs['username'], kwargs['password'])
        if kwargs["ssl_mode"] == 'n2n' or kwargs["ssl_mode"] == 'capella':
            connection_string = connection_string.replace('couchbase',
                                                          'couchbases')
            connection_string += '?certpath=root.pem' + "&sasl_mech_force=PLAIN"
        timeout = ClusterTimeoutOptions(kv_timeout=timedelta(seconds=self.TIMEOUT))
        options = ClusterOptions(authenticator=pass_auth, timeout_options=timeout)
        self.cluster = TxCluster(connection_string=connection_string, options=options)
        self.bucket_name = kwargs['bucket']
        self.collections = dict()
        self.collection = None

    def connect_collections(self, scope_collection_list):
        self.bucket = self.cluster.bucket(self.bucket_name)
        for scope_collection in scope_collection_list:
            scope, collection = scope_collection.split(":")
            if scope == "_default" and collection == "_default":
                self.collections[scope_collection] = \
                    self.bucket.default_collection()
            else:
                self.collections[scope_collection] = \
                    self.bucket.scope(scope).collection(collection)

    def create(self, *args, **kwargs):
        self.collection = self.collections[args[0]]
        return self.do_create(*args[1:], **kwargs)

    def do_create(self, key: str, doc: dict, persist_to: int = 0,
                  replicate_to: int = 0, ttl: int = 0):
        return self.collection.upsert(key, doc,
                                      persist_to=persist_to,
                                      replicate_to=replicate_to,
                                      ttl=ttl)

    def create_durable(self, *args, **kwargs):
        self.collection = self.collections[args[0]]
        return self.do_create_durable(*args[1:], **kwargs)

    def do_create_durable(self, key: str, doc: dict, durability: int = None, ttl: int = 0):
        return self.collection.upsert(key, doc,
                                      durability_level=durability,
                                      ttl=ttl)

    def read(self, *args, **kwargs):
        self.collection = self.collections[args[0]]
        return self.do_read(*args[1:], **kwargs)

    def do_read(self, key: str):
        return self.collection.get(key)

    def update(self, *args, **kwargs):
        self.collection = self.collections[args[0]]
        return self.do_update(*args[1:], **kwargs)

    def do_update(self, key: str, doc: dict, persist_to: int = 0,
                  replicate_to: int = 0, ttl: int = 0):
        return self.collection.upsert(key,
                                      doc,
                                      persist_to=persist_to,
                                      replicate_to=replicate_to,
                                      ttl=ttl)

    def update_durable(self, *args, **kwargs):
        self.collection = self.collections[args[0]]
        return self.do_update_durable(*args[1:], **kwargs)

    def do_update_durable(self, key: str, doc: dict, durability: int = None, ttl: int = 0):
        return self.collection.upsert(key, doc,
                                      durability_level=durability,
                                      ttl=ttl)

    def delete(self, *args, **kwargs):
        self.collection = self.collections[args[0]]
        return self.do_delete(*args[1:], **kwargs)

    def do_delete(self, key: str):
        return self.collection.remove(key)


class CBGen3(CBAsyncGen3):

    TIMEOUT = 600  # seconds
    N1QL_TIMEOUT = 600

    def __init__(self, ssl_mode: str = 'none', n1ql_timeout: int = None, **kwargs):
        connection_string = 'couchbase://{host}?{params}'
        connstr_params = parse.urlencode(kwargs["connstr_params"])

        if ssl_mode in ['data', 'n2n', 'capella', 'nebula', 'dapi']:
            connection_string = connection_string.replace('couchbase',
                                                          'couchbases')
            if ssl_mode in ['nebula', 'dapi']:
                connection_string += '&ssl=no_verify'
            else:
                connection_string += '&certpath=root.pem'
            connection_string += '&sasl_mech_force=PLAIN'

        connection_string = connection_string.format(host=kwargs['host'],
                                                     params=connstr_params)

        pass_auth = PasswordAuthenticator(kwargs['username'], kwargs['password'])
        timeout = ClusterTimeoutOptions(
            kv_timeout=timedelta(seconds=self.TIMEOUT),
            query_timeout=timedelta(
                seconds=n1ql_timeout if n1ql_timeout else self.N1QL_TIMEOUT)
        )
        options = ClusterOptions(authenticator=pass_auth, timeout_options=timeout)
        self.cluster = Cluster(connection_string=connection_string, options=options)
        self.bucket_name = kwargs['bucket']
        self.bucket = None
        self.collections = dict()
        self.collection = None

    def create(self, *args, **kwargs):
        self.collection = self.collections[args[0]]
        return self.do_create(*args[1:], **kwargs)

    @quiet
    @backoff
    def do_create(self, *args, **kwargs):
        super().do_create(*args, **kwargs)

    def create_durable(self, *args, **kwargs):
        self.collection = self.collections[args[0]]
        return self.do_create_durable(*args[1:], **kwargs)

    @quiet
    @backoff
    def do_create_durable(self, *args, **kwargs):
        super().do_create_durable(*args, **kwargs)

    def read(self, *args, **kwargs):
        self.collection = self.collections[args[0]]
        return self.do_read(*args[1:], **kwargs)

    def get(self, *args, **kwargs):
        self.collection = self.collections[args[0]]
        return self.do_get(*args[1:], **kwargs)

    def do_get(self, *args, **kwargs):
        return super().do_read(*args, **kwargs)

    @time_all
    def do_read(self, *args, **kwargs):
        super().do_read(*args, **kwargs)

    def set(self, *args, **kwargs):
        self.collection = self.collections[args[0]]
        return self.do_update(*args[1:], **kwargs)

    def do_set(self, *args, **kwargs):
        return super().do_update(*args, **kwargs)

    @time_all
    def do_update(self, *args, **kwargs):
        super().do_update(*args, **kwargs)

    def update_durable(self, *args, **kwargs):
        self.collection = self.collections[args[0]]
        return self.do_update_durable(*args[1:], **kwargs)

    @time_all
    def do_update_durable(self, *args, **kwargs):
        super().do_update_durable(*args, **kwargs)

    def delete(self, *args, **kwargs):
        self.collection = self.collections[args[0]]
        return self.do_delete(*args[1:], **kwargs)

    @quiet
    def do_delete(self, *args, **kwargs):
        super().do_delete(*args, **kwargs)

    @timeit
    def view_query(self, ddoc: str, view: str, query: ViewQuery):
        tuple(self.cluster.view_query(ddoc, view, query=query))

    @quiet
    @timeit
    def n1ql_query(self, n1ql_query: str, options: QueryOptions):
        libc = CDLL("libc.so.6")
        libc.srand(int(time.time_ns()))
        tuple(self.cluster.query(n1ql_query, options))

    def create_user_manager(self):
        self.user_manager = self.cluster.users()

    def create_collection_manager(self):
        self.collection_manager = self.cluster.bucket(self.bucket_name).collections()

    @quiet
    @backoff
    def do_upsert_user(self, *args, **kwargs):
        return self.user_manager.upsert_user(User(username=args[0],
                                                  roles=args[1],
                                                  password=args[2]))

    def get_roles(self):
        return self.user_manager.get_roles()

    def do_collection_create(self, *args, **kwargs):
        self.collection_manager.create_collection(
            CollectionSpec(scope_name=args[0],
                           collection_name=args[1]))

    def do_collection_drop(self, *args, **kwargs):
        self.collection_manager.drop_collection(
            CollectionSpec(scope_name=args[0],
                           collection_name=args[1]))


class DAPIGen:

    TIMEOUT = 600  # seconds
    N1QL_TIMEOUT = 600
    DURABILITY = ['majority', 'majorityPersistActive', 'persistMajority']

    def __init__(self, **kwargs):
        self.base_url = kwargs['host']
        self.auth = HTTPBasicAuth(kwargs['username'], kwargs['password'])
        self.bucket_name = kwargs['bucket']
        self.n1ql_timeout = kwargs.get('n1ql_timeout', self.N1QL_TIMEOUT)
        self.meta = 'true' if kwargs['meta'] else 'false'
        self.logs = 'true' if kwargs['logs'] else 'false'
        self.session = None

    def connect_collections(self, scope_collection_list):
        self.session = requests.Session()

    @time_all
    def create(self, target: str, key: str, doc: dict, persist_to: int = 0, replicate_to: int = 0,
               ttl: int = 0):
        scope, collection = target.split(':')
        api = 'https://{}/v1/scopes/{}/collections/{}/docs/{}'.format(
            self.base_url,
            scope,
            collection,
            key
        )
        api += '?meta={}&logs={}&timeout={}s&expiry={}s'.format(
            self.meta,
            self.logs,
            self.TIMEOUT,
            ttl
        )
        headers = {'Content-Type': 'application/json'}
        resp = self.session.post(url=api, auth=self.auth, headers=headers, json=doc)
        resp.raise_for_status()
        return resp

    @time_all
    def create_durable(self, target: str, key: str, doc: dict, durability: int = None,
                       ttl: int = 0):
        scope, collection = target.split(':')
        api = 'https://{}/v1/scopes/{}/collections/{}/docs/{}'.format(
            self.base_url,
            scope,
            collection,
            key
        )
        api += '?meta={}&logs={}&timeout={}s&expiry={}s'.format(
            self.meta,
            self.logs,
            self.TIMEOUT,
            ttl
        )
        if durability is not None and durability > 0:
            api += '&durability={}'.format(self.DURABILITY[durability])
        headers = {'Content-Type': 'application/json'}
        resp = self.session.post(url=api, auth=self.auth, headers=headers, json=doc)
        resp.raise_for_status()
        return resp

    @time_all
    def read(self, target: str, key: str):
        scope, collection = target.split(':')
        api = 'https://{}/v1/scopes/{}/collections/{}/docs/{}'.format(
            self.base_url,
            scope,
            collection,
            key
        )
        api += '?meta={}&logs={}&timeout={}s'.format(
            self.meta,
            self.logs,
            self.TIMEOUT
        )
        headers = {'Content-Type': 'application/json'}
        resp = self.session.get(url=api, auth=self.auth, headers=headers)
        resp.raise_for_status()
        return resp

    @time_all
    def update(self, target: str, key: str, doc: dict, persist_to: int = 0, replicate_to: int = 0,
               ttl: int = 0):
        scope, collection = target.split(':')
        api = 'https://{}/v1/scopes/{}/collections/{}/docs/{}'.format(
            self.base_url,
            scope,
            collection,
            key
        )
        api += '?meta={}&logs={}&timeout={}s&expiry={}s&upsert=true'.format(
            self.meta,
            self.logs,
            self.TIMEOUT,
            ttl
        )
        headers = {'Content-Type': 'application/json'}
        resp = self.session.put(url=api, auth=self.auth, headers=headers, json=doc)
        resp.raise_for_status()
        return resp

    @time_all
    def update_durable(self, target: str, key: str, doc: dict, durability: int = None,
                       ttl: int = 0):
        scope, collection = target.split(':')
        api = 'https://{}/v1/scopes/{}/collections/{}/docs/{}'.format(
            self.base_url,
            scope,
            collection,
            key
        )
        api += '?meta={}&logs={}&timeout={}s&expiry={}s&upsert=true'.format(
            self.meta,
            self.logs,
            self.TIMEOUT,
            ttl
        )
        if durability is not None and durability > 0:
            api += '&durability={}'.format(self.DURABILITY[durability])
        headers = {'Content-Type': 'application/json'}
        resp = self.session.put(url=api, auth=self.auth, headers=headers, json=doc)
        resp.raise_for_status()
        return resp

    @time_all
    def delete(self, target: str, key: str):
        scope, collection = target.split(':')
        api = 'https://{}/v1/scopes/{}/collections/{}/docs/{}'.format(
            self.base_url,
            scope,
            collection,
            key
        )
        api += '?meta={}&logs={}&timeout={}s'.format(
            self.meta,
            self.logs,
            self.TIMEOUT
        )
        headers = {'Content-Type': 'application/json'}
        resp = self.session.delete(url=api, auth=self.auth, headers=headers)
        resp.raise_for_status()
        return resp

    @timeit
    def n1ql_query(self, n1ql_query: str, options: QueryOptions):
        scope = options['query_context'].split(':')[1]
        api = 'https://{}/v1/scopes/{}/query'.format(self.base_url, scope)

        params = [
            'meta={}'.format(self.meta),
            'logs={}'.format(self.logs),
            'timeout={}s'.format(self.n1ql_timeout),
            (
                'preserveExpiry={}'.format(str(v).lower())
                if (v := options.get('preserve_expiry', False)) else None
            ),
            (
                'readonly={}'.format(str(v).lower())
                if (v := options.get('read_only', False)) else None
            ),
            (
                'adhoc={}'.format(str(v).lower())
                if (v := options.get('adhoc', False)) else None
            ),
            (
                'flexIndex={}'.format(str(v).lower())
                if (v := options.get('flex_index', False)) else None
            ),
            (
                'scanConsistency={}'.format(v.value[0] + v.value.title().replace('_', '')[1:])
                if (v := options.get('scan_consistency', False)) else None
            ),
            (
                'scanWait={}'.format(str(v))
                if (v := options.get('scan_wait', False)) else None
            ),
            (
                'maxParallelism={}'.format(v)
                if (v := options.get('max_parallelism', False)) else None
            ),
            (
                'pipelineBatch={}'.format(v)
                if (v := options.get('pipeline_batch', False)) else None
            ),
            (
                'pipelineCap={}'.format(v)
                if (v := options.get('pipeline_cap', False)) else None
            ),
            (
                'scanCap={}'.format(v)
                if (v := options.get('scan_cap', False)) else None
            ),
        ]

        api += '?' + '&'.join([p for p in params if p is not None])

        headers = {'Content-Type': 'application/json'}
        body = {
            'query': n1ql_query,
            'parameters': options.get('positional_parameters', options.get('named_parameters', {}))
        }
        resp = self.session.post(url=api, auth=self.auth, headers=headers, json=body)
        resp.raise_for_status()
        return resp
