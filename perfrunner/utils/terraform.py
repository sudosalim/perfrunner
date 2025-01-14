#!/usr/bin/env python
import json
import os
from argparse import ArgumentParser
from collections import Counter
from time import sleep, time
from typing import Dict
from uuid import uuid4

import requests
from capella.dedicated.CapellaAPI import CapellaAPI as CapellaAPIDedicated
from capella.serverless.CapellaAPI import CapellaAPI as CapellaAPIServerless
from fabric.api import local
from requests.exceptions import HTTPError

from logger import logger
from perfrunner.helpers.misc import maybe_atoi, pretty_dict, remove_nulls
from perfrunner.settings import ClusterSpec, TestConfig

CAPELLA_CREDS_FILE = '.capella_creds'
DATADOG_LINK_TEMPLATE = (
    'https://app.datadoghq.com/logs?'
    'query=status%3Aerror%20%40{}&'
    'cols=host%2Cservice&index=%2A&messageDisplay=inline&stream_sort=time%2Cdesc&viz=stream&'
    'from_ts={}&'
    'to_ts={}&'
    'live=true'
)
SERVICES_CAPELLA_TO_PERFRUNNER = {
    'Data': 'kv',
    'Analytics': 'cbas',
    'Query': 'n1ql',
    'Index': 'index',
    'Search': 'fts',
    'Eventing': 'eventing'
}

SERVICES_PERFRUNNER_TO_CAPELLA = {
    'kv': 'data',
    'cbas': 'analytics',
    'n1ql': 'query',
    'index': 'index',
    'fts': 'search',
    'eventing': 'eventing'
}


def raise_for_status(resp: requests.Response):
    try:
        resp.raise_for_status()
    except HTTPError as e:
        logger.error('HTTP Error {}: response content: {}'.format(resp.status_code, resp.content))
        raise e


def format_datadog_link(cluster_id: str = None, dataplane_id: str = None,
                        database_id: str = None) -> str:
    filters = [
        'clusterId%3A{}'.format(cluster_id) if cluster_id else None,
        'dataplaneId%3A{}'.format(dataplane_id) if dataplane_id else None,
        'databaseId%3A{}'.format(database_id) if database_id else None
    ]

    filters = [f for f in filters if f]
    filter_string = '%20%40'.join(filters) if filters else ''

    now = time()
    one_hour_ago = now - (60 * 60)

    return DATADOG_LINK_TEMPLATE.format(filter_string, int(one_hour_ago * 1000), int(now * 1000))


class Terraform:

    # TODO: AWS capacity retry,
    #  Reset TFs function,
    #  Swap find and replace for pipe,
    #  Backup spec update,
    #  Add support for multiple clusters.

    IMAGE_MAP = {
        'aws': {
            'clusters': {
                'x86_64': 'perf-server-x86-ubuntu20-2023-07',  # ami-08d83f4b122efb564
                'arm': 'perf-server-arm-us-east',  # ami-0f249abfe3dd01b30
                'al2': 'perf-server-al_x86-2022-03-us-east',  # ami-060e286353d227c32
            },
            'clients': 'perf-client-x86-ubuntu20-2023-06-v3',  # ami-0d9789eef66732b62
            'utilities': 'perf-broker-us-east',  # ami-0d9e5ee360aa02d94
            'syncgateways': 'perf-server-x86-ubuntu20-2023-07',  # ami-08d83f4b122efb564
        },
        'gcp': {
            'clusters': 'perftest-server-x86-ubuntu20-2023-07',
            'clients': 'perftest-client-x86-ubuntu20-2023-06-v3',
            'utilities': 'perftest-broker-disk-image',
            'syncgateways': 'perftest-server-x86-ubuntu20-2023-07'
        },
        'azure': {
            'clusters': 'perf-server-x86-ubuntu20-image-def',
            'clients': 'perf-client-x86-ubuntu20-image-def',
            'utilities': 'perf-broker-image-def',
            'syncgateways': 'perf-server-x86-ubuntu20-image-def'
        }
    }

    def __init__(self, options):
        self.options = options
        self.infra_spec = ClusterSpec()
        self.infra_spec.parse(self.options.cluster, override=options.override)
        self.provider = self.infra_spec.cloud_provider
        self.backend = None
        self.uuid = uuid4().hex[0:6] if self.provider != 'aws' else None
        self.os_arch = self.infra_spec.infrastructure_settings.get('os_arch', 'x86_64')
        self.node_list = {
            'clusters': [
                n for nodes in self.infra_spec.infrastructure_clusters.values()
                for n in nodes.strip().split()
            ],
            'clients': self.infra_spec.clients,
            'utilities': self.infra_spec.utilities,
            'syncgateways': self.infra_spec.sgw_servers
        }

        self.cloud_storage = bool(
            int(self.infra_spec.infrastructure_settings.get('cloud_storage', 0))
        )

        if self.provider == 'gcp' or \
           (self.provider == 'capella' and self.infra_spec.capella_backend == 'gcp'):
            self.zone = self.options.zone
            self.region = self.zone.rsplit('-', 1)[0]
        else:
            self.zone = None
            if self.provider == 'aws' or \
               (self.provider == 'capella' and self.infra_spec.capella_backend == 'aws'):
                self.region = self.options.aws_region
            else:
                self.region = self.options.azure_region

    def deploy(self):
        # Configure terraform
        self.populate_tfvars()
        self.terraform_init(self.provider)

        # Deploy resources
        self.terraform_apply(self.provider)

        # Get info about deployed resources and update cluster spec file
        output = self.terraform_output(self.provider)
        self.update_spec(output)

    def destroy(self):
        self.terraform_destroy(self.provider)

    def create_tfvar_nodes(self) -> Dict[str, Dict]:
        tfvar_nodes = {
            'clusters': {},
            'clients': {},
            'utilities': {},
            'syncgateways': {}
        }

        cloud_provider = self.backend if self.provider == 'capella' else self.provider

        for role, nodes in self.node_list.items():
            # If this is a capella test, skip cluster nodes
            if self.provider == 'capella' and (role == 'clusters' or role == 'syncgateways'):
                continue

            i = 0
            for node in nodes:
                node_cluster, node_group = node.split(':')[0].split('.', 2)[1:]
                parameters = self.infra_spec.infrastructure_config()[node_group.split('.')[0]]

                parameters['node_group'] = node_group

                # Try getting image name from cli option
                image = getattr(self.options, '{}_image'.format({
                    'clusters': 'cluster',
                    'clients': 'client',
                    'utilities': 'utility',
                    'syncgateways': 'sgw'
                }[role]))

                # If image name isn't provided as cli param, use hardcoded defaults
                if image is None:
                    image = self.IMAGE_MAP[cloud_provider][role]
                    if cloud_provider == 'aws' and role == 'clusters':
                        image = image.get(self.os_arch, image['x86_64'])

                parameters['image'] = image

                # Set disk size and type
                parameters['volume_size'] = int(parameters.get('volume_size', 0))

                storage_class = parameters.get('storage_class', parameters.get('volume_type', None))
                if not storage_class:
                    node_cluster_config = self.infra_spec.infrastructure_section(node_cluster)
                    storage_class = node_cluster_config.get('storage_class')
                parameters['storage_class'] = storage_class

                # Set CSP-specific options
                if cloud_provider == 'azure':
                    parameters['disk_tier'] = parameters.get('disk_tier', '')
                else:
                    # AWS and GCP
                    parameters['iops'] = int(parameters.get('iops', 0))
                    if cloud_provider == 'aws':
                        parameters['volume_throughput'] = int(parameters.get('volume_throughput',
                                                                             0))
                    else:
                        # GCP
                        parameters['local_nvmes'] = int(parameters.get('local_nvmes', 0))

                del parameters['instance_capacity']

                tfvar_nodes[role][str(i := i+1)] = parameters

        return tfvar_nodes

    def populate_tfvars(self):
        logger.info('Setting tfvars')
        cloud_provider = self.backend if self.provider == 'capella' else self.provider
        global_tag = self.options.tag if self.options.tag else ''
        if cloud_provider.lower() == 'gcp':
            # GCP doesn't allow uppercase letters in tags
            global_tag = global_tag.lower()

        tfvar_nodes = self.create_tfvar_nodes()

        replacements = {
            '<CLOUD_REGION>': self.region,
            '<CLUSTER_NODES>': tfvar_nodes['clusters'],
            '<CLIENT_NODES>': tfvar_nodes['clients'],
            '<UTILITY_NODES>': tfvar_nodes['utilities'],
            '<SYNCGATEWAY_NODES>': tfvar_nodes['syncgateways'],
            '<CLOUD_STORAGE>': self.cloud_storage,
            '<GLOBAL_TAG>': global_tag
        }

        if self.uuid:
            replacements['<UUID>'] = self.uuid

        if self.zone:
            replacements['<CLOUD_ZONE>'] = self.zone

        with open('terraform/{}/terraform.tfvars'.format(cloud_provider), 'r+') as tfvars:
            file_string = tfvars.read()

            for k, v in replacements.items():
                file_string = file_string.replace(k, json.dumps(v, indent=4))

            tfvars.seek(0)
            tfvars.write(file_string)

    # Initializes terraform environment.
    def terraform_init(self, provider):
        logger.info('Initializating Terraform (terraform init)')
        local('cd terraform/{} && terraform init >> terraform.log'.format(provider))

    # Apply and output terraform deployment.
    def terraform_apply(self, provider):
        logger.info('Building and executing Terraform deployment plan '
                    '(terraform plan, terraform apply)')
        local('cd terraform/{} && '
              'terraform plan -out tfplan.out >> terraform.log && '
              'terraform apply -auto-approve tfplan.out'
              .format(provider))

    def terraform_output(self, provider):
        output = json.loads(
            local('cd terraform/{} && terraform output -json'.format(provider), capture=True)
        )
        return output

    def terraform_destroy(self, provider):
        logger.info('Building and executing Terraform destruction plan '
                    '(terraform plan -destroy, terraform apply)')
        local('cd terraform/{} && '
              'terraform plan -destroy -out tfplan_destroy.out >> terraform.log && '
              'terraform apply -auto-approve tfplan_destroy.out'
              .format(provider))

    # Update spec file with deployed infrastructure.
    def update_spec(self, output):
        sections = ['clusters', 'clients', 'utilities', 'syncgateways']
        cluster_dicts = [
            self.infra_spec.infrastructure_clusters,
            self.infra_spec.infrastructure_clients,
            self.infra_spec.infrastructure_utilities,
            self.infra_spec.infrastructure_syncgateways
        ]
        output_keys = [
            'cluster_instance_ips',
            'client_instance_ips',
            'utility_instance_ips',
            'syncgateway_instance_ips'
        ]
        private_sections = [
            'cluster_private_ips',
            'client_private_ips',
            'utility_private_ips',
            'syncgateway_private_ips'
        ]

        for section, cluster_dict, output_key, private_section in zip(sections,
                                                                      cluster_dicts,
                                                                      output_keys,
                                                                      private_sections):
            if (section not in self.infra_spec.config.sections()) or \
               (self.provider == 'capella' and (section == 'clusters' or
                                                section == 'syncgateways')):
                continue

            for cluster, nodes in cluster_dict.items():
                node_list = nodes.strip().split()
                public_ips = [None for _ in node_list]
                private_ips = [None for _ in node_list]

                for _, info in output[output_key]['value'].items():
                    node_group = info['node_group']
                    for i, node in enumerate(node_list):
                        hostname, *extras = node.split(':', maxsplit=1)
                        if hostname.split('.', 2)[-1] == node_group:
                            public_ip = info['public_ip']
                            if extras:
                                public_ip += ':{}'.format(*extras)
                            public_ips[i] = public_ip
                            if 'private_ip' in info:
                                private_ips[i] = info['private_ip']
                            break

                self.infra_spec.config.set(section, cluster, '\n' + '\n'.join(public_ips))
                if any(private_ips):
                    if private_section not in self.infra_spec.config.sections():
                        self.infra_spec.config.add_section(private_section)
                    self.infra_spec.config.set(private_section, cluster,
                                               '\n' + '\n'.join(private_ips))

        if self.cloud_storage:
            bucket_url = output['cloud_storage']['value']['storage_bucket']
            self.infra_spec.config.set('storage', 'backup', bucket_url)
            if self.provider == 'azure':
                storage_acc = output['cloud_storage']['value']['storage_account']
                self.infra_spec.config.set('storage', 'storage_acc', storage_acc)

        self.infra_spec.update_spec_file()

        with open('cloud/infrastructure/cloud.ini', 'r+') as f:
            s = f.read()
            if self.provider != 'capella':
                s = s.replace("server_list", "\n".join(self.infra_spec.servers))
            s = s.replace("worker_list", "\n".join(self.infra_spec.clients))
            if self.infra_spec.sgw_servers:
                s = s.replace("sgw_list", "\n".join(self.infra_spec.sgw_servers))
            f.seek(0)
            f.write(s)


