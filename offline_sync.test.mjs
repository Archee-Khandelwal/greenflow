// Tests for offline_sync.js — the GreenFlow fleet-telemetry offline queue/sync layer.
// Plain Node, no dependencies. Run with:  node --test offline_sync.test.mjs
//
// These exercise the client-side halves of the challenge's TEST 1-3 and TEST 5-7 scenarios
// against a fake localStorage and a fake fetch, so they run in well under a second with no
// browser and no real backend. TEST 4 (survives a page refresh) is covered by creating a
// second createOfflineSync() instance against the same fake storage, which is exactly what a
// reloaded page does against real localStorage. TEST 8 (existing scheduling still works) and
// the server side of TEST 5-7 (the backend's own idempotency) are covered separately in
// greenflow_backend/test_offline_sync.py, since this module never talks to the real engine or
// the real server.
import assert from 'node:assert/strict';
import { test } from 'node:test';
import offlineSync from './offline_sync.js';
const { createOfflineSync } = offlineSync;

function fakeStorage() {
  const data = new Map();
  return {
    getItem: (k) => (data.has(k) ? data.get(k) : null),
    setItem: (k, v) => { data.set(k, String(v)); },
    _dump: () => new Map(data) // to hand the same backing store to a "second page load"
  };
}

// A scriptable fake backend: an array of {ok, status, json} responses consumed in order per URL
// suffix, so a test can say "the first /ingest call fails, the second one succeeds".
function fakeFetch(script) {
  const calls = [];
  const fetchImpl = async (url) => {
    calls.push(url);
    const path = url.includes('/health') ? '/health' : url.includes('/ingest') ? '/ingest' : url;
    const queue = script[path];
    if (!queue || !queue.length) throw new Error('network error: no scripted response for ' + path);
    const next = queue.shift();
    if (next.throw) throw next.throw;
    return { ok: next.ok, json: async () => next.body };
  };
  fetchImpl.calls = calls;
  return fetchImpl;
}

test('TEST 1: online -> a queued event is sent to /ingest immediately', async () => {
  const storage = fakeStorage();
  const fetchImpl = fakeFetch({
    '/health': [{ ok: true }],
    '/ingest': [{ ok: true, body: { stored_ids: ['r-1'], already_had: [], fleet_totals: {} } }]
  });
  const sync = createOfflineSync({ storage, fetchImpl, getBaseUrl: () => 'http://x' });
  await sync.checkConnectivity(); // establishes online=true, like the page's periodic ping
  assert.equal(sync.getStatus().online, true);

  const rec = sync.queueRecord({ id: 'r-1', workflow: 'docs', summary: {} });
  assert.equal(rec.id, 'r-1');
  // queueRecord fires trySync() without awaiting it (fire-and-forget, same as the real UI) —
  // give the microtask queue a turn to let that in-flight sync finish.
  await new Promise((r) => setTimeout(r, 0));
  await new Promise((r) => setTimeout(r, 0));

  assert.deepEqual(sync.getOutbox(), [], 'the event should have left the local queue');
  assert.ok(fetchImpl.calls.some((u) => u.includes('/ingest')), '/ingest should have been called');
});

test('TEST 2 + TEST 3: offline -> queueRecord still succeeds and the event is stored locally', async () => {
  const storage = fakeStorage();
  const fetchImpl = fakeFetch({ '/health': [{ ok: false }] }); // /health probe fails => offline
  const sync = createOfflineSync({ storage, fetchImpl, getBaseUrl: () => 'http://x' });
  await sync.checkConnectivity();
  assert.equal(sync.getStatus().online, false);

  const rec = sync.queueRecord({ workflow: 'support', summary: { carbon_g: 1 } });
  await new Promise((r) => setTimeout(r, 0));

  assert.equal(sync.getOutbox().length, 1);
  assert.equal(sync.getOutbox()[0].id, rec.id);
  assert.ok(rec.id, 'a unique id must be assigned even while offline');
});

test('TEST 4: refresh page while offline -> queued event still exists (survives reload)', async () => {
  const storage = fakeStorage();
  const offlineFetch = fakeFetch({ '/health': [{ ok: false }] });
  const page1 = createOfflineSync({ storage, fetchImpl: offlineFetch, getBaseUrl: () => 'http://x' });
  await page1.checkConnectivity();
  page1.queueRecord({ workflow: 'docs', summary: {} });
  await new Promise((r) => setTimeout(r, 0));
  assert.equal(page1.getOutbox().length, 1);

  // Simulate a page refresh: brand-new module instance, same localStorage-backed store.
  const page2 = createOfflineSync({ storage, fetchImpl: offlineFetch, getBaseUrl: () => 'http://x' });
  assert.equal(page2.getOutbox().length, 1, 'the queue must be reloaded from storage on a fresh page load');
  assert.equal(page2.getOutbox()[0].workflow, 'docs');
});

