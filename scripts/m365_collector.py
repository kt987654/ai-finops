"""Create the M365 Graph usage collector identity without changing privacy settings.

Grants Reports.Read.All (application) with admin consent. Tenant-wide report-name
concealment is left unchanged.
The client secret is returned on stdout only when --emit-secret is passed so the Fabric
setup script can place it directly into the Fabric connection; it is never written to disk.
"""

import json
import shutil
import subprocess
import sys
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = ROOT / ".deployment" / "azure.json"
APP_NAME = "Unified AI FinOps Collector"
GRAPH_APP_ID = "00000003-0000-0000-c000-000000000000"
GRAPH = "https://graph.microsoft.com/v1.0"


def az(*args):
    result = subprocess.run([shutil.which("az"), *args, "-o", "json"], capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip())
    return json.loads(result.stdout) if result.stdout.strip() else None


_graph_token = {}


def graph(method, path, body=None):
    if "value" not in _graph_token:
        _graph_token["value"] = az("account", "get-access-token", "--resource", "https://graph.microsoft.com")["accessToken"]
    request = Request(GRAPH + path.replace(" ", "%20").replace("'", "%27"), method=method,
                      data=None if body is None else json.dumps(body).encode(),
                      headers={"Authorization": f"Bearer {_graph_token['value']}", "Content-Type": "application/json"})
    try:
        with urlopen(request, timeout=60) as response:
            text = response.read().decode("utf-8-sig")
            return json.loads(text) if text.strip() else None
    except HTTPError as error:
        raise RuntimeError(f"Graph {method} {path.split('?')[0]} returned {error.code}: "
                           f"{error.read().decode()[:400]}") from None


def main():
    cfg = json.loads(CONFIG_PATH.read_text())
    graph_sp = graph("GET", f"/servicePrincipals(appId='{GRAPH_APP_ID}')?$select=id,appRoles")
    roles = {r["value"]: r["id"] for r in graph_sp["appRoles"]}
    reports_read = roles["Reports.Read.All"]

    apps = graph("GET", f"/applications?$filter=displayName eq '{APP_NAME}'&$select=id,appId")["value"]
    if len(apps) > 1:
        raise RuntimeError("Multiple collector apps exist; refusing to choose.")
    if apps:
        app = apps[0]
    else:
        app = graph("POST", "/applications", {
            "displayName": APP_NAME, "signInAudience": "AzureADMyOrg",
            "requiredResourceAccess": [{"resourceAppId": GRAPH_APP_ID, "resourceAccess": [
                {"id": reports_read, "type": "Role"}]}]})
    sps = graph("GET", f"/servicePrincipals?$filter=appId eq '{app['appId']}'&$select=id")["value"]
    sp = sps[0] if sps else graph("POST", "/servicePrincipals", {"appId": app["appId"]})

    assigned = {a["appRoleId"]: a["id"] for a in
                graph("GET", f"/servicePrincipals/{sp['id']}/appRoleAssignments")["value"]}
    for role in (reports_read,):
        if role not in assigned:
            created = graph("POST", f"/servicePrincipals/{sp['id']}/appRoleAssignments",
                            {"principalId": sp["id"], "resourceId": graph_sp["id"], "appRoleId": role})
            assigned[role] = created["id"]

    expiry = (datetime.now(timezone.utc) + timedelta(days=180)).strftime("%Y-%m-%dT%H:%M:%SZ")
    # Only called when no Fabric Graph connection exists, so earlier fabric-connection secrets are unused.
    for credential in graph("GET", f"/applications/{app['id']}?$select=passwordCredentials")["passwordCredentials"]:
        if (credential.get("displayName") or "").startswith("fabric-connection-"):
            graph("POST", f"/applications/{app['id']}/removePassword", {"keyId": credential["keyId"]})
    secret = graph("POST", f"/applications/{app['id']}/addPassword", {"passwordCredential": {
        "displayName": f"fabric-connection-{uuid.uuid4().hex[:8]}", "endDateTime": expiry}})["secretText"]

    cfg.update({"collector_app_id": app["appId"], "collector_sp_id": sp["id"], "collector_secret_expiry": expiry})
    CONFIG_PATH.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
    status = {"collector_app_id": app["appId"], "report_names_concealed": "unchanged",
              "report_settings_permission": "not requested", "secret_expires": expiry}
    print(json.dumps(status), file=sys.stderr)
    if "--emit-secret" in sys.argv:
        print(secret)


if __name__ == "__main__":
    main()