class CapellaTerraform(Terraform):

    def __init__(self, options):
        super().__init__(options)

        self.test_config = None
        if options.test_config:
            test_config = TestConfig()
            test_config.parse(options.test_config, override=options.override)
            self.test_config = test_config

        if self.test_config and self.test_config.rebalance_settings.nodes_after:
            self.node_list['clusters'] = [
                node
                for nodes, initial_num in zip(self.infra_spec.infrastructure_clusters.values(),
                                              self.test_config.cluster.initial_nodes)
                for node in nodes.strip().split()[:initial_num]
            ]

        self.backend = self.infra_spec.infrastructure_settings['backend']

        if public_api_url := self.options.capella_public_api_url:
            env = public_api_url.removeprefix('https://')\
                                .removesuffix('.nonprod-project-avengers.com')\
                                .split('.', 1)[1]
            self.infra_spec.config.set('infrastructure', 'cbc_env', env)
            self.infra_spec.update_spec_file()

        self.api_client = CapellaAPIDedicated(
            'https://cloudapi.{}.nonprod-project-avengers.com'.format(
                self.infra_spec.infrastructure_settings['cbc_env']
            ),
            os.getenv('CBC_SECRET_KEY'),
            os.getenv('CBC_ACCESS_KEY'),
            os.getenv('CBC_USER'),
            os.getenv('CBC_PWD'),
            os.getenv('CBC_TOKEN_FOR_INTERNAL_SUPPORT')
        )

        if (tenant := self.options.capella_tenant) is None:
            tenant = self.infra_spec.infrastructure_settings.get('cbc_tenant', None)
        else:
            self.infra_spec.config.set('infrastructure', 'cbc_tenant', tenant)
            self.infra_spec.update_spec_file()

        self.tenant_id = tenant or self.get_tenant_id()

        if not self.api_client.ACCESS or not self.api_client.SECRET:
            if os.path.isfile(CAPELLA_CREDS_FILE):
                with open(CAPELLA_CREDS_FILE, 'r') as f:
                    creds = json.load(f)
                    self.api_client.ACCESS = creds.get('access')
                    self.api_client.SECRET = creds.get('secret')
            else:
                self.api_client.ACCESS, self.api_client.SECRET = self.generate_api_key()

        if (project := self.options.capella_project) is None:
            project = self.infra_spec.infrastructure_settings.get('cbc_project', None)
        else:
            self.infra_spec.config.set('infrastructure', 'cbc_project', project)
            self.infra_spec.update_spec_file()

        self.project_id = project

        self.capella_timeout = max(0, self.options.capella_timeout)

        self.cluster_ids = self.infra_spec.infrastructure_settings.get('cbc_cluster', '').split()

    def generate_api_key(self):
        logger.info('Generating API key...')
        key_name = self.options.tag or 'perf-key-{}'.format(self.uuid)
        resp = self.api_client.create_access_secret_key(key_name, self.tenant_id)
        raise_for_status(resp)
        creds = {
            'id': resp.json()['id'],
            'access': resp.json()['access'],
            'secret': resp.json()['secret']
        }
        with open(CAPELLA_CREDS_FILE, 'w') as f:
            json.dump(creds, f)
        return creds['access'], creds['secret']

    def get_tenant_id(self):
        logger.info('Finding a tenant...')
        resp = self.api_client.list_accessible_tenants()
        raise_for_status(resp)
        tenant = resp.json()[0]['id']
        self.infra_spec.config.set('infrastructure', 'cbc_tenant', tenant)
        self.infra_spec.update_spec_file()
        logger.info('Found tenant ID: {}'.format(tenant))
        return tenant

    def create_project(self):
        logger.info('Creating project...')
        resp = self.api_client.create_project(
            self.tenant_id,
            self.options.tag or 'perf-provisioned-{}'.format(self.uuid)
        )
        raise_for_status(resp)
        project = resp.json()['id']
        self.infra_spec.config.set('infrastructure', 'cbc_project', project)
        self.infra_spec.config.set('infrastructure', 'cbc_project_created', '1')
        self.infra_spec.update_spec_file()
        self.project_id = project
        logger.info('Project created: {}'.format(project))

    def deploy(self):
        if not self.project_id:
            self.create_project()

        if not self.options.capella_only:
            # Configure terraform
            self.populate_tfvars()
            self.terraform_init(self.backend)

            # Deploy non-capella resources
            self.terraform_apply(self.backend)
            non_capella_output = self.terraform_output(self.backend)
            Terraform.update_spec(self, non_capella_output)

        # Deploy capella cluster
        self.deploy_cluster()

        if self.options.disable_autoscaling:
            self.disable_autoscaling()

        # Update cluster spec file
        self.update_spec()

        if not self.options.capella_only and self.options.vpc_peering:
            # Do VPC peering
            network_info = non_capella_output['network']['value']
            self.peer_vpc(network_info, self.cluster_ids[0])

    def destroy(self):
        # Tear down VPC peering connection
        self.destroy_peering_connection()

        # Destroy non-capella resources
        self.terraform_destroy(self.backend)

        # Destroy capella cluster
        if not self.options.keep_cluster:
            self.destroy_cluster()

            if int(self.infra_spec.infrastructure_settings.get('cbc_project_created', 0)):
                cluster_destroyed = self.wait_for_cluster_destroy()
                if cluster_destroyed:
                    self.destroy_project()

        if os.path.isfile(CAPELLA_CREDS_FILE):
            logger.info('Revoking API key...')
            with open(CAPELLA_CREDS_FILE, 'r') as f:
                creds = json.load(f)
                self.api_client.revoke_access_secret_key(self.tenant_id, creds['id'])
            os.remove(CAPELLA_CREDS_FILE)

    def wait_for_cluster_destroy(self) -> bool:
        logger.info('Waiting for clusters to be destroyed...')

        pending_clusters = [cluster_id for cluster_id in self.cluster_ids]
        timeout_mins = self.capella_timeout
        interval_secs = 30
        t0 = time()
        while pending_clusters and (time() - t0) < timeout_mins * 60:
            sleep(interval_secs)

            resp = self.api_client.get_clusters({'projectId': self.project_id, 'perPage': 100})
            raise_for_status(resp)

            pending_clusters = []
            for cluster in resp.json()['data'].get('items', []):
                if (cluster_id := cluster['id']) in self.cluster_ids:
                    pending_clusters.append(cluster_id)
                    logger.info('Cluster {} not destroyed yet'.format(cluster_id))

        if pending_clusters:
            logger.error('Timed out after {} mins waiting for clusters to delete.'
                         .format(timeout_mins))
            for cluster_id in pending_clusters:
                logger.error('DataDog link for debugging (cluster {}): {}'
                             .format(cluster_id, format_datadog_link(cluster_id=cluster_id)))
            return False
        else:
            logger.info('All clusters destroyed.')
            return True

    def destroy_project(self):
        logger.info('Deleting project...')
        resp = self.api_client.delete_project(self.tenant_id, self.project_id)
        raise_for_status(resp)
        logger.info('Project successfully queued for deletion.')

    @staticmethod
    def capella_server_group_sizes(node_schemas):
        server_groups = {}
        for node in node_schemas:
            name, services = node.split(':')[:2]

            _, cluster, node_group, _ = name.split('.')
            services_set = tuple(set(services.split(',')))

            node_tuple = (node_group, services_set)

            if cluster not in server_groups:
                server_groups[cluster] = [node_tuple]
            else:
                server_groups[cluster].append(node_tuple)

        server_group_sizes = {
            cluster: Counter(node_tuples) for cluster, node_tuples in server_groups.items()
        }

        return server_group_sizes

    @staticmethod
    def construct_capella_server_groups(infra_spec,
                                        node_schemas,
                                        enable_disk_autoscaling: bool = True):
        """Create correct server group objects for deploying clusters using internal API.

        Sample server group template:
        ```
        {
            "count": 3,
            "services": [
                {"type": "kv"},
                {"type": "index"}
            ],
            "compute": {
                "type": "r5.2xlarge",
                "cpu": 0,
                "memoryInGb": 0
            },
            "disk": {
                "type": "io2",
                "sizeInGb": 50,
                "iops": 3000
            }
        }
        ```
        """
        server_groups = CapellaTerraform.capella_server_group_sizes(node_schemas)

        cluster_list = []
        for cluster, server_groups in server_groups.items():
            server_list = []
            cluster_params = infra_spec.infrastructure_section(cluster)

            for (node_group, services), size in server_groups.items():
                node_group_config = infra_spec.infrastructure_section(node_group)

                storage_class = node_group_config.get(
                    'volume_type', node_group_config.get(
                        'storage_class', cluster_params.get('storage_class')
                    )
                ).lower()

                if infra_spec.capella_backend == 'azure' and 'disk_tier' in node_group_config:
                    disk_tier = node_group_config['disk_tier']
                else:
                    disk_tier = ""

                server_group = {
                    'count': size,
                    'services': [{'type': svc} for svc in services],
                    'compute': {
                        'type': node_group_config['instance_type'],
                        'cpu': 0,
                        'memoryInGb': 0
                    },
                    'disk': {
                        'type': storage_class
                        if infra_spec.capella_backend != 'azure'
                        else disk_tier,
                        'sizeInGb': int(node_group_config['volume_size']),
                    },
                    'diskAutoScaling': {
                        "enabled": enable_disk_autoscaling
                    }
                }

                if infra_spec.capella_backend == 'aws':
                    server_group['disk']['iops'] = int(node_group_config.get('iops', 3000))

                server_list.append(server_group)

            cluster_list.append(server_list)

        return cluster_list

    def deploy_cluster(self):
        specs = self.construct_capella_server_groups(
            self.infra_spec,
            self.node_list['clusters'],
            self.options.enable_disk_autoscaling)
        names = ['perf-cluster-{}'.format(self.uuid) for _ in specs]
        if len(names) > 1:
            names = ['{}-{}'.format(name, i) for i, name in enumerate(names)]

        for name, spec in zip(names, specs):
            config = {
                "cidr": self.get_available_cidr(),
                "name": name,
                "description": "",
                "projectId": self.project_id,
                "provider": {
                    'aws': 'hostedAWS',
                    'gcp': 'hostedGCP',
                    'azure': 'hostedAzure'
                }[self.infra_spec.capella_backend.lower()],
                "region": self.region,
                "singleAZ": not self.options.multi_az,
                "server": self.options.capella_cb_version,
                "specs": spec,
                "package": "enterprise"
            }

            logger.info(pretty_dict(config))

            if (server := self.options.capella_cb_version) and (ami := self.options.capella_ami):
                config['overRide'] = {
                    'token': os.getenv('CBC_OVERRIDE_TOKEN'),
                    'server': server,
                    'image': ami
                }
                config.pop('server')
                logger.info('Deploying cluster with custom AMI: {}'
                            .format(ami))
            if release_id := self.options.release_id:
                config['overRide'].update({'releaseId': release_id})

            resp = self.api_client.create_cluster_customAMI(self.tenant_id, config)
            raise_for_status(resp)
            cluster_id = resp.json().get('id')
            self.cluster_ids.append(cluster_id)

            logger.info('Initialised cluster deployment for cluster {} \n  config: {}'.
                        format(cluster_id, config))
            logger.info('Saving cluster ID to spec file.')
            self.infra_spec.config.set('infrastructure', 'cbc_cluster',
                                       '\n'.join(self.cluster_ids))
            self.infra_spec.update_spec_file()

        timeout_mins = self.capella_timeout
        interval_secs = 30
        pending_clusters = [cluster_id for cluster_id in self.cluster_ids]
        t0 = time()
        while pending_clusters and (time() - t0) < timeout_mins * 60:
            sleep(interval_secs)

            statuses = []
            for cluster_id in pending_clusters:
                status = self.api_client.get_cluster_status(cluster_id).json().get('status')
                logger.info('Cluster state for {}: {}'.format(cluster_id, status))
                if status == 'deploymentFailed':
                    logger.error('Deployment failed for cluster {}. DataDog link for debugging: {}'
                                 .format(cluster_id, format_datadog_link(cluster_id=cluster_id)))
                    exit(1)
                statuses.append(status)

            pending_clusters = [
                pending_clusters[i] for i, status in enumerate(statuses) if status != 'healthy'
            ]

        if pending_clusters:
            logger.error('Deployment timed out after {} mins'.format(timeout_mins))
            for cluster_id in pending_clusters:
                logger.error('DataDog link for debugging (cluster {}): {}'
                             .format(cluster_id, format_datadog_link(cluster_id=cluster_id)))
            exit(1)

    def destroy_cluster(self):
        for cluster_id in self.cluster_ids:
            logger.info('Deleting Capella cluster {}...'.format(cluster_id))
            resp = self.api_client.delete_cluster(cluster_id)
            raise_for_status(resp)
            logger.info('Capella cluster successfully queued for deletion.')

    def get_available_cidr(self):
        resp = self.api_client.get_deployment_options(self.tenant_id,
                                                      self.infra_spec.capella_backend.lower())
        return resp.json().get('suggestedCidr')

    def get_deployed_cidr(self, cluster_id):
        resp = self.api_client.get_cluster_info(cluster_id)
        return resp.json().get('place', {}).get('CIDR')

    def get_hostnames(self, cluster_id):
        resp = self.api_client.get_nodes(tenant_id=self.tenant_id,
                                         project_id=self.project_id,
                                         cluster_id=cluster_id)
        nodes = resp.json()['data']
        nodes = [node['data'] for node in nodes]
        services_per_node = {node['hostname']: node['services'] for node in nodes}

        kv_nodes = []
        non_kv_nodes = []
        for hostname, services in services_per_node.items():
            services_string = ','.join(SERVICES_CAPELLA_TO_PERFRUNNER[svc] for svc in services)
            if 'kv' in services_string:
                kv_nodes.append("{}:{}".format(hostname, services_string))
            else:
                non_kv_nodes.append("{}:{}".format(hostname, services_string))

        ret_list = kv_nodes + non_kv_nodes
        return ret_list

    def update_spec(self):
        self.infra_spec.config.add_section('clusters_schemas')
        for option, value in self.infra_spec.infrastructure_clusters.items():
            self.infra_spec.config.set('clusters_schemas', option, value)

        for i, cluster_id in enumerate(self.cluster_ids):
            hostnames = self.get_hostnames(cluster_id)
            cluster = self.infra_spec.config.options('clusters')[i]
            self.infra_spec.config.set('clusters', cluster, '\n' + '\n'.join(hostnames))

        self.infra_spec.update_spec_file()

    def disable_autoscaling(self):
        if self.api_client.TOKEN_FOR_INTERNAL_SUPPORT is None:
            logger.error('Cannot create circuit breaker to prevent auto-scaling. No value found '
                         'for CBC_TOKEN_FOR_INTERNAL_SUPPORT, so cannot authenticate with CP API.')
            return

        for cluster_id in self.cluster_ids:
            logger.info(
                'Creating deployment circuit breaker to prevent auto-scaling for cluster {}.'
                .format(cluster_id)
            )

            resp = self.api_client.create_circuit_breaker(cluster_id)
            raise_for_status(resp)

            resp = self.api_client.get_circuit_breaker(cluster_id)
            raise_for_status(resp)

            logger.info('Circuit breaker created: {}'.format(pretty_dict(resp.json())))

    def peer_vpc(self, network_info, cluster_id):
        logger.info('Setting up VPC peering...')
        if self.infra_spec.capella_backend == 'aws':
            peering_connection = self._peer_vpc_aws(network_info, cluster_id)
        elif self.infra_spec.capella_backend == 'gcp':
            peering_connection, dns_managed_zone, client_vpc = self._peer_vpc_gcp(network_info,
                                                                                  cluster_id)
            self.infra_spec.config.set('infrastructure', 'dns_managed_zone', dns_managed_zone)
            self.infra_spec.config.set('infrastructure', 'client_vpc', client_vpc)

        if peering_connection:
            self.infra_spec.config.set('infrastructure', 'peering_connection', peering_connection)
            self.infra_spec.update_spec_file()
        else:
            exit(1)

    def _peer_vpc_aws(self, network_info, cluster_id) -> str:
        # Initiate VPC peering
        logger.info('Initiating peering')

        client_vpc = network_info['vpc_id']
        cidr = network_info['subnet_cidr']
        route_table = network_info['route_table_id']
        cluster_cidr = self.get_deployed_cidr(cluster_id)

        logger.info('Adding Capella private network (AWS): VPC ID = {}'.format(client_vpc))

        account_id = local('AWS_PROFILE=default env/bin/aws sts get-caller-identity '
                           '--query Account --output text',
                           capture=True)

        data = {
            "name": "perftest-network",
            "aws": {
                "accountId": account_id,
                "vpcId": client_vpc,
                "region": self.region,
                "cidr": cidr
            },
            "provider": "aws"
        }

        peering_connection_id = None

        try:
            resp = self.api_client.create_private_network(
                self.tenant_id, self.project_id, cluster_id, data)
            private_network_id = resp.json()['id']

            # Get AWS CLI commands that we need to run to complete the peering process
            logger.info('Accepting peering request')
            resp = self.api_client.get_private_network(
                self.tenant_id, self.project_id, cluster_id, private_network_id)
            aws_commands = resp.json()['data']['commands']
            peering_connection_id = resp.json()['data']['aws']['providerId']

            # Finish peering process using AWS CLI
            for command in aws_commands:
                local("AWS_PROFILE=default env/bin/{}".format(command))

            # Finally, set up route table in our client VPC
            logger.info('Configuring route table in client VPC')
            local(
                (
                    "AWS_PROFILE=default env/bin/aws --region {} ec2 create-route "
                    "--route-table-id {} "
                    "--destination-cidr-block {} "
                    "--vpc-peering-connection-id {}"
                ).format(self.region, route_table, cluster_cidr, peering_connection_id)
            )
        except Exception as e:
            logger.error('Failed to complete VPC peering: {}'.format(e))

        return peering_connection_id

    def _peer_vpc_gcp(self, network_info, cluster_id) -> tuple[str, str]:
        # Initiate VPC peering
        logger.info('Initiating peering')

        client_vpc = network_info['vpc_id']
        cidr = network_info['subnet_cidr']
        service_account = local("gcloud config get account", capture=True)

        logger.info('Adding Capella private network (GCP): VPC name = {}'.format(client_vpc))

        project_id = local('gcloud config get project', capture=True)

        data = {
            "name": "perftest-network",
            "gcp": {
                "projectId": project_id,
                "networkName": client_vpc,
                "cidr": cidr,
                "serviceAccount": service_account
            },
            "provider": "gcp"
        }

        peering_connection_name = None
        dns_managed_zone_name = None

        try:
            resp = self.api_client.create_private_network(
                self.tenant_id, self.project_id, cluster_id, data)
            private_network_id = resp.json()['id']

            # Get gcloud commands that we need to run to complete the peering process
            logger.info('Accepting peering request')
            resp = self.api_client.get_private_network(
                self.tenant_id, self.project_id, cluster_id, private_network_id)
            gcloud_commands = resp.json()['data']['commands']

            # Finish peering process using gcloud
            for command in gcloud_commands:
                local(command)

            peering_connection_name, capella_vpc_uri = local(
                (
                    'gcloud compute networks peerings list '
                    '--network={} '
                    '--format="value(peerings[].name,peerings[].network)"'
                ).format(client_vpc),
                capture=True
            ).split()

            dns_managed_zone_name = local(
                (
                    'gcloud dns managed-zones list '
                    '--filter="(peeringConfig.targetNetwork.networkUrl = {})" '
                    '--format="value(name)"'
                ).format(capella_vpc_uri),
                capture=True
            )
        except Exception as e:
            logger.error('Failed to complete VPC peering: {}'.format(e))

        return peering_connection_name, dns_managed_zone_name, client_vpc

    def destroy_peering_connection(self):
        logger.info("Destroying peering connection...")
        if self.infra_spec.capella_backend == 'aws':
            self._destroy_peering_connection_aws()
        elif self.infra_spec.capella_backend == 'gcp':
            self._destroy_peering_connection_gcp()

    def _destroy_peering_connection_aws(self):
        peering_connection = self.infra_spec.infrastructure_settings.get('peering_connection', None)

        if not peering_connection:
            logger.warn('No peering connection ID found in cluster spec; nothing to destroy.')
            return

        local(
            (
                "AWS_PROFILE=default env/bin/aws "
                "--region {} ec2 delete-vpc-peering-connection "
                "--vpc-peering-connection-id {}"
            ).format(self.region, peering_connection)
        )

    def _destroy_peering_connection_gcp(self):
        peering_connection = self.infra_spec.infrastructure_settings.get('peering_connection', None)

        if not peering_connection:
            logger.warn('No peering connection ID found in cluster spec; nothing to destroy.')
            return

        dns_managed_zone = self.infra_spec.infrastructure_settings['dns_managed_zone']
        client_vpc = self.infra_spec.infrastructure_settings['client_vpc']

        local('gcloud compute networks peerings delete {} --network={}'
              .format(peering_connection, client_vpc))
        local('gcloud dns managed-zones delete {}'.format(dns_managed_zone))


