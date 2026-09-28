"""Tests over the results that ship, not over a fresh run.

results/*.json is evidence committed to the repository. These tests assert two
different kinds of thing about it:

  Internal consistency: the numbers in a file agree with each other. A file
  that says 383 MB is stranded while its own disk and referenced figures
  differ by something else is corrupt, however plausible the headline reads.

  The findings themselves: the qualitative results the README claims. These
  are the run-invariant parts. Exact byte totals are not asserted: Parquet
  file sizes move by a few hundredths of a percent between runs depending on
  write parallelism, and a test that pinned them would fail for a reason that
  means nothing.
"""

import ast
import json
import os
import re

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def load(name):
    with open(os.path.join(ROOT, "results", name), encoding="utf-8") as fh:
        return json.load(fh)


@pytest.fixture(scope="module")
def exp1():
    return load("exp1_operation_cost.json")


@pytest.fixture(scope="module")
def exp2():
    return load("exp2_partition_planning.json")


@pytest.fixture(scope="module")
def exp3():
    return load("exp3_snapshot_reclaim.json")


@pytest.fixture(scope="module")
def exp4():
    return load("exp4_delete_cost.json")


def op(exp1, name):
    return next(o for o in exp1["operations"] if o["operation"] == name)


METADATA_ONLY = ["add_column", "rename_column", "drop_column",
                 "widen_type_int_to_bigint", "set_partitioning"]


class TestOperationCost:
    @pytest.mark.parametrize("name", METADATA_ONLY)
    def test_schema_changes_touch_no_file(self, exp1, name):
        o = op(exp1, name)
        assert o["verdict"] == "metadata-only"
        assert o["files_added"] == 0 and o["files_removed"] == 0
        assert o["delete_files_added"] == 0
        assert o["bytes_added"] == 0 and o["bytes_removed"] == 0

    @pytest.mark.parametrize("name", METADATA_ONLY)
    def test_metadata_changes_preserve_every_row(self, exp1, name):
        o = op(exp1, name)
        assert o["rows_before"] == o["rows_after"]

    def test_partition_change_is_metadata_only(self, exp1):
        # The headline of experiment 2 depends on this: changing the partition
        # spec does NOT rewrite the files already written under the old one.
        o = op(exp1, "set_partitioning")
        assert o["files_untouched"] == o["files_before"]

    def test_delete_does_not_rewrite_data(self, exp1):
        # Merge-on-read. Every original data file survives a DELETE.
        o = op(exp1, "delete_matching_rows")
        assert o["verdict"] == "merge-on-read"
        assert o["files_removed"] == 0
        assert o["delete_files_added"] > 0

    def test_a_merge_on_read_write_still_writes_bytes(self, exp1):
        # Merge-on-read defers the write; it does not skip it, and that is the
        # finding of section 4. A DELETE writes no data bytes and megabytes of
        # delete files, so delete_bytes_added is what shows the deferred work.
        for name in ("delete_matching_rows", "update_matching_rows"):
            o = op(exp1, name)
            assert o["verdict"] == "merge-on-read"
            assert o["delete_files_added"] > 0
            assert o["delete_bytes_added"] > 0, (
                f"{name} added {o['delete_files_added']} delete files and "
                f"zero bytes, which cannot both be true")
        # The DELETE is the sharpest case: no data bytes, real delete bytes.
        d = op(exp1, "delete_matching_rows")
        assert d["bytes_added"] == 0 and d["delete_bytes_added"] > 0

    def test_optimize_is_the_only_true_rewrite(self, exp1):
        rewrites = [o for o in exp1["operations"] if o["verdict"] == "rewrite"]
        assert [o["operation"] for o in rewrites] == ["execute_optimize"]
        assert rewrites[0]["files_removed"] > 0

    def test_two_predictions_were_refuted_and_are_recorded_as_such(self, exp1):
        # The refuted predictions are the finding. An edit that "fixes" the
        # predictions to match the measurements turns this red.
        refuted = [o["operation"] for o in exp1["operations"]
                   if not o["prediction_held"]]
        assert refuted == ["delete_matching_rows", "update_matching_rows"]


