/**
 * Config page section switcher.
 *
 * The config page packs Providers, Router, Tiers, Routes, Limits and the
 * raw-overlay editor into one document. Rendering all of them at once is
 * overwhelming, so the rail acts like a tab strip: clicking an item shows
 * only that section and hides the rest. The URL hash is kept in sync so
 * links like /admin/config#tiers deep-link straight to a section. When
 * JavaScript is disabled the sections all render and the anchors still
 * scroll — progressive enhancement, nothing breaks.
 */
(function () {
  var rail = document.querySelector('.config-rail');
  var content = document.querySelector('.config-content');
  if (!rail || !content) return;

  var links = Array.from(rail.querySelectorAll('a[href^="#"]'));
  var sections = Array.from(content.querySelectorAll(':scope > .panel[id], :scope > details[id]'));
  if (!links.length || !sections.length) return;

  var idToLink = {};
  links.forEach(function (a) {
    var id = a.getAttribute('href').slice(1);
    if (id) idToLink[id] = a;
  });
  var ids = sections.map(function (s) { return s.id; }).filter(Boolean);

  content.classList.add('is-tabbed');

  function show(id) {
    if (!id || ids.indexOf(id) === -1) id = ids[0];
    sections.forEach(function (s) {
      var on = s.id === id;
      s.classList.toggle('is-section-hidden', !on);
      // The advanced section is a <details>; open it when selected so its
      // body shows without a second click.
      if (on && s.tagName === 'DETAILS') s.open = true;
    });
    links.forEach(function (a) { a.classList.remove('is-active'); });
    if (idToLink[id]) idToLink[id].classList.add('is-active');
  }

  links.forEach(function (a) {
    a.addEventListener('click', function (e) {
      var id = a.getAttribute('href').slice(1);
      if (!id) return;
      e.preventDefault();
      show(id);
      if (history.replaceState) history.replaceState(null, '', '#' + id);
      else window.location.hash = id;
    });
  });

  window.addEventListener('hashchange', function () {
    show((window.location.hash || '').slice(1));
  });

  var initial = (window.location.hash || '').slice(1);
  show(ids.indexOf(initial) !== -1 ? initial : ids[0]);
})();
