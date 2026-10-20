const token = document.querySelector('meta[name="element-mcp-token"]').content;
const version = document.querySelector("#server-version");
const source = document.querySelector("#update-source");
const title = document.querySelector("#update-title");
const message = document.querySelector("#update-message");
const action = document.querySelector("#update-action");
const actionLabel = action.querySelector(".button-label");
const restartAction = document.querySelector("#restart-action");
const restartMessage = document.querySelector("#restart-message");
const sourceSettings = document.querySelector("#source-settings");
const sourceInput = document.querySelector("#source-path");
const sourceFeedback = document.querySelector("#source-feedback");
const sourceError = document.querySelector("#source-error");
const sourceUseOrigin = document.querySelector("#source-use-origin");
const documentationSummary = document.querySelector("#documentation-summary");
const documentationSettings = document.querySelector("#documentation-settings");
const documentationPath = document.querySelector("#documentation-path");
const documentationHelp = document.querySelector("#documentation-help");
const documentationFeedback = document.querySelector("#documentation-feedback");
const documentationError = document.querySelector("#documentation-error");
const documentationActivate = document.querySelector("#documentation-activate");
const consoleSummary = document.querySelector("#console-summary");
const consoleSettings = document.querySelector("#console-settings");
const consoleEnabled = document.querySelector("#console-enabled");
const consoleFields = document.querySelector("#console-fields");
const consoleServer = document.querySelector("#console-server");
const consoleClientId = document.querySelector("#console-client-id");
const consoleClientSecret = document.querySelector("#console-client-secret");
const consoleSecretHelp = document.querySelector("#console-secret-help");
const consoleFeedback = document.querySelector("#console-feedback");
const consoleError = document.querySelector("#console-error");
const consoleSave = document.querySelector("#console-save");
const runtimeSummary = document.querySelector("#runtime-summary");
const runtimeSettings = document.querySelector("#runtime-settings");
const runtimeInstanceRoot = document.querySelector("#runtime-instance-root");
const runtimeManagerStatus = document.querySelector("#runtime-manager-status");
const runtimeFeedback = document.querySelector("#runtime-feedback");
const runtimeError = document.querySelector("#runtime-error");
const runtimeSave = document.querySelector("#runtime-save");
const actionsSummary = document.querySelector("#actions-summary");
const actionsSettings = document.querySelector("#actions-settings");
const actionsEnabled = document.querySelector("#actions-enabled");
const actionsFields = document.querySelector("#actions-fields");
const actionInputs = [...document.querySelectorAll(".action-selection input[type='checkbox']")];
const actionsUploadRoots = document.querySelector("#actions-upload-roots");
const actionsFeedback = document.querySelector("#actions-feedback");
const actionsError = document.querySelector("#actions-error");
const actionsSave = document.querySelector("#actions-save");

let latestStatus = null;
let initialVersion = null;
let sourceDirty = false;
let documentationStatus = null;
let documentationDirty = false;
let documentationBusy = false;
let consoleConfiguration = null;
let consoleBusy = false;
let runtimeBusy = false;
let actionsConfiguration = null;
let actionsBusy = false;
let restartBusy = false;
let updateBusy = false;
let lastRestartNotice = "";
let completedRestartId = null;
const sourceLocal = document.querySelector("#source-use-local");
const sourceSave = document.querySelector("#source-save");
const toastQueue = [];
const activeToasts = new Set();

function showToast(text, kind = "success") {
  if (!text) return;
  toastQueue.push({ text, kind, trigger: document.activeElement });
  drainToasts();
}

function drainToasts() {
  while (toastQueue.length && activeToasts.size < 3) {
    const { text, kind, trigger } = toastQueue.shift();
    const toast = document.querySelector("#toast-template").content.firstElementChild.cloneNode(true);
    const textNode = toast.querySelector(".toast-message");
    const close = toast.querySelector(".toast-close");
    toast.dataset.kind = kind;
    textNode.setAttribute("role", kind === "error" ? "alert" : "status");
    textNode.setAttribute("aria-live", kind === "error" ? "assertive" : "polite");
    let timer;
    const dismiss = () => {
      window.clearTimeout(timer);
      if (!activeToasts.delete(toast)) return;
      if (toast.contains(document.activeElement)) {
        const target = trigger?.isConnected && trigger.getClientRects().length && !trigger.disabled
          ? trigger : document.querySelector('[data-page][aria-current="page"]');
        target?.focus();
      }
      toast.remove();
      drainToasts();
    };
    close.addEventListener("click", dismiss);
    toast.addEventListener("keydown", event => { if (event.key === "Escape") dismiss(); });
    activeToasts.add(toast);
    document.querySelector("#toasts").append(toast);
    textNode.textContent = text;
    timer = window.setTimeout(dismiss, 3000);
  }
}

