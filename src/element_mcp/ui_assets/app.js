const token = document.querySelector('meta[name="element-mcp-token"]').content;
const version = document.querySelector("#server-version");
const source = document.querySelector("#update-source");
const title = document.querySelector("#update-title");
const message = document.querySelector("#update-message");
const action = document.querySelector("#update-action");
const actionLabel = action.querySelector(".button-label");
const sourceToggle = document.querySelector("#source-toggle");
const sourceSettings = document.querySelector("#source-settings");
const sourceInput = document.querySelector("#source-path");
const sourceFeedback = document.querySelector("#source-feedback");
const sourceError = document.querySelector("#source-error");
const sourceUseOrigin = document.querySelector("#source-use-origin");
const documentationSummary = document.querySelector("#documentation-summary");
const documentationToggle = document.querySelector("#documentation-toggle");
const documentationSettings = document.querySelector("#documentation-settings");
const documentationPath = document.querySelector("#documentation-path");
const documentationHelp = document.querySelector("#documentation-help");
const documentationFeedback = document.querySelector("#documentation-feedback");
const documentationError = document.querySelector("#documentation-error");
const documentationActivate = document.querySelector("#documentation-activate");
const consoleSummary = document.querySelector("#console-summary");
const consoleToggle = document.querySelector("#console-toggle");
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
const runtimeToggle = document.querySelector("#runtime-toggle");
const runtimeSettings = document.querySelector("#runtime-settings");
const runtimeInstanceRoot = document.querySelector("#runtime-instance-root");
const runtimeManagerEnabled = document.querySelector("#runtime-manager-enabled");
const runtimeManagerFields = document.querySelector("#runtime-manager-fields");
const runtimeManagerServer = document.querySelector("#runtime-manager-server");
const runtimeManagerUsername = document.querySelector("#runtime-manager-username");
const runtimeManagerPassword = document.querySelector("#runtime-manager-password");
const runtimeManagerVerifyTls = document.querySelector("#runtime-manager-verify-tls");
const runtimePasswordHelp = document.querySelector("#runtime-password-help");
const runtimeFeedback = document.querySelector("#runtime-feedback");
const runtimeError = document.querySelector("#runtime-error");
const runtimeSave = document.querySelector("#runtime-save");

let latestStatus = null;
let initialVersion = null;
let sourceDirty = false;
let documentationStatus = null;
let documentationDirty = false;
let documentationBusy = false;
let consoleConfiguration = null;
let consoleBusy = false;
let runtimeConfiguration = null;
let runtimeBusy = false;

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
}

function showSourceFeedback(value, state = "") {
  sourceFeedback.textContent = value;
  sourceFeedback.hidden = !value;
  sourceFeedback.dataset.state = state;
}

function showDocumentationError(value) {
  documentationError.textContent = value;
  documentationError.hidden = !value;
  documentationPath.setAttribute("aria-invalid", value ? "true" : "false");
}

function showDocumentationFeedback(value, state = "") {
  documentationFeedback.textContent = value;
  documentationFeedback.hidden = !value;
  documentationFeedback.dataset.state = state;
}

function setDocumentationBusy(value) {
  documentationBusy = value;
  const locked = documentationStatus?.path_change_supported === false;
  documentationPath.disabled = value || locked;
  documentationActivate.disabled = value || locked;
  documentationActivate.textContent = value ? "Проверяем…" : "Проверить и подключить";
}

function documentationSummaryText(payload) {
  if (payload.status === "ready") return `Подключена · ${payload.path}`;
  if (payload.status === "invalid") return `Ошибка · ${payload.path}`;
  return "Не подключена";
}

