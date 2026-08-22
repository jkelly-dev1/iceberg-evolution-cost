-- Deterministic synthetic events. No corpus is shipped: the rows are generated
-- from a sequence, so a clone reproduces them exactly without downloading
-- anything. Every value is a pure function of the row number.
--
-- Trino caps sequence() at 10000 entries, so the row count comes from a cross
-- join of 10000 x 2000 rather than one long sequence.
--
-- The payload_hash column exists to defeat Parquet. Sequential integers and
-- low-cardinality strings compress to almost nothing: 2,000,000 rows of them
-- encode to 9.5 MB in a single file, which leaves "how many bytes did this
-- operation move" unmeasurable. An md5 per row is incompressible and
-- deterministic, and it is what makes the byte columns in the results mean
-- anything.
CREATE TABLE iceberg.demo.events
WITH (format = 'PARQUET')
AS
SELECT
  id                                                             AS event_id,
  DATE '2026-01-01' + ((id % 90) * INTERVAL '1' DAY)             AS event_date,
  ELEMENT_AT(ARRAY['us-east','us-west','eu-west','eu-north','ap-south','sa-east'],
             CAST((id % 6) + 1 AS INTEGER))                      AS region,
  ELEMENT_AT(ARRAY['ok','retry','failed'],
             CAST((id % 3) + 1 AS INTEGER))                      AS status,
  CAST((id * 7919) % 100000 AS INTEGER)                          AS amount_cents,
  TO_HEX(MD5(TO_UTF8(CAST(id AS VARCHAR))))                      AS payload_hash,
  CAST(id AS INTEGER)                                            AS legacy_seq
FROM (
  SELECT ((b - 1) * 10000 + a) AS id
  FROM UNNEST(sequence(1, 10000)) AS x(a)
  CROSS JOIN UNNEST(sequence(1, 2000)) AS y(b)
)
