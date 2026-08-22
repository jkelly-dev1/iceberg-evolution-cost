"""Experiment 3: what expire_snapshots actually reclaims.

A table accumulates history: append, update, delete, compact. Every one of
those leaves data files that no longer belong to the current snapshot but are
still on disk, because older snapshots still reference them and time travel
has to keep working.

The question is what you get back, and when. Three numbers are tracked at
every stage and they are NOT the same number:

  referenced_bytes  data files the CURRENT snapshot points at
  disk_data_bytes   data files actually sitting in object storage
  disk_bytes        everything, including metadata.json and manifests

The gap between the first two is history you are paying to store.
"""

import lab

TABLE = "exp3_retention"
T = lab.table(TABLE)


def stage(label, statements=()):
    for s in statements:
        lab.sql(s)
    st = lab.state(TABLE)
    disk = lab.table_disk(TABLE)
    row = {
        "stage": label,
        "snapshots": st["snapshot_count"],
        "referenced_data_files": st["file_count"],
        "referenced_bytes": st["bytes"],
        "delete_files": st["delete_file_count"],
        "disk_objects": disk["objects"],
        "disk_data_bytes": disk["data_bytes"],
        "disk_bytes": disk["bytes"],
        "unreferenced_bytes": disk["data_bytes"] - st["bytes"],
    }
    print(f"  {label:24} snaps={row['snapshots']:>2} "
          f"ref={row['referenced_bytes']:>12,} "
          f"disk={row['disk_data_bytes']:>12,} "
          f"stranded={row['unreferenced_bytes']:>12,}", flush=True)
    return row


def main():
    lab.sql(f"DROP TABLE IF EXISTS {T}")
    history = []

    history.append(stage("01_created", [
        f"CREATE TABLE {T} WITH (format='PARQUET') AS "
        f"SELECT * FROM {lab.table('events')} WHERE event_id <= 8000000"]))

    history.append(stage("02_appended", [
        f"INSERT INTO {T} SELECT * FROM {lab.table('events')} "
        f"WHERE event_id > 8000000 AND event_id <= 12000000"]))

    history.append(stage("03_updated", [
        f"UPDATE {T} SET status = 'ok' WHERE status = 'retry'"]))

    history.append(stage("04_deleted", [
        f"DELETE FROM {T} WHERE status = 'failed'"]))

    history.append(stage("05_optimized", [
        f"ALTER TABLE {T} EXECUTE optimize"]))

    peak = history[-1]

    # --- the default refuses -------------------------------------------
    refusal = None
    try:
        lab.sql(f"ALTER TABLE {T} EXECUTE expire_snapshots("
                f"retention_threshold => '0s')")
    except lab.QueryError as exc:
        refusal = str(exc).splitlines()[-1][:300]
    print(f"  default expire refused: {bool(refusal)}")

    # --- expire, with the guard lowered in the SAME session -------------
    lab.sql_session(
        "SET SESSION iceberg.expire_snapshots_min_retention = '0s'",
        f"ALTER TABLE {T} EXECUTE expire_snapshots(retention_threshold => '0s')")
    after_expire = stage("06_expired_snapshots")

    # --- orphan files are a SEPARATE procedure --------------------------
    lab.sql_session(
        "SET SESSION iceberg.remove_orphan_files_min_retention = '0s'",
        f"ALTER TABLE {T} EXECUTE remove_orphan_files("
        f"retention_threshold => '0s')")
    after_orphans = stage("07_removed_orphans")

    payload = {
        "experiment": "snapshot_reclaim",
        "question": ("what expire_snapshots actually gives back, and what it "
                     "leaves behind"),
        "default_expire_refused": refusal is not None,
        "default_expire_error": refusal,
        "stages": history + [after_expire, after_orphans],
        "reclaim": {
            "peak_disk_data_bytes": peak["disk_data_bytes"],
            "stranded_at_peak": peak["unreferenced_bytes"],
            "after_expire_disk_data_bytes": after_expire["disk_data_bytes"],
            "reclaimed_by_expire": (peak["disk_data_bytes"]
                                    - after_expire["disk_data_bytes"]),
            "reclaimed_by_orphans": (after_expire["disk_data_bytes"]
                                     - after_orphans["disk_data_bytes"]),
            "still_stranded": after_orphans["unreferenced_bytes"],
            "snapshots_before": peak["snapshots"],
            "snapshots_after": after_orphans["snapshots"],
        },
    }
    lab.write_result("results/exp3_snapshot_reclaim.json", payload)


if __name__ == "__main__":
    main()
