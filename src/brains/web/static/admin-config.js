/**
 * Admin Config page controllers.
 *
 * Bootstrap blob is injected on the template via:
 *   window.__BRAINS_CONFIG_BOOTSTRAP__ = { ... }
 *
 * This script wires up the structured editors for model tiers, routes,
 * provider configuration, and rate limit, posting changes to
 * /admin/api/config.
 */
(function () {
var bootstrap = window.__BRAINS_CONFIG_BOOTSTRAP__ || {};
  var providers = bootstrap.providers || [];
  var modelCache = {};  // provider -> [{id,label,vendor}, ...]
  var priceCache = null; // { model_id: {input, output} } merged catalog
  var providerStatusCache = null; // { name: {configured, is_stub, reason} }

  // ---------- helpers ----------

  function el(tag, attrs, children) {
    var node = document.createElement(tag);
    if (attrs) Object.keys(attrs).forEach(function(k) {
      if (k === 'class') node.className = attrs[k];
      else if (k === 'value') node.value = attrs[k];
      else if (k === 'checked') node.checked = !!attrs[k];
      else node.setAttribute(k, attrs[k]);
    });
    (children || []).forEach(function(c) {
      if (c == null) return;
      if (typeof c === 'string') node.appendChild(document.createTextNode(c));
      else node.appendChild(c);
    });
    return node;
  }

  function setStatus(spanId, text, kind) {
    var span = document.getElementById(spanId);
    if (!span) return;
    span.textContent = text || '';
    span.style.color = kind === 'ok'  ? 'var(--ok)'
                     : kind === 'bad' ? 'var(--bad)'
                     : kind === 'pending' ? 'var(--warn)'
                     : 'var(--muted)';
  }

  function postJSON(body) {
    return fetch('/admin/api/config', {
      method: 'POST',
      credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    }).then(function(r) {
      if (r.ok) return r.json();
      return r.text().then(function(t) { throw new Error(t || r.statusText); });
    });
  }

  // ---------- Modal + summary-row helpers ----------

  var ICON_CHEVRON = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="m9 18 6-6-6-6"/></svg>';
  var ICON_CLOSE = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M18 6 6 18"/><path d="m6 6 12 12"/></svg>';

  // A compact clickable list row (div role=button so it dodges the
  // generic accent-button rule). name + free-text meta + chevron.
  function summaryRow(name, meta, onOpen) {
    var row = el('div', { class: 'summary-row', role: 'button', tabindex: '0' });
    row.appendChild(el('span', { class: 'summary-row__name' }, [name]));
    if (meta) row.appendChild(el('span', { class: 'summary-row__meta' }, [meta]));
    var chev = el('span', { class: 'summary-row__chev' });
    chev.innerHTML = ICON_CHEVRON;
    row.appendChild(chev);
    row.addEventListener('click', onOpen);
    row.addEventListener('keydown', function(e) {
      if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); onOpen(); }
    });
    return row;
  }

  // Opens a centered modal. opts: { title, body (Node), footer ([Node]), onClose }.
  function openModal(opts) {
    var overlay = el('div', { class: 'modal-overlay' });
    var modal = el('div', { class: 'modal', role: 'dialog', 'aria-modal': 'true' });
    var titleEl = el('div', { class: 'modal__title' }, [opts.title || '']);
    var closeBtn = el('button', { type: 'button', class: 'modal__close', 'aria-label': 'Close' });
    closeBtn.innerHTML = ICON_CLOSE;
    modal.appendChild(el('div', { class: 'modal__header' }, [titleEl, closeBtn]));
    var body = el('div', { class: 'modal__body' });
    if (opts.body) body.appendChild(opts.body);
    modal.appendChild(body);
    if (opts.footer && opts.footer.length) {
      var footer = el('div', { class: 'modal__footer' });
      opts.footer.forEach(function(node) { if (node) footer.appendChild(node); });
      modal.appendChild(footer);
    }
    overlay.appendChild(modal);
    function close() {
      document.removeEventListener('keydown', onKey);
      if (overlay.parentNode) overlay.parentNode.removeChild(overlay);
      if (typeof opts.onClose === 'function') opts.onClose();
    }
    function onKey(e) { if (e.key === 'Escape') close(); }
    overlay.addEventListener('mousedown', function(e) { if (e.target === overlay) close(); });
    closeBtn.addEventListener('click', close);
    document.addEventListener('keydown', onKey);
    document.body.appendChild(overlay);
    var first = body.querySelector('input, select, textarea, button');
    if (first) { try { first.focus(); } catch (e) {} }
    return { close: close, body: body };
  }

  function dsBtn(label, kind, danger) {
    var b = el('button', { type: 'button', class: 'btn ' + (kind || 'is-ghost') + ' is-sm' });
    if (danger) b.style.color = 'var(--color-danger)';
    b.appendChild(el('span', {}, [label]));
    return b;
  }

  function loadModels(name) {
    if (modelCache[name]) return Promise.resolve(modelCache[name]);
    if (!name) return Promise.resolve([]);
    return fetch('/admin/api/providers/' + encodeURIComponent(name) + '/models', {
      credentials: 'same-origin',
      headers: { 'Accept': 'application/json' },
    }).then(function(r) { return r.ok ? r.json() : {models: [], error: r.statusText}; })
      .then(function(body) {
        var items = (body && body.models) || [];
        modelCache[name] = items;
        return items;
      })
      .catch(function() { modelCache[name] = []; return []; });
  }

  function loadPrices() {
    if (priceCache) return Promise.resolve(priceCache);
    return fetch('/admin/api/prices', {
      credentials: 'same-origin',
      headers: { 'Accept': 'application/json' },
    }).then(function(r) { return r.ok ? r.json() : {prices: {}}; })
      .then(function(body) { priceCache = (body && body.prices) || {}; return priceCache; })
      .catch(function() { priceCache = {}; return priceCache; });
  }

  function loadProviderStatus(force) {
    if (providerStatusCache && !force) return Promise.resolve(providerStatusCache);
    return fetch('/admin/api/providers/status', {
      credentials: 'same-origin',
      headers: { 'Accept': 'application/json' },
    }).then(function(r) { return r.ok ? r.json() : {providers: []}; })
      .then(function(body) {
        var map = {};
        (body.providers || []).forEach(function(p) { map[p.name] = p; });
        providerStatusCache = map;
        return map;
      })
      .catch(function() { providerStatusCache = {}; return providerStatusCache; });
  }

  // Canonical route keys (classifier task_types) for the Routes editor
  // autocomplete. Free-form keys still work — this is a convenience.
  var routeKeyCache = null;
  function loadRouteKeys() {
    if (routeKeyCache) return Promise.resolve(routeKeyCache);
    return fetch('/admin/api/route-keys', {
      credentials: 'same-origin',
      headers: { 'Accept': 'application/json' },
    }).then(function(r) { return r.ok ? r.json() : {keys: []}; })
      .then(function(body) { routeKeyCache = (body && body.keys) || []; return routeKeyCache; })
      .catch(function() { routeKeyCache = []; return routeKeyCache; });
  }

  // GET /admin/api/savings/preview wrapper used by the tier row to
  // estimate "if you switch this tier from model A to model B, last
  // 7d would have cost X vs Y" — pure read, no mutation.
  function previewSavings(model, currentModel, days) {
    var qs = 'model=' + encodeURIComponent(model || '') + '&days=' + (days || 7);
    if (currentModel) qs += '&current_model=' + encodeURIComponent(currentModel);
    return fetch('/admin/api/savings/preview?' + qs, {
      credentials: 'same-origin',
      headers: { 'Accept': 'application/json' },
    }).then(function(r) {
      if (!r.ok) return null;
      return r.json();
    }).catch(function() { return null; });
  }

  // Longest-prefix price lookup (mirrors brains.router.prices.lookup_price).
  function lookupPrice(modelId, prices) {
    if (!modelId || !prices) return null;
    var lower = String(modelId).toLowerCase();
    if (prices[lower]) return prices[lower];
    var best = null;
    Object.keys(prices).forEach(function(prefix) {
      if (lower.indexOf(prefix) === 0) {
        if (!best || prefix.length > best.length) best = prefix;
      }
    });
    return best ? prices[best] : null;
  }

  function formatPrice(p) {
    if (!p) return '';
    // Show one decimal for cents-range, full $ for dollars-range
    function fmt(v) {
      if (v === 0) return '$0';
      if (v < 1) return '$' + v.toFixed(2).replace(/\.?0+$/, '');
      return '$' + v.toFixed(2);
    }
    return ' · ' + fmt(p.input) + '/' + fmt(p.output) + ' per 1M';
  }

  function modelOptionLabel(modelId, vendor, prices) {
    var label = modelId;
    if (vendor) label += ' (' + vendor + ')';
    return label + formatPrice(lookupPrice(modelId, prices || priceCache));
  }

  function providerSelect(currentValue) {
    var sel = el('select', {}, providers.map(function(p) {
      var status = (providerStatusCache || {})[p];
      var suffix = '';
      if (status) {
        if (status.is_stub) suffix = ' (stub)';
        else if (!status.configured) suffix = ' (not configured)';
      }
      return el('option', { value: p }, [p + suffix]);
    }));
    sel.value = currentValue || providers[0];
    return sel;
  }

  // ---------- Tiers editor ----------

  var tiersBox = document.getElementById('tiers-editor');

  var CUSTOM_OPTION_VALUE = '__custom__';

  function buildModelSelect(items, currentModel) {
    var sel = el('select', {});
    // Group discovered models
    if (items && items.length) {
      var group = document.createElement('optgroup');
      group.label = 'Discovered models';
      items.forEach(function(m) {
        var opt = document.createElement('option');
        opt.value = m.id;
        opt.textContent = modelOptionLabel(m.id, m.vendor);
        group.appendChild(opt);
      });
      sel.appendChild(group);
    }
    // If the current model isn't in the discovered list, surface it as
    // a pinned option so the operator can see what's currently saved.
    var inDiscovered = (items || []).some(function(m) { return m.id === currentModel; });
    if (currentModel && !inDiscovered) {
      var pinnedGroup = document.createElement('optgroup');
      pinnedGroup.label = 'Currently configured';
      var pinned = document.createElement('option');
      pinned.value = currentModel;
      pinned.textContent = modelOptionLabel(currentModel, null) + ' — not in discovery list';
      pinnedGroup.appendChild(pinned);
      sel.appendChild(pinnedGroup);
    }
    // Custom escape hatch
    var custom = document.createElement('option');
    custom.value = CUSTOM_OPTION_VALUE;
    custom.textContent = '✎ Custom model id…';
    sel.appendChild(custom);

    sel.value = currentModel || (items && items.length ? items[0].id : CUSTOM_OPTION_VALUE);
    return sel;
  }

  function makeTierRow(tierName, route) {
    var nameInput = el('input', { type: 'text', value: tierName || '', placeholder: 'tier name (e.g. default)' });
    var provSel   = providerSelect(route && route.provider);
    var modelHolder = el('div', { class: 'tier-model-holder' });
    var statusSpan = el('span', { class: 'muted', style: 'font-size:11px;' }, ['loading…']);
    // Savings-preview pill: shows "last 7d would cost $X vs $Y currently"
    // whenever the model changes. Hidden until we have a model.
    var previewSpan = el('span', { class: 'muted', style: 'font-size:11px; margin-left:8px;' });

    var initialModel = (route && route.model) || '';

    // Custom text input shown when "Custom model id..." is selected, OR
    // (initially) when the configured model isn't in the discovery list.
    var customInput = el('input', { type: 'text', value: '', placeholder: 'type model id', autocomplete: 'off' });
    customInput.style.display = 'none';
    customInput.style.marginTop = '4px';

    var modelSel = null;

    function pickedCustom() {
      if (!modelSel) return false;
      return modelSel.value === CUSTOM_OPTION_VALUE;
    }

    function readModel() {
      if (!modelSel) return '';
      if (pickedCustom()) return (customInput.value || '').trim();
      return modelSel.value || '';
    }
    // expose readModel for the save handler
    nameInput._readModel = readModel;

    // Debounced savings-preview refresh — only when the chosen model
    // differs from the saved value, so the operator sees the *delta*.
    var previewTimer = null;
    function refreshPreview() {
      if (previewTimer) clearTimeout(previewTimer);
      previewTimer = setTimeout(function() {
        var chosen = readModel();
        if (!chosen) { previewSpan.textContent = ''; return; }
        if (chosen === initialModel) {
          previewSpan.textContent = '';
          return;
        }
        previewSpan.textContent = '· estimating savings…';
        previewSavings(chosen, initialModel, 7).then(function(p) {
          if (!p) { previewSpan.textContent = ''; return; }
          if (p.rows_considered === 0) {
            previewSpan.textContent = '· no recent traffic on ' + (initialModel || 'this tier') + ' to compare';
            return;
          }
          if (p.projected_price_per_million == null) {
            previewSpan.textContent = '· ' + chosen + ' has no price catalog entry';
            return;
          }
          var fmt = function(v) { return '$' + (Math.round(v * 10000) / 10000).toFixed(4).replace(/0+$/, '').replace(/\.$/, ''); };
          var delta = p.delta_usd;
          var direction = delta > 0 ? 'save ' : 'cost ';
          var amount = fmt(Math.abs(delta));
          previewSpan.textContent = '· last ' + p.window_days + 'd: would ' + direction + amount
            + ' (now ' + fmt(p.current_actual_usd) + ', projected ' + fmt(p.projected_actual_usd) + ')';
          previewSpan.style.color = delta > 0 ? 'var(--ok)' : (delta < 0 ? 'var(--warn)' : 'var(--muted)');
        });
      }, 350);
    }

    function onSelChange() {
      if (pickedCustom()) {
        customInput.style.display = '';
        customInput.focus();
      } else {
        customInput.style.display = 'none';
      }
      refreshPreview();
    }
    customInput.addEventListener('input', refreshPreview);

    function refreshModels() {
      modelHolder.innerHTML = '';
      statusSpan.textContent = 'loading models for ' + provSel.value + '…';
      loadModels(provSel.value).then(function(items) {
        modelSel = buildModelSelect(items, (route && route.model) || '');
        modelSel.addEventListener('change', onSelChange);
        modelHolder.appendChild(modelSel);
        modelHolder.appendChild(customInput);
        // If pre-existing config used a non-discovered model, immediately
        // bias the UI toward the custom-input fallback so the operator
        // sees the raw value.
        if (modelSel.value === CUSTOM_OPTION_VALUE) {
          customInput.value = (route && route.model) || '';
          customInput.style.display = '';
        } else {
          customInput.style.display = 'none';
        }
        statusSpan.textContent = items.length
          ? items.length + ' model(s) suggested'
          : 'no discovery — use Custom';
      });
    }
    provSel.addEventListener('change', refreshModels);

    // Wait for prices before initial render so labels show inline.
    loadPrices().then(refreshModels);

    var delBtn = el('button', { type: 'button', class: 'secondary' }, ['Delete']);
    var row = el('tr', {}, [
      el('td', {}, [nameInput]),
      el('td', {}, [provSel]),
      el('td', {}, [modelHolder, el('br'), statusSpan, previewSpan]),
      el('td', { style: 'width: 80px; text-align: right;' }, [delBtn]),
    ]);
    delBtn.addEventListener('click', function() { row.parentNode.removeChild(row); });
    return row;
  }

  function renderTiers() {
    tiersBox.innerHTML = '';
    var models = bootstrap.models || {};
    var keys = Object.keys(models);
    if (!keys.length) {
      tiersBox.appendChild(el('p', { class: 'muted' }, ['No tiers yet — click "Add tier".']));
      return;
    }
    var list = el('div', { class: 'summary-list' });
    keys.forEach(function(tier) {
      var route = models[tier];
      list.appendChild(summaryRow(tier, route.provider + ' · ' + route.model, function() {
        openTierModal(tier, route);
      }));
    });
    tiersBox.appendChild(list);
  }

  // One tier editor hosted in a modal. Saves by merging into the full
  // models map and POSTing it (the API takes the whole dict).
  function openTierModal(originalName, route) {
    var tbody = el('tbody', {});
    var row = makeTierRow(originalName || '', route);
    var lastTd = row.querySelector('td:last-child');
    if (lastTd) lastTd.style.display = 'none'; // hide inline delete; footer has one
    tbody.appendChild(row);
    var table = el('table', {}, [tbody]);

    var status = el('span', { class: 'muted', style: 'font-size: var(--fs-xs);' });
    var saveBtn = dsBtn('Save tier', 'is-primary');
    var delBtn = originalName ? dsBtn('Delete', 'is-ghost', true) : null;
    var footer = [];
    if (delBtn) footer.push(delBtn);
    footer.push(el('span', { class: 'spacer' }));
    footer.push(status);
    footer.push(saveBtn);

    var m = openModal({
      title: originalName ? ('Edit tier · ' + originalName) : 'Add tier',
      body: table,
      footer: footer,
    });

    saveBtn.addEventListener('click', function() {
      var nameInput = row.querySelector('td:first-child input');
      var sel = row.querySelector('select');
      var tier = (nameInput.value || '').trim();
      var model = nameInput._readModel ? nameInput._readModel() : '';
      if (!tier) { status.textContent = 'tier name required'; status.style.color = 'var(--bad)'; return; }
      if (!model) { status.textContent = 'pick a model from the dropdown, or fill the custom field'; status.style.color = 'var(--bad)'; return; }
      var models = {};
      Object.keys(bootstrap.models || {}).forEach(function(k) { models[k] = bootstrap.models[k]; });
      if (originalName && originalName !== tier) delete models[originalName];
      models[tier] = { provider: sel.value, model: model };
      status.textContent = 'saving…'; status.style.color = '';
      postJSON({ models: models }).then(function(resp) {
        bootstrap.models = resp.live.models;
        renderTiers(); renderRoutes(); renderRoutingMap();
        m.close();
      }).catch(function(err) { status.textContent = 'failed: ' + err.message; status.style.color = 'var(--bad)'; });
    });

    if (delBtn) delBtn.addEventListener('click', function() {
      var models = {};
      Object.keys(bootstrap.models || {}).forEach(function(k) { if (k !== originalName) models[k] = bootstrap.models[k]; });
      if (!Object.keys(models).length) { status.textContent = 'at least one tier is required'; status.style.color = 'var(--bad)'; return; }
      status.textContent = 'deleting…'; status.style.color = '';
      postJSON({ models: models }).then(function(resp) {
        bootstrap.models = resp.live.models;
        renderTiers(); renderRoutes(); renderRoutingMap();
        m.close();
      }).catch(function(err) { status.textContent = 'failed: ' + err.message; status.style.color = 'var(--bad)'; });
    });
  }

  document.getElementById('add-tier').addEventListener('click', function() {
    openTierModal('', null);
  });

  // The legacy bulk "Save tiers" button is superseded by per-tier modal
  // saves; hide it so the section reads as a clean summary list.
  (function() { var b = document.getElementById('save-tiers'); if (b) b.style.display = 'none'; })();

  // ---------- Routes editor ----------

  var routesBox = document.getElementById('routes-editor');

  function tierSelect(currentValue) {
    var tiers = Object.keys(bootstrap.models || {});
    var sel = el('select', {}, tiers.map(function(t) {
      return el('option', { value: t }, [t]);
    }));
    sel.value = currentValue || tiers[0] || '';
    return sel;
  }

  // Single shared <datalist> with canonical route keys; every route-key
  // <input> points its `list` attribute at it. Free-form values still
  // work — datalist is purely a suggestion list.
  var ROUTE_KEY_DATALIST_ID = 'brains-route-key-suggestions';
  function ensureRouteKeyDatalist() {
    if (document.getElementById(ROUTE_KEY_DATALIST_ID)) return;
    var dl = el('datalist', { id: ROUTE_KEY_DATALIST_ID });
    document.body.appendChild(dl);
    loadRouteKeys().then(function(keys) {
      keys.forEach(function(k) {
        // <option> inside <datalist>: value drives the suggestion,
        // label appears as muted secondary text in most browsers.
        var opt = el('option', { value: k.key });
        if (k.description) opt.setAttribute('label', k.description);
        dl.appendChild(opt);
      });
    });
  }

  function makeRouteRow(routeKey, tierName) {
    ensureRouteKeyDatalist();
    var keyInput = el('input', {
      type: 'text',
      value: routeKey || '',
      placeholder: 'route key (e.g. code_fix)',
      list: ROUTE_KEY_DATALIST_ID,
      autocomplete: 'off',
    });
    var tierSel = tierSelect(tierName);
    var delBtn = el('button', { type: 'button', class: 'secondary' }, ['Delete']);
    var row = el('tr', {}, [
      el('td', {}, [keyInput]),
      el('td', {}, [tierSel]),
      el('td', { style: 'width: 80px; text-align: right;' }, [delBtn]),
    ]);
    delBtn.addEventListener('click', function() { row.parentNode.removeChild(row); });
    return row;
  }

  function renderRoutes() {
    routesBox.innerHTML = '';
    var routes = bootstrap.routes || {};
    var keys = Object.keys(routes);
    if (!keys.length) {
      routesBox.appendChild(el('p', { class: 'muted' }, ['No routes yet — click "Add route".']));
      return;
    }
    var list = el('div', { class: 'summary-list' });
    keys.forEach(function(key) {
      var tier = routes[key];
      var model = (bootstrap.models || {})[tier];
      var meta = '→ ' + tier + (model ? '  (' + model.provider + ' · ' + model.model + ')' : '');
      list.appendChild(summaryRow(key, meta, function() { openRouteModal(key, tier); }));
    });
    routesBox.appendChild(list);
  }

  function openRouteModal(originalKey, tierName) {
    var tbody = el('tbody', {});
    var row = makeRouteRow(originalKey || '', tierName);
    var lastTd = row.querySelector('td:last-child');
    if (lastTd) lastTd.style.display = 'none';
    tbody.appendChild(row);
    var table = el('table', {}, [tbody]);

    var status = el('span', { class: 'muted', style: 'font-size: var(--fs-xs);' });
    var saveBtn = dsBtn('Save route', 'is-primary');
    var delBtn = originalKey ? dsBtn('Delete', 'is-ghost', true) : null;
    var footer = [];
    if (delBtn) footer.push(delBtn);
    footer.push(el('span', { class: 'spacer' }));
    footer.push(status);
    footer.push(saveBtn);

    var m = openModal({
      title: originalKey ? ('Edit route · ' + originalKey) : 'Add route',
      body: table,
      footer: footer,
    });

    saveBtn.addEventListener('click', function() {
      var key = (row.querySelector('input').value || '').trim();
      var tier = row.querySelector('select').value;
      if (!key) { status.textContent = 'route key required'; status.style.color = 'var(--bad)'; return; }
      if (!tier) { status.textContent = 'pick a tier'; status.style.color = 'var(--bad)'; return; }
      var routes = {};
      Object.keys(bootstrap.routes || {}).forEach(function(k) { routes[k] = bootstrap.routes[k]; });
      if (originalKey && originalKey !== key) delete routes[originalKey];
      routes[key] = tier;
      status.textContent = 'saving…'; status.style.color = '';
      postJSON({ routes: routes }).then(function(resp) {
        bootstrap.routes = resp.live.routes;
        renderRoutes(); renderRoutingMap();
        m.close();
      }).catch(function(err) { status.textContent = 'failed: ' + err.message; status.style.color = 'var(--bad)'; });
    });

    if (delBtn) delBtn.addEventListener('click', function() {
      var routes = {};
      Object.keys(bootstrap.routes || {}).forEach(function(k) { if (k !== originalKey) routes[k] = bootstrap.routes[k]; });
      status.textContent = 'deleting…'; status.style.color = '';
      postJSON({ routes: routes }).then(function(resp) {
        bootstrap.routes = resp.live.routes;
        renderRoutes(); renderRoutingMap();
        m.close();
      }).catch(function(err) { status.textContent = 'failed: ' + err.message; status.style.color = 'var(--bad)'; });
    });
  }

  document.getElementById('add-route').addEventListener('click', function() {
    openRouteModal('', null);
  });

  (function() { var b = document.getElementById('save-routes'); if (b) b.style.display = 'none'; })();

  // ---------- Provider panes ----------

  var providerPanes = document.getElementById('provider-panes');

  function badgeForStatus(status) {
    var pill = el('span', { class: 'pill' });
    if (!status) {
      pill.textContent = 'unknown';
      pill.style.background = 'transparent';
      return pill;
    }
    if (status.is_stub) {
      pill.textContent = 'Stub';
      pill.className = 'pill';
      pill.title = status.reason || 'built-in dev provider';
    } else if (status.configured) {
      pill.textContent = 'Configured';
      pill.className = 'pill is-accent';
      pill.title = 'has enough settings to attempt a request (not yet tested live)';
    } else {
      pill.textContent = 'Not configured';
      pill.className = 'pill is-warning';
      pill.title = status.reason || 'missing required settings';
    }
    return pill;
  }

  function renderProviderPane(name, schema) {
    var paneDetails = el('section', { class: 'provider-pane' });

    var status = (providerStatusCache || {})[name];
    var badge = badgeForStatus(status);
    badge.style.marginLeft = '8px';
    var summary = el('div', { class: 'provider-pane__head' });
    summary.appendChild(el('span', { class: 'provider-pane__title' }, [name]));
    summary.appendChild(badge);
    paneDetails.appendChild(summary);

    if (status && !status.configured && status.reason) {
      var reasonRow = el('div', { class: 'muted', style: 'font-size:12px; margin-top:6px;' }, [status.reason]);
      paneDetails.appendChild(reasonRow);
    }

    var inputs = {};
    schema.fields.forEach(function(f) {
      var current = (schema.values || {})[f.key];
      if (current === undefined) current = (f.type === 'bool' ? false : '');
      var wrapper = el('div', { style: 'margin-top: 10px;' });
      var labelText = f.label + (f.help ? '' : '');
      wrapper.appendChild(el('label', {}, [labelText]));
      var input;
      if (f.type === 'bool') {
        input = el('input', { type: 'checkbox', checked: !!current });
        input.style.width = 'auto';
        var inlineRow = el('div', { style: 'display:flex; align-items:center; gap:8px;' }, [input]);
        inlineRow.appendChild(el('span', { class: 'muted', style: 'font-size:12px;' }, [f.help || '']));
        wrapper.appendChild(inlineRow);
      } else if (f.type === 'number') {
        input = el('input', { type: 'number', value: String(current), step: f.step || '1', min: f.min || '0' });
        wrapper.appendChild(input);
        if (f.help) wrapper.appendChild(el('p', { class: 'muted', style: 'font-size:11px; margin: 4px 0 0;' }, [f.help]));
      } else {
        input = el('input', { type: 'text', value: current == null ? '' : String(current), placeholder: f.placeholder || '' });
        if (f.readonly) input.setAttribute('readonly', 'readonly');
        wrapper.appendChild(input);
        if (f.help) wrapper.appendChild(el('p', { class: 'muted', style: 'font-size:11px; margin: 4px 0 0;' }, [f.help]));
      }
      inputs[f.key] = { el: input, type: f.type };
      paneDetails.appendChild(wrapper);
    });

    var statusSpan = el('span', { class: 'muted', style: 'font-size:12px; margin-left: 10px;' });
    var saveBtn = el('button', { type: 'button' }, ['Save ' + name]);
    var testBtn = el('button', { type: 'button', class: 'secondary' }, ['Test connection']);
    testBtn.style.marginLeft = '8px';
    var actionRow = el('div', { style: 'margin-top: 14px; display:flex; align-items:center;' }, [saveBtn, testBtn, statusSpan]);
    paneDetails.appendChild(actionRow);

    function refreshBadge() {
      return loadProviderStatus(true).then(function(map) {
        var fresh = map[name];
        var newBadge = badgeForStatus(fresh);
        newBadge.style.marginLeft = '8px';
        summary.replaceChild(newBadge, badge);
        badge = newBadge;
      });
    }

    saveBtn.addEventListener('click', function() {
      var body = {};
      schema.fields.forEach(function(f) {
        if (f.readonly) return;
        var entry = inputs[f.key];
        var val;
        if (f.type === 'bool') val = entry.el.checked;
        else if (f.type === 'number') val = parseFloat(entry.el.value);
        else val = entry.el.value;
        // Wrap bare env-var names as ${ENV:NAME} for the api_key field
        if (f.envRef && typeof val === 'string' && val) {
          var trimmed = val.trim();
          if (trimmed && !trimmed.startsWith('${ENV:')) {
            if (/^[A-Z][A-Z0-9_]*$/.test(trimmed)) val = '${ENV:' + trimmed + '}';
          }
        }
        body[f.overlay_key] = val;
      });
      statusSpan.textContent = 'saving…';
      statusSpan.style.color = 'var(--warn)';
      postJSON(body).then(function() {
        statusSpan.textContent = 'saved';
        statusSpan.style.color = 'var(--ok)';
        // Refresh badge — settings changed, configured-state may have flipped.
        return refreshBadge();
      }).catch(function(err) {
        statusSpan.textContent = 'failed: ' + err.message;
        statusSpan.style.color = 'var(--bad)';
      });
    });

    testBtn.addEventListener('click', function() {
      // Reuse the existing /admin/api/providers/test endpoint — needs a
      // model id. Use the model bound to the `default` tier for this
      // provider if any; otherwise prompt.
      var model = '';
      var tiers = bootstrap.models || {};
      Object.keys(tiers).forEach(function(t) {
        if (tiers[t] && tiers[t].provider === name && !model) model = tiers[t].model;
      });
      if (!model) {
        model = window.prompt('Test which model id?', '');
        if (!model) return;
      }
      statusSpan.textContent = 'testing ' + model + '…';
      statusSpan.style.color = 'var(--warn)';
      fetch('/admin/api/providers/test', {
        method: 'POST',
        credentials: 'same-origin',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ provider: name, model: model }),
      }).then(function(r) { return r.json().then(function(b) { return { ok: r.ok, body: b }; }); })
        .then(function(out) {
          if (out.ok && out.body && out.body.stage === 'ok') {
            statusSpan.textContent = '✓ connected (' + model + ')';
            statusSpan.style.color = 'var(--ok)';
            // Promote badge to a stronger "Connected" pill (ephemeral, until reload).
            var connected = el('span', { class: 'pill is-success' }, ['Connected']);
            connected.style.marginLeft = '8px';
            connected.title = 'live test succeeded against ' + model;
            summary.replaceChild(connected, badge);
            badge = connected;
          } else {
            var msg = (out.body && (out.body.error || out.body.detail)) || ('HTTP ' + (out.body && out.body.stage || '?'));
            statusSpan.textContent = 'test failed: ' + msg;
            statusSpan.style.color = 'var(--bad)';
          }
        })
        .catch(function(err) {
          statusSpan.textContent = 'test failed: ' + err.message;
          statusSpan.style.color = 'var(--bad)';
        });
    });

    if (typeof schema.custom === 'function') {
      schema.custom(paneDetails, { refreshBadge: refreshBadge });
    }

    return paneDetails;
  }

  var PROVIDER_SCHEMAS = {
    'ollama': {
      values: bootstrap.provider_config.ollama,
      fields: [
        { key: 'base_url', overlay_key: 'ollama_base_url', label: 'Base URL', type: 'text', placeholder: 'http://127.0.0.1:11434', help: 'Where Ollama is serving. Loopback is always allowed; private LAN needs BRAINS_ALLOW_PRIVATE_PROVIDERS=1.' },
        { key: 'timeout_seconds', overlay_key: 'ollama_timeout_seconds', label: 'Request timeout (seconds)', type: 'number', step: '1', min: '1' },
      ],
    },
    'openai_compatible': {
      values: bootstrap.provider_config.openai_compatible,
      fields: [
        { key: 'base_url', overlay_key: 'openai_compatible_base_url', label: 'Base URL', type: 'text', placeholder: 'https://api.openai.com/v1', help: 'OpenAI-compatible endpoint (OpenAI, Together, Groq, vLLM, llama-server, etc.).' },
        { key: 'api_key', overlay_key: 'openai_compatible_api_key', label: 'API key', type: 'text', placeholder: '${ENV:OPENAI_API_KEY} or just OPENAI_API_KEY', help: 'Bare env-var names are auto-wrapped as ${ENV:NAME}. The raw value is NEVER stored in YAML.', envRef: true },
        { key: 'timeout_seconds', overlay_key: 'openai_compatible_timeout_seconds', label: 'Request timeout (seconds)', type: 'number', step: '1', min: '1' },
      ],
    },
    'github_copilot': {
      values: bootstrap.provider_config.github_copilot,
      fields: [
        { key: 'allow_copilot_proxy', overlay_key: 'allow_copilot_proxy', label: 'Enable Copilot proxy', type: 'bool', help: 'Off by default — GitHub Copilot is licensed for editor code suggestions; using it as a gateway is a personal-use grey area. Turn on for your own loopback gateway, then Save. (Always refused on shared Postgres / multi-operator.)' },
        { key: 'use_gh_cli', overlay_key: 'github_copilot_use_gh_cli', label: 'Use gh CLI to resolve OAuth token', type: 'bool', help: 'When on, brains shells out to `gh auth token` to discover your Copilot OAuth token (zero config). When off, brains expects BRAINS_GITHUB_COPILOT_OAUTH_TOKEN in env or a token from the device-code sign-in below.' },
        { key: 'cache_dir', overlay_key: 'github_copilot_cache_dir', label: 'Session token cache dir', type: 'text', placeholder: 'leave blank for default (~/.brains/copilot)', help: 'Where the short-lived session token is cached between requests.' },
        { key: 'timeout_seconds', overlay_key: 'github_copilot_timeout_seconds', label: 'Request timeout (seconds)', type: 'number', step: '1', min: '1' },
        { key: 'editor_version', overlay_key: 'github_copilot_editor_version', label: 'Editor-Version header', type: 'text', placeholder: 'vscode/1.95.3', help: 'Identifies brains to the Copilot API. Defaults to a current VS Code version; only change if you know why.' },
        { key: 'integration_id', overlay_key: 'github_copilot_integration_id', label: 'Copilot-Integration-Id header', type: 'text', placeholder: 'vscode-chat' },
      ],
      custom: function(pane, ctx) {
        var box = el('div', { style: 'margin-top:16px; padding-top:12px; border-top:1px solid var(--border);' });
        box.appendChild(el('div', { style: 'font-weight:600; margin-bottom:6px;' }, ['GitHub Copilot sign-in (device code)']));
        var authLine = el('div', { class: 'muted', style: 'font-size:12px; margin-bottom:8px;' }, ['checking…']);
        box.appendChild(authLine);
        var loginBtn = el('button', { type: 'button' }, ['Login with device code']);
        var logoutBtn = el('button', { type: 'button', class: 'secondary', style: 'margin-left:8px;' }, ['Logout']);
        box.appendChild(el('div', { style: 'display:flex; align-items:center;' }, [loginBtn, logoutBtn]));
        var flowArea = el('div', { style: 'margin-top:10px;' });
        box.appendChild(flowArea);
        pane.appendChild(box);

        function refreshAuth() {
          return fetch('/admin/api/providers/github_copilot/auth-status', { credentials: 'same-origin' })
            .then(function(r) { return r.ok ? r.json() : {}; })
            .then(function(s) {
              var bits = [];
              bits.push(s.proxy_enabled ? 'proxy: enabled' : 'proxy: DISABLED — tick "Enable Copilot proxy" + Save');
              if (s.proxy_enabled && !s.proxy_allowed && s.proxy_blocked_reason) bits.push('blocked: ' + s.proxy_blocked_reason);
              bits.push('token: ' + (s.active_source ? s.active_source : 'none'));
              authLine.textContent = bits.join('  ·  ');
              authLine.style.color = (s.active_source && s.proxy_enabled && s.proxy_allowed) ? 'var(--ok)' : 'var(--muted)';
              return s;
            })
            .catch(function() { authLine.textContent = 'auth status unavailable'; });
        }

        var poller = null;
        function stopPoll() { if (poller) { clearInterval(poller); poller = null; } }

        loginBtn.addEventListener('click', function() {
          stopPoll();
          flowArea.innerHTML = '';
          loginBtn.disabled = true;
          var msg = el('div', { class: 'muted', style: 'font-size:12px;' }, ['requesting device code…']);
          flowArea.appendChild(msg);
          fetch('/admin/api/providers/github_copilot/device/start', { method: 'POST', credentials: 'same-origin' })
            .then(function(r) { return r.json(); })
            .then(function(d) {
              if (!d || d.error) {
                msg.textContent = 'failed: ' + ((d && d.error) || 'unknown error');
                msg.style.color = 'var(--bad)';
                loginBtn.disabled = false;
                return;
              }
              flowArea.innerHTML = '';
              var instr = el('div', { style: 'font-size:13px; margin-bottom:6px;' }, [
                'Open ', el('a', { href: d.verification_uri, target: '_blank', rel: 'noopener' }, [d.verification_uri]),
                ' and enter this code:',
              ]);
              var codeEl = el('code', { style: 'font-size:20px; letter-spacing:3px; padding:4px 10px; background:var(--panel); border-radius:6px; user-select:all;' }, [d.user_code]);
              var copyBtn = el('button', { type: 'button', class: 'secondary', style: 'margin-left:10px;' }, ['Copy']);
              copyBtn.addEventListener('click', function() {
                try { navigator.clipboard.writeText(d.user_code); copyBtn.textContent = 'Copied'; } catch (e) {}
              });
              var waiting = el('div', { class: 'muted', style: 'font-size:12px; margin-top:8px;' }, ['waiting for you to authorize in the browser…']);
              flowArea.appendChild(instr);
              flowArea.appendChild(el('div', { style: 'display:flex; align-items:center;' }, [codeEl, copyBtn]));
              flowArea.appendChild(waiting);
              var intervalMs = Math.max(2, (d.interval || 5)) * 1000;
              poller = setInterval(function() {
                fetch('/admin/api/providers/github_copilot/device/poll', {
                  method: 'POST', credentials: 'same-origin',
                  headers: { 'Content-Type': 'application/json' },
                  body: JSON.stringify({ device_code: d.device_code }),
                }).then(function(r) { return r.json(); }).then(function(p) {
                  if (!p) return;
                  if (p.status === 'authorized') {
                    stopPoll();
                    loginBtn.disabled = false;
                    waiting.textContent = '✓ authorized — OAuth token cached';
                    waiting.style.color = 'var(--ok)';
                    if (modelCache['github_copilot']) delete modelCache['github_copilot'];
                    refreshAuth();
                    if (ctx && ctx.refreshBadge) ctx.refreshBadge();
                  } else if (p.status === 'pending' || p.status === 'slow_down') {
                    // keep waiting
                  } else {
                    stopPoll();
                    loginBtn.disabled = false;
                    waiting.textContent = 'failed: ' + (p.error || p.status);
                    waiting.style.color = 'var(--bad)';
                  }
                }).catch(function() { /* transient network hiccup; keep polling */ });
              }, intervalMs);
            })
            .catch(function(err) {
              msg.textContent = 'failed: ' + err.message;
              msg.style.color = 'var(--bad)';
              loginBtn.disabled = false;
            });
        });

        logoutBtn.addEventListener('click', function() {
          stopPoll();
          fetch('/admin/api/providers/github_copilot/logout', { method: 'POST', credentials: 'same-origin' })
            .then(function(r) { return r.json(); })
            .then(function() {
              flowArea.innerHTML = '';
              if (modelCache['github_copilot']) delete modelCache['github_copilot'];
              refreshAuth();
              if (ctx && ctx.refreshBadge) ctx.refreshBadge();
            });
        });

        refreshAuth();
      },
    },
    'litellm': {
      values: bootstrap.provider_config.litellm,
      fields: [
        { key: 'timeout_seconds', overlay_key: 'litellm_timeout_seconds', label: 'Request timeout (seconds)', type: 'number', step: '1', min: '1', help: 'LiteLLM is an optional shim. Install with `pip install litellm` if you want to use it.' },
      ],
    },
  };

  function renderStubPane(name) {
    var status = (providerStatusCache || {})[name];
    var pane = el('section', { class: 'provider-pane' });
    var head = el('div', { class: 'provider-pane__head' });
    head.appendChild(el('span', { class: 'provider-pane__title' }, [name]));
    var badge = badgeForStatus(status);
    badge.style.marginLeft = '8px';
    head.appendChild(badge);
    pane.appendChild(head);
    var body = el('p', { class: 'muted', style: 'margin-top: 10px;' }, [
      (status && status.reason) || 'This provider has no editable settings — it is wired in code.',
    ]);
    pane.appendChild(body);
    return pane;
  }

  function providerMeta(name, status) {
    if (status && status.is_stub) return 'built-in stub — no editable settings';
    var cfg = (bootstrap.provider_config || {})[name] || {};
    if (cfg.base_url) return cfg.base_url;
    if (name === 'github_copilot') return 'OAuth via gh CLI / device code';
    if (status && status.configured) return 'configured';
    return (status && status.reason) || 'not configured';
  }

  function openProviderModal(name) {
    var schema = PROVIDER_SCHEMAS[name];
    var bodyEl = schema ? renderProviderPane(name, schema) : renderStubPane(name);
    // The pane carries its own header (name + badge) and Save/Test buttons,
    // so strip the card chrome — the modal supplies the surface.
    bodyEl.style.boxShadow = 'none';
    bodyEl.style.border = '0';
    bodyEl.style.padding = '0';
    bodyEl.style.background = 'none';
    openModal({ title: 'Provider settings', body: bodyEl, onClose: renderProviderSummary });
  }

  function renderProviderSummary() {
    providerPanes.innerHTML = '';
    providers.forEach(function(name) {
      var status = (providerStatusCache || {})[name];
      var row = summaryRow(name, providerMeta(name, status), function() { openProviderModal(name); });
      row.insertBefore(badgeForStatus(status), row.querySelector('.summary-row__chev'));
      providerPanes.appendChild(row);
    });
  }

  function openAddProviderModal() {
    var list = el('div', { class: 'summary-list' });
    providers.forEach(function(name) {
      if (!PROVIDER_SCHEMAS[name]) return; // only providers with editable settings
      var status = (providerStatusCache || {})[name];
      var row = summaryRow(name, providerMeta(name, status), function() { m.close(); openProviderModal(name); });
      row.insertBefore(badgeForStatus(status), row.querySelector('.summary-row__chev'));
      list.appendChild(row);
    });
    var info = el('p', { class: 'muted', style: 'margin-bottom: var(--space-3);' }, [
      'Pick an upstream to configure. brains ships a fixed provider registry; choosing one opens its settings.',
    ]);
    var m = openModal({ title: 'Add provider', body: el('div', {}, [info, list]) });
  }

  var addProviderBtn = document.getElementById('add-provider');
  if (addProviderBtn) addProviderBtn.addEventListener('click', openAddProviderModal);

  // Bootstrap rendering: load status + prices BEFORE first paint so
  // badges + price hints show on the first render.
  Promise.all([loadProviderStatus(), loadPrices()]).then(function() {
    renderProviderSummary();
  });

  // ---------- Rate limit ----------

  document.getElementById('save-rate-limit').addEventListener('click', function() {
    var v = parseInt(document.getElementById('rate-limit-input').value || '0', 10);
    if (isNaN(v) || v < 0) { setStatus('rate-limit-status', 'must be 0 or a positive integer', 'bad'); return; }
    setStatus('rate-limit-status', 'saving…', 'pending');
    postJSON({ rate_limit_per_minute: v }).then(function() {
      setStatus('rate-limit-status', 'saved', 'ok');
    }).catch(function(err) { setStatus('rate-limit-status', 'failed: ' + err.message, 'bad'); });
  });

  // ---------- Router toggle ----------

  var routerSave = document.getElementById('save-router');
  if (routerSave) {
    routerSave.addEventListener('click', function() {
      var enabled = !!document.getElementById('router-enabled-input').checked;
      setStatus('router-status', 'saving…', 'pending');
      postJSON({ router: { enabled: enabled } }).then(function() {
        setStatus('router-status', enabled ? 'saved — router ON' : 'saved — router OFF (pass-through)', 'ok');
      }).catch(function(err) { setStatus('router-status', 'failed: ' + err.message, 'bad'); });
    });
  }

  // ---------- Routing map (relationship visualization) ----------

  function renderRoutingMap() {
    var box = document.getElementById('routing-map');
    if (!box) return;
    box.innerHTML = '';
    var routes = bootstrap.routes || {};
    var models = bootstrap.models || {};
    var keys = Object.keys(routes);

    if (!keys.length) {
      box.appendChild(el('p', { class: 'muted' }, ['No routes defined yet — add one in the Routes section below.']));
    }

    keys.forEach(function(key) {
      var tier = routes[key];
      var route = models[tier];
      var modelText = route ? (route.provider + ' · ' + route.model) : (tier + ' — tier not defined');
      var chain = el('div', { class: 'flow-chain' }, [
        el('span', { class: 'flow-node flow-node--route', title: 'route key (classifier output)' }, [key]),
        el('span', { class: 'flow-arrow' }, ['→']),
        el('span', { class: 'flow-node flow-node--tier', title: 'tier' }, [tier]),
        el('span', { class: 'flow-arrow' }, ['→']),
        el('span', {
          class: route ? 'flow-node flow-node--model' : 'flow-node flow-node--model is-broken',
          title: 'resolved provider · model',
        }, [modelText]),
      ]);
      box.appendChild(chain);
    });

    // Surface tiers that exist but no route points at them, so the operator
    // can see unused capacity / dangling tiers at a glance.
    var usedTiers = {};
    keys.forEach(function(k) { usedTiers[routes[k]] = true; });
    var unused = Object.keys(models).filter(function(t) { return !usedTiers[t]; });
    if (unused.length) {
      var legend = el('div', { class: 'flow-legend muted' }, ['Tiers with no route: ']);
      unused.forEach(function(t) {
        var route = models[t];
        legend.appendChild(el('span', { class: 'flow-node flow-node--tier is-muted' }, [
          t + (route ? ' (' + route.provider + ' · ' + route.model + ')' : ''),
        ]));
      });
      box.appendChild(legend);
    }
  }

  var refreshMapBtn = document.getElementById('refresh-map');
  if (refreshMapBtn) refreshMapBtn.addEventListener('click', renderRoutingMap);

  // ---------- Boot ----------

  // Render tiers + routes immediately; provider panes wait for status
  // + prices to land so badges and price hints appear on first paint
  // (the wait is short — both endpoints are pure in-memory reads).
  renderTiers();
  renderRoutes();
  renderRoutingMap();
})();

