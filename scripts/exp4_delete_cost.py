"""Experiment 4: what a DELETE really costs, on both sides of the ledger.

Experiment 1 measured the WRITE side of a delete and stopped there: under
merge-on-read a DELETE adds delete files and rewrites no data file, which
refuted the prediction it had written down. That result is easy to misread as
"Iceberg never rewrites on a delete". It does not say that. It says THIS
ENGINE, on ITS default, deferred the work. Two things were left unmeasured.

Which mode, and who decides. Copy-on-write versus merge-on-read is a table
property in the Iceberg spec (write.delete.mode). If the property decides, then
setting it should change the verdict. Part A sets it two ways and measures
whether it does.

What the deferred work costs. Merge-on-read is not free, it is BORROWED: every
later reader applies the delete files the writer did not. Experiment 1 never
ran a query afterward, so the interest payment went unmeasured. Part B deletes
five times over and measures the same query after every round, then compacts
and measures again.

Part B is the shape a streaming upsert writer produces, many small commits,
each leaving delete files behind, so it is worth measuring here before
anything writes into a table this way.
"""

import sys
import json
import urllib.request

import lab

SOURCE = "events"
REST = "http://127.0.0.1:8181/v1/namespaces/demo/tables"

COW = {"write.delete.mode": "copy-on-write",
       "write.update.mode": "copy-on-write",
       "write.merge.mode": "copy-on-write"}

# Five disjoint slices, about 1% of the table each. Disjoint matters: deleting
# the same rows twice is a no-op after the first round and would flatten the
# curve for a reason that has nothing to do with Iceberg.
ROUNDS = [(0, 999), (1000, 1999), (2000, 2999), (3000, 3999), (4000, 4999)]

# The read side is measured with two queries, because one number cannot
# separate the two ways delete files cost you.
#   full_scan  touches every data file, so it pays the merge on all of them
#   selective  touches a subset, so it shows whether the cost tracks the data
#              actually read or the delete files that merely exist
QUERIES = {
    "full_scan": "SELECT count(*), sum(amount_cents) FROM {t}",
    "selective": "SELECT count(*) FROM {t} WHERE region = 'eu-west'",
}


def rest_get(name):
    with urllib.request.urlopen(f"{REST}/{name}") as resp:
        return json.load(resp)


def rest_set_properties(name, props):
    body = {"requirements": [],
            "updates": [{"action": "set-properties", "updates": props}]}
    req = urllib.request.Request(
        f"{REST}/{name}", data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req) as resp:
        return resp.status


def fresh(name, rows=None):
    """A fresh copy of the source table. Full size unless rows is given."""
    lab.sql(f"DROP TABLE IF EXISTS {lab.table(name)}")
    where = f" WHERE event_id <= {rows}" if rows else ""
    lab.sql(f"CREATE TABLE {lab.table(name)} WITH (format = 'PARQUET') AS "
            f"SELECT * FROM {lab.table(SOURCE)}{where}")


def part_a_direct():
    """Can the mode be set through Trino at all?

    Trino exposes no delete_mode table property. extra_properties is the
    documented escape hatch for arbitrary Iceberg properties, so that is the
    honest place to try.
    """
    keys = ", ".join(f"'{k}'" for k in COW)
    vals = ", ".join(f"'{v}'" for v in COW.values())
    stmt = (f"CREATE TABLE {lab.table('exp4_direct')} "
            f"WITH (format = 'PARQUET', extra_properties = "
            f"MAP(ARRAY[{keys}], ARRAY[{vals}])) AS "
            f"SELECT * FROM {lab.table(SOURCE)} WHERE event_id <= 200000")
    rec = {"method": "extra_properties on CREATE TABLE",
           "properties_requested": COW}
    try:
        lab.sql(stmt)
        rec["accepted"] = True
    except lab.QueryError as exc:
        # A refusal is the RESULT. The connector declining to carry a spec
        # property is exactly the thing worth knowing.
        rec["accepted"] = False
        rec["error"] = str(exc).splitlines()[-1][:200]
    finally:
        lab.sql(f"DROP TABLE IF EXISTS {lab.table('exp4_direct')}")
    return rec


