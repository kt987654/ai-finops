"""Create, execute, and schedule this synthetic-only Fabric demo using the current Azure CLI identity."""

import argparse
import base64
import json
import shutil
import subprocess
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
STATE = ROOT / ".deployment" / "fabric.json"
API = "https://api.fabric.microsoft.com/v1"
TAG = "AI Usage synthetic demo v1; no corporate sources."


def az_json(*args):
    executable = shutil.which("az")
    if not executable:
        raise RuntimeError("Azure CLI is required. Install it and sign in to the approved tenant.")
    result = subprocess.run([executable, *args, "-o", "json"], capture_output=True, text=True, check=True)
    return json.loads(result.stdout)


class Fabric:
    def __init__(self, tenant):
        account = az_json("account", "show")
        if account["tenantId"] != tenant:
            raise RuntimeError("Azure CLI is signed in to a different tenant; no changes made.")
        self.owner_email = account["user"]["name"]
        self.tokens = {}

    def token(self, audience):
        cached = self.tokens.get(audience)
        if not cached or time.monotonic() - cached[0] > 2400:
            self.tokens[audience] = (
                time.monotonic(),
                az_json("account", "get-access-token", "--resource", audience)["accessToken"],
            )
        return self.tokens[audience][1]

    def request(self, method, path, body=None):
        url = path if path.startswith("https://") else API + path
        parsed = urlparse(url)
        if (parsed.scheme == "https" and parsed.hostname
                and parsed.hostname.endswith(".analysis.windows.net")
                and parsed.path.startswith("/v1/operations/")):
            url = "https://api.fabric.microsoft.com" + parsed.path
            if parsed.query:
                url += "?" + parsed.query
        if urlparse(url).hostname != "api.fabric.microsoft.com":
            raise ValueError(f"Unexpected Fabric API destination: {urlparse(url).hostname} {urlparse(url).path}")
        data = json.dumps(body).encode() if body is not None else (b"{}" if method == "POST" else None)
        headers = {
            "Authorization": "Bearer " + self.token("https://api.fabric.microsoft.com"),
            "Content-Type": "application/json",
            "x-ms-fabric-skill": "spark-cli",
        }
        try:
            with urlopen(Request(url, data=data, method=method, headers=headers), timeout=90) as response:
                text = response.read().decode()
                return response.status, dict(response.headers), json.loads(text) if text else None
        except HTTPError as error:
            detail = error.read().decode()
            raise RuntimeError(f"Fabric {method} returned {error.code}: {detail}") from None

    def items(self, workspace):
        path = f"/workspaces/{workspace}/items"
        result = []
        while path:
            _, _, page = self.request("GET", path)
            result.extend(page["value"])
            path = page.get("continuationUri")
        return result

    def poll_operation(self, url, need_result=False):
        deadline = time.monotonic() + 900
        while time.monotonic() < deadline:
            _, headers, operation = self.request("GET", url)
            state = operation.get("status")
            if state == "Succeeded":
                return self.request("GET", url.rstrip("/") + "/result")[2] if need_result else operation
            if state in ("Failed", "Cancelled"):
                raise RuntimeError(f"Fabric operation {state}: {operation.get('error')}")
            time.sleep(max(5, int(headers.get("Retry-After", "5"))))
        raise TimeoutError(f"Fabric operation did not complete within 15 minutes: {url}")

    def write(self, method, path, body, need_result=False):
        status, headers, result = self.request(method, path, body)
        if status == 202:
            location = headers.get("Location")
            if not location:
                raise RuntimeError("Fabric accepted the operation without a polling URL. Do not retry blindly.")
            (ROOT / ".deployment" / "pending-operation.json").write_text(
                json.dumps({"location": location, "need_result": need_result}), encoding="utf-8"
            )
            time.sleep(int(headers.get("Retry-After", "5")))
            result = self.poll_operation(location, need_result)
            (ROOT / ".deployment" / "pending-operation.json").unlink()
            return result
        return result


def save(state):
    STATE.parent.mkdir(exist_ok=True)
    STATE.write_text(json.dumps(state, indent=2), encoding="utf-8")


