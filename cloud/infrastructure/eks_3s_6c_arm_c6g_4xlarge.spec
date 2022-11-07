[infrastructure]
provider = aws
type = kubernetes
os_arch = arm

[clusters]
couchbase1 =
        k8s.k8s_cluster_1.k8s_node_group_1.1:kv
        k8s.k8s_cluster_1.k8s_node_group_1.2:kv
        k8s.k8s_cluster_1.k8s_node_group_1.3:kv

[clients]
workers1 =
        k8s.k8s_cluster_1.k8s_node_group_2.1
        k8s.k8s_cluster_1.k8s_node_group_2.2
        k8s.k8s_cluster_1.k8s_node_group_2.3
        k8s.k8s_cluster_1.k8s_node_group_2.4
        k8s.k8s_cluster_1.k8s_node_group_2.5
        k8s.k8s_cluster_1.k8s_node_group_2.6

[utilities]
brokers1 = k8s.k8s_cluster_1.k8s_node_group_3
operators1 = k8s.k8s_cluster_1.k8s_node_group_3

[k8s]
clusters = k8s_cluster_1
worker_cpu_limit = 14
worker_mem_limit = 25

[k8s_cluster_1]
node_groups = k8s_node_group_1,k8s_node_group_2,k8s_node_group_3
version = 1.21
storage_class = gp2

[k8s_node_group_1]
instance_type = c6g.4xlarge
instance_capacity = 3
volume_size = 100

[k8s_node_group_2]
instance_type = c5.4xlarge
instance_capacity = 6
volume_size = 100

[k8s_node_group_3]
instance_type = c5.4xlarge
instance_capacity = 1
volume_size = 100

[storage]
data = /data

[credentials]
rest = Administrator:password
ssh = root:couchbase
aws_key_name = korry

[parameters]
OS = CentOS 7
CPU = c6g.4xlarge (16 vCPU)
Memory = 32 GB
Disk = EBS 1TB