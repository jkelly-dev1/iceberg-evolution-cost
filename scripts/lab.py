"""Shared plumbing for the four experiments: talk to Trino and to MinIO.

Everything here is a measurement helper. The experiments themselves live in
exp1_operation_cost.py, exp2_partition_planning.py, exp3_snapshot_reclaim.py
and exp4_delete_cost.py.
"""

import json
import subprocess
import time

TRINO = "iec-trino"
MINIO = "iec-minio"
CATALOG = "iceberg"
SCHEMA = "demo"

# The jline warning is written to stderr on EVERY invocation and says nothing
# about the query. It is filtered by pattern; discarding stderr wholesale once
# hid a real failure and made a broken statement look like an empty result.
_NOISE = ("org.jline", "dumb terminal", "WARNING:")


class QueryError(RuntimeError):
    pass


def _clean(text):
    return "\n".join(
        line for line in text.splitlines()
        if line.strip() and not any(n in line for n in _NOISE)
    )


def sql(statement, fmt="JSON"):
    """Run one statement. Returns parsed rows for JSON, else raw text."""
    proc = subprocess.run(
        ["docker", "exec", TRINO, "trino", "--output-format", fmt,
         "--execute", statement],
        capture_output=True, text=True,
    )
    out = _clean(proc.stdout + proc.stderr)
    if proc.returncode != 0:
        raise QueryError(f"{statement.splitlines()[0][:80]}\n{out}")
    if fmt != "JSON":
        return out
    return [json.loads(line) for line in out.splitlines() if line.startswith("{")]


def timed_sql(statement):
    """Run a statement and return (rows, wall_clock_ms).

    The clock covers the whole docker exec, so it includes CLI startup and is
    NOT a benchmark of Trino. It is only ever used to compare operations
    against each other under identical overhead, and the README says so.
    """
    start = time.perf_counter()
    rows = sql(statement)
    return rows, round((time.perf_counter() - start) * 1000, 1)


def one(statement):
    """First column of the first row, or None."""
    rows = sql(statement)
    return next(iter(rows[0].values())) if rows else None


def table(name):
    return f"{CATALOG}.{SCHEMA}.{name}"


def meta(name, kind):
    """A metadata table: $files, $snapshots, $partitions, $manifests, $history."""
    return f'{CATALOG}.{SCHEMA}."{name}${kind}"'


# Iceberg file content codes, from the table spec.
DATA, POSITION_DELETES, EQUALITY_DELETES = 0, 1, 2


def data_files(name):
    """Path -> (size, records, content) for every file the snapshot references.

    Content is not optional here. $files lists delete files alongside data
    files, and a DELETE under merge-on-read adds delete files while leaving
    every original data file in place. Counting rows of $files without reading
    `content` reports that as a rewrite, which is the opposite of what
    happened.
    """
    rows = sql(f"SELECT file_path, file_size_in_bytes, record_count, content "
               f"FROM {meta(name, 'files')}")
    return {r["file_path"]: (r["file_size_in_bytes"], r["record_count"],
                             r["content"])
            for r in rows}


def snapshots(name):
    rows = sql(f"SELECT snapshot_id, operation FROM {meta(name, 'snapshots')} "
               f"ORDER BY committed_at")
    return [(r["snapshot_id"], r["operation"]) for r in rows]


def _split(files):
    data = {p: v for p, v in files.items() if v[2] == DATA}
    deletes = {p: v for p, v in files.items() if v[2] != DATA}
    return data, deletes


def state(name):
    """Everything worth comparing before and after an operation."""
    files = data_files(name)
    data, deletes = _split(files)
    snaps = snapshots(name)
    return {
        "files": files,
        "data_files": data,
        "delete_files": deletes,
        "file_count": len(data),
        "delete_file_count": len(deletes),
        "bytes": sum(v[0] for v in data.values()),
        "delete_bytes": sum(v[0] for v in deletes.values()),
        "rows": sum(v[1] for v in data.values()),
        "snapshot_count": len(snaps),
        "snapshot_ids": [s for s, _ in snaps],
    }


