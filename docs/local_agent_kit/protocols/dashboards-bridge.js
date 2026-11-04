/*
 * dashboards-bridge.js - the reference `window.cinna` helper for dashboard web views
 * (cinna_protocol: dashboards/1, contract 1.20.0). Wire described in
 * protocols/dashboards.schema.json under `x-web-view.bridge`.
 *
 * Every host that shows `view: "html"` components injects THIS FILE, BYTE FOR BYTE,
 * into the page before the page's own scripts. A host never keeps a fork or a patched
 * copy: when the bridge must change, it changes here, in the kit, for every host.
 * Agents never copy it into a component; the host provides `window.cinna`.
 *
 * It speaks MCP Apps (2026-01-26) JSON-RPC 2.0 over postMessage with the parent frame.
 * Cinna-only methods live under `cinna/*`. It runs inside the web-view sandbox: an
 * opaque origin, no storage, no network, no string-to-code. Plain script, no
 * dependencies, no module syntax.
 *
 *   await cinna.ready                 -> {component, title, params, theme, size}
 *   cinna.onData(cb)                  -> cb(data, {stale, fetched_at}); returns an unsubscribe
 *   await cinna.call(endpoint, params) -> the endpoint's JSON value; rejects with {message}
 *   cinna.setParams(params)           -> the source recomputes; the result comes via onData
 *   cinna.setContext({summary, selection, visible})
 *   cinna.ask(text)                   -> fills the chat's composer, never sends
 *   cinna.openLink(url)               -> opens after the owner confirms
 *   cinna.onTheme(cb)                 -> cb(theme) on light/dark changes; returns an unsubscribe
 *   cinna.onHighlight(cb)             -> cb(ids) when the agent highlights rows; returns an unsubscribe
 *
 * It also answers the host's `cinna/collect-context` request itself, from the page's
 * microdata and text selection, for pages that never call setContext.
 */
