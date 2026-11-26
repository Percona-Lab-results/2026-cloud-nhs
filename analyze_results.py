#!/usr/bin/env python3
"""
analyze_results.py - build TPS / NOPM graphs from a cloud-mysql-perf.sh results tree.

The results root holds one directory per benchmark session, named
<provider>-<UTC stamp>[-<label>].  A session is classified by the result CSV it
contains, so the sysbench and the HammerDB session of a provider are kept apart
regardless of the order they were run in:

    <provider>_sysbench_results.csv  -> sysbench oltp_read_write, metric TPS
    <provider>_hammerdb_results.csv  -> HammerDB TPC-C,           metric NOPM

One graph per image file: a scaling chart and a peak chart for each data-set
size, each emitted once per theme (light / dark), plus the table view of the
same aggregates as CSV.  Charts of the same benchmark share a y-scale across the
sizes, so separate files stay comparable.  Rows whose metric is empty or NA are
dropped and reported - an aborted run leaves those behind.

Cost efficiency is deliberately *not* one of those per-benchmark charts.  It is
one number per provider, and it is quoted at the peak: every measured run is
read as a rate - the transactions the host committed while it ran, from the
database server's own counters, over the hours it took - the runs of one
benchmark, data size and concurrency are averaged together, and the best such
point is priced as a single hour at the provider's hourly rate.  So the length
of the campaign cancels out, and what is left is how much work a dollar buys at
full speed.  Schema initialisation - the drop, the rebuild and the data load
before every run - and the idle time around it are reported next to the figure
but enter neither half, and an iteration whose benchmark failed is dropped from
all of it and cannot stand as a peak.

Needs only numpy + matplotlib (no pandas).
"""
from __future__ import annotations

import argparse
import csv
import re
import sys
import textwrap
from collections import defaultdict
from datetime import datetime
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.patheffects as pe
import matplotlib.pyplot as plt
import matplotlib.ticker as mticker
import numpy as np
from matplotlib.lines import Line2D
from matplotlib.path import Path as MPath
from matplotlib.patches import PathPatch, Rectangle

import html_report

# --------------------------------------------------------------------- theme --
# Values are the documented default data-viz palette.  Categorical slots are
# assigned in fixed order and never cycled; a provider keeps its hue whatever
# else is on the chart.  Validated on the adjacent pairlist (lines/bars) in both
# modes: worst adjacent CVD delta-E 9.1 light / 8.4 dark (target >= 8), worst
# adjacent normal-vision delta-E 19.6 / 19.3 (floor >= 15).
SERIES = {
    "light": ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300"],
    "dark": ["#3987e5", "#d95926", "#199e70", "#c98500", "#d55181", "#008300"],
}
THEME = {
    "light": dict(
        surface="#ffffff", plane="#ffffff", primary="#0b0b0b",
        secondary="#52514e", muted="#898781", grid="#e1e0d9", axis="#c3c2b7",
    ),
    "dark": dict(
        surface="#1a1a19", plane="#0d0d0d", primary="#ffffff",
        secondary="#c3c2b7", muted="#898781", grid="#2c2c2a", axis="#383835",
    ),
}
# Secondary encoding, so identity never rests on hue alone (two light-mode
# slots sit below 3:1 on the light surface; the table view is the other relief).
MARKERS = ["o", "s", "^", "D", "v", "P"]

FONT = ["DejaVu Sans", "system-ui", "sans-serif"]
MAXSERIES = len(MARKERS)

DIRNAME = re.compile(r"^(?P<provider>[A-Za-z0-9]+)-(?P<stamp>\d{8}-\d{6})(?:-(?P<label>.+))?$")

# ----------------------------------------------------------------- pricing --
# USD per hour for the benchmarked instance at each provider, all six already
# quoted in USD, so nothing converts and no rate table dates the report.  Pinned
# here rather than fetched at render time: a report that silently re-prices
# itself is not reproducible.
#
# An hourly rate only becomes a cost once it is multiplied by hours, and the
# hours that count are the ones the harness itself ran - database init plus
# benchmark, start to finish of each session - not a billing period.  That span
# comes from the session's own dated stamps; see session_clock() below.
RATES_USD_HOUR = {
    "exoscale": 0.65,
    "scaleway": 0.57,
    "vultr": 0.429,
    "upcloud": 0.301,
    "digitalocean": 0.35,
    "ovhcloud": 0.49,
}


def rate_usd_hour(provider: str) -> float | None:
    """The provider's hourly price, or None if the instance is not priced."""
    return RATES_USD_HOUR.get(provider.lower())


def cost_usd(provider: str, hours: float | None) -> float | None:
    """What `hours` of the instance cost, or None if either side is missing."""
    rate = rate_usd_hour(provider)
    return None if rate is None or not hours else rate * hours


# ---------------------------------------------------------------- discovery --
class Session:
    """One results directory: a provider, a timestamp and one benchmark's rows."""

    def __init__(self, path: Path, provider: str, stamp: str, bench: str, csv_path: Path):
        self.path = path
        self.provider = provider
        self.stamp = stamp
        self.bench = bench          # "sysbench" | "hammerdb"
        self.csv_path = csv_path
        self.rows: list[dict] = []
        self.dropped = 0

    def __repr__(self) -> str:
        return f"<{self.provider} {self.stamp} {self.bench} n={len(self.rows)}>"


def dir_identity(d: Path) -> tuple[str, str]:
    """(provider, UTC stamp) from a results directory name."""
    m = DIRNAME.match(d.name)
    if not m:
        return d.name.split("-")[0].lower(), ""
    return m.group("provider").lower(), m.group("stamp")


def discover(root: Path) -> list[Session]:
    sessions = []
    for d in sorted(p for p in root.iterdir() if p.is_dir()):
        provider, stamp = dir_identity(d)
        for bench, pattern in (("sysbench", "*_sysbench_results.csv"),
                               ("hammerdb", "*_hammerdb_results.csv")):
            for csv_path in sorted(d.glob(pattern)):
                sessions.append(Session(d, provider, stamp, bench, csv_path))
    return sessions


def _num(value: str) -> float | None:
    value = (value or "").strip()
    if value in ("", "NA", "N/A", "nan"):
        return None
    try:
        return float(value)
    except ValueError:
        return None


# ---------------------------------------------------------------- latency ----
# sysbench reports its own p95 and the harness asks for it (--percentile=95),
# so the CSV column is taken as given.  It covers the measured window only:
# --report-checkpoints resets the counters at the end of the ramp-up and the
# harness parses the last statistics block.
#
# HammerDB keeps its response times in the xtprof profile, copied next to the
# run log.  Each file ends with a SUMMARY block that HammerDB merges across the
# active virtual users, so the p95 there is a real percentile over every call -
# not an average of per-user percentiles, which cannot be combined.  NEWORD is
# the new-order transaction, the one TPC-C response time that is quoted.
# Unlike NOPM it is *not* windowed: xtprof is never reset, so the figure covers
# the ramp-up as well as the measured 20 minutes.
XTPROF_NAME = re.compile(r"_run(?P<run>\d+)_w(?P<dataset>\d+)_vu(?P<conc>\d+)"
                         r"_hdbxtprofile\.log$")
# Non-greedy and barred from crossing the next ">>>>>" marker, so a NEWORD
# block without a percentile line cannot borrow the next procedure's.  MIN is
# taken along with P95: the fastest call a run ever managed is the floor under
# every other one, and a floor that high is a fixed cost per call rather than
# the database under load - see floor_note().
XTPROF_CALL = re.compile(r"PROC: NEWORD\n(?:(?!>>>>>).)*?MIN: ([\d.]+)ms"
                         r"(?:(?!>>>>>).)*?P95: ([\d.]+)ms", re.S)


def hammerdb_times(session: Session) -> dict[tuple[int, int, int], tuple[float, float]]:
    """New-Order (p95, fastest call) in ms per (run, warehouses, virtual users)."""
    out: dict[tuple[int, int, int], tuple[float, float]] = {}
    for log in sorted((session.path / "hammerdb").glob("*_hdbxtprofile.log")):
        name = XTPROF_NAME.search(log.name)
        if not name:
            continue
        # The last SUMMARY block is the one merged over all the virtual users;
        # everything before it is a single user's own profile.
        tail = log.read_text(errors="replace").rpartition(">>>>> SUMMARY OF ")[2]
        hit = XTPROF_CALL.search(tail)
        if hit:
            out[tuple(int(name[k]) for k in ("run", "dataset", "conc"))] = \
                (float(hit[2]), float(hit[1]))
    return out