def delta(before, after):
    """What actually changed, comparing file path SETS and not counts.

    Counts alone cannot tell a rewrite from a coincidence: an operation that
    replaces seven files with seven different files leaves the count identical.
    The set difference is the measurement that distinguishes them.

    DATA files and DELETE files are reported separately, because the headline
    question, did this operation rewrite my data, is answered by the data
    files alone. Merge-on-read DELETE adds delete files and touches no data
    file, and calling that a rewrite would be simply false.
    """
    d_before, d_after = before["data_files"], after["data_files"]
    x_before, x_after = before["delete_files"], after["delete_files"]

    added = set(d_after) - set(d_before)
    removed = set(d_before) - set(d_after)
    kept = set(d_before) & set(d_after)
    del_added = set(x_after) - set(x_before)

    return {
        "files_before": before["file_count"],
        "files_after": after["file_count"],
        "files_added": len(added),
        "files_removed": len(removed),
        "files_untouched": len(kept),
        "bytes_before": before["bytes"],
        "bytes_after": after["bytes"],
        "bytes_added": sum(d_after[p][0] for p in added),
        "bytes_removed": sum(d_before[p][0] for p in removed),
        "delete_files_before": before["delete_file_count"],
        "delete_files_after": after["delete_file_count"],
        "delete_files_added": len(del_added),
        "delete_bytes_added": sum(x_after[p][0] for p in del_added),
        "rows_before": before["rows"],
        "rows_after": after["rows"],
        "snapshots_before": before["snapshot_count"],
        "snapshots_after": after["snapshot_count"],
        "new_snapshots": after["snapshot_count"] - before["snapshot_count"],
        # Did any data file change identity? A bare append sets this true,
        # and that is not the question `verdict()` answers. `verdict()`
        # reserves "rewrite" for a data file that was removed and replaced,
        # so an UPDATE is `rewrote_data=True` here and `merge-on-read` in the
        # verdict, true under both definitions. SAMPLE_RUN.md shows this flag,
        # the README's table shows the verdict, and each says which it means.
        # test_measurement_logic.py::TestVerdict::
        # test_rewrote_data_and_the_verdict_mean_different_things pins the
        # relationship so neither definition can drift onto the other.
        "rewrote_data": len(added) > 0 or len(removed) > 0,
        "added_delete_files": len(del_added) > 0,
    }


def disk():
    """Exact bytes and object count under the warehouse bucket in MinIO."""
    proc = subprocess.run(
        ["docker", "exec", MINIO, "mc", "du", "--json", "local/warehouse"],
        capture_output=True, text=True,
    )
    for line in proc.stdout.splitlines():
        if line.startswith("{"):
            d = json.loads(line)
            return {"bytes": d["size"], "objects": d["objects"]}
    raise QueryError(f"mc du gave nothing parseable: {proc.stdout}{proc.stderr}")


def objects(prefix="local/warehouse"):
    """Every object under the prefix: key -> size. Used to find orphans."""
    proc = subprocess.run(
        ["docker", "exec", MINIO, "mc", "ls", "--recursive", "--json", prefix],
        capture_output=True, text=True,
    )
    # A failed listing prints nothing, and an empty dict would read as a
    # table holding zero bytes.
    if proc.returncode != 0:
        raise QueryError(f"mc ls failed ({proc.returncode}): {proc.stderr}")
    found = {}
    for line in proc.stdout.splitlines():
        if not line.startswith("{"):
            continue
        d = json.loads(line)
        if d.get("type") == "file":
            found[d["key"]] = d["size"]
    return found


def write_result(path, payload):
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2, sort_keys=True)
        fh.write("\n")
    print(f"wrote {path}")


_UNITS = {"B": 1, "kB": 1000, "MB": 1000**2, "GB": 1000**3, "TB": 1000**4}


