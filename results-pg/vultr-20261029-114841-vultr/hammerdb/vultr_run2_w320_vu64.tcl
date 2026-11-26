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
diset tpcc pg_driver timed
diset tpcc pg_rampup 5
diset tpcc pg_duration 20
diset tpcc pg_allwarehouse true
diset tpcc pg_timeprofile true
diset tpcc pg_raiseerror true
diset tpcc pg_keyandthink false
diset tpcc pg_storedprocs true
loadscript
vuset vu 64
vuset logtotemp 1
vucreate
tcstart
set jobid [ vurun ]
vudestroy
tcstop
# vurun returns either the job id or "Jobid = <id>"; accept both.
set jid $jobid
if { [ llength [ split $jobid = ] ] > 1 } {
    set jid [ string trim [ lindex [ split $jobid = ] 1 ] ]
}
puts "JOBID $jid"
if { [ catch {
    puts "TRANSACTION RESPONSE TIMES"
    job $jid timing
    puts "TRANSACTION COUNT"
    job $jid tcount
    puts "HAMMERDB RESULT"
    job $jid result
} err ] } { puts "RESULT EXTRACTION FAILED: $err" }
