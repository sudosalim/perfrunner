[clusters]
hera =
    172.23.96.117:kv
    172.23.96.118:kv
    172.23.96.119:kv
    172.23.96.120:kv

[clients]
hosts =
    172.23.99.111
credentials = root:couchbase

[storage]
data = /data
index = /data
backup = /workspace/backup

[credentials]
rest = Administrator:password
ssh = root:couchbase

[parameters]
OS = CentOS 7
CPU = E5-2630 v3 (32 vCPU)
Memory = 64GB
Disk = Samsung SM863
