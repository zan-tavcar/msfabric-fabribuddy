# Fabric notebook source

# META ********************

# META {
# META   "dependencies": {
# META     "lakehouse": {
# META       "default_lakehouse": "a1000000-0000-4000-8000-000000000003",
# META       "default_lakehouse_name": "lh_gold"
# META     }
# META   }
# META }

# CELL ********************

%%sql
CREATE OR REPLACE TABLE lh_gold.dbo.dim_customer AS
SELECT
    xxhash64(customer_id, company) AS customer_key,
    customer_id,
    customer_name,
    customer_group,
    country_code
FROM lh_silver.sales.customers;

# CELL ********************

%%sql
CREATE OR REPLACE TABLE lh_gold.dbo.fact_sales AS
SELECT
    l.sales_id,
    l.line_num,
    xxhash64(l.customer_id, l.company) AS customer_key,
    l.order_date,
    l.item_id,
    l.quantity,
    l.net_amount,
    l.net_amount * fx.rate_to_eur AS net_amount_eur
FROM lh_silver.sales.sales_lines l
LEFT JOIN lh_silver.ref.fx_rates fx
    ON fx.currency = l.currency
   AND fx.rate_date = l.order_date;