def part_a_out_of_band():
    """Set the property behind Trino's back, then delete through Trino.

    The REST catalog is the same catalog Trino reads, so a property set this
    way is a property Trino sees. This is what a table written by a different
    engine looks like from Trino's side.
    """
    name = "exp4_oob"
    fresh(name, rows=200000)
    rec = {"method": "set-properties via the Iceberg REST catalog",
           "properties_requested": COW}
    rec["http_status"] = rest_set_properties(name, COW)
    stored = rest_get(name)["metadata"]["properties"]
    rec["properties_stored"] = {k: stored.get(k) for k in COW}
    rec["properties_are_set_on_the_table"] = all(
        stored.get(k) == v for k, v in COW.items())

    before = lab.state(name)
    _, ms = lab.timed_sql(
        f"DELETE FROM {lab.table(name)} WHERE status = 'failed'")
    after = lab.state(name)
    d = lab.delta(before, after)
    rec.update({k: d[k] for k in
                ("files_added", "files_removed", "delete_files_added",
                 "rewrote_data")})
    rec["verdict"] = lab.verdict(d)
    rec["duration_ms"] = ms
    rec["honored"] = rec["verdict"] == "rewrite"
    lab.sql(f"DROP TABLE IF EXISTS {lab.table(name)}")
    return rec


# Each timing is the MEDIAN of this many runs. A single sample per stage
# cannot support any claim here: the spread between repeats of the same query
# is comparable to the effect being measured, because the clock covers a whole
# docker exec including CLI startup.
REPEATS = 7


def measure_reads(name):
    out = {}
    for label, template in QUERIES.items():
        q = template.format(t=lab.table(name))
        metrics = lab.explain_metrics(q)
        # Physical input does not move here. Trino's "Physical input" counts
        # the data scan and not the delete files it merges on top, so the
        # one metric you would reach for to measure merge-on-read read cost
        # reports the same number whether there are zero delete files or
        # fifty. It is recorded anyway, as the evidence for that claim.
        samples = sorted(lab.timed_sql(q)[1] for _ in range(REPEATS))
        metrics["duration_ms_samples"] = samples
        metrics["duration_ms_median"] = samples[len(samples) // 2]
        metrics["duration_ms_min"] = samples[0]
        out[label] = metrics
    return out


def part_b():
    name = "exp4_readcost"
    fresh(name)
    stages = []

    st = lab.state(name)
    stages.append({"stage": "00_baseline", "deletes_applied": 0,
                   "data_files": st["file_count"],
                   "delete_files": st["delete_file_count"],
                   "live_rows": st["rows"],
                   "queries": measure_reads(name)})
    print(f"  baseline: {st['file_count']} data files, "
          f"{st['delete_file_count']} delete files", flush=True)

    for i, (lo, hi) in enumerate(ROUNDS, start=1):
        _, ms = lab.timed_sql(
            f"DELETE FROM {lab.table(name)} "
            f"WHERE amount_cents BETWEEN {lo} AND {hi}")
        st = lab.state(name)
        stages.append({"stage": f"{i:02d}_after_delete_{i}",
                       "deletes_applied": i,
                       "delete_sql": f"amount_cents BETWEEN {lo} AND {hi}",
                       "delete_duration_ms": ms,
                       "data_files": st["file_count"],
                       "delete_files": st["delete_file_count"],
                       "delete_bytes": st["delete_bytes"],
                       # Rows here is the sum of data file record counts, so it
                       # still counts deleted rows. That is the point: the rows
                       # are gone logically and present physically.
                       "rows_in_data_files": st["rows"],
                       "queries": measure_reads(name)})
        print(f"  delete {i}: {st['delete_file_count']} delete files, "
              f"{ms:.0f} ms", flush=True)

    _, ms = lab.timed_sql(
        f"ALTER TABLE {lab.table(name)} EXECUTE optimize")
    st = lab.state(name)
    stages.append({"stage": "06_after_optimize", "deletes_applied": len(ROUNDS),
                   "optimize_duration_ms": ms,
                   "data_files": st["file_count"],
                   "delete_files": st["delete_file_count"],
                   "live_rows": st["rows"],
                   "queries": measure_reads(name)})
    print(f"  optimize: {st['file_count']} data files, "
          f"{st['delete_file_count']} delete files, {ms:.0f} ms", flush=True)

    lab.sql(f"DROP TABLE IF EXISTS {lab.table(name)}")
    return stages


def main():
    part = sys.argv[1] if len(sys.argv) > 1 else "all"
    payload = {"experiment": "delete_cost",
               "question": "who decides the write mode, and what does the "
                           "deferred work cost the reader"}

    if part in ("all", "a"):
        print("Part A: who decides the write mode", flush=True)
        payload["write_mode"] = {"direct": part_a_direct(),
                                 "out_of_band": part_a_out_of_band()}
        print(f"    direct accepted={payload['write_mode']['direct']['accepted']}"
              f"  out-of-band honored="
              f"{payload['write_mode']['out_of_band']['honored']}", flush=True)

    if part in ("all", "b"):
        print("Part B: what the reader pays", flush=True)
        payload["read_amplification"] = {
            "queries": {k: v.format(t="<table>") for k, v in QUERIES.items()},
            "stages": part_b()}

    lab.write_result("results/exp4_delete_cost.json", payload)


if __name__ == "__main__":
    main()
