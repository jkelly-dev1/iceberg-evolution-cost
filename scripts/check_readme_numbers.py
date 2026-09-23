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

# What GitHub stores for a repository description. Past this the tail is
# simply not shown, and nothing else in this tree knows the number.
GITHUB_DESCRIPTION_LIMIT = 350


def load(name):
    with open(os.path.join(ROOT, "results", name), encoding="utf-8") as fh:
        return json.load(fh)


def mb(n):
    return f"{n / 1e6:.1f} MB"


def block(tag, headers, rows):
    """One markdown table: its header, its delimiter and its body rows.

    The header is part of the claim. It says what each column means, so a
    body row can be right while its label is wrong: the column filled from
    `bytes_added` holds data bytes only, and a label that promised more would
    pass every check that reads body rows alone.

    Declaring the headers beside the rows also makes the cell counts agree by
    construction: a column added to one side alone raises here rather than
    rendering as an empty cell on GitHub.
    """
    out = [(tag + ":header", "| " + " | ".join(headers) + " |"),
           (tag + ":rule", "| " + " | ".join(["---"] * len(headers)) + " |")]
    for row_tag, cells in rows:
        if len(cells) != len(headers):
            raise AssertionError(
                f"{row_tag}: {len(cells)} cells against {len(headers)} "
                f"headers in table {tag}")
        out.append((row_tag, "| " + " | ".join(cells) + " |"))
    return out


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

    # Two byte columns, each named for what it holds. `bytes_added` is data
    # bytes, since lab.delta sums it over data files alone, so one column
    # would show a merge-on-read DELETE as 0 although it writes delete files.
    # Section 4 argues that merge-on-read defers the work, and a 0 there
    # would contradict it.
    def written(n):
        return "0" if n == 0 else f"{round(n / 1e6)} MB"

    rows += block(
        "exp1",
        ["Operation", "Verdict", "Data files", "Delete files",
         "Data bytes written", "Delete bytes written"],
        [("exp1:" + o["operation"],
          [label[o["operation"]], o["verdict"],
           f"+{o['files_added']} / -{o['files_removed']}",
           f"+{o['delete_files_added']}",
           written(o["bytes_added"]), written(o["delete_bytes_added"])])
         for o in e1["operations"]])

    rows += block(
        "exp2",
        ["Layout", "Partition specs", "Rows scanned", "Splits", "Bytes read"],
        [("exp2:" + name,
          [f"`{name}`",
           str(len(e2["layouts"][name]["partition_specs"])),
           f"{e2['layouts'][name]['queries']['single_day']['scan_rows']:,}",
           str(e2["layouts"][name]["queries"]["single_day"]["splits"]),
           f"{e2['layouts'][name]['queries']['single_day']['physical_input_bytes']:,}"])
         for name in ("none", "month", "day", "evolved")])

    stage_label = {
        "01_created": "created", "02_appended": "appended",
        "03_updated": "updated", "04_deleted": "deleted",
        "05_optimized": "**optimized**",
        "06_expired_snapshots": "after `expire_snapshots`",
        "07_removed_orphans": "after `remove_orphan_files`",
    }

    # Three numbers, because the prose above the table promises three:
    # referenced, data bytes on disk, and total including metadata. The total
    # is what the "it did free metadata" qualifier rests on.
    def exp3_cells(st):
        bold = st["stage"] in ("05_optimized", "06_expired_snapshots")
        disk = (f"**{mb(st['disk_data_bytes'])}**" if bold
                else mb(st["disk_data_bytes"]))
        stranded = ("0" if st["unreferenced_bytes"] == 0
                    else (f"**{mb(st['unreferenced_bytes'])}**" if bold
                          else mb(st["unreferenced_bytes"])))
        return [stage_label[st["stage"]], str(st["snapshots"]),
                mb(st["referenced_bytes"]), disk, mb(st["disk_bytes"]),
                stranded]

    rows += block(
        "exp3",
        ["Stage", "Snapshots", "Referenced", "On disk",
         "Total incl. metadata", "Stranded"],
        [("exp3:" + st["stage"], exp3_cells(st)) for st in e3["stages"]])

    oob = e4["write_mode"]["out_of_band"]
    direct = e4["write_mode"]["direct"]
    rows += block(
        "exp4_mode",
        ["Attempt", "Property stored", "`DELETE` verdict", "Data files",
         "Delete files"],
        [("exp4:direct",
          ["`extra_properties` on `CREATE TABLE`",
           "yes" if direct["accepted"] else "refused",
           "not reached", "n/a", "n/a"]),
         ("exp4:out_of_band",
          ["`set-properties` via the REST catalog",
           "yes" if oob["properties_are_set_on_the_table"] else "no",
           oob["verdict"],
           f"+{oob['files_added']} / -{oob['files_removed']}",
           f"+{oob['delete_files_added']}"])])

    stage_label4 = {
        "00_baseline": "baseline",
        "01_after_delete_1": "after delete 1",
        "02_after_delete_2": "after delete 2",
        "03_after_delete_3": "after delete 3",
        "04_after_delete_4": "after delete 4",
        "05_after_delete_5": "after delete 5",
        "06_after_optimize": "**after optimize**",
    }
    rows += block(
        "exp4_read",
        ["stage", "data files", "delete files", "delete bytes", "full scan",
         "selective"],
        [("exp4:" + st["stage"],
          [stage_label4[st["stage"]], str(st["data_files"]),
           str(st["delete_files"]), f"{st.get('delete_bytes', 0):,}",
           f"{st['queries']['full_scan']['duration_ms_median']:.0f} ms",
           f"{st['queries']['selective']['duration_ms_median']:.0f} ms"])
         for st in e4["read_amplification"]["stages"]])
    return rows