test('TEST 5: reconnect -> the queued event automatically syncs', async () => {
  const storage = fakeStorage();
  const offlineFetch = fakeFetch({ '/health': [{ ok: false }] });
  const sync = createOfflineSync({ storage, fetchImpl: offlineFetch, getBaseUrl: () => 'http://x' });
  await sync.checkConnectivity();
  const rec = sync.queueRecord({ workflow: 'docs', summary: {} });
  await new Promise((r) => setTimeout(r, 0));
  assert.equal(sync.getOutbox().length, 1);

  // Connectivity returns. Build a fresh instance against the same storage (equivalent to the
  // real page's periodic /health check succeeding) with a fetch that now answers successfully.
  const onlineFetch = fakeFetch({
    '/health': [{ ok: true }],
    '/ingest': [{ ok: true, body: { stored_ids: [rec.id], already_had: [], fleet_totals: {} } }]
  });
  const reconnected = createOfflineSync({ storage, fetchImpl: onlineFetch, getBaseUrl: () => 'http://x' });
  await reconnected.checkConnectivity(); // online=true and, since the queue is non-empty, auto-syncs

  assert.deepEqual(reconnected.getOutbox(), [], 'the event should be gone after a successful sync');
  assert.equal(reconnected.getStatus().lastSyncedCount, 1);
});

test('TEST 6: failed synchronization -> the event remains queued for a later retry', async () => {
  const storage = fakeStorage();
  const fetchImpl = fakeFetch({
    '/health': [{ ok: true }],
    '/ingest': [{ throw: new TypeError('fetch failed') }] // e.g. server briefly unreachable
  });
  const sync = createOfflineSync({ storage, fetchImpl, getBaseUrl: () => 'http://x' });
  await sync.checkConnectivity();
  sync.queueRecord({ workflow: 'docs', summary: {} });
  await new Promise((r) => setTimeout(r, 0));
  await new Promise((r) => setTimeout(r, 0));

  assert.equal(sync.getOutbox().length, 1, 'a failed /ingest call must not drop the event');
  assert.equal(sync.getStatus().syncing, false, 'the syncing flag must be cleared after the failure');
});

test('TEST 7: the same event is never uploaded twice, even if trySync runs concurrently', async () => {
  const storage = fakeStorage();
  let ingestCalls = 0;
  const fetchImpl = async (url) => {
    if (url.includes('/health')) return { ok: true, json: async () => ({}) };
    if (url.includes('/ingest')) {
      ingestCalls++;
      return { ok: true, json: async () => ({ stored_ids: ['r-1'], already_had: [], fleet_totals: {} }) };
    }
    throw new Error('unexpected url ' + url);
  };
  const sync = createOfflineSync({ storage, fetchImpl, getBaseUrl: () => 'http://x' });
  await sync.checkConnectivity();
  sync.queueRecord({ id: 'r-1', workflow: 'docs', summary: {} }); // fires trySync() once already
  // Calling trySync() again right away (e.g. a second timer tick) must be a no-op while the
  // first is in flight, and a no-op again once the queue is already empty.
  await Promise.all([sync.trySync(), sync.trySync()]);
  await new Promise((r) => setTimeout(r, 0));

  assert.equal(ingestCalls, 1, '/ingest should only ever be called once for a single queued event');
  assert.deepEqual(sync.getOutbox(), []);
});

test('Simulate offline demo control uses the same queue/recovery code as real detection', async () => {
  const storage = fakeStorage();
  const fetchImpl = fakeFetch({
    '/health': [{ ok: true }, { ok: true }], // one for the initial check, one for "Reconnect"
    '/ingest': [{ ok: true, body: { stored_ids: ['r-1'], already_had: [], fleet_totals: {} } }]
  });
  const sync = createOfflineSync({ storage, fetchImpl, getBaseUrl: () => 'http://x' });
  await sync.checkConnectivity();
  assert.equal(sync.getStatus().online, true);

  sync.setSimulatedOffline(true); // "Simulate offline" button
  assert.equal(sync.getStatus().online, false);
  assert.equal(sync.getStatus().simulatedOffline, true);

  const rec = sync.queueRecord({ id: 'r-1', workflow: 'docs', summary: {} });
  await new Promise((r) => setTimeout(r, 0));
  assert.equal(sync.getOutbox().length, 1, 'queueRecord must not call the network while simulating offline');
  assert.ok(!fetchImpl.calls.some((u) => u.includes('/ingest')), 'no /ingest call should happen while simulated-offline');

  await sync.setSimulatedOffline(false); // "Reconnect" button
  assert.equal(sync.getStatus().simulatedOffline, false);
  assert.deepEqual(sync.getOutbox(), [], 'reconnecting from simulated offline must flush the same queue');
  assert.equal(rec.id, 'r-1');
});