def notebook_definition(workspace, lakehouse):
    import nbformat
    cells = [
        nbformat.v4.new_markdown_cell(
            "# AI usage - SYNTHETIC DATA ONLY\n"
            "Daily UTC demo. No GitHub, M365, Foundry, or APIM service is called."
        ),
        nbformat.v4.new_code_cell('processing_date = ""\n', metadata={"tags": ["parameters"]}),
        nbformat.v4.new_code_cell((ROOT / "fabric" / "synthetic.py").read_text(encoding="utf-8")),
        nbformat.v4.new_code_cell((ROOT / "fabric" / "daily_job.py").read_text(encoding="utf-8")),
    ]
    notebook = nbformat.v4.new_notebook(cells=cells, metadata={
        "kernelspec": {"display_name": "Synapse PySpark", "language": "python", "name": "synapse_pyspark"},
        "language_info": {"name": "python"},
        "dependencies": {"lakehouse": {
            "default_lakehouse": lakehouse["id"],
            "default_lakehouse_name": lakehouse["displayName"],
            "default_lakehouse_workspace_id": workspace,
        }},
    })
    nbformat.validate(notebook)
    serialized = nbformat.writes(notebook)
    (ROOT / ".deployment" / "daily.ipynb").write_text(serialized, encoding="utf-8")
    return {"format": "ipynb", "parts": [{
        "path": "notebook-content.ipynb",
        "payloadType": "InlineBase64",
        "payload": base64.b64encode(serialized.encode()).decode(),
    }]}


def setup(client, state):
    ws = state["workspace_id"]
    pending_path = ROOT / ".deployment" / "pending-operation.json"
    if pending_path.exists():
        pending = json.loads(pending_path.read_text())
        client.poll_operation(pending["location"], pending["need_result"])
        pending_path.unlink()
    _, _, workspace = client.request("GET", f"/workspaces/{ws}")
    if not workspace.get("capacityId"):
        raise RuntimeError("The destination workspace must have an active Fabric capacity.")
    for kind, name, key in (
        ("Lakehouse", "ai_usage_demo_lakehouse", "lakehouse_id"),
        ("Notebook", "ai_usage_demo_daily", "notebook_id"),
    ):
        matches = [i for i in client.items(ws) if i["type"] == kind and i["displayName"] == name]
        if len(matches) > 1:
            raise RuntimeError(f"Multiple items named {name}; refusing to choose.")
        if matches:
            item = matches[0]
            if state.get(key) != item["id"]:
                raise RuntimeError(f"Existing {name} is not in this project's deployment state; refusing to overwrite.")
        else:
            body = {"displayName": name, "type": kind, "description": TAG}
            if kind == "Lakehouse":
                body["creationPayload"] = {"enableSchemas": True}
            item = client.write("POST", f"/workspaces/{ws}/items", body, need_result=True)
            state[key] = item["id"]
            save(state)
        if kind == "Lakehouse":
            lakehouse = item
        else:
            client.write("POST", f"/workspaces/{ws}/notebooks/{item['id']}/updateDefinition",
                         {"definition": notebook_definition(ws, lakehouse)})
    _, _, details = client.request("GET", f"/workspaces/{ws}/lakehouses/{state['lakehouse_id']}")
    state["lakehouse_name"] = details["displayName"]
    state["owner_email"] = client.owner_email
    state["snapshot_url"] = (
        f"https://onelake.dfs.fabric.microsoft.com/{ws}/{state['lakehouse_id']}/Files/published/usage.json"
    )
    save(state)
    config = {"snapshotUrl": state["snapshot_url"], "ownerEmail": state["owner_email"]}
    (ROOT / "rayfin" / "functions" / "src" / "deployment.ts").write_text(
        "export const deployment = " + json.dumps(config, indent=2) + ";\n", encoding="utf-8"
    )
    print(json.dumps({"workspace": workspace["displayName"], "lakehouse": state["lakehouse_id"],
                      "notebook": state["notebook_id"], "mode": "synthetic"}), flush=True)


