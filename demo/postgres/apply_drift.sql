\set ON_ERROR_STOP on

BEGIN;

DROP SCHEMA IF EXISTS candidate_gross CASCADE;
DROP SCHEMA IF EXISTS candidate_net CASCADE;
DROP SCHEMA IF EXISTS candidate_containment CASCADE;
DROP SCHEMA IF EXISTS analytics CASCADE;

DROP TABLE IF EXISTS raw.payments_raw;
CREATE TABLE raw.payments_raw AS
SELECT
    payment_id,
    customer_id,
    paid_at,
    currency,
    status,
    gross_amount,
    net_amount
FROM demo.payments_seed
ORDER BY payment_id;

ALTER TABLE raw.payments_raw
    ADD PRIMARY KEY (payment_id),
    ALTER COLUMN customer_id SET NOT NULL,
    ALTER COLUMN paid_at SET NOT NULL,
    ALTER COLUMN currency SET NOT NULL,
    ALTER COLUMN status SET NOT NULL,
    ALTER COLUMN gross_amount SET NOT NULL,
    ALTER COLUMN net_amount SET NOT NULL;

COMMENT ON TABLE raw.payments_raw IS
    'Drifted payment source. The legacy amount field was split into gross_amount and net_amount.';
COMMENT ON COLUMN raw.payments_raw.gross_amount IS
    'Customer charge before processing fees. It must not be used as recognized merchant revenue.';
COMMENT ON COLUMN raw.payments_raw.net_amount IS
    'Amount received by the merchant after processing fees; this preserves the legacy amount meaning.';

COMMIT;

SELECT
    'DRIFTED' AS source_state,
    count(*) AS row_count,
    sum(net_amount)::numeric(18, 2) AS net_total,
    sum(gross_amount)::numeric(18, 2) AS gross_total,
    round(
        100 * (sum(gross_amount) - sum(net_amount)) / nullif(sum(net_amount), 0),
        2
    ) AS gross_variance_pct
FROM raw.payments_raw;
