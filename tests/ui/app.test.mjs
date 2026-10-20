import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";
import { JSDOM } from "jsdom";

const assets = new URL("../../src/element_mcp/ui_assets/", import.meta.url);
const html = readFileSync(new URL("index.html", assets), "utf8");
const app = readFileSync(new URL("app.js", assets), "utf8");
const theme = readFileSync(new URL("theme.js", assets), "utf8");
const settle = async () => { for (let i = 0; i < 25; i++) await Promise.resolve(); };

async function setup(t, options = {}) {
  const dom = new JSDOM(html, { url: "http://localhost:9900", runScripts: "outside-only" });
  const w = dom.window;
  await new Promise(resolve => w.document.addEventListener("DOMContentLoaded", resolve, { once: true }));
  t.after(() => w.close());
  let now = 1_000_000, sequence = 0, reloads = 0;
  const timers = new Map(), calls = [], listeners = new Map();
  w.Date.now = () => now;
  w.setTimeout = (fn, ms) => { const id = ++sequence; timers.set(id, { fn, at: now + ms }); return id; };
  w.clearTimeout = id => timers.delete(id);
  const advance = async ms => {
    const end = now + ms;
    for (;;) {
      const next = [...timers].filter(([, timer]) => timer.at <= end).sort((a, b) => a[1].at - b[1].at)[0];
      if (!next) break;
      now = next[1].at; timers.delete(next[0]); next[1].fn(); await settle();
    }
    now = end; await settle();
  };
  const media = { matches: Boolean(options.dark), addEventListener: (type, fn) => listeners.set(type, fn) };
  w.matchMedia = () => media;
  if (options.theme) w.localStorage.setItem("element-mcp.theme", options.theme);
  if (options.notice) w.sessionStorage.setItem("element-mcp.completed", JSON.stringify(options.notice));
  if (options.storageBlocked) Object.defineProperty(w, "localStorage", { get() { throw new Error("blocked"); } });
  const state = {
    status: { server: { version: "0.23.3" }, updates: { state: "current", source: { kind: "remote", label: "origin" }, can_apply: false }, restart: { can_restart: true, state: "idle" } },
    documentation: { status: "ready", path: "/docs", path_change_supported: true, aggregate: { documents: 200, chunks: 700 } },
    console: { status: "ready", enabled: true, configured: true, server: "https://example.test", client_id: "test-client", secret_present: true, credential_kind: "client_credentials" },
    runtime: { status: "configured", instance_root: "/instance", application_manager: { status: "configured" } },
    actions: { enabled: false, allowed_actions: [], upload_roots: [], max_upload_bytes: 12345678, approval_ttl_seconds: 42 },
  };
  options.configure?.(state);
  w.fetch = async (url, request = {}) => {
    const path = url.split("?")[0];
    const body = request.body ? JSON.parse(request.body) : undefined;
    const call = { path, method: request.method || "GET", body, headers: request.headers };
    calls.push(call);
    const custom = await options.respond?.(call, state);
    if (custom) return { ok: custom.ok !== false, status: custom.status || 200, json: async () => structuredClone(custom.payload) };
    let payload;
    if (path === "/api/status" || path === "/api/updates/check") payload = state.status;
    else if (path === "/api/updates/source") {
      state.status.updates.source = body.path ? { kind: "local", label: body.path } : { kind: "remote", label: "origin" };
      payload = state.status;
    } else if (path === "/api/documentation/activate") payload = { ...state.documentation, path: body.path };
    else if (path === "/api/documentation") payload = state.documentation;
    else {
      const key = path.split("/")[2];
      payload = { ...state[key], ...(body || {}) };
      if (body) state[key] = payload;
    }
    return { ok: true, json: async () => structuredClone(payload) };
  };
  w.eval(theme);
  w.document.dispatchEvent(new w.Event("DOMContentLoaded"));
  // Replace only browser navigation, so restart polling can be verified without leaving the test DOM.
  w.recordReload = () => reloads++;
  w.eval(app.replace("window.location.reload()", "window.recordReload()"));
  await settle();
  await advance(3000);
  const $ = selector => w.document.querySelector(selector);
  const input = (selector, value) => { $(selector).value = value; $(selector).dispatchEvent(new w.Event("input", { bubbles: true })); };
  const submit = async selector => { $(selector).dispatchEvent(new w.Event("submit", { bubbles: true, cancelable: true })); await settle(); };
  return { w, $, state, calls, advance, input, submit, media, listeners, reloads: () => reloads };
}

