/**
 * WebSocket signaling relay + HTTP mailbox for llming P2P connections.
 *
 * Two modes of operation per room (Durable Object):
 *
 * 1. WebSocket relay (existing): Two peers exchange ~4KB SDP data, then disconnect.
 *    Usage: wss://relay.openhort.ai/{room-id}
 *
 * 2. HTTP mailbox (new): Polling-based connection brokering.
 *    - App posts connection wish:  POST /{room}/connect
 *    - Host polls for wishes:      GET  /{room}/pending
 *    - Host posts P2P URL:         POST /{room}/respond
 *    - App polls for URL:          GET  /{room}/response?h=HASH
 *
 * Deploy as a Cloudflare Worker at your relay domain
 */

const REQUEST_TTL_MS = 60_000; // 60s — pending requests expire
const RESPONSE_TTL_MS = 60_000; // 60s — responses expire
const CLEANUP_INTERVAL_MS = 60_000; // alarm interval

const DEFAULT_CONFIG = {
  poll_interval_ms: 5000, // host polling (premium default, will be per-account later)
  app_poll_interval_ms: 3000, // app polling during connection
  request_ttl_ms: REQUEST_TTL_MS,
};


const ROOM_GRANT_TTL_MS = 60 * 60 * 1000; // 1h default room grant
const ROOM_GRANT_MAX_TTL_MS = 24 * 60 * 60 * 1000; // 24h max room grant
const MAX_PENDING_REQUESTS = 32;

function corsHeaders() {
  return {
    'Content-Type': 'application/json',
    'Access-Control-Allow-Origin': '*',
    'Access-Control-Allow-Headers': 'Content-Type, Authorization, X-Llming-Admission-Key, X-OpenHort-Admission-Key',
  };
}

function jsonResponse(data, status = 200) {
  return new Response(JSON.stringify(data), { status, headers: corsHeaders() });
}

function extractAdmissionKey(request) {
  const auth = request.headers.get('Authorization') || '';
  const bearer = auth.match(/^Bearer\s+(.+)$/i);
  if (bearer) return bearer[1].trim();
  return (request.headers.get('X-Llming-Admission-Key') || request.headers.get('X-OpenHort-Admission-Key') || '').trim();
}

function configuredAdmissionHashes(env) {
  const raw = env.HOST_ADMISSION_KEY_HASHES || env.LLMING_ADMISSION_KEY_HASHES || env.OPENHORT_ADMISSION_KEY_HASHES || '';
  return raw.split(',').map(v => v.trim()).filter(Boolean);
}

function constantTimeEqual(a, b) {
  if (a.length !== b.length) return false;
  let diff = 0;
  for (let i = 0; i < a.length; i++) diff |= a.charCodeAt(i) ^ b.charCodeAt(i);
  return diff === 0;
}

async function sha256Hex(value) {
  const data = new TextEncoder().encode(value);
  const digest = await crypto.subtle.digest('SHA-256', data);
  return [...new Uint8Array(digest)].map(b => b.toString(16).padStart(2, '0')).join('');
}

async function authorizeHost(request, env) {
  const hashes = configuredAdmissionHashes(env);
  if (hashes.length === 0) return { ok: false, reason: 'admission is not configured' };
  const key = extractAdmissionKey(request);
  if (!key) return { ok: false, reason: 'missing admission key' };
  const candidate = `sha256:${await sha256Hex(key)}`;
  for (const hash of hashes) {
    if (constantTimeEqual(candidate, hash)) return { ok: true, key_hash: candidate };
  }
  return { ok: false, reason: 'invalid admission key' };
}

function testRelayDisabled(request, env) {
  const host = new URL(request.url).hostname;
  const isTestHost = host.startsWith('test-relay.') || env.LLMING_TEST_RELAY === '1' || env.OPENHORT_TEST_RELAY === '1';
  if (!isTestHost) return false;
  const until = env.LLMING_TEST_RELAY_ENABLED_UNTIL || env.OPENHORT_TEST_RELAY_ENABLED_UNTIL || '';
  if (!until) return true;
  const untilMs = Date.parse(until);
  return !Number.isFinite(untilMs) || Date.now() >= untilMs;
}

