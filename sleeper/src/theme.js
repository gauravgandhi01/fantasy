// Apply before styles paint to avoid flashing the wrong theme on reload.
(() => {
  const key = "truewatch:theme";
  const choices = new Set(["system", "light", "dark"]);
  const system = window.matchMedia("(prefers-color-scheme: dark)");
  let preference = "system";
  try {
    const saved = localStorage.getItem(key);
    if (choices.has(saved)) preference = saved;
  } catch { /* Persistence is optional. */ }
  function apply() {
    const dark = preference === "dark" || (preference === "system" && system.matches);
    document.documentElement.dataset.theme = dark ? "dark" : "light";
    document.querySelector('meta[name="theme-color"]').content = dark ? "#141416" : "#f5f5f6";
    const select = document.getElementById("themeSelect");
    if (select) select.value = preference;
  }
  apply();
  system.addEventListener("change", apply);
  document.addEventListener("DOMContentLoaded", () => {
    const select = document.getElementById("themeSelect");
    select.value = preference;
    select.addEventListener("change", () => {
      preference = select.value;
      try { localStorage.setItem(key, preference); } catch { /* Persistence is optional. */ }
      apply();
    });
  });
  window.addEventListener("storage", (event) => {
    if (event.key === key || event.key === null) {
      preference = choices.has(event.newValue) ? event.newValue : "system";
      apply();
    }
  });
})();
