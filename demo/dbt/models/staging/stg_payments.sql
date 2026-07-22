{#
  Candidate validation supplies DATARESCUE_REVENUE_COLUMN. Only known source
  identifiers are accepted, so metadata or model output can never inject SQL.
  The default preserves the healthy pre-drift model and is the line patched by
  the draft recovery PR after net_amount is proven safe.
#}
{% set revenue_column = env_var("DATARESCUE_REVENUE_COLUMN", "amount") %}
{% set allowed_revenue_columns = ["amount", "gross_amount", "net_amount", "settlement_amount"] %}

{% if revenue_column not in allowed_revenue_columns %}
  {{ exceptions.raise_compiler_error(
      "Unsafe DATARESCUE_REVENUE_COLUMN: " ~ revenue_column ~
      ". Expected one of: " ~ (allowed_revenue_columns | join(", "))
  ) }}
{% endif %}

select
    payment_id,
    customer_id,
    paid_at,
    currency,
    status,
    cast({{ adapter.quote(revenue_column) }} as numeric(14, 2)) as revenue
from {{ source("payments_source", "payments_raw") }}
