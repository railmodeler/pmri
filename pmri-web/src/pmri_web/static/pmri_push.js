/**
 * pmri-push: open a persistent WebSocket to /ws/nudge and dispatch
 * server-sent topic nudges as DOM CustomEvents on ``document.body``.
 *
 * Why not htmx-ext-ws directly? That extension routes incoming frames
 * through hx-swap-oob, which means the server has to serialise HTML.
 * We'd rather keep Jinja rendering in the HTTP pipeline (where it
 * already lives, with request context + auth) and have the WebSocket
 * carry only "something changed, pull fresh" nudges. This gives each
 * page the same shape:
 *
 *     <table id="..."
 *            hx-get="/..."
 *            hx-trigger="pmri:<topic> from:body"
 *            hx-target="this" hx-swap="outerHTML">
 *
 * No periodic refresh; the server decides when to nudge. A nudge may
 * carry ``data`` (freeform object), surfaced as ``event.detail``.
 *
 * The socket auto-reconnects with exponential backoff. One socket
 * per page is enough because nudges are page-scoped broadcasts, not
 * per-element subscriptions.
 */
(function () {
  "use strict";

  const MAX_BACKOFF_MS = 10_000;
  const INITIAL_BACKOFF_MS = 500;

  let ws = null;
  let backoff = INITIAL_BACKOFF_MS;
  let reconnectTimer = null;

  function wsUrl() {
    const proto = location.protocol === "https:" ? "wss:" : "ws:";
    return `${proto}//${location.host}/ws/nudge`;
  }

  function connect() {
    if (reconnectTimer !== null) {
      clearTimeout(reconnectTimer);
      reconnectTimer = null;
    }
    try {
      ws = new WebSocket(wsUrl());
    } catch (err) {
      // Immediately schedule a retry; nothing else to do.
      scheduleReconnect();
      return;
    }
    ws.addEventListener("open", () => {
      backoff = INITIAL_BACKOFF_MS;
    });
    ws.addEventListener("message", (ev) => {
      let frame;
      try {
        frame = JSON.parse(ev.data);
      } catch {
        return;
      }
      if (!frame || typeof frame.topic !== "string") return;
      // Dispatch as "pmri:<topic>" so page HTML can listen via
      // ``hx-trigger="pmri:scripts from:body"``.
      document.body.dispatchEvent(new CustomEvent(
        `pmri:${frame.topic}`, { detail: frame.data || {}, bubbles: true },
      ));
    });
    ws.addEventListener("close", () => {
      ws = null;
      scheduleReconnect();
    });
    ws.addEventListener("error", () => {
      // onclose fires after onerror; the reconnect scheduling lives
      // there to keep a single code path.
    });
  }

  function scheduleReconnect() {
    if (reconnectTimer !== null) return;
    reconnectTimer = window.setTimeout(() => {
      reconnectTimer = null;
      connect();
      backoff = Math.min(MAX_BACKOFF_MS, backoff * 2);
    }, backoff);
  }

  // Start on load. ``DOMContentLoaded`` is already past by the time
  // a deferred script runs; connect immediately.
  connect();
})();
