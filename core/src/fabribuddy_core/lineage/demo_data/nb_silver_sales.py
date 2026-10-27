# Fabric notebook source

# META ********************

# META {
# META   "dependencies": {
# META     "lakehouse": {
# META       "default_lakehouse": "a1000000-0000-4000-8000-000000000002",
# META       "default_lakehouse_name": "lh_silver"
# META     }
# META   }
# META }

# CELL ********************

from delta.tables import DeltaTable
from pyspark.sql.functions import col, to_date, trim, upper

# CELL ********************

st = spark.read.table("lh_bronze.d365.salestable")
sl = spark.read.table("lh_bronze.d365.salesline")

# CELL ********************

lines = sl.join(st, ["SALESID", "DATAAREAID"]).select(
    col("SALESID").alias("sales_id"),
    col("LINENUM").alias("line_num"),
    upper(trim(col("CUSTACCOUNT"))).alias("customer_id"),
    to_date(col("CREATEDDATETIME")).alias("order_date"),
    col("ITEMID").alias("item_id"),
    col("SALESQTY").alias("quantity"),
    col("CURRENCYCODE").alias("currency"),
    col("LINEAMOUNT").alias("gross_amount"),
    col("LINEDISC").alias("discount_amount"),
    (col("LINEAMOUNT") - col("LINEDISC")).alias("net_amount"),
    col("DATAAREAID").alias("company"),
)

# CELL ********************

(
    DeltaTable.forName(spark, "lh_silver.sales.sales_lines")
    .alias("t")
    .merge(
        lines.alias("s"),
        "t.sales_id = s.sales_id AND t.line_num = s.line_num AND t.company = s.company",
    )
    .whenMatchedUpdateAll()
    .whenNotMatchedInsertAll()
    .execute()
)