class TestSourceTable:
    """The table every other experiment copies from.

    README's "about 458 MB" comes from its byte figure. The recorded run
    carries these counts without having read them off the table
    (`measured_from` is null), so these are cross-checks that hold for a run
    that did read them and for one that did not.
    """

    def test_the_run_says_whether_it_measured_the_source_table(self, exp1):
        # A figure whose provenance is unrecorded reads exactly like a
        # measured one. The key names the table instead of carrying a
        # boolean, so it cannot itself be a constant asserting a measurement.
        src = exp1["source_table"]
        assert "measured_from" in src
        assert src["measured_from"] is None or (
            isinstance(src["measured_from"], str) and "." in src["measured_from"])

    def test_the_source_row_count_agrees_with_every_copy_of_it(self, exp1):
        # Each operation runs on a FRESH CTAS copy of the source, so every
        # rows_before in the file is an independent reading of the same table.
        # A source row count that disagrees with all of them is not describing
        # this run's table.
        src = exp1["source_table"]["rows"]
        copies = {o["rows_before"] for o in exp1["operations"]
                  if o.get("supported")}
        assert copies == {src}, (
            f"source_table says {src:,} rows; its own copies say "
            f"{sorted(copies)}")

    def test_the_source_byte_total_is_in_range_of_its_own_copies(self, exp1):
        # Not an equality: a CTAS copy repacks, so the copies run a couple of
        # percent above the source. A bound still catches the failure that
        # matters: a stale or invented figure is off by far more than that.
        src = exp1["source_table"]["bytes"]
        copies = [o["bytes_before"] for o in exp1["operations"]
                  if o.get("supported")]
        assert src > 0 and copies
        assert 0.8 < src / min(copies) < 1.25, (
            f"source_table says {src:,} bytes against copies of "
            f"{min(copies):,}-{max(copies):,}")

    def test_the_source_file_count_is_plausible_for_its_byte_total(self, exp1):
        # The file count moves between runs, as SAMPLE_RUN says, so the
        # assertion is that it is a real file count for this byte total.
        src = exp1["source_table"]
        assert src["files"] >= 1
        assert src["bytes"] / src["files"] > 1_000_000


class TestPartitionPlanning:
    def test_day_partitioning_prunes_a_single_day_query_to_one_split(self, exp2):
        q = exp2["layouts"]["day"]["queries"]["single_day"]
        assert q["splits"] == 1
        assert q["physical_input_reported"] is False
        assert q["scan_rows"] < 250000

    def test_evolution_leaves_old_files_under_the_old_spec(self, exp2):
        specs = exp2["layouts"]["evolved"]["partition_specs"]
        assert len({s["spec_id"] for s in specs}) == 2

    def test_evolved_table_scans_far_more_than_a_native_day_table(self, exp2):
        # The result this experiment exists for. Both tables are "partitioned
        # by day". The evolved one still reads a whole month of the old files,
        # because a partition spec change does not rewrite them.
        native = exp2["layouts"]["day"]["queries"]["single_day"]["scan_rows"]
        evolved = exp2["layouts"]["evolved"]["queries"]["single_day"]["scan_rows"]
        assert evolved > native * 10

    def test_month_partitioning_does_not_help_a_non_partition_predicate(self, exp2):
        # The name says "month" because the assertion is about month. The day
        # layout does help, for the reason the next test pins, and the README
        # cites this name beside the claim.
        none_rows = exp2["layouts"]["none"]["queries"]["non_partition_column"]["scan_rows"]
        month_rows = exp2["layouts"]["month"]["queries"]["non_partition_column"]["scan_rows"]
        assert month_rows == none_rows

    def test_the_day_layout_helps_only_because_the_generator_correlates_them(
            self, exp2):
        # The result the README reads as a generator artifact. `region` is
        # `id % 6` and `event_date` is `id % 90` in sql/generate.sql; 6 divides
        # 90, so every row on a given day carries the same region and a region
        # predicate prunes to the days that hold it. The pruning is real and
        # says nothing about Iceberg.
        none_q = exp2["layouts"]["none"]["queries"]["non_partition_column"]
        day_q = exp2["layouts"]["day"]["queries"]["non_partition_column"]
        assert day_q["scan_rows"] < none_q["scan_rows"]
        # Six regions, so a sixth of the rows and a sixth of the 90 days.
        assert round(none_q["scan_rows"] / day_q["scan_rows"]) == 6
        assert day_q["splits"] == 90 // 6
        # The day table holds 90 day partitions to prune from.
        assert exp2["layouts"]["day"]["partition_specs"][0]["files"] == 90

    def test_the_alter_itself_moved_no_data(self, exp2):
        # The recorded run did not isolate the ALTER from the INSERT after it,
        # so this asserts the evidence the run does contain: the evolved table
        # holds, under the original spec id, exactly the file count it had
        # before the ALTER. Had the ALTER rewritten anything, those files
        # would have been replaced under the new spec.
        change = exp2["evolved_partition_change"]
        specs = exp2["layouts"]["evolved"]["partition_specs"]
        original = min(specs, key=lambda sp: sp["spec_id"])
        assert original["files"] == change["files_before_alter"], (
            "files under the original spec no longer match the count taken "
            "before the ALTER, so something rewrote them")
        assert len(specs) == 2 and original["files"] > 0

        # The run has to say whether it isolated the ALTER. A results file
        # without the marker is not one that measured it.
        assert "alter_isolated" in change
        if change["alter_isolated"]:
            # A run that isolated it must report the measurement, and the
            # measurement must be the one the README claims.
            assert change["alter_rewrote_data"] is False
            assert change["alter_files_added"] == 0
            assert change["alter_files_removed"] == 0
            assert change["files_after_alter"] == change["files_before_alter"]
        else:
            # A run that did not must not carry a verdict it could not have
            # reached.
            assert "alter_rewrote_data" not in change


