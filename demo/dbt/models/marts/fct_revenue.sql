select
    payment_id,
    customer_id,
    paid_at,
    cast(paid_at as date) as payment_date,
    currency,
    revenue
from {{ ref("stg_payments") }}
where status = 'settled'
