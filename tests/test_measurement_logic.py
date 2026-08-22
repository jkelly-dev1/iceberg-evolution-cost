"""The measurement logic, tested without Docker, Trino or MinIO.

Every function here is pure. The experiments are what need a running stack;
the reasoning that turns raw metadata into a verdict does not.

That reasoning is where a wrong answer costs most, because it produces a
plausible VERDICT rather than a crash. The tests below pin the distinctions
that decide it: data files against delete files, path sets against counts,
and three outcomes rather than two.
"""

import lab


def files(*specs):
    """(path, size, records, content) tuples -> the dict shape lab uses."""
    return {p: (size, rec, content) for p, size, rec, content in specs}


def state(data=(), deletes=(), snapshots=1):
    all_files = files(*data, *deletes)
    d = {p: v for p, v in all_files.items() if v[2] == lab.DATA}
    x = {p: v for p, v in all_files.items() if v[2] != lab.DATA}
    return {
        "files": all_files, "data_files": d, "delete_files": x,
        "file_count": len(d), "delete_file_count": len(x),
        "bytes": sum(v[0] for v in d.values()),
        "delete_bytes": sum(v[0] for v in x.values()),
        "rows": sum(v[1] for v in d.values()),
        "snapshot_count": snapshots, "snapshot_ids": list(range(snapshots)),
    }


class TestByteParsing:
    def test_reads_each_unit_trino_prints(self):
        assert lab._bytes("512B") == 512
        assert lab._bytes("702.07kB") == 702070
        assert lab._bytes("171.66MB") == 171660000

    def test_units_are_decimal_not_binary(self):
        # Trino prints kB/MB as powers of 1000. Reading them as 1024 would
        # overstate every byte figure in the results by 2.4% and upward.
        assert lab._bytes("1kB") == 1000
        assert lab._bytes("1MB") == 1000000


class TestDelta:
    def test_identical_file_sets_are_not_a_rewrite(self):
        before = state(data=[("a.parquet", 100, 10, lab.DATA)])
        after = state(data=[("a.parquet", 100, 10, lab.DATA)])
        d = lab.delta(before, after)
        assert d["rewrote_data"] is False
        assert d["files_untouched"] == 1

    def test_same_count_different_paths_is_still_a_rewrite(self):
        # The reason delta compares sets. One file in, one file out, count
        # unchanged: a count-based check calls this "no change" and is wrong.
        before = state(data=[("old.parquet", 100, 10, lab.DATA)])
        after = state(data=[("new.parquet", 100, 10, lab.DATA)])
        d = lab.delta(before, after)
        assert d["rewrote_data"] is True
        assert d["files_added"] == 1 and d["files_removed"] == 1
        assert d["files_before"] == d["files_after"] == 1

    def test_delete_files_do_not_count_as_rewritten_data(self):
        # Merge-on-read DELETE adds delete files and touches no data file.
        # Counting rows of $files without reading `content` reports that as a
        # rewrite, which is the opposite of what happened.
        before = state(data=[("a.parquet", 100, 10, lab.DATA)])
        after = state(data=[("a.parquet", 100, 10, lab.DATA)],
                      deletes=[("d.parquet", 5, 2, lab.POSITION_DELETES)])
        d = lab.delta(before, after)
        assert d["rewrote_data"] is False
        assert d["added_delete_files"] is True
        assert d["delete_files_added"] == 1
        assert d["bytes_added"] == 0

    def test_byte_totals_count_only_data_files(self):
        before = state(data=[("a.parquet", 100, 10, lab.DATA)])
        after = state(data=[("a.parquet", 100, 10, lab.DATA),
                            ("b.parquet", 250, 20, lab.DATA)],
                      deletes=[("d.parquet", 999, 2, lab.POSITION_DELETES)])
        d = lab.delta(before, after)
        assert d["bytes_added"] == 250
        assert d["bytes_after"] == 350


class TestVerdict:
    def test_no_change_is_metadata_only(self):
        assert lab.verdict({"files_removed": 0, "delete_files_added": 0,
                            "files_added": 0}) == "metadata-only"

    def test_delete_files_without_removal_is_merge_on_read(self):
        assert lab.verdict({"files_removed": 0, "delete_files_added": 3,
                            "files_added": 0}) == "merge-on-read"

    def test_removing_a_data_file_is_a_rewrite(self):
        assert lab.verdict({"files_removed": 12, "delete_files_added": 0,
                            "files_added": 1}) == "rewrite"

    def test_new_data_files_alone_are_an_append(self):
        assert lab.verdict({"files_removed": 0, "delete_files_added": 0,
                            "files_added": 2}) == "append"

    def test_rewrite_outranks_merge_on_read(self):
        # UPDATE adds delete files AND replaces data files. If a data file was
        # removed the honest word is rewrite, whatever else also happened.
        assert lab.verdict({"files_removed": 1, "delete_files_added": 9,
                            "files_added": 1}) == "rewrite"


PLAN_WITH_READ = """
    TableScan[table = iceberg:demo.t]
           Input: 6,244,438 rows (12MB), Splits: 4
           Physical input: 854.61kB, Physical input time: 20.00ms
"""

PLAN_FULLY_PRUNED = """
    TableScan[table = iceberg:demo.t]
           Input: 222222 rows (0B), Splits: 1, Splits generation wait time: 12ms
"""


class TestPlanParsing:
    """THESE CALL lab.parse_explain.

    This class used to re-declare the parser's three regexes inline and run
    them against the constants below. That checks that a regex can be written
    twice; it says nothing about the shipped parser, which produces every
    "Bytes read", "Rows scanned" and "Splits" figure in sections 2 and 4:
    including the 28x headline. Breaking any of the three in lab.py left the
    whole suite green while those figures silently became 0.
    """

    def test_reads_bytes_rows_and_splits(self):
        got = lab.parse_explain(PLAN_WITH_READ)
        assert got["physical_input_bytes"] == 854610
        assert got["physical_input_reported"] is True
        assert got["scan_rows"] == 6244438
        assert got["splits"] == 4

    def test_a_fully_pruned_scan_reports_no_physical_input_line(self):
        # Absent is not a parse failure. When a count is answered from
        # manifest and row-group metadata the plan carries no "Physical input"
        # line at all, and the scan itself prints "(0B)". Treating that as a
        # broken parse would have hidden the most favorable result in
        # experiment 2, so the flag records which case produced the zero.
        got = lab.parse_explain(PLAN_FULLY_PRUNED)
        assert got["physical_input_bytes"] == 0
        assert got["physical_input_reported"] is False, (
            "a pruned scan must be distinguishable from a failed parse")
        assert got["scan_rows"] == 222222
        assert got["splits"] == 1

    def test_the_largest_node_is_taken_not_the_first_or_the_sum(self):
        """A plan has several scan nodes. Taking the first, the smallest or
        the sum each produces a plausible wrong number from the same text."""
        two_nodes = """
            TableScan[a]  Input: 100 rows (1MB), Splits: 2
                          Physical input: 1.00kB
            TableScan[b]  Input: 6,244,438 rows (12MB), Splits: 4
                          Physical input: 854.61kB
        """
        got = lab.parse_explain(two_nodes)
        assert got["scan_rows"] == 6244438
        assert got["splits"] == 4
        assert got["physical_input_bytes"] == 854610

    def test_an_unparseable_plan_reports_zero_and_says_it_did_not_parse(self):
        got = lab.parse_explain("Query 20260820_013900 failed: no such table")
        assert got == {"physical_input_bytes": 0, "physical_input_reported": False,
                       "scan_rows": 0, "splits": 0}