def run(client, state):
    path = f"/workspaces/{state['workspace_id']}/items/{state['notebook_id']}/jobs/instances"
    _, _, history = client.request("GET", path)
    recent = []
    for job in history.get("value", []):
        when = datetime.fromisoformat(job["startTimeUtc"].replace("Z", "+00:00"))
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        if job["status"] in ("NotStarted", "InProgress") or (
            datetime.now(timezone.utc) - when < timedelta(minutes=5) and job["status"] == "Completed"
        ):
            recent.append(job)
    if recent:
        job_url = API + path + "/" + recent[0]["id"]
    else:
        _, headers, _ = client.request("POST", path + "?jobType=RunNotebook", {
            "executionData": {"configuration": {"defaultLakehouse": {
                "id": state["lakehouse_id"], "name": state["lakehouse_name"], "workspaceId": state["workspace_id"],
            }}},
        })
        job_url = headers.get("Location")
        if not job_url:
            raise RuntimeError("Missing job location; inspect run history before submitting another job.")
        state["last_job_url"] = job_url
        save(state)
        time.sleep(int(headers.get("Retry-After", "15")))
    deadline = time.monotonic() + 1200
    previous = None
    while time.monotonic() < deadline:
        _, _, job = client.request("GET", job_url)
        status = job["status"]
        if status != previous:
            print(f"Notebook status: {status}", flush=True)
            previous = status
        if status == "Completed":
            verify_snapshot(client, state)
            return
        if status in ("Failed", "Cancelled", "Deduped"):
            raise RuntimeError(f"Notebook {status}: {json.dumps(job.get('failureReason'))}")
        time.sleep(20)
    raise TimeoutError(f"Notebook still running; monitor {job_url}. Do not start a duplicate.")


def verify_snapshot(client, state):
    headers = {"Authorization": "Bearer " + client.token("https://storage.azure.com")}
    with urlopen(Request(state["snapshot_url"], headers=headers), timeout=60) as response:
        snapshot = json.load(response)
    if snapshot["is_synthetic"] is not True or len(snapshot["rows"]) != 540:
        raise RuntimeError("Published snapshot is not the expected complete synthetic dataset.")
    if {r["subject_id"] for r in snapshot["rows"]} != {"demo-user"}:
        raise RuntimeError("Unexpected subject in demo data.")
    print(json.dumps({"verified": True, "rows": len(snapshot["rows"]),
                      "as_of_date": snapshot["as_of_date"], "run_id": snapshot["run_id"]}), flush=True)


def schedule(client, state):
    path = f"/workspaces/{state['workspace_id']}/items/{state['notebook_id']}/jobs/RunNotebook/schedules"
    _, _, current = client.request("GET", path)
    if current.get("value"):
        if len(current["value"]) != 1 or current["value"][0]["id"] != state.get("schedule_id"):
            raise RuntimeError("Unexpected existing schedule; refusing to create duplicates or change it.")
        print(json.dumps(current["value"][0]))
        return
    now = datetime.now(timezone.utc)
    body = {"enabled": True, "configuration": {
        "type": "Daily", "times": ["06:00"], "localTimeZoneId": "UTC",
        "startDateTime": now.strftime("%Y-%m-%dT%H:%M:%S"),
        "endDateTime": (now + timedelta(days=365)).strftime("%Y-%m-%dT%H:%M:%S"),
    }}
    created = client.write("POST", path, body)
    state["schedule_id"] = created["id"]
    save(state)
    _, _, verified = client.request("GET", path + "/" + created["id"])
    if verified["enabled"] is not True or verified["configuration"]["times"] != ["06:00"]:
        raise RuntimeError("Schedule readback did not match the requested daily 06:00 UTC run.")
    print(json.dumps(verified), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("setup", "run", "schedule", "verify"))
    parser.add_argument("--workspace", required=True, type=lambda s: str(uuid.UUID(s)))
    parser.add_argument("--tenant", required=True, type=lambda s: str(uuid.UUID(s)))
    args = parser.parse_args()
    state = json.loads(STATE.read_text()) if STATE.exists() else {
        "workspace_id": args.workspace, "tenant_id": args.tenant,
    }
    if state["workspace_id"] != args.workspace or state["tenant_id"] != args.tenant:
        raise RuntimeError("Deployment state belongs to another workspace or tenant.")
    client = Fabric(args.tenant)
    save(state)
    actions = {"setup": setup, "run": run, "schedule": schedule, "verify": verify_snapshot}
    actions[args.action](client, state)


if __name__ == "__main__":
    main()