class TestSnapshotReclaim:
    def test_expire_snapshots_is_refused_by_default(self, exp3):
        assert exp3["default_expire_refused"] is True
        assert "minimum retention" in exp3["default_expire_error"].lower()

    def test_expiring_snapshots_reclaims_no_data_bytes(self, exp3):
        # The surprise. Snapshots go from 5 to 1 and not one byte of DATA
        # comes back. This is exactly 0, in every run, so it is asserted
        # exactly rather than as a bound.
        #
        # The name says "no data bytes" and not "nothing": expiry does free
        # metadata, and the README cites this name beside the claim.
        assert exp3["reclaim"]["reclaimed_by_expire"] == 0
        assert exp3["reclaim"]["snapshots_before"] > exp3["reclaim"]["snapshots_after"]

    def test_expiring_snapshots_frees_metadata_and_says_so(self, exp3):
        # The other half of the same claim. "It did free metadata" is an
        # assertion about the total column: data bytes stand still across the
        # expiry, and the total does not.
        stages = {s["stage"]: s for s in exp3["stages"]}
        peak, after = stages["05_optimized"], stages["06_expired_snapshots"]
        assert after["disk_data_bytes"] == peak["disk_data_bytes"]
        assert after["disk_bytes"] < peak["disk_bytes"], (
            "expiry freed no metadata either, so the qualifier the README "
            "spends a paragraph on describes nothing")
        assert after["disk_objects"] < peak["disk_objects"]
        # What it freed is small enough that the data column is the one to
        # read, which is the comparison the README's percentage makes.
        freed = peak["disk_bytes"] - after["disk_bytes"]
        assert freed < peak["unreferenced_bytes"] / 100

    def test_orphan_removal_is_what_actually_frees_the_space(self, exp3):
        r = exp3["reclaim"]
        assert r["reclaimed_by_orphans"] > 0
        assert r["reclaimed_by_orphans"] == r["stranded_at_peak"]
        assert r["still_stranded"] == 0

    def test_compaction_is_what_strands_the_bytes(self, exp3):
        stages = {s["stage"]: s for s in exp3["stages"]}
        assert stages["04_deleted"]["unreferenced_bytes"] < 10_000_000
        assert stages["05_optimized"]["unreferenced_bytes"] > 300_000_000

    def test_every_stage_is_internally_consistent(self, exp3):
        # stranded is a derived number; if it ever disagrees with its own
        # inputs the file is corrupt no matter how good the headline looks.
        # Rows written before exp3 recorded referenced delete bytes carry no
        # such key; for them the gap includes live delete files (see
        # test_the_gap_before_compaction_is_live_delete_files).
        for s in exp3["stages"]:
            assert (s["unreferenced_bytes"]
                    == s["disk_data_bytes"] - s["referenced_bytes"]
                    - s.get("referenced_delete_bytes", 0))
            assert s["disk_bytes"] >= s["disk_data_bytes"]

    def test_the_gap_before_compaction_is_live_delete_files(self, exp3):
        # In the shipped run, the gap at the update and delete stages is not
        # history: the referenced data files only grew from the append, and
        # the current snapshot carries delete files. README labels that
        # column for what it is and says so; this pins the facts it states.
        stages = {s["stage"]: s for s in exp3["stages"]}
        appended = stages["02_appended"]
        for name in ("03_updated", "04_deleted"):
            s = stages[name]
            assert "referenced_delete_bytes" not in s
            assert s["delete_files"] > 0
            assert s["unreferenced_bytes"] > 0
            assert (s["referenced_data_files"]
                    >= appended["referenced_data_files"])
            assert s["referenced_bytes"] >= appended["referenced_bytes"]

    def test_the_live_data_is_never_touched(self, exp3):
        # Reclaiming space must not cost rows. Referenced bytes are identical
        # before and after both procedures.
        stages = {s["stage"]: s for s in exp3["stages"]}
        assert (stages["05_optimized"]["referenced_bytes"]
                == stages["06_expired_snapshots"]["referenced_bytes"]
                == stages["07_removed_orphans"]["referenced_bytes"])


