# Sample run

Captured from a single machine: Docker Compose with MinIO, an Iceberg REST
catalog and Trino 478, against 20,000,000 generated rows. Experiments 1 to 3
were captured on 2026-08-20; experiment 4 was added later and captured on
2026-08-21, in a separate run against the same stack and the same source table.
It is a separate capture and is labeled as one. It is not presented as part of a
single sitting.

This file is verbatim stdout. Nothing has been reordered, shortened or edited.
The only thing removed is the leading `### expN` marker this capture added to
separate the scripts.

Those `results/*.json` files committed alongside this repository are the output
of exactly this run, and `scripts/check_readme_numbers.py` re-derives every
number in the README from them.

## Setup

```
$ cd stack && docker compose up -d
$ docker exec iec-trino trino --execute "SELECT 1"
"1"
$ ./scripts/q.sh "CREATE SCHEMA IF NOT EXISTS iceberg.demo"
$ ./scripts/q.sh "$(cat sql/generate.sql)"
CREATE TABLE: 20000000 rows
```

## Experiment 1: which changes are metadata-only

```
$ PYTHONPATH=scripts ./envs/bin/python scripts/exp1_operation_cost.py
  add_column ...
    rewrote_data=False  data +0/-0  deletefiles +0  0 B added  670.6 ms
  rename_column ...
    rewrote_data=False  data +0/-0  deletefiles +0  0 B added  754.9 ms
  drop_column ...
    rewrote_data=False  data +0/-0  deletefiles +0  0 B added  639.9 ms
  widen_type_int_to_bigint ...
    rewrote_data=False  data +0/-0  deletefiles +0  0 B added  722.8 ms
  set_partitioning ...
    rewrote_data=False  data +0/-0  deletefiles +0  0 B added  712.6 ms
  delete_matching_rows ...
    rewrote_data=False  data +0/-0  deletefiles +11  0 B added  4123.5 ms
  update_matching_rows ...
    rewrote_data=True  data +1/-0  deletefiles +12  161124742 B added  10467.9 ms
  execute_optimize ...
    rewrote_data=True  data +1/-11  deletefiles +0  359436308 B added  9086.5 ms
wrote results/exp1_operation_cost.json
```

## Experiment 2: what partition evolution costs afterward

```
$ PYTHONPATH=scripts ./envs/bin/python scripts/exp2_partition_planning.py
  building none ...
  building month ...
  building day ...
  building evolved ...
    none     single_day              2,190,000 B    20,000,000 rows    12 splits
    none     single_month            2,190,000 B    20,000,000 rows    12 splits
    none     non_partition_column    1,950,000 B    20,000,000 rows    12 splits
    month    single_day                279,290 B     6,222,216 rows     3 splits
    month    single_month                    0 B     6,222,216 rows     3 splits
    month    non_partition_column      743,310 B    20,000,000 rows     9 splits
    day      single_day                      0 B       222,222 rows     1 splits
    day      single_month                    0 B     6,222,216 rows    28 splits
    day      non_partition_column      722,830 B     3,333,334 rows    15 splits
    evolved  single_day                853,110 B     6,244,438 rows     4 splits
    evolved  single_month                    0 B     6,844,432 rows    31 splits
    evolved  non_partition_column    9,140,000 B    20,333,334 rows    24 splits
wrote results/exp2_partition_planning.json
```

## Experiment 3: what expire_snapshots reclaims

```
$ PYTHONPATH=scripts ./envs/bin/python scripts/exp3_snapshot_reclaim.py
  01_created               snaps= 1 ref= 185,851,292 disk= 185,851,292 stranded=           0
  02_appended              snaps= 2 ref= 278,820,576 disk= 278,820,576 stranded=           0
  03_updated               snaps= 3 ref= 375,286,803 disk= 379,504,547 stranded=   4,217,744
  04_deleted               snaps= 4 ref= 375,286,803 disk= 383,722,053 stranded=   8,435,250
  05_optimized             snaps= 5 ref= 192,887,929 disk= 576,609,982 stranded= 383,722,053
  default expire refused: True
  06_expired_snapshots     snaps= 1 ref= 192,887,929 disk= 576,609,982 stranded= 383,722,053
  07_removed_orphans       snaps= 1 ref= 192,887,929 disk= 192,887,929 stranded=           0
wrote results/exp3_snapshot_reclaim.json
```

## Experiment 4: what a delete really costs

Captured 2026-08-21. Its data file count is 7 here where an earlier run of the
same script produced 10; Parquet write parallelism varies between runs, and the
per-round delete file count tracks it exactly either way.

```
$ PYTHONPATH=scripts ./envs/bin/python scripts/exp4_delete_cost.py
Part A: who decides the write mode
    direct accepted=False  out-of-band honored=False
Part B: what the reader pays
  baseline: 7 data files, 0 delete files
  delete 1: 7 delete files, 962 ms
  delete 2: 14 delete files, 1341 ms
  delete 3: 21 delete files, 1384 ms
  delete 4: 28 delete files, 1398 ms
  delete 5: 35 delete files, 1137 ms
  optimize: 1 data files, 0 delete files, 8878 ms
wrote results/exp4_delete_cost.json
```

## The offline test suite

```
$ ./envs/bin/pytest -q
53 passed in 0.02s
$ python3 scripts/check_readme_numbers.py

33 of 33 derived figures found verbatim in README.md (27 table rows, 6 in prose)
```
