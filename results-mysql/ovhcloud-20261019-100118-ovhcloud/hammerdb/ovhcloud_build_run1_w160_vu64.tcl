dbset db mysql
dbset bm TPC-C
diset connection mysql_host mysql-84cd30a3-o59368f7f.database.cloud.ovh.net
diset connection mysql_port 20184

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
