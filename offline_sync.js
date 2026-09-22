/*
 * offline_sync.js — the offline-resilience layer for GreenFlow's fleet telemetry.
 *
 * The scheduler engine in greenflow.html has zero network dependency already: it computes full
 * plans entirely client-side. This module only covers the optional "log a completed plan to the
 * central fleet dashboard" feature — the one thing that actually talks to the backend and so is
 * the thing that needs to survive an outage and reconcile afterward.
 *
 * Framework-free so it can run unmodified as a browser <script> (exposes window.GreenFlowOfflineSync)
 * and be unit-tested under plain Node (module.exports) with a fake storage/fetch — no DOM required.
 */
(function (root, factory) {
  if (typeof module === 'object' && module.exports) {
    module.exports = factory();
  } else {
    root.GreenFlowOfflineSync = factory();
  }
})(typeof self !== 'undefined' ? self : this, function () {
  'use strict';

  function makeId() {
    return 'r-' + Date.now().toString(36) + '-' + Math.random().toString(36).slice(2, 10);
  }

  function createStore(storage, key) {
    return {
      load: function () {
        if (!storage) return [];
        try {
          var raw = storage.getItem(key);
          var parsed = raw ? JSON.parse(raw) : [];
          return Array.isArray(parsed) ? parsed : [];
        } catch (e) {
          return [];
        }
      },
      save: function (list) {
        if (!storage) return;
        try {
          storage.setItem(key, JSON.stringify(list));
        } catch (e) {
          // Storage unavailable or full: the run continues in memory for this session.
        }
      }
    };
  }

  // opts:
  //   storage       localStorage-like {getItem,setItem} — omit to keep the queue in-memory only
  //   storageKey    key under which the queue is persisted (default 'greenflow_outbox_v1')
  //   fetchImpl     fetch-compatible function (required to ever go online)
  //   getBaseUrl    () => backend base URL, e.g. () => $('apiBase').value
  //   pingTimeoutMs abort the /health probe after this long (default 1800ms)
  function createOfflineSync(opts) {
    opts = opts || {};
    var store = createStore(opts.storage, opts.storageKey || 'greenflow_outbox_v1');
    var fetchImpl = opts.fetchImpl;
    var getBaseUrl = opts.getBaseUrl || function () { return 'http://127.0.0.1:8000'; };
    var pingTimeoutMs = opts.pingTimeoutMs || 1800;

    var outbox = store.load();
    var online = false;
    var simulatedOffline = false;
    var syncing = false;
    var lastSyncedCount = 0;
    var listeners = [];

    function status() {
      return {
        online: online && !simulatedOffline,
        simulatedOffline: simulatedOffline,
        syncing: syncing,
        queueLength: outbox.length,
        lastSyncedCount: lastSyncedCount
      };
    }

    function emit() {
      var s = status();
      listeners.forEach(function (fn) {
        try { fn(s); } catch (e) { /* a listener's own bug should not break sync */ }
      });
    }

    function onStatusChange(fn) {
      listeners.push(fn);
      return function unsubscribe() {
        listeners = listeners.filter(function (l) { return l !== fn; });
      };
    }

    // Queue a record for delivery to POST /ingest. Assigns a stable unique id up front (unless
    // the caller already supplied one) so a later retry can never be double-counted server-side.
    function queueRecord(record) {
      var rec = Object.assign({ id: (record && record.id) || makeId() }, record);
      outbox.push(rec);
      store.save(outbox);
      emit();
      trySync(); // fire-and-forget: harmless no-op if we are offline or already syncing
      return rec;
    }

    async function pingOnce() {
      if (simulatedOffline || !fetchImpl) return false;
      try {
        var hasAbort = typeof AbortController !== 'undefined';
        var ctl = hasAbort ? new AbortController() : null;
        var timer = ctl ? setTimeout(function () { ctl.abort(); }, pingTimeoutMs) : null;
        var res = await fetchImpl(getBaseUrl().replace(/\/$/, '') + '/health',
          ctl ? { signal: ctl.signal, cache: 'no-store' } : { cache: 'no-store' });
        if (timer) clearTimeout(timer);
        return !!(res && res.ok);
      } catch (e) {
        return false; // network error, timeout, CORS failure, server down — all mean "offline"
      }
    }

    // The single source of truth for connectivity. navigator.onLine / the browser's online/offline
    // events are only ever used as a hint to call this sooner — never as the actual verdict, since
    // a backend request can fail even while navigator.onLine reports true.
    async function checkConnectivity() {
      online = simulatedOffline ? false : await pingOnce();
      emit();
      if (online && outbox.length) await trySync();
      return online;
    }

    async function trySync() {
      if (syncing || simulatedOffline || !online || !outbox.length) return { synced: 0, ok: true };
      syncing = true; emit();
      var base = getBaseUrl().replace(/\/$/, '');
      var synced = 0, ok = false;
      try {
        var res = await fetchImpl(base + '/ingest', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ records: outbox })
        });
        if (res && res.ok) {
          var j = await res.json();
          var done = {};
          (j.stored_ids || []).concat(j.already_had || []).forEach(function (id) { done[id] = true; });
          var before = outbox.length;
          outbox = outbox.filter(function (r) { return !done[r.id]; });
          synced = before - outbox.length;
          store.save(outbox);
          lastSyncedCount = synced;
          ok = true;
        }
      } catch (e) {
        // Still offline, server unreachable, or a transient failure — leave the outbox queued
        // exactly as it is and let the next connectivity check retry it. Nothing is lost.
      }
      syncing = false; emit();
      return { synced: synced, ok: ok };
    }

    // Demo-mode switch. It is intentionally just another way to set the same `simulatedOffline`
    // flag that real detection would clear on its own — every code path below (queueRecord,
    // trySync, checkConnectivity) is shared, so a simulated outage exercises the identical queue
    // and recovery logic as a real one, not a separate animation.
    function setSimulatedOffline(v) {
      simulatedOffline = !!v;
      if (simulatedOffline) {
        online = false;
        emit();
        return Promise.resolve(false);
      }
      return checkConnectivity(); // "Reconnect": leave simulation and immediately re-verify + sync
    }

    return {
      queueRecord: queueRecord,
      trySync: trySync,
      checkConnectivity: checkConnectivity,
      pingOnce: pingOnce,
      setSimulatedOffline: setSimulatedOffline,
      isSimulatedOffline: function () { return simulatedOffline; },
      getOutbox: function () { return outbox.slice(); },
      getStatus: status,
      onStatusChange: onStatusChange
    };
  }

  return { createOfflineSync: createOfflineSync, makeId: makeId };
});
