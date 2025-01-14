import fileinput
import json
import shutil
import subprocess
import time
from dataclasses import dataclass
from hashlib import md5
from typing import Any, Union
from uuid import uuid4

import yaml

from logger import logger


@dataclass
class SGPortRange:
    min_port: int
    max_port: int = None
    protocol: str = 'tcp'

    def __init__(self, min_port: int, max_port: int = None, protocol: str = 'tcp'):
        self.min_port = min_port
        self.max_port = max_port if max_port else min_port
        self.protocol = protocol

    def port_range_str(self) -> str:
        return '{}{}'.format(
            self.min_port,
            '-{}'.format(self.max_port) if self.max_port != self.min_port else ''
        )

    def __str__(self) -> str:
        return '(ports={}, protocol={})'.format(
            self.port_range_str(),
            self.protocol
        )


def uhex() -> str:
    return uuid4().hex


def pretty_dict(d: Any) -> str:
    return json.dumps(d, indent=4, sort_keys=True,
                      default=lambda o: o.__dict__)


def target_hash(*args: str) -> str:
    int_hash = hash(args)
    str_hash = md5(hex(int_hash).encode('utf-8')).hexdigest()
    return str_hash[:6]


def retry(catch: tuple = (), iterations: int = 5, wait: int = 10):
    """Retry a function while discarding the specified exceptions.

    'catch' is a tuple of exceptions. Passing in a list is also fine.

    'iterations' means number of total attempted calls. 'iterations' is only
    meaningful when >= 2.

    'wait' is wait time between calls.

    Usage:

    import perfrunner.helpers.misc

    @perfrunner.helpers.misc.retry(catch=[RuntimeError, KeyError])
    def hi():
        raise KeyError("Key Errrrr from Hi")

    # or if you want to tune your own iterations and wait

    @perfrunner.helpers.misc.retry(
        catch=[KeyError, TypeError],
        iterations=3, wait=1)
    def hi(who):
        print "hi called"
        return "hi " +  who

    print hi("john")
    # this throws TypeError when 'str' and 'None are concatenated
    print hi(None)
    """
    # in case the user specifies a list of Exceptions instead of a tuple
    catch = tuple(catch)

    def retry_decorator(func):
        def retry_wrapper(*arg, **kwargs):
            for i in range(iterations):
                try:
                    result = func(*arg, **kwargs)
                except catch:
                    if i == (iterations - 1):
                        raise
                    else:
                        pass
                else:
                    return result
                time.sleep(wait)
        return retry_wrapper
    return retry_decorator


def read_json(filename: str) -> dict:
    with open(filename) as fh:
        return json.load(fh)


def maybe_atoi(a: str, t=int) -> Union[int, float, str, bool]:
    if a.lower() == 'false':
        return False
    elif a.lower() == 'true':
        return True
    else:
        try:
            return t(a)
        except ValueError:
            return a


def human_format(number: float) -> str:
    magnitude = 0
    while abs(number) >= 1e3:
        magnitude += 1
        number /= 1e3
    return '{:.0f}{}'.format(number, ['', 'K', 'M', 'G', 'T', 'P'][magnitude])


def copy_template(source, dest):
    shutil.copyfile(source, dest)


def inject_config_tags(config_path,
                       operator_tag,
                       admission_controller_tag):
    #  operator
    with fileinput.FileInput(config_path, inplace=True, backup='.bak') as file:
        search = 'couchbase/operator:build'
        replace = operator_tag
        for line in file:
            print(line.replace(search, replace), end='')

    #  admission controller
    with fileinput.FileInput(config_path, inplace=True, backup='.bak') as file:
        search = 'couchbase/admission-controller:build'
        replace = admission_controller_tag
        for line in file:
            print(line.replace(search, replace), end='')


def inject_cluster_tags(cluster_path,
                        couchbase_tag,
                        operator_backup_tag,
                        exporter_tag,
                        refresh_rate):
    #  couchbase
    with fileinput.FileInput(cluster_path, inplace=True, backup='.bak') as file:
        search = 'couchbase/server:build'
        replace = couchbase_tag
        for line in file:
            print(line.replace(search, replace), end='')

    #  operator backup
    with fileinput.FileInput(cluster_path, inplace=True, backup='.bak') as file:
        search = 'couchbase/operator-backup:build'
        replace = operator_backup_tag
        for line in file:
            print(line.replace(search, replace), end='')

    #  exporter
    with fileinput.FileInput(cluster_path, inplace=True, backup='.bak') as file:
        search = 'couchbase/exporter:build'
        replace = exporter_tag
        for line in file:
            print(line.replace(search, replace), end='')

    # Refresh rate
    with fileinput.FileInput(cluster_path, inplace=True, backup='.bak') as file:
        search = 'XX'
        replace = refresh_rate
        for line in file:
            print(line.replace(search, replace), end='')


def inject_server_count(cluster_path, server_count):
    with fileinput.FileInput(cluster_path, inplace=True, backup='.bak') as file:
        search = 'size: node_count'
        replace = 'size: {}'.format(server_count)
        for line in file:
            print(line.replace(search, replace), end='')


def inject_workers_spec(num_workers, mem_limit, cpu_limit, worker_template_path, worker_path):
    with open(worker_template_path) as file:
        worker_config = yaml.safe_load(file)

    worker_config['spec']['replicas'] = num_workers
    limits = worker_config['spec']['template']['spec']['containers'][0]['resources']['limits']
    limits['cpu'] = cpu_limit
    limits['memory'] = mem_limit

    with open(worker_path, "w") as file:
        yaml.dump(worker_config, file)


def is_null(element) -> bool:
    if (isinstance(element, int) or isinstance(element, float)) and element == 0:
        return False
    elif isinstance(element, bool):
        return False
    else:
        return False if element else True


def remove_nulls(d: dict) -> dict:
    if not isinstance(d, dict):
        return d
    return {k: new_v for k, v in d.items() if not is_null(new_v := remove_nulls(v))}


def run_local_shell_command(command: str, success_msg: str = '',
                            err_msg: str = '') -> tuple[str, str, int]:
    process = subprocess.run(command, shell=True, capture_output=True)
    if (returncode := process.returncode) == 0:
        logger.info(success_msg)
    else:
        if err_msg:
            logger.error(err_msg)
        logger.error('Command failed with return code {}: {}'
                     .format(returncode, process.args))
        logger.error('Command stdout: {}'.format(process.stdout.decode()))
        logger.error('Command stderr: {}'.format(process.stderr.decode()))

    return process.stdout.decode(), process.stderr.decode(), returncode


def set_azure_subscription(sub_name: str, alias: str) -> int:
    _, _, err = run_local_shell_command(
        command='az account set --subscription "{}"'.format(sub_name),
        success_msg='Set active Azure subscription to "{}" ({})'.format(sub_name, alias),
        err_msg='Failed to set active Azure subscription to "{}" ({})'.format(sub_name, alias)
    )
    return err


def set_azure_perf_subscription() -> int:
    return set_azure_subscription('130 - QE', 'perf')


def set_azure_capella_subscription(capella_env: str) -> int:
    if 'sandbox' in capella_env:
        sub = 'couchbasetest1-rcm'
    else:
        sub = 'capellanonprod-rcm'

    return set_azure_subscription(sub, 'capella')