function renderDocumentationStatus(payload) {
  documentationStatus = payload;
  documentationSummary.textContent = documentationSummaryText(payload);
  documentationSummary.title = payload.path || "";
  documentationToggle.textContent = documentationSettings.hidden
    ? payload.path
      ? "Изменить"
      : "Подключить"
    : "Скрыть";
  const fixedSetting =
    payload.path_source === "startup_argument"
      ? "параметром --corpus-path"
      : payload.path_source === "environment"
        ? "переменной ELEMENT_DOCS_PATH"
        : null;
  documentationHelp.innerHTML = fixedSetting
    ? `Путь задан ${fixedSetting}. Измените настройку запуска и перезапустите MCP.`
    : "Укажите корень готового корпуса с <code>manifest.json</code> и каталогами " +
      "<code>docs-lang</code>, <code>docs-console</code>, <code>docs-server</code>.";
  documentationPath.disabled = payload.path_change_supported === false;
  documentationActivate.disabled = payload.path_change_supported === false;
  if (!documentationDirty) documentationPath.value = payload.path || "";
}

async function loadDocumentationStatus() {
  try {
    renderDocumentationStatus(await api("/api/documentation"));
  } catch (error) {
    documentationSummary.textContent = "Недоступна";
    showDocumentationError(error.message);
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
    const documents = payload.aggregate?.documents;
    const chunks = payload.aggregate?.chunks;
    const details =
      Number.isInteger(documents) && Number.isInteger(chunks)
        ? ` Документов: ${documents}, фрагментов: ${chunks}.`
        : "";
    showDocumentationFeedback(`Корпус проверен и подключён.${details}`, "success");
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
}

function showConsoleFeedback(value, state = "") {
  consoleFeedback.textContent = value;
  consoleFeedback.hidden = !value;
  consoleFeedback.dataset.state = state;
}

function setConsoleBusy(value, label = "") {
  consoleBusy = value;
  consoleEnabled.disabled = value;
  consoleServer.disabled = value;
  consoleClientId.disabled = value;
  consoleClientSecret.disabled = value;
  consoleSave.disabled = value;
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
          ? payload.server
          : "Отключена"
        : "Не настроена";
  consoleSummary.title = payload.server || "";
  consoleServer.value = payload.server || "";
  consoleClientId.value = payload.client_id || "";
  consoleClientSecret.value = "";
  consoleClientSecret.placeholder = payload.secret_present ? "Сохранён — оставьте пустым" : "";
  consoleSecretHelp.textContent =
    payload.credential_kind === "access_token"
      ? "Текущая конфигурация использует готовый токен. Введите Client ID и Client Secret, чтобы перейти на автоматическое получение bearer."
      : payload.secret_present
        ? "Оставьте поле пустым, чтобы использовать сохранённый секрет. Он не возвращается в браузер или агенту."
        : "Секрет не возвращается в браузер или агенту после сохранения.";
  consoleFields.hidden = !payload.enabled;
  consoleSave.textContent = payload.configured ? "Проверить и сохранить" : "Проверить и включить";
  consoleToggle.textContent = consoleSettings.hidden ? "Настроить" : "Скрыть";
}

async function loadConsoleConfiguration() {
  try {
    renderConsoleConfiguration(await api("/api/console/configuration"));
  } catch (error) {
    consoleSummary.textContent = "Недоступна";
    showConsoleError(error.message);
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
      `Подключение работает. Доступно пространств: ${payload.spaces_count}.`,
      "success",
    );
  } catch (error) {
    consoleEnabled.checked = Boolean(consoleConfiguration?.enabled);
    consoleFields.hidden = false;
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
    showConsoleFeedback("Удалённый сервер отключён. Настройки сохранены.", "success");
  } catch (error) {
    consoleEnabled.checked = true;
    consoleFields.hidden = false;
    showConsoleFeedback("");
    showConsoleError(error.message);
  } finally {
    setConsoleBusy(false);
  }
}

function showRuntimeError(value, invalidField = null) {
  runtimeError.textContent = value;
  runtimeError.hidden = !value;
  for (const input of [
    runtimeInstanceRoot,
    runtimeManagerServer,
    runtimeManagerUsername,
    runtimeManagerPassword,
  ]) {
    input.setAttribute("aria-invalid", input === invalidField ? "true" : "false");
  }
}

function showRuntimeFeedback(value, state = "") {
  runtimeFeedback.textContent = value;
  runtimeFeedback.hidden = !value;
  runtimeFeedback.dataset.state = state;
}

