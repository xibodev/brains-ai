/* Live filtering for list pages.
 *
 * Any <form data-live-filter> auto-submits on input/select changes:
 * - text inputs debounce for 300ms (so we don't refetch on every keystroke)
 * - selects submit immediately
 * - the visible "Apply" button is hidden when JS is active (CSS handles it)
 *   but still works as a fallback if JS fails.
 *
 * Used by Tasks, Sessions, Decisions, and any other page that imports
 * the _list_chrome `list_controls()` macro.
 */
(function () {
  "use strict";

  function debounce(fn, ms) {
    let t = null;
    return function () {
      const ctx = this;
      const args = arguments;
      clearTimeout(t);
      t = setTimeout(function () { fn.apply(ctx, args); }, ms);
    };
  }

  function wire(form) {
    form.classList.add("is-live");

    const submit = debounce(function () {
      if (typeof form.requestSubmit === "function") {
        form.requestSubmit();
      } else {
        form.submit();
      }
    }, 300);

    form.querySelectorAll("input[type='search'], input[type='text']").forEach(function (el) {
      el.addEventListener("input", submit);
    });

    form.querySelectorAll("select").forEach(function (el) {
      el.addEventListener("change", function () {
        if (typeof form.requestSubmit === "function") {
          form.requestSubmit();
        } else {
          form.submit();
        }
      });
    });
  }

  function init() {
    document.querySelectorAll("form[data-live-filter]").forEach(wire);
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