function feedback(element, value, state = "") {
  element.textContent = value;
  element.hidden = !value;
  if (value && !["loading", "pending"].includes(state)) showToast(value, state === "success" ? "success" : "error");
}

function selectPage(page) {
  if (!["connections", "documentation", "actions", "updates"].includes(page)) return;
  document.querySelectorAll("[data-screen]").forEach(element => { element.hidden = element.dataset.screen !== page; });
  document.querySelectorAll("[data-page]").forEach(button => {
    if (button.dataset.page === page) button.setAttribute("aria-current", "page");
    else button.removeAttribute("aria-current");
  });
  try { sessionStorage.setItem("element-mcp.page", page); } catch { /* Navigation still works without storage. */ }
}

document.querySelectorAll("[data-page]").forEach(button => button.addEventListener("click", () => selectPage(button.dataset.page)));
try { selectPage(sessionStorage.getItem("element-mcp.page")); } catch { /* Use the first section. */ }
document.querySelector("#endpoint").textContent = `${window.location.origin}/mcp`;

function serverState(label, state = "") {
  document.querySelector("#server-state-label").textContent = label;
  document.querySelector("#server-state").dataset.state = state;
}

function restartLabel(label, busy = false) {
  restartAction.setAttribute("aria-label", label);
  restartAction.dataset.tooltip = label;
  restartAction.setAttribute("aria-busy", String(busy));
}

function sleep(milliseconds) {
  return new Promise((resolve) => window.setTimeout(resolve, milliseconds));
}

async function api(path, options = {}) {
  const response = await fetch(path, {
    cache: "no-store",
    ...options,
    headers: {
      "X-Element-MCP-Token": token,
      ...(options.headers || {}),
    },
  });
  const payload = await response.json();
  if (!response.ok) {
    throw new Error(payload.message || `HTTP ${response.status}`);
  }
  return payload;
}

function sourceText(value) {
  if (value.kind === "local") return `Локальный Git · ${value.label}`;
  if (value.kind === "remote") return `Git remote · ${value.label}`;
  return value.label;
}

function showSourceError(value) {
  sourceError.textContent = value;
  sourceError.hidden = !value;
  sourceInput.setAttribute("aria-invalid", value ? "true" : "false");
  if (value) showToast(value, "error");
}

function showSourceFeedback(value, state = "") {
  feedback(sourceFeedback, value, state);
}

function showDocumentationError(value) {
  documentationError.textContent = value;
  documentationError.hidden = !value;
  documentationPath.setAttribute("aria-invalid", value ? "true" : "false");
  if (value) showToast(value, "error");
}

function showDocumentationFeedback(value, state = "") {
  feedback(documentationFeedback, value, state);
}

function setDocumentationBusy(value) {
  documentationBusy = value;
  const locked = documentationStatus?.path_change_supported === false;
  documentationPath.disabled = value || locked;
  documentationActivate.disabled = value || locked;
  documentationActivate.textContent = value ? "Проверяем…" : "Проверить и подключить";
}

function documentationSummaryText(payload) {
  if (payload.status === "ready") return "Подключена";
  if (payload.status === "invalid") return "Нужна настройка";
  return "Не подключена";
}

function renderDocumentationStatus(payload) {
  documentationStatus = payload;
  documentationSummary.textContent = documentationSummaryText(payload);
  documentationSummary.title = payload.path || "";
  documentationSummary.dataset.state = payload.status === "ready" ? "ready" : payload.status === "invalid" ? "error" : "";
  const fixedSetting =
    payload.path_source === "startup_argument"
      ? "параметром --corpus-path"
      : payload.path_source === "environment"
        ? "переменной ELEMENT_DOCS_PATH"
        : null;
  documentationHelp.textContent = fixedSetting ? `Путь задан ${fixedSetting}.` : "";
  documentationHelp.hidden = !fixedSetting;
  documentationPath.disabled = payload.path_change_supported === false;
  documentationActivate.disabled = payload.path_change_supported === false;
  if (!documentationDirty) documentationPath.value = payload.path || "";
  const aggregate = payload.aggregate;
  document.querySelector("#documentation-counts").hidden = !aggregate;
  document.querySelector("#documentation-documents").textContent = aggregate?.documents ?? "—";
  document.querySelector("#documentation-chunks").textContent = aggregate?.chunks ?? "—";
  if (payload.status === "invalid") showDocumentationError(payload.message || "Каталог документации недоступен.");
}

