> Sanitized source: live credentials, bindings, outputs and generated archives were removed. Configure your own environment before running. See [PUBLICATION.md](PUBLICATION.md) for setup, dependency limitations, and verification scope. Original verification notes are historical, not a verification of this copy.

# Unified AI FinOps

Customer-facing dashboard showing **who is consuming AI, which agents and models they use, and what it costs over time**,
attributed to Microsoft Entra identities. Developed as a Unified AI FinOps hackathon demonstration.

## Architecture

```text
User (Entra token, scope user_impersonation)
  -> Azure API Management  YOUR_APIM        validates the user token, calls the agent with
     |                                                 its managed identity, logs one ai_usage trace per call
     -> Microsoft Foundry  YOUR_AI_SERVICES/finops-agents  prompt agents (see "Agents and models")
  -> Log Analytics         YOUR_LOG_ANALYTICS          ApiManagementGatewayLogs.TraceRecords (+ GatewayLlmLog)
  -> Fabric pipeline       pl_aifinops_ingest (hourly)
       Copy  Log Analytics (ADX proxy, workspace identity) -> bronze.apim_usage_events
       Copy  Microsoft Graph Copilot usage report (service principal) -> Files/raw/m365/...
       Notebook nb_aifinops_transform -> silver / gold tables + Files/published/enterprise_usage.json
  -> Rayfin app (Fabric-hosted, protected) -> authenticated function getEnterpriseUsage -> dashboard
```

