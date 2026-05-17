(() => {
  const DB_NAME = "llming-p2p";
  const DB_VERSION = 1;
  const STORE_NAME = "pairings";
  const CURRENT_KEY = "llming-p2p-current-pairing";

  function readConfig() {
    const defaults = {
      redeemUrl: "/p2p/api/pair/redeem",
      handshakeUrl: "",
      appUrl: "/p2p/app",
      pairParam: "pt",
      autoConnect: false,
    };
    const node = document.getElementById("llming-p2p-config");
    if (!node) return defaults;
    try {
      return { ...defaults, ...JSON.parse(node.textContent || "{}") };
    } catch {
      return defaults;
    }
  }

  function emit(name, detail) {
    window.dispatchEvent(new CustomEvent(`llming-p2p:${name}`, { detail }));
  }

  function setStatus(text, tone = "") {
    const node = document.querySelector("[data-p2p-status]");
    if (!node) return;
    node.textContent = text;
    node.dataset.tone = tone;
  }

  function openDb() {
    if (!("indexedDB" in window)) return Promise.resolve(null);
    return new Promise((resolve, reject) => {
      const request = indexedDB.open(DB_NAME, DB_VERSION);
      request.onupgradeneeded = () => {
        const db = request.result;
        if (!db.objectStoreNames.contains(STORE_NAME)) {
          db.createObjectStore(STORE_NAME, { keyPath: "id" });
        }
      };
      request.onsuccess = () => resolve(request.result);
      request.onerror = () => reject(request.error);
    });
  }

  async function withStore(mode, callback) {
    const db = await openDb();
    if (!db) return callback(null);
    return new Promise((resolve, reject) => {
      const tx = db.transaction(STORE_NAME, mode);
      const store = tx.objectStore(STORE_NAME);
      Promise.resolve(callback(store)).then(resolve, reject);
      tx.onerror = () => reject(tx.error);
    }).finally(() => db.close());
  }

  async function putPairing(pairing) {
    const now = new Date().toISOString();
    const id = pairing.id || pairing.room_id || crypto.randomUUID();
    const record = { ...pairing, id, updated_at: now };
    await withStore("readwrite", (store) => {
      if (!store) {
        localStorage.setItem(`llming-p2p-pairing:${id}`, JSON.stringify(record));
        return undefined;
      }
      return new Promise((resolve, reject) => {
        const req = store.put(record);
        req.onsuccess = () => resolve();
        req.onerror = () => reject(req.error);
      });
    });
    localStorage.setItem(CURRENT_KEY, id);
    return record;
  }

  async function getCurrentPairing() {
    const id = localStorage.getItem(CURRENT_KEY);
    if (!id) return null;
    return withStore("readonly", (store) => {
      if (!store) {
        const raw = localStorage.getItem(`llming-p2p-pairing:${id}`);
        return raw ? JSON.parse(raw) : null;
      }
      return new Promise((resolve, reject) => {
        const req = store.get(id);
        req.onsuccess = () => resolve(req.result || null);
        req.onerror = () => reject(req.error);
      });
    });
  }

  async function listPairings() {
    return withStore("readonly", (store) => {
      if (!store) {
        const rows = [];
        for (let i = 0; i < localStorage.length; i += 1) {
          const key = localStorage.key(i);
          if (key && key.startsWith("llming-p2p-pairing:")) {
            rows.push(JSON.parse(localStorage.getItem(key)));
          }
        }
        return rows;
      }
      return new Promise((resolve, reject) => {
        const req = store.getAll();
        req.onsuccess = () => resolve(req.result || []);
        req.onerror = () => reject(req.error);
      });
    });
  }

  function tokenFromHash(config) {
    const params = new URLSearchParams(window.location.hash.replace(/^#/, ""));
    return params.get(config.pairParam) || params.get("pairing_token") || "";
  }

  async function postJson(url, body) {
    const res = await fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      credentials: "include",
      body: JSON.stringify(body),
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) {
      const message = data.error || data.message || `request failed (${res.status})`;
      throw new Error(message);
    }
    return data;
  }

  async function sha256Hex(value) {
    const bytes = new TextEncoder().encode(value);
    const digest = await crypto.subtle.digest("SHA-256", bytes);
    return [...new Uint8Array(digest)].map((b) => b.toString(16).padStart(2, "0")).join("");
  }

  function joinUrl(base, path) {
    return `${base.replace(/\/+$/, "")}/${path.replace(/^\/+/, "")}`;
  }

  async function connectViaRelay(pairing) {
    const relayEndpoint = pairing.relay_endpoint || pairing.relayEndpoint;
    const roomId = pairing.room_id || pairing.roomId;
    const deviceCredential = pairing.device_credential || pairing.deviceCredential || pairing.device_token || pairing.deviceToken;
    if (!relayEndpoint || !roomId || !deviceCredential) {
      throw new Error("pairing is missing relay endpoint, room id, or device credential");
    }
    const hash = await sha256Hex(deviceCredential);
    const connectUrl = joinUrl(relayEndpoint, `${encodeURIComponent(roomId)}/connect`);
    const responseUrl = joinUrl(relayEndpoint, `${encodeURIComponent(roomId)}/response?h=${encodeURIComponent(hash)}`);
    const connect = await postJson(connectUrl, { device_token_hash: hash });
    const pollMs = Math.max(1000, Number(connect.config?.app_poll_interval_ms || pairing.app_poll_interval_ms || 3000));
    const deadline = Date.now() + Math.max(10_000, Number(connect.config?.request_ttl_ms || pairing.request_ttl_ms || 60_000));
    while (Date.now() < deadline) {
      const res = await fetch(responseUrl, { credentials: "include" });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(data.error || `relay response failed (${res.status})`);
      if (data.url) return data.url;
      await new Promise((resolve) => setTimeout(resolve, pollMs));
    }
    throw new Error("handshake timed out");
  }

  async function redeemPairingToken() {
    const config = readConfig();
    const pairingToken = tokenFromHash(config);
    if (!pairingToken) {
      setStatus("Missing pairing token.", "error");
      throw new Error("missing pairing token");
    }
    history.replaceState(null, "", window.location.pathname + window.location.search);
    setStatus("Pairing device...");
    const data = await postJson(config.redeemUrl, { pairing_token: pairingToken });
    const pairing = await putPairing(data.pairing || data);
    emit("paired", pairing);
    setStatus("Paired. Opening app...");
    window.location.assign(pairing.app_url || config.appUrl);
  }

  async function startHandshake(pairing) {
    const config = readConfig();
    const selected = pairing || await getCurrentPairing();
    if (!selected) {
      setStatus("No paired device credential found.", "error");
      throw new Error("no paired device credential found");
    }
    setStatus("Preparing handshake...");
    const targetUrl = config.handshakeUrl
      ? (await postJson(config.handshakeUrl, { pairing_id: selected.id })).url
      : await connectViaRelay(selected);
    if (!targetUrl) throw new Error("handshake did not return a URL");
    emit("handshake", { pairing: selected, url: targetUrl });
    window.location.assign(targetUrl);
  }

  async function initApp() {
    const config = readConfig();
    const pairings = await listPairings();
    emit("ready", { pairings });
    const title = document.querySelector("[data-p2p-title]");
    const current = pairings.find((p) => p.id === localStorage.getItem(CURRENT_KEY)) || pairings[0] || null;
    if (title && current?.app?.name) title.textContent = current.app.name;
    if (!current) {
      setStatus("Pair this device before starting a handshake.", "error");
      return;
    }
    setStatus("Ready.");
    const button = document.querySelector("[data-p2p-connect]");
    if (button) {
      button.disabled = false;
      button.addEventListener("click", () => {
        button.disabled = true;
        startHandshake(current).catch((error) => {
          button.disabled = false;
          setStatus(error.message, "error");
        });
      });
    }
    if (config.autoConnect) await startHandshake(current);
  }

  window.LlmingP2PViewer = {
    getCurrentPairing,
    listPairings,
    putPairing,
    redeemPairingToken,
    startHandshake,
    initApp,
  };
})();