async function loadDocumentationStatus() {
  try {
    renderDocumentationStatus(await api("/api/documentation"));
  } catch (error) {
    documentationSummary.textContent = "Недоступна";
    showDocumentationError(error.message);
    setDocumentationBusy(false);
  }
}

async function activateDocumentation() {
  if (documentationBusy) return;
  if (documentationStatus?.path_change_supported === false) return;
  const path = documentationPath.value.trim();
  showDocumentationError("");
  if (!path) {
    showDocumentationError("Укажите полный путь к каталогу документации.");
    documentationPath.focus();
    return;
  }
  showDocumentationFeedback("Проверяем корпус и все поисковые индексы…", "loading");
  setDocumentationBusy(true);
  try {
    const payload = await api("/api/documentation/activate", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ path }),
    });
    documentationDirty = false;
    renderDocumentationStatus(payload);
    showDocumentationFeedback("Документация подключена", "success");
  } catch (error) {
    showDocumentationFeedback("");
    showDocumentationError(error.message);
    documentationPath.focus();
  } finally {
    setDocumentationBusy(false);
  }
}

function showConsoleError(value, invalidField = null) {
  consoleError.textContent = value;
  consoleError.hidden = !value;
  for (const input of [consoleServer, consoleClientId, consoleClientSecret]) {
    input.setAttribute("aria-invalid", input === invalidField ? "true" : "false");
  }
  if (value) showToast(value, "error");
}

function showConsoleFeedback(value, state = "") {
  feedback(consoleFeedback, value, state);
}

function setConsoleBusy(value, label = "") {
  consoleBusy = value;
  consoleFields.disabled = value || !consoleEnabled.checked;
  consoleEnabled.disabled = value;
  consoleServer.disabled = value;
  consoleClientId.disabled = value;
  consoleClientSecret.disabled = value;
  consoleSave.disabled = value || !consoleEnabled.checked;
  if (label) consoleSave.textContent = label;
}

function renderConsoleConfiguration(payload) {
  consoleConfiguration = payload;
  consoleEnabled.checked = payload.enabled;
  consoleSummary.textContent =
    payload.status === "invalid"
      ? "Нужна настройка"
      : payload.configured
        ? payload.enabled
          ? "Подключена"
          : "Отключена"
        : "Не настроена";
  consoleSummary.title = payload.server || "";
  consoleServer.value = payload.server || "";
  consoleClientId.value = payload.client_id || "";
  consoleClientSecret.value = "";
  resetSecretVisibility();
  consoleClientSecret.placeholder = payload.secret_present ? "Сохранён" : "";
  consoleSecretHelp.textContent =
    payload.credential_kind === "access_token"
      ? "Токен: для смены подключения нужны Client ID и Client Secret."
      : "";
  consoleSecretHelp.hidden = !consoleSecretHelp.textContent;
  consoleFields.disabled = !payload.enabled;
  consoleSave.textContent = payload.configured ? "Проверить и сохранить" : "Проверить и включить";
  if (payload.status === "invalid") showConsoleError(payload.message || "Проверьте настройки подключения.");
}

async function loadConsoleConfiguration() {
  try {
    renderConsoleConfiguration(await api("/api/console/configuration"));
  } catch (error) {
    consoleSummary.textContent = "Недоступна";
    showConsoleError(error.message);
  } finally {
    setConsoleBusy(false);
  }
}

async function saveConsoleConnection() {
  if (consoleBusy) return;
  showConsoleError("");
  showConsoleFeedback("Получаем токен и проверяем доступ к пространствам…", "loading");
  setConsoleBusy(true, "Проверяем…");
  try {
    const payload = await api("/api/console/configuration", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        enabled: true,
        server: consoleServer.value.trim(),
        client_id: consoleClientId.value.trim(),
        client_secret: consoleClientSecret.value || null,
      }),
    });
    renderConsoleConfiguration(payload);
    showConsoleFeedback(
      "Подключение проверено и сохранено",
      "success",
    );
  } catch (error) {
    consoleEnabled.checked = true;
    consoleFields.disabled = false;
    showConsoleFeedback("");
    let invalidField = null;
    if (!consoleServer.value.trim()) invalidField = consoleServer;
    else if (!consoleClientId.value.trim()) invalidField = consoleClientId;
    else if (!consoleClientSecret.value && !consoleConfiguration?.secret_present) invalidField = consoleClientSecret;
    showConsoleError(error.message, invalidField);
    if (invalidField) invalidField.focus();
  } finally {
    setConsoleBusy(false);
    consoleSave.textContent = consoleConfiguration?.configured
      ? "Проверить и сохранить"
      : "Проверить и включить";
  }
}

