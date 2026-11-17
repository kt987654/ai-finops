import { RayfinClient, resolveRayfinConfig } from "@microsoft/rayfin-client";
import { ensureSignedInWithFabric, initEmbeddedAuth } from "@microsoft/rayfin-auth-provider-fabric";
import type { AppFunctionsSchema } from "../rayfin/functions/src/types.js";
import { parseSnapshot, type EnterpriseSnapshot } from "../rayfin/functions/src/contract.js";
import { dailySeries, groupBy, isStale, rangeStart, selectRows, summarize, userLabel, type Period } from "./analytics.js";
import "./style.css";

const root = document.querySelector<HTMLDivElement>("#app");
if (!root) throw new Error("Missing application root.");
root.innerHTML = `
  <header class="topbar"><a class="brand" href="./"><span class="brand-icon">AI</span> Unified AI FinOps</a>
    <span class="workspace">ENTERPRISE AI CONSUMPTION</span><button id="signout" class="quiet" hidden>Sign out</button></header>
  <div class="live-banner"><strong>LIVE DATA</strong> Foundry agents metered per user by APIM &middot; Microsoft 365 via Graph &middot; refreshed hourly in Fabric. Costs are estimates, not invoices.</div>
  <main>
    <section class="page-heading"><div><p class="eyebrow">WHO IS USING AI, AND ON WHAT</p><h1>AI consumption</h1>
      <p class="subtitle">Per-user usage across agents and models, attributed to Microsoft Entra identities.</p></div><span class="badge">Hackathon preview</span></section>
    <div id="error" class="error" role="alert" hidden></div>
    <section id="signin-panel" class="signin-panel">
      <div class="signin-mark">AI</div><h2>Enterprise AI usage, from your Fabric data</h2>
      <p>Sign in with your organization account to view metered AI consumption.<br>
      Data is read live from the latest validated Fabric snapshot.</p>
      <button id="signin" class="primary">Sign in with Fabric</button>
      <p class="small">Access is limited to approved viewers.</p>
    </section>
    <section id="dashboard" hidden>
      <div class="toolbar"><div class="periods" aria-label="Reporting period">
        <button data-period="7">Last 7 days</button><button data-period="28" aria-pressed="true" class="selected">Last 28 days</button>
        <button data-period="month">Month to date</button></div>
        <div class="toolbar-right"><label for="ecosystem">Source</label><select id="ecosystem">
          <option value="all">All sources</option><option value="foundry">Foundry agents</option><option value="m365">Microsoft 365</option><option value="github">GitHub</option>
        </select><button id="refresh" class="secondary">Refresh data</button></div></div>
      <div class="freshness"><span id="freshness" role="status" aria-live="polite"></span><span id="anchor"></span></div>
      <div id="metrics" class="metrics"></div>
      <section class="panel"><div class="section-title"><div><p class="eyebrow">DAILY PATTERN</p><h2>Tokens over time</h2></div>
        <span class="small">Successful requests &middot; UTC days</span></div><div id="trend"></div></section>
      <section class="panel"><div class="section-title"><div><p class="eyebrow">WHO IS CONSUMING AI</p><h2>Users</h2></div>
        <span class="small">Attributed by Entra object ID</span></div><div class="table-scroll"><table>
        <thead><tr><th>User</th><th>Requests</th><th>Input tokens</th><th>Output tokens</th><th>Estimated cost</th><th>Agents</th><th>Models</th><th>Last active</th></tr></thead>
        <tbody id="users"></tbody><tfoot id="user-totals"></tfoot></table></div></section>
      <div class="bottom-grid">
        <section class="panel"><div class="section-title"><div><p class="eyebrow">WHAT THEY USE</p><h2>Agents</h2></div></div>
          <div class="table-scroll"><table><thead><tr><th>Agent</th><th>Users</th><th>Requests</th><th>Tokens</th><th>Estimated cost</th></tr></thead>
          <tbody id="agents"></tbody></table></div></section>
        <section class="panel"><div class="section-title"><div><p class="eyebrow">WHAT POWERS IT</p><h2>Models</h2></div></div>
          <div id="models"></div></section>
      </div>
      <div class="bottom-grid">
        <section class="panel"><div class="section-title"><div><p class="eyebrow">MICROSOFT 365</p><h2>Copilot users</h2></div>
          <span class="small">Graph usage report &middot; licensed users</span></div><div id="m365"></div></section>
        <section class="panel"><div class="section-title"><div><p class="eyebrow">DATA YOU CAN TRUST</p><h2>Data sources</h2></div></div>
          <ul id="sources" class="sources"></ul></section>
      </div>
      <details class="definitions"><summary>How these numbers are calculated</summary>
        <p>Foundry agent calls pass through Azure API Management, which validates each caller's Microsoft Entra token and records
        the user, agent, model and token counts returned by the model to Log Analytics. An hourly Fabric pipeline copies those records,
        removes duplicates by gateway request ID, and aggregates them by user, day, agent and model. Estimated cost applies the configured
        per-model list rates to input, cached-input and output tokens; it excludes licenses, discounts and platform charges.
        Microsoft 365 Copilot data comes from the Graph usage report, which covers licensed users and reports activity dates and prompts, not tokens.
        This dashboard shows aggregates only; prompt and response content logging is disabled in this publication copy.</p><p id="run-info"></p></details>
    </section>
    <footer>Built on Azure API Management, Microsoft Foundry, Microsoft Fabric &amp; Rayfin <span>UTC &middot; Live data</span></footer>
  </main>`;