function safeTtlMs(value) {
  const requested = Number(value || ROOM_GRANT_TTL_MS);
  if (!Number.isFinite(requested) || requested <= 0) return ROOM_GRANT_TTL_MS;
  return Math.min(requested, ROOM_GRANT_MAX_TTL_MS);
}

// ====== Durable Object: per-room state ======

export class SignalRelay {
  constructor(state, env) {
    this.state = state;
    this.env = env;
    // In-memory ephemeral mailbox — lost on eviction, which is fine (60s TTL data)
    this.pending = new Map(); // device_token_hash → {device_token_hash, timestamp}
    this.responses = new Map(); // device_token_hash → {device_token_hash, url, timestamp}
    // SDP bridge: viewer WebSocket messages buffered for host HTTP polling
    this.sdpInbox = []; // messages FROM viewer WebSocket, FOR host HTTP
    this.sdpOutbox = []; // messages FROM host HTTP, FOR viewer WebSocket
    // Rate limiting
    this._lastPendingPoll = 0;
  }

  async fetch(request) {
    const url = new URL(request.url);
    const upgrade = request.headers.get('Upgrade');

    if (testRelayDisabled(request, this.env)) {
      return jsonResponse({ error: 'test relay disabled' }, 403);
    }

    // WebSocket upgrade — viewer SDP path. Allowed only for registered rooms.
    if (upgrade === 'websocket') {
      const grant = await this._getActiveGrant();
      if (!grant) return jsonResponse({ error: 'room is not registered' }, 403);
      const [client, server] = Object.values(new WebSocketPair());
      this.state.acceptWebSocket(server);
      return new Response(null, { status: 101, webSocket: client });
    }

    // HTTP mailbox endpoints
    const parts = url.pathname.split('/').filter(Boolean);
    // Path: /{room_id}/{action} — the room_id is already resolved by the outer fetch,
    // so here we just need the action (last segment)
    const action = parts[parts.length - 1];

    if (request.method === 'POST' && action === 'register') {
      return this._handleRegister(request, parts);
    }
    if (request.method === 'POST' && action === 'connect') {
      return this._handleConnect(request);
    }
    if (request.method === 'GET' && action === 'pending') {
      return this._handlePending(request);
    }
    if (request.method === 'POST' && action === 'respond') {
      return this._handleRespond(request);
    }
    if (request.method === 'GET' && action === 'response') {
      const h = url.searchParams.get('h') || '';
      return this._handleResponse(h);
    }
    if (request.method === 'GET' && action === 'config') {
      return this._handleConfig();
    }
    // SDP bridge: host polls for viewer's WebSocket messages via HTTP
    if (request.method === 'GET' && action === 'sdp-inbox') {
      return this._handleSdpInbox(request);
    }
    // SDP bridge: host sends message to viewer's WebSocket via HTTP
    if (request.method === 'POST' && action === 'sdp-send') {
      return this._handleSdpSend(request);
    }

    return jsonResponse({ error: 'unknown action' }, 404);
  }

  // --- WebSocket relay (unchanged) ---

  async webSocketMessage(ws, message) {
    // Forward to other WebSocket peers (existing behavior)
    const sockets = this.state.getWebSockets();
    let forwarded = false;
    for (const peer of sockets) {
      if (peer !== ws) {
        try {
          peer.send(message);
          forwarded = true;
        } catch (e) {}
      }
    }
    // If no WebSocket peer received it, buffer for HTTP polling (SDP bridge)
    if (!forwarded && typeof message === 'string') {
      try {
        const parsed = JSON.parse(message);
        this.sdpInbox.push(parsed);
        // Also flush any pending outbox messages to this WebSocket
        while (this.sdpOutbox.length > 0) {
          const msg = this.sdpOutbox.shift();
          try { ws.send(JSON.stringify(msg)); } catch (e) {}
        }
      } catch (e) {}
    }
  }

  async webSocketClose(ws) {}
  async webSocketError(ws) {}

  // --- SDP bridge (WebSocket ↔ HTTP) ---

  async _getActiveGrant() {
    const grant = await this.state.storage.get('room_grant');
    if (!grant || grant.status !== 'active' || Date.now() >= grant.expires_at) {
      if (grant) await this.state.storage.delete('room_grant');
      return null;
    }
    return grant;
  }