class ServerlessTerraform(CapellaTerraform):

    NEBULA_OVERRIDE_ARGS = ['override_count', 'min_count', 'max_count', 'instance_type']

    def __init__(self, options):
        Terraform.__init__(self, options)
        if not options.test_config:
            logger.error('Test config required if deploying serverless infrastructure.')
            exit(1)

        test_config = TestConfig()
        test_config.parse(options.test_config, override=options.override)
        self.test_config = test_config

        for prefix, section in {'dapi': 'data_api', 'nebula': 'direct_nebula'}.items():
            for arg in self.NEBULA_OVERRIDE_ARGS:
                if (value := getattr(options, prefix + '_' + arg)) is not None:
                    self.infra_spec.config.set(section, arg, str(value))

        self.infra_spec.update_spec_file()

        self.backend = self.infra_spec.infrastructure_settings['backend']

        if public_api_url := self.options.capella_public_api_url:
            env = public_api_url.removeprefix('https://')\
                                .removesuffix('.nonprod-project-avengers.com')\
                                .split('.', 1)[1]
            self.infra_spec.config.set('infrastructure', 'cbc_env', env)
            self.infra_spec.update_spec_file()
        else:
            public_api_url = 'https://cloudapi.{}.nonprod-project-avengers.com'.format(
                self.infra_spec.infrastructure_settings['cbc_env']
            )

        self.serverless_client = CapellaAPIServerless(
            public_api_url,
            os.getenv('CBC_USER'),
            os.getenv('CBC_PWD'),
            os.getenv('CBC_TOKEN_FOR_INTERNAL_SUPPORT')
        )

        self.dedicated_client = CapellaAPIDedicated(
            public_api_url,
            None,  # Don't need access key and secret key for creating a project or getting tenant
            None,  # IDs (which are the only things we need it for)
            os.getenv('CBC_USER'),
            os.getenv('CBC_PWD')
        )

        if (tenant := self.options.capella_tenant) is None:
            tenant = self.infra_spec.infrastructure_settings.get('cbc_tenant', None)
        else:
            self.infra_spec.config.set('infrastructure', 'cbc_tenant', tenant)
            self.infra_spec.update_spec_file()

        self.tenant_id = tenant or self.get_tenant_id()

        if (dp_id := self.options.capella_dataplane) is None:
            dp_id = self.infra_spec.infrastructure_settings.get('cbc_dataplane', None)
        else:
            self.infra_spec.config.set('infrastructure', 'cbc_dataplane', dp_id)
            self.infra_spec.update_spec_file()

        self.dp_id = dp_id

        self.project_id = self.infra_spec.infrastructure_settings.get('cbc_project', None)
        self.cluster_id = self.infra_spec.infrastructure_settings.get('cbc_cluster', None)

    def get_tenant_id(self):
        logger.info('Getting tenant ID...')
        resp = self.dedicated_client.list_accessible_tenants()
        raise_for_status(resp)
        tenant_id = resp.json()[0]['id']
        self.infra_spec.config.set('infrastructure', 'cbc_tenant', tenant_id)
        self.infra_spec.update_spec_file()
        logger.info('Found tenant ID: {}'.format(tenant_id))
        return tenant_id

    def deploy(self):
        if not self.options.capella_only:
            logger.info('Deploying non-Capella resources')
            # Configure terraform
            Terraform.populate_tfvars(self)
            self.terraform_init(self.backend)

            # Deploy non-capella resources
            self.terraform_apply(self.backend)
            non_capella_output = self.terraform_output(self.backend)
            Terraform.update_spec(self, non_capella_output)
        else:
            logger.info('Skipping deploying non-Capella resources as --capella-only flag is set')

        # Deploy serverless dataplane + databases
        self.deploy_serverless_dataplane()

        if not self.options.no_serverless_dbs:
            self.create_project()
            self.create_serverless_dbs()
        else:
            logger.info('Skipping deploying serverless dbs as --no-serverless-dbs flag is set')

        self.update_spec()

    def destroy(self):
        # Destroy non-capella resources
        self.terraform_destroy(self.backend)

        dbs_destroyed = False

        # Destroy capella cluster
        if self.dp_id:
            dbs_destroyed = self.destroy_serverless_databases()
            if dbs_destroyed and not self.options.keep_cluster:
                self.destroy_serverless_dataplane()
        else:
            logger.warn('No serverless dataplane ID found. Not destroying serverless dataplane.')

        if self.tenant_id and self.project_id and dbs_destroyed:
            self.destroy_project()
        else:
            logger.warn('No tenant ID or project ID found. Not destroying project.')

    def deploy_serverless_dataplane(self):
        if not self.dp_id:
            logger.info('Deploying serverless dataplane')
            # If no dataplane ID given (which is normal) then we deploy a new one
            nebula_config = self.infra_spec.direct_nebula
            dapi_config = self.infra_spec.data_api

            config = remove_nulls({
                "provider": "aws",
                "region": self.region,
                'overRide': {
                    'couchbase': {
                        'image': self.options.capella_ami,
                        'version': self.options.capella_cb_version,
                        'specs': (
                            specs[0]
                            if (specs := self.construct_capella_server_groups(
                                self.infra_spec,
                                self.node_list['clusters'],
                                self.options.enable_disk_autoscaling
                            ))
                            else None
                        )
                    },
                    'nebula': {
                        'image': self.options.nebula_ami,
                        'compute': {
                            'type': nebula_config.get('instance_type', None),
                            'count': {
                                'min': maybe_atoi(nebula_config.get('min_count', '')),
                                'max': maybe_atoi(nebula_config.get('max_count', '')),
                                'overRide': maybe_atoi(nebula_config.get('override_count', ''))
                            }
                        }
                    },
                    'dataApi': {
                        'image': self.options.dapi_ami,
                        'compute': {
                            'type': dapi_config.get('instance_type', None),
                            'count': {
                                'min': maybe_atoi(dapi_config.get('min_count', '')),
                                'max': maybe_atoi(dapi_config.get('max_count', '')),
                                'overRide': maybe_atoi(dapi_config.get('override_count', ''))
                            }
                        }
                    }
                }
            })

            logger.info(pretty_dict(config))

            resp = self.serverless_client.create_serverless_dataplane(config)
            raise_for_status(resp)
            self.dp_id = resp.json().get('dataplaneId')
            logger.info('Initialised deployment for serverless dataplane {}'.format(self.dp_id))
            logger.info('Saving dataplane ID to spec file.')
            self.infra_spec.config.set('infrastructure', 'cbc_dataplane', self.dp_id)
            self.infra_spec.update_spec_file()
        else:
            logger.info('Skipping serverless dataplane deployment as existing dataplane specified: '
                        '{}'.format(self.dp_id))
            logger.info('Verifying existing dataplane deployment')

        # Whether we have just deployed a dataplane or not, we will confirm the deployment
        # status of the dataplane to check its ready for a test
        resp = self.serverless_client.get_dataplane_deployment_status(self.dp_id)
        raise_for_status(resp)
        status = resp.json()['status']['state']

        self.cluster_id = resp.json()['couchbaseCluster']['id']
        self.infra_spec.config.set('infrastructure', 'cbc_cluster', self.cluster_id)
        self.infra_spec.update_spec_file()

        if self.options.disable_autoscaling:
            self.disable_autoscaling()

        timeout_mins = self.options.capella_timeout
        interval_secs = 30
        status = None
        t0 = time()
        while (time() - t0) < timeout_mins * 60 and status != 'ready':
            resp = self.serverless_client.get_dataplane_deployment_status(self.dp_id)
            raise_for_status(resp)
            status = resp.json()['status']['state']

            logger.info('Dataplane state: {}'.format(status))
            if status == 'disabled':
                break
            elif status != 'ready':
                sleep(interval_secs)

        if status != 'ready':
            if status == 'disabled':
                logger.error('Deployment failed, dataplane entered disabled state.')
            else:
                logger.error('Deployment timed out after {} mins.'.format(timeout_mins))
            logger.error('DataDog link for debugging (filtering by dataplane ID): {}'
                         .format(format_datadog_link(dataplane_id=self.dp_id)))
            logger.error('DataDog link for debugging (filtering by cluster ID): {}'
                         .format(format_datadog_link(cluster_id=self.cluster_id)))
            exit(1)

        resp = self.serverless_client.get_serverless_dataplane_info(self.dp_id)
        raise_for_status(resp)
        logger.info('Deployed dataplane info: {}'.format(pretty_dict(resp.json())))

    def disable_autoscaling(self):
        logger.info('Creating deployment circuit breaker to prevent auto-scaling.')

        resp = self.serverless_client.create_circuit_breaker(self.cluster_id)
        raise_for_status(resp)

        resp = self.serverless_client.get_circuit_breaker(self.cluster_id)
        raise_for_status(resp)

        logger.info('Circuit breaker created: {}'.format(pretty_dict(resp.json())))

    def _create_db(self, name, width=1, weight=30):
        logger.info('Adding new serverless DB: {}'.format(name))

        data = {
            'name': name,
            'tenantId': self.tenant_id,
            'projectId': self.project_id,
            'provider': self.backend,
            'region': self.region,
            'overRide': {
                'width': width,
                'weight': weight,
                'dataplaneId': self.dp_id
            },
            'dontImportSampleData': True
        }

        logger.info('DB configuration: {}'.format(pretty_dict(data)))

        resp = self.serverless_client.create_serverless_database_overRide(data)
        raise_for_status(resp)
        return resp.json()

    def _get_db_info(self, db_id):
        resp = self.serverless_client.get_database_debug_info(db_id)
        raise_for_status(resp)
        return resp.json()

    def create_serverless_dbs(self):
        dbs = {}

        if not (init_db_map := self.test_config.serverless_db.init_db_map):
            init_db_map = {
                'bucket-{}'.format(i+1): {'width': 1, 'weight': 30}
                for i in range(self.test_config.cluster.num_buckets)
            }

        for db_name, params in init_db_map.items():
            resp = self._create_db(db_name, params['width'], params['weight'])
            db_id = resp['databaseId']
            logger.info('Database ID for {}: {}'.format(db_name, db_id))
            dbs[db_id] = {
                'name': db_name,
                'width': params['width'],
                'weight': params['weight'],
                'nebula_uri': None,
                'dapi_uri': None,
                'access': None,
                'secret': None
            }
            self.test_config.serverless_db.update_db_map(dbs)

        timeout_mins = self.options.capella_timeout
        interval_secs = 20
        t0 = time()
        db_ids = list(dbs.keys())
        while db_ids and time() - t0 < timeout_mins * 60:
            for db_id in db_ids:
                db_info = self._get_db_info(db_id)['database']
                db_state = db_info['status']['state']
                logger.info('{} state: {}'.format(db_id, db_state))
                if db_state == 'ready':
                    logger.info('Serverless DB deployed: {}'.format(db_id))
                    db_ids.remove(db_id)
                    dbs[db_id]['nebula_uri'] = db_info['connect']['sdk']
                    dbs[db_id]['dapi_uri'] = db_info['connect']['dataApi']

            if db_ids:
                sleep(interval_secs)

        self.test_config.serverless_db.update_db_map(dbs)

        if db_ids:
            logger.error('Serverless DB deployment timed out after {} mins'.format(timeout_mins))
            for db_id in db_ids:
                logger.error('DataDog link for debugging (database {}): {}'
                             .format(db_id, format_datadog_link(database_id=db_id)))
            exit(1)
        else:
            logger.info('All serverless DBs deployed')

    def update_spec(self):
        resp = self.serverless_client.get_serverless_dataplane_info(self.dp_id)
        raise_for_status(resp)
        dp_info = resp.json()

        resp = self.serverless_client.get_access_to_serverless_dataplane_nodes(self.dp_id)
        raise_for_status(resp)
        dp_creds = resp.json()

        hostname = dp_info['couchbase']['nodes'][0]['hostname']
        auth = (
            dp_creds['couchbaseCreds']['username'],
            dp_creds['couchbaseCreds']['password']
        )
        self.infra_spec.config.set('credentials', 'rest', ':'.join(auth).replace('%', '%%'))

        default_pool = self.get_default_pool(hostname, auth)
        nodes = []
        for node in default_pool['nodes']:
            hostname = node['hostname'].removesuffix(':8091')
            services = sorted(node['services'], key=lambda s: s != 'kv')
            services_str = ','.join(services)
            group = node['serverGroup'].removeprefix('group:')
            nodes.append((hostname, services_str, group))

        nodes = sorted(nodes, key=lambda n: n[2])
        nodes = [':'.join(n) for n in sorted(nodes, key=lambda n: '' if 'kv' in n[1] else n[1])]
        node_string = '\n'.join(nodes)

        self.infra_spec.config.set('clusters', 'serverless', node_string)

        self.infra_spec.update_spec_file()

    def get_default_pool(self, hostname, auth):
        session = requests.Session()
        resp = session.get('https://{}:18091/pools/default'.format(hostname),
                           auth=auth, verify=False)
        raise_for_status(resp)
        return resp.json()

    def create_project(self):
        logger.info('Creating project for serverless DBs')
        resp = self.dedicated_client.create_project(
            self.tenant_id,
            self.options.tag or 'perf-{}'.format(uuid4().hex[:6])
        )
        raise_for_status(resp)
        project_id = resp.json()['id']
        self.project_id = project_id
        self.infra_spec.config.set('infrastructure', 'cbc_project', project_id)
        self.infra_spec.update_spec_file()
        logger.info('Project created: {}'.format(project_id))

    def _destroy_db(self, db_id):
        logger.info('Destroying serverless DB {}'.format(db_id))
        resp = self.serverless_client.delete_database(self.tenant_id, self.project_id, db_id)
        raise_for_status(resp)
        logger.info('Serverless DB queued for deletion: {}'.format(db_id))

    def destroy_serverless_databases(self) -> bool:
        logger.info('Deleting all serverless databases...')

        pending_dbs = []
        for db_id in self.test_config.serverless_db.db_map:
            self._destroy_db(db_id)
            pending_dbs.append(db_id)
        logger.info('All serverless databases queued for deletion.')

        overall_timeout_mins = self.options.capella_timeout
        retries = 2
        timeout_mins = overall_timeout_mins / (retries + 1)
        interval_secs = 20
        while pending_dbs and retries >= 0:
            logger.info('Waiting for databases to be deleted (retries left: {})'.format(retries))
            t0 = time()
            while pending_dbs and time() - t0 < timeout_mins * 60:
                sleep(interval_secs)
                resp = self.serverless_client.list_all_databases(self.tenant_id, self.project_id)
                raise_for_status(resp)
                pending_dbs = [db['data']['id'] for db in resp.json()['data']]
                logger.info('{} databases remaining: {}'.format(len(pending_dbs), pending_dbs))

            if pending_dbs:
                logger.info('Database deletion timeout reached.'
                            'Retrying deletion of remaining databases.')
                for db_id in pending_dbs:
                    self._destroy_db(db_id)
            else:
                logger.info('All databases successfully deleted.')
                return True

            retries -= 1

        if pending_dbs:
            logger.error('Timed out after {} mins waiting for databases to delete.'
                         .format(overall_timeout_mins))
            for db_id in pending_dbs:
                logger.error('DataDog link for debugging (database {}): {}'
                             .format(db_id, format_datadog_link(database_id=db_id)))
            return False

    def destroy_serverless_dataplane(self):
        logger.info('Deleting serverless dataplane...')
        while (resp := self.serverless_client.delete_dataplane(self.dp_id)).status_code == 422:
            logger.info("Waiting for databases to be fully deleted...")
            sleep(5)
        raise_for_status(resp)
        logger.info('Serverless dataplane successfully queued for deletion.')

    def destroy_project(self):
        logger.info('Deleting project...')
        resp = self.dedicated_client.delete_project(self.tenant_id, self.project_id)
        if resp.status_code == 400 and resp.json()['errorType'] == 'DeleteProjectsWithDatabases':
            logger.warn('Cannot delete project because some serverless databases failed to delete.')
            return
        else:
            raise_for_status(resp)
        logger.info('Project successfully queued for deletion.')


