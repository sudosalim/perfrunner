[infrastructure]
provider = azure
type = azurerm

[clusters]
couchbase1 =
    azurerm.azurerm_cluster_1.azurerm_node_group_1.1:kv,index,n1ql
    azurerm.azurerm_cluster_1.azurerm_node_group_1.2:kv,index,n1ql
    azurerm.azurerm_cluster_1.azurerm_node_group_1.3:kv,index,n1ql

[clients]
workers1 =
    azurerm.azurerm_cluster_1.azurerm_node_group_2.1

[utilities]
brokers1 = azurerm.azurerm_cluster_1.azurerm_node_group_3.1

[azurerm]
clusters = azurerm_cluster_1

[azurerm_cluster_1]
node_groups = azurerm_node_group_1,azurerm_node_group_2,azurerm_node_group_3
storage_class = Premium_LRS

[azurerm_node_group_1]
instance_type = Standard_F8s_v2
instance_capacity = 3
volume_size = 256
disk_tier = P20

[azurerm_node_group_2]
instance_type = Standard_D32as_v4
instance_capacity = 1
volume_size = 100

[azurerm_node_group_3]
instance_type = Standard_D16as_v4
instance_capacity = 1
volume_size = 100

[storage]
data = /data

[credentials]
rest = Administrator:password
ssh = root:couchbase

[parameters]
os = CentOS 7
cpu = Standard_F8s_v2 (8 vCPU)
memory = 16 GB
disk = Premium SSD 256GB (P15)