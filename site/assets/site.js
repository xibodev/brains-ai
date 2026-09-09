// Preserve bookmarks to the former single-page tabs and generated release history.
if (location.pathname.endsWith('/') || location.pathname.endsWith('/index.html')) {
  const legacy = {
    'i-pipx': 'quickstart.html#install', 'i-pip': 'quickstart.html#install',
    'i-uv': 'quickstart.html#install', 'i-src': 'quickstart.html#install',
    'v-state': 'quickstart.html#verify', 'v-ready': 'operations.html#diagnose',
    'v-audit': 'operations.html#trust', 'c-life': 'cli.html#lifecycle',
    'c-sess': 'cli.html#coordination', 'c-work': 'cli.html#coordination',
    'c-comm': 'cli.html#communication', 'c-gov': 'cli.html#governance',
    'c-ops': 'cli.html#operations', 'm-auto': 'connecting.html#wire',
    'm-claude': 'connecting.html#authentication', 'm-codex': 'connecting.html#authentication'
  };
  function followLegacyHash() {
    const hash = location.hash.slice(1);
    const target = /^release-v\d+(?:-\d+){1,3}$/.test(hash)
      ? 'releases.html#' + hash : legacy[hash];
    if (target) location.replace(target);
  }
  followLegacyHash();
  window.addEventListener('hashchange', followLegacyHash);
}
