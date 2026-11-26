dbset db mysql
dbset bm TPC-C
diset connection mysql_host upcloud-perf-odqiztpebptc.db.upclouddatabases.com
diset connection mysql_port 11569

diset tpcc mysql_user {upadmin}
diset tpcc mysql_pass <redacted>
diset tpcc mysql_dbase tpcc
diset tpcc mysql_count_ware 160
diset tpcc mysql_num_vu 24
diset tpcc mysql_storage_engine innodb
diset tpcc mysql_partition false
diset tpcc mysql_no_stored_procs false
diset tpcc mysql_history_pk true
buildschema