class TestDeleteCost:
    """Experiment 4: who decides the write mode, and what the reader pays."""

    def test_trino_refuses_the_write_mode_property(self, exp4):
        # Trino exposes no delete_mode property, and it will not carry the
        # spec one through extra_properties either. It rejects the key.
        d = exp4["write_mode"]["direct"]
        assert d["accepted"] is False
        assert "illegal keys" in d["error"].lower()
        assert "write.delete.mode" in d["error"]

    def test_the_property_really_is_set_on_the_table(self, exp4):
        # Without this the next test proves nothing: it has to be established
        # that the table genuinely carries copy-on-write before concluding
        # anything from Trino not acting on it.
        o = exp4["write_mode"]["out_of_band"]
        assert o["http_status"] == 200
        assert o["properties_are_set_on_the_table"] is True
        assert o["properties_stored"]["write.delete.mode"] == "copy-on-write"

    def test_trino_ignores_a_copy_on_write_table(self, exp4):
        # The finding. The table says copy-on-write, in the same catalog Trino
        # reads, and Trino writes a position delete file anyway. The engine
        # decides the write mode here, not the table property.
        o = exp4["write_mode"]["out_of_band"]
        assert o["honored"] is False
        assert o["verdict"] == "merge-on-read"
        assert o["files_removed"] == 0
        assert o["delete_files_added"] > 0

    def test_each_delete_adds_one_delete_file_per_data_file(self, exp4):
        stages = exp4["read_amplification"]["stages"][:6]
        data_files = stages[0]["data_files"]
        for i, st in enumerate(stages[1:], start=1):
            assert st["delete_files"] == data_files * i

    def test_no_delete_ever_rewrites_a_data_file(self, exp4):
        stages = exp4["read_amplification"]["stages"][:6]
        assert len({st["data_files"] for st in stages}) == 1

    def test_the_rows_are_logically_gone_and_physically_present(self, exp4):
        # Five deletes removed a million rows from the answer and not one row
        # from the files. That gap is exactly what the reader pays for.
        #
        # Both halves are taken at stage 5, which is what "after five deletes"
        # means. The logical half is the measured scan count, what the scan
        # handed upward with the deletes applied. Stage 6 is after optimize,
        # where the gap no longer exists.
        stages = exp4["read_amplification"]["stages"]
        baseline, after_deletes, after_optimize = stages[0], stages[5], stages[6]

        assert baseline["queries"]["full_scan"]["scan_rows"] == 20_000_000
        assert after_deletes["rows_in_data_files"] == 20_000_000
        for query in ("full_scan", "selective"):
            assert after_deletes["queries"][query]["scan_rows"] == 19_000_000, (
                f"{query} still answers 20,000,000 after five deletes, so the "
                f"rows are not logically gone")
        # The gap itself, stated as a gap and at one stage.
        assert (after_deletes["rows_in_data_files"]
                - after_deletes["queries"]["full_scan"]["scan_rows"]
                == 1_000_000)
        # Optimize is where the physical count finally catches up.
        assert after_optimize["live_rows"] == 19_000_000

    def test_every_delete_moves_the_logical_count_and_not_the_physical(self, exp4):
        # The same gap, opened one delete at a time, so that a results file
        # in which the deletes did nothing logically cannot pass. Each round
        # removes about 1% of the table.
        stages = exp4["read_amplification"]["stages"][:6]
        scanned = [st["queries"]["full_scan"]["scan_rows"] for st in stages]
        assert scanned == sorted(scanned, reverse=True)
        assert len(set(scanned)) == len(scanned), (
            "two rounds answer the same row count, so at least one delete "
            "removed nothing from the answer")
        for st in stages[1:]:
            assert st["rows_in_data_files"] == 20_000_000

    def test_physical_input_is_blind_to_delete_files(self, exp4):
        # A measurement trap, kept as a claim. "Physical input" counts the
        # data scan and not the delete files merged on top, so it reports the
        # same number at zero delete files and at thirty-five. Anyone
        # measuring merge-on-read read cost with it concludes deletes are
        # free, and the timing test below shows they are not.
        stages = exp4["read_amplification"]["stages"][:6]
        reported = {st["queries"]["full_scan"]["physical_input_bytes"]
                    for st in stages}
        assert len(reported) == 1

    def test_delete_file_bytes_are_a_large_invisible_share_of_the_read(self, exp4):
        st = exp4["read_amplification"]["stages"][5]
        scan = st["queries"]["full_scan"]["physical_input_bytes"]
        assert st["delete_bytes"] > scan * 0.5

    @pytest.mark.parametrize("query", ["full_scan", "selective"])
    def test_every_run_with_delete_files_is_slower_than_every_run_without(
            self, exp4, query):
        # The timing claim, stated the only way this clock can support it.
        # Wall time covers a whole docker exec, so no single pair of samples
        # means anything. What does mean something: across 7 repeats per
        # stage, the ranges do not overlap at all.
        stages = exp4["read_amplification"]["stages"]
        clean = [ms for st in stages if st["delete_files"] == 0
                 for ms in st["queries"][query]["duration_ms_samples"]]
        dirty = [ms for st in stages if st["delete_files"] > 0
                 for ms in st["queries"][query]["duration_ms_samples"]]
        assert min(dirty) > max(clean)

    def test_the_cost_appears_at_the_first_delete_and_then_plateaus(self, exp4):
        # It is not proportional to delete file count at this scale: five
        # times the delete files does not multiply the cost by five, and the
        # intuitive model says it should.
        # "Appears in full, then flattens": everything after the first
        # delete moves less than the first delete did, on both queries.
        stages = exp4["read_amplification"]["stages"]
        for query in ("full_scan", "selective"):
            med = [st["queries"][query]["duration_ms_median"]
                   for st in stages[:6]]
            step = med[1] - med[0]
            assert step > 0
            assert max(abs(m - med[1]) for m in med[2:]) < step, query

    def test_optimize_returns_the_read_to_baseline(self, exp4):
        stages = exp4["read_amplification"]["stages"]
        assert stages[6]["delete_files"] == 0
        # Both queries, as README says: back inside the range of the clean
        # baseline runs, below every run with delete files present.
        for query in ("full_scan", "selective"):
            base = stages[0]["queries"][query]["duration_ms_median"]
            after = stages[6]["queries"][query]["duration_ms_median"]
            dirty = [ms for st in stages[1:6]
                     for ms in st["queries"][query]["duration_ms_samples"]]
            assert after < base * 1.2, query
            assert after < min(dirty), query