def load(session: Session) -> None:
    """Read the session CSV into rows of {dataset, conc, metric, p95, run}."""
    # sysbench: provider,benchmark,run,tables,rows_per_table,threads,
    #           elapsed_s,transactions,tps,queries,qps,lat_*,errors
    # hammerdb: provider,benchmark,run,warehouses,vu,nopm,tpm,status,log_errors
    key = ("rows_per_table", "threads", "tps") if session.bench == "sysbench" \
        else ("warehouses", "vu", "nopm")
    lat = {} if session.bench == "sysbench" else hammerdb_times(session)
    with session.csv_path.open(newline="") as fh:
        for rec in csv.DictReader(fh):
            dataset, conc, metric = (_num(rec.get(k, "")) for k in key)
            if None in (dataset, conc, metric) or metric <= 0:
                session.dropped += 1
                continue
            dataset, conc = int(dataset), int(conc)
            run = int(_num(rec.get("run", "0")) or 0)
            # A row can have throughput and no latency (an older CSV, a missing
            # profile).  That is not a reason to drop the throughput, so the
            # latency is carried as None and counted where it is aggregated.
            if session.bench == "sysbench":
                p95 = _num(rec.get("lat_p95_ms", ""))
                floor = _num(rec.get("lat_min_ms", ""))
            else:
                p95, floor = lat.get((run, dataset, conc), (None, None))
            session.rows.append(dict(
                dataset=dataset, conc=conc, metric=metric,
                p95=p95 if (p95 or 0) > 0 else None,
                floor=floor if (floor or 0) > 0 else None, run=run,
                provider=rec.get("provider", session.provider).strip() or session.provider,
            ))


# ------------------------------------------------------------- host work ----
# Cost efficiency is one measure for the whole host rather than a property of
# either benchmark, and it covers the measured runs only: what the server
# committed while a benchmark was running, over the hours those runs took.  The
# server's own counters are what make that countable across both benchmarks at
# once, so the totals come from the status captures the harness took, not from
# the result CSVs:
#
#   <provider>_status_baseline.tsv                    when the session started
#   <provider>_status_<bench>_r<n>_w<ds>_vu<c>_before.tsv   just before one run
#   <provider>_status_<bench>_r<n>_w<ds>_vu<c>_after.tsv    just after it
#   <provider>_status_final.tsv                       when the session finished
#
# Before-to-after is therefore the counted part, and the rest of what the
# captures see - the schema builds and data loads between the runs, and the idle
# time - is reported beside it but priced into neither half.
#
# A transaction here is a commit or a rollback.  Each capture is a wide
# variable/value table, and the counter names say which engine wrote it:
# PostgreSQL keeps them per database in pg_stat_database (db.tpcc.xact_commit),
# MySQL one flat set for the whole server (Com_commit).  Both are read, because
# the same report has been run against both.
#
# The catch, and the reason the walk below is not a plain end-minus-start: the
# harness drops and reloads the benchmark database before every measured run, so
# PostgreSQL's per-database counters reset 15 or 30 times inside one session and
# a baseline-to-final subtraction loses everything but the last run.  A counter
# that moves backwards has therefore not gone negative - it has restarted from
# zero, and its new value is all work done since.
ENGINE_KEYS = {
    "postgres": dict(txn=("xact_commit", "xact_rollback"), rows=("tup_inserted",)),
    "mysql": dict(txn=("Com_commit", "Com_rollback"), rows=("Innodb_rows_inserted",)),
}
# One iteration of the sweep is a (benchmark, run, data-set size, concurrency),
# and every artefact the harness writes is named after it - but the two
# benchmarks spell the same four things differently:
#
#   hammerdb   _r1_w160_vu4   160 warehouses, 4 virtual users
#   sysbench   _r1_s40M_t16   40M rows per table, 16 threads
#
# so the names are normalised to one key below.  That is what lets a status
# capture, a sample file, a result row and a benchmark log be recognised as
# records of the same iteration - and therefore lets a failed iteration be
# dropped from all of them at once.
ITER_CAPTURE = re.compile(r"_(?P<bench>sysbench|hammerdb)_r(?P<run>\d+)"
                          r"_(?:w(?P<warehouses>\d+)|s(?P<rows>\d+)M)"
                          r"_(?:vu|t)(?P<conc>\d+)")
# The benchmark's own log for one iteration, anchored so that the schema build,
# the data load and the cleanup - which share the suffix - are not mistaken for
# the measured run itself.
ITER_LOG = re.compile(r"^[^_]+_run(?P<run>\d+)"
                      r"_(?:w(?P<warehouses>\d+)|s(?P<rows>\d+)M)"
                      r"_(?:vu|t)(?P<conc>\d+)\.log$")
# The harness's own verdict, written into <provider>_run.log the moment a
# benchmark came back non-zero.
RUN_WARN = re.compile(r"WARN: (?P<tool>sysbench|hammerdbcli) run (?P<run>\d+) "
                      r"(?:rows=(?P<rows>\d+) t=|wh=(?P<warehouses>\d+) vu=)"
                      r"(?P<conc>\d+) exited non-zero")
CSV_METRIC = {"sysbench": ("rows_per_table", "threads", "tps"),
              "hammerdb": ("warehouses", "vu", "nopm")}