async function disableConsoleConnection() {
  if (consoleBusy) return;
  showConsoleError("");
  showConsoleFeedback("Отключаем удалённый сервер…", "loading");
  setConsoleBusy(true, "Проверить и сохранить");
  try {
    const payload = await api("/api/console/configuration", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ enabled: false }),
    });
    renderConsoleConfiguration(payload);
    showConsoleFeedback("Подключение отключено", "success");
  } catch (error) {
    consoleEnabled.checked = true;
    consoleFields.disabled = false;
    showConsoleFeedback("");
    showConsoleError(error.message);
  } finally {
    setConsoleBusy(false);
  }
}

function showRuntimeError(value, invalidField = null) {
  runtimeError.textContent = value;
  runtimeError.hidden = !value;
  runtimeInstanceRoot.setAttribute("aria-invalid", runtimeInstanceRoot === invalidField ? "true" : "false");
  if (value) showToast(value, "error");
}

function showRuntimeFeedback(value, state = "") {
  feedback(runtimeFeedback, value, state);
}

function setRuntimeBusy(value) {
  runtimeBusy = value;
  for (const input of [runtimeInstanceRoot, runtimeSave]) {
    input.disabled = value;
  }
  runtimeSave.textContent = value ? "Сохраняем…" : "Сохранить";
}

function renderRuntimeConfiguration(payload) {
  const manager = payload.application_manager || {};
  runtimeSummary.textContent =
    payload.status === "configured" ? "Подключён" : "Не настроен";
  runtimeSummary.dataset.state = payload.status === "configured" ? "ready" : "";
  runtimeSummary.title = payload.instance_root || "";
  runtimeInstanceRoot.value = payload.instance_root || "";
  if (manager.status === "configured") {
    runtimeManagerStatus.textContent = "Журнал событий подключён";
  } else if (manager.message) {
    runtimeManagerStatus.textContent = `Журнал событий: ${manager.message}`;
  } else {
    runtimeManagerStatus.textContent = "";
  }
}

async function loadRuntimeConfiguration() {
  try {
    renderRuntimeConfiguration(await api("/api/runtime/configuration"));
  } catch (error) {
    runtimeSummary.textContent = "Недоступен";
    showRuntimeError(error.message);
  } finally {
    setRuntimeBusy(false);
  }
}

async function saveRuntimeConfiguration() {
  if (runtimeBusy) return;
  showRuntimeError("");
  const invalidField = runtimeInstanceRoot.value.trim() ? null : runtimeInstanceRoot;
  if (invalidField) {
    showRuntimeError("Укажите каталог экземпляра сервера.", invalidField);
    invalidField.focus();
    return;
  }
  showRuntimeFeedback("Проверяем каталог экземпляра и сохраняем настройки…", "loading");
  setRuntimeBusy(true);
  try {
    const payload = await api("/api/runtime/configuration", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        instance_root: runtimeInstanceRoot.value.trim(),
      }),
    });
    renderRuntimeConfiguration(payload);
    showRuntimeFeedback("Настройки сервера сохранены", "success");
  } catch (error) {
    showRuntimeFeedback("");
    showRuntimeError(error.message);
  } finally {
    setRuntimeBusy(false);
  }
}

function showActionsError(value, invalidField = null) {
  actionsError.textContent = value;
  actionsError.hidden = !value;
  for (const input of [actionsUploadRoots]) {
    input.setAttribute("aria-invalid", input === invalidField ? "true" : "false");
  }
  if (value) showToast(value, "error");
}

function showActionsFeedback(value, state = "") {
  feedback(actionsFeedback, value, state);
}

function setActionsBusy(value, label = "") {
  actionsBusy = value;
  actionsFields.disabled = value || !actionsEnabled.checked;
  for (const input of [
    actionsEnabled,
    ...actionInputs,
    actionsUploadRoots,
    actionsSave,
  ]) {
    input.disabled = value;
  }
  actionsSave.disabled = value || !actionsEnabled.checked || !actionsConfiguration;
  actionsEnabled.disabled = value || !actionsConfiguration;
  if (label) actionsSave.textContent = label;
}

function parseLines(value) {
  return [...new Set(value.split(/[\n,]+/).map((item) => item.trim()).filter(Boolean))];
}

