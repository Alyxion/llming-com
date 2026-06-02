/*
 * Knowledge-board client. Talks to the backend ONLY through llming-com sessions
 * (LlmingWebSocket + handlers) — never a raw socket. Reads its config from the
 * <body> data-* attributes. The reusable LlmingPairing component handles the
 * host's pairing UI; LlmingWebSocket (inlined alongside this) owns the transport.
 */
(function () {
  'use strict';
  var body = document.body;
  var ROLE = body.dataset.role || 'guest';
  var N = parseInt(body.dataset.grid || '9', 10);
  var WITH_QR = body.dataset.qr === '1';
  var CODE = body.dataset.code || '';
  // Resolve paths against the page base ('/' direct, or the tunnel root).
  var baseEl = document.querySelector('base');
  var base = (baseEl && baseEl.getAttribute('href')) || '/';

  var grid = document.getElementById('grid');
  var tiles = [];
  var sock = null;
  for (var i = 0; i < N; i++) {
    (function (idx) {
      var t = document.createElement('div');
      t.className = 'tile';
      t.textContent = idx + 1;
      t.onclick = function () { if (sock) sock.send({ type: 'board.toggle', toggle: idx, by: ROLE }); };
      grid.appendChild(t);
      tiles.push(t);
    })(i);
  }

  function render(state, lastBy, peers) {
    for (var i = 0; i < N; i++) tiles[i].classList.toggle('on', !!(state && state[i]));
    if (lastBy) document.getElementById('lastby').textContent = lastBy;
    if (peers != null) document.getElementById('peers').textContent = peers;
  }
  function pulse(i) {
    if (i == null || !tiles[i]) return;
    tiles[i].classList.remove('pulse'); void tiles[i].offsetWidth; tiles[i].classList.add('pulse');
  }
  function status(t) { document.getElementById('status').textContent = t; }
  function describeTransport(via) {
    var c = window.__llmingConn;
    if (c && c.transport === 'p2p') return c.path === 'turn' ? 'P2P · TURN relay' : (c.path === 'direct' ? 'P2P · direct' : 'P2P');
    if (c && c.transport === 'proxy') return c.path === 'e2e' ? 'proxy · E2E encrypted' : 'proxy relay';
    if (via === 'proxy') return 'proxy relay';
    if (via === 'p2p') return 'P2P';
    return via || 'direct';
  }

  fetch(base + 'api/info', { cache: 'no-store' })
    .then(function (r) { return r.json(); })
    .then(function (d) { document.getElementById('transport').textContent = describeTransport(d.forwarded_via); })
    .catch(function () {});

  // Sign out: only meaningful through the llming-com viewer (window.LlmingPublish).
  var signoutEl = document.getElementById('signout');
  if (window.LlmingPublish) {
    signoutEl.style.display = '';
    signoutEl.onclick = function () { window.LlmingPublish.signOut().then(function () { location.reload(); }); };
  }

  // Drop-in pairing UI (host view only): the "pair a device" button pops the
  // invite QR; the host-screen code pops while a device is connecting.
  if (WITH_QR && window.LlmingPairing) {
    window.LlmingPairing.mount({
      button: '#pairbtn',
      inviteQrUrl: base + 'qr.svg',  // the invite QR carries the key in its #sk fragment
      code: CODE,
      revertMs: 16000,
    });
  }

  // Server → client calls: session.call("board.<method>", ...args).
  var methods = {
    update: function (d) { render(d.state, d.last_by, d.peers); pulse(d.changed); },
    pairing: function (state) { if (window.LlmingPairing) window.LlmingPairing.onPairing(state); },
  };

  (async function () {
    var info;
    try { info = await (await fetch(base + 'api/session', { cache: 'no-store' })).json(); }
    catch (e) { status('connection failed'); return; }
    sock = new LlmingWebSocket(info.wsUrl, {
      showBanner: false,
      onOpen: function () { status('live'); },
      onReconnecting: function () { status('reconnecting…'); },
      onReconnected: function () { status('live'); },
      onSessionLost: function () { status('disconnected'); },
      onMessage: function (msg) {
        if (msg && msg.type === 'llming.call' && msg.target === 'board' && methods[msg.method]) {
          methods[msg.method].apply(null, msg.args || []);
        }
      },
    });
    sock.connect();
  })();
})();