CLOCK_LINE = re.compile(r"^(started|finished)_utc\s+(\S+)", re.M)
ISO_STAMP = re.compile(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")


def iteration_key(m) -> tuple[str, int, int, int] | None:
    """(benchmark, run, data-set size, concurrency) from a name match.

    The data-set size is counted in warehouses for hammerdb and in rows per
    table for sysbench, which the file names write in millions - expanded here,
    so the key matches the rows_per_table the result CSV carries.
    """
    if not m:
        return None
    group = m.groupdict()
    dataset = (int(group["warehouses"]) if group.get("warehouses")
               else int(group["rows"]) * 1_000_000)
    bench = group.get("bench") or ("hammerdb" if group.get("warehouses")
                                   else "sysbench")
    return bench, int(group["run"]), dataset, int(group["conc"])


def read_status(path: Path, column: int = 0) -> dict[str, int]:
    """One status capture as {variable: value}.  The delta files carry three
    value columns (before, after, delta); column picks one."""
    out: dict[str, int] = {}
    for line in path.read_text(errors="replace").splitlines():
        f = line.split("\t")
        if len(f) < 2 + column:
            continue
        try:
            out[f[0]] = int(f[1 + column])
        except ValueError:
            continue
    return out


def counter_keys(capture: dict[str, int], which: str) -> list[str]:
    """The names in one capture that add up to `which` ("txn" or "rows").

    Matched rather than hard-coded, so one reader serves both engines: a
    PostgreSQL capture answers with one key per database, a MySQL capture with
    the server-wide pair.
    """
    tail = ENGINE_KEYS["postgres"][which]
    pg = [k for k in capture if k.startswith("db.") and k.rsplit(".", 1)[-1] in tail]
    if pg:
        return sorted(pg)
    return sorted(k for k in capture if k in ENGINE_KEYS["mysql"][which])


def failed_iterations(path: Path) -> dict[tuple[str, int, int, int], str]:
    """The iterations of one session whose benchmark never produced a result.

    An iteration that failed measured nothing, so neither the transactions the
    host happened to commit during it nor the hours it burned belong in a
    cost-efficiency figure.  Three independent records say that it failed, and
    they agree wherever they overlap, so all three are read and their union is
    excluded:

      * the result CSV, whose row for that iteration has no throughput in it;
      * <provider>_run.log, where the harness wrote "exited non-zero" itself;
      * the benchmark's own log for that iteration, which ends in FATAL.

    HammerDB's log_errors column is deliberately not one of them.  It counts
    benign lines as well - every PostgreSQL run in this tree reports some while
    reporting status=ok and a valid NOPM - so believing it would throw away
    whole campaigns.
    """
    out: dict[tuple[str, int, int, int], str] = {}
    for bench, (ds_col, conc_col, metric_col) in CSV_METRIC.items():
        for csv_path in sorted(path.glob(f"*_{bench}_results.csv")):
            with csv_path.open(newline="") as fh:
                for rec in csv.DictReader(fh):
                    dataset, conc = _num(rec.get(ds_col, "")), _num(rec.get(conc_col, ""))
                    metric = _num(rec.get(metric_col, ""))
                    status = (rec.get("status") or "ok").strip().lower()
                    run = _num(rec.get("run", ""))
                    if None in (dataset, conc, run):
                        continue
                    key = (bench, int(run), int(dataset), int(conc))
                    if metric is None or metric <= 0:
                        out[key] = f"no {metric_col} in {csv_path.name}"
                    elif status not in ("ok", ""):
                        out[key] = f"status={status} in {csv_path.name}"
    for log in sorted(path.glob("*_run.log")):
        for m in RUN_WARN.finditer(log.read_text(errors="replace")):
            key = iteration_key(m)
            if key:
                out.setdefault(key, f"exited non-zero per {log.name}")
    for bench in CSV_METRIC:
        for log in sorted((path / bench).glob("*.log")) if (path / bench).is_dir() else []:
            key = iteration_key(ITER_LOG.match(log.name))
            if key and key not in out and "FATAL" in log.read_text(errors="replace"):
                out[key] = f"FATAL in {log.name}"
    return out


def walk_counters(keys: list[str], start: dict[str, int],
                  pairs: list[tuple[tuple, dict, dict, bool]],
                  end: dict[str, int]) -> tuple[int, int, int, dict[tuple, int]]:
    """Sum a set of counters across a session as (in_run, setup, excluded), plus
    what each measured run contributed to `in_run` on its own.

    Walks the captures in order and treats a drop in any counter as a reset to
    zero rather than as negative work, which is what it is: the database the
    counter belongs to was dropped and rebuilt.  Work inside a measured run is
    before-to-after; everything between runs - the schema builds, the loads and
    the idle time - falls out as the rest.

    A pair flagged not-ok is an iteration whose benchmark failed.  Both of its
    shares - the load that preceded it and whatever ran during it - go to
    `excluded` instead, while the walk still advances past its counters, so the
    work it did is dropped rather than handed to the iteration after it.
    """
    prev = {k: start.get(k, 0) for k in keys}
    in_run = setup = excluded = 0
    per_iter: dict[tuple, int] = {}
    for key, before, after, ok in pairs:
        mine = 0
        for k in keys:
            b, a = before.get(k, 0), after.get(k, 0)
            lead = b if b < prev[k] else b - prev[k]
            during = a if a < b else a - b
            if ok:
                setup += lead
                mine += during
            else:
                excluded += lead + during
            prev[k] = a
        if ok:
            in_run += mine
            per_iter[key] = mine
    for k in keys:
        last = end.get(k, 0)
        setup += last if last < prev[k] else last - prev[k]
    return in_run, setup, excluded, per_iter


def peak_point(peaks: dict[tuple, list[tuple[int, float]]]) -> dict | None:
    """The best point in a set of measured runs: the (benchmark, data size,
    concurrency) whose runs committed the most transactions per hour, the mean
    of that rate, and how many runs stand behind it.

    Mean over the runs at that point rather than the best single run, the way
    every other peak in this report is taken - one lucky run should not set a
    provider's price.  `peaks` maps the point to its runs as (transactions,
    hours); a run the harness never sampled has no rate and no vote.
    """
    best = None
    for key, runs in peaks.items():
        rates = [txn / hours for txn, hours in runs if hours > 0]
        if not rates:
            continue
        rate = sum(rates) / len(rates)
        if best is None or rate > best["rate"]:
            best = dict(rate=rate, key=key, runs=len(rates),
                        txn=sum(t for t, _h in runs) / len(runs),
                        hours=sum(h for _t, h in runs) / len(runs))
    return best


def session_clock(path: Path) -> tuple[str, str, float] | None:
    """(started_utc, finished_utc, hours) for one results directory.

    This span - database init plus benchmark, the idle gaps between phases
    included - is what the hourly rate is charged against, so it is read from
    the harness's own dated stamps.  Not from file mtimes, which this tree
    carries eleven hours off the stamps, and no longer from the server's uptime
    counter, which a managed PostgreSQL capture does not report at all.
    """
    provider, _stamp = dir_identity(path)
    found = {}
    for summary in sorted(path.glob("*_summary.txt")):
        found.update({k: v for k, v in CLOCK_LINE.findall(summary.read_text(errors="replace"))})
    if "started" not in found or "finished" not in found:
        # No summary: fall back to the 10-second status samples, which carry a
        # full ISO stamp per row and bracket the measured runs.
        seen = []
        for f in sorted((path / "server").glob("*_samples_*.tsv")) if (path / "server").is_dir() else []:
            for line in f.read_text(errors="replace").splitlines():
                ts = line.split("\t")[0]
                if ISO_STAMP.match(ts):
                    seen.append(ts)
        if not seen:
            return None
        found = {"started": min(seen), "finished": max(seen)}
    try:
        t0 = datetime.fromisoformat(found["started"].replace("Z", "+00:00"))
        t1 = datetime.fromisoformat(found["finished"].replace("Z", "+00:00"))
    except ValueError:
        return None
    return found["started"], found["finished"], (t1 - t0).total_seconds() / 3600.0


def iteration_spans(srv: Path) -> dict[tuple[str, int, int, int], tuple[str, str]]:
    """(first sample, last sample) per iteration, from the 10-second samples.

    The samples are the only artefact that timestamps an iteration, and the
    harness writes one file per iteration - a failed one included, which gets a
    single row.  Their stamps also put the iterations in the order they ran,
    which the keys alone would not: two benchmarks interleave in one session and
    vu4 sorts after vu32.
    """
    out: dict[tuple[str, int, int, int], tuple[str, str]] = {}
    for f in sorted(srv.glob("*_samples_*.tsv")) if srv.is_dir() else []:
        key = iteration_key(ITER_CAPTURE.search(f.name))
        if not key:
            continue
        seen = [ts for ts in (line.split("\t")[0] for line
                              in f.read_text(errors="replace").splitlines())
                if ISO_STAMP.match(ts)]
        if seen:
            out[key] = (min(seen), max(seen))
    return out


def host_session(path: Path) -> dict | None:
    """What the host did over one results directory, and how long the harness
    took doing it, or None if the directory is not a session at all.

    Only the measured benchmark runs are counted, and each is measured on its
    own: the transactions committed while it was running, over the hours it ran.
    That per-run throughput is what the price is quoted against - the provider's
    best point sets its cost efficiency.  Schema initialisation - the drop, the
    rebuild and the data load the harness does before every run - and the idle
    time around it are reported but neither timed nor counted, and an iteration
    whose benchmark failed is dropped from all of it.
    """
    provider, stamp = dir_identity(path)
    srv = path / "server"
    base = sorted(srv.glob("*_status_baseline.tsv")) if srv.is_dir() else []
    final = sorted(srv.glob("*_status_final.tsv")) if srv.is_dir() else []
    head = dict(path=path, name=path.name, provider=provider, stamp=stamp)
    if not base or not final:
        return dict(head, ok=False, why="no start/end counter capture")
    start, end = read_status(base[0]), read_status(final[0])

    failed = failed_iterations(path)
    spans = iteration_spans(srv)
    # Pair the per-run captures and put them in the order they were taken -
    # chronologically, by the samples that ran alongside them, with anything
    # unsampled falling back to the key order at the end.
    order: dict[tuple, dict[str, Path]] = defaultdict(dict)
    for when in ("before", "after"):
        for f in srv.glob(f"*_status_*_{when}.tsv"):
            key = iteration_key(ITER_CAPTURE.search(f.name))
            if key:
                order[key][when] = f
    sequence = sorted(order, key=lambda k: (spans.get(k, ("9",))[-1], k))
    pairs = [(k, read_status(order[k]["before"]), read_status(order[k]["after"]),
              k not in failed)
             for k in sequence if {"before", "after"} <= set(order[k])]

    txn_keys = counter_keys(end or start, "txn")
    if not txn_keys:
        return dict(head, ok=False, why="no transaction counters in the captures")
    clock = session_clock(path)
    if clock is None or clock[2] <= 0:
        return dict(head, ok=False, why="no start/end timestamp for the session")
    started, finished, wall = clock

    # The billed hours are the measured runs and nothing else.  Each iteration
    # carries its own clock: the window the harness sampled the server over
    # while the benchmark ran, first sample to last, which is the ramp-up plus
    # the measured interval and is accurate to the ten-second sampling period.
    # The schema build and data load that precede a run are not sampled, and are
    # therefore not in it.
    def span_hours(key: tuple) -> float:
        first, last = spans.get(key, (None, None))
        if not first or not last:
            return 0.0
        return (datetime.fromisoformat(last.replace("Z", "+00:00"))
                - datetime.fromisoformat(first.replace("Z", "+00:00"))
                ).total_seconds() / 3600.0

    run_hours = sum(span_hours(k) for k in sequence if k not in failed)
    # A failed run has next to nothing to sample, so this is near zero - but it
    # is kept apart rather than folded into either of the others.
    failed_hours = sum(span_hours(k) for k in sequence if k in failed)
    # Everything else the instance was up for: dropping and rebuilding the
    # schema before every run, loading it, and waiting in between.
    init_hours = max(wall - run_hours - failed_hours, 0.0)

    in_run, setup, excl, per_iter = walk_counters(txn_keys, start, pairs, end)
    rows, rows_setup, rows_excl, _each = walk_counters(
        counter_keys(end or start, "rows"), start, pairs, end)
    # Every counted run as (transactions, hours), grouped by the point it
    # measured - the run index drops out, because runs of one point are repeats
    # of the same measurement and are averaged.
    peaks: dict[tuple, list[tuple[int, float]]] = defaultdict(list)
    for key, txn in per_iter.items():
        bench, _run, dataset, conc = key
        peaks[(bench, dataset, conc)].append((txn, span_hours(key)))
    benches = sorted(b for b, pat in (("sysbench", "*_sysbench_results.csv"),
                                      ("hammerdb", "*_hammerdb_results.csv"))
                     if any(path.glob(pat)))
    return dict(head, ok=True, why="", benches=benches,
                runs=sum(1 for *_x, ok in pairs if ok),
                # Per-point throughput, which is what the price is quoted
                # against: the peak is taken from these.
                peaks=dict(peaks),
                # The counted pair: committed inside the measured runs, over the
                # hours those runs took.
                txn=in_run, hours=run_hours, rows=rows,
                # Seen by the counters but outside any measured run - the schema
                # builds and data loads, plus idle time - and so counted in
                # neither half.
                outside=setup, outside_rows=rows_setup, hours_init=init_hours,
                # Dropped outright: the iterations whose benchmark failed.
                failed_runs=sum(1 for *_x, ok in pairs if not ok),
                failed_keys=[(k, failed[k]) for k in sequence if k in failed],
                failed_txn=excl, failed_rows=rows_excl,
                failed_hours=failed_hours,
                started=started, finished=finished, hours_wall=wall)


class HostWork:
    """What the hosts committed and how long they took doing it, per provider -
    run by run, so a provider can be priced at the best rate it reached."""

    def __init__(self, root: Path):
        self.sessions: list[dict] = []
        self.skipped: list[dict] = []
        for d in sorted(p for p in root.iterdir() if p.is_dir()):
            rec = host_session(d)
            if rec is None:
                continue
            (self.sessions if rec["ok"] else self.skipped).append(rec)
        self.providers = sorted({r["provider"] for r in self.sessions})

    def of(self, provider: str) -> list[dict]:
        return [r for r in self.sessions if r["provider"] == provider]

    def total(self, provider: str) -> dict:
        """The provider's whole campaign as one record."""
        rows = self.of(provider)
        out = {k: sum(r[k] for r in rows) for k in
               ("txn", "outside", "runs", "hours", "rows", "outside_rows",
                "hours_init", "hours_wall", "failed_runs", "failed_txn",
                "failed_hours")}
        out["sessions"] = len(rows)
        out["benches"] = sorted({b for r in rows for b in r["benches"]})
        # The cost of a campaign is the cost of its sessions, because both are
        # the same rate times the same hours - so one multiplication serves.
        out["usd"] = cost_usd(provider, out["hours"])
        out["rate"] = rate_usd_hour(provider)
        # Repeats of the same point taken in different sessions are repeats all
        # the same, so they average together.
        peaks: dict[tuple, list[tuple[int, float]]] = defaultdict(list)
        for r in rows:
            for key, runs in r["peaks"].items():
                peaks[key].extend(runs)
        out["peak"] = peak_point(peaks)
        return out

    def per_usd(self, provider: str) -> float | None:
        """Transactions per USD at the provider's peak throughput: an hour of
        running at its best measured point, over what that hour costs.

        The hours cancel out of the quotient, which is the point - a provider is
        priced on how fast it can go, not on how long this campaign happened to
        keep it running.  None if either side is missing: a provider without a
        price, or without a sampled run to take a rate from, gets no bar rather
        than a zero.
        """
        total = self.total(provider)
        peak, rate = total["peak"], total["rate"]
        return None if not peak or not rate else peak["rate"] / rate


# --------------------------------------------------------------- aggregation --
class Agg:
    """mean / min / max of a metric per (provider, dataset, concurrency), plus
    the individual run values behind each of those means."""

    def __init__(self, bench: str, sessions: list[Session], field: str = "metric"):
        self.bench = bench
        self.sessions = sessions
        self.field = field
        self.missing = 0            # rows that carried no value for this field
        bucket: dict[tuple[str, int, int], list[float]] = defaultdict(list)
        runs: dict[tuple[str, int, int], list[tuple[int, float]]] = defaultdict(list)
        for s in sessions:
            for r in s.rows:
                value = r.get(field)
                if value is None:
                    self.missing += 1
                    continue
                bucket[(s.provider, r["dataset"], r["conc"])].append(value)
                runs[(s.provider, r["dataset"], r["conc"])].append((r["run"], value))
        self.stats = {k: (float(np.mean(v)), float(np.min(v)), float(np.max(v)), len(v))
                      for k, v in bucket.items()}
        # Kept in run order so "individual runs" is a stable series, not a set.
        self.runs = {k: sorted(v) for k, v in runs.items()}
        self.providers = sorted({k[0] for k in self.stats})
        self.datasets = sorted({k[1] for k in self.stats})
        self.concurrency = sorted({k[2] for k in self.stats})

    def mean(self, provider: str, dataset: int, conc: int) -> float | None:
        hit = self.stats.get((provider, dataset, conc))
        return hit[0] if hit else None

    def peak(self, provider: str, dataset: int):
        """Best mean over the concurrency sweep, with the level that reached it."""
        best = (None, None)
        for conc in self.concurrency:
            value = self.mean(provider, dataset, conc)
            if value is not None and (best[0] is None or value > best[0]):
                best = (value, conc)
        return best

    def at_top(self, provider: str, dataset: int):
        """The value at the highest concurrency level this provider reached, with
        that level.  For latency the interesting figure is the loaded one, and
        not every provider got to the same top of the sweep."""
        for conc in reversed(self.concurrency):
            value = self.mean(provider, dataset, conc)
            if value is not None:
                return value, conc
        return None, None

    def incomplete(self) -> list[tuple[str, int, list[int]]]:
        """Provider/data-set pairs that did not cover the whole sweep - their
        'peak' is a peak over fewer levels, so it is not directly comparable."""
        gaps = []
        for provider in self.providers:
            for dataset in self.datasets:
                have = [c for c in self.concurrency
                        if self.mean(provider, dataset, c) is not None]
                if have and len(have) < len(self.concurrency):
                    gaps.append((provider, dataset, have))
        return gaps


# ------------------------------------------------------------------ plumbing --
def label_dataset(bench: str, value: int) -> str:
    if bench == "sysbench":
        return f"{value // 1_000_000}M rows/table" if value >= 1_000_000 else f"{value:,} rows/table"
    return f"{value} warehouses"


def peak_short(key: tuple[str, int, int]) -> str:
    """The same point in the space a chart label has: HammerDB 160w vu32."""
    bench, dataset, conc = key
    size = f"{dataset // 1_000_000}M" if bench == "sysbench" else f"{dataset}w"
    return (f"{'HammerDB' if bench == 'hammerdb' else bench} "
            f"{size} {'vu' if bench == 'hammerdb' else 't'}{conc}")


def peak_label(key: tuple[str, int, int]) -> str:
    """The point a peak was measured at: sysbench 40M rows/table t32."""
    bench, dataset, conc = key
    return (f"{'HammerDB' if bench == 'hammerdb' else bench} "
            f"{label_dataset(bench, dataset)} "
            f"{'vu' if bench == 'hammerdb' else 't'}{conc}")


def compact(v: float) -> str:
    """A large count at a glance: 43.2M, 431k, 86."""
    for scale, suffix in ((1e9, "B"), (1e6, "M"), (1e3, "k")):
        if abs(v) >= scale:
            q = v / scale
            return f"{q:,.1f}{suffix}" if abs(q) < 100 else f"{q:,.0f}{suffix}"
    return f"{v:,.0f}"


def slug_dataset(bench: str, value: int) -> str:
    """The data-set size as a filename fragment: 10m / 160w."""
    if bench == "sysbench":
        return f"{value // 1_000_000}m" if value >= 1_000_000 else f"{value}rows"
    return f"{value}w"


def pretty(provider: str) -> str:
    return {"digitalocean": "DigitalOcean", "exoscale": "Exoscale", "ovhcloud": "OVHcloud",
            "upcloud": "UpCloud", "vultr": "Vultr"}.get(provider, provider.title())


def style(mode: str) -> dict:
    t = THEME[mode]
    plt.rcParams.update({
        "font.family": "sans-serif", "font.sans-serif": FONT, "font.size": 10,
        "figure.facecolor": t["plane"], "axes.facecolor": t["surface"],
        "savefig.facecolor": t["plane"],
        "text.color": t["primary"], "axes.labelcolor": t["secondary"],
        "xtick.color": t["muted"], "ytick.color": t["muted"],
        "xtick.labelcolor": t["muted"], "ytick.labelcolor": t["muted"],
        "axes.edgecolor": t["axis"], "axes.linewidth": 1.0,
        "xtick.major.size": 0, "ytick.major.size": 0,
        "legend.frameon": False, "figure.dpi": 110, "savefig.dpi": 160,
    })
    return t


def chrome(ax, t: dict, label: str = "", orient: str = "v") -> None:
    """Recessive axes: a hairline solid grid along the value axis only, no
    top/right spines.  orient="h" for bars that run sideways."""
    value_axis = "y" if orient == "v" else "x"
    ax.set_axisbelow(True)
    ax.grid(axis=value_axis, color=t["grid"], linewidth=1.0, linestyle="-")
    ax.grid(axis="x" if value_axis == "y" else "y", visible=False)
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)
    for side in ("left", "bottom"):
        ax.spines[side].set_color(t["axis"])
    getattr(ax, f"{value_axis}axis").set_major_formatter(lambda v, _pos: f"{v:,.0f}")
    if label and orient == "v":
        ax.set_ylabel(label, color=t["secondary"], labelpad=8)
    elif label:
        ax.set_xlabel(label, color=t["secondary"], labelpad=8)


