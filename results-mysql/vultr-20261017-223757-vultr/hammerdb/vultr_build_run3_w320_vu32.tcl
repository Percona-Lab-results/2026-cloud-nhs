dbset db mysql
dbset bm TPC-C
diset connection mysql_host vultr-prod-8bc13ae7-fb94-4ef0-975c-9177772f624a-vultr-prod-def3.vultrdb.com
diset connection mysql_port 23473

diset tpcc mysql_user {vultradmin}
diset tpcc mysql_pass <redacted>
diset tpcc mysql_dbase tpcc
diset tpcc mysql_count_ware 320
diset tpcc mysql_num_vu 16
diset tpcc mysql_storage_engine innodb
diset tpcc mysql_partition false
diset tpcc mysql_no_stored_procs false
diset tpcc mysql_history_pk true
buildschema