def prose_facts():
    """Numbers that live in SENTENCES, not in tables, re-derived the same way.

    The tables are not the whole risk. A percentage inside a paragraph does
    not look like a figure the way a table row does, to a reader or to whoever
    writes the deriver, so it is the figure most likely to go unchecked and
    drift. Everything below is built from results/*.json and has to appear in
    the README verbatim.
    """
    e1, e2, e3, e4 = (load("exp1_operation_cost.json"),
                      load("exp2_partition_planning.json"),
                      load("exp3_snapshot_reclaim.json"),
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
    # After five deletes is stage 5, not stage 6. Stage 6 is after optimize,
    # where `live_rows` equals the live count only because optimize purged the
    # deleted rows, so the gap this sentence describes no longer exists there.
    # The logical count is the measured one at stage 5: what the scan handed
    # upward with the delete files applied.
    after_deletes = stages[5]
    logical_rows = after_deletes["queries"]["full_scan"]["scan_rows"]
    physical_rows = after_deletes["rows_in_data_files"]

    # Section 3's paragraph. Every figure the "it did free metadata"
    # qualifier rests on is derived here: the two totals, the object counts,
    # the bytes, and the percentage, whose denominator is what compaction
    # stranded, as the sentence says.
    peak, expired = e3["stages"][4], e3["stages"][5]
    r = e3["reclaim"]
    meta_freed = peak["disk_bytes"] - expired["disk_bytes"]
    objects_freed = peak["disk_objects"] - expired["disk_objects"]
    stranded = peak["unreferenced_bytes"]
    # Named after its own denominator, so the two percentages below cannot be
    # confused.
    share_of_stranded = f"{100 * meta_freed / stranded:.2f}%"
    reclaimed = r["reclaimed_by_expire"]
    zero_data = ("exactly zero data bytes" if reclaimed == 0
                 else f"{reclaimed:,} data bytes")
    # The other denominator is derived too. The README names it to say what
    # the figure is not, and a sentence that contrasts two numbers can drift
    # in either of them.
    share_of_peak = f"{100 * meta_freed / peak['disk_data_bytes']:.2f}%"
    a_delete = next(o for o in e1["operations"]
                    if o["operation"] == "delete_matching_rows")

    return [
        ("prose:headline_expire",
         f"`expire_snapshots` reclaimed {zero_data}"),
        ("prose:expire_snapshot_count",
         f"took the snapshot count from {r['snapshots_before']} to "
         f"{r['snapshots_after']} and reclaimed zero data bytes"),
        ("prose:expire_data_unchanged",
         f"identical to the byte, {r['peak_disk_data_bytes']:,} before and "
         f"after"),
        ("prose:orphan_reclaim",
         f"Every one of the {mb(r['reclaimed_by_orphans'])} came back only "
         f"from"),
        ("prose:expire_metadata_freed",
         f"Total on-disk went from {peak['disk_bytes']:,} to "
         f"{expired['disk_bytes']:,} and the object count from "
         f"{peak['disk_objects']} to {expired['disk_objects']}: "
         f"{meta_freed:,} bytes across {objects_freed} objects"),
        ("prose:expire_share_of_stranded",
         f"That is {share_of_stranded} of what compaction stranded"),
        ("prose:expire_share_numerator",
         f"{meta_freed:,} of {stranded:,} bytes"),
        ("prose:expire_wrong_denominator",
         f"not against the {mb(peak['disk_data_bytes'])} peak, which is a "
         f"different denominator and gives the flattering {share_of_peak}"),
        ("prose:source_table_scale",
         f"{e1['source_table']['rows']:,} rows and about "
         f"{round(e1['source_table']['bytes'] / 1e6)} MB"),
        ("prose:delete_writes_delete_bytes",
         f"writes no data bytes and {a_delete['delete_bytes_added']:,} bytes "
         f"of delete files"),
        ("prose:evolution_ratio",
         f"through evolution reads {round(evolved / native)}x more rows"),
        ("prose:day_helps_a_non_partition_predicate",
         f"the day layout reads "
         f"{round(e2['layouts']['none']['queries']['non_partition_column']['scan_rows'] / e2['layouts']['day']['queries']['non_partition_column']['scan_rows'])}x "
         f"fewer"),
        ("prose:region_rows_under_day",
         f"{e2['layouts']['day']['queries']['non_partition_column']['scan_rows']:,} "
         f"rows from "
         f"{e2['layouts']['day']['queries']['non_partition_column']['splits']} "
         f"of the 90 day partitions"),
        ("prose:delete_files",
         f"{last['data_files']} data files throughout, "
         f"{last['delete_files']} delete files at the end"),
        ("prose:logical_vs_physical",
         f"answer is {logical_rows:,} rows while the data files still "
         f"physically hold all {physical_rows:,}"),
        ("prose:full_scan_ranges",
         f"full scan ran {full_without} ms with no delete files and "
         f"{full_with} ms with them"),
        ("prose:selective_ranges",
         f"the selective query {sel_without} ms against {sel_with} ms"),
        ("prose:invisible_share",
         f"were {last['delete_bytes']:,} bytes against a reported {scan:,} "
         f"byte scan, {share}% of the read"),
    ]


def description_facts():
    """The repository description, the first thing a reader of the project
    sees.

    GITHUB_DESCRIPTION.txt is not the README, so a deriver pointed at the
    README alone cannot see it, and a qualifier added to one document does not
    reach the other. A summary surface is where a wrong figure is read by
    someone who reads nothing else.
    """
    e1, e3 = (load("exp1_operation_cost.json"),
              load("exp3_snapshot_reclaim.json"))
    reclaimed = e3["reclaim"]["reclaimed_by_expire"]
    return [
        ("desc:rows",
         f"measured on {e1['source_table']['rows'] // 1_000_000}M rows"),
        ("desc:expire",
         "really reclaims (zero data bytes; only metadata)"
         if reclaimed == 0 else
         f"really reclaims ({reclaimed:,} data bytes)"),
    ]


# The documents this script is answerable for, and the facts each one owes.
# A file that is not in this list is a file nothing re-derives.
DOCUMENTS = [
    ("README.md", lambda: expected_rows() + prose_facts()),
    ("GITHUB_DESCRIPTION.txt", description_facts),
]


def main():
    total = missing_total = 0
    for name, build in DOCUMENTS:
        path = os.path.join(ROOT, name)
        # No try/except. A missing document is not "0 figures missing": if
        # this script cannot open what it is checking it must raise, not print
        # a total over a file it never read.
        with open(path, encoding="utf-8") as fh:
            body = fh.read()
        # Whitespace is normalized on both sides. A prose figure lands wherever
        # the line wrap puts it, so matching raw text would fail on a reflowed
        # paragraph: a false alarm that trains a reader to ignore this script.
        flat = " ".join(body.split())
        checked = build()
        missing = [(tag, row) for tag, row in checked
                   if " ".join(row.split()) not in flat]
        for tag, row in missing:
            print(f"MISSING [{tag}] in {name}\n  {row}")
        total += len(checked)
        missing_total += len(missing)

    # GitHub truncates a repository description at 350 characters. A
    # description that grows past it loses its tail silently, and the tail
    # here is "Claims mapped to tests", the sentence that says the project is
    # evidence-backed at all.
    with open(os.path.join(ROOT, "GITHUB_DESCRIPTION.txt"), encoding="utf-8") as fh:
        desc_len = len(fh.read().strip())
    if desc_len > GITHUB_DESCRIPTION_LIMIT:
        print(f"TOO LONG [GITHUB_DESCRIPTION.txt] {desc_len} characters, "
              f"GitHub truncates at {GITHUB_DESCRIPTION_LIMIT}")
        missing_total += 1

    rows, facts, desc = expected_rows(), prose_facts(), description_facts()
    print(f"\n{total - missing_total} of {total} derived figures found "
          f"verbatim across {len(DOCUMENTS)} document(s) "
          f"({len(rows)} table rows, {len(facts)} in README prose, "
          f"{len(desc)} in GITHUB_DESCRIPTION.txt)")
    return 1 if missing_total else 0


if __name__ == "__main__":
    sys.exit(main())