function setRuntimeBusy(value) {
  runtimeBusy = value;
  for (const input of [
    runtimeInstanceRoot,
    runtimeManagerEnabled,
    runtimeManagerServer,
    runtimeManagerUsername,
    runtimeManagerPassword,
    runtimeManagerVerifyTls,
    runtimeSave,
  ]) {
    input.disabled = value;
  }
  runtimeSave.textContent = value ? "Сохраняем…" : "Сохранить настройки";
}

function renderRuntimeConfiguration(payload) {
  runtimeConfiguration = payload;
  const manager = payload.application_manager || {};
  runtimeSummary.textContent =
    payload.status === "configured" ? payload.instance_root : "Не настроен";
  runtimeSummary.title = payload.instance_root || "";
  runtimeInstanceRoot.value = payload.instance_root || "";
  runtimeManagerEnabled.checked = Boolean(manager.enabled);
  runtimeManagerFields.hidden = !manager.enabled;
  runtimeManagerServer.value = manager.server || "";
  runtimeManagerUsername.value = manager.username || "";
  runtimeManagerPassword.value = "";
  runtimeManagerPassword.placeholder = manager.password_present
    ? "Сохранён — оставьте пустым"
    : "";
  runtimePasswordHelp.textContent = manager.password_present
    ? "Оставьте поле пустым, чтобы использовать сохранённый пароль. Он не возвращается браузеру или агенту."
    : "На Windows пароль защищается средствами ОС. Он не возвращается браузеру или агенту.";
  runtimeManagerVerifyTls.checked = manager.verify_tls !== false;
  runtimeToggle.textContent = runtimeSettings.hidden ? "Настроить" : "Скрыть";
}

async function loadRuntimeConfiguration() {
  try {
    renderRuntimeConfiguration(await api("/api/runtime/configuration"));
  } catch (error) {
    runtimeSummary.textContent = "Недоступен";
    showRuntimeError(error.message);
  }
}

async function saveRuntimeConfiguration() {
  if (runtimeBusy) return;
  showRuntimeError("");
  const managerEnabled = runtimeManagerEnabled.checked;
  let invalidField = null;
  if (!runtimeInstanceRoot.value.trim()) invalidField = runtimeInstanceRoot;
  else if (managerEnabled && !runtimeManagerServer.value.trim()) invalidField = runtimeManagerServer;
  else if (managerEnabled && !runtimeManagerUsername.value.trim()) invalidField = runtimeManagerUsername;
  else if (
    managerEnabled &&
    !runtimeManagerPassword.value &&
    !runtimeConfiguration?.application_manager?.password_present
  ) {
    invalidField = runtimeManagerPassword;
  }
  if (invalidField) {
    showRuntimeError("Заполните обязательные поля настройки.", invalidField);
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
        application_manager_enabled: managerEnabled,
        server: runtimeManagerServer.value.trim() || null,
        username: runtimeManagerUsername.value.trim() || null,
        password: runtimeManagerPassword.value || null,
        api_version: "auto",
        verify_tls: runtimeManagerVerifyTls.checked,
      }),
    });
    renderRuntimeConfiguration(payload);
    showRuntimeFeedback(
      managerEnabled
        ? "Диагностика и доступ к журналу событий настроены."
        : "Локальная диагностика сервера настроена.",
      "success",
    );
  } catch (error) {
    showRuntimeFeedback("");
    showRuntimeError(error.message);
  } finally {
    setRuntimeBusy(false);
  }
}

function setSourceEditor(updateSource) {
  if (!sourceDirty) {
    sourceInput.value = updateSource.kind === "local" ? updateSource.label : "";
  }
  sourceUseOrigin.hidden = updateSource.kind !== "local" && !sourceDirty;
}

function markSourceDirty() {
  sourceDirty = true;
  showSourceError("");
  showSourceFeedback("Путь изменён. Сохраните его вместе с проверкой обновлений.", "pending");
  sourceUseOrigin.hidden = false;
  actionLabel.textContent = "Сохранить и проверить";
  action.disabled = false;
  action.dataset.action = "check";
}

