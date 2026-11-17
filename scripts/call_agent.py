"""Call a Foundry agent through the metered APIM gateway as the signed-in Azure CLI user.

Usage: python scripts/call_agent.py "your prompt" [--agent finops-poc-agent]
The Entra user token is obtained from Azure CLI (pre-authorized on the gateway API) and is
never printed or stored.
"""

import argparse
import json
import shutil
import subprocess
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("prompt")
    parser.add_argument("--agent", default="finops-poc-agent")
    args = parser.parse_args()
    cfg = json.loads((ROOT / ".deployment" / "azure.json").read_text())
    token = json.loads(subprocess.run(
        [shutil.which("az"), "account", "get-access-token", "--scope",
         f"api://{cfg['gateway_app_id']}/user_impersonation", "-o", "json"],
        capture_output=True, text=True, check=True).stdout)["accessToken"]
    request = Request(f"{cfg['apim_gateway']}/{args.agent}/responses", method="POST",
                      data=json.dumps({"input": args.prompt}).encode(),
                      headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
    try:
        with urlopen(request, timeout=150) as response:
            body = json.load(response)
            print(json.dumps({"status": response.status,
                              "apim_request_id": response.headers.get("x-aifinops-request-id"),
                              "model": body.get("model"), "usage": body.get("usage"),
                              "output": "".join(part.get("text", "") for item in body.get("output", [])
                                                for part in item.get("content", []) or []
                                                if part.get("type") == "output_text")}, indent=2))
    except HTTPError as error:
        raise SystemExit(f"Gateway returned {error.code}: {error.read().decode()[:500]}")


if __name__ == "__main__":
    main()