class EKSTerraform(Terraform):
    pass


class AppServicesTerraform(Terraform):
    def __init__(self, options):
        super().__init__(options)

        self.test_config = None
        if options.test_config:
            test_config = TestConfig()
            test_config.parse(options.test_config, override=options.override)
            self.test_config = test_config

        self.backend = self.infra_spec.infrastructure_settings['backend']

        if public_api_url := self.options.capella_public_api_url:
            env = public_api_url.removeprefix('https://')\
                                .removesuffix('.nonprod-project-avengers.com')\
                                .split('.', 1)[1]
            self.infra_spec.config.set('infrastructure', 'cbc_env', env)

        if tenant := self.options.capella_tenant:
            self.infra_spec.config.set('infrastructure', 'cbc_tenant', tenant)

        if project := self.options.capella_project:
            self.infra_spec.config.set('infrastructure', 'cbc_project', project)

        self.infra_spec.update_spec_file()

        self.tenant_id = self.infra_spec.infrastructure_settings['cbc_tenant']
        self.project_id = self.infra_spec.infrastructure_settings['cbc_project']
        self.cluster_ids = self.infra_spec.infrastructure_settings.get('cbc_cluster', '').split()
        self.cluster_id = self.cluster_ids[0]

        logger.info("The tenant id is: {}".format(self.tenant_id))
        logger.info("The project id is: {}".format(self.project_id))
        logger.info("The cluster id is: {}".format(self.cluster_id))

        self.api_client = CapellaAPIDedicated(
            'https://cloudapi.{}.nonprod-project-avengers.com'.format(
                self.infra_spec.infrastructure_settings['cbc_env']
            ),
            os.getenv('CBC_SECRET_KEY'),
            os.getenv('CBC_ACCESS_KEY'),
            os.getenv('CBC_USER'),
            os.getenv('CBC_PWD')
        )

        self.capella_timeout = max(0, self.options.capella_timeout)

    def deploy(self):
        # Configure terraform
        logger.info("Started deploying the AS")
        # Deploy capella cluster
        # Create sgw backend(app services)
        logger.info("Deploying sgw backend")
        sgw_cluster_id = self.deploy_cluster_internal_api()

        # Create sgw database(app endpoint)
        logger.info("Deploying sgw database")
        sgw_db_id, sgw_db_name = self.deploy_sgw_db(sgw_cluster_id)

        # Set sync function
        logger.info("Setting sync function")
        sync_function = "function (doc) { channel(doc.channels); }"
        self.api_client.update_sync_function_sgw(self.tenant_id, self.project_id,
                                                 self.cluster_id, sgw_cluster_id,
                                                 sgw_db_name, sync_function)

        # Resume sgw database
        logger.info("Resuming sgw database")
        self.api_client.resume_sgw_database(self.tenant_id, self.project_id,
                                            self.cluster_id, sgw_cluster_id,
                                            sgw_db_name)

        # Allow my IP
        logger.info("Whitelisting own IP on sgw backend")
        self.api_client.allow_my_ip_sgw(self.tenant_id, self.project_id,
                                        self.cluster_id, sgw_cluster_id)

        # Add allowed IPs
        logger.info("Whitelisting IPs")
        client_ips = self.infra_spec.clients
        logger.info("The client list is: {}".format(client_ips))
        if self.infra_spec.capella_backend == 'aws':
            client_ips = [
                dns.split('.')[0].removeprefix('ec2-').replace('-', '.') for dns in client_ips
            ]
        logger.info("The client list is: {}".format(client_ips))
        for client_ip in client_ips:
            self.api_client.add_allowed_ip_sgw(self.tenant_id, self.project_id,
                                               sgw_cluster_id, self.cluster_id,
                                               client_ip)

        # Add app roles
        logger.info("Adding app roles")
        app_role = {"name": "moderator", "admin_channels": []}
        self.api_client.add_app_role_sgw(self.tenant_id, self.project_id,
                                         self.cluster_id, sgw_cluster_id,
                                         sgw_db_name, app_role)
        app_role = {"name": "admin", "admin_channels": []}
        self.api_client.add_app_role_sgw(self.tenant_id, self.project_id,
                                         self.cluster_id, sgw_cluster_id,
                                         sgw_db_name, app_role)

        # Add user
        logger.info("Adding user")
        user = {"email": "", "password": "Password123!", "name": "guest",
                "disabled": False, "admin_channels": ["*"], "admin_roles": []}
        self.api_client.add_user_sgw(self.tenant_id, self.project_id,
                                     self.cluster_id, sgw_cluster_id,
                                     sgw_db_name, user)

        # Add admin user
        logger.info("Adding admin user")
        admin_user = {"name": "Administrator", "password": "Password123!"}
        self.api_client.add_admin_user_sgw(self.tenant_id, self.project_id,
                                           self.cluster_id, sgw_cluster_id,
                                           sgw_db_name, admin_user)

        # Update cluster spec file
        self.update_spec(sgw_cluster_id, sgw_db_name)

    def destroy(self):
        # Destroy capella cluster
        sgw_cluster_id = self.infra_spec.infrastructure_settings['app_services_cluster']
        self.destroy_cluster_internal_api(sgw_cluster_id)

    def update_spec(self, sgw_cluster_id, sgw_db_name):
        # Get sgw public and private ips
        resp = self.api_client.get_sgw_links(self.tenant_id, self.project_id,
                                             self.cluster_id, sgw_cluster_id,
                                             sgw_db_name)

        logger.info("The connect response is: {}".format(resp))
        adminurl = resp.json().get('data').get('adminURL').split(':')[1].split('//')[1]
        logger.info("The admin url is: {}".format(adminurl))
        self.infra_spec.config.add_section('sgw_schemas')
        for option, value in self.infra_spec.infrastructure_syncgateways.items():
            self.infra_spec.config.set('sgw_schemas', option, value)

        sgw_option = self.infra_spec.config.options('syncgateways')[0]
        sgw_list = []
        for i in range(0, self.test_config.syncgateway_settings.nodes):
            sgw_list.append(adminurl)
        logger.info("the sgw list is: {}".format(sgw_list))
        self.infra_spec.config.set('syncgateways', sgw_option, '\n' + '\n'.join(sgw_list))
        self.infra_spec.update_spec_file()

    @staticmethod
    def capella_sgw_group_sizes(self, node_schemas):
        sgw_groups = {}
        for node in node_schemas:
            _, cluster, node_group, _ = node.split('.')
            if cluster not in sgw_groups:
                sgw_groups[cluster] = [node_group]
            else:
                sgw_groups[cluster].append(node_group)

        sgw_group_sizes = {
            cluster: Counter(node_groups) for cluster, node_groups in sgw_groups.items()
        }

        return sgw_group_sizes

    def construct_capella_sgw_groups(self, infra_spec, node_schemas):
        sgw_groups = AppServicesTerraform.capella_sgw_group_sizes(node_schemas)

        for sgw_groups in sgw_groups.items():

            for (node_group) in sgw_groups.items():
                node_group_config = infra_spec.infrastructure_section(node_group)

                return node_group_config['instance_type']

    def deploy_cluster_internal_api(self):
        """Sample config.

        {
            "clusterId": "71eff4a9-bc4a-442f-8196-3339f9128c08",
            "name": "my-sync-gateway-backend",
            "description": "sgw backend that drives my amazing app",
            "desired_capacity": 2
            "compute": {"type":"c5.large"}
        }
        """
        config = {
            "clusterId": self.cluster_id,
            "name": "perf-sgw-{}".format(self.uuid),
            "description": "",
            "desired_capacity": self.test_config.syncgateway_settings.nodes,
            "compute": {"type": self.test_config.syncgateway_settings.instance}
        }

        logger.info("The payload is: {}".format(config))

        if self.options.capella_cb_version and self.options.capella_sgw_ami:
            config['overRide'] = {
                'token': os.getenv('CBC_OVERRIDE_TOKEN'),
                'server': self.options.capella_cb_version,
                'image': self.options.capella_sgw_ami
            }
            logger.info('Deploying with custom AMI: {}'.format(self.options.capella_sgw_ami))

        resp = self.api_client.create_sgw_backend(self.tenant_id, config)
        raise_for_status(resp)
        sgw_cluster_id = resp.json().get('id')
        logger.info('Initialised app services deployment {}'.format(sgw_cluster_id))
        logger.info('Saving app services ID to spec file.')

        self.infra_spec.config.set('infrastructure', 'app_services_cluster', sgw_cluster_id)
        self.infra_spec.update_spec_file()

        timeout_mins = self.capella_timeout
        interval_secs = 30
        pending_sgw_cluster = sgw_cluster_id
        t0 = time()
        while pending_sgw_cluster and (time() - t0) < timeout_mins * 60:
            sleep(interval_secs)

            status = self.api_client.get_sgw_info(self.tenant_id, self.project_id,
                                                  self.cluster_id, sgw_cluster_id).json() \
                                                                                  .get('data') \
                                                                                  .get('status') \
                                                                                  .get('state')
            logger.info('Cluster state for {}: {}'.format(pending_sgw_cluster, status))
            if status == "deploymentFailed":
                logger.error('Deployment failed for cluster {}. DataDog link for debugging: {}'
                             .format(pending_sgw_cluster,
                                     format_datadog_link(cluster_id=pending_sgw_cluster)))
                exit(1)

            if status == "healthy":
                break

        return sgw_cluster_id

    def deploy_sgw_db(self, sgw_cluster_id):
        """Sample config.

        {
            "name": "sgw-1",
            "sync": "",
            "bucket": "bucket-1",
            "delta_sync": false,
            "import_filter": ""
        }
        """
        sgw_db_name = "db-1"
        config = {
            "name": "db-1",
            "sync": "",
            "bucket": "bucket-1",
            "delta_sync": False,
            "import_filter": ""
        }
        logger.info(config)

        resp = self.api_client.create_sgw_database(self.tenant_id, self.project_id,
                                                   self.cluster_id, sgw_cluster_id, config)
        raise_for_status(resp)
        sgw_db_id = resp.json().get('id')
        logger.info('Initialised sgw database deployment {}'.format(sgw_db_id))
        logger.info('Saving sgw db ID to spec file.')

        sleep(10)

        return sgw_db_id, sgw_db_name

    def destroy_cluster_internal_api(self, sgw_cluster_id):
        logger.info('Deleting Capella App Services cluster...')
        resp = self.api_client.delete_sgw_backend(self.tenant_id, self.project_id,
                                                  self.cluster_id, sgw_cluster_id)

        timeout_mins = self.capella_timeout
        interval_secs = 30
        pending_sgw_cluster = sgw_cluster_id
        t0 = time()
        while pending_sgw_cluster and (time() - t0) < timeout_mins * 60:
            sleep(interval_secs)
            try:
                status = self.api_client.get_sgw_info(self.tenant_id, self.project_id,
                                                      self.cluster_id, sgw_cluster_id)
                status = status.json().get('data').get('status').get('state')
                logger.info('Cluster state for {}: {}'.format(pending_sgw_cluster, status))
            except Exception as e:
                logger.info("The exception is: {}".format(e))
                sleep(30)
                break

        raise_for_status(resp)
        logger.info('Capella App Services cluster successfully queued for deletion.')