function render(payload) {
  latestStatus = payload;
  initialVersion ||= payload.server.version;
  version.textContent = payload.server.version;
  source.textContent = sourceText(payload.updates.source);
  source.title = payload.updates.source.label;
  setSourceEditor(payload.updates.source);

  const applyState = payload.updates.apply?.state;
  if (["queued", "checking", "applying"].includes(applyState)) {
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
}

async function saveSourceIfNeeded() {
  if (!sourceDirty) return;

  showSourceError("");
  try {
    const path = sourceInput.value.trim() || null;
    const payload = await api("/api/updates/source", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ path }),
    });
    sourceDirty = false;
    render(payload);
    showSourceFeedback("Локальный источник сохранён. Проверяем обновления…", "success");
  } catch (error) {
    showSourceError(error.message);
    sourceInput.focus();
    throw error;
  }
}

async function switchToOrigin() {
  const originalLabel = sourceUseOrigin.textContent;
  sourceUseOrigin.disabled = true;
  sourceInput.disabled = true;
  sourceUseOrigin.textContent = "Переключаем…";
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
    sourceSettings.hidden = true;
    sourceToggle.setAttribute("aria-expanded", "false");
    sourceToggle.textContent = "Изменить";
    title.textContent = "Источник обновлений изменён";
    message.textContent = "Следующая проверка будет выполнена через удалённый origin.";
    actionLabel.textContent = "Проверить обновления";
    action.disabled = false;
    action.dataset.action = "check";
    sourceToggle.focus();
  } catch (error) {
    showSourceFeedback("");
    showSourceError(`Не удалось переключиться на origin: ${error.message}`);
  } finally {
    sourceInput.disabled = false;
    sourceUseOrigin.disabled = false;
    sourceUseOrigin.textContent = originalLabel;
  }
}

async function checkUpdates(applyLocalUpdate = false) {
  action.disabled = true;
  actionLabel.textContent = "Проверяем…";
  title.textContent = "Проверяем обновления";
  message.textContent = "Это может занять несколько секунд.";
  try {
    await saveSourceIfNeeded();
    const payload = await api("/api/updates/check", { method: "POST" });
    render(payload);
    showSourceFeedback("");
    if (
      applyLocalUpdate &&
      payload.updates.source.kind === "local" &&
      payload.updates.state === "available" &&
      payload.updates.can_apply
    ) {
      await applyUpdate();
    }
  } catch (error) {
    showSourceFeedback("");
    title.textContent = sourceError.hidden ? "Проверка недоступна" : "Проверьте каталог";
    message.textContent = error.message;
    action.disabled = false;
    actionLabel.textContent = "Проверить обновления";
    action.dataset.action = "check";
  }
}

async function waitForRestart() {
  const deadline = Date.now() + 180_000;
  await sleep(1_500);
  while (Date.now() < deadline) {
    try {
      const payload = await api(`/api/status?t=${Date.now()}`);
      if (payload.server.version !== initialVersion || payload.updates.apply?.state === "success") {
        window.location.reload();
        return;
      }
      render(payload);
    } catch {
      title.textContent = "Перезапускаем MCP";
      message.textContent = "Страница подключится снова автоматически.";
    }
    await sleep(1_500);
  }
  title.textContent = "Перезапуск занимает больше времени";
  message.textContent = "Обновите страницу или проверьте журнал службы.";
}

async function applyUpdate() {
  action.disabled = true;
  actionLabel.textContent = "Запускаем обновление…";
  try {
    render(await api("/api/updates/apply", { method: "POST" }));
    await waitForRestart();
  } catch (error) {
    title.textContent = "Обновление не запущено";
    message.textContent = error.message;
    action.disabled = false;
    actionLabel.textContent = "Проверить обновления";
    action.dataset.action = "check";
  }
}

action.addEventListener("click", () => {
  if (action.dataset.action === "apply") applyUpdate();
  else checkUpdates(true);
});

