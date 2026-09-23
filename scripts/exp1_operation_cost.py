"""Experiment 1: which table changes are metadata-only, and which rewrite data?

Each operation runs on its own fresh copy of the events table. Sharing one
table would let operation N inherit the files operation N-1 rewrote, and
rewritten bytes have to be attributed to the operation that caused them.

The measurement is a set difference over data file paths, not a count. An
operation that swaps seven files for seven different files leaves the count
unchanged and is still a full rewrite.
"""

import sys
import lab
from lab import verdict

SOURCE = "events"

# (name, sql template, the NAIVE PREDICTION made before running anything)
#
# The predictions are kept exactly as first written, including the wrong ones.
# "delete_matching_rows" was predicted to rewrite data and does not: under
# merge-on-read it writes delete files and leaves every data file in place.
# Recording the refuted prediction is the point; silently correcting it would
# throw away the only surprising result in the table.
OPERATIONS = [
    ("add_column",
     "ALTER TABLE {t} ADD COLUMN note varchar",
     "metadata-only"),
    ("rename_column",
     "ALTER TABLE {t} RENAME COLUMN legacy_seq TO seq_no",
     "metadata-only"),
    ("drop_column",
     "ALTER TABLE {t} DROP COLUMN legacy_seq",
     "metadata-only"),
    ("widen_type_int_to_bigint",
     "ALTER TABLE {t} ALTER COLUMN amount_cents SET DATA TYPE bigint",
     "metadata-only"),
    ("set_partitioning",
     "ALTER TABLE {t} SET PROPERTIES partitioning = ARRAY['month(event_date)']",
     "metadata-only"),
    ("delete_matching_rows",
     "DELETE FROM {t} WHERE status = 'failed'",
     "rewrites data"),
    ("update_matching_rows",
     "UPDATE {t} SET status = 'ok' WHERE status = 'retry'",
     "rewrites data"),
    ("execute_optimize",
     "ALTER TABLE {t} EXECUTE optimize",
     "rewrites data"),
]


def run_one(name, template, expectation):
    tmp = f"exp1_{name}"
    lab.sql(f"DROP TABLE IF EXISTS {lab.table(tmp)}")
    lab.sql(f"CREATE TABLE {lab.table(tmp)} AS SELECT * FROM {lab.table(SOURCE)}")

    before = lab.state(tmp)
    disk_before = lab.disk()
    record = {"operation": name, "naive_prediction": expectation,
              "sql": template.format(t="<table>")}
    try:
        _, ms = lab.timed_sql(template.format(t=lab.table(tmp)))
        record["supported"] = True
        record["duration_ms"] = ms
        after = lab.state(tmp)
        record.update(lab.delta(before, after))
        record["verdict"] = verdict(record)
        # A prediction of "rewrites data" is REFUTED by a merge-on-read
        # verdict. Collapsing the three verdicts to a metadata-only/other
        # boolean scored that as a hit, which quietly turned the most
        # interesting result in this table into a passing row.
        expected_verdict = ("metadata-only" if expectation == "metadata-only"
                            else "rewrite")
        record["expected_verdict"] = expected_verdict
        record["prediction_held"] = record["verdict"] == expected_verdict
        record["disk_bytes_before"] = disk_before["bytes"]
        record["disk_bytes_after"] = lab.disk()["bytes"]
    except lab.QueryError as exc:
        # An unsupported operation is recorded as a result. Trino's Iceberg
        # connector does not implement every Iceberg spec operation, and which
        # ones it declines is part of the answer.
        record["supported"] = False
        record["error"] = str(exc).splitlines()[-1][:200]
    finally:
        lab.sql(f"DROP TABLE IF EXISTS {lab.table(tmp)}")
    return record


def main():
    only = sys.argv[1] if len(sys.argv) > 1 else None
    results = []
    for name, template, expectation in OPERATIONS:
        if only and only != name:
            continue
        print(f"  {name} ...", flush=True)
        rec = run_one(name, template, expectation)
        results.append(rec)
        if rec.get("supported"):
            print(f"    rewrote_data={rec['rewrote_data']}  "
                  f"data +{rec['files_added']}/-{rec['files_removed']}  "
                  f"deletefiles +{rec['delete_files_added']}  "
                  f"{rec['bytes_added']} B added  {rec['duration_ms']} ms")
        else:
            print(f"    UNSUPPORTED: {rec['error'][:90]}")

    # Read off the source table, so the results file describes the table this
    # run actually used. The README's "about 458 MB" comes from the byte
    # figure, and a change to sql/generate.sql moves all three. One query.
    src = lab.state(SOURCE)
    payload = {
        "experiment": "operation_cost",
        "question": "which table changes are metadata-only and which rewrite data",
        "source_table": {
            # Which table these were read off, and null in a run that did not
            # read them. The key names the table, so it cannot be a constant
            # claiming a measurement.
            "measured_from": lab.table(SOURCE),
            "rows": src["rows"],
            "files": src["file_count"],
            "bytes": src["bytes"],
        },
        "operations": results,
    }
    lab.write_result("results/exp1_operation_cost.json", payload)


if __name__ == "__main__":
    main()