def px_to_data(ax, px: float, axis: str = "x") -> float:
    """A display-space length in data units. Only valid once limits are set."""
    inv = ax.transData.inverted()
    o = ax.transData.transform((0, 0))
    if axis == "x":
        return abs(inv.transform((o[0] + px, o[1]))[0] - inv.transform(o)[0])
    return abs(inv.transform((o[0], o[1] + px))[1] - inv.transform(o)[1])


def rounded_barh(ax, y: float, height: float, length: float, color: str,
                 radius_px: float = 4.0):
    """The same bar on its side: 4px rounded data-end, square at the baseline."""
    if length <= 0:
        return
    rx = min(px_to_data(ax, radius_px, "x"), length)
    ry = min(px_to_data(ax, radius_px, "y"), height / 2)
    bottom, top, end = y - height / 2, y + height / 2, length
    k = 0.5523
    verts = [
        (0, bottom), (end - rx, bottom),
        (end - rx + rx * k, bottom), (end, bottom + ry - ry * k), (end, bottom + ry),
        (end, top - ry),
        (end, top - ry + ry * k), (end - rx + rx * k, top), (end - rx, top),
        (0, top), (0, bottom),
    ]
    codes = [MPath.MOVETO, MPath.LINETO,
             MPath.CURVE4, MPath.CURVE4, MPath.CURVE4,
             MPath.LINETO,
             MPath.CURVE4, MPath.CURVE4, MPath.CURVE4,
             MPath.LINETO, MPath.CLOSEPOLY]
    patch = PathPatch(MPath(verts, codes), facecolor=color, edgecolor="none", zorder=3)
    ax.add_patch(patch)
    return patch


