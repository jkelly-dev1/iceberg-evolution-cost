"""Experiment 2: what partition evolution does to query planning.

Four layouts of the same 20,000,000 rows:

  none      no partitioning at all
  month     partitioned by month(event_date) from the start
  day       partitioned by day(event_date) from the start
  evolved   written under month(event_date), then ALTERed to day(event_date),
            then MORE ROWS INSERTED so the table holds files under BOTH specs

The evolved table is the point of the experiment. Iceberg does not rewrite old
files when the partition spec changes, so old files keep the old spec forever.
A single-day predicate can only prune those to month granularity.
"""

import lab

SOURCE = "events"
DAY = "DATE '2026-02-10'"

LAYOUTS = [
    ("none", None),
    ("month", "ARRAY['month(event_date)']"),
    ("day", "ARRAY['day(event_date)']"),
]

QUERIES = {
    "single_day": f"SELECT count(*) FROM {{t}} WHERE event_date = {DAY}",
    "single_month": ("SELECT count(*) FROM {t} WHERE event_date "
                     ">= DATE '2026-02-01' AND event_date < DATE '2026-03-01'"),
    "non_partition_column": "SELECT count(*) FROM {t} WHERE region = 'eu-west'",
}


def build(name, partitioning):
    t = lab.table(f"exp2_{name}")
    lab.sql(f"DROP TABLE IF EXISTS {t}")
    props = "format = 'PARQUET'"
    if partitioning:
        props += f", partitioning = {partitioning}"
    lab.sql(f"CREATE TABLE {t} WITH ({props}) AS "
            f"SELECT * FROM {lab.table(SOURCE)}")
    return t


def build_evolved():
    """month spec, then evolve to day, then add rows under the new spec.

    Three states. The middle one, between the ALTER and the INSERT, is what
    lets "the ALTER rewrote no data" be observed: a delta across both
    statements cannot say which one added files, because an INSERT always
    adds some.
    """
    t = lab.table("exp2_evolved")
    lab.sql(f"DROP TABLE IF EXISTS {t}")
    lab.sql(f"CREATE TABLE {t} WITH (format='PARQUET', "
            f"partitioning = ARRAY['month(event_date)']) AS "
            f"SELECT * FROM {lab.table(SOURCE)}")
    before = lab.state("exp2_evolved")
    lab.sql(f"ALTER TABLE {t} SET PROPERTIES "
            f"partitioning = ARRAY['day(event_date)']")
    after_alter = lab.state("exp2_evolved")
    # New rows, distinct ids, same date range: these land under the DAY spec
    # while every original file stays under MONTH.
    lab.sql(f"INSERT INTO {t} SELECT event_id + 100000000, event_date, region, "
            f"status, amount_cents, payload_hash, legacy_seq "
            f"FROM {lab.table(SOURCE)} WHERE event_id <= 2000000")
    after = lab.state("exp2_evolved")
    return t, before, after_alter, after


def spec_mix(name):
    """How many data files sit under each partition spec id."""
    rows = lab.sql(f"SELECT spec_id, count(*) AS files, "
                   f"sum(file_size_in_bytes) AS bytes "
                   f"FROM {lab.meta(name, 'files')} WHERE content = 0 "
                   f"GROUP BY spec_id ORDER BY spec_id")
    return [{"spec_id": r["spec_id"], "files": r["files"], "bytes": r["bytes"]}
            for r in rows]


def main():
    layouts = {}
    for name, part in LAYOUTS:
        print(f"  building {name} ...", flush=True)
        t = build(name, part)
        layouts[name] = {"table": t, "specs": spec_mix(f"exp2_{name}"),
                         "state": None}

    print("  building evolved ...", flush=True)
    t_evo, evo_before, evo_after_alter, evo_after = build_evolved()
    alter = lab.delta(evo_before, evo_after_alter)
    layouts["evolved"] = {"table": t_evo, "specs": spec_mix("exp2_evolved")}

    results = {}
    for name, info in layouts.items():
        results[name] = {"partition_specs": info["specs"], "queries": {}}
        for qname, template in QUERIES.items():
            q = template.format(t=info["table"])
            m = lab.explain_metrics(q)
            results[name]["queries"][qname] = m
            print(f"    {name:8} {qname:22} "
                  f"{m['physical_input_bytes']:>10,} B  "
                  f"{m['scan_rows']:>12,} rows  "
                  f"{m['splits']:>4} splits", flush=True)

    payload = {
        "experiment": "partition_planning",
        "question": ("what partition evolution does to pruning, and what it "
                     "costs to hold files under two specs at once"),
        "day_predicate": DAY,
        "evolved_partition_change": {
            # Whether a distinct middle state exists, asked of the states
            # themselves. A run that hands the same state back twice reports
            # false here, and the ALTER figures below are then not evidence.
            "alter_isolated": evo_after_alter is not evo_after,
            "files_before_alter": evo_before["file_count"],
            "files_after_alter": evo_after_alter["file_count"],
            "files_after_alter_and_insert": evo_after["file_count"],
            "alter_rewrote_data": alter["rewrote_data"],
            "alter_files_added": alter["files_added"],
            "alter_files_removed": alter["files_removed"],
            "alter_bytes_added": alter["bytes_added"],
        },
        "layouts": results,
    }
    lab.write_result("results/exp2_partition_planning.json", payload)


if __name__ == "__main__":
    main()