function renderActionsConfiguration(payload) {
  actionsConfiguration = payload;
  const allowed = new Set(payload.allowed_actions || []);
  actionsEnabled.checked = Boolean(payload.enabled);
  for (const input of actionInputs) input.checked = allowed.has(input.value);
  actionsUploadRoots.value = (payload.upload_roots || []).join("\n");
  actionsFields.disabled = !payload.enabled;
  actionsSave.disabled = !payload.enabled;
  actionsSummary.textContent =
    payload.status === "invalid"
      ? "Нужна настройка"
      : payload.enabled
        ? "Включены"
        : "Выключены";
  if (payload.migration_required) {
    showActionsFeedback(
      "После обновления нужно заново включить действия агента.",
    );
  } else if (payload.status === "invalid") {
    showActionsError(payload.message || "Проверьте настройки действий агента.");
  }
}

async function loadActionsConfiguration() {
  try {
    renderActionsConfiguration(await api("/api/actions/configuration"));
  } catch (error) {
    actionsSummary.textContent = "Недоступны";
    showActionsError(error.message);
  } finally {
    setActionsBusy(false);
  }
}

function validateActionsForm() {
  const selected = actionInputs.filter((input) => input.checked).map((input) => input.value);
  const uploadRoots = parseLines(actionsUploadRoots.value);
  if (!selected.length) return ["Выберите хотя бы одну операцию.", null];
  if (
    selected.includes("upload_project_assembly") &&
    !uploadRoots.length
  ) {
    return ["Для загрузки укажите каталог со сборками.", actionsUploadRoots];
  }
  return [null, null];
}

async function saveActionsConfiguration(enabled = true) {
  if (actionsBusy) return;
  showActionsError("");
  if (enabled) {
    const [message, field] = validateActionsForm();
    if (message) {
      showActionsError(message, field);
      field?.focus();
      return;
    }
  }
  showActionsFeedback(
    enabled ? "Проверяем каталоги и сохраняем разрешения…" : "Запрещаем действия агента…",
    "loading",
  );
  setActionsBusy(true, enabled ? "Сохраняем…" : "Выключаем…");
  try {
    const payload = await api("/api/actions/configuration", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        enabled,
        allowed_actions: actionInputs.filter((input) => input.checked).map((input) => input.value),
        upload_roots: parseLines(actionsUploadRoots.value),
        max_upload_bytes: actionsConfiguration?.max_upload_bytes || 512 * 1024 * 1024,
        approval_ttl_seconds: actionsConfiguration?.approval_ttl_seconds || 300,
      }),
    });
    renderActionsConfiguration(payload);
    showActionsFeedback(
      enabled
        ? "Разрешения сохранены"
        : "Действия агента выключены",
      "success",
    );
  } catch (error) {
    actionsEnabled.checked = Boolean(actionsConfiguration?.enabled);
    actionsFields.disabled = !actionsEnabled.checked;
    actionsSave.disabled = !actionsEnabled.checked;
    showActionsFeedback("");
    showActionsError(error.message);
  } finally {
    setActionsBusy(false);
    actionsSave.textContent = "Сохранить";
  }
}

function setSourceEditor(updateSource) {
  if (!sourceDirty) {
    sourceInput.value = updateSource.kind === "local" ? updateSource.label : "";
    setSourceMode(updateSource.kind === "local" ? "local" : "remote");
  }
}

function setSourceMode(mode) {
  sourceLocal.setAttribute("aria-pressed", String(mode === "local"));
  sourceUseOrigin.setAttribute("aria-pressed", String(mode === "remote"));
  document.querySelector("#source-local-field").hidden = mode !== "local";
  document.querySelector("#source-remote-field").hidden = mode === "local";
  document.querySelector("#source-save-area").hidden = mode !== "local";
}

function setUpdateBusy(value) {
  updateBusy = value;
  for (const control of [sourceInput, sourceLocal, sourceUseOrigin, sourceSave]) control.disabled = value;
  action.setAttribute("aria-busy", String(value));
  if (value) { action.disabled = true; restartAction.disabled = true; }
}

function markSourceDirty() {
  sourceDirty = true;
  showSourceError("");
  showSourceFeedback("");
  actionLabel.textContent = "Сохранить и проверить";
  action.disabled = false;
  action.dataset.action = "check";
}

function showRestartMessage(value, state = "") {
  restartMessage.textContent = value;
  restartMessage.hidden = !value;
  restartMessage.dataset.state = state;
  if (value && ["success", "error"].includes(state) && lastRestartNotice !== `${state}:${value}`) {
    lastRestartNotice = `${state}:${value}`;
    showToast(value, state);
  }
}

