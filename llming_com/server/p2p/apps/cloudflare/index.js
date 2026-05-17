const DEFAULT_ENABLED_UNTIL = "1970-01-01T00:00:00Z";

function jsonResponse(data, status = 200) {
  return new Response(JSON.stringify(data), {
    status,
    headers: {
      "Content-Type": "application/json",
      "Access-Control-Allow-Origin": "*",
      "Access-Control-Allow-Headers": "Content-Type",
    },
  });
}

function htmlResponse(body, status = 200) {
  return new Response(body, {
    status,
    headers: {
      "Content-Type": "text/html; charset=utf-8",
      "Cache-Control": "no-store",
    },
  });
}

function testAppsDisabled(env) {
  if (env.LLMING_TEST_APPS !== "1") return false;
  const untilMs = Date.parse(env.LLMING_TEST_APPS_ENABLED_UNTIL || DEFAULT_ENABLED_UNTIL);
  return !Number.isFinite(untilMs) || Date.now() >= untilMs;
}

function appConfig(env) {
  return {
    relay_endpoint: env.LLMING_TEST_RELAY_ENDPOINT || "https://test-relay.example.com",
    room_id: env.LLMING_TEST_ROOM || "llming-test-room",
    pairing_token: env.LLMING_TEST_PAIRING_TOKEN || "llming-test-pair",
    device_credential: env.LLMING_TEST_DEVICE_CREDENTIAL || "llming-test-device",
  };
}

function shell(env) {
  const config = appConfig(env);
  return `<!doctype html>
<html lang="en">
  <head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <meta name="robots" content="noindex">
    <title>llming apps</title>
    <style>
      :root { color-scheme: light dark; font-family: system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; }
      body { margin: 0; min-height: 100vh; display: grid; place-items: center; background: Canvas; color: CanvasText; }
      main { width: min(42rem, calc(100vw - 2rem)); }
      h1 { margin: 0 0 1rem; font-size: 1.6rem; }
      ul { padding: 0; list-style: none; display: grid; gap: .75rem; }
      li { border: 1px solid color-mix(in srgb, CanvasText 20%, transparent); border-radius: .5rem; padding: 1rem; }
      button { min-height: 2.5rem; padding: 0 .9rem; border-radius: .375rem; border: 1px solid ButtonBorder; background: ButtonFace; color: ButtonText; font: inherit; }
      code { word-break: break-all; }
    </style>
  </head>
  <body>
    <main>
      <h1>Recent apps</h1>
      <ul id="apps"><li>Loading...</li></ul>
      <p id="status"></p>
    </main>
    <script>
      const cfg = ${JSON.stringify(config)};
      async function loadApps() {
        const res = await fetch('/p2p/api/apps/recent', { credentials: 'include' });
        const data = await res.json();
        document.getElementById('apps').innerHTML = data.apps.map(app => '<li><strong>' + app.name + '</strong><br><code>' + app.id + '</code><br><button data-room="' + app.room_id + '">Continue</button></li>').join('');
        document.querySelectorAll('button[data-room]').forEach((button) => {
          button.addEventListener('click', () => {
            document.getElementById('status').textContent = 'Ready to request a fresh handshake for ' + button.dataset.room;
          });
        });
      }
      loadApps().catch((error) => {
        document.getElementById('apps').innerHTML = '<li>' + error.message + '</li>';
      });
    </script>
  </body>
</html>`;
}

function pairPage() {
  return `<!doctype html>
<html lang="en">
  <head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <meta name="robots" content="noindex">
    <title>Pair llming app</title>
  </head>
  <body>
    <p id="status">Pairing...</p>
    <script>
      const params = new URLSearchParams(location.hash.replace(/^#/, ''));
      const token = params.get('pt') || params.get('pairing_token') || '';
      history.replaceState(null, '', location.pathname + location.search);
      fetch('/p2p/api/pair/redeem', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        credentials: 'include',
        body: JSON.stringify({ pairing_token: token }),
      }).then(async (res) => {
        const data = await res.json();
        if (!res.ok) throw new Error(data.error || 'pairing failed');
        localStorage.setItem('llming-p2p-current-pairing', data.pairing.id);
        localStorage.setItem('llming-p2p-pairing:' + data.pairing.id, JSON.stringify(data.pairing));
        location.assign(data.pairing.app_url || '/');
      }).catch((error) => {
        document.getElementById('status').textContent = error.message;
      });
    </script>
  </body>
</html>`;
}

async function redeem(request, env) {
  if (testAppsDisabled(env)) return jsonResponse({ error: "test apps disabled" }, 403);
  const config = appConfig(env);
  const body = await request.json().catch(() => ({}));
  if (body.pairing_token !== config.pairing_token) {
    return jsonResponse({ error: "invalid pairing token" }, 401);
  }
  const pairing = {
    id: "llming-test-app",
    room_id: config.room_id,
    relay_endpoint: config.relay_endpoint,
    device_credential: config.device_credential,
    app_url: "/",
    app: {
      id: "llming-test-app",
      name: "llming test app",
    },
  };
  return jsonResponse({
    pairing,
    room_id: pairing.room_id,
    relay_endpoint: pairing.relay_endpoint,
    app: pairing.app,
  });
}

function recentApps(env) {
  const config = appConfig(env);
  return jsonResponse({
    apps: [
      {
        id: "llming-test-app",
        name: "llming test app",
        room_id: config.room_id,
        relay_endpoint: config.relay_endpoint,
      },
    ],
  });
}

export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    if (request.method === "OPTIONS") return jsonResponse({});
    if (url.pathname === "/health") {
      return jsonResponse({
        ok: true,
        status: "ok",
        service: "llming-p2p-apps",
        disabled: testAppsDisabled(env),
      });
    }
    if (url.pathname === "/" || url.pathname === "/p2p/app") {
      if (testAppsDisabled(env)) return htmlResponse("<p>test apps disabled</p>", 403);
      return htmlResponse(shell(env));
    }
    if (url.pathname === "/p2p/pair") return htmlResponse(pairPage());
    if (url.pathname === "/p2p/api/pair/redeem" && request.method === "POST") return redeem(request, env);
    if (url.pathname === "/p2p/api/apps/recent") {
      if (testAppsDisabled(env)) return jsonResponse({ error: "test apps disabled" }, 403);
      return recentApps(env);
    }
    return jsonResponse({ error: "not found" }, 404);
  },
};
