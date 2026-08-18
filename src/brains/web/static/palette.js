/* Brains command palette — vanilla JS, no deps.
 * Trigger: Ctrl+K / Cmd+K, or click the .js-palette-trigger button.
 * Reads commands from window.__BRAINS_COMMANDS__ (array of {label, hint, href, icon}).
 * Keyboard: ArrowUp/Down + Enter to navigate; Escape to close.
 */
(function () {
  "use strict";

  function ready(fn) {
    if (document.readyState !== "loading") fn();
    else document.addEventListener("DOMContentLoaded", fn);
  }

  function fuzzyMatch(needle, hay) {
    if (!needle) return true;
    needle = needle.toLowerCase();
    hay = (hay || "").toLowerCase();
    let i = 0;
    for (const ch of hay) {
      if (ch === needle[i]) i += 1;
      if (i === needle.length) return true;
    }
    return false;
  }

  function score(needle, label, hint) {
    if (!needle) return 0;
    const n = needle.toLowerCase();
    const l = (label || "").toLowerCase();
    const h = (hint || "").toLowerCase();
    if (l.startsWith(n)) return 0;
    if (l.includes(n)) return 1;
    if (h.includes(n)) return 2;
    return 3;
  }

  function htmlEscape(s) {
    return String(s).replace(/[&<>"']/g, (c) => (
      { "&": "&amp;", "<": "&lt;", ">": "&gt;", "\"": "&quot;", "'": "&#39;" }[c]
    ));
  }

  function buildOverlay() {
    const overlay = document.createElement("div");
    overlay.className = "palette-overlay";
    overlay.setAttribute("hidden", "hidden");
    overlay.setAttribute("role", "dialog");
    overlay.setAttribute("aria-label", "Command palette");
    overlay.innerHTML = [
      '<div class="palette" role="combobox" aria-expanded="true" aria-haspopup="listbox">',
      '  <input class="palette__input" type="text" placeholder="Jump to… (try \'tasks\', \'config\')" aria-label="Command" autocomplete="off" spellcheck="false" />',
      '  <ul class="palette__list" role="listbox"></ul>',
      '  <div class="palette__footer">',
      '    <span><kbd>↑</kbd><kbd>↓</kbd> navigate</span>',
      '    <span><kbd>↵</kbd> open</span>',
      '    <span><kbd>esc</kbd> close</span>',
      '  </div>',
      '</div>',
    ].join("");
    document.body.appendChild(overlay);
    return overlay;
  }

  ready(function () {
    const commands = (window.__BRAINS_COMMANDS__ || []);
    if (!commands.length) return;

    const overlay = buildOverlay();
    const input = overlay.querySelector(".palette__input");
    const list = overlay.querySelector(".palette__list");
    let selected = 0;
    let filtered = commands.slice();

    function render() {
      const q = input.value.trim();
      if (q) {
        filtered = commands
          .filter((c) => fuzzyMatch(q, c.label) || fuzzyMatch(q, c.hint || ""))
          .sort((a, b) => score(q, a.label, a.hint) - score(q, b.label, b.hint));
      } else {
        filtered = commands.slice();
      }
      if (selected >= filtered.length) selected = Math.max(0, filtered.length - 1);
      if (!filtered.length) {
        list.innerHTML = '<li class="palette__empty">No commands match.</li>';
        return;
      }
      list.innerHTML = filtered.map((c, i) => (
        '<li class="palette__item' + (i === selected ? " is-selected" : "") + '"' +
        ' role="option" aria-selected="' + (i === selected) + '" data-index="' + i + '">' +
        '  <span class="palette__label">' + htmlEscape(c.label) + '</span>' +
        (c.hint ? '<span class="palette__hint">' + htmlEscape(c.hint) + '</span>' : "") +
        '</li>'
      )).join("");
    }

    function open() {
      overlay.removeAttribute("hidden");
      input.value = "";
      selected = 0;
      render();
      setTimeout(() => input.focus(), 0);
    }
    function close() {
      overlay.setAttribute("hidden", "hidden");
    }
    function activate() {
      const cmd = filtered[selected];
      if (!cmd) return;
      close();
      if (cmd.href) window.location.href = cmd.href;
    }
    function move(delta) {
      if (!filtered.length) return;
      selected = (selected + delta + filtered.length) % filtered.length;
      render();
      const el = list.querySelector(".is-selected");
      if (el) el.scrollIntoView({ block: "nearest" });
    }

    document.addEventListener("keydown", function (ev) {
      const isOpen = !overlay.hasAttribute("hidden");
      const ctrlOrMeta = ev.ctrlKey || ev.metaKey;
      if (ctrlOrMeta && (ev.key === "k" || ev.key === "K")) {
        ev.preventDefault();
        isOpen ? close() : open();
        return;
      }
      if (!isOpen) return;
      if (ev.key === "Escape") { ev.preventDefault(); close(); }
      else if (ev.key === "ArrowDown") { ev.preventDefault(); move(1); }
      else if (ev.key === "ArrowUp") { ev.preventDefault(); move(-1); }
      else if (ev.key === "Enter") { ev.preventDefault(); activate(); }
    });

    input.addEventListener("input", function () {
      selected = 0;
      render();
    });

    list.addEventListener("click", function (ev) {
      const item = ev.target.closest(".palette__item");
      if (!item) return;
      selected = parseInt(item.getAttribute("data-index"), 10) || 0;
      activate();
    });

    overlay.addEventListener("click", function (ev) {
      if (ev.target === overlay) close();
    });

    document.querySelectorAll(".js-palette-trigger").forEach((btn) => {
      btn.addEventListener("click", function (ev) {
        ev.preventDefault();
        open();
      });
    });
  });
})();