  async _requireHost(request) {
    const auth = await authorizeHost(request, this.env);
    if (!auth.ok) return { error: jsonResponse({ error: auth.reason }, auth.reason === 'admission is not configured' ? 503 : 401) };
    return { auth };
  }

  async _requireActiveRoom() {
    const grant = await this._getActiveGrant();
    if (!grant) return { error: jsonResponse({ error: 'room is not registered' }, 403) };
    return { grant };
  }

  async _handleRegister(request, parts) {
    const host = await this._requireHost(request);
    if (host.error) return host.error;
    let body = {};
    try { body = await request.json(); } catch (e) {}
    const ttlMs = safeTtlMs(body.ttl_ms);
    const room = parts.length >= 2 ? parts[parts.length - 2] : parts[0] || '';
    const grant = {
      status: 'active',
      room,
      key_hash: host.auth.key_hash,
      app_id: String(body.app_id || ''),
      app_name: String(body.app_name || ''),
      created_at: Date.now(),
      expires_at: Date.now() + ttlMs,
    };
    await this.state.storage.put('room_grant', grant);
    return jsonResponse({ ok: true, room, expires_at: grant.expires_at, config: DEFAULT_CONFIG });
  }

  async _handleConfig() {
    const active = await this._requireActiveRoom();
    if (active.error) return active.error;
    return jsonResponse({ config: DEFAULT_CONFIG, room_expires_at: active.grant.expires_at });
  }

  async _handleSdpInbox(request) {
    const host = await this._requireHost(request);
    if (host.error) return host.error;
    const active = await this._requireActiveRoom();
    if (active.error) return active.error;
    // Host polls: get all buffered messages from viewer's WebSocket
    const messages = this.sdpInbox.splice(0);
    return jsonResponse({ messages });
  }

  async _handleSdpSend(request) {
    const host = await this._requireHost(request);
    if (host.error) return host.error;
    const active = await this._requireActiveRoom();
    if (active.error) return active.error;
    // Host sends: forward message to viewer's WebSocket
    try {
      const msg = await request.json();
      // Try to send to any connected WebSocket peer
      const sockets = this.state.getWebSockets();
      let sent = false;
      for (const peer of sockets) {
        try {
          peer.send(JSON.stringify(msg));
          sent = true;
        } catch (e) {}
      }
      if (!sent) {
        // No WebSocket peer connected yet — buffer for when they connect
        this.sdpOutbox.push(msg);
      }
      return jsonResponse({ ok: true, sent });
    } catch (e) {
      return jsonResponse({ error: 'invalid body' }, 400);
    }
  }

  // --- HTTP mailbox handlers ---

  async _handleConnect(request) {
    const active = await this._requireActiveRoom();
    if (active.error) return active.error;
    if (this.pending.size >= MAX_PENDING_REQUESTS) {
      return jsonResponse({ error: 'too many pending requests' }, 429);
    }
    try {
      const body = await request.json();
      const hash = body.device_token_hash;
      if (!hash || typeof hash !== 'string') {
        return jsonResponse({ error: 'device_token_hash required' }, 400);
      }
      this.pending.set(hash, { device_token_hash: hash, timestamp: Date.now() });
      this._scheduleCleanup();
      return jsonResponse({ ok: true, config: DEFAULT_CONFIG });
    } catch (e) {
      return jsonResponse({ error: 'invalid body' }, 400);
    }
  }

  async _handlePending(request) {
    const host = await this._requireHost(request);
    if (host.error) return host.error;
    const active = await this._requireActiveRoom();
    if (active.error) return active.error;
    // Rate limit: enforce minimum poll interval
    const now = Date.now();
    if (this._lastPendingPoll && (now - this._lastPendingPoll) < DEFAULT_CONFIG.poll_interval_ms * 0.8) {
      // Polling too fast — return empty, don't penalize yet (just ignore)
      return jsonResponse({ requests: [], config: DEFAULT_CONFIG, throttled: true });
    }
    this._lastPendingPoll = now;

    this._cleanup();
    const requests = [];
    for (const [hash, entry] of this.pending) {
      requests.push(entry);
    }
    // Clear after reading — host consumes the requests
    this.pending.clear();
    return jsonResponse({ requests, config: DEFAULT_CONFIG });
  }

