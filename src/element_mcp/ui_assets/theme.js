(() => {
  const key = "element-mcp.theme";
  const system = window.matchMedia("(prefers-color-scheme: dark)");
  let preference = null;
  try { preference = localStorage.getItem(key); } catch { /* Storage may be disabled. */ }
  if (!["light", "dark"].includes(preference)) preference = null;
  function renderTheme() {
    const theme = preference || (system.matches ? "dark" : "light");
    document.documentElement.dataset.theme = theme;
    const button = document.querySelector("#theme-action");
    if (button) {
      const label = theme === "dark" ? "Включить светлую тему" : "Включить тёмную тему";
      button.setAttribute("aria-label", label);
      button.dataset.tooltip = label;
    }
  }
  renderTheme();
  system.addEventListener("change", () => { if (!preference) renderTheme(); });
  window.addEventListener("storage", event => {
    if (event.key !== key && event.key !== null) return;
    preference = ["light", "dark"].includes(event.newValue) ? event.newValue : null;
    renderTheme();
  });
  document.addEventListener("DOMContentLoaded", () => {
    renderTheme();
    document.querySelector("#theme-action").addEventListener("click", () => {
      preference = document.documentElement.dataset.theme === "dark" ? "light" : "dark";
      try { localStorage.setItem(key, preference); } catch { /* Keep this tab usable. */ }
      renderTheme();
    });
  });
})();
