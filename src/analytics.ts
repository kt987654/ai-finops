import type { DailyUsageRow, EnterpriseSnapshot } from "../rayfin/functions/src/contract.js";

export type Period = "7" | "28" | "month";

export interface Summary {
  requests: number;
  failedRequests: number;
  inputTokens: number;
  outputTokens: number;
  tokens: number;
  cost: number | null;
  costPartial: boolean;
  activeUsers: number;
  agents: number;
  models: number;
  lastActive: string | null;
}

export function rangeStart(asOf: string, period: Period): string {
  if (period === "month") return asOf.slice(0, 8) + "01";
  const date = new Date(asOf + "T00:00:00Z");
  date.setUTCDate(date.getUTCDate() - (Number(period) - 1));
  return date.toISOString().slice(0, 10);
}

export function selectRows(snapshot: EnterpriseSnapshot, period: Period, ecosystem: string): DailyUsageRow[] {
  const start = rangeStart(snapshot.as_of_date, period);
  return snapshot.daily.filter((row) => row.usage_date >= start && row.usage_date <= snapshot.as_of_date
    && (ecosystem === "all" || row.ecosystem === ecosystem));
}

export function summarize(rows: DailyUsageRow[]): Summary {
  const priced = rows.filter((row) => row.estimated_cost_usd !== null);
  const active = rows.filter((row) => row.requests > 0);
  return {
    requests: rows.reduce((total, row) => total + row.requests, 0),
    failedRequests: rows.reduce((total, row) => total + row.failed_requests, 0),
    inputTokens: rows.reduce((total, row) => total + row.input_tokens, 0),
    outputTokens: rows.reduce((total, row) => total + row.output_tokens, 0),
    tokens: rows.reduce((total, row) => total + row.total_tokens, 0),
    cost: priced.length ? priced.reduce((total, row) => total + (row.estimated_cost_usd ?? 0), 0) : null,
    costPartial: rows.some((row) => row.missing_rate || (row.requests > 0 && row.estimated_cost_usd === null)),
    activeUsers: new Set(active.map((row) => `${row.tenant_id}|${row.user_oid}`)).size,
    agents: new Set(active.map((row) => row.agent_name)).size,
    models: new Set(active.map((row) => row.model)).size,
    lastActive: active.reduce<string | null>((latest, row) => !latest || row.usage_date > latest ? row.usage_date : latest, null),
  };
}

export function groupBy(rows: DailyUsageRow[], key: (row: DailyUsageRow) => string) {
  const groups = new Map<string, DailyUsageRow[]>();
  for (const row of rows) {
    const id = key(row);
    groups.set(id, [...(groups.get(id) ?? []), row]);
  }
  return [...groups].map(([id, values]) => ({ id, rows: values, ...summarize(values) }));
}

export function userLabel(row: DailyUsageRow): string {
  return row.user_name || row.user_upn || row.user_oid;
}

export function dailySeries(rows: DailyUsageRow[], start: string, end: string) {
  const byDate = new Map(groupBy(rows, (row) => row.usage_date).map((group) => [group.id, group]));
  const series = [];
  for (let day = new Date(start + "T00:00:00Z"); day.toISOString().slice(0, 10) <= end; day.setUTCDate(day.getUTCDate() + 1)) {
    const date = day.toISOString().slice(0, 10);
    const group = byDate.get(date);
    series.push({ date, tokens: group?.tokens ?? 0, requests: group?.requests ?? 0, users: group?.activeUsers ?? 0 });
  }
  return series;
}

export function isStale(snapshot: EnterpriseSnapshot, now = Date.now(), maxHours = 3): boolean {
  return now - Date.parse(snapshot.generated_at) > maxHours * 3_600_000;
}