def _bytes(text):
    """'702.07kB' -> 702070. Trino prints decimal units, not binary."""
    num = float("".join(c for c in text if c.isdigit() or c == "."))
    unit = "".join(c for c in text if c.isalpha())
    return int(num * _UNITS.get(unit, 1))


def parse_explain(text):
    """Pull what the scan actually read out of an EXPLAIN ANALYZE plan.

    Separated from the query so it can be tested without Trino. This parser
    produces every "Bytes read", "Rows scanned" and "Splits" figure in
    sections 2 and 4, including the 28x evolution headline, so the tests call
    it directly on captured plan text.

    physical_input_bytes is bytes fetched from object storage AFTER partition
    pruning and Parquet row-group skipping. scan_rows is rows the table scan
    handed upward. Together they separate "pruned a file" from "read a file
    and skipped its row groups", which a single number cannot do.
    """
    import re
    phys = re.findall(r"Physical input: ([0-9.]+[kMGT]?B)", text)
    rows = [int(m.replace(",", ""))
            for m in re.findall(r"Input: ([0-9,]+) rows", text)]
    splits = [int(m.replace(",", ""))
              for m in re.findall(r"Splits: ([0-9,]+)", text)]
    return {
        # Absent is not zero-by-accident. When a count is satisfied from
        # Iceberg manifest counts and Parquet row-group metadata, the plan
        # carries no "Physical input" line at all and the scan reports
        # "(0B)". That is a real result, the query read no column data, so
        # the flag records which of the two cases produced the number.
        "physical_input_bytes": max((_bytes(p) for p in phys), default=0),
        "physical_input_reported": bool(phys),
        "scan_rows": max(rows, default=0),
        "splits": max(splits, default=0),
    }


def explain_metrics(query):
    """Run EXPLAIN ANALYZE and parse it. Needs Trino; the parser does not."""
    return parse_explain(sql(f"EXPLAIN ANALYZE {query}", fmt="CSV"))


def sql_session(*statements):
    """Run several statements in ONE Trino session.

    Each `trino --execute` is a fresh session, so a SET SESSION in its own
    call is discarded before the next statement ever runs. Anything that needs
    a session property, expire_snapshots below its default 7 day guard, for
    one, has to travel with it.
    """
    return sql("; ".join(statements), fmt="CSV")


def table_disk(name):
    """Bytes and object count actually stored for one table.

    Iceberg lays a table out under demo/<name>-<uuid>/, so the prefix
    identifies it. This counts EVERY object including metadata.json, manifest
    lists and manifests, and data files no live snapshot references any more.
    That last category is the whole subject of experiment 3.
    """
    prefix = f"{SCHEMA}/{name}-"
    found = {k: v for k, v in objects().items() if k.startswith(prefix)}
    data = {k: v for k, v in found.items() if "/data/" in k}
    return {
        "objects": len(found),
        "bytes": sum(found.values()),
        "data_objects": len(data),
        "data_bytes": sum(data.values()),
        "metadata_objects": len(found) - len(data),
        "metadata_bytes": sum(found.values()) - sum(data.values()),
    }


def verdict(rec):
    """Four outcomes, not two.

    metadata-only  no data file and no delete file changed identity
    merge-on-read  delete files appeared; no data file was removed
    append         new data files only, nothing removed
    rewrite        at least one data file was removed and replaced

    "Rewrite" here is narrower than `delta()["rewrote_data"]`. That flag
    asks whether the data file set changed, so an append sets it;
    this asks whether anything was thrown away and written again, which is the
    question "did this operation rewrite my data" actually means. The UPDATE
    in experiment 1 is True by the first and "merge-on-read" by the second.
    """
    if rec["files_removed"] > 0:
        return "rewrite"
    if rec["delete_files_added"] > 0:
        return "merge-on-read"
    if rec["files_added"] > 0:
        return "append"
    return "metadata-only"
