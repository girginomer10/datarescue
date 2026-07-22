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
    net_amount AS amount
FROM demo.payments_seed
ORDER BY payment_id;

ALTER TABLE raw.payments_raw
    ADD PRIMARY KEY (payment_id),
    ALTER COLUMN customer_id SET NOT NULL,
    ALTER COLUMN paid_at SET NOT NULL,
    ALTER COLUMN currency SET NOT NULL,
    ALTER COLUMN status SET NOT NULL,
    ALTER COLUMN amount SET NOT NULL;

COMMENT ON TABLE raw.payments_raw IS
    'Healthy pre-drift payment source. The amount field means merchant net revenue.';
COMMENT ON COLUMN raw.payments_raw.amount IS
    'Net amount received by the merchant, in AUD.';

COMMIT;

SELECT
    'HEALTHY' AS source_state,
    count(*) AS row_count,
    sum(amount)::numeric(18, 2) AS total_amount
FROM raw.payments_raw;
