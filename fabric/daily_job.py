"""Fabric notebook body; synthetic.py is included as the preceding cell."""

import json
import uuid
from datetime import datetime, timezone

import notebookutils
from pyspark.sql import functions as F
from pyspark.sql.types import (
    StructType, StructField, StringType, LongType, DoubleType, BooleanType,
)

spark.conf.set("spark.sql.session.timeZone", "UTC")
spark.conf.set("spark.sql.parquet.vorder.default", "true")
spark.conf.set("spark.databricks.delta.optimizeWrite.enabled", "true")

run_id = str(uuid.uuid4())
started_at = datetime.now(timezone.utc).isoformat()
end = date.fromisoformat(processing_date) if processing_date else datetime.now(timezone.utc).date() - timedelta(days=1)
if end >= datetime.now(timezone.utc).date():
    raise ValueError("Only completed UTC dates may be processed.")

for schema in ("bronze", "silver", "gold"):
    spark.sql(f"CREATE SCHEMA IF NOT EXISTS {schema}")

last_success = None
if spark.catalog.tableExists("gold.run_history"):
    latest = spark.table("gold.run_history").where(F.col("status") == "succeeded").agg(F.max("as_of_date")).first()[0]
    if latest:
        last_success = date.fromisoformat(latest)
# Advance only after publication succeeds, not when an intermediate layer was written.
start = processing_start(end, last_success)

records = generate_records(start, end)
raw_schema = "run_id STRING, ingested_at STRING, source_file STRING, usage_date STRING, payload STRING"
raw = [
    (run_id, started_at, "synthetic-generator-v1", row["usage_date"], json.dumps(row))
    for row in records
]
spark.createDataFrame(raw, raw_schema).write.format("delta").mode("append").saveAsTable("bronze.usage_raw")
if spark.table("bronze.usage_raw").where(F.col("run_id") == run_id).count() != len(records):
    raise RuntimeError("Bronze row count validation failed.")

strings = (
    "record_id", "usage_date", "subject_id", "ecosystem", "display_name", "surface",
    "work_category", "model", "provider", "source_system", "attribution_type",
    "credit_unit", "cost_status", "currency",
)
schema = StructType(
    [StructField(key, StringType(), key == "credit_unit") for key in strings]
    + [StructField(key, LongType(), True) for key in ("input_tokens", "output_tokens", "requests", "prompts")]
    + [StructField(key, DoubleType(), True) for key in ("credits", "estimated_cost_usd")]
    + [StructField("active", LongType(), False), StructField("is_synthetic", BooleanType(), False)]
)
clean = spark.createDataFrame(records, schema).withColumn("last_run_id", F.lit(run_id))
predicate = f"usage_date >= '{start.isoformat()}' AND usage_date <= '{end.isoformat()}'"


def replace_dates(frame, table):
    writer = frame.write.format("delta").mode("overwrite")
    if spark.catalog.tableExists(table):
        writer.option("replaceWhere", predicate).saveAsTable(table)
    else:
        writer.partitionBy("usage_date").saveAsTable(table)


replace_dates(clean, "silver.usage")
stored = spark.table("silver.usage").where(predicate)
if stored.count() != len(records) or stored.select("record_id").distinct().count() != len(records):
    raise RuntimeError("Silver completeness or uniqueness validation failed.")

daily = stored.groupBy("usage_date", "subject_id").agg(
    F.sum("input_tokens").alias("known_input_tokens"),
    F.sum("output_tokens").alias("known_output_tokens"),
    F.sum("requests").alias("known_model_requests"),
    F.sum("prompts").alias("known_prompts"),
    F.sum("credits").alias("m365_demo_credits"),
    F.sum("estimated_cost_usd").alias("known_estimated_cost_usd"),
    F.max("active").alias("active"),
    F.sum(F.when(F.col("input_tokens").isNull(), 1).otherwise(0)).alias("surfaces_missing_tokens"),
    F.sum(F.when(F.col("estimated_cost_usd").isNull(), 1).otherwise(0)).alias("surfaces_missing_cost"),
).withColumn("is_synthetic", F.lit(True)).withColumn("last_run_id", F.lit(run_id))
replace_dates(daily, "gold.user_daily")
replace_dates(stored, "gold.surface_daily")
if spark.table("gold.user_daily").where(predicate).count() != (end - start).days + 1:
    raise RuntimeError("Gold per-user/day row count validation failed.")

# Publish only after all layers validate. The app reads this serving projection, not bundled assets.
window_start = end - timedelta(days=89)
window = f"usage_date >= '{window_start.isoformat()}' AND usage_date <= '{end.isoformat()}'"
published = [
    row.asDict() for row in spark.table("gold.surface_daily")
    .where(window).orderBy("usage_date", "record_id").limit(1001).collect()
]
validate_records(published)
if len(published) != 90 * len(SURFACES):
    raise RuntimeError("Serving projection must contain exactly 90 complete demo days.")
completed_at = datetime.now(timezone.utc).isoformat()
snapshot = {
    "schema_version": SCHEMA_VERSION,
    "is_synthetic": True,
    "subject_id": SUBJECT,
    "timezone": "UTC",
    "as_of_date": end.isoformat(),
    "coverage_start": window_start.isoformat(),
    "generated_at": completed_at,
    "run_id": run_id,
    "rows": published,
}
notebookutils.fs.mkdirs("Files/published")
notebookutils.fs.put("Files/published/usage.json", json.dumps(snapshot, allow_nan=False), True)
summary = {
    "run_id": run_id, "status": "succeeded", "completed_at": completed_at,
    "as_of_date": end.isoformat(), "rows": len(published), "is_synthetic": True,
}
spark.createDataFrame([summary]).write.format("delta").mode("append").saveAsTable("gold.run_history")
print(json.dumps(summary))
