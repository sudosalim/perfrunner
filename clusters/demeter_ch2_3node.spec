[clusters]
demeter =
    172.23.100.161:kv,index,n1ql
    172.23.100.162:cbas
    172.23.100.163:cbas

[clients]
hosts =
    172.23.100.165
credentials = root:couchbase

[storage]
data = /data
analytics = /data1

[credentials]
rest = Administrator:password
ssh = root:couchbase

[parameters]
OS = CentOS 7
CPU = E5-2630 v3 (32 vCPU)
Memory = 64 GB
Disk = Samsung 860 1TB
