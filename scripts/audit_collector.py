"""Grant the collector app audit-log query access and create the Fabric connection used by the pipeline.

Steps (idempotent):
  1. Grant AuditLogsQuery.Read.All (application) to "Unified AI FinOps Collector".
  2. Rotate a dedicated "fabric-audit-*" client secret (other secrets are untouched).
  3. Probe: run a 7-day CopilotInteraction audit query with that secret and report the record count.
  4. Create/replace the Fabric WebForPipeline connection "aifinops-graph-audit" holding the secret.
The secret is never printed or written to disk.
"""

import json
import sys
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

sys.path.insert(0, str(Path(__file__).resolve().parent))
from fabric_demo import Fabric  # noqa: E402
from m365_collector import CONFIG_PATH, GRAPH, GRAPH_APP_ID, app_token, graph  # noqa: E402

CONNECTION = "aifinops-graph-audit"
SECRET_PREFIX = "fabric-audit-"
AUDIT_ROLE = "AuditLogsQuery.Read.All"


def app_graph(token, method, path, body=None):
    request = Request("https://graph.microsoft.com" + path, method=method,
                      data=None if body is None else json.dumps(body).encode(),
                      headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
    with urlopen(request, timeout=60) as response:
        return json.load(response)


def probe(tenant, client_id, secret):
    end = datetime.now(timezone.utc)
    body = {"displayName": f"aifinops-probe-{uuid.uuid4().hex[:6]}",
            "filterStartDateTime": (end - timedelta(days=7)).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "filterEndDateTime": end.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "recordTypeFilters": ["copilotInteraction"]}
    for attempt in range(20):
        try:
            token = app_token(tenant, client_id, secret)
            query = app_graph(token, "POST", "/beta/security/auditLog/queries", body)
            break
        except HTTPError as error:
            if error.code not in (401, 403) or attempt == 19:
                raise RuntimeError(f"Audit query start failed: {error.code} {error.read().decode()[-1200:]}")
            time.sleep(20)  # app role and new secret propagation
    deadline = time.monotonic() + 1200
    while time.monotonic() < deadline:
        status = app_graph(token, "GET", f"/beta/security/auditLog/queries/{query['id']}")["status"]
        if status in ("succeeded", "failed", "cancelled"):
            break
        time.sleep(20)
    if status != "succeeded":
        raise RuntimeError(f"Audit query ended with status {status}.")
    records = app_graph(token, "GET", f"/beta/security/auditLog/queries/{query['id']}/records?$top=999")["value"]
    sample = [{"user": r.get("userPrincipalName"), "time": r.get("createdDateTime"),
               "operation": r.get("operation"),
               "app_host": (r.get("auditData") or {}).get("CopilotEventData", {}).get("AppHost")}
              for r in records[:5]]
    return {"query_id": query["id"], "records_7d": len(records), "sample": sample}


def main():
    cfg = json.loads(CONFIG_PATH.read_text())
    tenant, app_id, sp_id = cfg["tenant_id"], cfg["collector_app_id"], cfg["collector_sp_id"]
    graph_sp = graph("GET", f"/servicePrincipals(appId='{GRAPH_APP_ID}')?$select=id,appRoles")
    role_id = {r["value"]: r["id"] for r in graph_sp["appRoles"]}[AUDIT_ROLE]
    assigned = {a["appRoleId"] for a in graph("GET", f"/servicePrincipals/{sp_id}/appRoleAssignments")["value"]}
    if role_id not in assigned:
        graph("POST", f"/servicePrincipals/{sp_id}/appRoleAssignments",
              {"principalId": sp_id, "resourceId": graph_sp["id"], "appRoleId": role_id})

    app = graph("GET", f"/applications(appId='{app_id}')?$select=id,passwordCredentials")
    for credential in app["passwordCredentials"]:
        if (credential.get("displayName") or "").startswith(SECRET_PREFIX):
            graph("POST", f"/applications/{app['id']}/removePassword", {"keyId": credential["keyId"]})
    expiry = (datetime.now(timezone.utc) + timedelta(days=180)).strftime("%Y-%m-%dT%H:%M:%SZ")
    secret = graph("POST", f"/applications/{app['id']}/addPassword", {"passwordCredential": {
        "displayName": SECRET_PREFIX + uuid.uuid4().hex[:8], "endDateTime": expiry}})["secretText"]

    result = probe(tenant, app_id, secret)
    print(json.dumps(result, indent=2), file=sys.stderr)

    client = Fabric(tenant)
    _, _, page = client.request("GET", "/connections")
    for existing in [c for c in page["value"] if c["displayName"] == CONNECTION]:
        client.request("DELETE", f"/connections/{existing['id']}")
    _, _, created = client.request("POST", "/connections", {
        "connectivityType": "ShareableCloud", "displayName": CONNECTION, "privacyLevel": "Organizational",
        "connectionDetails": {"type": "WebForPipeline", "creationMethod": "WebForPipeline.Contents", "parameters": [
            {"dataType": "Text", "name": "baseUrl", "value": "https://graph.microsoft.com/"},
            {"dataType": "Text", "name": "audience", "value": "https://graph.microsoft.com"}]},
        "credentialDetails": {"singleSignOnType": "None", "connectionEncryption": "NotEncrypted",
                              "skipTestConnection": False, "credentials": {
                                  "credentialType": "ServicePrincipal", "tenantId": tenant,
                                  "servicePrincipalClientId": app_id, "servicePrincipalSecret": secret}}})
    del secret
    cfg.update({"fabric_audit_connection_id": created["id"], "audit_secret_expiry": expiry})
    CONFIG_PATH.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
    print(json.dumps({"fabric_audit_connection_id": created["id"], "secret_expires": expiry}))


if __name__ == "__main__":
    main()