def figure_legend(fig, providers: list[str], colors: list[str], t: dict,
                  y: float = 0.0, ncol: int | None = None) -> None:
    """A legend is always present for two or more series."""
    handles = [Line2D([], [], color=colors[i], marker=MARKERS[i], linewidth=2,
                      markersize=6.5, markeredgecolor=t["surface"], markeredgewidth=1.6,
                      label=pretty(p))
               for i, p in enumerate(providers)]
    leg = fig.legend(handles=handles, loc="lower center", ncol=ncol or min(len(providers), 6),
                     bbox_to_anchor=(0.5, y), handletextpad=0.5, columnspacing=1.6)
    for text in leg.get_texts():
        text.set_color(t["secondary"])


def titles(fig, title: str, subtitle: str, t: dict) -> None:
    fig.text(0.012, 0.975, title, ha="left", va="top", fontsize=14.5,
             fontweight="semibold", color=t["primary"])
    fig.text(0.012, 0.925, subtitle, ha="left", va="top", fontsize=10,
             color=t["secondary"])


# -------------------------------------------------------------------- charts --
def chart_scaling(agg: Agg, mode: str, out: Path, metric: str, conc_label: str,
                  title: str, subtitle: str, dataset: int,
                  logy: bool = False) -> Path:
    """A metric against the concurrency sweep, for one data-set size.

    logy is there for a metric that spans decades, which p95 does across these
    providers; the latency charts are drawn linear all the same, on the same
    kind of axis as everything else in the report.

    The y-axis is fit to this size's own data: a size whose worst point is a
    quarter of another's would otherwise be a flat line on the floor
    (latency), or leave most of the panel empty (throughput).
    """
    t = style(mode)
    colors = SERIES[mode]
    fig, ax = plt.subplots(figsize=(10.3, 5.4))
    xs = np.arange(len(agg.concurrency))
    scope = [v[0] for k, v in agg.stats.items() if k[1] == dataset]
    ymax = max(scope, default=1.0)
    ymin = min((v for v in scope if v > 0), default=1.0)

    chrome(ax, t, metric)
    for i, provider in enumerate(agg.providers):
        means = [agg.mean(provider, dataset, c) for c in agg.concurrency]
        keep = [j for j, v in enumerate(means) if v is not None]
        if not keep:
            continue
        kx = xs[keep]
        ax.plot(kx, [means[j] for j in keep], color=colors[i], linewidth=2,
                solid_capstyle="round", solid_joinstyle="round",
                marker=MARKERS[i], markersize=6.5, markeredgecolor=t["surface"],
                markeredgewidth=1.6, zorder=4 + i)
    ax.set_xticks(xs, [str(c) for c in agg.concurrency])
    ax.tick_params(axis="x", labelcolor=t["secondary"])
    ax.set_xlim(-0.35, len(xs) - 0.65)
    if logy:
        # 1-2-5 per decade: enough gridlines to read a value off, few enough to
        # stay recessive.  A log axis cannot start at zero, so it is said in the
        # axis label rather than left for the reader to infer from the ticks.
        ax.set_yscale("log")
        ax.set_ylim(ymin / 1.4, ymax * 1.4)
        ax.yaxis.set_major_locator(mticker.LogLocator(base=10, subs=(1, 2, 5), numticks=16))
        ax.yaxis.set_minor_locator(mticker.NullLocator())
        ax.yaxis.set_major_formatter(lambda v, _pos: f"{v:,.6g}")
    else:
        ax.set_ylim(0, ymax * 1.08)
    ax.set_xlabel(conc_label, color=t["secondary"], labelpad=6)
    ax.set_title(label_dataset(agg.bench, dataset), fontsize=11,
                 color=t["primary"], pad=10)

    titles(fig, title, subtitle, t)
    figure_legend(fig, agg.providers, colors, t)
    fig.tight_layout(rect=(0.0, 0.07, 1.0, 0.88))
    fig.savefig(out)
    plt.close(fig)
    return out


def chart_peak(agg: Agg, mode: str, out: Path, metric: str, conc_short: str,
               title: str, subtitle: str, dataset: int) -> Path:
    """Peak throughput per provider at one data-set size, one bar per provider.

    A ranking of named categories, so the bars run sideways with the names on
    the axis and no legend - the same figure the HTML page draws, so a reader
    who has seen one recognises the other.  Every bar carries its value, the
    level that reached it and the spread of the runs behind it.
    """
    t = style(mode)
    colors = SERIES[mode]
    # Hue follows the provider, never the rank, so sorting cannot repaint anyone.
    slot = {p: colors[i % len(colors)] for i, p in enumerate(agg.providers)}
    rows = sorted(((agg.peak(p, dataset), p) for p in agg.providers),
                  key=lambda r: (r[0][0] is not None, r[0][0] or 0), reverse=True)
    # Only the gaps that apply to the size this file is about.
    gaps = [g for g in agg.incomplete() if g[1] == dataset]

    fig, ax = plt.subplots(figsize=(8.6, 0.66 * len(rows) + (3.0 if gaps else 2.3)))
    chrome(ax, t, metric, orient="h")

    # Scaled to the largest bar across *all* sizes, not just this one, so the
    # three files of a benchmark stay comparable bar-for-bar.
    xmax = max((agg.peak(p, d)[0] or 0.0
                for p in agg.providers for d in agg.datasets), default=1.0)
    ax.set_yticks(range(len(rows)), [pretty(p) for _v, p in rows])
    # The pad clears the colour key that goes in beside each name below.
    ax.tick_params(axis="y", labelcolor=t["secondary"], labelsize=10, pad=16)
    ax.set_ylim(len(rows) - 0.5, -0.5)
    # Provisional; the real headroom is measured off the labels once the axes
    # box exists, because what has to fit is text, not a fraction of the panel.
    ax.set_xlim(0, xmax * 1.30)

    # A peak taken over a partial sweep is not comparable with a full one.  Say
    # it rather than letting the bar imply otherwise.
    note = "Incomplete sweep, peak taken over fewer levels: " + "; ".join(
        f"{pretty(p)} {label_dataset(agg.bench, d)} ({conc_short}="
        f"{', '.join(str(c) for c in have)} only)" for p, d, have in gaps)
    # Wrapped to the figure, not run off its right edge: a footnote that is cut
    # in half is worse than no footnote, because it reads as complete.
    lines = textwrap.wrap(note, width=int((fig.get_figwidth() - 0.3) / 0.0666)) \
        if gaps else []
    foot = 0.012 + 0.0175 * len(lines)

    # Bar geometry is sized in display pixels, so the final axes box has to be
    # settled first - both the limits and the layout feed that transform.
    titles(fig, title, subtitle, t)
    fig.tight_layout(rect=(0.0, foot + 0.06, 1.0, 0.87))
    if lines:
        fig.text(0.012, 0.010, "\n".join(lines), ha="left", va="bottom",
                 fontsize=8, color=t["muted"], linespacing=1.5)
    fig.canvas.draw()

    detail = {}
    for (_v, at), provider in rows:
        line = f"{conc_short}={at}"
        cell = agg.stats.get((provider, dataset, at))
        if cell:
            _mean, lo, hi, runs = cell
            line += f"  ·  {lo:,.0f}–{hi:,.0f} / n={runs}"
        detail[provider] = line

    def text_px(s: str, size: float, weight: str = "normal") -> float:
        probe = ax.text(0, 0, s, fontsize=size, fontweight=weight)
        width = probe.get_window_extent(renderer=fig.canvas.get_renderer()).width
        probe.remove()
        return width

    # The value and its detail line hang off the bar tip, so the panel has to
    # hold the widest of them.  A fixed multiplier only guesses: it clipped the
    # longest run range ("58,217-59,077 / n=3") and would waste a third of the
    # panel when every range is short.  Reserve what the text actually needs.
    widest = max(text_px("no data", 9) if value is None
                 else max(text_px(f"{value:,.0f}", 10, "semibold"),
                          text_px(detail[provider], 8))
                 for (value, _at), provider in rows)
    panel = ax.get_window_extent().width
    ax.set_xlim(0, xmax * panel / max(panel - (12.0 + widest + 6.0), 40.0))
    fig.canvas.draw()

    bar_h = min(0.62, px_to_data(ax, 24.0, "y"))
    sw = px_to_data(ax, 9.0)                       # the 9px key beside the name
    halo = [pe.withStroke(linewidth=2.5, foreground=t["surface"])]
    for i, ((value, at), provider) in enumerate(rows):
        # Identity comes from a key beside the name, never from colouring the
        # text itself - the same arrangement as the HTML.
        ax.add_patch(Rectangle((-sw, i - px_to_data(ax, 4.5, "y")),
                               sw, px_to_data(ax, 9.0, "y"),
                               facecolor=slot[provider], edgecolor="none",
                               clip_on=False, zorder=4))
        if value is None:
            ax.text(px_to_data(ax, 12.0), i, "no data", va="center",
                    fontsize=9, color=t["muted"])
            continue
        rounded_barh(ax, i, bar_h, value, slot[provider])
        # Bars -> value at the tip, outside the bar end so it is never clipped.
        # The labels sit out in the gridded field, so each carries a 2px surface
        # halo - otherwise a gridline runs straight through the digits.
        tx = value + px_to_data(ax, 12.0)
        ax.text(tx, i - px_to_data(ax, 5.0, "y"), f"{value:,.0f}", va="center",
                fontsize=10, fontweight="semibold", color=t["primary"],
                path_effects=halo)
        ax.text(tx, i + px_to_data(ax, 8.0, "y"), detail[provider], va="center",
                fontsize=8, color=t["muted"], path_effects=halo)

    fig.savefig(out)
    plt.close(fig)
    return out