function element<T extends HTMLElement>(id: string): T {
  const found = document.getElementById(id);
  if (!found) throw new Error(`Missing element ${id}.`);
  return found as T;
}
const escape = (text: string): string => text.replace(/[&<>"']/g, (c) =>
  ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c] ?? c));
const integer = (value: number): string => new Intl.NumberFormat("en-US", { maximumFractionDigits: 0 }).format(value);
const money = (value: number | null): string => value === null ? "Not priced"
  : new Intl.NumberFormat("en-US", { style: "currency", currency: "USD", minimumFractionDigits: value < 1 ? 4 : 2, maximumFractionDigits: value < 1 ? 4 : 2 }).format(value);
const empty = (columns: number, message: string): string => `<tr><td colspan="${columns}" class="empty">${escape(message)}</td></tr>`;

let snapshot: EnterpriseSnapshot | null = null;
let period: Period = "28";
let ecosystem = "all";
let requestVersion = 0;

function showError(message: string): void {
  element("error").textContent = message;
  element("error").hidden = false;
}

function render(): void {
  if (!snapshot) return;
  const rows = selectRows(snapshot, period, ecosystem);
  const totals = summarize(rows);
  const start = rangeStart(snapshot.as_of_date, period);
  element("anchor").textContent = `${start} to ${snapshot.as_of_date} (UTC)`;
  const stale = isStale(snapshot);
  const fresh = element("freshness");
  fresh.className = stale ? "stale" : "ready";
  fresh.textContent = `${stale ? "Stale - the hourly Fabric pipeline has not published recently" : "Validated Fabric snapshot"} | Updated ${new Date(snapshot.generated_at).toLocaleString()}`;
  const cards: [string, string, string][] = [
    ["Estimated cost", money(totals.cost), totals.costPartial ? "Some models have no configured rate" : "At configured list rates; not an invoice"],
    ["Total tokens", integer(totals.tokens), `${integer(totals.inputTokens)} in / ${integer(totals.outputTokens)} out`],
    ["Requests", integer(totals.requests), totals.failedRequests ? `${integer(totals.failedRequests)} failed requests excluded` : "Successful agent calls"],
    ["Active users", integer(totals.activeUsers), "Distinct Entra identities"],
    ["Agents in use", integer(totals.agents), `${integer(totals.models)} model${totals.models === 1 ? "" : "s"}`],
  ];
  element("metrics").innerHTML = cards.map(([label, value, note]) =>
    `<article class="metric"><p>${label}</p><strong>${value}</strong><small>${escape(note)}</small></article>`).join("");

  const series = dailySeries(rows, start, snapshot.as_of_date);
  const max = Math.max(1, ...series.map((day) => day.tokens));
  element("trend").innerHTML = `<div class="chart" role="img" aria-label="Daily token totals">${series.map((day) =>
    `<div class="chart-column" title="${day.date}: ${integer(day.tokens)} tokens, ${integer(day.requests)} requests, ${day.users} users"><span class="bar" style="height:${day.tokens ? Math.max(2, day.tokens / max * 100) : 0}%"></span><span class="chart-label">${day.date.slice(8)}</span></div>`).join("")}</div>
    <details class="chart-data"><summary>Show daily values</summary><table><thead><tr><th>UTC date</th><th>Tokens</th><th>Requests</th><th>Users</th></tr></thead>
    <tbody>${series.map((day) => `<tr><td>${day.date}</td><td>${integer(day.tokens)}</td><td>${integer(day.requests)}</td><td>${day.users}</td></tr>`).join("")}</tbody></table></details>`;

  const users = groupBy(rows, (row) => `${row.tenant_id}|${row.user_oid}`).sort((a, b) => b.tokens - a.tokens);
  element("users").innerHTML = users.length ? users.map((user) => {
    const latest = user.rows.reduce((a, b) => a.usage_date >= b.usage_date ? a : b);
    return `<tr><td><strong>${escape(userLabel(latest))}</strong><small>${escape(latest.user_upn)}</small></td>
      <td>${integer(user.requests)}</td><td>${integer(user.inputTokens)}</td><td>${integer(user.outputTokens)}</td><td>${money(user.cost)}</td>
      <td>${escape([...new Set(user.rows.map((r) => r.agent_name))].join(", "))}</td><td>${escape([...new Set(user.rows.map((r) => r.model))].join(", "))}</td>
      <td>${user.lastActive ?? ""}</td></tr>`;
  }).join("") : empty(8, "No metered usage in this period.");
  element("user-totals").innerHTML = `<tr><th>All users (${totals.activeUsers})</th><td>${integer(totals.requests)}</td><td>${integer(totals.inputTokens)}</td>
    <td>${integer(totals.outputTokens)}</td><td>${money(totals.cost)}</td><td>${totals.agents}</td><td>${totals.models}</td><td>${totals.lastActive ?? ""}</td></tr>`;

  const agents = groupBy(rows, (row) => row.agent_name).sort((a, b) => b.tokens - a.tokens);
  element("agents").innerHTML = agents.length ? agents.map((agent) => `<tr><td><strong>${escape(agent.id)}</strong><small>${escape(agent.rows[0].ecosystem)}</small></td>
    <td>${agent.activeUsers}</td><td>${integer(agent.requests)}</td><td>${integer(agent.tokens)}</td><td>${money(agent.cost)}</td></tr>`).join("")
    : empty(5, "No agent usage in this period.");

  const models = groupBy(rows, (row) => row.model).sort((a, b) => b.tokens - a.tokens);
  const maxTokens = Math.max(1, ...models.map((model) => model.tokens));
  element("models").innerHTML = models.length ? models.map((model) => `<div class="model"><div><strong>${escape(model.id)}</strong><span>${integer(model.tokens)} tokens</span></div>
    <div class="track"><span style="width:${model.tokens / maxTokens * 100}%"></span></div>
    <small>${integer(model.requests)} requests &middot; ${model.activeUsers} users &middot; ${money(model.cost)} estimated</small></div>`).join("")
    : '<p class="small">No model usage in this period.</p>';

  const m365Source = snapshot.sources.find((s) => s.source === "m365_graph");
  element("m365").innerHTML = snapshot.m365_users.length
    ? `<div class="table-scroll"><table><thead><tr><th>User</th><th>Last activity</th><th>Copilot apps used</th></tr></thead><tbody>${
      snapshot.m365_users.map((user) => `<tr><td><strong>${escape(user.displayName ?? user.userPrincipalName)}</strong><small>${escape(user.userPrincipalName)}</small></td>
        <td>${user.lastActivityDate ?? "No activity"}</td><td>${escape(user.activeApps.join(", ") || "None")}</td></tr>`).join("")}</tbody></table></div>`
    : `<p class="empty-state"><strong>${m365Source?.status === "connected" ? "Connected - no licensed Copilot users yet" : "No Graph data available"}</strong>
      ${escape(m365Source?.note ?? "")} Usage appears here after users are assigned Microsoft 365 Copilot licenses and use it.</p>`;

  element("sources").innerHTML = snapshot.sources.map((s) => `<li><span><strong>${escape(s.label)}</strong><small>${escape(s.note)}</small></span>
    <span class="source-state ${s.status}">${s.status === "connected" ? "Connected" : s.status === "no_data" ? "No data" : "Not connected"}</span></li>`).join("");
  const rate = snapshot.rates.map((r) => `${r.model}: $${r.input_per_1m}/1M input, $${r.cached_input_per_1m}/1M cached, $${r.output_per_1m}/1M output (${r.rate_source})`).join("; ");
  element("run-info").textContent = `Rates: ${rate}. Pipeline run ${snapshot.run_id}; coverage from ${snapshot.coverage_start}.`;
}

