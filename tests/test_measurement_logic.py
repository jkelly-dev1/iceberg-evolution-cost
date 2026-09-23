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

    def test_rewrote_data_and_the_verdict_mean_different_things(self):
        """Two definitions of one word, pinned together so neither moves alone.

        `delta()["rewrote_data"]` is True when the data file set changed
        identity, so a bare append sets it. `verdict()` reserves "rewrite" for
        a data file being removed and replaced. SAMPLE_RUN.md prints the first
        for the UPDATE and the README's table prints the second, and each
        document says which it means. This test fails if either definition
        drifts onto the other.
        """
        before = state(data=[("a.parquet", 100, 10, lab.DATA)])

        appended = lab.delta(before, state(
            data=[("a.parquet", 100, 10, lab.DATA),
                  ("b.parquet", 250, 20, lab.DATA)]))
        assert appended["rewrote_data"] is True     # the file set changed
        assert lab.verdict(appended) == "append"    # nothing was removed

        # The shape the UPDATE actually took: a new data file, delete files,
        # and not one original data file gone.
        updated = lab.delta(before, state(
            data=[("a.parquet", 100, 10, lab.DATA),
                  ("b.parquet", 250, 20, lab.DATA)],
            deletes=[("d.parquet", 9, 2, lab.POSITION_DELETES)]))
        assert updated["rewrote_data"] is True
        assert lab.verdict(updated) == "merge-on-read"

        # Where the two agree: a real rewrite.
        replaced = lab.delta(before,
                             state(data=[("c.parquet", 100, 10, lab.DATA)]))
        assert replaced["rewrote_data"] is True
        assert lab.verdict(replaced) == "rewrite"

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
    """These call lab.parse_explain itself on captured plan text.

    The shipped parser produces every "Bytes read", "Rows scanned" and
    "Splits" figure in sections 2 and 4, including the 28x headline, so the
    tests exercise it directly instead of a copy of its regexes.
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
        # broken parse would hide the most favorable result in experiment 2,
        # so the flag records which case produced the zero.
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


class TestTheEvolvedBuildIsolatesTheAlter:
    """exp2's `build_evolved` needs Trino, so its sequencing is pinned here
    with lab's functions patched.

    The experiment's claim is that the `ALTER` which changed the partition
    spec rewrote no data. Observing that requires a state taken between the
    ALTER and the INSERT that follows it: an INSERT always adds files, so a
    delta spanning both cannot tell which statement added them.

    lab's functions are patched on the `lab` module object, which is the
    binding `exp2_partition_planning` resolves: it does `import lab` and
    calls `lab.sql`. Patching a local copy would leave the real functions in
    place and this test would reach Docker.
    """

    def _trace(self, monkeypatch):
        import exp2_partition_planning as exp2

        calls = []

        def fake_sql(statement, fmt="JSON"):
            calls.append(("sql", " ".join(statement.split())))
            return []

        def fake_state(name):
            calls.append(("state", name))
            return {"file_count": len(calls), "delete_file_count": 0,
                    "data_files": {}, "delete_files": {}, "files": {},
                    "bytes": 0, "delete_bytes": 0, "rows": 0,
                    "snapshot_count": 1, "snapshot_ids": [1]}

        monkeypatch.setattr(lab, "sql", fake_sql)
        monkeypatch.setattr(lab, "state", fake_state)
        returned = exp2.build_evolved()
        return calls, returned

    def test_a_state_is_taken_between_the_alter_and_the_insert(self, monkeypatch):
        calls, _ = self._trace(monkeypatch)
        kinds = [k for k, _ in calls]
        assert kinds.count("state") == 3, (
            f"build_evolved took {kinds.count('state')} states; three are "
            f"needed to isolate the ALTER from the INSERT")

        at = [i for i, (k, _) in enumerate(calls) if k == "state"]
        alter = next(i for i, (k, v) in enumerate(calls)
                     if k == "sql" and "SET PROPERTIES" in v)
        insert = next(i for i, (k, v) in enumerate(calls)
                      if k == "sql" and v.startswith("INSERT INTO"))
        assert at[0] < alter, "the first state must precede the ALTER"
        assert alter < at[1] < insert, (
            "the middle state must fall between the ALTER and the INSERT, or "
            "the two are measured together and the ALTER is never isolated")
        assert insert < at[2], "the last state must follow the INSERT"

    def test_it_hands_back_the_state_that_isolates_the_alter(self, monkeypatch):
        # Taking the state and then dropping it on the floor would satisfy the
        # ordering test above while leaving main() nothing to measure with.
        _, returned = self._trace(monkeypatch)
        assert len(returned) == 4
        _, before, after_alter, after = returned
        assert before is not after_alter is not after
        assert after_alter is not after

    def test_the_alter_really_does_precede_the_insert(self, monkeypatch):
        # The ordering the experiment's whole story rests on: the spec is
        # changed first, and the rows inserted afterwards land under the new
        # spec while the old files keep the old one.
        calls, _ = self._trace(monkeypatch)
        statements = [v for k, v in calls if k == "sql"]
        alter = next(i for i, v in enumerate(statements)
                     if "SET PROPERTIES" in v)
        insert = next(i for i, v in enumerate(statements)
                      if v.startswith("INSERT INTO"))
        create = next(i for i, v in enumerate(statements)
                      if v.startswith("CREATE TABLE"))
        assert create < alter < insert
        assert "month(event_date)" in statements[create]
        assert "day(event_date)" in statements[alter]