def chart_cost(work: HostWork, mode: str, out: Path, title: str,
               subtitle: str) -> Path:
    """Transactions per USD at peak throughput, one bar per provider - an hour
    of each host running at the best point it measured, over what that hour
    costs.

    A ranking of named categories, so the bars run sideways with the names on
    the axis: the order is the message, and no label has to be rotated.
    """
    t = style(mode)
    colors = SERIES[mode]
    # Hue follows the provider, never the rank, so the ranking below cannot
    # repaint anyone.
    slot = {p: colors[i % len(colors)] for i, p in enumerate(work.providers)}
    rows = sorted(((work.per_usd(p), p) for p in work.providers),
                  key=lambda r: (r[0] is not None, r[0] or 0), reverse=True)

    fig, ax = plt.subplots(figsize=(8.6, 0.72 * len(rows) + 3.0))
    chrome(ax, t, "Transactions committed per USD at peak throughput", orient="h")
    # Counts run to the millions; full digits on every tick would be a wall of
    # zeroes.  The exact figures are in the table and the CSV.
    ax.xaxis.set_major_formatter(lambda v, _pos: compact(v))
    ax.set_yticks(range(len(rows)), [pretty(p) for _v, p in rows])
    ax.tick_params(axis="y", labelcolor=t["secondary"], labelsize=10)
    ax.set_ylim(len(rows) - 0.5, -0.5)
    xmax = max((v or 0.0 for v, _p in rows), default=1.0)
    # Provisional: the real headroom is measured off the labels below, once the
    # axes box exists, because what has to fit there is text.
    ax.set_xlim(0, xmax * 1.38)

    note = (
        "From the server's own counters (commits + rollbacks), taken just before "
        "and just after each measured run, so ramp-ups count and nothing between "
        "the runs does. Each run is read as a rate - what it committed over the "
        "hours it took - and the runs of one (benchmark, data size, concurrency) "
        "point are averaged, so no single lucky run sets a price. The best point "
        "a provider reached is the bar: an hour at that rate, over what the hour "
        "costs at its rate. The campaign's own length cancels out of that, which "
        "is the intent - this prices how fast a host can go, not how long these "
        "runs happened to keep it switched on. The schema builds, data loads and "
        "idle time between the runs are in neither half."
    )
    # Only worth saying where something did fail; on a clean campaign it would
    # be a sentence about nothing.
    dropped = sum(work.total(p)["failed_runs"] for p in work.providers)
    if dropped:
        note += (
            f" {dropped} iteration{'' if dropped == 1 else 's'} whose benchmark "
            f"failed to run {'is' if dropped == 1 else 'are'} dropped outright, "
            "and cannot stand as anyone's peak."
        )
    lines = textwrap.wrap(note, width=int((fig.get_figwidth() - 0.3) / 0.0666))
    foot = 0.012 + 0.0145 * len(lines)

    titles(fig, title, subtitle, t)
    fig.tight_layout(rect=(0.0, foot + 0.05, 1.0, 0.86))
    fig.text(0.012, 0.010, "\n".join(lines), ha="left", va="bottom",
             fontsize=8, color=t["muted"], linespacing=1.5)
    fig.canvas.draw()

    # The quotient's inputs, so the bar is never an unexplained ratio: the rate
    # the peak ran at, the point it was measured at and how many runs that is,
    # and the hourly price it is divided by.
    detail = {}
    for _v, provider in rows:
        total = work.total(provider)
        peak = total["peak"]
        if not peak:
            continue
        detail[provider] = (
            f"{compact(peak['rate'])} txn/h at {peak_short(peak['key'])}"
            f" (n={peak['runs']})  ·  {total['rate'] or 0:.3f}/h")

    def text_px(s: str, size: float, weight: str = "normal") -> float:
        probe = ax.text(0, 0, s, fontsize=size, fontweight=weight)
        width = probe.get_window_extent(renderer=fig.canvas.get_renderer()).width
        probe.remove()
        return width

    # The value and its detail line hang off the bar tip, so the panel has to
    # hold the widest of them.  Measured rather than guessed at a multiplier,
    # because the detail line now carries an hourly rate as well.
    widest = max(text_px("not priced", 9) if value is None
                 else max(text_px(compact(value), 10, "semibold"),
                          text_px(detail[provider], 8))
                 for value, provider in rows)
    panel = ax.get_window_extent().width
    ax.set_xlim(0, xmax * panel / max(panel - (12.0 + widest + 6.0), 40.0))
    fig.canvas.draw()

    bar_h = min(0.62, px_to_data(ax, 24.0, "y"))
    for i, (value, provider) in enumerate(rows):
        if value is None:
            ax.text(px_to_data(ax, 12.0), i, "not priced", va="center",
                    fontsize=9, color=t["muted"])
            continue
        rounded_barh(ax, i, bar_h, value, slot[provider])
        tx = value + px_to_data(ax, 12.0)
        # The labels sit out in the gridded field, so each carries a 2px surface
        # halo - otherwise a gridline runs straight through the digits.
        halo = [pe.withStroke(linewidth=2.5, foreground=t["surface"])]
        ax.text(tx, i - px_to_data(ax, 5.0, "y"), compact(value), va="center",
                fontsize=10, fontweight="semibold", color=t["primary"],
                path_effects=halo)
        ax.text(tx, i + px_to_data(ax, 8.0, "y"), detail[provider],
                va="center", fontsize=8, color=t["muted"], path_effects=halo)

    fig.savefig(out)
    plt.close(fig)
    return out


# --------------------------------------------------------------- table view --
def write_table(agg: Agg, out: Path, dataset_col: str, conc_col: str, metric: str) -> Path:
    with out.open("w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["provider", dataset_col, conc_col, f"{metric}_mean",
                    f"{metric}_min", f"{metric}_max", "runs"])
        for provider in agg.providers:
            for dataset in agg.datasets:
                for conc in agg.concurrency:
                    hit = agg.stats.get((provider, dataset, conc))
                    if not hit:
                        continue
                    mean, lo, hi, cnt = hit
                    w.writerow([provider, dataset, conc, f"{mean:.2f}",
                                f"{lo:.2f}", f"{hi:.2f}", cnt])
    return out


def report_costs(work: HostWork, providers: list[str]) -> None:
    print("\ncost of the benchmarked instance (hourly rate x the hours of "
          "measured benchmark runs)")
    print("  " + f"{'provider':<14}{'USD per hour':>14}{'run hours':>11}"
                 f"{'USD':>10}{'init/idle h':>13}{'wall clock h':>14}")
    for provider in providers:
        rate = rate_usd_hour(provider)
        total = work.total(provider)
        if rate is None:
            print("  " + f"{pretty(provider):<14}{'not priced':>14}")
            continue
        usd = cost_usd(provider, total["hours"])
        print("  " + f"{pretty(provider):<14}{rate:>14.3f}{total['hours']:>11,.2f}"
              + (f"{usd:>10,.2f}" if usd else f"{'-':>10}")
              # Not in the price: the schema builds and loads between the runs,
              # the idle time, and whatever the failed iterations took.
              + f"{total['hours_init'] + total['failed_hours']:>13,.2f}"
              + f"{total['hours_wall']:>14,.2f}")
    counted = sum(work.total(p)["hours"] for p in providers)
    unpriced = sum(work.total(p)["hours_init"] + work.total(p)["failed_hours"]
                   for p in providers)
    if counted:
        print(f"  {counted:,.1f} h of measured runs priced; the other "
              f"{unpriced:,.1f} h the instances were up - schema builds, data "
              f"loads, idle time - are not")


