dbset db pg
dbset bm TPC-C
diset connection pg_host upcloud-pg-4cpu-16gb-odqiztpebptc.db.upclouddatabases.com
diset connection pg_port 11569
diset connection pg_sslmode prefer
diset tpcc pg_superuser {upadmin}
diset tpcc pg_superuserpass <redacted>
diset tpcc pg_defaultdbase defaultdb
diset tpcc pg_user {upadmin}
diset tpcc pg_pass <redacted>
diset tpcc pg_dbase tpcc
diset tpcc pg_count_ware 320
diset tpcc pg_num_vu 16
diset tpcc pg_tspace pg_default
diset tpcc pg_partition false
diset tpcc pg_storedprocs true
diset tpcc pg_vacuum true
buildschema
