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
    round(net_amount * 0.985, 2)::numeric(14, 2) AS settlement_amount
FROM demo.payments_seed
ORDER BY payment_id;

ALTER TABLE raw.payments_raw
    ADD PRIMARY KEY (payment_id),
    ALTER COLUMN customer_id SET NOT NULL,
    ALTER COLUMN paid_at SET NOT NULL,
    ALTER COLUMN currency SET NOT NULL,
    ALTER COLUMN status SET NOT NULL,
    ALTER COLUMN gross_amount SET NOT NULL,
    ALTER COLUMN settlement_amount SET NOT NULL;

COMMENT ON TABLE raw.payments_raw IS
    'Fail-closed fixture: neither replacement column preserves the legacy revenue meaning and value.';
COMMENT ON COLUMN raw.payments_raw.gross_amount IS
    'Customer charge before fees; semantically conflicts with recognized revenue.';
COMMENT ON COLUMN raw.payments_raw.settlement_amount IS
    'Illustrative settlement estimate with incomplete business context; deliberately differs from last-good.';

COMMIT;

SELECT
    'CONTAINMENT_FIXTURE' AS source_state,
    count(*) AS row_count,
    sum(gross_amount)::numeric(18, 2) AS gross_total,
    sum(settlement_amount)::numeric(18, 2) AS settlement_total
FROM raw.payments_raw;
