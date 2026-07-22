\set ON_ERROR_STOP on

DO $$
DECLARE
    baseline_rows bigint;
    drift_rows bigint;
    overlapping_keys bigint;
    baseline_total numeric(18, 2);
    net_total numeric(18, 2);
    gross_total numeric(18, 2);
    gross_variance numeric(10, 4);
BEGIN
    SELECT count(*), sum(revenue)
      INTO baseline_rows, baseline_total
      FROM audit.payments_fct_last_good;

    SELECT count(*), sum(net_amount), sum(gross_amount)
      INTO drift_rows, net_total, gross_total
      FROM raw.payments_raw;

    SELECT count(*)
      INTO overlapping_keys
      FROM audit.payments_fct_last_good baseline
      JOIN raw.payments_raw drift USING (payment_id);

    gross_variance := round(100 * (gross_total - baseline_total) / baseline_total, 4);

    IF baseline_rows <> 10 OR drift_rows <> 10 OR overlapping_keys <> 10 THEN
        RAISE EXCEPTION
            'PK invariant failed: baseline=%, drift=%, overlap=%',
            baseline_rows,
            drift_rows,
            overlapping_keys;
    END IF;

    IF net_total <> baseline_total THEN
        RAISE EXCEPTION 'Net reconciliation failed: net=%, baseline=%', net_total, baseline_total;
    END IF;

    IF gross_variance <> 3.4000 THEN
        RAISE EXCEPTION 'Gross variance is %, expected exactly 3.4000', gross_variance;
    END IF;
END
$$;

WITH metrics AS (
    SELECT
        baseline.row_count AS baseline_rows,
        drift.row_count AS drift_rows,
        overlap.row_count AS overlapping_primary_keys,
        baseline.total AS baseline_total,
        drift.net_total,
        drift.gross_total,
        round(100 * (drift.gross_total - baseline.total) / baseline.total, 2) AS gross_variance_pct
    FROM
        (SELECT count(*) AS row_count, sum(revenue) AS total FROM audit.payments_fct_last_good) baseline
    CROSS JOIN
        (SELECT count(*) AS row_count, sum(net_amount) AS net_total, sum(gross_amount) AS gross_total
           FROM raw.payments_raw) drift
    CROSS JOIN
        (SELECT count(*) AS row_count
           FROM audit.payments_fct_last_good b
           JOIN raw.payments_raw r USING (payment_id)) overlap
)
SELECT
    baseline_rows,
    drift_rows,
    overlapping_primary_keys,
    baseline_total,
    net_total,
    gross_total,
    gross_variance_pct,
    'PASS' AS fixture_verdict
FROM metrics;
