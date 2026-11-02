// jmri-web — minimal client-side glue.
//
// Opens a WebSocket to /json once and exposes it as `jmriBus`. Pages can
// subscribe to message types via `jmriBus.on(type, handler)`. Subscriptions
// survive page navigation because the bus is created lazily and cached on
// `window`. Combined with htmx for request/response actions, this keeps
// every page reactive without bringing in a framework.

(function () {
  if (window.jmriBus) return; // singleton

  const handlers = new Map(); // type -> Set<fn>
  let ws = null;
  let queue = [];

  function connect() {
    const proto = location.protocol === "https:" ? "wss" : "ws";
    ws = new WebSocket(`${proto}://${location.host}/json`);
    ws.addEventListener("open", () => {
      // Flush any queued sends from before the socket was open.
      queue.forEach((msg) => ws.send(msg));
      queue = [];
    });
    ws.addEventListener("message", (evt) => {
      let msg;
      try { msg = JSON.parse(evt.data); } catch { return; }
      const set = handlers.get(msg.type);
      if (set) for (const fn of set) try { fn(msg); } catch (e) { console.warn(e); }
      const allSet = handlers.get("*");
      if (allSet) for (const fn of allSet) try { fn(msg); } catch (e) { console.warn(e); }
    });
    ws.addEventListener("close", () => {
      // Reconnect after a brief delay.
      setTimeout(connect, 1000);
    });
  }

  function send(obj) {
    const text = JSON.stringify(obj);
    if (ws && ws.readyState === WebSocket.OPEN) ws.send(text);
    else queue.push(text);
  }

  window.jmriBus = {
    on(type, fn) {
      if (!handlers.has(type)) handlers.set(type, new Set());
      handlers.get(type).add(fn);
      return () => handlers.get(type).delete(fn);
    },
    send,
  };

  connect();
})();
