\set ON_ERROR_STOP on

DO $$
DECLARE
    baseline_rows bigint;
    baseline_total numeric(18, 2);
    gross_rows bigint;
    gross_overlap bigint;
    gross_total numeric(18, 2);
    net_rows bigint;
    net_overlap bigint;
    net_total numeric(18, 2);
    gross_variance numeric(10, 4);
    net_variance numeric(10, 4);
BEGIN
    SELECT count(*), sum(revenue)
      INTO baseline_rows, baseline_total
      FROM audit.payments_fct_last_good;

    SELECT count(*), sum(revenue)
      INTO gross_rows, gross_total
      FROM candidate_gross.fct_revenue;
    SELECT count(*) INTO gross_overlap
      FROM candidate_gross.fct_revenue candidate
      JOIN audit.payments_fct_last_good baseline USING (payment_id);

    SELECT count(*), sum(revenue)
      INTO net_rows, net_total
      FROM candidate_net.fct_revenue;
    SELECT count(*) INTO net_overlap
      FROM candidate_net.fct_revenue candidate
      JOIN audit.payments_fct_last_good baseline USING (payment_id);

    gross_variance := round(100 * (gross_total - baseline_total) / baseline_total, 4);
    net_variance := round(100 * (net_total - baseline_total) / baseline_total, 4);

    IF gross_rows <> baseline_rows OR gross_overlap <> baseline_rows THEN
        RAISE EXCEPTION 'Gross candidate PK reconciliation failed';
    END IF;
    IF net_rows <> baseline_rows OR net_overlap <> baseline_rows THEN
        RAISE EXCEPTION 'Net candidate PK reconciliation failed';
    END IF;
    IF gross_variance <> 3.4000 THEN
        RAISE EXCEPTION 'Gross candidate variance is %, expected 3.4000', gross_variance;
    END IF;
    IF net_variance <> 0.0000 THEN
        RAISE EXCEPTION 'Net candidate variance is %, expected 0.0000', net_variance;
    END IF;
END
$$;

WITH baseline AS (
    SELECT count(*)::numeric AS row_count, sum(revenue) AS total
    FROM audit.payments_fct_last_good
), candidates AS (
    SELECT 'gross_amount' AS candidate, count(*)::numeric AS row_count, sum(revenue) AS total
    FROM candidate_gross.fct_revenue
    UNION ALL
    SELECT 'net_amount', count(*)::numeric, sum(revenue)
    FROM candidate_net.fct_revenue
)
SELECT
    candidate,
    candidates.row_count::bigint AS row_count,
    round(100 * candidates.row_count / baseline.row_count, 2) AS pk_overlap_pct,
    candidates.total AS total_revenue,
    round(100 * (candidates.total - baseline.total) / baseline.total, 2) AS total_variance_pct,
    CASE candidate
        WHEN 'gross_amount' THEN 'REJECTED'
        WHEN 'net_amount' THEN 'SELECTED'
    END AS policy_outcome
FROM candidates
CROSS JOIN baseline
ORDER BY candidate;
