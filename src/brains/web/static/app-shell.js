/* Sidebar + info-toggle behaviour.
 *
 * Sidebar
 * -------
 *   - <div class="app-shell" id="app-shell"> wraps the sidenav + page.
 *   - Toggling adds/removes ``.is-expanded`` (rail widens, labels appear).
 *   - Persisted in localStorage under ``brains.sidenav.expanded``.
 *
 * Info-toggle
 * -----------
 *   - <div class="info-toggle" data-info-key="tasks"> wraps the button +
 *     panel rendered by the ``info_toggle()`` Jinja macro.
 *   - Click the button → toggles ``.is-collapsed``.
 *   - Persisted in localStorage under ``brains.info.<key>`` so each page
 *     remembers its own dismissed state independently.
 *
 * Defensive: if localStorage throws (private mode, quota), we fall back
 * to in-memory state — the UI still works for the session.
 */
(function () {
  "use strict";

  // ---- safe storage ------------------------------------------------
  const memStore = {};
  function get(key) {
    try { return window.localStorage.getItem(key); }
    catch (_) { return memStore[key] || null; }
  }
  function set(key, value) {
    try { window.localStorage.setItem(key, value); }
    catch (_) { memStore[key] = value; }
  }

  // ---- sidebar -----------------------------------------------------
  function initSidebar() {
    const shell = document.getElementById("app-shell");
    if (!shell) return;

    const expanded = get("brains.sidenav.expanded") === "1";
    if (expanded) shell.classList.add("is-expanded");

    const toggle = shell.querySelector(".sidenav__toggle");
    if (!toggle) return;

    toggle.addEventListener("click", function () {
      const now = !shell.classList.contains("is-expanded");
      shell.classList.toggle("is-expanded", now);
      set("brains.sidenav.expanded", now ? "1" : "0");
    });
  }

  // ---- info-toggle -------------------------------------------------
  function initInfoToggles() {
    document.querySelectorAll(".info-toggle[data-info-key]").forEach(function (root) {
      const key = "brains.info." + root.getAttribute("data-info-key");
      // First visit (no stored value) → show open. Stored "0" = collapsed.
      const stored = get(key);
      if (stored === "0") root.classList.add("is-collapsed");

      const btn = root.querySelector(".info-toggle__btn");
      if (!btn) return;

      btn.addEventListener("click", function () {
        const collapsed = root.classList.toggle("is-collapsed");
        set(key, collapsed ? "0" : "1");
      });
    });
  }

  function init() {
    initSidebar();
    initInfoToggles();
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