class TestReadmeAgreesWithResults:
    def test_every_shipped_number_is_derived_from_the_results(self):
        # The documents are prose and drift; results/*.json is evidence and
        # does not. This rebuilds each figure from the JSON and requires the
        # exact string in the document, so a re-run that shifts a byte figure
        # fails here instead of leaving it silently wrong.
        #
        # Prose is covered as well as table rows. A percentage inside a
        # sentence does not look like a figure the way a table row does, so it
        # is the kind most likely to drift from the results it came from.
        #
        # Every document the deriver answers for is read, not only the README:
        # GITHUB_DESCRIPTION.txt is the summary a reader meets first.
        import check_readme_numbers as chk
        for name, build in chk.DOCUMENTS:
            with open(os.path.join(ROOT, name), encoding="utf-8") as fh:
                flat = " ".join(fh.read().split())
            missing = [tag for tag, row in build()
                       if " ".join(row.split()) not in flat]
            assert missing == [], f"{name} is missing {missing}"
        # Count what ran, not only what passed: a version of the deriver that
        # silently stopped producing half its figures would otherwise be clean.
        assert len(chk.prose_facts()) >= 15
        # The figures a README sentence repeats or explains are rows of their
        # own; the floor above would not notice one of them going.
        tags = {tag for tag, _ in chk.prose_facts()}
        assert {"prose:generator_moduli", "prose:moduli_in_words",
                "prose:region_prunes_to_days", "prose:per_delete_share",
                "prose:claims_physical_input",
                "prose:scale_named_again"} <= tags, tags
        assert len(chk.description_facts()) >= 2
        assert [n for n, _ in chk.DOCUMENTS] == ["README.md",
                                                 "GITHUB_DESCRIPTION.txt"]

    def test_the_github_description_fits_what_github_stores(self):
        # GitHub stores 350 characters and shows nothing past them. The tail
        # is the sentence saying the claims are mapped to tests, so silent
        # truncation costs the part that makes the summary worth trusting.
        with open(os.path.join(ROOT, "GITHUB_DESCRIPTION.txt"),
                  encoding="utf-8") as fh:
            body = fh.read().strip()
        import check_readme_numbers as chk
        assert len(body) <= chk.GITHUB_DESCRIPTION_LIMIT, (
            f"{len(body)} characters; GitHub truncates at "
            f"{chk.GITHUB_DESCRIPTION_LIMIT}")


