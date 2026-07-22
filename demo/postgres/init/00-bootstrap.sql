\set ON_ERROR_STOP on

CREATE SCHEMA IF NOT EXISTS raw;
CREATE SCHEMA IF NOT EXISTS audit;
CREATE SCHEMA IF NOT EXISTS demo;

CREATE TABLE IF NOT EXISTS demo.payments_seed (
    payment_id text PRIMARY KEY,
    customer_id text NOT NULL,
    paid_at timestamptz NOT NULL,
    currency char(3) NOT NULL CHECK (currency = 'AUD'),
    status text NOT NULL CHECK (status = 'settled'),
    net_amount numeric(14, 2) NOT NULL CHECK (net_amount >= 0),
    gross_amount numeric(14, 2) NOT NULL CHECK (gross_amount >= net_amount),
    CHECK (gross_amount = round(net_amount * 1.034, 2))
);

TRUNCATE TABLE demo.payments_seed;

INSERT INTO demo.payments_seed (
    payment_id,
    customer_id,
    paid_at,
    currency,
    status,
    net_amount,
    gross_amount
)
VALUES
    ('pay_001', 'cus_001', '2026-07-20 00:10:00+00', 'AUD', 'settled',  500.00,  517.00),
    ('pay_002', 'cus_002', '2026-07-20 01:15:00+00', 'AUD', 'settled',  750.00,  775.50),
    ('pay_003', 'cus_003', '2026-07-20 02:20:00+00', 'AUD', 'settled', 1200.00, 1240.80),
    ('pay_004', 'cus_004', '2026-07-20 03:25:00+00', 'AUD', 'settled',  900.00,  930.60),
    ('pay_005', 'cus_005', '2026-07-20 04:30:00+00', 'AUD', 'settled', 1500.00, 1551.00),
    ('pay_006', 'cus_006', '2026-07-20 05:35:00+00', 'AUD', 'settled', 1100.00, 1137.40),
    ('pay_007', 'cus_007', '2026-07-20 06:40:00+00', 'AUD', 'settled',  800.00,  827.20),
    ('pay_008', 'cus_008', '2026-07-20 07:45:00+00', 'AUD', 'settled', 1250.00, 1292.50),
    ('pay_009', 'cus_009', '2026-07-20 08:50:00+00', 'AUD', 'settled', 1000.00, 1034.00),
    ('pay_010', 'cus_010', '2026-07-20 09:55:00+00', 'AUD', 'settled', 1000.00, 1034.00);

DROP TABLE IF EXISTS audit.payments_fct_last_good;
CREATE TABLE audit.payments_fct_last_good AS
SELECT
    payment_id,
    customer_id,
    paid_at,
    paid_at::date AS payment_date,
    currency,
    net_amount AS revenue
FROM demo.payments_seed
ORDER BY payment_id;

ALTER TABLE audit.payments_fct_last_good
    ADD PRIMARY KEY (payment_id),
    ALTER COLUMN customer_id SET NOT NULL,
    ALTER COLUMN paid_at SET NOT NULL,
    ALTER COLUMN payment_date SET NOT NULL,
    ALTER COLUMN currency SET NOT NULL,
    ALTER COLUMN revenue SET NOT NULL;

COMMENT ON TABLE audit.payments_fct_last_good IS
    'Immutable demo baseline. Recognized revenue is the net amount received by the merchant.';
COMMENT ON COLUMN audit.payments_fct_last_good.revenue IS
    'Recognized merchant revenue after processing fees; the semantic ground truth for recovery.';

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

CREATE OR REPLACE VIEW audit.demo_baseline_summary AS
SELECT
    count(*)::bigint AS row_count,
    sum(revenue)::numeric(18, 2) AS total_revenue,
    min(paid_at) AS first_payment_at,
    max(paid_at) AS last_payment_at
FROM audit.payments_fct_last_good;

DO $$
DECLARE
    baseline_total numeric(18, 2);
BEGIN
    SELECT total_revenue INTO baseline_total FROM audit.demo_baseline_summary;
    IF baseline_total <> 10000.00 THEN
        RAISE EXCEPTION 'Fixture invariant failed: baseline total is %, expected 10000.00', baseline_total;
    END IF;
END
$$;
