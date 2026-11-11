# Fabric notebook source

# MARKDOWN ********************

# Shared settings for the Contoso sales notebooks, included with %run.

# CELL ********************

env = "prod"
spark.conf.set("spark.sql.parquet.vorder.default", "true")
spark.conf.set("spark.microsoft.delta.optimizeWrite.enabled", "true")