test("navigation, startup state and local assets", async t => {
  const { $, calls } = await setup(t);
  $('[data-page="updates"]').click();
  assert.equal($("#page-updates").hidden, false);
  assert.equal($("#page-connections").hidden, true);
  assert.equal($("#server-state-label").textContent, "Работает");
  assert.equal($("#console-client-secret").value, "");
  assert.equal($("#console-client-secret").placeholder, "Сохранён");
  assert.equal($("#documentation-documents").textContent, "200");
  assert.equal(calls.some(c => /apply|restart/.test(c.path)), false);
  assert.doesNotMatch(html, /(?:src|href)="https?:/);
});

test("toasts last exactly 3 seconds and don't replace button icons", async t => {
  const { $, advance } = await setup(t);
  $("#update-action").click(); await settle();
  assert.equal($("#toasts").children.length, 1);
  assert.match($(".toast-message").textContent, /последняя версия/);
  await advance(2999); assert.equal($("#toasts").children.length, 1);
  await advance(1); assert.equal($("#toasts").children.length, 0);
  assert.ok($("#restart-action svg"));
  assert.ok($("#update-action svg"));
});

test("errors are toasts, use textContent, and can be dismissed", async t => {
  const { $, submit } = await setup(t, { respond: call => call.method === "POST" && call.path.includes("runtime") ? { ok: false, payload: { message: "<img src=x onerror=alert(1)>" } } : null });
  await submit("#runtime-settings");
  assert.equal($(".toast-message").textContent, "<img src=x onerror=alert(1)>");
  assert.equal($(".toast-message img"), null);
  assert.equal($(".toast-message").getAttribute("role"), "alert");
  assert.equal($("#runtime-error").className, "sr-only");
  $(".toast-close").click(); assert.equal($("#toasts").children.length, 0);
  assert.equal($("#runtime-save").disabled, false);
});

test("theme follows system, persists explicit choice, and survives unavailable storage", async t => {
  const a = await setup(t, { dark: true });
  assert.equal(a.$("html").dataset.theme, "dark");
  a.$("#theme-action").click();
  assert.equal(a.$("html").dataset.theme, "light");
  assert.equal(a.w.localStorage.getItem("element-mcp.theme"), "light");
  a.listeners.get("change")(); assert.equal(a.$("html").dataset.theme, "light");
  const b = await setup(t, { dark: true, theme: "light" });
  assert.equal(b.$("html").dataset.theme, "light");
  const c = await setup(t, { storageBlocked: true });
  c.$("#theme-action").click(); assert.equal(c.$("html").dataset.theme, "dark");
});

test("Console reuses protected secret and rolls back failed disable", async t => {
  const { $, calls, submit } = await setup(t, { respond: call => call.body?.enabled === false && call.path.includes("console") ? { ok: false, payload: { message: "Console недоступна" } } : null });
  await submit("#console-settings");
  assert.equal(calls.find(c => c.body?.server).body.client_secret, null);
  $("#console-enabled").click(); await settle();
  assert.equal($("#console-enabled").checked, true);
  assert.equal($("#console-fields").disabled, false);
  assert.equal($("#console-save").disabled, false);
});

test("failed new Console connection stays editable", async t => {
  const { $, input, submit } = await setup(t, { configure: s => { s.console = { enabled: false, configured: false }; }, respond: c => c.method === "POST" && c.path.includes("console") ? { ok: false, payload: { message: "Нет доступа" } } : null });
  $("#console-enabled").click(); input("#console-server", "https://example.test");
  await submit("#console-settings");
  assert.equal($("#console-fields").disabled, false);
  assert.equal($("#console-save").disabled, false);
});

test("actions require explicit save, retain limits and deduplicate upload paths", async t => {
  const { $, calls, input, submit } = await setup(t);
  $("#actions-enabled").click(); await settle();
  assert.equal(calls.filter(c => c.method === "POST" && c.path.includes("actions")).length, 0);
  $("#action-upload").click(); input("#actions-upload-roots", "/builds\n/builds, /releases");
  await submit("#actions-settings");
  const body = calls.find(c => c.method === "POST" && c.path.includes("actions")).body;
  assert.deepEqual(body, { enabled: true, allowed_actions: ["upload_project_assembly"], upload_roots: ["/builds", "/releases"], max_upload_bytes: 12345678, approval_ttl_seconds: 42 });
  $("#actions-enabled").click(); await settle();
  assert.equal(calls.at(-1).body.enabled, false);
  assert.equal($("#actions-fields").disabled, true);
});

test("actions reject empty selection and missing upload roots without POST", async t => {
  const { $, calls, submit } = await setup(t);
  $("#actions-enabled").click(); await submit("#actions-settings");
  assert.match($(".toast-message").textContent, /Выберите/);
  $("#action-upload").click(); await submit("#actions-settings");
  assert.equal($("#actions-upload-roots").getAttribute("aria-invalid"), "true");
  assert.equal(calls.some(c => c.method === "POST" && c.path.includes("actions")), false);
});

test("documentation lock is preserved and failed activation keeps active corpus", async t => {
  const a = await setup(t, { configure: s => { s.documentation.path_change_supported = false; s.documentation.path_source = "environment"; } });
  assert.equal(a.$("#documentation-path").disabled, true);
  assert.match(a.$("#documentation-help").textContent, /ELEMENT_DOCS_PATH/);
  const b = await setup(t, { respond: c => c.path.endsWith("/activate") ? { ok: false, payload: { message: "Индекс не найден" } } : null });
  b.input("#documentation-path", "/missing"); await b.submit("#documentation-settings");
  assert.equal(b.$("#documentation-summary").textContent, "Подключена");
  assert.equal(b.$("#documentation-documents").textContent, "200");
  assert.equal(b.$("#documentation-activate").disabled, false);
});

test("source changes preserve API semantics and blank local path is rejected", async t => {
  const { $, calls, input, submit } = await setup(t);
  $("#source-use-local").click(); await submit("#source-settings");
  assert.equal(calls.some(c => c.path === "/api/updates/source"), false);
  input("#source-path", "/repo"); await submit("#source-settings");
  assert.deepEqual(calls.find(c => c.path === "/api/updates/source").body, { path: "/repo" });
  $("#source-use-origin").click(); await settle();
  assert.deepEqual(calls.at(-1).body, { path: null });
  assert.equal($("#source-local-field").hidden, true);
});

test("update check ignores duplicate clicks and disables restart until settled", async t => {
  let finish;
  const { $, calls } = await setup(t, { respond: c => c.path === "/api/updates/check" && finish !== undefined ? new Promise(resolve => { finish = resolve; }) : null });
  finish = true; $("#update-action").click(); $("#update-action").click(); await settle();
  assert.equal($("#restart-action").disabled, true);
  assert.equal(calls.filter(c => c.path === "/api/updates/check").length, 2);
  finish(null); await settle(); assert.equal($("#restart-action").disabled, false);
});

test("restart polls matching request then reloads with completion toast", async t => {
  const { $, advance, reloads, w } = await setup(t, { respond: (c, s) => {
    if (c.path !== "/api/server/restart") return null;
    s.status.restart = { can_restart: true, state: "success", request_id: "req-1" };
    return { payload: { ...s.status, restart_request_id: "req-1", restart: { can_restart: true, state: "queued", request_id: "req-1" } } };
  } });
  $("#restart-action").click(); await settle(); await advance(1500);
  assert.equal(reloads(), 1);
  assert.equal(JSON.parse(w.sessionStorage.getItem("element-mcp.completed")).restartId, "req-1");
});

test("reload completion notice is not repeated from status", async t => {
  const { $, w } = await setup(t, { notice: { at: 1_000_000, restartId: "req-1", text: "Сервер перезапущен" }, configure: s => { s.status.restart = { can_restart: true, state: "success", request_id: "req-1", updated_at: new Date(1_000_000).toISOString(), message: "MCP успешно перезапущен" }; } });
  assert.equal($("#toasts").children.length, 0);
  assert.equal(w.sessionStorage.getItem("element-mcp.completed"), null);
});

test("each rejected restart attempt shows its own three-second error toast", async t => {
  const { $, advance, calls } = await setup(t, { respond: c => c.path === "/api/server/restart" ? { ok: false, payload: { message: "Служба перезапуска недоступна" } } : null });
  for (let attempt = 0; attempt < 2; attempt++) {
    $("#restart-action").click(); await settle();
    assert.equal($("#toasts").children.length, 1);
    assert.equal($(".toast-message").textContent, "Служба перезапуска недоступна");
    assert.equal($("#restart-action").disabled, false);
    await advance(2999); assert.equal($("#toasts").children.length, 1);
    await advance(1); assert.equal($("#toasts").children.length, 0);
  }
  assert.equal(calls.filter(c => c.path === "/api/server/restart").length, 2);
});

test("unsupported restart stays disabled and startup action failure remains default-deny", async t => {
  const { $ } = await setup(t, { configure: s => { s.status.restart.can_restart = false; }, respond: c => c.path.includes("actions") ? { ok: false, payload: { message: "Недоступно" } } : null });
  assert.equal($("#restart-action").disabled, true);
  assert.equal($("#actions-enabled").disabled, true);
  assert.equal($("#actions-save").disabled, true);
});

test("manual local check applies available update, automatic initial check never applies", async t => {
  const { calls, advance, reloads, submit } = await setup(t, { configure: s => { s.status.updates = { state: "available", source: { kind: "local", label: "/repo" }, available_version: "0.24.0", can_apply: true }; }, respond: (c, s) => {
    if (c.path !== "/api/updates/apply") return null;
    s.status.updates.apply = { state: "success" };
    return { payload: { ...s.status, updates: { ...s.status.updates, apply: { state: "queued" } } } };
  } });
  assert.equal(calls.some(c => c.path === "/api/updates/apply"), false);
  await submit("#source-settings"); await advance(1500);
  assert.equal(calls.filter(c => c.path === "/api/updates/apply").length, 1);
  assert.equal(reloads(), 1);
});
