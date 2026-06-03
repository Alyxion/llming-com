/*
 * LlmingPairing — a drop-in, dependency-free host-side pairing UI component.
 *
 * Shows ONE QR. Scanning it (the invite) opens the app AND carries the security
 * key in the URL #fragment (never sent to the server), so a single scan pairs
 * end-to-end. The key is also shown as text for desktop users who type it.
 *
 *   LlmingPairing.mount({
 *     button:     '#pairbtn',   // element (or selector) that opens the popup
 *     inviteQrUrl:'/qr.svg',    // QR of the invite (carries the key in #sk=…)
 *     code:       'ABCD-…',     // the security key, shown as copy-pastable text
 *     revertMs:   16000,        // auto-hide after this when a device is connecting
 *   });
 *   LlmingPairing.onPairing('requested'); // a device connected → pop it up
 *   LlmingPairing.onPairing('connected'); // it's in → hide
 *
 * Host-side UI only — no transport, no dependencies.
 */
(function (g) {
  'use strict';
  var el = null, timer = null, cfg = {}, revertMs = 16000;

  function build() {
    el = document.createElement('div');
    el.setAttribute('style', 'position:fixed;right:20px;bottom:20px;z-index:2147483646;background:#fff;color:#0d1117;border-radius:18px;box-shadow:0 12px 48px rgba(0,0,0,.5);padding:18px;width:250px;text-align:center;font-family:system-ui,sans-serif;animation:lpp-in .18s ease-out');
    el.innerHTML =
      '<style>@keyframes lpp-in{from{transform:translateY(12px);opacity:0}to{transform:none;opacity:1}}</style>' +
      '<div style="font-size:14px;font-weight:700;margin-bottom:2px">📲 Scan to join</div>' +
      '<div style="font-size:11px;color:#475569;margin-bottom:10px">one scan opens & pairs your phone</div>' +
      '<div class="lpp-qr" id="lpp-qr" style="display:flex;justify-content:center;min-height:170px"></div>' +
      (cfg.code
        ? '<div style="font-size:11px;color:#475569;margin-top:10px">or type the security key</div>' +
          '<div style="display:flex;align-items:center;justify-content:center;gap:6px;margin-top:3px;max-width:100%">' +
            '<span class="lpp-key" style="font-family:ui-monospace,monospace;font-size:9px;font-weight:600;white-space:nowrap;overflow-x:auto;letter-spacing:0;user-select:all">' + cfg.code + '</span>' +
            '<button class="lpp-copy" aria-label="Copy" title="Copy" style="flex:none;cursor:pointer;border:1px solid #cbd5e1;background:#f1f5f9;color:#0d1117;border-radius:6px;padding:1px 6px;font-size:12px;line-height:1.4">⧉</button>' +
          '</div>'
        : '');
    document.body.appendChild(el);
    var copyBtn = el.querySelector('.lpp-copy');
    if (copyBtn) copyBtn.onclick = function () {
      var done = function () { copyBtn.textContent = '✓'; setTimeout(function () { if (copyBtn) copyBtn.textContent = '⧉'; }, 1200); };
      var fallback = function () {  // execCommand path for blocked/absent clipboard API
        try {
          var s = el.querySelector('.lpp-key'), r = document.createRange();
          r.selectNodeContents(s); var sel = getSelection(); sel.removeAllRanges(); sel.addRange(r);
          document.execCommand('copy'); done();
        } catch (e) {}
      };
      if (navigator.clipboard && navigator.clipboard.writeText) navigator.clipboard.writeText(cfg.code).then(done).catch(fallback);
      else fallback();
    };
    if (cfg.inviteQrUrl) {
      fetch(cfg.inviteQrUrl).then(function (r) { return r.text(); }).then(function (svg) {
        var box = el && el.querySelector('#lpp-qr');
        if (!box) return;
        box.innerHTML = svg;
        var s = box.querySelector('svg');
        if (s) s.setAttribute('style', 'width:180px;height:180px');
      }).catch(function () {});
    }
  }

  function show() { if (!el) build(); }
  function hide() { if (el) { el.remove(); el = null; } }
  function toggle() { if (el) hide(); else show(); }

  function mount(opts) {
    cfg = opts || {};
    revertMs = cfg.revertMs || 16000;
    var btn = (typeof cfg.button === 'string') ? document.querySelector(cfg.button) : cfg.button;
    if (btn) { btn.style.display = ''; btn.onclick = toggle; }
  }

  // A device is connecting → pop the QR (auto-hide after revertMs if it never
  // finishes); once it's in, hide.
  function onPairing(state) {
    if (timer) { clearTimeout(timer); timer = null; }
    if (state === 'requested') { show(); timer = setTimeout(hide, revertMs); }
    else hide();
  }

  g.LlmingPairing = { mount: mount, onPairing: onPairing, show: show, hide: hide, toggle: toggle, get visible() { return !!el; } };
})(window);
