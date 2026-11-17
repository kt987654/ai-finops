"""Configure APIM as the per-user metering gateway for Foundry agents.

Idempotent: every call is a PUT or an existence-checked create, so it can be re-run.
Requires `az login` to the your demo tenant and .deployment/azure.json from provisioning.
"""

import json
import shutil
import subprocess
import time
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = ROOT / ".deployment" / "azure.json"
POLICY_PATH = ROOT / "infra" / "apim" / "agent-api-policy.xml"
API_ID = "foundry-agents"
APIM_API_VERSION = "2024-06-01-preview"
FOUNDRY_USER_ROLE = "Foundry User"


def az(*args, parse=True):
    executable = shutil.which("az")
    if not executable:
        raise RuntimeError("Azure CLI is required.")
    result = subprocess.run([executable, *args, *(["-o", "json"] if parse else [])],
                            capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"az {' '.join(args[:3])} failed: {result.stderr.strip()}")
    return json.loads(result.stdout) if parse and result.stdout.strip() else result.stdout


_token = {}


def arm(method, url, body=None):
    if "value" not in _token or time.monotonic() - _token["at"] > 2400:
        _token.update(value=az("account", "get-access-token", "--resource", "https://management.azure.com")["accessToken"],
                      at=time.monotonic())
    request = Request(f"https://management.azure.com{url}", method=method,
                      data=None if body is None else json.dumps(body).encode(),
                      headers={"Authorization": f"Bearer {_token['value']}", "Content-Type": "application/json"})
    try:
        with urlopen(request, timeout=120) as response:
            text = response.read().decode("utf-8-sig")
            return json.loads(text) if text.strip() else {}
    except HTTPError as error:
        raise RuntimeError(f"ARM {method} {url.split('?')[0].rsplit('/', 2)[-2:]} returned {error.code}: "
                           f"{error.read().decode('utf-8-sig')[:800]}") from None


def main():
    cfg = json.loads(CONFIG_PATH.read_text())
    rg, apim, ai = cfg["resource_group"], cfg["apim"], cfg["ai_services"]
    service = az("apim", "show", "-g", rg, "-n", apim)
    if service["provisioningState"] != "Succeeded":
        raise RuntimeError(f"APIM is {service['provisioningState']}; wait for Succeeded.")
    apim_id, principal = service["id"], service["identity"]["principalId"]
    ai_id = az("cognitiveservices", "account", "show", "-g", rg, "-n", ai)["id"]
    law_id = az("monitor", "log-analytics", "workspace", "show", "-g", rg, "-n", cfg["log_analytics"])["id"]

    existing = az("role", "assignment", "list", "--assignee", principal, "--scope", ai_id,
                  "--role", FOUNDRY_USER_ROLE)
    if not existing:
        az("role", "assignment", "create", "--assignee-object-id", principal,
           "--assignee-principal-type", "ServicePrincipal", "--role", FOUNDRY_USER_ROLE, "--scope", ai_id)

    base = f"{apim_id}/apis/{API_ID}"
    q = f"?api-version={APIM_API_VERSION}"
    arm("PUT", base + q, {"properties": {
        "displayName": "Foundry agents (metered)",
        "description": "Calls Foundry prompt agents as the signed-in user; logs per-user token usage.",
        "path": "agents",
        "protocols": ["https"],
        "serviceUrl": f"https://{ai}.services.ai.azure.com/api/projects/finops-agents/openai/v1",
        "subscriptionRequired": False,
    }})
    arm("PUT", f"{base}/operations/invoke-agent{q}", {"properties": {
        "displayName": "Invoke agent (Responses API)",
        "method": "POST",
        "urlTemplate": "/{agent}/responses",
        "templateParameters": [{"name": "agent", "type": "string", "required": True}],
    }})
    policy = (POLICY_PATH.read_text(encoding="utf-8")
              .replace("{{TENANT_ID}}", cfg["tenant_id"])
              .replace("{{GATEWAY_APP_ID}}", cfg["gateway_app_id"]))
    arm("PUT", f"{base}/policies/policy{q}", {"properties": {"format": "rawxml", "value": policy}})

    categories = {c["name"] for c in az("monitor", "diagnostic-settings", "categories", "list",
                                          "--resource", apim_id)["value"]}
    wanted = [c for c in ("GatewayLogs", "GatewayLlmLogs") if c in categories]
    if "GatewayLogs" not in wanted:
        raise RuntimeError(f"GatewayLogs category unavailable: {sorted(categories)}")
    arm("PUT", f"{apim_id}/providers/Microsoft.Insights/diagnosticSettings/aifinops-to-law"
               "?api-version=2021-05-01-preview", {"properties": {
        "workspaceId": law_id,
        "logAnalyticsDestinationType": "Dedicated",
        "logs": [{"category": c, "enabled": True} for c in wanted],
    }})

    # Gateway diagnostics require the azureMonitor logger; create it if APIM has not.
    loggers = arm("GET", f"{apim_id}/loggers{q}")["value"]
    if not any(l["name"] == "azuremonitor" for l in loggers):
        arm("PUT", f"{apim_id}/loggers/azuremonitor{q}",
            {"properties": {"loggerType": "azureMonitor", "isBuffered": True}})
    body_bytes = 0
    diagnostic = {"properties": {
        "loggerId": "/loggers/azuremonitor",
        "alwaysLog": "allErrors",
        "verbosity": "information",
        "logClientIp": False,
        "sampling": {"samplingType": "fixed", "percentage": 100},
        "frontend": {"request": {"body": {"bytes": body_bytes}}, "response": {"body": {"bytes": body_bytes}}},
        "backend": {"request": {"body": {"bytes": body_bytes}}, "response": {"body": {"bytes": body_bytes}}},
    }}
    arm("PUT", f"{apim_id}/diagnostics/azuremonitor{q}", diagnostic)
    llm = json.loads(json.dumps(diagnostic))
    llm["properties"]["largeLanguageModel"] = {
        "logs": "disabled",
        "requests": {"messages": "none", "maxSizeInBytes": 0},
        "responses": {"messages": "none", "maxSizeInBytes": 0},
    }
    arm("PUT", f"{base}/diagnostics/azuremonitor?api-version=2024-10-01-preview", llm)
    llm_status = "disabled"

    gateway = f"{service['gatewayUrl']}/agents"
    cfg.update({"apim_gateway": gateway, "apim_principal": principal})
    CONFIG_PATH.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
    print(json.dumps({"gateway": gateway, "log_categories": wanted, "llm_message_logging": llm_status}, indent=2))


if __name__ == "__main__":
    main()
