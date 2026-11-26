dbset db pg
dbset bm TPC-C
diset connection pg_host vultr-prod-32c5bec6-a843-43c2-bfb7-a9fd902f0bf8-vultr-prod-def3.vultrdb.com
diset connection pg_port 23473
diset connection pg_sslmode prefer
diset tpcc pg_superuser {vultradmin}
diset tpcc pg_superuserpass <redacted>
diset tpcc pg_defaultdbase defaultdb
diset tpcc pg_user {vultradmin}
diset tpcc pg_pass <redacted>
diset tpcc pg_dbase tpcc
diset tpcc pg_count_ware 320
diset tpcc pg_num_vu 48
diset tpcc pg_tspace pg_default
diset tpcc pg_partition false
diset tpcc pg_storedprocs true
diset tpcc pg_vacuum true
buildschema
