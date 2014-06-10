import sys
from time import sleep

from celery import Celery
from fabric import state
from fabric.api import cd, run, local, settings, quiet
from kombu import Queue
from logger import logger
from spring.wgen import WorkloadGen

from perfrunner.helpers.misc import uhex
from perfrunner.settings import BROKER_URL, LOCAL_BROKER_URL, REPO

if {'--local', '-C'} & set(sys.argv):
    # -C is a hack to distinguish local and remote workers!
    broker = LOCAL_BROKER_URL
else:
    broker = BROKER_URL
celery = Celery('workers', backend='amqp', broker=broker)


@celery.task
def task_run_workload(settings, target, timer):
    wg = WorkloadGen(settings, target, timer=timer)
    wg.run()


class WorkerManager(object):

    def __new__(cls, *args, **kwargs):
        if '--local' in sys.argv:
            return LocalWorkerManager(*args, **kwargs)
        else:
            return RemoteWorkerManager(*args, **kwargs)


class RemoteWorkerManager(object):

    DELAY_BEFORE_START = 2

    def __init__(self, cluster_spec, test_config):
        self.cluster_spec = cluster_spec
        self.buckets = test_config.buckets or test_config.max_buckets

        self.temp_dir = '/tmp/{}'.format(uhex()[:12])
        self.user, self.password = cluster_spec.client_credentials
        with settings(user=self.user, password=self.password):
            self.initialize_project()
            self.start()

    def initialize_project(self):
        for worker, master in zip(self.cluster_spec.workers,
                                  self.cluster_spec.yield_masters()):
            state.env.host_string = worker
            run('killall -9 celery', quiet=True)
            for bucket in self.buckets:
                logger.info('Intializing remote worker environment')

                qname = '{}-{}'.format(master.split(':')[0], bucket)
                temp_dir = '{}-{}'.format(self.temp_dir, qname)
                run('mkdir {}'.format(temp_dir))
                with cd(temp_dir):
                    run('git clone {}'.format(REPO))
                with cd('{}/perfrunner'.format(temp_dir)):
                    run('virtualenv -p python2.7 env')
                    run('PATH=/usr/lib/ccache:/usr/lib64/ccache/bin:$PATH '
                        'env/bin/pip install '
                        '--download-cache /tmp/pip -r requirements.txt')

    def start(self):
        for worker, master in zip(self.cluster_spec.workers,
                                  self.cluster_spec.yield_masters()):
            state.env.host_string = worker
            for bucket in self.buckets:
                qname = '{}-{}'.format(master.split(':')[0], bucket)
                logger.info('Starting remote Celery worker: {}'.format(qname))

                temp_dir = '{}-{}/perfrunner'.format(self.temp_dir, qname)
                run('cd {0}; nohup env/bin/celery worker '
                    '-A perfrunner.helpers.worker -Q {1} -c 1 '
                    '&>/tmp/worker_{1}.log &'.format(temp_dir, qname),
                    pty=False)

    def run_workload(self, settings, target_iterator, timer=None):
        self.workers = []
        for target in target_iterator:
            logger.info('Running workload generator')

            qname = '{}-{}'.format(target.node.split(':')[0], target.bucket)
            queue = Queue(name=qname)
            worker = task_run_workload.apply_async(
                args=(settings, target, timer),
                queue=queue.name, expires=timer,
            )
            self.workers.append(worker)
            sleep(self.DELAY_BEFORE_START)

    def wait_for_workers(self):
        for worker in self.workers:
            worker.wait()

    def terminate(self):
        for worker, master in zip(self.cluster_spec.workers,
                                  self.cluster_spec.yield_masters()):
            state.env.host_string = worker
            for bucket in self.buckets:
                with settings(user=self.user, password=self.password):
                    logger.info('Terminating remote Celery worker')
                    run('killall -9 celery', quiet=True)

                    logger.info('Cleaning up remote worker environment')
                    qname = '{}-{}'.format(master.split(':')[0], bucket)
                    temp_dir = '{}-{}'.format(self.temp_dir, qname)
                    run('rm -fr {}'.format(temp_dir))


class LocalWorkerManager(RemoteWorkerManager):

    def __init__(self, cluster_spec, test_config):
        self.cluster_spec = cluster_spec
        self.buckets = test_config.buckets or test_config.max_buckets

        self.initialize_project()
        self.start()

    def initialize_project(self):
        with quiet():
            local('virtualenv -p python2.7 env')
            local('PATH=/usr/lib/ccache:/usr/lib64/ccache/bin:$PATH '
                  'env/bin/pip install '
                  '--download-cache /tmp/pip -r requirements.txt')

    def start(self):
        logger.info('Terminating local Celery workers')
        with quiet():
            local('killall -9 celery')

        for master in self.cluster_spec.yield_masters():
            for bucket in self.buckets:
                qname = '{}-{}'.format(master.split(':')[0], bucket)
                logger.info('Starting local Celery worker: {}'.format(qname))
                with quiet():
                    local('nohup env/bin/celery worker '
                          '-A perfrunner.helpers.worker -Q {0} -c 1 -C '
                          '&>/tmp/worker_{0}.log &'.format(qname))
                sleep(self.DELAY_BEFORE_START)

    def terminate(self):
        logger.info('Terminating local Celery workers')
        with quiet():
            local('killall -9 celery')
            local('rm -fr /tmp/perfrunner.db')
