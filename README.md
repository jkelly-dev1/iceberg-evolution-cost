# iceberg-evolution-cost

[![CI](https://github.com/jkelly-dev1/iceberg-evolution-cost/actions/workflows/ci.yml/badge.svg)](https://github.com/jkelly-dev1/iceberg-evolution-cost/actions/workflows/ci.yml)

What Apache Iceberg table changes actually cost, measured on 20,000,000 rows
with Trino 478, MinIO and an Iceberg REST catalog in Docker Compose on one
machine. No cloud account, no managed service, and no corpus to download: the
data is generated from a sequence in SQL, so a clone reproduces it exactly.

A personal learning project. Each claim names a test, and every figure is read
from a shipped results file rather than from the prose.

Four measurements, four questions:

1. Which table changes are metadata-only, and which rewrite data?
2. What does changing the partition spec do to query planning afterward?
3. What does `expire_snapshots` actually give back?
4. Who decides whether a `DELETE` rewrites data, and what does deferring it cost?

What the four measurements found. `DELETE` does not rewrite data, a table
partitioned by day through evolution reads 28x more rows than one partitioned by
day from the start, `expire_snapshots` reclaimed exactly zero bytes, and a table
marked `copy-on-write` is written merge-on-read anyway because the engine
decides, not the table.

## 1. Which changes are metadata-only

Each operation runs on its own fresh copy of the table, so rewritten bytes can
be attributed to the operation that caused them. The measurement is a set
difference over data file paths, not a count: an operation that swaps seven
files for seven different files leaves the count unchanged and is still a full
rewrite.

| Operation | Verdict | Data files | Delete files | Bytes written |
| --- | --- | --- | --- | --- |
| `ADD COLUMN` | metadata-only | +0 / -0 | +0 | 0 |
| `RENAME COLUMN` | metadata-only | +0 / -0 | +0 | 0 |
| `DROP COLUMN` | metadata-only | +0 / -0 | +0 | 0 |
| `ALTER COLUMN ... SET DATA TYPE bigint` | metadata-only | +0 / -0 | +0 | 0 |
| `SET PROPERTIES partitioning = ...` | metadata-only | +0 / -0 | +0 | 0 |
| `DELETE FROM ... WHERE` | merge-on-read | +0 / -0 | +11 | 0 |
| `UPDATE ... SET` | merge-on-read | +1 / -0 | +12 | 161 MB |
| `ALTER TABLE ... EXECUTE optimize` | rewrite | +1 / -11 | +0 | 359 MB |

Two of the eight predictions were wrong, and they are still in the code. Both
`DELETE` and `UPDATE` were predicted to rewrite data. Under merge-on-read they
write delete files and leave every original data file exactly where it was.
`scripts/exp1_operation_cost.py` keeps the refuted predictions rather than
quietly correcting them, and a test asserts that those two are recorded as
refuted, so a later tidy-up cannot erase the only surprise in the table.

Reading `$files` naively produces the opposite conclusion. That table lists
delete files alongside data files, so counting its rows reports a merge-on-read
`DELETE` as a rewrite: the exact reverse of what happened. The `content` column
is what separates them, and nothing in the row count hints that it needs to be
read.

## 2. What partition evolution costs afterward

Four layouts of the same rows. `evolved` was written under
`month(event_date)`, altered to `day(event_date)`, and then had more rows
inserted, so it holds files under both specs at once.

Query: `SELECT count(*) ... WHERE event_date = DATE '2026-02-10'`

| Layout | Partition specs | Rows scanned | Splits | Bytes read |
| --- | --- | --- | --- | --- |
| `none` | 1 | 20,000,000 | 12 | 2,190,000 |
| `month` | 1 | 6,222,216 | 3 | 279,290 |
| `day` | 1 | 222,222 | 1 | 0 |
| `evolved` | 2 | 6,244,438 | 4 | 853,110 |

The last two rows are both "partitioned by day". The native one prunes to a
single split and reads no column data at all, because the count is answered from
manifest and row-group metadata. The evolved one reads a whole month of the old
files plus one day of the new, because changing the partition spec does not
rewrite the files already written under the old one. They keep the old spec
forever, and a single-day predicate can only prune them to the month they were
written under.

That is the same fact that makes the `SET PROPERTIES` row in section 1 free.
The cheapness of the `ALTER` and the cost of every query afterward are the
same property seen from two ends.

## 3. What expire_snapshots reclaims

A table is taken through append, update, delete and compaction, then cleaned
up. Three numbers are tracked, and they are not the same number: bytes the
current snapshot references, data bytes actually in object storage, and total
bytes including metadata.

| Stage | Snapshots | Referenced | On disk | Stranded |
| --- | --- | --- | --- | --- |
| created | 1 | 185.9 MB | 185.9 MB | 0 |
| appended | 2 | 278.8 MB | 278.8 MB | 0 |
| updated | 3 | 375.3 MB | 379.5 MB | 4.2 MB |
| deleted | 4 | 375.3 MB | 383.7 MB | 8.4 MB |
| **optimized** | 5 | 192.9 MB | **576.6 MB** | **383.7 MB** |
| after `expire_snapshots` | 1 | 192.9 MB | **576.6 MB** | **383.7 MB** |
| after `remove_orphan_files` | 1 | 192.9 MB | 192.9 MB | 0 |

`expire_snapshots` took the snapshot count from 5 to 1 and reclaimed zero data
bytes. Not a rounding-error amount: the data figure is identical to the byte,
576,609,982 before and after. Every one of the 383.7 MB came back only from
`remove_orphan_files`, a separate procedure.

It did free metadata, and saying "zero bytes" without that qualifier would be
wrong in the direction that flatters the finding. Total on-disk went from
577,129,173 to 576,824,855 and the object count from 53 to 43: 304,318 bytes
across 10 objects, all of it manifests, manifest lists and `metadata.json`.
That is 0.05% of what compaction stranded, so the data column is the one to
read, but it is not nothing, and the third column above is there so a reader can
see it rather than take it on trust.

Compaction is what created the extra copies; expiry is what turned them from
reachable history into garbage. Both are needed before `remove_orphan_files`
can delete anything.

Two operational details a reader will meet on the way:

- `expire_snapshots` REFUSES BY DEFAULT. Trino enforces a 7 day minimum
  retention, so expiring anything recent fails until
  `iceberg.expire_snapshots_min_retention` is lowered.
- Each `trino --execute` IS ITS OWN SESSION, so a `SET SESSION` in a separate
  invocation is discarded before the next statement runs. It has to travel in
  the same call as the statement it configures.

## 4. What a delete really costs

Experiment 1 measured the write side of a `DELETE` and stopped there: no data
file rewritten. That is easy to read as "Iceberg does not rewrite on a delete".
It does not say that. It says this engine, on its default, deferred the work.
Two things were left unmeasured: whether the mode can be changed, and what the
deferral costs whoever queries the table next.

### Who decides the write mode

| Attempt | Property stored | `DELETE` verdict | Data files | Delete files |
| --- | --- | --- | --- | --- |
| `extra_properties` on `CREATE TABLE` | refused | not reached | n/a | n/a |
| `set-properties` via the REST catalog | yes | merge-on-read | +0 / -0 | +1 |

Copy-on-write versus merge-on-read is a table property in the Iceberg spec.
Trino 478 exposes no `delete_mode` property of its own, and it rejects the spec
one outright rather than ignoring it:

```
Illegal keys in extra_properties: [write.merge.mode, write.delete.mode, write.update.mode]
```

Setting it through the Iceberg REST catalog instead works, and the table really
does carry `write.delete.mode = copy-on-write` afterward, in the same catalog
Trino reads. Trino writes a position delete file anyway. The engine decides the
write mode here; the table property does not.

### What the reader pays

Five successive deletes, each removing about 1% of the table, on a fresh copy
with 7 data files. Timings are the MEDIAN OF 7 RUNS, because a single sample
cannot support any claim here: the clock covers a whole `docker exec`.

| stage | data files | delete files | delete bytes | full scan | selective |
|---|---|---|---|---|---|
| baseline | 7 | 0 | 0 | 677 ms | 658 ms |
| after delete 1 | 7 | 7 | 231,507 | 858 ms | 820 ms |
| after delete 2 | 7 | 14 | 463,238 | 966 ms | 831 ms |
| after delete 3 | 7 | 21 | 694,747 | 913 ms | 859 ms |
| after delete 4 | 7 | 28 | 926,285 | 853 ms | 841 ms |
| after delete 5 | 7 | 35 | 1,157,927 | 863 ms | 855 ms |
| **after optimize** | 1 | 0 | 0 | 690 ms | 666 ms |

Each delete adds exactly one delete file per data file and rewrites nothing: 7
data files throughout, 35 delete files at the end. After five deletes the answer
is 19,000,000 rows while the data files still physically hold all 20,000,000.
That gap is the borrowed work.

Every run with delete files present was slower than every run without. Not on
average; the ranges do not overlap at all. Across 7 repeats per stage the full
scan ran 664-724 ms with no delete files and 797-1025 ms with them, and the
selective query 612-691 ms against 799-958 ms. `optimize` cleared all 35 delete
files into a single data file and returned both queries to baseline.

Two things worth noticing, because both cut against intuition:

- **The cost is not proportional to the delete files.** It appears in full at
  the first delete and then flattens. Going from 7 delete files to 35 did not
  multiply anything by five.
- **`Physical input` never moves.** Trino reports the same scan bytes at zero
  delete files and at thirty-five, because it counts the data scan and not the
  delete files merged on top. The one metric you would reach for to measure
  merge-on-read read cost is blind to it. At the last stage the delete files
  were 1,157,927 bytes against a reported 1,420,000 byte scan, 82% of the read,
  invisible.

## Claims backed by tests

Run `pytest -q`. The suite is offline: it tests the measurement logic as pure
functions, and it tests the shipped `results/*.json` as evidence. It never
needs Docker.

| Claim | Test |
| --- | --- |
| Schema changes touch no data file and no delete file | `tests/test_results_invariants.py::TestOperationCost::test_schema_changes_touch_no_file` |
| Metadata-only changes preserve every row | `tests/test_results_invariants.py::TestOperationCost::test_metadata_changes_preserve_every_row` |
| Changing the partition spec rewrites nothing | `tests/test_results_invariants.py::TestOperationCost::test_partition_change_is_metadata_only` |
| `DELETE` does not rewrite data; it adds delete files | `tests/test_results_invariants.py::TestOperationCost::test_delete_does_not_rewrite_data` |
| `optimize` is the only operation that truly rewrites | `tests/test_results_invariants.py::TestOperationCost::test_optimize_is_the_only_true_rewrite` |
| The two refuted predictions stay recorded as refuted | `tests/test_results_invariants.py::TestOperationCost::test_two_predictions_were_refuted_and_are_recorded_as_such` (mutation-checked: "correct" them in the results and it fails) |
| Day partitioning prunes a single-day query to one split | `tests/test_results_invariants.py::TestPartitionPlanning::test_day_partitioning_prunes_a_single_day_query_to_one_split` |
| An evolved table holds files under two partition specs | `tests/test_results_invariants.py::TestPartitionPlanning::test_evolution_leaves_old_files_under_the_old_spec` |
| An evolved day table scans far more than a native one | `tests/test_results_invariants.py::TestPartitionPlanning::test_evolved_table_scans_far_more_than_a_native_day_table` |
| MONTH partitioning does not help a non-partition predicate. The day layout DOES, by 6x, and that is an artifact of this generator rather than an Iceberg property -- see below | `tests/test_results_invariants.py::TestPartitionPlanning::test_partitioning_does_not_help_a_non_partition_predicate` |
| The EXPLAIN parser reads bytes, rows and splits from a real plan, takes the largest node rather than the first, and reports a pruned scan as pruned rather than as a failed parse | `tests/test_measurement_logic.py::TestPlanParsing::test_reads_bytes_rows_and_splits`, `::TestPlanParsing::test_a_fully_pruned_scan_reports_no_physical_input_line`, `::TestPlanParsing::test_the_largest_node_is_taken_not_the_first_or_the_sum` (mutation-checked: break any of the parser's three regexes and it fails) |
| `expire_snapshots` is refused at the default retention | `tests/test_results_invariants.py::TestSnapshotReclaim::test_expire_snapshots_is_refused_by_default` |
| `expire_snapshots` reclaims exactly zero bytes | `tests/test_results_invariants.py::TestSnapshotReclaim::test_expiring_snapshots_reclaims_nothing` (mutation-checked: record any nonzero reclaim and it fails) |
| `remove_orphan_files` is what frees the space | `tests/test_results_invariants.py::TestSnapshotReclaim::test_orphan_removal_is_what_actually_frees_the_space` |
| Compaction is what strands the bytes | `tests/test_results_invariants.py::TestSnapshotReclaim::test_compaction_is_what_strands_the_bytes` |
| Every stage's stranded figure agrees with its own inputs | `tests/test_results_invariants.py::TestSnapshotReclaim::test_every_stage_is_internally_consistent` (mutation-checked: perturb one figure and it fails) |
| Reclaiming space never touches live data | `tests/test_results_invariants.py::TestSnapshotReclaim::test_the_live_data_is_never_touched` |
| A rewrite is detected by path set, not by file count | `tests/test_measurement_logic.py::TestDelta::test_same_count_different_paths_is_still_a_rewrite` (mutation-checked: compare counts and it fails) |
| Delete files are never counted as rewritten data | `tests/test_measurement_logic.py::TestDelta::test_delete_files_do_not_count_as_rewritten_data` (mutation-checked: fold them in and it fails) |
| A data-file removal outranks a merge-on-read verdict | `tests/test_measurement_logic.py::TestVerdict::test_rewrite_outranks_merge_on_read` (mutation-checked: reorder the checks and it fails) |
| Trino's byte units are decimal, not binary | `tests/test_measurement_logic.py::TestByteParsing::test_units_are_decimal_not_binary` (mutation-checked: read them as 1024 and it fails) |
| A fully pruned scan reports no physical input at all | `tests/test_measurement_logic.py::TestPlanParsing::test_a_fully_pruned_scan_reports_no_physical_input_line` |
| Trino refuses the Iceberg write-mode property outright | `tests/test_results_invariants.py::TestDeleteCost::test_trino_refuses_the_write_mode_property` (mutation-checked: record the refusal as an acceptance and it fails) |
| Setting it through the REST catalog really does store it on the table | `tests/test_results_invariants.py::TestDeleteCost::test_the_property_really_is_set_on_the_table` |
| A table marked `copy-on-write` is still written merge-on-read by Trino | `tests/test_results_invariants.py::TestDeleteCost::test_trino_ignores_a_copy_on_write_table` (mutation-checked: flip `honored` to true, or relabel the verdict a rewrite, and it fails) |
| Each `DELETE` adds one delete file per data file | `tests/test_results_invariants.py::TestDeleteCost::test_each_delete_adds_one_delete_file_per_data_file` (mutation-checked: move any round off the per-data-file ratio and it fails) |
| Five successive deletes rewrite no data file at all | `tests/test_results_invariants.py::TestDeleteCost::test_no_delete_ever_rewrites_a_data_file` (mutation-checked: drop one data file from any round and it fails) |
| A million rows are logically gone while physically still in the files | `tests/test_results_invariants.py::TestDeleteCost::test_the_rows_are_logically_gone_and_physically_present` (mutation-checked: claim the rows left the files and it fails) |
| `Physical input` reports the same bytes at 0 and at 35 delete files | `tests/test_results_invariants.py::TestDeleteCost::test_physical_input_is_blind_to_delete_files` (mutation-checked: vary the reported scan by one byte and it fails) |
| Delete-file bytes are over half the reported scan, and uncounted | `tests/test_results_invariants.py::TestDeleteCost::test_delete_file_bytes_are_a_large_invisible_share_of_the_read` |
| Every timed run with delete files was SLOWER than every run without, on both queries | `tests/test_results_invariants.py::TestDeleteCost::test_every_run_with_delete_files_is_slower_than_every_run_without` (mutation-checked: overlap the ranges by a single sample and it fails) |
| The read cost appears at the first delete and then plateaus | `tests/test_results_invariants.py::TestDeleteCost::test_the_cost_appears_at_the_first_delete_and_then_plateaus` |
| `optimize` clears the delete files and returns the read to baseline | `tests/test_results_invariants.py::TestDeleteCost::test_optimize_returns_the_read_to_baseline` (mutation-checked: leave delete files behind and it fails) |
| Every number in this README is derived from `results/*.json` | `tests/test_results_invariants.py::TestReadmeAgreesWithResults::test_every_readme_number_is_derived_from_the_results` (mutation-checked: change one figure in the README and it fails) |

## Running it

The tests need nothing but pytest. Reproducing the measurements needs Docker.

```
python3 -m venv envs && ./envs/bin/pip install "pytest>=8,<10"
./envs/bin/pytest -q
```

```
cd stack && docker compose up -d
docker exec iec-trino trino --execute "SELECT 1"
```

Then, from the repository root:

```
./scripts/q.sh "CREATE SCHEMA IF NOT EXISTS iceberg.demo"
./scripts/q.sh "$(cat sql/generate.sql)"
PYTHONPATH=scripts ./envs/bin/python scripts/exp1_operation_cost.py
PYTHONPATH=scripts ./envs/bin/python scripts/exp2_partition_planning.py
PYTHONPATH=scripts ./envs/bin/python scripts/exp3_snapshot_reclaim.py
PYTHONPATH=scripts ./envs/bin/python scripts/exp4_delete_cost.py
```

`SAMPLE_RUN.md` is the captured output of exactly those commands.

The stack binds every port to loopback and picks unusual ones (Trino on 8090,
MinIO on 9000/9001, the catalog on 8181) so it does not collide with anything
already running. The MinIO credentials in `stack/compose.yaml` are fixtures,
not secrets: MinIO needs a root user to start and the whole thing is bound to
127.0.0.1.

## What this does not measure

- **One engine.** Every number is Trino 478. Spark, Flink and Dremio implement
  the same Iceberg spec with different defaults. Experiment 4 shows that on
  Trino the write mode is not negotiable through the table property at all, but
  that is a fact about Trino: another engine may well honor it, and nothing
  here says what Spark would do.
- **One scale, one machine.** 20,000,000 rows and about 458 MB, all local,
  with object storage one hop away. The pruning ratios should hold; the
  wall-clock numbers are worth nothing on a real cluster over real network
  storage, and are reported only to compare operations against each other.
- **Durations include CLI startup.** Every timing wraps a whole `docker exec`,
  so roughly 600 ms of it is process launch. That is why the metadata-only
  operations all land near the same figure: it is mostly overhead, which is
  itself the finding. They are not benchmarks of Trino.
- **Byte totals move slightly between runs.** Parquet file sizes vary with
  write parallelism by a few hundredths of a percent. The qualitative results
  reproduce exactly; the byte figures in the tables are from the recorded run
  in `results/`, and no test pins them.

## Related repositories

[roster-ingest-guarantees](https://github.com/jkelly-dev1/roster-ingest-guarantees)
is the one to read directly against this repository, and the two were built to
be read that way. This one measures what table changes cost on a STATIC table
with Trino. That one puts a live Flink writer on the other side and asks what
survives a restart: where exactly-once stops being true, what an old savepoint
does to a table, and what the two maintenance procedures in section 3 here,
`expire_snapshots` and `remove_orphan_files`, do to a job that is still writing.

Both follow the same rules: no claim without a test, mutation checks on the
tests that matter, every number re-derived from the shipped results by script,
and predictions recorded before the run so the refuted ones survive.

## License

MIT