  async _handleRespond(request) {
    const host = await this._requireHost(request);
    if (host.error) return host.error;
    const active = await this._requireActiveRoom();
    if (active.error) return active.error;
    try {
      const body = await request.json();
      const hash = body.device_token_hash;
      const url = body.url;
      if (!hash || !url) {
        return jsonResponse({ error: 'device_token_hash and url required' }, 400);
      }
      // Remove from pending if still there
      this.pending.delete(hash);
      // Store response for the app to pick up
      this.responses.set(hash, { device_token_hash: hash, url, timestamp: Date.now() });
      this._scheduleCleanup();
      return jsonResponse({ ok: true });
    } catch (e) {
      return jsonResponse({ error: 'invalid body' }, 400);
    }
  }

  async _handleResponse(hash) {
    const active = await this._requireActiveRoom();
    if (active.error) return active.error;
    this._cleanup();
    if (!hash) {
      return jsonResponse({ error: 'h parameter required' }, 400);
    }
    const entry = this.responses.get(hash);
    if (entry) {
      // One-time read — delete after returning
      this.responses.delete(hash);
      return jsonResponse({ url: entry.url, config: DEFAULT_CONFIG });
    }
    return jsonResponse({ url: null, config: DEFAULT_CONFIG });
  }

  // --- Cleanup ---

  _cleanup() {
    const now = Date.now();
    for (const [hash, entry] of this.pending) {
      if (now - entry.timestamp > REQUEST_TTL_MS) this.pending.delete(hash);
    }
    for (const [hash, entry] of this.responses) {
      if (now - entry.timestamp > RESPONSE_TTL_MS) this.responses.delete(hash);
    }
  }

  _scheduleCleanup() {
    // Schedule alarm for periodic cleanup (Durable Object feature)
    try {
      this.state.storage.setAlarm(Date.now() + CLEANUP_INTERVAL_MS);
    } catch (e) {
      // Alarm already scheduled or not available
    }
  }

  async alarm() {
    this._cleanup();
    // Re-schedule if there's still data
    if (this.pending.size > 0 || this.responses.size > 0) {
      this._scheduleCleanup();
    }
  }
}

// ====== Worker entry point ======

export default {
  async fetch(request, env) {
    const url = new URL(request.url);

    // CORS preflight
    if (request.method === 'OPTIONS') {
      return new Response(null, {
        headers: {
          'Access-Control-Allow-Origin': '*',
          'Access-Control-Allow-Methods': 'GET, POST, OPTIONS',
          'Access-Control-Allow-Headers': 'Content-Type, Authorization, X-Llming-Admission-Key, X-OpenHort-Admission-Key',
        },
      });
    }

    if (testRelayDisabled(request, env)) {
      return jsonResponse({ error: 'test relay disabled' }, 403);
    }

    // Health check
    if (url.pathname === '/' || url.pathname === '/health') {
      return new Response(JSON.stringify({ status: 'ok', service: 'llming-relay' }), {
        headers: { 'Content-Type': 'application/json', 'Access-Control-Allow-Origin': '*' },
      });
    }

    // Parse: /{room_id} or /{room_id}/{action}
    const parts = url.pathname.slice(1).split('/');
    const roomId = parts[0] || 'default';

    // Route to the room's Durable Object (handles both WebSocket and HTTP)
    const id = env.RELAY.idFromName(roomId);
    const stub = env.RELAY.get(id);

    // For WebSocket upgrades, check the header
    const upgrade = request.headers.get('Upgrade');
    if (upgrade === 'websocket' && parts.length === 1) {
      // Direct WebSocket to room (existing SDP relay path)
      return stub.fetch(request);
    }

    // HTTP mailbox — must have an action
    if (parts.length >= 2) {
      return stub.fetch(request);
    }

    // Room ID only, no action, no WebSocket — 404
    return new Response(JSON.stringify({ error: 'action required' }), {
      status: 404,
      headers: { 'Content-Type': 'application/json', 'Access-Control-Allow-Origin': '*' },
    });
  },
};