function renderRestart(payload, updateInProgress) {
  const restart = payload.restart || {};
  const restartInProgress = ["queued", "restarting"].includes(restart.state);
  const restartUpdatedAt = Date.parse(restart.updated_at || "");
  const recentlyRestarted = Number.isFinite(restartUpdatedAt) && Date.now() - restartUpdatedAt < 60_000;
  restartLabel(restartInProgress ? "Перезапускаем…" : restart.can_restart ? "Перезапустить сервер" : "Перезапуск доступен на Windows Server", restartInProgress);
  restartAction.disabled = restartBusy || updateBusy || updateInProgress || restartInProgress || !restart.can_restart;

  if (restartInProgress) {
    showRestartMessage(restart.message || "MCP перезапускается…");
  } else if (restart.state === "error") {
    showRestartMessage(restart.message || "Не удалось перезапустить MCP.", "error");
  } else if (restart.state === "success" && recentlyRestarted && restart.request_id !== completedRestartId) {
    showRestartMessage(restart.message || "Сервер перезапущен", "success");
  } else if (!restart.can_restart) {
    showRestartMessage("Перезапуск из интерфейса доступен для установки Windows Server.");
  } else {
    showRestartMessage("");
  }
  return restartInProgress;
}

function render(payload) {
  latestStatus = payload;
  initialVersion ||= payload.server.version;
  version.textContent = payload.server.version;
  document.querySelector("#footer-version").textContent = payload.server.version;
  serverState("Работает", "running");
  document.querySelector("#latest-version").textContent = payload.updates.available_version ||
    (payload.updates.state === "current" ? payload.server.version : payload.updates.state === "unavailable" ? "Недоступна" : "Не проверена");
  source.textContent = sourceText(payload.updates.source);
  source.title = payload.updates.source.label;
  setSourceEditor(payload.updates.source);

  const applyState = payload.updates.apply?.state;
  const updateInProgress = ["queued", "checking", "applying"].includes(applyState);
  for (const control of [sourceInput, sourceLocal, sourceUseOrigin, sourceSave]) control.disabled = updateBusy || updateInProgress || restartBusy;
  const restartInProgress = renderRestart(payload, updateInProgress);
  if (updateInProgress) {
    title.textContent = "Устанавливаем обновление";
    message.textContent = payload.updates.apply.message || "Сервер скоро перезапустится.";
    actionLabel.textContent = "Обновляем…";
    action.disabled = true;
    action.dataset.action = "wait";
    return;
  }

  if (applyState === "error") {
    title.textContent = "Обновление не выполнено";
    message.textContent = payload.updates.apply.message;
  } else {
    title.textContent = {
      idle: "Обновления не проверялись",
      checking: "Проверяем обновления",
      current: "Установлена актуальная версия",
      available: "Доступно обновление",
      unavailable: "Проверка недоступна",
    }[payload.updates.state] || "Обновления";
    message.textContent = payload.updates.message;
  }

  if (payload.updates.state === "checking") {
    actionLabel.textContent = "Проверяем…";
    action.disabled = true;
    action.dataset.action = "check";
  } else if (payload.updates.state === "available" && payload.updates.can_apply) {
    actionLabel.textContent = `Обновить до ${payload.updates.available_version}`;
    action.disabled = false;
    action.dataset.action = "apply";
  } else {
    actionLabel.textContent = "Проверить обновления";
    action.disabled = false;
    action.dataset.action = "check";
  }

  if (sourceDirty && payload.updates.state !== "checking") {
    actionLabel.textContent = "Сохранить и проверить";
    action.disabled = false;
    action.dataset.action = "check";
  }
  if (restartInProgress || restartBusy || updateBusy) action.disabled = true;
}

async function saveSourceIfNeeded() {
  if (!sourceDirty) return;

  showSourceError("");
  try {
    const path = sourceInput.value.trim();
    if (!path) throw new Error("Укажите полный путь к репозиторию.");
    const payload = await api("/api/updates/source", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ path }),
    });
    sourceDirty = false;
    render(payload);
    showSourceFeedback("");
  } catch (error) {
    showSourceError(error.message);
    sourceInput.focus();
    throw error;
  }
}

