// Exercise the production draft queue with deterministic timers and browser I/O.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');

function harness(apiOverride) {
  const elements = new Map();
  const cache = new Map();
  const timers = new Map();
  const requests = [];
  const beacons = [];
  let timerId = 0;
  const element = id => {
    if (!elements.has(id)) elements.set(id, {value: '', textContent: ''});
    return elements.get(id);
  };
  const context = vm.createContext({
    document: {
      getElementById: element,
      querySelectorAll: selector => Array.from({length: selector.includes('day-worker') ? 8 : 2}, (_, i) => element(selector + i)),
    },
    localStorage: {getItem: key => cache.get(key), setItem: (key, value) => cache.set(key, value)},
    location: {origin: 'http://127.0.0.1:8765'},
    navigator: {sendBeacon: (url, blob) => { beacons.push({url, blob}); return true; }},
    Blob,
    setTimeout: (callback, delay) => { timers.set(++timerId, {callback, delay}); return timerId; },
    clearTimeout: id => timers.delete(id),
    api: async (url, draft) => {
      requests.push({url, draft});
      return apiOverride ? apiOverride(url, draft) : {draft};
    },
  });
  const source = fs.readFileSync(path.join(__dirname, '../local-web/static/app.js'), 'utf8');
  const queueSource = source.slice(0, source.indexOf('async function restoreDraft('));
  const draft = vm.runInContext(queueSource + `
    Object.assign(state, {draftReady:true, draftDate:'2026-12-31', draftScope:'test', hmiFolder:'C:/test/hmi'});
    ({queueDraft, flushDrafts, flushDraftBeacons, pendingDrafts, state});
  `, context);
  return {draft, element, cache, timers, requests, beacons,
    async fireTimer() {
      assert.equal(timers.size, 1);
      const [id, timer] = timers.entries().next().value;
      timers.delete(id);
      await timer.callback();
    },
  };
}

test('rapid input caches each edit immediately and sends only the latest draft after 300 ms', async () => {
  const h = harness();
  for (const value of ['1', '12', '123']) {
    h.element('B32').value = value;
    h.draft.queueDraft();
    assert.equal(JSON.parse([...h.cache.values()][0]).values.B32, value);
    assert.equal(h.requests.length, 0);
    assert.equal(h.timers.size, 1);
  }
  assert.equal([...h.timers.values()][0].delay, 300);
  await h.fireTimer();
  assert.equal(h.requests.length, 1);
  assert.equal(h.requests[0].draft.values.B32, '123');
  assert.equal(h.draft.pendingDrafts.size, 0);
});

test('explicit flush sends pending input immediately and clears its scheduled timer', async () => {
  const h = harness();
  h.element('G32').value = '0';
  h.draft.queueDraft();
  await h.draft.flushDrafts();
  assert.equal(h.timers.size, 0);
  assert.equal(h.requests.length, 1);
  assert.equal(h.requests[0].draft.values.G32, '0');
  assert.equal(h.requests[0].draft.values.B32, '');
});

test('edits arriving during a save remain queued and are saved before the flush finishes', async () => {
  let finishFirst;
  let count = 0;
  const h = harness((url, draft) => ++count === 1 ? new Promise(resolve => {finishFirst = () => resolve({draft});}) : {draft});
  h.element('B32').value = '1'; h.draft.queueDraft();
  const saving = h.draft.flushDrafts();
  h.element('B32').value = '2'; h.draft.queueDraft();
  finishFirst();
  await saving;
  assert.equal(h.requests.length, 2);
  assert.equal(h.requests[1].draft.values.B32, '2');
  assert.equal(h.draft.pendingDrafts.size, 0);
});

test('closing before the debounce deadline sends a beacon and retains browser recovery data', async () => {
  const h = harness();
  h.element('F42').value = '1100'; h.draft.queueDraft();
  h.draft.flushDraftBeacons();
  assert.equal(h.timers.size, 0);
  assert.equal(h.requests.length, 0);
  assert.equal(h.beacons.length, 1);
  assert.equal(JSON.parse(await h.beacons[0].blob.text()).values.F42, '1100');
  assert.equal(JSON.parse([...h.cache.values()][0]).values.F42, '1100');
});

test('failed saves retain the latest draft for a later retry', async () => {
  let failed = true;
  const h = harness((url, draft) => { if (failed) throw Error('offline'); return {draft}; });
  h.element('B32').value = '7'; h.draft.queueDraft();
  await h.fireTimer();
  assert.equal(h.draft.pendingDrafts.size, 1);
  assert.equal(JSON.parse([...h.cache.values()][0]).values.B32, '7');
  failed = false;
  await h.draft.flushDrafts();
  assert.equal(h.draft.pendingDrafts.size, 0);
});


test('HMI dates show Korean weekdays and preserve ISO values without timezone drift', () => {
  const source = fs.readFileSync(path.join(__dirname, '../local-web/static/app.js'), 'utf8');
  const context = vm.createContext({});
  const label = vm.runInContext(source.slice(0, source.indexOf('const state =')) + 'hmiDateLabel', context);
  for (const [value, expected] of [['2026-10-25','일'],['2026-10-26','월'],['2026-10-27','화'],
      ['2026-10-28','수'],['2026-10-29','목'],['2026-10-30','금'],['2026-10-31','토'],
      ['2028-02-29','화'],['2027-01-01','금']]) {
    assert.equal(label(value), `${value}(${expected})`);
  }
  for (const value of ['', '2026-02-29', '2026-10-32', 'bad', '2026-1-1']) assert.equal(label(value), '');
});
