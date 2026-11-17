"""Provision the real-data Fabric ingestion path: lakehouse, notebook, Graph connection, pipeline, schedule.

Actions:
  setup     create/update items (idempotent; never overwrites untracked items)
  run       run the pipeline once and wait (use --lookback 30d for the first backfill)
  schedule  enable the recurring pipeline schedule (hourly)
  verify    read the published app snapshot and summarize it
"""

import argparse
import base64
import json
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.request import Request, urlopen

sys.path.insert(0, str(Path(__file__).resolve().parent))
from fabric_demo import API, Fabric  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = ROOT / ".deployment" / "azure.json"
LAKEHOUSE = "aifinops_lakehouse"
NOTEBOOK = "nb_aifinops_transform"
PIPELINE = "pl_aifinops_ingest"
GRAPH_CONNECTION = "aifinops-graph-reports"
GRAPH_RELATIVE_URL = "beta/reports/getMicrosoft365CopilotUsageUserDetail(period='D28')?$format=application/json"
COPY_APIM = "Copy APIM usage from Log Analytics"
COPY_M365 = "Copy M365 Copilot usage from Graph"

KQL = """let lookback = {lookback};
ApiManagementGatewayLogs
| where TimeGenerated > ago(lookback)
| where isnotempty(TraceRecords)
| mv-expand trace = parse_json(tostring(TraceRecords))
| where tostring(trace.source) == 'unified-ai-finops'
| extend e = parse_json(tostring(trace.message))
| where tostring(e.event) == 'ai_usage'
| project
    log_time = TimeGenerated,
    correlation_id = CorrelationId,
    apim_request_id = tostring(e.apim_request_id),
    event_time = tostring(e.event_time),
    tenant_id = tostring(e.tenant_id),
    user_oid = tostring(e.user_oid),
    user_upn = tostring(e.user_upn),
    user_name = tostring(e.user_name),
    client_app_id = tostring(e.client_app_id),
    agent_name = tostring(e.agent_name),
    agent_version = tostring(e.agent_version),
    model = tostring(e.model),
    response_id = tostring(e.response_id),
    status_code = tolong(e.status_code),
    input_tokens = tolong(e.input_tokens),
    output_tokens = tolong(e.output_tokens),
    total_tokens = tolong(e.total_tokens),
    cached_tokens = tolong(e.cached_tokens),
    reasoning_tokens = tolong(e.reasoning_tokens),
    gateway_total_ms = TotalTime"""


def save(cfg):
    CONFIG_PATH.write_text(json.dumps(cfg, indent=2), encoding="utf-8")


def ensure_item(client, cfg, kind, name, key, body_extra=None):
    ws = cfg["fabric_workspace_id"]
    matches = [i for i in client.items(ws) if i["type"] == kind and i["displayName"] == name]
    if len(matches) > 1:
        raise RuntimeError(f"Multiple {kind} items named {name}; refusing to choose.")
    if matches:
        if cfg.get(key) != matches[0]["id"]:
            raise RuntimeError(f"{name} exists but is not tracked by this project; refusing to overwrite.")
        return matches[0]
    body = {"displayName": name, "type": kind,
            "description": "Unified AI FinOps - real usage ingestion (APIM/Log Analytics, Graph)."}
    body.update(body_extra or {})
    item = client.write("POST", f"/workspaces/{ws}/items", body, need_result=True)
    cfg[key] = item["id"]
    save(cfg)
    return item


def notebook_definition(cfg):
    import nbformat
    cells = [
        nbformat.v4.new_markdown_cell("# Unified AI FinOps - transform and publish\n"
                                      "Real usage: APIM traces from Log Analytics and Microsoft Graph reports."),
        nbformat.v4.new_code_cell((ROOT / "fabric" / "enterprise_job.py").read_text(encoding="utf-8")),
    ]
    nb = nbformat.v4.new_notebook(cells=cells, metadata={
        "kernelspec": {"display_name": "Synapse PySpark", "language": "python", "name": "synapse_pyspark"},
        "language_info": {"name": "python"},
        "dependencies": {"lakehouse": {"default_lakehouse": cfg["fabric_lakehouse_id"],
                                       "default_lakehouse_name": LAKEHOUSE,
                                       "default_lakehouse_workspace_id": cfg["fabric_workspace_id"]}},
    })
    nbformat.validate(nb)
    text = nbformat.writes(nb)
    return {"format": "ipynb", "parts": [{"path": "notebook-content.ipynb", "payloadType": "InlineBase64",
                                          "payload": base64.b64encode(text.encode()).decode()}]}


