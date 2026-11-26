dbset db pg
dbset bm TPC-C
diset connection pg_host exoscale-pg-4vcpu-16gb-exoscale-37d268a1-816e-4c65-8e0e-ff9912d.e.aivencloud.com
diset connection pg_port 26071
diset connection pg_sslmode prefer
diset tpcc pg_superuser {avnadmin}
diset tpcc pg_superuserpass <redacted>
diset tpcc pg_defaultdbase defaultdb
diset tpcc pg_user {avnadmin}
diset tpcc pg_pass <redacted>
diset tpcc pg_dbase tpcc
diset tpcc pg_count_ware 640
diset tpcc pg_driver timed
diset tpcc pg_rampup 5
diset tpcc pg_duration 20
diset tpcc pg_allwarehouse true
diset tpcc pg_timeprofile true
diset tpcc pg_raiseerror true
diset tpcc pg_keyandthink false
diset tpcc pg_storedprocs true
loadscript
vuset vu 16
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
