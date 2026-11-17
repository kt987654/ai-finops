"""Run a read-only KQL query against the project Log Analytics workspace and print rows as JSON."""

import json
import shutil
import subprocess
import sys
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]


def query(kql: str) -> list[dict]:
    cfg = json.loads((ROOT / ".deployment" / "azure.json").read_text())
    token = json.loads(subprocess.run(
        [shutil.which("az"), "account", "get-access-token", "--resource", "https://api.loganalytics.io", "-o", "json"],
        capture_output=True, text=True, check=True).stdout)["accessToken"]
    request = Request(f"https://api.loganalytics.io/v1/workspaces/{cfg['log_analytics_customer_id']}/query",
                      method="POST", data=json.dumps({"query": kql}).encode(),
                      headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
    try:
        with urlopen(request, timeout=120) as response:
            table = json.load(response)["tables"][0]
    except HTTPError as error:
        raise SystemExit(f"Log Analytics returned {error.code}: {error.read().decode()[:600]}")
    names = [c["name"] for c in table["columns"]]
    return [dict(zip(names, row)) for row in table["rows"]]


if __name__ == "__main__":
    print(json.dumps(query(sys.argv[1]), indent=2, default=str))