async function initialize(): Promise<void> {
  try {
    const { baseUrl, publishableKey, runtimeConfig } = await resolveRayfinConfig({
      apiUrl: import.meta.env.VITE_RAYFIN_API_URL,
      publishableKey: import.meta.env.VITE_RAYFIN_PUBLISHABLE_KEY,
      workspaceId: import.meta.env.VITE_FABRIC_WORKSPACE_ID,
      itemId: import.meta.env.VITE_FABRIC_ITEM_ID,
    });
    const workspaceId = runtimeConfig.workspaceId;
    const projectId = runtimeConfig.itemId;
    if (!baseUrl || !publishableKey || !workspaceId || !projectId) {
      showError("Deployment configuration is missing. Deploy the Rayfin backend and generate the Vite environment before building.");
      element<HTMLButtonElement>("signin").disabled = true;
      return;
    }
    const client = new RayfinClient<Record<string, never>, AppFunctionsSchema>({ baseUrl, publishableKey });
    const authOptions = { workspaceId, projectId, fabricPortalUrl: "https://app.fabric.microsoft.com", returnOrigin: location.origin };
    const load = async (): Promise<void> => {
      const version = ++requestVersion;
      const refresh = element<HTMLButtonElement>("refresh");
      refresh.disabled = true;
      refresh.textContent = "Loading...";
      element("error").hidden = true;
      try {
        const result = await client.functions.getEnterpriseUsage.invoke();
        if (version !== requestVersion) return;
        snapshot = parseSnapshot(result);
        element("signin-panel").hidden = true;
        element("dashboard").hidden = false;
        element("signout").hidden = false;
        render();
      } catch (error) {
        if (version === requestVersion) showError(
          `${error instanceof Error ? error.message : "Unable to load Fabric usage."}${snapshot ? " Previously loaded data remains visible; it has not been refreshed." : ""}`,
        );
      } finally {
        refresh.disabled = false;
        refresh.textContent = "Refresh data";
      }
    };
    element("signin").addEventListener("click", async () => {
      try {
        await ensureSignedInWithFabric(client.auth, authOptions);
        await load();
      } catch (error) {
        showError(error instanceof Error ? error.message : "Sign-in failed.");
      }
    });
    element("refresh").addEventListener("click", () => { void load(); });
    element("signout").addEventListener("click", async () => {
      ++requestVersion;
      snapshot = null;
      element("dashboard").hidden = true;
      element("signin-panel").hidden = false;
      element("signout").hidden = true;
      try { await client.auth.signOut(); }
      catch (error) { showError(error instanceof Error ? error.message : "Sign-out failed."); }
    });
    await initEmbeddedAuth(client.auth, authOptions);
    if (client.auth.getSession().isAuthenticated) await load();
  } catch (error) {
    showError(error instanceof Error ? error.message : "Could not load application configuration.");
    element<HTMLButtonElement>("signin").disabled = true;
  }
}
void initialize();

document.querySelectorAll<HTMLButtonElement>("[data-period]").forEach((button) => {
  button.addEventListener("click", () => {
    const selected = button.dataset.period;
    if (selected !== "7" && selected !== "28" && selected !== "month") throw new Error("Invalid period.");
    period = selected;
    document.querySelectorAll<HTMLButtonElement>("[data-period]").forEach((item) => {
      item.classList.toggle("selected", item === button);
      item.setAttribute("aria-pressed", String(item === button));
    });
    render();
  });
});
element<HTMLSelectElement>("ecosystem").addEventListener("change", (event) => {
  if (!(event.target instanceof HTMLSelectElement)) throw new Error("Invalid source control.");
  ecosystem = event.target.value;
  render();
});