def lakehouse_link(cfg, root_folder):
    return {"name": LAKEHOUSE, "properties": {"annotations": [], "type": "Lakehouse", "typeProperties": {
        "workspaceId": cfg["fabric_workspace_id"], "artifactId": cfg["fabric_lakehouse_id"], "rootFolder": root_folder}}}


def pipeline_definition(cfg):
    query = KQL.replace("{lookback}", "@{pipeline().parameters.lookback}")
    activities = [
        {
            "name": COPY_APIM, "type": "Copy", "dependsOn": [],
            "policy": {"timeout": "0.01:00:00", "retry": 2, "retryIntervalInSeconds": 60,
                       "secureInput": True, "secureOutput": True},
            "typeProperties": {
                "source": {
                    "type": "AzureDataExplorerSource",
                    "query": {"value": query, "type": "Expression"},
                    "queryTimeout": "00:10:00", "noTruncation": False,
                    "additionalColumns": [
                        {"name": "pipeline_run_id", "value": {"value": "@pipeline().RunId", "type": "Expression"}},
                        {"name": "ingested_at", "value": {"value": "@utcNow()", "type": "Expression"}},
                    ],
                    "datasetSettings": {"annotations": [], "type": "AzureDataExplorerTable", "schema": [],
                                        "typeProperties": {"database": cfg["log_analytics"]},
                                        "externalReferences": {"connection": cfg["fabric_la_connection_id"]}},
                },
                "sink": {
                    "type": "LakehouseTableSink", "tableActionOption": "Append",
                    "datasetSettings": {"annotations": [], "type": "LakehouseTable", "schema": [],
                                        "linkedService": lakehouse_link(cfg, "Tables"),
                                        "typeProperties": {"schema": "bronze", "table": "apim_usage_events"}},
                },
                "enableStaging": False,
                "translator": {"type": "TabularTranslator", "typeConversion": True,
                               "typeConversionSettings": {"allowDataTruncation": True, "treatBooleanAsNumber": False}},
            },
        },
        {
            "name": COPY_M365, "type": "Copy", "dependsOn": [],
            "policy": {"timeout": "0.00:30:00", "retry": 1, "retryIntervalInSeconds": 60,
                       "secureInput": True, "secureOutput": True},
            "typeProperties": {
                "source": {
                    "type": "RestSource", "requestMethod": "GET", "httpRequestTimeout": "00:05:00",
                    "requestInterval": "00.00:00:00.010",
                    "paginationRules": {"AbsoluteUrl": "$['@odata.nextLink']"},
                    "datasetSettings": {"annotations": [], "type": "RestResource", "schema": [],
                                        "typeProperties": {"relativeUrl": GRAPH_RELATIVE_URL},
                                        "externalReferences": {"connection": cfg["fabric_graph_connection_id"]}},
                },
                "sink": {
                    "type": "JsonSink",
                    "storeSettings": {"type": "LakehouseWriteSettings"},
                    "formatSettings": {"type": "JsonWriteSettings", "filePattern": "setOfObjects"},
                    "datasetSettings": {"annotations": [], "type": "Json", "schema": {},
                                        "linkedService": lakehouse_link(cfg, "Files"),
                                        "typeProperties": {"location": {
                                            "type": "LakehouseLocation",
                                            "folderPath": "raw/m365/copilot_user_detail",
                                            "fileName": {"value": "@concat(pipeline().RunId, '.json')",
                                                         "type": "Expression"}}}},
                },
                "enableStaging": False,
            },
        },
        {
            "name": "Transform and publish", "type": "TridentNotebook",
            "dependsOn": [{"activity": COPY_APIM, "dependencyConditions": ["Succeeded"]},
                          {"activity": COPY_M365, "dependencyConditions": ["Completed"]}],
            "policy": {"timeout": "0.01:00:00", "retry": 1, "retryIntervalInSeconds": 120,
                       "secureInput": False, "secureOutput": False},
            "typeProperties": {"notebookId": cfg["fabric_notebook_id"], "workspaceId": cfg["fabric_workspace_id"]},
        },
    ]
    content = {"properties": {
        "description": "Ingest per-user AI usage from APIM (Log Analytics) and Microsoft Graph, then publish.",
        "activities": activities,
        "parameters": {"lookback": {"type": "string", "defaultValue": "3d"}},
    }}
    return {"parts": [{"path": "pipeline-content.json", "payloadType": "InlineBase64",
                       "payload": base64.b64encode(json.dumps(content).encode()).decode()}]}


