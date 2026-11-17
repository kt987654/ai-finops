/**
 * Function schema types for RayfinClient.
 *
 * AUTO-GENERATED — do not edit manually.
 * Re-generated automatically when function source files change.
 *
 * If this file is not updating automatically, run:
 *   rayfin dev functions apply
 *
 * The schema is a closed object type: only the function names listed
 * below are accepted by RayfinClient.functions.<name>.invoke(...).
 * Adding, renaming, or changing the signature of a udf.func() call
 * regenerates this file and surfaces type errors at every consumer.
 *
 * IMPORTANT: This file must NOT import any Node.js packages — it is
 * resolved by the frontend app's TypeScript compiler.
 */

export type AppFunctionsSchema = {
  getEnterpriseUsage: {
    input: Record<string, never>;
    output: { schema_version: 2; is_synthetic: false; timezone: 'UTC'; as_of_date: string; coverage_start: string; generated_at: string; run_id: string; daily: { usage_date: string; ecosystem: string; source_system: string; tenant_id: string; user_oid: string; user_upn: string; user_name: string; agent_name: string; model: string; requests: number; failed_requests: number; input_tokens: number; output_tokens: number; total_tokens: number; estimated_cost_usd: null | number; missing_rate: boolean }[]; m365_users: { userPrincipalName: string; displayName: null | string; lastActivityDate: null | string; activeApps: string[] }[]; sources: { source: string; label: string; status: 'connected' | 'no_data' | 'not_connected'; rows: number; last_event_time: null | string; note: string }[]; rates: { model: string; input_per_1m: number; cached_input_per_1m: number; output_per_1m: number; rate_source: string }[] };
  };
};
