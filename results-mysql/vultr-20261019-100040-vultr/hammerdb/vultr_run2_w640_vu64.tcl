dbset db mysql
dbset bm TPC-C
diset connection mysql_host vultr-prod-8bc13ae7-fb94-4ef0-975c-9177772f624a-vultr-prod-def3.vultrdb.com
diset connection mysql_port 23473

diset tpcc mysql_user {vultradmin}
diset tpcc mysql_pass <redacted>
diset tpcc mysql_dbase tpcc
diset tpcc mysql_count_ware 640
diset tpcc mysql_driver timed
diset tpcc mysql_rampup 5
diset tpcc mysql_duration 20
diset tpcc mysql_allwarehouse true
diset tpcc mysql_timeprofile true
diset tpcc mysql_raiseerror true
diset tpcc mysql_keyandthink false
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
