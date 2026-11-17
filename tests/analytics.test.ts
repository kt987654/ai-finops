import assert from "node:assert/strict";
import { test } from "node:test";
import { dailySeries, isStale, rangeStart, summarize } from "../src/analytics.ts";
import { assertViewerClaims } from "../rayfin/functions/src/authorization.ts";
import type { DailyUsageRow, EnterpriseSnapshot } from "../rayfin/functions/src/contract.ts";

function row(day: string, changes: Partial<DailyUsageRow> = {}): DailyUsageRow {
  return {
    usage_date: day, ecosystem: "foundry", source_system: "apim", tenant_id: "t", user_oid: "u1",
    user_upn: "u1@example.com", user_name: "User One", agent_name: "agent-a", model: "gpt-4.1-mini",
    requests: 2, failed_requests: 0, input_tokens: 100, output_tokens: 20, total_tokens: 120,
    estimated_cost_usd: 0.001, missing_rate: false, ...changes,
  };
}

test("inclusive 7 and 28 day windows and month boundaries", () => {
  assert.equal(rangeStart("2026-11-05", "28"), "2026-10-09");
  assert.equal(rangeStart("2026-11-05", "7"), "2026-10-30");
  assert.equal(rangeStart("2026-11-05", "month"), "2026-11-01");
  assert.equal(rangeStart("2024-03-01", "7"), "2024-02-24");
});

test("active users are distinct identities, not rows", () => {
  const totals = summarize([row("2026-11-01"), row("2026-11-02"), row("2026-11-02", { user_oid: "u2", agent_name: "agent-b" })]);
  assert.equal(totals.activeUsers, 2);
  assert.equal(totals.agents, 2);
  assert.equal(totals.requests, 6);
  assert.equal(totals.tokens, 360);
});

test("same object ID in another tenant is a different user", () => {
  assert.equal(summarize([row("2026-11-01"), row("2026-11-01", { tenant_id: "other" })]).activeUsers, 2);
});

test("missing rates make cost partial rather than zero", () => {
  const unpriced = row("2026-11-01", { model: "new-model", estimated_cost_usd: null, missing_rate: true });
  assert.equal(summarize([unpriced]).cost, null);
  assert.equal(summarize([unpriced, row("2026-11-01")]).costPartial, true);
});

test("failed-only rows do not count as active users", () => {
  const totals = summarize([row("2026-11-01", { requests: 0, failed_requests: 3, input_tokens: 0, output_tokens: 0, total_tokens: 0 })]);
  assert.equal(totals.activeUsers, 0);
  assert.equal(totals.failedRequests, 3);
});

test("daily series fills days without usage with zeros", () => {
  const series = dailySeries([row("2026-11-03")], "2026-11-01", "2026-11-04");
  assert.deepEqual(series.map((d) => d.tokens), [0, 0, 120, 0]);
});

test("snapshot is stale after three hours without a refresh", () => {
  const snapshot = { generated_at: "2026-11-05T10:00:00Z" } as EnterpriseSnapshot;
  assert.equal(isStale(snapshot, Date.parse("2026-11-05T12:59:00Z")), false);
  assert.equal(isStale(snapshot, Date.parse("2026-11-05T13:01:00Z")), true);
});

test("viewer allowlist rejects missing or unapproved identities", () => {
  assert.doesNotThrow(() => assertViewerClaims({ sub: "s", email: "LEADER@example.com" }, ["leader@example.com"]));
  assert.doesNotThrow(() => assertViewerClaims({
    sub: "/apps/x/users/s", xms_attr: { x: { rfn_email: "leader@example.com" } },
  }, ["leader@example.com"]));
  for (const claims of [null, {}, { email: "leader@example.com" }, { sub: "s", email: "other@example.com" }]) {
    assert.throws(() => assertViewerClaims(claims, ["leader@example.com"]));
  }
});
