"use strict";

// ---------------------------------------------------------------------------
// State and API
// ---------------------------------------------------------------------------

const state = {
  capture: "live",
  tab: "calls",
  showAll: false,
  version: -1,
  status: null,
  overview: null,
  library: [],
  drift: null,
  against: "library",
  filters: { calls: "", endpoints: "", library: "" },
  selected: null, // {kind: "call"|"endpoint", id}
  openDrift: new Set(),
  graph: { view: null },
};

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];

async function api(path, { method = "GET", body } = {}) {
  const res = await fetch(path, {
    method,
    headers: { "X-Endpointer-Token": window.ENDPOINTER_TOKEN, ...(body !== undefined ? { "Content-Type": "application/json" } : {}) },
    body: body !== undefined ? JSON.stringify(body) : undefined,
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.error || `HTTP ${res.status}`);
  return data;
}

function esc(value) {
  return String(value ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
}

function toast(message, ms = 2600) {
  const el = $("#toast");
  el.textContent = message;
  el.hidden = false;
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => (el.hidden = true), ms);
}

async function act(fn, done) {
  try {
    const result = await fn();
    if (done) toast(typeof done === "function" ? done(result) : done);
    await refresh(true);
    return result;
  } catch (e) {
    toast(e.message, 5000);
  }
}

const q = (params) => new URLSearchParams(params).toString();
const time = (ts) => (ts ? new Date(ts * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" }) : "");
const date = (ts) => (ts ? new Date(ts * 1000).toLocaleString([], { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" }) : "");
const label = (e) => (e.graphql_op ? `${e.template} · ${e.graphql_op}` : e.template);
const methodBadge = (m) => `<span class="method m-${esc(m)}">${esc(m)}</span>`;
const statusText = (s, err) => `<span class="status s${String(s || 0)[0]}">${s || esc(err || "—")}</span>`;
const lvl = (level) => (level ? `<span class="lvl lvl-${esc(level.replace(" ", "-"))}">${esc(level)}</span>` : "");
const matches = (filter, ...fields) => !filter || fields.join(" ").toLowerCase().includes(filter.toLowerCase());

// ---------------------------------------------------------------------------
// Polling and header
// ---------------------------------------------------------------------------

async function poll() {
  try {
    const status = await api("/api/status");
    state.status = status;
    renderHeader();
    if (status.version !== state.version) await refresh();
  } catch (e) {
    banner("Lost the connection to Endpointer. Is it still running?", true);
  }
  setTimeout(poll, 1000);
}

function banner(text, error = false) {
  const el = $("#banner");
  el.hidden = !text;
  el.innerHTML = text || "";
  el.classList.toggle("error", error);
}

function renderHeader() {
  const s = state.status;
  const sel = $("#capture");
  const options = s.captures
    .map((c) => `<option value="${c.live ? "live" : esc(c.id)}">${c.live ? "● " : ""}${esc(c.name)} · ${c.calls}</option>`)
    .join("");
  if (sel.dataset.options !== options) {
    sel.innerHTML = options;
    sel.dataset.options = options;
  }
  if (![...sel.options].some((o) => o.value === state.capture)) state.capture = "live";
  sel.value = state.capture;

  const against = $("#against");
  const againstOpts =
    `<option value="library">your library</option>` +
    s.captures.filter((c) => !c.live && c.id !== state.capture).map((c) => `<option value="${esc(c.id)}">${esc(c.name)} (${date(c.started)})</option>`).join("");
  if (against.dataset.options !== againstOpts) {
    against.innerHTML = againstOpts;
    against.dataset.options = againstOpts;
    if (![...against.options].some((o) => o.value === state.against)) state.against = "library";
  }
  against.value = state.against;

  const b = s.browser;
  const rec = $("#rec");
  rec.classList.toggle("on", b.recording);
  rec.classList.toggle("paused", b.connected && !b.recording);
  $(".label", rec).textContent = b.recording ? "Recording" : b.connected ? "Paused" : "Off";
  rec.title = b.connected ? "Pause or resume recording" : "Open the capture browser";

  if (b.connected) banner("");
  else if (b.error) banner(`The capture browser closed (${esc(b.error)}). Press <b>Open</b> to start it again.`);
  else if (!b.found) banner("No Chrome, Edge, Brave or Chromium found. Set <code>ENDPOINTER_BROWSER</code>, attach to a running browser (⋯ menu), or import a HAR file.");
  else if (state.capture === "live" && !(state.overview && state.overview.counts.calls)) banner("Type a site above and press <b>Open</b>: it opens in a separate browser window, and everything it calls shows up here.");
  else banner("");
}

async function refresh(force = false) {
  if (!state.status) return;
  const version = state.status.version;
  try {
    const [overview, library] = await Promise.all([
      api(`/api/overview?${q({ capture: state.capture, all: state.showAll ? 1 : 0 })}`),
      api("/api/library"),
    ]);
    state.overview = overview;
    state.library = library;
    state.version = version;
    renderCounts();
    renderCalls();
    renderEndpoints();
    renderLibrary();
    if (state.tab === "graph") renderGraph();
    if (state.tab === "drift") await loadDrift();
    if (state.selected && (force || state.selected.kind === "endpoint")) await reopenDetail();
  } catch (e) {
    toast(e.message, 5000);
  }
}

function renderCounts() {
  const o = state.overview;
  $("#n-calls").textContent = o.counts.calls ? (state.showAll ? o.counts.calls : o.counts.calls - o.counts.hidden) : "";
  $("#n-endpoints").textContent = o.endpoints.length || "";
  $("#n-library").textContent = state.library.length || "";
}

// ---------------------------------------------------------------------------
// Lists
// ---------------------------------------------------------------------------

function renderCalls() {
  const o = state.overview;
  const f = state.filters.calls;
  const rows = o.calls.filter((c) => matches(f, c.method, c.url, c.status, c.graphql_op)).reverse();
  const saved = new Set(state.library.map((e) => e.key));
  const el = $("#calls-list");
  if (!rows.length) {
    el.innerHTML = `<div class="empty">${
      o.counts.calls
        ? `No API calls ${f ? "match" : "yet"}. ${o.counts.hidden} other requests (pages, scripts, images, analytics) are hidden. <a href="#" data-showall>Show them</a>.`
        : "<b>Nothing captured yet.</b><br>Open a site in the capture browser and use it as you normally would."
    }</div>`;
    return;
  }
  el.innerHTML = rows
    .map(
      (c) => `<div class="row ${c.important ? "" : "noise"} ${sel("call", c.id)}" data-call="${esc(c.id)}">
      ${methodBadge(c.method)}
      <div class="main"><div class="path">${esc(c.path)}${c.graphql_op ? ` <span class="pill">${esc(c.graphql_op)}</span>` : ""}</div>
      <div class="sub">${esc(c.host)} · ${time(c.ts)}${c.type ? " · " + esc(c.type) : ""}</div></div>
      <div class="side">${saved.has(c.key) ? '<span class="pill saved">saved</span>' : ""}${c.duration_ms ? c.duration_ms + " ms" : ""} ${statusText(c.status, c.error)}</div>
    </div>`
    )
    .join("");
}

function sel(kind, id) {
  return state.selected && state.selected.kind === kind && state.selected.id === id ? "selected" : "";
}

function renderEndpoints() {
  const f = state.filters.endpoints;
  const rows = state.overview.endpoints.filter((e) => matches(f, e.method, e.host, e.template, e.graphql_op));
  const el = $("#endpoints-list");
  if (!rows.length) {
    el.innerHTML = `<div class="empty">${f ? "No endpoints match." : "Endpoints appear here as calls come in, grouped by method and path (ids in paths become <code>{placeholders}</code>)."}</div>`;
    return;
  }
  el.innerHTML = rows
    .map(
      (e) => `<div class="row ${e.important ? "" : "noise"} ${sel("endpoint", e.key)}" data-endpoint="${esc(e.key)}">
      ${methodBadge(e.method)}
      <div class="main"><div class="path">${esc(label(e))}</div>
      <div class="sub">${esc(e.host)} · ${e.count} call${e.count === 1 ? "" : "s"} · ${esc(e.reasons.slice(0, 3).join(", "))}</div></div>
      <div class="side">${e.saved ? lvl(e.drift === "same" ? "" : e.drift) + '<span class="pill saved">saved</span>' : `<button class="ghost small" data-save="${esc(e.key)}">Save</button>`}
      ${e.statuses.map((s) => statusText(s)).join(" ")}</div>
    </div>`
    )
    .join("");
}

function renderLibrary() {
  const f = state.filters.library;
  const rows = state.library.filter((e) => matches(f, e.name, e.method, e.host, e.template, e.notes, (e.tags || []).join(" ")));
  const el = $("#library-list");
  if (!rows.length) {
    el.innerHTML = `<div class="empty">${
      f ? "Nothing matches." : "<b>Your library is empty.</b><br>Save endpoints from the Endpoints tab. Saved endpoints are always shown, and Drift compares later captures against them."
    }</div>`;
    return;
  }
  el.innerHTML = rows
    .map(
      (e) => `<div class="row ${sel("endpoint", e.key)}" data-endpoint="${esc(e.key)}">
      ${methodBadge(e.method)}
      <div class="main"><div class="path">${esc(e.name)}</div>
      <div class="sub mono">${esc(label(e))} · ${esc(e.host)}</div></div>
      <div class="side">${e.last_check ? lvl(e.last_check.level) + " " + date(e.last_check.at) : "not checked"}</div>
    </div>`
    )
    .join("");
}

// ---------------------------------------------------------------------------
// Drift
// ---------------------------------------------------------------------------

async function loadDrift() {
  try {
    state.drift = await api(`/api/drift?${q({ capture: state.capture, against: state.against })}`);
  } catch (e) {
    state.drift = null;
    $("#drift-list").innerHTML = `<div class="empty">${esc(e.message)}</div>`;
    return;
  }
  renderDrift();
}

function renderDrift() {
  const d = state.drift;
  const order = ["failing", "changed", "new", "unsure", "minor", "not seen", "same"];
  $("#drift-summary").innerHTML = order.filter((k) => d.summary[k]).map((k) => `${lvl(k)} <span class="count">${d.summary[k]}</span>`).join(" ");
  const el = $("#drift-list");
  if (!d.results.length) {
    el.innerHTML = `<div class="empty">${
      d.against === "library" ? "<b>Nothing to compare yet.</b><br>Save endpoints to your library, then come back after using the site again (or press <b>Re-check library</b>)." : "Neither capture has API calls."
    }</div>`;
    return;
  }
  el.innerHTML = d.results
    .map((r) => {
      const open = state.openDrift.has(r.key);
      const canAccept = d.against === "library" && !["same", "not seen"].includes(r.level);
      const diffs = (title, list) =>
        list.length ? `<div>${title}</div><ul>${list.map((x) => `<li class="k-${x.kind}">${esc(x.text)}</li>`).join("")}</ul>` : "";
      return `<div class="row" data-drift="${esc(r.key)}">
        ${methodBadge(r.method)}
        <div class="main"><div class="path">${esc(label(r))}</div><div class="sub">${esc(r.host)} · ${esc(r.notes.join("; ") || summarize(r))}</div></div>
        <div class="side">${lvl(r.level)}</div>
      </div>${
        open
          ? `<div class="drift-detail">${diffs("Response", r.response)}${diffs("Request", r.request)}${
              !r.response.length && !r.request.length && !r.notes.length ? "<div class='reasons'>No differences in shape.</div>" : ""
            }<div class="actions">${canAccept ? `<button class="ghost small" data-accept="${esc(r.key)}">Accept as new baseline</button>` : ""}${
              r.level === "new" && d.against === "library" ? `<button class="ghost small" data-save="${esc(r.key)}">Save to library</button>` : ""
            }<button class="ghost small" data-endpoint="${esc(r.key)}">Details</button></div></div>`
          : ""
      }`;
    })
    .join("");
}

function summarize(r) {
  const sig = r.response.filter((x) => x.kind === "missing" || x.kind === "type").length;
  const added = r.response.filter((x) => x.kind === "added").length;
  const parts = [];
  if (sig) parts.push(`${sig} field${sig === 1 ? "" : "s"} missing or retyped`);
  if (added) parts.push(`${added} new field${added === 1 ? "" : "s"}`);
  if (r.request.length) parts.push("request changed");
  return parts.join(", ") || (r.level === "same" ? "unchanged" : "");
}

// ---------------------------------------------------------------------------
// Detail panel
// ---------------------------------------------------------------------------

function openDetail(kind, id) {
  state.selected = { kind, id };
  $$(".row.selected").forEach((r) => r.classList.remove("selected"));
  $$(kind === "call" ? `[data-call="${CSS.escape(id)}"]` : `.row[data-endpoint="${CSS.escape(id)}"]`).forEach((r) => r.classList.add("selected"));
  return reopenDetail();
}

async function reopenDetail() {
  const { kind, id } = state.selected;
  try {
    if (kind === "call") renderCallDetail(await api(`/api/call?${q({ capture: state.capture, id })}`));
    else renderEndpointDetail(await api(`/api/endpoint?${q({ capture: state.capture, key: id })}`));
    $("#detail").hidden = false;
  } catch (e) {
    closeDetail();
  }
}

function closeDetail() {
  state.selected = null;
  $("#detail").hidden = true;
  $$(".row.selected").forEach((r) => r.classList.remove("selected"));
}

function highlight(value) {
  if (value === null || value === undefined || value === "") return '<span class="reasons">(empty)</span>';
  let text = typeof value === "string" ? value : JSON.stringify(value, null, 2);
  if (text.length > 300000) text = text.slice(0, 300000) + "\n… (truncated)";
  const html = esc(text);
  if (typeof value === "string") return `<pre class="json">${html}</pre>`;
  return `<pre class="json">${html.replace(
    /(&quot;(?:\\.|[^&]|&(?!quot;))*?&quot;)(\s*:)?|\b(true|false|null)\b|(-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?)/g,
    (m, str, colon, lit, num) =>
      str ? (colon ? `<span class="j-key">${str}</span>${colon}` : `<span class="j-str">${str}</span>`) : lit ? `<span class="j-lit">${lit}</span>` : `<span class="j-num">${num}</span>`
  )}</pre>`;
}

function kv(obj) {
  const entries = Object.entries(obj || {});
  if (!entries.length) return '<div class="reasons">(none)</div>';
  return `<div class="kv">${entries.map(([k, v]) => `<div class="k">${esc(k)}</div><div class="v">${esc(typeof v === "string" ? v : JSON.stringify(v))}</div>`).join("")}</div>`;
}

function depList(links, dir) {
  if (!links.length) return "";
  return links
    .map((l) => {
      const other = dir === "uses" ? l.source : l.target;
      const fields = l.fields.map((f) => (dir === "uses" ? `${f.from} → ${f.to}` : `${f.from} → ${f.to}`)).join("<br>");
      return `<div class="dep" data-endpoint="${esc(other)}"><div class="mono">${esc(other)}</div><div class="fields">${fields}</div></div>`;
    })
    .join("");
}

function curl(c) {
  const quote = (s) => `'${String(s).replace(/'/g, `'\\''`)}'`;
  const parts = [`curl -X ${c.method} ${quote(c.full_url)}`];
  for (const [k, v] of Object.entries(c.request_headers || {})) if (!k.startsWith(":") && k !== "content-length") parts.push(`-H ${quote(`${k}: ${v}`)}`);
  if (c.request_body !== null && c.request_body !== undefined)
    parts.push(`--data-raw ${quote(typeof c.request_body === "string" ? c.request_body : JSON.stringify(c.request_body))}`);
  return parts.join(" \\\n  ");
}

function renderCallDetail(c) {
  const saved = state.library.some((e) => e.key === c.key);
  $("#detail-title").innerHTML = `<div>${methodBadge(c.method)} ${statusText(c.status, c.error)} <span class="reasons">${time(c.ts)} · ${c.duration_ms} ms · ${esc(c.mime || c.response_kind)}</span></div>
    <div class="path">${esc(c.full_url)}</div>`;
  const tab = renderCallDetail.tab || "response";
  const sections = {
    response: highlight(c.response_body),
    request: (Object.keys(c.query || {}).length ? `<h3>Query</h3>${kv(c.query)}` : "") + `<h3>Body</h3>${highlight(c.request_body)}`,
    headers: `<h3>Request headers</h3>${kv(c.request_headers)}<h3>Response headers</h3>${kv(c.response_headers)}`,
    links: `<h3>Uses values from</h3>${depList(c.uses, "uses") || '<div class="reasons">nothing earlier</div>'}<h3>Feeds</h3>${
      depList(c.feeds, "feeds") || '<div class="reasons">nothing later</div>'
    }`,
  };
  $("#detail-body").innerHTML = `
    <div class="actions">
      ${saved ? `<button class="ghost small" data-endpoint="${esc(c.key)}">Open in library</button>` : `<button class="primary small" data-save="${esc(c.key)}">Save endpoint</button>`}
      <button class="ghost small" data-endpoint="${esc(c.key)}">Endpoint</button>
      <button class="ghost small" data-copy-curl>Copy as curl</button>
      <button class="ghost small" data-rule="hide_keys" data-value="${esc(c.key)}">Hide endpoint</button>
      <button class="ghost small" data-rule="hide_hosts" data-value="${esc(c.host)}">Hide ${esc(c.host)}</button>
      ${c.relevance && !c.relevance.important ? `<button class="ghost small" data-rule="pin_keys" data-value="${esc(c.key)}">Always show</button>` : ""}
    </div>
    <div class="reasons">${c.relevance ? `Relevance ${c.relevance.score}: ${esc(c.relevance.reasons.join(", "))}` : ""}</div>
    <div class="mono reasons">${esc(c.key)}</div>
    <div class="subtabs">${Object.keys(sections).map((k) => `<button data-subtab="${k}" class="${k === tab ? "active" : ""}">${k[0].toUpperCase() + k.slice(1)}</button>`).join("")}</div>
    <div>${sections[tab]}</div>
    <p class="reasons">Secrets (auth headers, cookies, token and password fields) are shown as <code>&lt;secret:…&gt;</code> fingerprints and never saved.</p>`;
  $("#detail-body").dataset.curl = curl(c);
}

function schemaTree(schema, indent = "") {
  if (!schema || !Object.keys(schema).length) return `${indent}any\n`;
  const t = Array.isArray(schema.type) ? schema.type.join(" | ") : schema.type || "any";
  if (schema.properties) {
    const req = new Set(schema.required || []);
    return Object.entries(schema.properties)
      .map(([k, sub]) => {
        const st = Array.isArray(sub.type) ? sub.type.join(" | ") : sub.type || "any";
        const nested = sub.properties || sub.items || sub.additionalProperties ? "\n" + schemaTree(sub, indent + "  ") : "\n";
        return `${indent}${k}${req.has(k) ? "" : "?"}: ${st}${nested}`;
      })
      .join("");
  }
  if (schema.additionalProperties) return `${indent}{<id>}:\n${schemaTree(schema.additionalProperties, indent + "  ")}`;
  if (schema.items) {
    const it = schema.items;
    const st = Array.isArray(it.type) ? it.type.join(" | ") : it.type || "any";
    return it.properties || it.additionalProperties ? `${indent}[] ${st}\n${schemaTree(it, indent + "  ")}` : `${indent}[] ${st}\n`;
  }
  return `${indent}${t}\n`;
}

function renderEndpointDetail(d) {
  const shape = d.shape || (d.library && d.library.baseline) || {};
  const lib = d.library;
  $("#detail-title").innerHTML = `<div>${methodBadge(shape.method)} ${lib ? `<b>${esc(lib.name)}</b>` : ""}</div>
    <div class="path">${esc(shape.host)}${esc(label(shape))}</div>
    <div class="sub">${d.shape ? `${d.shape.count} call${d.shape.count === 1 ? "" : "s"} in this capture` : "not in this capture"}${lib ? ` · saved ${date(lib.saved)}` : ""}</div>`;
  const drift = d.drift;
  const baseline = lib && lib.baseline;
  $("#detail-body").innerHTML = `
    <div class="actions">
      ${lib ? `<button class="ghost small danger" data-remove="${esc(d.key)}">Remove from library</button>` : d.shape ? `<button class="primary small" data-save="${esc(d.key)}">Save to library</button>` : ""}
      ${lib && d.shape && drift && drift.level !== "same" ? `<button class="ghost small" data-accept="${esc(d.key)}">Accept this capture as baseline</button>` : ""}
      <button class="ghost small" data-rule="hide_keys" data-value="${esc(d.key)}">Hide endpoint</button>
      ${d.relevance && !d.relevance.important ? `<button class="ghost small" data-rule="pin_keys" data-value="${esc(d.key)}">Always show</button>` : ""}
    </div>
    ${d.relevance ? `<div class="reasons">Relevance ${d.relevance.score}: ${esc(d.relevance.reasons.join(", "))}</div>` : ""}
    ${
      lib
        ? `<h3>Library</h3><input class="field" data-lib-field="name" value="${esc(lib.name)}" placeholder="Name">
           <textarea class="field" data-lib-field="notes" placeholder="Notes">${esc(lib.notes || "")}</textarea>
           <input class="field" data-lib-field="tags" value="${esc((lib.tags || []).join(", "))}" placeholder="Tags, comma separated">
           <div class="reasons">Baseline from ${date(lib.updated)}${lib.history && lib.history.length ? ` · ${lib.history.length} earlier baseline${lib.history.length === 1 ? "" : "s"}` : ""}${
             lib.last_check ? ` · last check ${date(lib.last_check.at)}: ${esc(lib.last_check.level)}` : ""
           }</div>`
        : ""
    }
    ${
      drift
        ? `<h3>Compared with the baseline</h3><div>${lvl(drift.level)} ${esc(drift.notes.join("; "))}</div>
           <div class="drift-detail" style="padding-left:0">${[...drift.response, ...drift.request.map((x) => ({ ...x, text: "request " + x.text }))]
             .map((x) => `<div class="mono k-${x.kind}">${esc(x.text)}</div>`)
             .join("")}</div>`
        : ""
    }
    <h3>Statuses</h3><div>${(shape.statuses || []).map((s) => statusText(s)).join(" ") || "—"}</div>
    ${shape.query && shape.query.length ? `<h3>Query parameters</h3><div class="mono">${shape.query.map(esc).join(", ")}</div>` : ""}
    ${shape.request_schema && Object.keys(shape.request_schema).length ? `<h3>Request body</h3><pre class="json">${esc(schemaTree(shape.request_schema))}</pre>` : ""}
    <h3>Response</h3><pre class="json">${esc(schemaTree(shape.response_schema))}</pre>
    ${baseline && d.shape ? "" : ""}
    <h3>Uses values from</h3>${depList(d.uses, "uses") || '<div class="reasons">nothing in this capture</div>'}
    <h3>Feeds</h3>${depList(d.feeds, "feeds") || '<div class="reasons">nothing in this capture</div>'}
    ${
      d.calls && d.calls.length
        ? `<h3>Calls</h3>${d.calls
            .slice()
            .reverse()
            .map((c) => `<div class="row" data-call="${esc(c.id)}">${methodBadge(c.method)}<div class="main"><div class="path">${esc(c.path)}</div><div class="sub">${time(c.ts)}</div></div><div class="side">${statusText(c.status, c.error)}</div></div>`)
            .join("")}`
        : ""
    }`;
}

// ---------------------------------------------------------------------------
// Graph
// ---------------------------------------------------------------------------

const NODE_W = 230, NODE_H = 46, GAP_X = 34, GAP_Y = 70;

function layout(nodes, edges) {
  const out = new Map(nodes.map((n) => [n.key, []]));
  const inc = new Map(nodes.map((n) => [n.key, []]));
  edges.forEach((e) => { out.get(e.source).push(e.target); inc.get(e.target).push(e.source); });
  // Break cycles: DFS from sources (and from anything left over), ignoring back edges.
  const back = new Set(), seen = new Set(), stack = new Set();
  const visit = (k) => {
    seen.add(k); stack.add(k);
    for (const t of out.get(k)) {
      if (stack.has(t)) back.add(k + "\u0000" + t);
      else if (!seen.has(t)) visit(t);
    }
    stack.delete(k);
  };
  nodes.filter((n) => !inc.get(n.key).length).forEach((n) => !seen.has(n.key) && visit(n.key));
  nodes.forEach((n) => !seen.has(n.key) && visit(n.key));
  // Longest path from a source gives the layer.
  const rank = new Map();
  const rankOf = (k, guard = 0) => {
    if (rank.has(k)) return rank.get(k);
    const parents = inc.get(k).filter((p) => !back.has(p + "\u0000" + k));
    const r = parents.length && guard < 200 ? 1 + Math.max(...parents.map((p) => rankOf(p, guard + 1))) : 0;
    rank.set(k, r);
    return r;
  };
  nodes.forEach((n) => rankOf(n.key));
  const layers = [];
  nodes.forEach((n) => (layers[rank.get(n.key)] ||= []).push(n.key));
  // Order each layer by the average position of its parents (two sweeps).
  const pos = new Map();
  layers.forEach((layer) => layer.forEach((k, i) => pos.set(k, i)));
  for (let sweep = 0; sweep < 2; sweep++) {
    layers.forEach((layer, r) => {
      if (!r) return;
      const bary = (k) => {
        const ps = inc.get(k).filter((p) => rank.get(p) < r);
        return ps.length ? ps.reduce((a, p) => a + pos.get(p), 0) / ps.length : pos.get(k);
      };
      layer.sort((a, b) => bary(a) - bary(b));
      layer.forEach((k, i) => pos.set(k, i));
    });
  }
  const widest = Math.max(...layers.map((l) => l.length));
  const coords = new Map();
  layers.forEach((layer, r) => {
    const offset = ((widest - layer.length) * (NODE_W + GAP_X)) / 2;
    layer.forEach((k, i) => coords.set(k, { x: offset + i * (NODE_W + GAP_X), y: r * (NODE_H + GAP_Y) }));
  });
  return { coords, back, width: widest * (NODE_W + GAP_X) - GAP_X, height: layers.length * (NODE_H + GAP_Y) - GAP_Y };
}

function trim(text, n) {
  return text.length > n ? "…" + text.slice(text.length - n + 1) : text;
}

function renderGraph() {
  const o = state.overview;
  const svg = $("#graph");
  const empty = $("#graph-empty");
  const byKey = new Map(o.endpoints.map((e) => [e.key, e]));
  const edges = o.links.filter((l) => byKey.has(l.source) && byKey.has(l.target));
  const keys = new Set(edges.flatMap((e) => [e.source, e.target]));
  const nodes = [...keys].map((k) => byKey.get(k));
  if (!nodes.length) {
    svg.innerHTML = "";
    empty.hidden = false;
    empty.innerHTML = `<div class="empty"><b>No dependencies yet.</b><br>When a value from one response (an id, a cursor, a token) is used in a later request, the two endpoints are linked here.${
      o.endpoints.length ? `<br>${o.endpoints.length} endpoint${o.endpoints.length === 1 ? "" : "s"} so far, none linked.` : ""
    }</div>`;
    return;
  }
  empty.hidden = true;
  const { coords, back, width, height } = layout(nodes, edges);
  const center = (k) => coords.get(k);
  const edgePath = (e) => {
    const a = center(e.source), b = center(e.target);
    const x1 = a.x + NODE_W / 2, y1 = a.y + NODE_H, x2 = b.x + NODE_W / 2, y2 = b.y;
    if (back.has(e.source + "\u0000" + e.target) || y2 <= y1) {
      const side = Math.max(a.x, b.x) + NODE_W + 30;
      return `M${a.x + NODE_W},${a.y + NODE_H / 2} C${side},${a.y + NODE_H / 2} ${side},${b.y + NODE_H / 2} ${b.x + NODE_W},${b.y + NODE_H / 2}`;
    }
    const dy = Math.max(30, (y2 - y1) / 2);
    if (y2 - y1 > NODE_H + GAP_Y) {
      // Skips a layer: bow out sideways so it doesn't run behind the nodes in between.
      const off = (x2 >= x1 ? 1 : -1) * (NODE_W * 0.75);
      return `M${x1},${y1} C${x1 + off},${y1 + dy} ${x2 + off},${y2 - dy} ${x2},${y2}`;
    }
    return `M${x1},${y1} C${x1},${y1 + dy} ${x2},${y2 - dy} ${x2},${y2}`;
  };
  const color = (m) => `var(--${(m || "get").toLowerCase()}, var(--get))`;
  svg.innerHTML = `
    <defs><marker id="arrow" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse"><path d="M0,0 L10,5 L0,10 z" fill="var(--muted)"/></marker>
    <marker id="arrow-focus" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse"><path d="M0,0 L10,5 L0,10 z" fill="var(--accent)"/></marker></defs>
    <g id="graph-root">
    ${edges
      .map(
        (e, i) => `<path class="edge" data-i="${i}" data-s="${esc(e.source)}" data-t="${esc(e.target)}" d="${edgePath(e)}" marker-end="url(#arrow)"><title>${esc(
          e.fields.map((f) => `${f.from} → ${f.to}`).join("\n")
        )}\n(${e.count}×)</title></path>`
      )
      .join("")}
    ${nodes
      .map((n) => {
        const { x, y } = center(n.key);
        return `<g class="node ${n.saved ? "saved" : ""}" data-endpoint="${esc(n.key)}" transform="translate(${x},${y})">
          <rect width="${NODE_W}" height="${NODE_H}"></rect>
          <rect width="4" height="${NODE_H}" style="fill:${color(n.method)};stroke:none" rx="2"></rect>
          <text class="nm" x="12" y="18" style="fill:${color(n.method)}">${esc(n.method)}</text>
          <text x="${12 + n.method.length * 7 + 6}" y="18">${esc(trim(label(n), 29 - n.method.length))}</text>
          <text class="host" x="12" y="35">${esc(trim(n.host, 34))} · ${n.count}×</text>
          <title>${esc(n.method + " " + n.host + label(n))}</title>
        </g>`;
      })
      .join("")}
    </g>`;
  if (!state.graph.view || state.graph.size !== `${width}x${height}`) {
    state.graph.size = `${width}x${height}`;
    fitGraph(width, height);
  } else applyView();
}

function fitGraph(width, height) {
  const [w, h] = state.graph.size ? state.graph.size.split("x").map(Number) : [width, height];
  const pad = 40;
  state.graph.view = { x: -pad, y: -pad, w: w + pad * 2 + 40, h: h + pad * 2 };
  applyView();
}

function applyView() {
  const v = state.graph.view;
  if (v) $("#graph").setAttribute("viewBox", `${v.x} ${v.y} ${v.w} ${v.h}`);
}

function focusNode(key) {
  const linked = new Set([key]);
  $$("#graph .edge").forEach((e) => {
    const on = key && (e.dataset.s === key || e.dataset.t === key);
    if (on) linked.add(e.dataset.s).add(e.dataset.t);
    e.classList.toggle("focus", !!on);
    e.classList.toggle("dim", !!key && !on);
    e.setAttribute("marker-end", on ? "url(#arrow-focus)" : "url(#arrow)");
  });
  $$("#graph .node").forEach((n) => {
    n.classList.toggle("focus", n.dataset.endpoint === key);
    n.classList.toggle("dim", !!key && !linked.has(n.dataset.endpoint));
  });
}

function setupGraphInteraction() {
  const svg = $("#graph");
  let drag = null;
  svg.addEventListener("pointerdown", (ev) => {
    if (ev.target.closest(".node")) return;
    drag = { x: ev.clientX, y: ev.clientY, view: { ...state.graph.view } };
    svg.classList.add("dragging");
    svg.setPointerCapture(ev.pointerId);
  });
  svg.addEventListener("pointermove", (ev) => {
    if (!drag || !state.graph.view) return;
    const scale = state.graph.view.w / svg.clientWidth;
    state.graph.view.x = drag.view.x - (ev.clientX - drag.x) * scale;
    state.graph.view.y = drag.view.y - (ev.clientY - drag.y) * scale;
    applyView();
  });
  svg.addEventListener("pointerup", () => { drag = null; svg.classList.remove("dragging"); });
  svg.addEventListener("wheel", (ev) => {
    if (!state.graph.view) return;
    ev.preventDefault();
    const v = state.graph.view;
    const rect = svg.getBoundingClientRect();
    const factor = Math.exp(ev.deltaY * 0.0015);
    const scale = Math.max(v.w / rect.width, v.h / rect.height);
    const mx = v.x + (ev.clientX - rect.left) * scale, my = v.y + (ev.clientY - rect.top) * scale;
    v.x = mx - (mx - v.x) * factor; v.y = my - (my - v.y) * factor; v.w *= factor; v.h *= factor;
    applyView();
  }, { passive: false });
  svg.addEventListener("mouseover", (ev) => {
    const node = ev.target.closest(".node");
    focusNode(node ? node.dataset.endpoint : null);
  });
  svg.addEventListener("mouseleave", () => focusNode(null));
}

// ---------------------------------------------------------------------------
// Events
// ---------------------------------------------------------------------------

function switchTab(tab) {
  state.tab = tab;
  $$(".tabs button").forEach((b) => b.classList.toggle("active", b.dataset.tab === tab));
  $$(".view").forEach((v) => v.classList.toggle("active", v.id === `view-${tab}`));
  try { localStorage.setItem("endpointer.tab", tab); } catch (e) { /* storage may be blocked */ }
  if (tab === "graph" && state.overview) renderGraph();
  if (tab === "drift" && state.overview) loadDrift();
}

function saveKeys(keys) {
  return act(() => api("/api/library/save", { method: "POST", body: { capture: state.capture, keys } }), (r) => `Saved ${r.length} endpoint${r.length === 1 ? "" : "s"} to your library`);
}

document.addEventListener("click", async (ev) => {
  const t = ev.target;
  const tabBtn = t.closest(".tabs button");
  if (tabBtn) return switchTab(tabBtn.dataset.tab);
  if (t.closest("#menu-btn")) { $("#menu").hidden = !$("#menu").hidden; return; }
  if (!t.closest("#menu")) $("#menu").hidden = true;

  const el = t.closest("[data-save],[data-accept],[data-remove],[data-rule],[data-subtab],[data-copy-curl],[data-showall],[data-act],[data-drift],[data-call],[data-endpoint]");
  if (!el) return;
  if (el.matches("[data-showall]")) { ev.preventDefault(); $("#show-all").checked = true; state.showAll = true; return refresh(); }
  if (el.dataset.save) return saveKeys([el.dataset.save]);
  if (el.dataset.accept)
    return act(() => api("/api/drift/accept", { method: "POST", body: { capture: state.capture, key: el.dataset.accept } }), "Baseline updated (the previous one is kept in history)");
  if (el.dataset.remove) {
    if (!confirm("Remove this endpoint from your library? Its baseline and history are deleted.")) return;
    return act(() => api("/api/library/remove", { method: "POST", body: { key: el.dataset.remove } }), "Removed");
  }
  if (el.dataset.rule)
    return act(() => api("/api/rules", { method: "POST", body: { kind: el.dataset.rule, value: el.dataset.value, on: true } }), el.dataset.rule.startsWith("hide") ? "Hidden. Tick “show noise” to see it again." : "It will always be shown");
  if (el.dataset.subtab) { renderCallDetail.tab = el.dataset.subtab; return reopenDetail(); }
  if (el.matches("[data-copy-curl]")) { await navigator.clipboard.writeText($("#detail-body").dataset.curl || ""); return toast("Copied (secrets are fingerprints: paste your own)"); }
  if (el.dataset.act) return menuAction(el.dataset.act);
  if (el.dataset.drift) {
    state.openDrift.has(el.dataset.drift) ? state.openDrift.delete(el.dataset.drift) : state.openDrift.add(el.dataset.drift);
    return renderDrift();
  }
  if (el.dataset.call) return openDetail("call", el.dataset.call);
  if (el.dataset.endpoint) return openDetail("endpoint", el.dataset.endpoint);
});

async function menuAction(name) {
  $("#menu").hidden = true;
  const live = state.capture === "live";
  if (name === "new") return act(() => api("/api/capture/new", { method: "POST", body: {} }).then(() => (state.capture = "live")), "Started a new capture; the previous one is saved");
  if (name === "rename") {
    const current = state.status.captures.find((c) => (c.live ? "live" : c.id) === state.capture);
    const value = prompt("Name this capture", current ? current.name : "");
    if (value) return act(() => api("/api/capture/rename", { method: "POST", body: { id: state.capture, name: value } }), "Renamed");
  }
  if (name === "import") return $("#har-file").click();
  if (name === "export") {
    const data = await api(`/api/export/har?${q({ capture: state.capture })}`);
    const blob = new Blob([JSON.stringify(data, null, 2)], { type: "application/json" });
    const a = Object.assign(document.createElement("a"), { href: URL.createObjectURL(blob), download: `endpointer-${state.capture}.har` });
    a.click();
    return;
  }
  if (name === "attach") {
    const port = prompt("DevTools port of the running browser (started with --remote-debugging-port=PORT)", "9222");
    if (port) return act(() => api("/api/browser/attach", { method: "POST", body: { port: Number(port) } }), "Attached");
  }
  if (name === "stop") return act(() => api("/api/browser/stop", { method: "POST", body: {} }), "Capture browser closed; the capture is saved");
  if (name === "delete") {
    if (!confirm(live ? "Clear every call in the live capture?" : "Delete this saved capture?")) return;
    return act(() => api("/api/capture/delete", { method: "POST", body: { id: state.capture } }).then(() => (state.capture = "live")), "Deleted");
  }
}

$("#har-file").addEventListener("change", async (ev) => {
  const file = ev.target.files[0];
  ev.target.value = "";
  if (!file) return;
  try {
    const har = JSON.parse(await file.text());
    const meta = await api("/api/import/har", { method: "POST", body: { har, name: file.name.replace(/\.har$/i, "") } });
    state.capture = meta.id;
    toast(`Imported ${meta.calls} calls`);
    await poll.once();
  } catch (e) {
    toast(`Couldn't import: ${e.message}`, 5000);
  }
});

poll.once = async () => {
  state.status = await api("/api/status");
  renderHeader();
  await refresh(true);
};

$("#go").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  let url = $("#url").value.trim();
  if (url && !/^[a-z]+:\/\//i.test(url)) url = "https://" + url;
  const button = $("#go button");
  button.disabled = true;
  try {
    await api("/api/browser/launch", { method: "POST", body: { url } });
    state.capture = "live";
    $("#url").value = "";
    await poll.once();
  } catch (e) {
    toast(e.message, 6000);
  } finally {
    button.disabled = false;
  }
});

$("#rec").addEventListener("click", async () => {
  const b = state.status && state.status.browser;
  if (!b || !b.connected) return $("#go").requestSubmit();
  await act(() => api("/api/recording", { method: "POST", body: { on: !b.recording } }));
  await poll.once();
});

$("#capture").addEventListener("change", (ev) => {
  state.capture = ev.target.value;
  state.graph.view = null;
  closeDetail();
  refresh(true);
});
$("#against").addEventListener("change", (ev) => { state.against = ev.target.value; loadDrift(); });
$("#show-all").addEventListener("change", (ev) => { state.showAll = ev.target.checked; refresh(); });
$("#detail-close").addEventListener("click", closeDetail);
document.addEventListener("keydown", (ev) => { if (ev.key === "Escape") closeDetail(); });
$("#graph-fit").addEventListener("click", () => fitGraph());
$("#save-all").addEventListener("click", () => {
  const keys = state.overview.endpoints.filter((e) => !e.saved && matches(state.filters.endpoints, e.method, e.host, e.template, e.graphql_op)).map((e) => e.key);
  if (!keys.length) return toast("Everything shown is already saved");
  if (confirm(`Save ${keys.length} endpoint${keys.length === 1 ? "" : "s"} to your library?`)) saveKeys(keys);
});
$("#check").addEventListener("click", async () => {
  const button = $("#check");
  button.disabled = true;
  button.textContent = "Checking…";
  try {
    const report = await api("/api/check", { method: "POST", body: {} });
    state.capture = report.capture;
    state.against = "library";
    state.drift = report;
    renderDrift();
    toast(`Replayed ${report.replayed.length} GET request${report.replayed.length === 1 ? "" : "s"}${report.skipped.length ? `, skipped ${report.skipped.length}` : ""}`);
    await poll.once();
  } catch (e) {
    toast(e.message, 6000);
  } finally {
    button.disabled = false;
    button.textContent = "Re-check library";
  }
});
$$(".search").forEach((input) =>
  input.addEventListener("input", () => {
    state.filters[input.dataset.for] = input.value;
    ({ calls: renderCalls, endpoints: renderEndpoints, library: renderLibrary })[input.dataset.for]();
  })
);
$("#detail-body").addEventListener("change", (ev) => {
  const field = ev.target.dataset.libField;
  if (!field || !state.selected) return;
  const value = field === "tags" ? ev.target.value.split(",").map((s) => s.trim()).filter(Boolean) : ev.target.value;
  act(() => api("/api/library/update", { method: "POST", body: { key: state.selected.id, [field]: value } }), "Saved");
});

setupGraphInteraction();
try {
  const tab = localStorage.getItem("endpointer.tab");
  if (tab && $(`.tabs [data-tab="${tab}"]`)) switchTab(tab);
} catch (e) { /* storage may be blocked */ }
poll();