(function () {
  "use strict";

  if (window.cinna) {
    return;
  }

  var PROTOCOL_VERSION = "2026-01-26";
  var CALL_TIMEOUT_MS = 330000; // an endpoint may run up to 300 s, plus the host's own work
  var OPEN_LINK_TIMEOUT_MS = 300000; // the owner confirms the URL first
  var REQUEST_TIMEOUT_MS = 60000;
  var MAX_TEXT = 2000; // selected_text
  var MAX_SUMMARY = 200;
  var MAX_ITEMS = 100; // microdata items collected
  var MAX_PROPS = 20; // properties per item
  var MAX_VALUE = 200; // characters per property value
  var MAX_IDS = 500;

  var host = window.parent;
  var nonce = null;
  var nextId = 1;
  var pending = {}; // id -> {resolve, reject, timer}
  var handshakeDone = false;
  var queued = []; // messages written before the ui/initialize result arrived
  var dataListeners = [];
  var themeListeners = [];
  var lastData = null; // {data, meta}: replayed to a listener that subscribes late
  var highlightListeners = [];
  var lastHighlight = null; // ids: replayed to a listener that subscribes late
  var highlightReadySent = false;
  var lastContext = null; // what the page last passed to setContext
  var theme = "light";

  function failure(message) {
    return { message: String(message || "The request failed.") };
  }

  function quiet(promise) {
    // Fire-and-forget helpers: a caller may await the promise, but an unanswered
    // request never surfaces as an unhandled rejection.
    promise.then(null, function () {});
    return promise;
  }

  function withNonce(params) {
    var out = {};
    var key;
    for (key in params || {}) {
      if (Object.prototype.hasOwnProperty.call(params, key)) {
        out[key] = params[key];
      }
    }
    var meta = {};
    for (key in out._meta || {}) {
      if (Object.prototype.hasOwnProperty.call(out._meta, key)) {
        meta[key] = out._meta[key];
      }
    }
    if (nonce !== null) {
      meta.cinna = { nonce: nonce };
    }
    out._meta = meta;
    return out;
  }

  function post(message) {
    host.postMessage(message, "*");
  }

  function deliver(message) {
    if (!handshakeDone) {
      queued.push(message);
      return;
    }
    message.params = withNonce(message.params);
    post(message);
  }

  function flush() {
    var waiting = queued;
    queued = [];
    for (var i = 0; i < waiting.length; i += 1) {
      deliver(waiting[i]);
    }
  }

  function request(method, params, timeoutMs) {
    var id = nextId;
    nextId += 1;
    return new Promise(function (resolve, reject) {
      var timer = setTimeout(function () {
        delete pending[id];
        for (var i = 0; i < queued.length; i += 1) {
          if (queued[i].id === id) {
            queued.splice(i, 1); // never posted: it must not go out after the page gave up
            break;
          }
        }
        reject(failure(method + ": no answer from the host within " + timeoutMs / 1000 + " s."));
      }, timeoutMs);
      pending[id] = { resolve: resolve, reject: reject, timer: timer };
      deliver({ jsonrpc: "2.0", id: id, method: method, params: params || {} });
    });
  }

  function notify(method, params) {
    deliver({ jsonrpc: "2.0", method: method, params: params || {} });
  }

  function takeNonce(meta) {
    if (meta && meta.cinna && typeof meta.cinna.nonce === "string" && meta.cinna.nonce) {
      nonce = meta.cinna.nonce;
    }
  }

  function applyHostContext(context) {
    if (!context || typeof context !== "object") {
      return false;
    }
    var root = document.documentElement;
    var variables = context.styles && context.styles.variables;
    if (variables && typeof variables === "object") {
      for (var name in variables) {
        if (Object.prototype.hasOwnProperty.call(variables, name) && name.indexOf("--cinna-") === 0) {
          root.style.setProperty(name, String(variables[name]));
        }
      }
    }
    if (context.theme === "light" || context.theme === "dark") {
      var changed = context.theme !== theme;
      theme = context.theme;
      root.style.colorScheme = theme;
      root.setAttribute("data-cinna-theme", theme);
      return changed;
    }
    return false;
  }

  function resultValue(result) {
    if (!result || typeof result !== "object") {
      return result;
    }
    if (result.structuredContent !== undefined) {
      return result.structuredContent;
    }
    var content = result.content;
    if (content && content.length && content[0] && content[0].type === "text") {
      try {
        return JSON.parse(content[0].text);
      } catch (error) {
        return content[0].text;
      }
    }
    return null;
  }

  function errorText(result) {
    var content = result && result.content;
    if (content && content.length && content[0] && typeof content[0].text === "string") {
      return content[0].text;
    }
    return "The endpoint failed.";
  }

  function emitData(data, meta) {
    lastData = { data: data, meta: meta };
    var listeners = dataListeners.slice();
    for (var i = 0; i < listeners.length; i += 1) {
      listeners[i](data, meta);
    }
  }

  function emitTheme() {
    var listeners = themeListeners.slice();
    for (var i = 0; i < listeners.length; i += 1) {
      listeners[i](theme);
    }
  }

  function onToolResult(params) {
    if (!params || params.isError) {
      return; // the host marks a failed refresh itself; the page keeps its last data
    }
    var cinnaMeta = (params._meta && params._meta.cinna) || {};
    emitData(resultValue(params), {
      stale: cinnaMeta.stale === true,
      fetched_at: cinnaMeta.fetched_at || null,
    });
  }

  function onHighlightNotice(params) {
    var ids = params && params.ids;
    if (!Array.isArray(ids)) {
      return; // not a list: ignored, the page keeps its highlight
    }
    var clean = [];
    for (var i = 0; i < ids.length && clean.length < MAX_IDS; i += 1) {
      if (typeof ids[i] === "string" && ids[i]) {
        clean.push(ids[i]);
      }
    }
    lastHighlight = clean;
    var listeners = highlightListeners.slice();
    for (var j = 0; j < listeners.length; j += 1) {
      listeners[j](clean.slice());
    }
  }

  function clip(text, limit) {
    var out = String(text === null || text === undefined ? "" : text).replace(/\s+/g, " ");
    out = out.replace(/^ | $/g, "");
    return out.length > limit ? out.slice(0, limit) : out;
  }

  function itemId(item) {
    return item.getAttribute("itemid") || item.getAttribute("id") || "";
  }

  function propValue(node) {
    // The microdata value rules, reduced to what a chat can use.
    if (node.hasAttribute("itemscope")) {
      return itemId(node) || null;
    }
    var tag = String(node.tagName || "").toLowerCase();
    if (tag === "meta") {
      return node.getAttribute("content") || "";
    }
    if (tag === "a" || tag === "area" || tag === "link") {
      return node.getAttribute("href") || "";
    }
    if (tag === "img" || tag === "audio" || tag === "video" || tag === "source") {
      return node.getAttribute("src") || "";
    }
    if (tag === "time" && node.hasAttribute("datetime")) {
      return node.getAttribute("datetime");
    }
    if ((tag === "data" || tag === "meter") && node.hasAttribute("value")) {
      return node.getAttribute("value");
    }
    return node.textContent;
  }

  function onScreen(node) {
    var rect = node.getBoundingClientRect();
    var height = window.innerHeight || document.documentElement.clientHeight || 0;
    var width = window.innerWidth || document.documentElement.clientWidth || 0;
    return (rect.width > 0 || rect.height > 0) &&
      rect.bottom > 0 && rect.right > 0 && rect.top < height && rect.left < width;
  }

  function collectContext() {
    var out = { source: "collected", summary: "", selection: [], visible: [], microdata: [], selected_text: "" };
    var selection = null;
    try {
      selection = window.getSelection ? window.getSelection() : null;
      out.selected_text = clip(selection ? selection.toString() : "", MAX_TEXT);
    } catch (error) {
      selection = null;
    }
    try {
      out.summary = clip(document.title, MAX_SUMMARY);
      var items = document.querySelectorAll("[itemscope]");
      for (var i = 0; i < items.length && out.microdata.length < MAX_ITEMS; i += 1) {
        var item = items[i];
        var entry = { properties: {} };
        var id = itemId(item);
        if (id) {
          entry.id = id;
          if (out.visible.length < MAX_IDS && onScreen(item)) {
            out.visible.push(id);
          }
          if (selection && !selection.isCollapsed && selection.containsNode &&
              out.selection.length < MAX_IDS && selection.containsNode(item, true)) {
            out.selection.push(id);
          }
        }
        if (item.getAttribute("itemtype")) {
          entry.type = item.getAttribute("itemtype");
        }
        var props = item.querySelectorAll("[itemprop]");
        var count = 0;
        for (var j = 0; j < props.length && count < MAX_PROPS; j += 1) {
          var prop = props[j];
          var owner = prop.parentElement && prop.parentElement.closest("[itemscope]");
          if (owner !== item) {
            continue; // a property of a nested item
          }
          var names = String(prop.getAttribute("itemprop")).split(/\s+/);
          var value = propValue(prop);
          for (var k = 0; k < names.length; k += 1) {
            if (names[k] && !Object.prototype.hasOwnProperty.call(entry.properties, names[k])) {
              entry.properties[names[k]] = value === null ? null : clip(value, MAX_VALUE);
              count += 1;
            }
          }
        }
        out.microdata.push(entry);
      }
    } catch (error) {
      // A page that breaks the scan still gets an answer: whatever was collected.
    }
    if (lastContext) {
      out.source = "set_context";
      out.summary = lastContext.summary;
      out.selection = lastContext.selection;
      out.visible = lastContext.visible;
    }
    return out;
  }

  function answerHost(message) {
    // A request from the host: `ping` and `ui/resource-teardown` succeed,
    // `cinna/collect-context` is answered from the page itself; anything else is
    // answered "method not found" so the host never waits on the page.
    var reply = { jsonrpc: "2.0", id: message.id };
    if (message.method === "ping" || message.method === "ui/resource-teardown") {
      reply.result = withNonce({});
    } else if (message.method === "cinna/collect-context") {
      reply.result = withNonce(collectContext());
    } else {
      reply.error = { code: -32601, message: "Method not found: " + message.method, data: withNonce({}) };
    }
    post(reply);
  }

  function onMessage(event) {
    if (event.source !== host) {
      return;
    }
    var message = event.data;
    if (!message || typeof message !== "object" || message.jsonrpc !== "2.0") {
      return;
    }
    if (message.method) {
      takeNonce(message.params && message.params._meta);
      if (message.id !== undefined && message.id !== null) {
        answerHost(message);
      } else if (message.method === "ui/notifications/tool-result") {
        onToolResult(message.params);
      } else if (message.method === "ui/notifications/host-context-changed") {
        if (applyHostContext(message.params)) {
          emitTheme();
        }
      } else if (message.method === "cinna/highlight") {
        onHighlightNotice(message.params);
      }
      return;
    }
    var waiting = pending[message.id];
    if (!waiting) {
      return;
    }
    delete pending[message.id];
    clearTimeout(waiting.timer);
    if (message.error) {
      waiting.reject(failure(message.error.message));
    } else {
      takeNonce(message.result && message.result._meta);
      waiting.resolve(message.result);
    }
  }

  window.addEventListener("message", onMessage);

  // The handshake: ui/initialize goes out at once, without a nonce; its result carries
  // the nonce every later message repeats. A result of any later ui/initialize, or any
  // host message carrying `_meta.cinna.nonce`, replaces it. A result without one is
  // accepted (with one warning): the page then sends no nonce until the host gives one,
  // since enforcing it is the host's job.
  var initId = nextId;
  nextId += 1;
  var ready = new Promise(function (resolve, reject) {
    pending[initId] = {
      resolve: function (result) {
        result = result || {};
        var cinnaMeta = (result._meta && result._meta.cinna) || {};
        takeNonce(result._meta);
        if (nonce === null) {
          console.warn("cinna: the host gave no _meta.cinna.nonce; messages go without one.");
        }
        applyHostContext(result.hostContext);
        resolve({
          component: cinnaMeta.component || null,
          title: cinnaMeta.title || null,
          params: cinnaMeta.params || {},
          theme: theme,
          size: cinnaMeta.size || null,
        });
        handshakeDone = true;
        notify("ui/notifications/initialized", {});
        flush();
      },
      reject: reject,
      timer: null,
    };
  });
  quiet(ready);
  post({
    jsonrpc: "2.0",
    id: initId,
    method: "ui/initialize",
    params: {
      protocolVersion: PROTOCOL_VERSION,
      appInfo: { name: "cinna-dashboards-bridge", version: "1" },
      appCapabilities: {},
    },
  });

  function subscribe(list, cb) {
    if (typeof cb !== "function") {
      return function () {};
    }
    list.push(cb);
    return function () {
      var at = list.indexOf(cb);
      if (at >= 0) {
        list.splice(at, 1);
      }
    };
  }

  window.cinna = Object.freeze({
    ready: ready,

    onData: function (cb) {
      var unsubscribe = subscribe(dataListeners, cb);
      if (lastData && typeof cb === "function") {
        var seen = lastData;
        Promise.resolve().then(function () {
          if (dataListeners.indexOf(cb) >= 0) {
            cb(seen.data, seen.meta);
          }
        });
      }
      return unsubscribe;
    },

    call: function (endpoint, params) {
      return request("tools/call", { name: String(endpoint), arguments: params || {} }, CALL_TIMEOUT_MS)
        .then(function (result) {
          if (result && result.isError) {
            throw failure(errorText(result));
          }
          return resultValue(result);
        });
    },

    setParams: function (params) {
      notify("cinna/params-changed", { params: params || {} });
    },

    setContext: function (context) {
      context = context || {};
      var summary = typeof context.summary === "string" ? context.summary : "";
      lastContext = {
        summary: summary,
        selection: context.selection || [],
        visible: context.visible || [],
      };
      return quiet(request("ui/update-model-context", {
        content: summary ? [{ type: "text", text: summary }] : [],
        structuredContent: lastContext,
      }, REQUEST_TIMEOUT_MS));
    },

    ask: function (text) {
      return quiet(request("ui/message", {
        role: "user",
        content: [{ type: "text", text: String(text) }],
      }, REQUEST_TIMEOUT_MS));
    },

    openLink: function (url) {
      return quiet(request("ui/open-link", { url: String(url) }, OPEN_LINK_TIMEOUT_MS));
    },

    onTheme: function (cb) {
      return subscribe(themeListeners, cb);
    },

    onHighlight: function (cb) {
      var unsubscribe = subscribe(highlightListeners, cb);
      if (typeof cb !== "function") {
        return unsubscribe;
      }
      if (!highlightReadySent) {
        // Tells the host this page draws highlights; until then it reports
        // `highlighted: false` to the agent. Sent once, queued until the handshake.
        highlightReadySent = true;
        notify("cinna/highlight-ready", {});
      }
      if (lastHighlight) {
        var seen = lastHighlight;
        Promise.resolve().then(function () {
          if (highlightListeners.indexOf(cb) >= 0) {
            cb(seen.slice());
          }
        });
      }
      return unsubscribe;
    },
  });
})();
