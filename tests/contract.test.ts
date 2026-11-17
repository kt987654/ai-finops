import assert from "node:assert/strict";
import { test } from "node:test";
import { parseSnapshot } from "../rayfin/functions/src/contract.ts";

function fixture(): Record<string, unknown> {
  return {
    schema_version: 2, is_synthetic: false, timezone: "UTC", as_of_date: "2026-11-05", coverage_start: "2026-08-08",
    generated_at: "2026-11-05T18:55:00Z", run_id: "run-1",
    daily: [{
      usage_date: "2026-11-05", ecosystem: "foundry", source_system: "apim", tenant_id: "t", user_oid: "u1",
      user_upn: "u1@example.com", user_name: "User One", agent_name: "finops-poc-agent", model: "gpt-4.1-mini",
      requests: 2, failed_requests: 0, input_tokens: 93, output_tokens: 75, total_tokens: 168,
      estimated_cost_usd: 0.0001572, missing_rate: false,
    }],
    m365_users: [{ userPrincipalName: "u1@example.com", displayName: "User One", lastActivityDate: "2026-11-04",
      teamsCopilotLastActivityDate: "2026-11-04", wordCopilotLastActivityDate: "", reportRefreshDate: "2026-11-05" }],
    sources: [{ source: "foundry_apim", label: "Foundry agents via APIM", status: "connected", rows: 2,
      last_event_time: "2026-11-05T18:37:56Z", note: "ok" }],
    rates: [{ model: "gpt-4.1-mini", input_per_1m: 0.4, cached_input_per_1m: 0.1, output_per_1m: 1.6, rate_source: "list" }],
  };
}

test("accepts a valid real-data snapshot and derives M365 apps", () => {
  const snapshot = parseSnapshot(fixture());
  assert.equal(snapshot.daily[0].total_tokens, 168);
  assert.deepEqual(snapshot.m365_users[0].activeApps, ["teams"]);
});

test("rejects synthetic, duplicate, out-of-window, and malformed data", () => {
  const base = fixture();
  const daily = base.daily as Record<string, unknown>[];
  for (const invalid of [
    { ...base, is_synthetic: true },
    { ...base, schema_version: 1 },
    { ...base, daily: [daily[0], daily[0]] },
    { ...base, daily: [{ ...daily[0], usage_date: "2026-11-06" }] },
    { ...base, daily: [{ ...daily[0], input_tokens: -1 }] },
    { ...base, daily: [{ ...daily[0], user_oid: "" }] },
    { ...base, daily: [{ ...daily[0], estimated_cost_usd: Number.NaN }] },
    { ...base, sources: [{ source: "x", label: "x", status: "unknown", rows: 0, note: "" }] },
  ]) assert.throws(() => parseSnapshot(invalid));
});

test("accepts empty usage while sources are connected", () => {
  assert.equal(parseSnapshot({ ...fixture(), daily: [], m365_users: [] }).daily.length, 0);
});
