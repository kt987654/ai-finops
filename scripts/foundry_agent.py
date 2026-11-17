"""Create or update the Unified AI FinOps prompt agents in the Foundry project.

Usage: python scripts/foundry_agent.py [--smoke]
Each run creates a new version only for agents whose definition changed.
"""

import json
import sys
from pathlib import Path

import httpx2 as httpx
from azure.ai.projects import AIProjectClient
from azure.ai.projects.models import PromptAgentDefinition
from azure.identity import AzureCliCredential

ROOT = Path(__file__).resolve().parents[1]
CONFIG = json.loads((ROOT / ".deployment" / "azure.json").read_text())
ENDPOINT = f"https://{CONFIG['ai_services']}.services.ai.azure.com/api/projects/finops-agents"

AGENTS = {
    "finops-poc-agent": ("gpt-4.1-mini",
        "You are a concise assistant for a Unified AI FinOps proof of concept. "
        "Answer the user's question in two or three sentences.",
        "General FinOps assistant used to prove per-user metering through APIM."),
    "contract-review-agent": ("gpt-5.4-mini",
        "You review supplier and customer contract questions for a procurement team. Identify the relevant "
        "clause types, key risks, and one recommended next step. Keep answers under 120 words and note that "
        "this is not legal advice.",
        "Procurement contract review assistant."),
    "it-helpdesk-agent": ("gpt-5.4-nano",
        "You are a first-line IT helpdesk agent for Microsoft 365 and Windows users. Give short, numbered "
        "troubleshooting steps (at most five) and say when to escalate to IT support.",
        "Employee IT helpdesk assistant."),
}

captured = {}


def capture(request: httpx.Request) -> None:
    if request.url.path.endswith("/responses"):
        captured.update(method=request.method, path=request.url.path, body=json.loads(request.content or b"{}"))


def main() -> None:
    client = AIProjectClient(endpoint=ENDPOINT, credential=AzureCliCredential())
    existing = {}
    for agent in client.agents.list():
        definition = agent.versions.latest.definition
        existing[agent.name] = (definition.get("model"), definition.get("instructions"))
    for name, (model, instructions, description) in AGENTS.items():
        if existing.get(name) == (model, instructions):
            print(json.dumps({"agent": name, "model": model, "status": "unchanged"}))
            continue
        version = client.agents.create_version(
            agent_name=name, description=description,
            definition=PromptAgentDefinition(model=model, instructions=instructions))
        print(json.dumps({"agent": name, "model": model, "version": version.version}))
    if "--smoke" not in sys.argv:
        return
    openai_client = client.get_openai_client()
    openai_client._client.event_hooks["request"].append(capture)
    response = openai_client.responses.create(
        input="In one sentence, what is AI FinOps?",
        extra_body={"agent_reference": {"name": "finops-poc-agent", "type": "agent_reference"}})
    print(json.dumps({"request": captured, "usage": response.usage.model_dump() if response.usage else None,
                      "model": response.model, "output": response.output_text}, indent=2))


if __name__ == "__main__":
    main()