Everything runs in resource group `YOUR_RESOURCE_GROUP` (North Central US) and Fabric workspace **AI Usage**.
The design follows the [AI gateway billing sample](https://github.com/tjsullivan1/tjs-scripts/tree/master/solutions/ai-gateway-billing-sample)
with one deliberate change: the sample attributes usage to service principals and APIM subscriptions; this project
attributes usage to **human users** by requiring delegated Entra tokens and logging the caller's `oid`, UPN, and name.

## Agents and models

| Agent | Model | Purpose |
|---|---|---|
| `finops-poc-agent` | gpt-4.1-mini | General FinOps questions |
| `contract-review-agent` | gpt-5.4-mini | Procurement contract review (not legal advice) |
| `it-helpdesk-agent` | gpt-5.4-nano | First-line Microsoft 365 and Windows support |

Agent definitions live in `scripts/foundry_agent.py`; rerun it after editing (unchanged agents are skipped).
`config.model_rates` is seeded with these models' rates; the gpt-5.4 rates come from the Azure Retail Prices API
(Foundry Models, Global Standard, northcentralus). New default rates are added without overwriting edited ones.

## Data sources

| Source | Status | What it provides |
|---|---|---|
| Foundry agents via APIM | Implemented; configure your own environment | Per user, per day, per agent and model: requests, input/output/cached tokens, estimated cost |
| Microsoft 365 Copilot (Graph `getMicrosoft365CopilotUsageUserDetail`, beta JSON, D28) | Implemented; configure your own environment | Licensed users' last activity per Copilot app. Available data depends on your tenant and licenses. Graph does not report tokens. |
| GitHub Copilot usage API | Not connected | Requires appropriate organization or enterprise permissions. |
| Fabric data agents | Not connected | Not available through the Graph usage reports. |

## Metric rules

- Users are keyed by Entra tenant ID + object ID; display names come from the token.
- Requests and tokens count successful calls only; failed calls are counted separately.
- Events are deduplicated by APIM request ID, so overlapping pipeline windows and retries never double count.
- Estimated cost = (uncached input x input rate + cached input x cached rate + output x output rate) / 1M using
  `config.model_rates` (seeded with gpt-4.1-mini list rates: $0.40 / $0.10 / $1.60 per 1M; verify and edit for your
  agreement). Models without a rate show as "Not priced", never $0. Licenses and platform charges are excluded.
- All dates are UTC. Windows (7 / 28 days, month to date) are inclusive and anchored to the snapshot date.

## Privacy and access

- This publication copy logs usage metadata, not prompt or completion content. APIM request/response body
  capture, LLM message logging and client-IP logging are disabled. The transform drops legacy content/IP
  columns before writing curated events. Existing remote logs are not deleted by these source changes.
- The dashboard snapshot contains aggregates only, never prompts or responses.
- The Rayfin function admits only emails in `dashboard_viewers` (`.deployment/azure.json`, default: an empty list),
  after the Fabric host has authenticated the caller. Anonymous calls are rejected.
- Microsoft 365 report name concealment is left unchanged. The collector requests only `Reports.Read.All`,
  not permission to change tenant-wide report settings. Honor your organization's existing privacy settings.
- Log Analytics access uses the Fabric workspace identity. Configure your own Graph collector credential
  through the Fabric connection and follow your administrator's credential-rotation procedure.

## Operations

Prerequisites: configure this copy using [PUBLICATION.md](PUBLICATION.md) first. Then use Azure CLI signed in to
your own demo tenant, Python 3 with `nbformat`, and npm. The Foundry SDK belongs in your local `.venv`.

```powershell
# Call an agent through the gateway as yourself (Azure CLI is pre-authorized on the gateway API)
python .\scripts\call_agent.py "How can teams forecast monthly LLM spend?" --agent contract-review-agent

# Generate a realistic batch of metered usage across all agents (22 prompts per round)
python .\scripts\generate_usage.py --rounds 2 --workers 3

# Generate usage as another user: sign in with an isolated Azure CLI profile, then use a persona
# (default, ca-analyst, compliance, formulary). --expect-user refuses to send if the token is for anyone else.
$env:AZURE_CONFIG_DIR = "$PWD\.deployment\az-otheruser"
az login --use-device-code --tenant <tenant-id> --allow-no-subscriptions
python .\scripts\generate_usage.py --persona formulary --expect-user user@contoso.com
az logout; Remove-Item Env:\AZURE_CONFIG_DIR; Remove-Item -Recurse "$PWD\.deployment\az-otheruser"

# Inspect raw usage in Log Analytics (read-only KQL)
python .\scripts\la_query.py "ApiManagementGatewayLogs | take 5"

# Re-apply APIM API, policy, and diagnostics (idempotent)
python .\scripts\configure_apim.py

# Fabric: (re)deploy items, run once, enable the hourly schedule, verify the published snapshot
python .\scripts\fabric_real.py setup
python .\scripts\fabric_real.py run --lookback 3d
python .\scripts\fabric_real.py schedule
python .\scripts\fabric_real.py verify

# Rayfin app
.\scripts\rayfin.ps1 functions init     # regenerates typed client
npm run build
.\scripts\rayfin.ps1 up --tenant <tenant-id> --workspace-id <workspace-id> --yes
```

Measured freshness: APIM records reach Log Analytics about 65 s after a call (p95 89 s, max ~3.5 min), and a pipeline
run takes about 2 minutes. With the hourly schedule the dashboard lags by up to ~65 minutes; run
`python .\scripts\fabric_real.py run` for an immediate refresh. Shorter schedules (for example 15 minutes, ~20-minute
freshness) are possible but use more Fabric capacity. The dashboard marks the snapshot stale after 3 hours without a
successful run. The Log Analytics workspace keeps 30 days; the pipeline copies a 3-day window each hour, and Fabric
retains history beyond that.

To let other users call agents: grant them access (they sign in with Entra); any client app must request the
`api://<gateway-app-id>/user_impersonation` scope. To add dashboard viewers, add their emails to `dashboard_viewers`,
rerun `fabric_real.py setup`, redeploy, and share the Fabric app with them.

## Tests

```powershell
.\node_modules\node\bin\node.exe --test .\tests\analytics.test.ts .\tests\contract.test.ts
```

## Earlier synthetic demo

`fabric/synthetic.py`, `fabric/daily_job.py`, `scripts/fabric_demo.py`, and `tests/test_synthetic.py` are the
earlier synthetic-data prototype. Its Fabric schedule is disabled and the app no longer reads its output.
`scripts/fabric_demo.py` still provides the shared Fabric REST client used by `fabric_real.py`.

## Cost notes

APIM Developer tier (no SLA), Foundry pay-per-token, Log Analytics ingestion (prompt logging increases volume), and the
existing F16 Fabric capacity running an hourly pipeline. Delete `YOUR_RESOURCE_GROUP` and the Fabric items when the
hackathon ends.