def ensure_graph_connection(client, cfg):
    _, _, page = client.request("GET", "/connections")
    found = [c for c in page["value"] if c["displayName"] == GRAPH_CONNECTION]
    if found:
        cfg["fabric_graph_connection_id"] = found[0]["id"]
        save(cfg)
        return
    result = subprocess.run([sys.executable, str(ROOT / "scripts" / "m365_collector.py"), "--emit-secret"],
                            capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"Collector setup failed: {result.stderr.strip()[-800:]}")
    print(result.stderr.strip(), file=sys.stderr)
    secret = result.stdout.strip()
    cfg.update(json.loads(CONFIG_PATH.read_text()))
    body = {
        "connectivityType": "ShareableCloud", "displayName": GRAPH_CONNECTION, "privacyLevel": "Organizational",
        "connectionDetails": {"type": "RestService", "creationMethod": "RestService.Contents", "parameters": [
            {"dataType": "Text", "name": "baseUrl", "value": "https://graph.microsoft.com/"},
            {"dataType": "Text", "name": "audience", "value": "https://graph.microsoft.com"}]},
        "credentialDetails": {"singleSignOnType": "None", "connectionEncryption": "NotEncrypted",
                              "skipTestConnection": False, "credentials": {
                                  "credentialType": "ServicePrincipal", "tenantId": cfg["tenant_id"],
                                  "servicePrincipalClientId": cfg["collector_app_id"],
                                  "servicePrincipalSecret": secret}},
    }
    _, _, created = client.request("POST", "/connections", body)
    del secret, body
    cfg["fabric_graph_connection_id"] = created["id"]
    save(cfg)


def setup(client, cfg):
    ensure_graph_connection(client, cfg)
    ensure_item(client, cfg, "Lakehouse", LAKEHOUSE, "fabric_lakehouse_id",
                {"creationPayload": {"enableSchemas": True}})
    notebook = ensure_item(client, cfg, "Notebook", NOTEBOOK, "fabric_notebook_id")
    client.write("POST", f"/workspaces/{cfg['fabric_workspace_id']}/notebooks/{notebook['id']}/updateDefinition",
                 {"definition": notebook_definition(cfg)})
    pipeline = ensure_item(client, cfg, "DataPipeline", PIPELINE, "fabric_pipeline_id")
    client.write("POST", f"/workspaces/{cfg['fabric_workspace_id']}/items/{pipeline['id']}/updateDefinition",
                 {"definition": pipeline_definition(cfg)})
    cfg["snapshot_url"] = (f"https://onelake.dfs.fabric.microsoft.com/{cfg['fabric_workspace_id']}/"
                           f"{cfg['fabric_lakehouse_id']}/Files/published/enterprise_usage.json")
    cfg.setdefault("dashboard_viewers", [client.owner_email])
    save(cfg)
    (ROOT / "rayfin" / "functions" / "src" / "deployment.ts").write_text(
        "export const deployment = " + json.dumps({"snapshotUrl": cfg["snapshot_url"],
                                                   "viewerEmails": cfg["dashboard_viewers"]}, indent=2) + ";\n",
        encoding="utf-8")
    print(json.dumps({k: cfg[k] for k in ("fabric_lakehouse_id", "fabric_notebook_id", "fabric_pipeline_id",
                                           "fabric_la_connection_id", "fabric_graph_connection_id")}, indent=2))


