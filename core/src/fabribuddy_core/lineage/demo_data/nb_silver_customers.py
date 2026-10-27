# Fabric notebook source

# META ********************

# META {
# META   "kernel_info": { "name": "synapse_pyspark" },
# META   "dependencies": {
# META     "lakehouse": {
# META       "default_lakehouse": "a1000000-0000-4000-8000-000000000002",
# META       "default_lakehouse_name": "lh_silver",
# META       "default_lakehouse_workspace_id": "11111111-1111-4111-8111-111111111111"
# META     }
# META   }
# META }

# MARKDOWN ********************

# ## Silver · customers
# Cleans the D365 customer master. One row per customer and legal entity.

# CELL ********************

from pyspark.sql.functions import col, current_timestamp, initcap, lower, trim, upper

# CELL ********************

df = spark.read.table("lh_bronze.d365.custtable")

# CELL ********************

customers = (
    df.select(
        upper(trim(col("ACCOUNTNUM"))).alias("customer_id"),
        initcap(trim(col("NAME"))).alias("customer_name"),
        col("CUSTGROUP").alias("customer_group"),
        col("COUNTRYREGIONID").alias("country_code"),
        lower(col("EMAIL")).alias("email"),
        col("DATAAREAID").alias("company"),
    )
    .withColumn("_loaded_at", current_timestamp())
)

customers.write.mode("overwrite").saveAsTable("sales.customers")
