/**
 * LlmingWebSocket — Framework-agnostic WebSocket client with auto-reconnect.
 *
 * Works with any JavaScript UI (vanilla, Vue, React, Svelte, etc.).
 * Part of llming-com.
 *
 * Features:
 * - Exponential-backoff reconnect on unexpected disconnect
 * - Heartbeat keepalive with ack timeout (shows warning banner)
 * - Handles llming-com close codes (4004 session-not-found, 4001 superseded)
 * - Optional built-in reconnect/warning banner (disable for custom UI)
 * - Zero dependencies, no DOM required (works in Workers too)
 *
 * Usage:
 *   const ws = new LlmingWebSocket('ws://localhost:8001/ws/abc123', {
 *     onMessage(msg)    { console.log('Got:', msg); },
 *     onSessionLost(r)  { location.href = '/login'; },
 *   });
 *   ws.connect();
 */
class LlmingWebSocket {
  /**
   * @param {string} url — WebSocket endpoint URL
   * @param {object} [options]
   * @param {function(object):void}      [options.onMessage]       — parsed JSON message
   * @param {function():void}            [options.onOpen]          — connection opened
   * @param {function(CloseEvent):void}  [options.onClose]         — connection closed (raw)
   * @param {function(Event):void}       [options.onError]         — WebSocket error
   * @param {function({attempt,maxAttempts,delay}):void}
   *        [options.onReconnecting]  — reconnect attempt starting
   * @param {function():void}            [options.onReconnected]   — reconnect succeeded
   * @param {function({reason,code?}):void}
   *        [options.onSessionLost]   — session is unrecoverable
   * @param {function():void}            [options.onConnectionWarning] — heartbeat ack overdue
   * @param {function():void}            [options.onConnectionRestored] — ack received after warning
   * @param {number}  [options.maxReconnectAttempts=15]
   * @param {number}  [options.heartbeatInterval=15000]  — ms, 0 to disable
   * @param {number}  [options.maxBackoff=5000]          — max reconnect delay ms
   * @param {boolean} [options.showBanner=true]          — built-in reconnect/warning banner
   * @param {string}  [options.bannerText='Reconnecting\u2026']
   * @param {string}  [options.warningText='Connection unstable\u2026']
   */
  constructor(url, options = {}) {
    this.url = url;
    this._onMessage            = options.onMessage            || null;
    this._onOpen               = options.onOpen               || null;
    this._onClose              = options.onClose              || null;
    this._onError              = options.onError              || null;
    this._onReconnecting       = options.onReconnecting       || null;
    this._onReconnected        = options.onReconnected        || null;
    this._onSessionLost        = options.onSessionLost        || null;
    this._onConnectionWarning  = options.onConnectionWarning  || null;
    this._onConnectionRestored = options.onConnectionRestored || null;

    this._maxReconnectAttempts = options.maxReconnectAttempts ?? 15;
    this._heartbeatIntervalMs = options.heartbeatInterval ?? 15000;
    this._maxBackoff          = options.maxBackoff ?? 5000;
    this._showBanner          = options.showBanner ?? true;
    this._bannerText          = options.bannerText ?? 'Reconnecting\u2026';
    this._warningText         = options.warningText ?? 'Connection unstable\u2026';

    // Offline mode: connect() and send() are no-ops, no socket is opened.
    // Useful for self-contained demo bundles where every command/query
    // is served by an in-page mock (the consumer routes those through
    // their own dispatcher; this client just stays out of the way).
    // Defaults to ``window.__LLMING_OFFLINE__`` if present.
    this._offline = options.offline ?? (typeof window !== 'undefined' && !!window.__LLMING_OFFLINE__);

    this.ws = null;
    this._heartbeatTimer   = null;
    this._ackTimer         = null;
    this._ackWarningShown  = false;
    this._reconnectAttempts = 0;
    this._reconnectTimer   = null;
    this._intentionalClose = false;
    this._everConnected    = false;
  }