async function switchToOrigin() {
  if (updateBusy || restartBusy) return;
  if (latestStatus?.updates.source.kind !== "local" && !sourceDirty) { setSourceMode("remote"); return; }
  setUpdateBusy(true);
  showSourceError("");
  showSourceFeedback("Сохраняем удалённый origin…", "loading");

  try {
    const payload = await api("/api/updates/source", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ path: null }),
    });
    sourceDirty = false;
    sourceInput.value = "";
    render(payload);
    showSourceFeedback("");
    setSourceMode("remote");
    title.textContent = "Источник обновлений изменён";
    message.textContent = "Следующая проверка будет выполнена через удалённый origin.";
    actionLabel.textContent = "Проверить обновления";
    action.disabled = false;
    action.dataset.action = "check";
    showToast("Источник обновлений сохранён");
  } catch (error) {
    showSourceFeedback("");
    showSourceError(`Не удалось переключиться на origin: ${error.message}`);
  } finally {
    setUpdateBusy(false);
    if (latestStatus) render(latestStatus);
  }
}

async function checkUpdates(applyLocalUpdate = false) {
  if (updateBusy || restartBusy) return;
  setUpdateBusy(true);
  let shouldApply = false;
  action.disabled = true;
  actionLabel.textContent = "Проверяем…";
  title.textContent = "Проверяем обновления";
  message.textContent = "Это может занять несколько секунд.";
  try {
    await saveSourceIfNeeded();
    const payload = await api("/api/updates/check", { method: "POST" });
    render(payload);
    showSourceFeedback("");
    shouldApply = Boolean(
      applyLocalUpdate &&
      payload.updates.source.kind === "local" &&
      payload.updates.state === "available" &&
      payload.updates.can_apply
    );
    if (!shouldApply) showToast(
      payload.updates.state === "current" ? "Установлена последняя версия" :
        payload.updates.state === "available" ? `Доступна версия ${payload.updates.available_version}` :
          payload.updates.message || "Проверка обновлений недоступна",
      payload.updates.state === "unavailable" ? "error" : "success",
    );
  } catch (error) {
    showSourceFeedback("");
    title.textContent = sourceError.hidden ? "Проверка недоступна" : "Проверьте каталог";
    message.textContent = error.message;
    if (sourceError.hidden) showToast(error.message, "error");
    action.disabled = false;
    actionLabel.textContent = "Проверить обновления";
    action.dataset.action = "check";
  } finally {
    setUpdateBusy(false);
    if (latestStatus) render(latestStatus);
    else action.disabled = false;
  }
  if (shouldApply) await applyUpdate();
}

async function waitForRestart(restartRequestId = null) {
  const deadline = Date.now() + 180_000;
  await sleep(1_500);
  while (Date.now() < deadline) {
    let payload;
    try {
      payload = await api(`/api/status?t=${Date.now()}`);
    } catch {
      serverState("Перезапуск…");
      if (restartRequestId) {
        showRestartMessage("MCP перезапускается. Страница подключится снова автоматически.");
      } else {
        title.textContent = "Перезапускаем MCP";
        message.textContent = "Страница подключится снова автоматически.";
      }
      await sleep(1_500);
      continue;
    }

    const requestedRestart =
      restartRequestId && payload.restart?.request_id === restartRequestId ? payload.restart : null;
    if (requestedRestart?.state === "error") throw new Error(requestedRestart.message);
    if (!restartRequestId && payload.updates.apply?.state === "error") throw new Error(payload.updates.apply.message);
    if (
      requestedRestart?.state === "success" ||
      (!restartRequestId &&
        (payload.server.version !== initialVersion || payload.updates.apply?.state === "success"))
    ) {
      try { sessionStorage.setItem("element-mcp.completed", JSON.stringify({ at: Date.now(), restartId: restartRequestId, text: restartRequestId ? "Сервер перезапущен" : "Обновление установлено" })); } catch { /* Reload is still required for a fresh CSRF token. */ }
      window.location.reload();
      return;
    }
    render(payload);
    await sleep(1_500);
  }
  throw new Error("Перезапуск занимает больше времени. Обновите страницу или проверьте журнал службы.");
}

async function applyUpdate() {
  if (updateBusy || restartBusy) return;
  setUpdateBusy(true);
  action.disabled = true;
  actionLabel.textContent = "Запускаем обновление…";
  try {
    render(await api("/api/updates/apply", { method: "POST" }));
    await waitForRestart();
  } catch (error) {
    title.textContent = "Обновление не запущено";
    message.textContent = error.message;
    showToast(error.message, "error");
    action.disabled = false;
    actionLabel.textContent = "Проверить обновления";
    action.dataset.action = "check";
  } finally {
    setUpdateBusy(false);
    if (latestStatus) render(latestStatus);
  }
}