class TestTheClaimsTableIsHonest:
    """The README's "Claims backed by tests" table cites test node ids of the
    form `tests/<module>.py::<Class>::<test_name>`, and each must exist.

    A rename is the ordinary edit that breaks a citation, and a claim pointing
    at a test that no longer exists renders exactly like one that has
    evidence behind it.

    The bound: this reads the test files with `ast` and checks that the
    module, the class and the function are defined. It does not run a
    collector, so it cannot see a node that exists but is not collected.
    Every class here is named Test* and every function test_*, which is what
    pytest collects by default; a class renamed out of that convention would
    pass this and fail to run.
    """

    CITATION = re.compile(r"`(tests/[a-z_]+\.py)((?:::[A-Za-z_][A-Za-z_0-9]*)+)`")
    CONTINUATION = re.compile(r"`((?:::[A-Za-z_][A-Za-z_0-9]*)+)`")

    def _readme(self):
        with open(os.path.join(ROOT, "README.md"), encoding="utf-8") as fh:
            return fh.read()

    def _defined(self):
        """{(module, class, function)} actually defined under tests/."""
        found = set()
        tests_dir = os.path.join(ROOT, "tests")
        for name in sorted(os.listdir(tests_dir)):
            if not name.startswith("test_") or not name.endswith(".py"):
                continue
            with open(os.path.join(tests_dir, name), encoding="utf-8") as fh:
                tree = ast.parse(fh.read())
            for node in tree.body:
                if not isinstance(node, ast.ClassDef):
                    continue
                for sub in node.body:
                    if isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        found.add((f"tests/{name}", node.name, sub.name))
        return found

    def _cited(self):
        """Every (module, class, function) the README points at.

        A row may cite several nodes, and the later ones are written as
        `::Class::test_name` with the module left off, so a citation is
        resolved against the last full one seen, exactly as a reader resolves
        it. Dropping those would leave a third of the table unchecked while
        this test reported a clean pass.
        """
        cited, module = [], None
        for line in self._readme().splitlines():
            if "tests/" not in line and "::" not in line:
                continue
            for match in self.CITATION.finditer(line):
                module = match.group(1)
                parts = match.group(2).strip(":").split("::")
                cited.append((module, parts[0], parts[-1]))
            for match in self.CONTINUATION.finditer(line):
                parts = match.group(1).strip(":").split("::")
                if len(parts) == 2 and module:
                    cited.append((module, parts[0], parts[1]))
        return cited

    def test_every_test_the_readme_cites_exists(self):
        cited, defined = self._cited(), self._defined()
        phantom = sorted(set(cited) - defined)
        assert phantom == [], f"README cites tests that do not exist: {phantom}"

    def test_the_citation_parser_still_finds_the_whole_table(self):
        # Count what was examined. A regex that stopped matching would make
        # the test above pass over an empty set, which is the shape of a check
        # that reports on a target it never looked at.
        cited = self._cited()
        assert len(cited) >= 35, f"only {len(cited)} citations parsed"
        assert len({c[0] for c in cited}) == 2, "both test modules cited"

    def test_every_section_the_readme_points_at_exists(self):
        # A cross reference names a heading, so it can be checked.
        body = self._readme()
        headings = {line.lstrip("#").strip()
                    for line in body.splitlines() if line.startswith("#")}
        pointed = re.findall(r'\(section \d+, "([^"]+)"\)', body)
        assert pointed, "no section cross-reference found to check"
        for title in pointed:
            assert any(h.startswith(title) for h in headings), (
                f"README points at a section titled {title!r} and no heading "
                f"begins with it")

    def test_no_claims_row_defers_to_an_unnamed_place(self):
        # "See below" in a table row cannot be resolved by a reader: a row has
        # no below.
        for line in self._readme().splitlines():
            if line.startswith("|"):
                assert "see below" not in line.lower(), line
                assert "see above" not in line.lower(), line