# CLI args.
def get_args():
    parser = ArgumentParser()

    parser.add_argument('-c', '--cluster',
                        required=True,
                        help='the path to a infrastructure specification file')
    parser.add_argument('--test-config',
                        required=False,
                        help='the path to the test configuration file')
    parser.add_argument('--verbose',
                        action='store_true',
                        help='enable verbose logging')
    parser.add_argument('--azure-region',
                        choices=[
                            'eastus',
                            'westeurope',
                            'eastus2',
                            'westus2',
                            'uksouth'
                        ],
                        default='eastus',
                        help='the cloud region (Azure)')
    parser.add_argument('-r', '--aws-region',
                        choices=[
                            'us-east-1',
                            'us-east-2',
                            'us-west-2',
                            'ca-central-1',
                            'ap-northeast-1',
                            'ap-northeast-2',
                            'ap-southeast-1',
                            'ap-south-1',
                            'eu-west-1',
                            'eu-west-2',
                            'eu-west-3',
                            'eu-central-1',
                            'sa-east-1'
                        ],
                        default='us-east-1',
                        help='the cloud region (AWS)')
    parser.add_argument('-z', '--gcp-zone',
                        choices=[
                           'us-central1-a',
                           'us-central1-b',
                           'us-central1-c',
                           'us-central1-f',
                           'us-west1-a',
                           'us-west1-b',
                           'us-west1-c'
                        ],
                        default='us-west1-b',
                        dest='zone',
                        help='the cloud zone (GCP)')
    parser.add_argument('--cluster-image',
                        help='Image/AMI name to use for cluster nodes')
    parser.add_argument('--client-image',
                        help='Image/AMI name to use for client nodes')
    parser.add_argument('--utility-image',
                        help='Image/AMI name to use for utility nodes')
    parser.add_argument('--sgw-image',
                        help='Image/AMI name to use for sync gateway nodes')
    parser.add_argument('--capella-public-api-url',
                        help='public API URL for Capella environment')
    parser.add_argument('--capella-tenant',
                        help='tenant ID for Capella deployment')
    parser.add_argument('--capella-project',
                        help='project ID for Capella deployment')
    parser.add_argument('--capella-dataplane',
                        help='serverless dataplane ID to use for serverless database deployment')
    parser.add_argument('--capella-cb-version',
                        help='cb version to use for Capella deployment')
    parser.add_argument('--capella-ami',
                        help='custom AMI to use for Capella deployment')
    parser.add_argument('--capella-sgw-ami',
                        help='custom AMI to use for App Services Capella Deployment')
    parser.add_argument('--release-id',
                        help='release id for managing releases')
    parser.add_argument('--dapi-ami',
                        help='AMI to use for Data API deployment (serverless)')
    parser.add_argument('--dapi-override-count',
                        type=int,
                        help='number of DAPI nodes to deploy')
    parser.add_argument('--dapi-min-count',
                        type=int,
                        help='minimum number of DAPI nodes in autoscaling group')
    parser.add_argument('--dapi-max-count',
                        type=int,
                        help='maximum number of DAPI nodes in autoscaling group')
    parser.add_argument('--dapi-instance-type',
                        help='instance type to use for DAPI nodes')
    parser.add_argument('--nebula-ami',
                        help='AMI to use for Direct Nebula deployment (serverless)')
    parser.add_argument('--nebula-override-count',
                        type=int,
                        help='number of Direct Nebula nodes to deploy')
    parser.add_argument('--nebula-min-count',
                        type=int,
                        help='minimum number of Direct Nebula nodes in autoscaling group')
    parser.add_argument('--nebula-max-count',
                        type=int,
                        help='maximum number of Direct Nebula nodes in autoscaling group')
    parser.add_argument('--nebula-instance-type',
                        help='instance type to use for Direct Nebula nodes')
    parser.add_argument('--vpc-peering',
                        action='store_true',
                        help='enable VPC peering for Capella deployment')
    parser.add_argument('--capella-timeout',
                        type=int,
                        default=20,
                        help='Timeout (minutes) for Capella deployment when using internal API')
    parser.add_argument('--disable-autoscaling',
                        action='store_true',
                        help='Disable cluster auto-scaling for Capella clusters by creating a '
                             'deployment circuit breaker for the cluster.')
    parser.add_argument('--keep-cluster',
                        action='store_true',
                        help='Don\'t destroy cluster or serverless dataplane, only the clients '
                             'and utilities')
    parser.add_argument('--no-serverless-dbs',
                        action='store_true',
                        help='Don\'t deploy serverless databases, only deploy serverless dataplane')
    parser.add_argument('--capella-only',
                        action='store_true',
                        help='Only deploy Capella resources (provisioned cluster or serverless '
                             'dataplane). Will not deploy perf client or utility nodes.')
    parser.add_argument('-t', '--tag',
                        help='Global tag for launched instances.')
    parser.add_argument('--enable-disk-autoscaling',
                        action='store_true',
                        default=True,
                        help='Enables Capella disk autoscaling')
    parser.add_argument('--multi-az',
                        action="store_true",
                        default=False,
                        help="Deploy Capella cluster to multiple Availability Zones")
    parser.add_argument('override',
                        nargs='*',
                        help='custom cluster and/or test settings')

    return parser.parse_args()


def destroy():
    args = get_args()
    infra_spec = ClusterSpec()
    infra_spec.parse(fname=args.cluster, override=args.override)
    if infra_spec.cloud_provider != 'capella':
        deployer = Terraform(args)
    elif infra_spec.serverless_infrastructure:
        deployer = ServerlessTerraform(args)
    elif infra_spec.app_services == 'true':
        deployer = AppServicesTerraform(args)
    else:
        deployer = CapellaTerraform(args)

    deployer.destroy()


def main():
    args = get_args()
    infra_spec = ClusterSpec()
    infra_spec.parse(fname=args.cluster, override=args.override)
    if infra_spec.cloud_provider != 'capella':
        deployer = Terraform(args)
    elif infra_spec.serverless_infrastructure:
        deployer = ServerlessTerraform(args)
    elif infra_spec.app_services == 'true':
        deployer = AppServicesTerraform(args)
    else:
        deployer = CapellaTerraform(args)

    deployer.deploy()