def run(client, cfg, lookback):
    ws, item = cfg["fabric_workspace_id"], cfg["fabric_pipeline_id"]
    base = f"/workspaces/{ws}/items/{item}/jobs/instances"
    _, _, history = client.request("GET", base)
    active = [j for j in history.get("value", []) if j["status"] in ("NotStarted", "InProgress")]
    if active:
        url = API + base + "/" + active[0]["id"]
        print("Monitoring the run already in progress.")
    else:
        _, headers, _ = client.request("POST", base + "?jobType=Pipeline",
                                       {"executionData": {"parameters": {"lookback": lookback}}})
        url = headers["Location"]
    deadline, last = time.monotonic() + 1800, None
    while time.monotonic() < deadline:
        _, _, job = client.request("GET", url)
        if job["status"] != last:
            print(f"Pipeline status: {job['status']}", flush=True)
            last = job["status"]
        if job["status"] == "Completed":
            return verify(client, cfg)
        if job["status"] in ("Failed", "Cancelled", "Deduped"):
            raise RuntimeError(f"Pipeline {job['status']}: {json.dumps(job.get('failureReason'))}")
        time.sleep(20)
    raise TimeoutError(f"Pipeline still running; monitor {url}")


def verify(client, cfg):
    headers = {"Authorization": "Bearer " + client.token("https://storage.azure.com")}
    with urlopen(Request(cfg["snapshot_url"], headers=headers), timeout=60) as response:
        snapshot = json.load(response)
    if snapshot.get("schema_version") != 2 or snapshot.get("is_synthetic") is not False:
        raise RuntimeError("Published snapshot is not the real-data schema.")
    print(json.dumps({"as_of_date": snapshot["as_of_date"], "daily_rows": len(snapshot["daily"]),
                      "users": len({r["user_oid"] for r in snapshot["daily"]}),
                      "requests": sum(r["requests"] for r in snapshot["daily"]),
                      "tokens": sum(r["total_tokens"] for r in snapshot["daily"]),
                      "m365_users": len(snapshot["m365_users"]),
                      "sources": {s["source"]: s["status"] for s in snapshot["sources"]}}, indent=2))


def schedule(client, cfg):
    ws, item = cfg["fabric_workspace_id"], cfg["fabric_pipeline_id"]
    path = f"/workspaces/{ws}/items/{item}/jobs/Pipeline/schedules"
    _, _, current = client.request("GET", path)
    if current.get("value"):
        if len(current["value"]) != 1 or current["value"][0]["id"] != cfg.get("fabric_pipeline_schedule_id"):
            raise RuntimeError("Unexpected existing schedule; refusing to create duplicates.")
        print(json.dumps(current["value"][0]))
        return
    now = datetime.now(timezone.utc)
    created = client.write("POST", path, {"enabled": True, "configuration": {
        "type": "Cron", "interval": 60, "localTimeZoneId": "UTC",
        "startDateTime": now.strftime("%Y-%m-%dT%H:%M:%S"),
        "endDateTime": (now + timedelta(days=365)).strftime("%Y-%m-%dT%H:%M:%S")}})
    cfg["fabric_pipeline_schedule_id"] = created["id"]
    save(cfg)
    _, _, verified = client.request("GET", f"{path}/{created['id']}")
    if not verified["enabled"] or verified["configuration"].get("interval") != 60:
        raise RuntimeError("Schedule readback did not match an enabled hourly run.")
    print(json.dumps(verified))


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("action", choices=("setup", "run", "schedule", "verify"))
    parser.add_argument("--lookback", default="3d")
    args = parser.parse_args()
    cfg = json.loads(CONFIG_PATH.read_text())
    client = Fabric(cfg["tenant_id"])
    if args.action == "setup":
        setup(client, cfg)
    elif args.action == "run":
        run(client, cfg, args.lookback)
    elif args.action == "schedule":
        schedule(client, cfg)
    else:
        verify(client, cfg)


if __name__ == "__main__":
    main()
