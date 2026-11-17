"""Fabric notebook body: transform real AI usage into curated tables and the app snapshot.

Inputs (written by pipeline pl_aifinops_ingest):
  bronze.apim_usage_events                 APIM per-user trace events copied from Log Analytics
  Files/raw/m365/copilot_user_detail/*.json Graph M365 Copilot usage user detail (optional)
Outputs:
  silver.ai_usage_events, silver.m365_copilot_users
  gold.ai_usage_daily, gold.source_status, gold.run_history
  Files/published/enterprise_usage.json   aggregate-only snapshot read by the Rayfin app
"""

import json
import uuid
from datetime import datetime, timedelta, timezone

import notebookutils
from pyspark.sql import Window
from pyspark.sql import functions as F

spark.conf.set("spark.sql.session.timeZone", "UTC")
run_id = str(uuid.uuid4())
started_at = datetime.now(timezone.utc)
for schema in ("bronze", "silver", "gold", "config"):
    spark.sql(f"CREATE SCHEMA IF NOT EXISTS {schema}")

DEFAULT_RATES = [
    # Azure Retail Prices API, Foundry Models, Global Standard, northcentralus (USD per 1M tokens).
    ("gpt-4.1-mini", 0.40, 0.10, 1.60, "Azure list price, Global Standard; verify before use"),
    ("gpt-5.4-mini", 0.75, 0.075, 4.50, "Azure Retail Prices API, Global Standard, northcentralus"),
    ("gpt-5.4-nano", 0.20, 0.02, 1.25, "Azure Retail Prices API, Global Standard, northcentralus"),
]
rate_schema = "model STRING, input_per_1m DOUBLE, cached_input_per_1m DOUBLE, output_per_1m DOUBLE, rate_source STRING"
defaults = spark.createDataFrame(DEFAULT_RATES, rate_schema)
if not spark.catalog.tableExists("config.model_rates"):
    defaults.write.format("delta").saveAsTable("config.model_rates")
else:
    # Add newly supported models without overwriting rates someone has edited.
    missing = defaults.join(spark.table("config.model_rates").select("model"), "model", "left_anti")
    if missing.count():
        missing.write.format("delta").mode("append").saveAsTable("config.model_rates")
rates = spark.table("config.model_rates")

# ---- Foundry agent usage metered by APIM -------------------------------------------
if not spark.catalog.tableExists("bronze.apim_usage_events"):
    raise RuntimeError("bronze.apim_usage_events is missing; the Log Analytics copy did not run.")
bronze = spark.table("bronze.apim_usage_events").drop("prompt", "completion", "client_ip")
latest = Window.partitionBy("apim_request_id").orderBy(F.col("ingested_at").desc_nulls_last())
events = (
    bronze.where(F.col("apim_request_id").isNotNull() & F.col("user_oid").isNotNull() & (F.col("user_oid") != ""))
    .withColumn("_rank", F.row_number().over(latest)).where("_rank = 1").drop("_rank")
    .withColumn("event_time", F.to_timestamp("event_time"))
    .withColumn("usage_date", F.to_date("event_time"))
    .withColumn("succeeded", F.col("status_code") < 400)
)
for metric in ("input_tokens", "output_tokens", "total_tokens", "cached_tokens", "reasoning_tokens", "status_code"):
    events = events.withColumn(metric, F.col(metric).cast("long"))
events.write.format("delta").mode("overwrite").option("overwriteSchema", "true").saveAsTable("silver.ai_usage_events")
if events.select("apim_request_id").distinct().count() != events.count():
    raise RuntimeError("Duplicate APIM request IDs survived deduplication.")

priced = (
    events.join(rates, on="model", how="left")
    .withColumn("billable_input", F.col("input_tokens") - F.coalesce(F.col("cached_tokens"), F.lit(0)))
    .withColumn("estimated_cost_usd", F.when(F.col("input_per_1m").isNull(), F.lit(None).cast("double")).otherwise(
        (F.col("billable_input") * F.col("input_per_1m")
         + F.coalesce(F.col("cached_tokens"), F.lit(0)) * F.col("cached_input_per_1m")
         + F.col("output_tokens") * F.col("output_per_1m")) / 1_000_000))
)
daily = (
    priced.groupBy("usage_date", "tenant_id", "user_oid", "agent_name", "model")
    .agg(
        F.max_by("user_upn", "event_time").alias("user_upn"),
        F.max_by("user_name", "event_time").alias("user_name"),
        F.sum(F.col("succeeded").cast("long")).alias("requests"),
        F.sum((~F.col("succeeded")).cast("long")).alias("failed_requests"),
        F.sum(F.when(F.col("succeeded"), F.col("input_tokens"))).alias("input_tokens"),
        F.sum(F.when(F.col("succeeded"), F.col("output_tokens"))).alias("output_tokens"),
        F.sum(F.when(F.col("succeeded"), F.col("total_tokens"))).alias("total_tokens"),
        F.sum(F.when(F.col("succeeded"), F.col("estimated_cost_usd"))).alias("estimated_cost_usd"),
        F.max(F.col("input_per_1m").isNull().cast("int")).alias("missing_rate"),
    )
    .withColumn("ecosystem", F.lit("foundry"))
    .withColumn("source_system", F.lit("apim"))
)
daily.write.format("delta").mode("overwrite").option("overwriteSchema", "true").saveAsTable("gold.ai_usage_daily")

