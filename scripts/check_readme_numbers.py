"""Re-derive every number in README.md from results/*.json and diff them.

A README is prose and drifts; results/*.json is evidence and does not. This
script rebuilds each table row from the JSON and asserts the exact string is
present in the README, so a re-run that changes a byte figure fails loudly
instead of leaving the document quietly wrong.

Run it from the repository root:  python3 scripts/check_readme_numbers.py
"""

import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def load(name):
    with open(os.path.join(ROOT, "results", name), encoding="utf-8") as fh:
        return json.load(fh)


def mb(n):
    return f"{n / 1e6:.1f} MB"


def expected_rows():
    e1, e2, e3, e4 = (load("exp1_operation_cost.json"),
                      load("exp2_partition_planning.json"),
                      load("exp3_snapshot_reclaim.json"),
                      load("exp4_delete_cost.json"))
    rows = []

    label = {
        "add_column": "`ADD COLUMN`",
        "rename_column": "`RENAME COLUMN`",
        "drop_column": "`DROP COLUMN`",
        "widen_type_int_to_bigint": "`ALTER COLUMN ... SET DATA TYPE bigint`",
        "set_partitioning": "`SET PROPERTIES partitioning = ...`",
        "delete_matching_rows": "`DELETE FROM ... WHERE`",
        "update_matching_rows": "`UPDATE ... SET`",
        "execute_optimize": "`ALTER TABLE ... EXECUTE optimize`",
    }
    for o in e1["operations"]:
        written = "0" if o["bytes_added"] == 0 else f"{round(o['bytes_added']/1e6)} MB"
        rows.append(("exp1:" + o["operation"],
                     f"| {label[o['operation']]} | {o['verdict']} | "
                     f"+{o['files_added']} / -{o['files_removed']} | "
                     f"+{o['delete_files_added']} | {written} |"))

    for name in ("none", "month", "day", "evolved"):
        lay = e2["layouts"][name]
        q = lay["queries"]["single_day"]
        rows.append(("exp2:" + name,
                     f"| `{name}` | {len(lay['partition_specs'])} | "
                     f"{q['scan_rows']:,} | {q['splits']} | "
                     f"{q['physical_input_bytes']:,} |"))

    stage_label = {
        "01_created": "| created | ", "02_appended": "| appended | ",
        "03_updated": "| updated | ", "04_deleted": "| deleted | ",
        "05_optimized": "| **optimized** | ",
        "06_expired_snapshots": "| after `expire_snapshots` | ",
        "07_removed_orphans": "| after `remove_orphan_files` | ",
    }
    for st in e3["stages"]:
        bold = st["stage"] in ("05_optimized", "06_expired_snapshots")
        disk = f"**{mb(st['disk_data_bytes'])}**" if bold else mb(st["disk_data_bytes"])
        stranded = ("0" if st["unreferenced_bytes"] == 0
                    else (f"**{mb(st['unreferenced_bytes'])}**" if bold
                          else mb(st["unreferenced_bytes"])))
        rows.append(("exp3:" + st["stage"],
                     f"{stage_label[st['stage']]}{st['snapshots']} | "
                     f"{mb(st['referenced_bytes'])} | {disk} | {stranded} |"))

    oob = e4["write_mode"]["out_of_band"]
    rows.append(("exp4:out_of_band",
                 f"| `set-properties` via the REST catalog | yes | "
                 f"{oob['verdict']} | +{oob['files_added']} / "
                 f"-{oob['files_removed']} | +{oob['delete_files_added']} |"))

    stage_label4 = {
        "00_baseline": "baseline",
        "01_after_delete_1": "after delete 1",
        "02_after_delete_2": "after delete 2",
        "03_after_delete_3": "after delete 3",
        "04_after_delete_4": "after delete 4",
        "05_after_delete_5": "after delete 5",
        "06_after_optimize": "**after optimize**",
    }
    for st in e4["read_amplification"]["stages"]:
        f, v = st["queries"]["full_scan"], st["queries"]["selective"]
        rows.append(("exp4:" + st["stage"],
                     f"| {stage_label4[st['stage']]} | {st['data_files']} | "
                     f"{st['delete_files']} | {st.get('delete_bytes', 0):,} | "
                     f"{f['duration_ms_median']:.0f} ms | "
                     f"{v['duration_ms_median']:.0f} ms |"))
    return rows


def prose_facts():
    """Numbers that live in SENTENCES, not in tables, re-derived the same way.

    The tables are not the whole risk. A percentage inside a paragraph does
    not look like a figure the way a table row does, to a reader or to whoever
    writes the deriver, so it is the figure most likely to go unchecked and
    drift. Everything below is built from results/*.json and has to appear in
    the README verbatim.
    """
    e2, e4 = (load("exp2_partition_planning.json"),
              load("exp4_delete_cost.json"))
    stages = e4["read_amplification"]["stages"]
    last = stages[5]
    scan = last["queries"]["full_scan"]["physical_input_bytes"]
    share = round(100 * last["delete_bytes"] / scan)

    evolved = e2["layouts"]["evolved"]["queries"]["single_day"]["scan_rows"]
    native = e2["layouts"]["day"]["queries"]["single_day"]["scan_rows"]

    def span(query):
        without = [x for s in stages if s["delete_files"] == 0
                   for x in s["queries"][query]["duration_ms_samples"]]
        with_ = [x for s in stages if s["delete_files"] > 0
                 for x in s["queries"][query]["duration_ms_samples"]]
        return (f"{min(without):.0f}-{max(without):.0f}",
                f"{min(with_):.0f}-{max(with_):.0f}")

    full_without, full_with = span("full_scan")
    sel_without, sel_with = span("selective")
    baseline_rows = stages[0]["live_rows"]
    after_rows = stages[6]["live_rows"]

    return [
        ("prose:evolution_ratio",
         f"through evolution reads {round(evolved / native)}x more rows"),
        ("prose:delete_files",
         f"{last['data_files']} data files throughout, "
         f"{last['delete_files']} delete files at the end"),
        ("prose:logical_vs_physical",
         f"answer is {after_rows:,} rows while the data files still "
         f"physically hold all {baseline_rows:,}"),
        ("prose:full_scan_ranges",
         f"full scan ran {full_without} ms with no delete files and "
         f"{full_with} ms with them"),
        ("prose:selective_ranges",
         f"the selective query {sel_without} ms against {sel_with} ms"),
        ("prose:invisible_share",
         f"were {last['delete_bytes']:,} bytes against a reported {scan:,} "
         f"byte scan, {share}% of the read"),
    ]


def main():
    with open(os.path.join(ROOT, "README.md"), encoding="utf-8") as fh:
        readme = fh.read()
    # Whitespace is normalized on both sides. A prose figure lands wherever the
    # line wrap puts it, so matching raw text would fail on a reflowed
    # paragraph: a false alarm that trains a reader to ignore this script.
    flat = " ".join(readme.split())
    rows, facts = expected_rows(), prose_facts()
    checked = rows + facts
    missing = [(tag, row) for tag, row in checked
               if " ".join(row.split()) not in flat]
    for tag, row in missing:
        print(f"MISSING [{tag}]\n  {row}")
    print(f"\n{len(checked) - len(missing)} of {len(checked)} derived figures "
          f"found verbatim in README.md "
          f"({len(rows)} table rows, {len(facts)} in prose)")
    return 1 if missing else 0


if __name__ == "__main__":
    sys.exit(main())
