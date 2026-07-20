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
const sourceError = document.querySelector("#source-error");
const sourceUseOrigin = document.querySelector("#source-use-origin");

let latestStatus = null;
let initialVersion = null;
let sourceDirty = false;

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

function setSourceEditor(updateSource) {
  if (!sourceDirty) {
    sourceInput.value = updateSource.kind === "local" ? updateSource.label : "";
  }
  sourceUseOrigin.hidden = updateSource.kind !== "local" && !sourceDirty;
}

function markSourceDirty() {
  sourceDirty = true;
  showSourceError("");
  sourceUseOrigin.hidden = false;
  actionLabel.textContent = "Проверить обновления";
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
  } catch (error) {
    showSourceError(error.message);
    sourceInput.focus();
    throw error;
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
    if (
      applyLocalUpdate &&
      payload.updates.source.kind === "local" &&
      payload.updates.state === "available" &&
      payload.updates.can_apply
    ) {
      await applyUpdate();
    }
  } catch (error) {
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
  if (open) sourceInput.focus();
});

sourceInput.addEventListener("input", markSourceDirty);

sourceUseOrigin.addEventListener("click", () => {
  sourceInput.value = "";
  markSourceDirty();
  sourceInput.focus();
});

sourceSettings.addEventListener("submit", (event) => {
  event.preventDefault();
  checkUpdates(true);
});

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
