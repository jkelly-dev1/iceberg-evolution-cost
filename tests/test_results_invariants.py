"""Tests over the RESULTS THAT SHIP, not over a fresh run.

results/*.json is evidence committed to the repository. These tests assert two
different kinds of thing about it:

  INTERNAL CONSISTENCY: the numbers in a file agree with each other. A file
  that says 383 MB is stranded while its own disk and referenced figures
  differ by something else is corrupt, however plausible the headline reads.

  THE FINDINGS THEMSELVES: the qualitative results the README claims. These
  are the run-invariant parts. Exact byte totals are not asserted: Parquet
  file sizes move by a few hundredths of a percent between runs depending on
  write parallelism, and a test that pinned them would fail for a reason that
  means nothing.
"""

import json
import os

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

    def test_optimize_is_the_only_true_rewrite(self, exp1):
        rewrites = [o for o in exp1["operations"] if o["verdict"] == "rewrite"]
        assert [o["operation"] for o in rewrites] == ["execute_optimize"]
        assert rewrites[0]["files_removed"] > 0

    def test_two_predictions_were_refuted_and_are_recorded_as_such(self, exp1):
        # The refuted predictions are the finding. If a later edit "fixes" the
        # predictions to match the measurements, this goes red on purpose.
        refuted = [o["operation"] for o in exp1["operations"]
                   if not o["prediction_held"]]
        assert refuted == ["delete_matching_rows", "update_matching_rows"]


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

    def test_partitioning_does_not_help_a_non_partition_predicate(self, exp2):
        none_rows = exp2["layouts"]["none"]["queries"]["non_partition_column"]["scan_rows"]
        month_rows = exp2["layouts"]["month"]["queries"]["non_partition_column"]["scan_rows"]
        assert month_rows == none_rows

    def test_the_alter_itself_moved_no_data(self, exp2):
        assert exp2["evolved_partition_change"]["alter_rewrote_data"] is False


class TestSnapshotReclaim:
    def test_expire_snapshots_is_refused_by_default(self, exp3):
        assert exp3["default_expire_refused"] is True
        assert "minimum retention" in exp3["default_expire_error"].lower()

    def test_expiring_snapshots_reclaims_nothing(self, exp3):
        # The surprise. Snapshots go from 5 to 1 and not one byte of data
        # comes back. This is exactly 0, in every run, so it is asserted
        # exactly rather than as a bound.
        assert exp3["reclaim"]["reclaimed_by_expire"] == 0
        assert exp3["reclaim"]["snapshots_before"] > exp3["reclaim"]["snapshots_after"]

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
        for s in exp3["stages"]:
            assert (s["unreferenced_bytes"]
                    == s["disk_data_bytes"] - s["referenced_bytes"])
            assert s["disk_bytes"] >= s["disk_data_bytes"]

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
        stages = exp4["read_amplification"]["stages"]
        assert stages[0]["live_rows"] == 20_000_000
        assert stages[5]["rows_in_data_files"] == 20_000_000
        assert stages[6]["live_rows"] == 19_000_000

    def test_physical_input_is_blind_to_delete_files(self, exp4):
        # A MEASUREMENT TRAP, kept as a claim. "Physical input" counts the
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
        # It is NOT proportional to delete file count at this scale. Going
        # from 7 delete files to 35 does not multiply the cost by five, and
        # the intuitive model says it should.
        stages = exp4["read_amplification"]["stages"]
        first = stages[1]["queries"]["full_scan"]["duration_ms_median"]
        fifth = stages[5]["queries"]["full_scan"]["duration_ms_median"]
        assert fifth < first * 2

    def test_optimize_returns_the_read_to_baseline(self, exp4):
        stages = exp4["read_amplification"]["stages"]
        base = stages[0]["queries"]["full_scan"]["duration_ms_median"]
        after = stages[6]["queries"]["full_scan"]["duration_ms_median"]
        assert stages[6]["delete_files"] == 0
        assert after < base * 1.2


class TestReadmeAgreesWithResults:
    def test_every_readme_number_is_derived_from_the_results(self):
        # The README is prose and drifts; results/*.json is evidence and does
        # not. This rebuilds each figure from the JSON and requires the exact
        # string in the README, so a re-run that shifts a byte figure fails
        # here instead of leaving the document quietly wrong.
        #
        # Prose is covered as well as table rows. A percentage inside a
        # sentence does not look like a figure the way a table row does, so it
        # is the kind most likely to go unchecked and drift out of agreement
        # with the results it came from.
        import check_readme_numbers as chk
        with open(os.path.join(ROOT, "README.md"), encoding="utf-8") as fh:
            readme = fh.read()
        flat = " ".join(readme.split())
        checked = chk.expected_rows() + chk.prose_facts()
        missing = [tag for tag, row in checked
                   if " ".join(row.split()) not in flat]
        assert missing == []
        # Count what ran, not only what passed: a version of the deriver that
        # silently stopped producing half its figures would otherwise be clean.
        assert len(chk.prose_facts()) >= 6
