dbset db mysql
dbset bm TPC-C
diset connection mysql_host exoscale4vcpu16gb-exoscale-37d268a1-816e-4c65-8e0e-ff9912ddcfcf.k.aivencloud.com
diset connection mysql_port 26071

diset tpcc mysql_user {avnadmin}
diset tpcc mysql_pass <redacted>
diset tpcc mysql_dbase tpcc
diset tpcc mysql_count_ware 160
diset tpcc mysql_num_vu 16
diset tpcc mysql_storage_engine innodb
diset tpcc mysql_partition false
diset tpcc mysql_no_stored_procs false
diset tpcc mysql_history_pk true
buildschema
