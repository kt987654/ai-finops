"""Generate realistic metered usage: send a batch of prompts to each agent through the APIM gateway.

Every call uses the signed-in Azure CLI user's Entra token, so all usage is attributed to that user.
Usage: python scripts/generate_usage.py [--rounds 1] [--workers 3]
"""

import argparse
import base64
import json
import random
import shutil
import subprocess
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]

PROMPTS = {
    "finops-poc-agent": [
        "How should we allocate shared AI platform costs across business units?",
        "What is the difference between showback and chargeback for AI usage?",
        "Which three KPIs best show return on investment for generative AI?",
        "How can we spot teams that are overspending on large language models?",
        "Explain token-based pricing to a finance executive.",
        "When does provisioned throughput make more sense than pay-as-you-go?",
        "How do we forecast next quarter's AI spend from current usage trends?",
        "What governance controls reduce unexpected AI costs?",
    ],
    "contract-review-agent": [
        "A supplier contract auto-renews for three years unless cancelled 90 days before expiry. What are the risks?",
        "Our SaaS agreement caps liability at one month of fees. Is that unusual?",
        "The vendor can change pricing with 30 days' notice. How should we respond?",
        "What should a data processing addendum include for an AI vendor?",
        "A contract has no termination-for-convenience clause. What are our options?",
        "Review an indemnity clause that excludes third-party IP claims.",
    ],
    "it-helpdesk-agent": [
        "Outlook keeps asking for my password.",
        "Teams video calls freeze when I share my screen.",
        "I can't sync my OneDrive folder to my laptop.",
        "How do I reset my multifactor authentication method?",
        "My Windows laptop is very slow after an update.",
        "Excel crashes when I open a large workbook.",
        "I lost access to a SharePoint site I used yesterday.",
        "The printer in my office shows as offline.",
    ],
}

# Persona-specific workloads so each user has a distinct, realistic usage pattern.
PERSONAS = {
    "default": PROMPTS,
    "ca-analyst": {
        "finops-poc-agent": [
            "Estimate the monthly AI cost of summarizing 2,000 California hospital quality reports.",
            "How should a regional analytics team budget for AI-assisted reporting?",
            "Compare the cost of nightly batch summaries versus on-demand AI questions.",
            "Which usage metrics show whether regional analysts benefit from AI tools?",
        ],
        "it-helpdesk-agent": [
            "Power BI refresh fails on my California hospital dashboard.",
            "Excel crashes when I open the regional readmissions workbook.",
            "How do I get access to the shared regional analytics Teams channel?",
        ],
    },
    "compliance": {
        "contract-review-agent": [
            "Review a data use agreement clause allowing a vendor to retain patient data for five years.",
            "What should an audit right clause include for a healthcare analytics vendor?",
            "A business associate agreement lacks breach notification timelines. What is the risk?",
            "Does a 72-hour incident notification clause meet typical healthcare expectations?",
            "Review a subcontracting clause that lets a vendor use offshore processors.",
            "What termination rights should we require if a vendor fails a compliance audit?",
        ],
        "finops-poc-agent": [
            "How do we document AI usage for a compliance audit trail?",
            "Which AI cost controls should internal audit verify?",
        ],
    },
    "formulary": {
        "finops-poc-agent": [
            "How can AI help prioritize national formulary reviews by cost impact?",
            "Estimate token usage for summarizing 500 drug monographs each month.",
            "What is a sensible chargeback model for a national pharmacy analytics team?",
        ],
        "contract-review-agent": [
            "Review a rebate clause tied to formulary tier placement.",
            "What risks exist in a pharmacy benefit manager contract with spread pricing?",
            "Explain a most-favored-nation pricing clause in a drug supply agreement.",
        ],
        "it-helpdesk-agent": [
            "I can't open the national formulary SharePoint list.",
            "My multifactor prompt never arrives on my phone.",
        ],
    },
}


def token(cfg: dict) -> str:
    result = subprocess.run([shutil.which("az"), "account", "get-access-token", "--scope",
                             f"api://{cfg['gateway_app_id']}/user_impersonation", "-o", "json"],
                            capture_output=True, text=True, check=True)
    return json.loads(result.stdout)["accessToken"]


def call(gateway: str, bearer: str, agent: str, prompt: str) -> dict:
    request = Request(f"{gateway}/{agent}/responses", method="POST", data=json.dumps({"input": prompt}).encode(),
                      headers={"Authorization": f"Bearer {bearer}", "Content-Type": "application/json"})
    for attempt in range(3):
        try:
            with urlopen(request, timeout=150) as response:
                body = json.load(response)
                usage = body.get("usage") or {}
                return {"agent": agent, "status": response.status, "model": body.get("model"),
                        "tokens": usage.get("total_tokens", 0)}
        except HTTPError as error:
            if error.code in (429, 500, 502, 503, 504) and attempt < 2:
                time.sleep(int(error.headers.get("Retry-After", "5")) + attempt * 5)
                continue
            return {"agent": agent, "status": error.code, "model": None, "tokens": 0}
        except URLError:
            if attempt < 2:
                time.sleep(5)
                continue
            return {"agent": agent, "status": 0, "model": None, "tokens": 0}
    return {"agent": agent, "status": 0, "model": None, "tokens": 0}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--rounds", type=int, default=1, help="Times to send each prompt (1-5).")
    parser.add_argument("--workers", type=int, default=3, help="Concurrent requests (1-5).")
    parser.add_argument("--persona", choices=sorted(PERSONAS), default="default", help="Prompt set to send.")
    parser.add_argument("--expect-user", help="Refuse to run unless the Azure CLI token belongs to this UPN.")
    args = parser.parse_args()
    if not 1 <= args.rounds <= 5 or not 1 <= args.workers <= 5:
        raise SystemExit("--rounds and --workers must be between 1 and 5.")
    cfg = json.loads((ROOT / ".deployment" / "azure.json").read_text())
    bearer = token(cfg)
    payload = bearer.split(".")[1]
    claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    signed_in = claims.get("preferred_username") or claims.get("upn") or ""
    if args.expect_user and signed_in.lower() != args.expect_user.lower():
        raise SystemExit(f"Token belongs to {signed_in or 'an unknown user'}, not {args.expect_user}; nothing sent.")
    print(f"Calling as {signed_in} with persona '{args.persona}'.", flush=True)
    jobs = [(agent, prompt) for _ in range(args.rounds)
            for agent, prompts in PERSONAS[args.persona].items() for prompt in prompts]
    random.shuffle(jobs)
    print(f"Sending {len(jobs)} requests through {cfg['apim_gateway']} ...", flush=True)
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        results = list(pool.map(lambda job: call(cfg["apim_gateway"], bearer, *job), jobs))
    summary = defaultdict(lambda: {"ok": 0, "failed": 0, "tokens": 0, "model": None})
    for result in results:
        entry = summary[result["agent"]]
        entry["ok" if result["status"] == 200 else "failed"] += 1
        entry["tokens"] += result["tokens"]
        entry["model"] = entry["model"] or result["model"]
    print(json.dumps(summary, indent=2))
    failures = [r for r in results if r["status"] != 200]
    if failures:
        print(f"{len(failures)} failed: {sorted({r['status'] for r in failures})}")


if __name__ == "__main__":
    main()
