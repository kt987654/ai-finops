export interface DailyUsageRow {
  usage_date: string;
  ecosystem: string;
  source_system: string;
  tenant_id: string;
  user_oid: string;
  user_upn: string;
  user_name: string;
  agent_name: string;
  model: string;
  requests: number;
  failed_requests: number;
  input_tokens: number;
  output_tokens: number;
  total_tokens: number;
  estimated_cost_usd: number | null;
  missing_rate: boolean;
}

export interface M365CopilotUser {
  userPrincipalName: string;
  displayName: string | null;
  lastActivityDate: string | null;
  activeApps: string[];
}

export interface SourceStatus {
  source: string;
  label: string;
  status: "connected" | "no_data" | "not_connected";
  rows: number;
  last_event_time: string | null;
  note: string;
}

export interface ModelRate {
  model: string;
  input_per_1m: number;
  cached_input_per_1m: number;
  output_per_1m: number;
  rate_source: string;
}

export interface EnterpriseSnapshot {
  schema_version: 2;
  is_synthetic: false;
  timezone: "UTC";
  as_of_date: string;
  coverage_start: string;
  generated_at: string;
  run_id: string;
  daily: DailyUsageRow[];
  m365_users: M365CopilotUser[];
  sources: SourceStatus[];
  rates: ModelRate[];
}

type Bag = Record<string, unknown>;

function bag(value: unknown): value is Bag {
  return value !== null && typeof value === "object" && !Array.isArray(value);
}

function isoDate(value: unknown): value is string {
  return typeof value === "string" && /^\d{4}-\d{2}-\d{2}$/.test(value)
    && new Date(value + "T00:00:00Z").toISOString().slice(0, 10) === value;
}

function count(value: unknown): value is number {
  return typeof value === "number" && Number.isInteger(value) && value >= 0;
}

function text(value: unknown): value is string {
  return typeof value === "string";
}

function dailyRow(value: unknown): DailyUsageRow {
  if (!bag(value) || !isoDate(value.usage_date)
    || !["user_oid", "agent_name", "model", "ecosystem", "source_system", "tenant_id"].every((k) => text(value[k]))
    || value.user_oid === ""
    || !["requests", "failed_requests", "input_tokens", "output_tokens", "total_tokens"].every((k) => count(value[k]))
    || !(value.estimated_cost_usd === null || (typeof value.estimated_cost_usd === "number"
      && Number.isFinite(value.estimated_cost_usd) && value.estimated_cost_usd >= 0))
    || typeof value.missing_rate !== "boolean") {
    throw new Error("Fabric returned an invalid daily usage row.");
  }
  return {
    usage_date: value.usage_date, ecosystem: value.ecosystem as string, source_system: value.source_system as string,
    tenant_id: value.tenant_id as string, user_oid: value.user_oid as string,
    user_upn: text(value.user_upn) ? value.user_upn : "", user_name: text(value.user_name) ? value.user_name : "",
    agent_name: value.agent_name as string, model: value.model as string,
    requests: value.requests as number, failed_requests: value.failed_requests as number,
    input_tokens: value.input_tokens as number, output_tokens: value.output_tokens as number,
    total_tokens: value.total_tokens as number, estimated_cost_usd: value.estimated_cost_usd as number | null,
    missing_rate: value.missing_rate,
  };
}

function m365User(value: unknown): M365CopilotUser {
  if (!bag(value) || !text(value.userPrincipalName)) throw new Error("Fabric returned an invalid M365 user row.");
  const activeApps = Object.entries(value)
    .filter(([key, date]) => /CopilotLastActivityDate$/i.test(key) && isoDate(date))
    .map(([key]) => key.replace(/CopilotLastActivityDate$/i, ""));
  return {
    userPrincipalName: value.userPrincipalName,
    displayName: text(value.displayName) ? value.displayName : null,
    lastActivityDate: isoDate(value.lastActivityDate) ? value.lastActivityDate : null,
    activeApps,
  };
}

function source(value: unknown): SourceStatus {
  if (!bag(value) || !text(value.source) || !text(value.label) || !text(value.note) || !count(value.rows)
    || !["connected", "no_data", "not_connected"].includes(String(value.status))) {
    throw new Error("Fabric returned an invalid source status.");
  }
  return {
    source: value.source, label: value.label, status: value.status as SourceStatus["status"], rows: value.rows,
    last_event_time: text(value.last_event_time) ? value.last_event_time : null, note: value.note,
  };
}

export function parseSnapshot(value: unknown): EnterpriseSnapshot {
  if (!bag(value) || value.schema_version !== 2 || value.is_synthetic !== false || value.timezone !== "UTC"
    || !isoDate(value.as_of_date) || !isoDate(value.coverage_start) || !text(value.run_id)
    || !text(value.generated_at) || !Number.isFinite(Date.parse(value.generated_at))
    || !Array.isArray(value.daily) || !Array.isArray(value.m365_users) || !Array.isArray(value.sources)
    || !Array.isArray(value.rates) || value.daily.length > 50_000) {
    throw new Error("Fabric returned an invalid enterprise usage snapshot.");
  }
  const daily = value.daily.map(dailyRow);
  const asOf = value.as_of_date;
  const start = value.coverage_start;
  if (daily.some((row) => row.usage_date < start || row.usage_date > asOf)) {
    throw new Error("Fabric snapshot contains rows outside its coverage window.");
  }
  const keys = new Set(daily.map((r) => [r.usage_date, r.tenant_id, r.user_oid, r.agent_name, r.model].join("|")));
  if (keys.size !== daily.length) throw new Error("Fabric snapshot contains duplicate daily usage rows.");
  const rates = value.rates.map((rate): ModelRate => {
    if (!bag(rate) || !text(rate.model) || !text(rate.rate_source)
      || !["input_per_1m", "cached_input_per_1m", "output_per_1m"].every((k) => typeof rate[k] === "number")) {
      throw new Error("Fabric returned an invalid model rate.");
    }
    return rate as unknown as ModelRate;
  });
  return {
    schema_version: 2, is_synthetic: false, timezone: "UTC", as_of_date: asOf, coverage_start: start,
    generated_at: value.generated_at, run_id: value.run_id, daily,
    m365_users: value.m365_users.map(m365User), sources: value.sources.map(source), rates,
  };
}
