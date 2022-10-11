[infrastructure]
provider = aws
type = ec2
os_arch = arm

[clusters]
couchbase1 =
        ec2.ec2_cluster_1.ec2_node_group_1.1:kv:group1
        ec2.ec2_cluster_1.ec2_node_group_1.2:kv:group1
        ec2.ec2_cluster_1.ec2_node_group_1.3:kv:group1
        ec2.ec2_cluster_1.ec2_node_group_1.4:index:group1
        ec2.ec2_cluster_1.ec2_node_group_1.5:index:group2
        ec2.ec2_cluster_1.ec2_node_group_1.6:index:group1
        ec2.ec2_cluster_1.ec2_node_group_1.7:index:group2

[clients]
workers1 =
        ec2.ec2_cluster_1.ec2_node_group_2.1

[utilities]
brokers1 = ec2.ec2_cluster_1.ec2_node_group_3.1

[ec2]
clusters = ec2_cluster_1

[ec2_cluster_1]
node_groups = ec2_node_group_1,ec2_node_group_2,ec2_node_group_3
storage_class = gp3

[ec2_node_group_1]
instance_type = c6gd.4xlarge
instance_capacity = 7
volume_size = 1000
volume_type = gp3
volume_throughput = 1000
iops = 12000

[ec2_node_group_2]
instance_type = c5.24xlarge
instance_capacity = 1
volume_size = 100

[ec2_node_group_3]
instance_type = c5.24xlarge
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
CPU = c6gd.4xlarge (16 vCPU)
Memory = 32 GB
Disk = EBS 4TB (gp3)