# ---- Microsoft 365 Copilot (Graph reports) ----------------------------------------
m365_status, m365_rows, m365_note = "no_data", 0, "No Graph export landed in this run."
m365_users = []
raw_path = "Files/raw/m365/copilot_user_detail"
if notebookutils.fs.exists(raw_path) and notebookutils.fs.ls(raw_path):
    newest = sorted(notebookutils.fs.ls(raw_path), key=lambda f: f.modifyTime)[-1].path
    raw = spark.read.json(newest)
    if raw.columns == ["_corrupt_record"]:
        raw = spark.read.option("multiLine", "true").json(newest)
    from pyspark.sql.types import ArrayType, StructType
    value_type = raw.schema["value"].dataType if "value" in raw.columns else None
    if value_type is not None and isinstance(value_type, ArrayType) and isinstance(value_type.elementType, StructType):
        records = raw.select(F.explode("value").alias("r")).select("r.*")
    elif value_type is not None:
        records = spark.createDataFrame([], "userPrincipalName STRING")  # Graph returned an empty list
    else:
        records = raw
    if records.columns and "userPrincipalName" in records.columns:
        cols = [c for c in records.columns if c != "copilotActivityUserDetailsByPeriod"]
        m365 = records.select(*cols)
        m365.write.format("delta").mode("overwrite").option("overwriteSchema", "true").saveAsTable("silver.m365_copilot_users")
        m365_users = [r.asDict() for r in m365.limit(5000).collect()]
        m365_rows = len(m365_users)
        m365_status = "connected"
        m365_note = ("Graph returned licensed Microsoft 365 Copilot users." if m365_rows
                     else "Graph connected; no licensed Microsoft 365 Copilot users reported.")
    else:
        m365_status, m365_note = "connected", "Graph connected; no licensed Microsoft 365 Copilot users reported."

# ---- Serving snapshot (aggregates only; no prompts or completions) ---------------
latest_event = events.agg(F.max("event_time")).first()[0]
as_of = started_at.date()
window_start = as_of - timedelta(days=89)
daily_rows = [
    {**row.asDict(), "usage_date": row["usage_date"].isoformat()}
    for row in spark.table("gold.ai_usage_daily")
    .where(F.col("usage_date") >= F.lit(window_start.isoformat()))
    .orderBy("usage_date", "user_oid", "agent_name", "model").limit(50_000).collect()
]
for row in daily_rows:
    for key in ("input_tokens", "output_tokens", "total_tokens", "requests", "failed_requests", "missing_rate"):
        row[key] = int(row[key] or 0)
    row["estimated_cost_usd"] = None if row["estimated_cost_usd"] is None else round(float(row["estimated_cost_usd"]), 6)
    row["missing_rate"] = bool(row["missing_rate"])
for user in m365_users:
    for key, value in list(user.items()):
        if hasattr(value, "isoformat"):
            user[key] = value.isoformat()

completed_at = datetime.now(timezone.utc)
sources = [
    {"source": "foundry_apim", "label": "Foundry agents via APIM", "status": "connected",
     "rows": events.count(), "last_event_time": latest_event.isoformat() if latest_event else None,
     "note": "Per-user Entra identity, agent, model and token counts from APIM traces in Log Analytics."},
    {"source": "m365_graph", "label": "Microsoft 365 Copilot (Graph)", "status": m365_status,
     "rows": m365_rows, "last_event_time": None, "note": m365_note},
    {"source": "github", "label": "GitHub Copilot", "status": "not_connected", "rows": 0,
     "last_event_time": None, "note": "Requires organization owner or enterprise access to Copilot usage reports."},
]
snapshot = {
    "schema_version": 2,
    "is_synthetic": False,
    "timezone": "UTC",
    "as_of_date": as_of.isoformat(),
    "coverage_start": window_start.isoformat(),
    "generated_at": completed_at.isoformat(),
    "run_id": run_id,
    "daily": daily_rows,
    "m365_users": m365_users,
    "sources": sources,
    "rates": [r.asDict() for r in rates.collect()],
}
notebookutils.fs.mkdirs("Files/published")
notebookutils.fs.put("Files/published/enterprise_usage.json", json.dumps(snapshot, allow_nan=False, default=str), True)
spark.createDataFrame([(s["source"], s["status"], s["rows"], s["note"], completed_at.isoformat()) for s in sources],
                      "source STRING, status STRING, rows LONG, note STRING, checked_at STRING") \
    .write.format("delta").mode("overwrite").saveAsTable("gold.source_status")
summary = {"run_id": run_id, "status": "succeeded", "completed_at": completed_at.isoformat(),
           "foundry_events": events.count(), "daily_rows": len(daily_rows), "m365_users": m365_rows}
spark.createDataFrame([summary]).write.format("delta").mode("append").option("mergeSchema", "true").saveAsTable("gold.run_history")
print(json.dumps(summary))