def report_host_efficiency(work: HostWork) -> None:
    """Cost efficiency as the host measured it: an hour at the best throughput
    the database reached, over what that hour costs."""
    print("\ncost efficiency - transactions per USD at each provider's peak throughput")
    print("  (the server's own counters, captured just before and just after each")
    print("   measured run.  Every run becomes a rate - what it committed over the")
    print("   hours it took - and the runs of one benchmark, data size and")
    print("   concurrency are averaged; the best such point is the provider's")
    print("   peak, priced as one hour at that rate.  An iteration whose benchmark")
    print("   failed is no candidate.  The campaign columns on the right are")
    print("   context, not inputs - the length of the campaign cancels out)")
    print("  " + f"{'provider':<14}{'USD/h':>7}{'peak txn/h':>12}"
                 f"  {'peak at':<30}{'n':>3}{'txn per USD':>14}"
                 f"{'all runs':>10}{'run h':>8}{'transactions':>14}")
    ranked = []
    for provider in work.providers:
        total = work.total(provider)
        peak = total["peak"]
        per = work.per_usd(provider)
        if per is not None:
            ranked.append((per, provider, total))
        print("  " + f"{pretty(provider):<14}"
              + (f"{total['rate']:>7.3f}" if total["rate"] else f"{'-':>7}")
              + (f"{compact(peak['rate']):>12}  {peak_label(peak['key']):<30}"
                 f"{peak['runs']:>3}" if peak
                 else f"{'-':>12}  {'no sampled run':<30}{'-':>3}")
              + (f"{compact(per) + ' / USD':>14}" if per is not None
                 else f"{'not priced':>14}")
              # Context, not inputs: the whole campaign behind the peak.
              + f"{total['runs']:>10}{total['hours']:>8,.1f}"
              + f"{compact(total['txn']):>14}")
    if ranked:
        ranked.sort(reverse=True)
        best, worst = ranked[0], ranked[-1]
        print(f"  best: {pretty(best[1])} peaked at "
              f"{compact(best[2]['peak']['rate'])} txn/h on "
              f"{peak_label(best[2]['peak']['key'])} = {compact(best[0])} per "
              f"USD, {best[0] / worst[0]:.1f}x {pretty(worst[1])} "
              f"({compact(worst[0])} per USD)")
    # What was thrown away, and on whose evidence - an iteration silently
    # dropped from both halves of a quotient is worth a line of its own.
    for rec in work.sessions:
        if not rec["failed_keys"]:
            continue
        # An iteration that died on its first sample took no billable time and
        # committed nothing, so say that rather than print two zeroes.
        minutes = rec["failed_hours"] * 60
        print(f"  dropped {len(rec['failed_keys'])} failed iteration(s) from "
              f"{rec['name']}: "
              + (f"{compact(rec['failed_txn'])} transactions and {minutes:,.1f} "
                 "min of run time left uncounted" if minutes >= 0.05
                 else "they never ran long enough to record a rate or to "
                      "commit anything"))
        for key, why in rec["failed_keys"][:3]:
            bench, run, dataset, conc = key
            print(f"    run {run} {label_dataset(bench, dataset)} "
                  f"{'vu' if bench == 'hammerdb' else 't'}{conc} - {why}")
        if len(rec["failed_keys"]) > 3:
            print(f"    ... and {len(rec['failed_keys']) - 3} more")
    for rec in work.skipped:
        print(f"  skipped {rec['name']}: {rec['why']}")


def write_cost_table(work: HostWork, out: Path) -> Path:
    """The peak and its price: one row per session, plus the whole-campaign row
    per provider.  A session's peak is the best point it measured; a campaign's
    is the best point across its sessions, with repeats of a point averaged
    together, so the two can differ and both are worth having.

    The quotient's own columns come first, then the campaign behind it, then
    what was left out: the work the counters saw between the runs, the hours the
    instance spent on it, and the iterations whose benchmark failed."""
    with out.open("w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["provider", "scope", "session", "started_utc", "finished_utc",
                    "benchmarks", "peak_benchmark", "peak_data_size",
                    "peak_concurrency", "peak_runs",
                    "peak_transactions_per_hour", "usd_per_hour",
                    "transactions_per_usd_at_peak",
                    "run_hours", "measured_runs", "transactions_in_runs",
                    "rows_inserted_in_runs", "cost_usd",
                    "transactions_outside_runs", "rows_inserted_outside_runs",
                    "init_and_idle_hours", "wall_clock_hours", "failed_runs",
                    "failed_run_hours", "failed_transactions"])

        def row(provider: str, scope: str, rec: dict, session: str):
            rate = rate_usd_hour(provider)
            usd = cost_usd(provider, rec["hours"])
            # A campaign record carries its peak already; a session is asked for
            # its own, over the points it measured.
            peak = rec.get("peak") or peak_point(rec.get("peaks", {}))
            w.writerow([
                provider, scope, session, rec.get("started", ""),
                rec.get("finished", ""), " + ".join(rec["benches"]),
                peak["key"][0] if peak else "",
                peak["key"][1] if peak else "",
                peak["key"][2] if peak else "",
                peak["runs"] if peak else "",
                f"{peak['rate']:.0f}" if peak else "",
                "" if rate is None else f"{rate:.3f}",
                "" if not (peak and rate) else f"{peak['rate'] / rate:.1f}",
                # The campaign the peak was drawn from.
                f"{rec['hours']:.2f}", rec["runs"], rec["txn"], rec["rows"],
                "" if usd is None else f"{usd:.2f}",
                # Seen by the counters, in neither half.
                rec["outside"], rec["outside_rows"],
                f"{rec['hours_init']:.2f}", f"{rec['hours_wall']:.2f}",
                # Dropped outright.
                rec["failed_runs"], f"{rec['failed_hours']:.2f}",
                rec["failed_txn"],
            ])

        for provider in work.providers:
            for rec in work.of(provider):
                row(provider, "session", rec, rec["name"])
            total = work.total(provider)
            rows = work.of(provider)
            # The campaign's clock runs from the first session's start to the
            # last one's finish; the hours it is billed for are the sessions'
            # own, not that span, because nothing was running in between.
            row(provider, "campaign", dict(
                total, started=min((r["started"] for r in rows), default=""),
                finished=max((r["finished"] for r in rows), default="")), "")
    return out


def report(agg: Agg, metric: str, conc_label: str) -> None:
    print(f"\n{agg.bench}: {metric} peak per provider "
          f"(mean over runs, best {conc_label.lower()})")
    head = "  " + f"{'provider':<14}" + "".join(
        f"{label_dataset(agg.bench, d):>24}" for d in agg.datasets)
    print(head)
    for provider in agg.providers:
        line = "  " + f"{pretty(provider):<14}"
        for dataset in agg.datasets:
            value, at = agg.peak(provider, dataset)
            line += f"{(f'{value:,.0f} @ {at}' if value else 'no data'):>24}"
        print(line)


def report_latency(agg: Agg, metric: str, conc_short: str) -> None:
    """p95 under the heaviest load each provider reached - the level is printed
    with it, because not every provider finished the sweep."""
    print(f"\n{agg.bench}: {metric} at the top of the sweep "
          f"(mean over runs; @ is the {conc_short} level)")
    print("  " + f"{'provider':<14}" + "".join(
        f"{label_dataset(agg.bench, d):>24}" for d in agg.datasets))
    for provider in agg.providers:
        line = "  " + f"{pretty(provider):<14}"
        for dataset in agg.datasets:
            value, at = agg.at_top(provider, dataset)
            line += f"{(f'{value:,.1f} ms @ {at}' if value else 'no data'):>24}"
        print(line)
    if agg.missing:
        print(f"  {agg.missing} measurement(s) had no p95 and are not in the "
              f"latency charts")


# A fixed cost per call - a long network path, nearly always - shows up as a
# floor under every response time a run recorded.  Where that floor is most of
# the percentile, the chart is measuring the path rather than the database under
# load, which is worth saying next to the figure.  A provider trips the test at
# twenty times the median floor of the campaign and at least 50 ms, so a tree
# where everyone sits close to the wire never trips it.
FLOOR_FACTOR = 20
FLOOR_MS = 50.0
# New-Order's share of the TPC-C call mix, read off the RATIO fields of the
# profiles themselves (41-45% across these providers).  Latency and throughput
# are two views of the same thing when the virtual users never pause: with
# keying and thinking off, mean response time is simply concurrency over
# throughput, which is the only latency a run with no profile can still be
# asked for.  sysbench quotes transactions a second, HammerDB new orders a
# minute, hence the conversion.
NEWORD_SHARE = 0.44


def call_floors(sessions: list[Session]) -> dict[str, float]:
    """The fastest call each provider's runs ever recorded, in ms."""
    out: dict[str, float] = {}
    for s in sessions:
        for r in s.rows:
            floor = r.get("floor")
            if floor and floor < out.get(s.provider, float("inf")):
                out[s.provider] = floor
    return out


def derived_ms(bench: str, row: dict) -> float | None:
    """Mean response time implied by a row's own throughput and concurrency."""
    rate = row["metric"] / 60 / NEWORD_SHARE if bench == "hammerdb" else row["metric"]
    return row["conc"] / rate * 1000 if rate > 0 else None


def floor_note(bench: str, sessions: list[Session]) -> str:
    """One sentence per provider whose response times rest on a floor high
    enough that its percentile is a fixed cost per call, not load."""
    floors = call_floors(sessions)
    if len(floors) < 3:          # too few to say what "normal" is here
        return ""
    cut = max(FLOOR_MS, FLOOR_FACTOR * float(np.median(list(floors.values()))))
    out = []
    for provider in sorted(floors):
        if floors[provider] < cut:
            continue
        rest = [f for p, f in floors.items() if p != provider]
        rows = [r for s in sessions if s.provider == provider for r in s.rows]
        measured = [r["p95"] for r in rows if r["p95"]]
        derived = sorted(filter(None, (derived_ms(bench, r) for r in rows)))
        txt = (f"{pretty(provider)}'s calls never came back faster than "
               f"{floors[provider]:,.0f} ms, against {min(rest):.1f}-{max(rest):.1f} ms "
               f"everywhere else, so what its percentile measures is a fixed cost "
               f"per call - a long network path - rather than the database under "
               f"load; its throughput rose in step with the concurrency to the top "
               f"of the sweep, so the host itself was never the limit.")
        if len(measured) < len(rows):
            txt += (f" {len(measured)} of its {len(rows)} runs recorded a profile "
                    f"at all")
            if measured:
                lo, hi = min(measured), max(measured)
                txt += (f" ({lo:,.0f} ms)" if round(lo) == round(hi)
                        else f" ({lo:,.0f}-{hi:,.0f} ms)")
            if derived:
                txt += (f", and read back from throughput and concurrency the rest "
                        f"sit at {derived[0]:,.0f}-{derived[-1]:,.0f} ms")
            txt += "."
        out.append(txt)
    return "  ".join(out)


