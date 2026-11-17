"""Deterministic demo records. This module never calls a product usage API."""

import hashlib
import math
from datetime import date, timedelta

SCHEMA_VERSION = 1
SUBJECT = "demo-user"
SURFACES = (
    ("github", "VS Code Copilot Chat", "ide", "development", "demo-reasoning", "github_demo"),
    ("github", "GitHub Copilot CLI", "terminal", "development", "demo-fast", "github_demo"),
    ("m365", "M365 Copilot Cowork", "m365_app", "productivity", "unavailable", "m365_credits_demo"),
    ("m365", "Teams Copilot", "m365_app", "productivity", "unavailable", "m365_credits_demo"),
    ("foundry", "Foundry agent", "agent", "development", "demo-reasoning", "apim_demo"),
    ("foundry", "Foundry playground", "playground", "experimentation", "demo-fast", "foundry_demo"),
)
KEY_FIELDS = ("usage_date", "subject_id", "ecosystem", "display_name", "model")
METRICS = ("input_tokens", "output_tokens", "requests", "prompts", "credits", "estimated_cost_usd")


def processing_start(end: date, last_success: date | None) -> date:
    initial_start = end - timedelta(days=89)
    if last_success is None:
        return initial_start
    return max(initial_start, min(last_success + timedelta(days=1), end - timedelta(days=6)))


def generate_records(start: date, end: date) -> list[dict]:
    if start > end or (end - start).days >= 366:
        raise ValueError("A demo batch must contain 1 to 366 days.")
    records = []
    for offset in range((end - start).days + 1):
        day = start + timedelta(days=offset)
        for ecosystem, name, surface, category, model, source in SURFACES:
            digest = hashlib.sha256(f"demo-v1|{day}|{name}".encode()).digest()
            active = int(digest[0] % (3 if day.weekday() >= 5 else 7) != 0)
            calls = active * (12 + digest[1] % 160)
            is_m365 = ecosystem == "m365"
            input_tokens = None if is_m365 else calls * (1000 + digest[2] * 30)
            output_tokens = None if is_m365 else calls * (80 + digest[3] * 3)
            # Fictional illustration rates, not provider prices or invoiced charges.
            rate = 2 if model == "demo-reasoning" else 0.3
            cost = None if is_m365 else round(
                (input_tokens * rate + output_tokens * rate * 3) / 1_000_000, 6
            )
            row = {
                "usage_date": day.isoformat(),
                "subject_id": SUBJECT,
                "ecosystem": ecosystem,
                "display_name": name,
                "surface": surface,
                "work_category": category,
                "model": model,
                "provider": "Demo provider" if not is_m365 else "Unavailable",
                "source_system": source,
                "attribution_type": "synthetic_subject",
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "requests": None if is_m365 else calls,
                "prompts": active * (3 + digest[4] % 25) if is_m365 else None,
                "credits": round(active * (1 + digest[5] / 10), 2) if is_m365 else None,
                "credit_unit": "m365_copilot_credit" if is_m365 else None,
                "estimated_cost_usd": cost,
                "cost_status": "unavailable" if is_m365 else "simulated_estimate",
                "currency": "USD",
                "active": active,
                "is_synthetic": True,
            }
            row["record_id"] = hashlib.sha256(
                "|".join(str(row[k]) for k in KEY_FIELDS).encode()
            ).hexdigest()
            records.append(row)
    validate_records(records)
    return records


def validate_records(records: list[dict]) -> None:
    seen = set()
    for row in records:
        if row["record_id"] in seen:
            raise ValueError("Duplicate daily usage key.")
        seen.add(row["record_id"])
        if row["subject_id"] != SUBJECT or row["is_synthetic"] is not True:
            raise ValueError("This pipeline only accepts the synthetic demo subject.")
        date.fromisoformat(row["usage_date"])
        if row["active"] not in (0, 1):
            raise ValueError("Activity must be 0 or 1.")
        for key in METRICS:
            value = row[key]
            if value is not None and (
                isinstance(value, bool) or not isinstance(value, (int, float))
                or not math.isfinite(value) or value < 0
            ):
                raise ValueError(f"Invalid {key}.")
        if row["ecosystem"] == "m365" and any(
            row[k] is not None for k in ("input_tokens", "output_tokens", "requests", "estimated_cost_usd")
        ):
            raise ValueError("Do not invent M365 tokens, model requests, or dollar charges.")