  /** Open the WebSocket connection.  Safe to call again after close(). */
  connect() {
    if (this._offline) {
      // No socket — consumer is responsible for serving everything in-page.
      return;
    }
    this._intentionalClose = false;
    this.ws = new WebSocket(this.url);

    this.ws.onopen = () => {
      const wasReconnect = this._reconnectAttempts > 0;
      this._reconnectAttempts = 0;
      this._everConnected = true;
      this._hideBanner();
      this._ackWarningShown = false;
      this._startHeartbeat();
      if (wasReconnect && this._onReconnected) this._onReconnected();
      if (this._onOpen) this._onOpen();
    };

    this.ws.onmessage = (e) => {
      try {
        const msg = JSON.parse(e.data);
        if (msg.type === 'heartbeat_ack') {
          this._onHeartbeatAck();
          return; // internal — don't forward to app
        }
        if (this._onMessage) this._onMessage(msg);
      } catch (err) {
        console.error('[LlmingWS] Parse error:', err);
      }
    };

    this.ws.onclose = (e) => {
      this._stopHeartbeat();
      if (this._onClose) this._onClose(e);
      if (this._intentionalClose) return;

      // Session not found — server closed explicitly
      if (e.code === 4004) {
        this._hideBanner();
        if (this._onSessionLost) this._onSessionLost({ reason: 'not_found', code: 4004 });
        return;
      }

      // Superseded — another tab/window took over this session
      if (e.code === 4001) {
        this._hideBanner();
        if (this._onSessionLost) this._onSessionLost({ reason: 'superseded', code: 4001 });
        return;
      }

      // Dead session — never connected successfully after several attempts
      if (e.code === 1006 && this._reconnectAttempts >= 2 && !this._everConnected) {
        this._hideBanner();
        if (this._onSessionLost) this._onSessionLost({ reason: 'dead_session', code: 1006 });
        return;
      }

      // Reconnect with exponential backoff
      if (this._reconnectAttempts < this._maxReconnectAttempts) {
        this._reconnectAttempts++;
        const delay = Math.min(
          1000 * Math.pow(1.5, this._reconnectAttempts),
          this._maxBackoff,
        );
        console.log(
          `[LlmingWS] Reconnecting in ${Math.round(delay)}ms ` +
          `(attempt ${this._reconnectAttempts}/${this._maxReconnectAttempts})`,
        );
        if (this._showBanner) this._showBannerEl(this._bannerText);
        if (this._onReconnecting) {
          this._onReconnecting({
            attempt: this._reconnectAttempts,
            maxAttempts: this._maxReconnectAttempts,
            delay,
          });
        }
        this._reconnectTimer = setTimeout(() => this.connect(), delay);
      } else {
        this._hideBanner();
        if (this._onSessionLost) this._onSessionLost({ reason: 'exhausted' });
      }
    };

    this.ws.onerror = (e) => {
      if (this._onError) this._onError(e);
    };
  }

  /** Send a JSON message.  No-op if not connected. */
  send(msg) {
    if (this.ws && this.ws.readyState === WebSocket.OPEN) {
      this.ws.send(JSON.stringify(msg));
    }
  }

  /** Gracefully close without triggering reconnect. */
  close() {
    this._intentionalClose = true;
    clearTimeout(this._reconnectTimer);
    this._stopHeartbeat();
    this._hideBanner();
    if (this.ws) this.ws.close();
  }

  /** Whether the WebSocket is currently open. */
  get connected() {
    return !!(this.ws && this.ws.readyState === WebSocket.OPEN);
  }

  // ── Heartbeat with ack timeout ─────────────────────────

  _startHeartbeat() {
    if (!this._heartbeatIntervalMs) return;
    this._heartbeatTimer = setInterval(() => {
      this.send({ type: 'heartbeat' });
      this._startAckTimeout();
    }, this._heartbeatIntervalMs);
  }

  _stopHeartbeat() {
    if (this._heartbeatTimer) {
      clearInterval(this._heartbeatTimer);
      this._heartbeatTimer = null;
    }
    clearTimeout(this._ackTimer);
    this._ackTimer = null;
  }

  _startAckTimeout() {
    clearTimeout(this._ackTimer);
    // If no ack within one full heartbeat interval, show warning
    this._ackTimer = setTimeout(() => {
      if (this._intentionalClose) return;
      console.warn('[LlmingWS] Heartbeat ack overdue');
      this._ackWarningShown = true;
      if (this._showBanner) this._showBannerEl(this._warningText);
      if (this._onConnectionWarning) this._onConnectionWarning();
    }, this._heartbeatIntervalMs);
  }

  _onHeartbeatAck() {
    clearTimeout(this._ackTimer);
    this._ackTimer = null;
    if (this._ackWarningShown) {
      this._ackWarningShown = false;
      this._hideBanner();
      if (this._onConnectionRestored) this._onConnectionRestored();
    }
  }

  // ── Built-in banner (opt-out via showBanner:false) ─────

  _showBannerEl(text) {
    if (typeof document === 'undefined') return;
    let banner = document.getElementById('llming-reconnect-banner');
    if (!banner) {
      banner = document.createElement('div');
      banner.id = 'llming-reconnect-banner';
      banner.style.cssText =
        'position:fixed;top:0;left:0;right:0;z-index:99999;' +
        'background:rgba(30,30,50,0.95);color:#7dd3fc;' +
        'text-align:center;padding:8px 16px;font-size:13px;' +
        'backdrop-filter:blur(4px);border-bottom:1px solid rgba(125,211,252,0.2);';
      document.body.appendChild(banner);
    }
    banner.textContent = text;
    banner.style.display = '';
  }

  _hideBanner() {
    if (typeof document === 'undefined') return;
    const banner = document.getElementById('llming-reconnect-banner');
    if (banner) banner.style.display = 'none';
  }
}