# ----------------------------------------------------------------- main ------
BENCH_SPEC = {
    "sysbench": dict(
        metric="TPS (transactions/s)", conc_label="Threads", conc_short="t",
        dataset_col="rows_per_table", conc_col="threads",
        title="sysbench oltp_read_write - throughput",
        sub="Managed MySQL, 10 tables; mean of the measured 20 min window across runs",
        peak_title="sysbench oltp_read_write - peak throughput",
        peak_sub="Best thread level per provider, at the selected data-set size",
        table="summary_sysbench_tps.csv",
        p95_metric="p95 latency (ms) - lower is better",
        p95_title="sysbench oltp_read_write - 95th percentile latency",
        p95_sub="sysbench's own p95 over the measured 20 min window; mean across runs",
        p95_note="sysbench reports a fresh statistics block after the ramp-up, so this "
                 "percentile covers the measured window only - the same window as the TPS above.",
        p95_table="summary_sysbench_p95_ms.csv",
    ),
    "hammerdb": dict(
        metric="NOPM (new orders/min)", conc_label="Virtual users", conc_short="vu",
        dataset_col="warehouses", conc_col="vu",
        title="HammerDB TPC-C - throughput",
        sub="Managed MySQL; mean of the measured 20 min window across runs",
        peak_title="HammerDB TPC-C - peak throughput",
        peak_sub="Best virtual-user level per provider, at the selected warehouse count",
        table="summary_hammerdb_nopm.csv",
        p95_metric="New-Order p95 latency (ms) - lower is better",
        p95_title="HammerDB TPC-C - 95th percentile New-Order latency",
        # HammerDB's time profile is never reset between ramp-up and the measured
        # window, so unlike NOPM this percentile includes the ramp-up.
        p95_sub="HammerDB xtprof p95 merged across virtual users, ramp-up included; "
                "mean across runs",
        p95_note="HammerDB's time profile is never reset, so unlike the NOPM above this "
                 "percentile includes the 5 min ramp-up - it reads a little better than a "
                 "window-only figure would, and is not directly comparable with sysbench's.",
        p95_table="summary_hammerdb_p95_ms.csv",
    ),
}

# Cost efficiency spans the benchmarks rather than sitting inside one, so its
# copy lives next to them rather than in either spec.
COST_SPEC = dict(
    title="Managed database - cost efficiency",
    sub="An hour at the best throughput each host reached, over what that hour "
        "costs at the provider's rate",
)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", required=True, choices=("MySQL", "PostgreSQL"),
                    help="DBMS these results came from; names it in the report")
    ap.add_argument("-r", "--results", type=Path, default=Path("results"),
                    help="results root (default: %(default)s)")
    ap.add_argument("-o", "--outdir", type=Path, default=Path("analysis"),
                    help="where the graphs and tables go (default: %(default)s)")
    ap.add_argument("--theme", choices=("light", "dark", "both"), default="both",
                    help="render light, dark, or both (default: %(default)s)")
    ap.add_argument("--format", default="png", help="image format (default: %(default)s)")
    ap.add_argument("--html", dest="html", default="index.html",
                    help="name of the HTML report inside --outdir (default: %(default)s)")
    ap.add_argument("--no-html", dest="html", action="store_const", const="",
                    help="skip the HTML report")
    ap.add_argument("--no-images", dest="images", action="store_false",
                    help="skip the PNGs and write only the HTML report and CSVs")
    args = ap.parse_args(argv)

    # The engine's name is a caption, not a measurement, so it is simply
    # substituted into the copy the charts and the page already carry.
    for spec in BENCH_SPEC.values():
        spec["sub"] = spec["sub"].replace("Managed MySQL", f"Managed {args.db}")

    if not args.results.is_dir():
        print(f"analyze_results.py: no such results directory: {args.results}", file=sys.stderr)
        return 1

    sessions = discover(args.results)
    if not sessions:
        print(f"analyze_results.py: no result CSVs under {args.results}", file=sys.stderr)
        return 1
    for s in sessions:
        load(s)

    print(f"results root: {args.results.resolve()}")
    for s in sessions:
        note = f"  ({s.dropped} row(s) without a result dropped)" if s.dropped else ""
        print(f"  {s.bench:<9} {pretty(s.provider):<14} {s.stamp}  "
              f"{len(s.rows):>3} measurements{note}")

    # Read straight off the results tree, not off the sessions: a directory can
    # hold both benchmarks, and the host is billed once either way.
    work = HostWork(args.results)

    report_costs(work, sorted({s.provider for s in sessions}))
    report_host_efficiency(work)

    args.outdir.mkdir(parents=True, exist_ok=True)
    modes = ("light", "dark") if args.theme == "both" else (args.theme,)
    written: list[Path] = []
    aggs: dict[str, Agg] = {}
    lats: dict[str, Agg] = {}

    for bench, spec in BENCH_SPEC.items():
        group = [s for s in sessions if s.bench == bench and s.rows]
        if not group:
            print(f"\n{bench}: no usable rows, skipped")
            continue
        agg = Agg(bench, group)
        if len(agg.providers) > MAXSERIES:
            print(f"\n{bench}: {len(agg.providers)} providers exceeds the "
                  f"{MAXSERIES}-series palette cap; plotting the first {MAXSERIES}",
                  file=sys.stderr)
            agg.providers = agg.providers[:MAXSERIES]

        aggs[bench] = agg
        report(agg, spec["metric"], spec["conc_label"])
        written.append(write_table(agg, args.outdir / spec["table"],
                                   spec["dataset_col"], spec["conc_col"],
                                   spec["metric"].split()[0].lower()))

        # Throughput says how much work got through; p95 says what it cost the
        # slowest 5% of statements.  Same sweep, same providers, own axis.
        lat = Agg(bench, group, field="p95")
        lat.providers = [p for p in agg.providers if p in lat.providers]
        lats[bench] = lat
        # A provider whose response times sit on a floor is measuring the path
        # to the host, so the figure says so - in the console, and in the note
        # the HTML report carries under the chart.
        floors = floor_note(bench, group)
        if floors:
            spec["p95_note"] += "  " + floors
        if lat.providers:
            report_latency(lat, spec["p95_metric"].split(" (")[0], spec["conc_short"])
            for line in textwrap.wrap(floors, width=86):
                print(f"  {line}")
            written.append(write_table(lat, args.outdir / spec["p95_table"],
                                       spec["dataset_col"], spec["conc_col"], "p95_ms"))
        if not args.images:
            continue
        # One graph per file: every image holds a single plot for a single
        # data-set size, with the y-scale fit to that size's own data.
        for mode in modes:
            suffix = "" if args.theme != "both" else f"-{mode}"
            metric_slug = "tps" if bench == "sysbench" else "nopm"
            for dataset in agg.datasets:
                ds = slug_dataset(bench, dataset)
                written.append(chart_scaling(
                    agg, mode,
                    args.outdir / f"{bench}-{metric_slug}-vs-{spec['conc_col']}"
                                  f"-{ds}{suffix}.{args.format}",
                    spec["metric"], spec["conc_label"], spec["title"], spec["sub"],
                    dataset))
                written.append(chart_peak(
                    agg, mode,
                    args.outdir / f"{bench}-peak-{metric_slug}-{ds}{suffix}.{args.format}",
                    spec["metric"], spec["conc_short"], spec["peak_title"],
                    spec["peak_sub"], dataset))
                if dataset in lat.datasets:
                    written.append(chart_scaling(
                        lat, mode,
                        args.outdir / f"{bench}-p95-vs-{spec['conc_col']}"
                                      f"-{ds}{suffix}.{args.format}",
                        spec["p95_metric"], spec["conc_label"], spec["p95_title"],
                        spec["p95_sub"], dataset))

    # One chart, not one per benchmark: the instance is billed once for
    # everything that ran on it.
    if work.providers:
        written.append(write_cost_table(work, args.outdir / "summary_cost_efficiency.csv"))
        if args.images:
            for mode in modes:
                suffix = "" if args.theme != "both" else f"-{mode}"
                written.append(chart_cost(
                    work, mode,
                    args.outdir / f"cost-efficiency{suffix}.{args.format}",
                    COST_SPEC["title"], COST_SPEC["sub"]))

    if args.html and aggs:
        payload = html_report.build_payload(
            aggs, lats, sessions, work, args.results.resolve(), BENCH_SPEC,
            COST_SPEC, pretty, label_dataset, rate_usd_hour, peak_label,
            peak_short)
        written.append(html_report.write(payload, args.outdir / args.html,
                                         args.db))

    print("\nwrote:")
    for p in written:
        print(f"  {p}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
