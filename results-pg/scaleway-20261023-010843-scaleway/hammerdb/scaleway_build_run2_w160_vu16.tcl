dbset db pg
dbset bm TPC-C
diset connection pg_host 78.232.128.21
diset connection pg_port 23299
diset connection pg_sslmode prefer
diset tpcc pg_superuser {scaledbs}
diset tpcc pg_superuserpass <redacted>
diset tpcc pg_defaultdbase rdb
diset tpcc pg_user {scaledbs}
diset tpcc pg_pass <redacted>
diset tpcc pg_dbase tpcc
diset tpcc pg_count_ware 160
diset tpcc pg_num_vu 16
diset tpcc pg_tspace pg_default
diset tpcc pg_partition false
diset tpcc pg_storedprocs true
diset tpcc pg_vacuum true
buildschema