sourceToggle.addEventListener("click", () => {
  const open = sourceSettings.hidden;
  sourceSettings.hidden = !open;
  sourceToggle.setAttribute("aria-expanded", String(open));
  sourceToggle.textContent = open ? "Скрыть" : "Изменить";
  if (open) {
    if (!sourceDirty) showSourceFeedback("");
    sourceInput.focus();
  }
});

sourceInput.addEventListener("input", markSourceDirty);

sourceUseOrigin.addEventListener("click", switchToOrigin);

sourceSettings.addEventListener("submit", (event) => {
  event.preventDefault();
  checkUpdates(true);
});

documentationToggle.addEventListener("click", () => {
  const open = documentationSettings.hidden;
  documentationSettings.hidden = !open;
  documentationToggle.setAttribute("aria-expanded", String(open));
  documentationToggle.textContent = open
    ? "Скрыть"
    : documentationStatus?.path
      ? "Изменить"
      : "Подключить";
  if (open) {
    showDocumentationFeedback("");
    showDocumentationError(documentationStatus?.status === "invalid" ? documentationStatus.message : "");
    documentationPath.focus();
  }
});

documentationPath.addEventListener("input", () => {
  documentationDirty = true;
  showDocumentationError("");
  showDocumentationFeedback("Путь изменён, но ещё не подключён.", "pending");
});

documentationSettings.addEventListener("submit", (event) => {
  event.preventDefault();
  activateDocumentation();
});

consoleToggle.addEventListener("click", () => {
  const open = consoleSettings.hidden;
  consoleSettings.hidden = !open;
  consoleToggle.setAttribute("aria-expanded", String(open));
  consoleToggle.textContent = open ? "Скрыть" : "Настроить";
  if (open) {
    showConsoleError(consoleConfiguration?.status === "invalid" ? consoleConfiguration.message : "");
    showConsoleFeedback("");
    if (consoleEnabled.checked) consoleServer.focus();
    else consoleEnabled.focus();
  }
});

consoleEnabled.addEventListener("change", () => {
  if (consoleEnabled.checked) {
    consoleFields.hidden = false;
    if (
      consoleConfiguration?.configured &&
      consoleConfiguration?.credential_kind === "client_credentials"
    ) {
      saveConsoleConnection();
    } else {
      showConsoleFeedback("Заполните адрес и учётные данные, затем проверьте подключение.", "pending");
      consoleServer.focus();
    }
  } else {
    consoleFields.hidden = true;
    disableConsoleConnection();
  }
});

consoleSettings.addEventListener("submit", (event) => {
  event.preventDefault();
  saveConsoleConnection();
});

runtimeToggle.addEventListener("click", () => {
  const open = runtimeSettings.hidden;
  runtimeSettings.hidden = !open;
  runtimeToggle.setAttribute("aria-expanded", String(open));
  runtimeToggle.textContent = open ? "Скрыть" : "Настроить";
  if (open) {
    showRuntimeFeedback("");
    showRuntimeError("");
    runtimeInstanceRoot.focus();
  }
});

runtimeManagerEnabled.addEventListener("change", () => {
  runtimeManagerFields.hidden = !runtimeManagerEnabled.checked;
  showRuntimeError("");
  showRuntimeFeedback(
    runtimeManagerEnabled.checked
      ? "Заполните внутреннее подключение Application Manager и сохраните настройки."
      : "Журнал событий будет отключён после сохранения; локальные логи останутся доступны.",
    "pending",
  );
  if (runtimeManagerEnabled.checked) runtimeManagerServer.focus();
});

runtimeSettings.addEventListener("submit", (event) => {
  event.preventDefault();
  saveRuntimeConfiguration();
});

loadDocumentationStatus();
loadConsoleConfiguration();
loadRuntimeConfiguration();

api("/api/status")
  .then((payload) => {
    render(payload);
    return checkUpdates(false);
  })
  .catch((error) => {
    title.textContent = "Не удалось получить состояние";
    message.textContent = error.message;
    action.disabled = false;
    actionLabel.textContent = "Повторить";
    action.dataset.action = "check";
  });
