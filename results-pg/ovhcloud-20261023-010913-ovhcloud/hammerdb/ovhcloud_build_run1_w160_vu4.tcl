dbset db pg
dbset bm TPC-C
diset connection pg_host postgresql-4338e31a-o59368f7f.database.cloud.ovh.net
diset connection pg_port 20184
diset connection pg_sslmode prefer
diset tpcc pg_superuser {avnadmin}
diset tpcc pg_superuserpass <redacted>
diset tpcc pg_defaultdbase defaultdb
diset tpcc pg_user {avnadmin}
diset tpcc pg_pass <redacted>
diset tpcc pg_dbase tpcc
diset tpcc pg_count_ware 160
diset tpcc pg_num_vu 16
diset tpcc pg_tspace pg_default
diset tpcc pg_partition false
diset tpcc pg_storedprocs true
diset tpcc pg_vacuum true
buildschema