async function restartServer() {
  if (restartBusy || updateBusy || restartAction.disabled) return;
  lastRestartNotice = "";
  restartBusy = true;
  restartAction.disabled = true;
  restartLabel("Перезапуск…", true);
  for (const control of [sourceInput, sourceLocal, sourceUseOrigin, sourceSave]) control.disabled = true;
  action.disabled = true;
  showRestartMessage("Текущие подключения MCP будут кратко прерваны.");
  try {
    const payload = await api("/api/server/restart", { method: "POST" });
    render(payload);
    await waitForRestart(payload.restart_request_id);
  } catch (error) {
    restartBusy = false;
    showRestartMessage(error.message, "error");
    restartLabel("Перезапустить сервер");
    restartAction.disabled = !latestStatus?.restart?.can_restart;
    action.disabled = false;
    if (latestStatus) render(latestStatus);
  }
}

action.addEventListener("click", () => {
  if (action.dataset.action === "apply") applyUpdate();
  else checkUpdates(true);
});

restartAction.addEventListener("click", restartServer);

sourceLocal.addEventListener("click", () => {
  if (updateBusy || restartBusy) return;
  setSourceMode("local");
  if (latestStatus?.updates.source.kind !== "local") markSourceDirty();
  sourceInput.focus();
});

sourceInput.addEventListener("input", markSourceDirty);

sourceUseOrigin.addEventListener("click", switchToOrigin);

sourceSettings.addEventListener("submit", (event) => {
  event.preventDefault();
  checkUpdates(true);
});

documentationPath.addEventListener("input", () => {
  documentationDirty = true;
  showDocumentationError("");
  showDocumentationFeedback("");
});

documentationSettings.addEventListener("submit", (event) => {
  event.preventDefault();
  activateDocumentation();
});

consoleEnabled.addEventListener("change", () => {
  if (consoleEnabled.checked) {
    consoleFields.disabled = false;
    consoleSave.disabled = false;
    if (
      consoleConfiguration?.configured &&
      consoleConfiguration?.credential_kind === "client_credentials"
    ) {
      saveConsoleConnection();
    } else {
      consoleServer.focus();
    }
  } else {
    consoleFields.disabled = true;
    disableConsoleConnection();
  }
});

consoleSettings.addEventListener("submit", (event) => {
  event.preventDefault();
  saveConsoleConnection();
});

runtimeSettings.addEventListener("submit", (event) => {
  event.preventDefault();
  saveRuntimeConfiguration();
});

actionsEnabled.addEventListener("change", () => {
  if (actionsEnabled.checked) {
    actionsFields.disabled = false;
    actionsSave.disabled = false;
    showActionsError("");
    actionsSummary.textContent = "Не сохранено";
    actionInputs[0]?.focus();
  } else {
    actionsFields.disabled = true;
    actionsSave.disabled = true;
    saveActionsConfiguration(false);
  }
});

actionsSettings.addEventListener("submit", (event) => {
  event.preventDefault();
  saveActionsConfiguration(true);
});

function resetSecretVisibility() {
  consoleClientSecret.type = "password";
  const button = document.querySelector("#secret-visibility");
  button.setAttribute("aria-label", "Показать Client Secret");
  button.dataset.tooltip = "Показать Client Secret";
}

document.querySelector("#secret-visibility").addEventListener("click", event => {
  const show = consoleClientSecret.type === "password";
  consoleClientSecret.type = show ? "text" : "password";
  const label = show ? "Скрыть Client Secret" : "Показать Client Secret";
  event.currentTarget.setAttribute("aria-label", label);
  event.currentTarget.dataset.tooltip = label;
});

document.querySelectorAll("form").forEach(form => form.addEventListener("input", event => {
  const input = event.target;
  if (input.getAttribute("aria-invalid") === "true") input.removeAttribute("aria-invalid");
}));

try {
  const notice = JSON.parse(sessionStorage.getItem("element-mcp.completed") || "null");
  sessionStorage.removeItem("element-mcp.completed");
  if (notice && Date.now() - notice.at < 60_000) {
    showToast(notice.text);
    completedRestartId = notice.restartId || null;
  }
} catch { /* A previous restart notice is optional. */ }

loadDocumentationStatus();
loadConsoleConfiguration();
loadRuntimeConfiguration();
loadActionsConfiguration();

api("/api/status")
  .then((payload) => {
    render(payload);
    return checkUpdates(false);
  })
  .catch((error) => {
    serverState("Недоступен", "error");
    title.textContent = "Не удалось получить состояние";
    message.textContent = error.message;
    showToast(error.message, "error");
    action.disabled = false;
    actionLabel.textContent = "Повторить";
    action.dataset.action = "check";
  });
