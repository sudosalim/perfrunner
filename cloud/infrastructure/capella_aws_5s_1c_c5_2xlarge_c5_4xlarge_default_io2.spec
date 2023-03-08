[infrastructure]
provider = capella
backend = aws

[clusters]
couchbase1 =
    ec2.ec2_cluster_1.ec2_node_group_1.1:kv
    ec2.ec2_cluster_1.ec2_node_group_1.2:kv
    ec2.ec2_cluster_1.ec2_node_group_1.3:kv
    ec2.ec2_cluster_1.ec2_node_group_2.1:index,n1ql
    ec2.ec2_cluster_1.ec2_node_group_2.2:index,n1ql

[clients]
workers1 =
    ec2.ec2_cluster_1.ec2_node_group_3.1

[utilities]
brokers1 =
    ec2.ec2_cluster_1.ec2_node_group_4.1

[ec2]
clusters = ec2_cluster_1

[ec2_cluster_1]
node_groups = ec2_node_group_1,ec2_node_group_2,ec2_node_group_3,ec2_node_group_4
storage_class = gp3

[ec2_node_group_1]
instance_type = c5.2xlarge
instance_capacity = 3
volume_size = 2000
storage_class = io2
iops = 16000

[ec2_node_group_2]
instance_type = c5.4xlarge
instance_capacity = 2
volume_size = 2000
storage_class = io2
iops = 16000

[ec2_node_group_3]
instance_type = c5.24xlarge
instance_capacity = 1
volume_size = 100

[ec2_node_group_4]
instance_type = c5.9xlarge
instance_capacity = 1
volume_size = 100

[storage]
data = var/cb/data

[credentials]
rest = Administrator:Password123!
ssh = root:couchbase

[parameters]
os = Amazon Linux 2
cpu = Data: c5.2xlarge (8 vCPU), Index/Query: c5.4xlarge (16 vCPU)
memory = Data: 16GB, Index/Query: 32GB
disk = gp3, 2000GB, 16000 IOPS