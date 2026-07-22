# DataRescue dbt demo

The project deliberately separates structural correctness from business
correctness. Both candidate columns compile and pass eight dbt data tests, but
only `net_amount` reconciles to the last-good revenue contract.

The candidate is selected with one allowlisted environment variable:

```bash
DATARESCUE_REVENUE_COLUMN=gross_amount DBT_SCHEMA=candidate_gross dbt build
DATARESCUE_REVENUE_COLUMN=net_amount DBT_SCHEMA=candidate_net dbt build
```

`stg_payments.sql` rejects any identifier outside its explicit allowlist at
compile time. Candidate selection therefore cannot be used to inject SQL.

The eight data tests are:

1. `stg_payments.payment_id` is not null.
2. `stg_payments.payment_id` is unique.
3. `stg_payments.customer_id` is not null.
4. `stg_payments.currency` is `AUD`.
5. `stg_payments.revenue` is not null.
6. `fct_revenue.payment_id` is not null.
7. `fct_revenue.payment_id` is unique.
8. `fct_revenue.revenue` is not null.

Use the root Makefile for the reproducible flow:

```bash
make demo-drift
make dbt-candidates
```
