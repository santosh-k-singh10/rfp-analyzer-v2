/**
 * tests/test_edit_ui_mutations.js
 *
 * jsdom-based tests for sub-task 1.3b: mutationSuccess, replaceRow, rerenderSection.
 *
 * Tests:
 *   1. mutationSuccess updates RV.state.result (item replaced in skill bucket)
 *   2. mutationSuccess creates a new SkillOutput for a previously-empty bucket
 *   3. mutationSuccess updates spine items correctly
 *   4. mutationSuccess updates RV.state.revision from editState
 *   5. replaceRow replaces an <li> row in place
 *   6. replaceRow replaces a <tr> row in place
 *   7. rerenderSection re-renders a skill panel when active tab matches
 *   8. rerenderSection re-renders an AMS section card (requirements)
 *
 * Usage: node tests/test_edit_ui_mutations.js
 * Exits 0 on success, 1 on any failure.
 */

'use strict';

const fs   = require('fs');
const path = require('path');
const { JSDOM } = require('jsdom');

const ROOT       = path.resolve(__dirname, '..');
const APP_JS     = path.join(ROOT, 'app', 'static', 'js', 'app.js');
const REVIEW_JS  = path.join(ROOT, 'app', 'static', 'js', 'review.js');
const EDIT_JS    = path.join(ROOT, 'app', 'static', 'js', 'review-edit.js');
const FIXTURE    = path.join(ROOT, 'tests', 'fixtures', 'result.json');

let passed = 0;
let failed = 0;

function assert(cond, msg) {
  if (cond) {
    console.log('  ✓ ' + msg);
    passed++;
  } else {
    console.error('  ✗ ' + msg);
    failed++;
  }
}

/** Build a fresh JSDOM with all scripts loaded and RV bootstrapped. */
function buildEnv() {
  const dom = new JSDOM(`<!DOCTYPE html><html><body>
    <div id="tab-panels"></div>
    <div id="stats-bar"></div>
    <div id="tab-bar"></div>
    <div id="loading-state"></div>
    <div id="error-state"></div>
    <div id="results-state" style="display:none"></div>
    <div id="page-title"></div>
    <div id="page-meta"></div>
    <div id="rail-skills-section"></div>
    <div id="rail-skills-list"></div>
    <button type="button" id="btn-reset-edits" data-action="reset-edits">Reset all edits</button>
    <span id="save-indicator" style="display:none;"></span>
  </body></html>`, {
    runScripts: 'dangerously',
    url: 'http://localhost/',
  });

  const { window } = dom;
  const { document } = window;

  // Stubs
  window.fetch = () => Promise.reject(new Error('no fetch in test'));
  window.JOB_ID = 'test-job';
  window.CSS = { escape: s => s };  // minimal CSS.escape stub

  function runScript(filePath) {
    const code = fs.readFileSync(filePath, 'utf8');
    const el = document.createElement('script');
    el.textContent = code;
    document.head.appendChild(el);
  }

  runScript(APP_JS);
  runScript(REVIEW_JS);
  runScript(EDIT_JS);

  // Dispatch DOMContentLoaded so delegated handlers attach in JSDOM
  document.dispatchEvent(new window.Event('DOMContentLoaded'));

  // Bootstrap state with fixture data
  const result = JSON.parse(fs.readFileSync(FIXTURE, 'utf8'));
  const RV = window.RV;
  RV.state.result = result;
  RV.state.showRemoved = false;
  RV.state.revision = 0;

  return { dom, window, document, RV };
}

// ── Helper: make a minimal item ──────────────────────────────────────────────

function makeItem(uid, overrides) {
  return Object.assign({
    id: 'T-001',
    title: 'Test item',
    description: 'A test',
    source_section: 'Section 1',
    _meta: { uid, origin: 'ai', version: 1, status: 'active', edited: false },
  }, overrides);
}

// ── Test 1: mutationSuccess updates item in skill bucket ─────────────────────

async function main() {

(function test1() {
  console.log('\nTest 1: mutationSuccess replaces item in skill bucket');
  const { RV } = buildEnv();

  const result = RV.state.result;
  // Find or create a skill output and inject a synthetic uid'd item
  let so = (result.skill_outputs || []).find(s => s.items && s.items.length > 0);
  if (!so) {
    if (!result.skill_outputs) result.skill_outputs = [];
    so = { skill_slug: 'test-skill', items: [] };
    result.skill_outputs.push(so);
  }

  const uid = 'itm_t1aabbccdd00';
  const originalItem = makeItem(uid, {});
  so.items.push(originalItem);

  const updatedItem = Object.assign({}, originalItem, { title: 'Updated title' });
  updatedItem._meta = Object.assign({}, originalItem._meta, { version: 2, edited: true });

  RV.mutationSuccess(so.skill_slug, uid, updatedItem, { revision: 5 });

  const soAfter = result.skill_outputs.find(s => s.skill_slug === so.skill_slug);
  const itemAfter = soAfter.items.find(i => i._meta && i._meta.uid === uid);
  assert(itemAfter && itemAfter.title === 'Updated title', 'item title updated in state');
  assert(RV.state.revision === 5, 'revision updated to 5');
})();

// ── Test 2: mutationSuccess creates SkillOutput for empty bucket ─────────────

(function test2() {
  console.log('\nTest 2: mutationSuccess creates SkillOutput for new bucket');
  const { RV } = buildEnv();

  const result = RV.state.result;
  const newBucket = 'brand-new-skill';
  // Ensure bucket doesn't exist
  result.skill_outputs = (result.skill_outputs || []).filter(s => s.skill_slug !== newBucket);

  const newItem = makeItem('itm_aabbccddee11', { _meta: { uid: 'itm_aabbccddee11', origin: 'user', version: 1, status: 'active', edited: false } });
  RV.mutationSuccess(newBucket, 'itm_aabbccddee11', newItem, { revision: 1 });

  const so = (result.skill_outputs || []).find(s => s.skill_slug === newBucket);
  assert(!!so, 'SkillOutput created for new bucket');
  assert(so.items && so.items.length === 1, 'item added to new SkillOutput');
  assert(result.fired_skills && result.fired_skills.includes(newBucket), 'bucket added to fired_skills');
})();

// ── Test 3: mutationSuccess updates spine items ───────────────────────────────

(function test3() {
  console.log('\nTest 3: mutationSuccess replaces item in spine bucket');
  const { RV } = buildEnv();

  const result = RV.state.result;
  if (!result.spine) result.spine = {};

  // Always use a synthetic uid'd item so the test is fixture-independent
  const uid = 'itm_spine001xx';
  const synth = makeItem(uid, {});
  result.spine.compliance = (result.spine.compliance || []).concat([synth]);

  const updated = Object.assign({}, synth, { title: 'Spine updated' });
  updated._meta = Object.assign({}, synth._meta, { version: 3 });

  RV.mutationSuccess('compliance', uid, updated, { revision: 7 });

  const itemAfter = result.spine.compliance.find(i => i._meta && i._meta.uid === uid);
  assert(itemAfter && itemAfter.title === 'Spine updated', 'spine item updated');
  assert(RV.state.revision === 7, 'revision updated from spine mutation');
})();

// ── Test 4: mutationSuccess revision update ───────────────────────────────────

(function test4() {
  console.log('\nTest 4: mutationSuccess preserves existing revision when editState.revision is falsy');
  const { RV } = buildEnv();

  RV.state.revision = 42;
  const result = RV.state.result;
  if (!result.spine) result.spine = { compliance: [] };
  const item = makeItem('itm_revtest', {});
  result.spine.compliance.push(item);

  RV.mutationSuccess('compliance', 'itm_revtest', item, {});  // no revision in editState

  assert(RV.state.revision === 42, 'revision unchanged when editState.revision falsy');
})();

// ── Test 5: replaceRow replaces an <li> ───────────────────────────────────────

(function test5() {
  console.log('\nTest 5: replaceRow replaces <li> in place');
  const { document, RV } = buildEnv();

  const panels = document.getElementById('tab-panels');
  panels.innerHTML = '<ul><li data-uid="itm_testli01"><span>Old content</span></li></ul>';

  const newHtml = '<li data-uid="itm_testli01"><span>New content</span></li>';
  RV.replaceRow('itm_testli01', newHtml);

  const li = panels.querySelector('[data-uid="itm_testli01"]');
  assert(!!li, 'li element still present after replace');
  assert(li && li.querySelector('span') && li.querySelector('span').textContent === 'New content', 'li content replaced');
})();

// ── Test 6: replaceRow replaces a <tr> ───────────────────────────────────────

(function test6() {
  console.log('\nTest 6: replaceRow replaces <tr> in place');
  const { document, RV } = buildEnv();

  const panels = document.getElementById('tab-panels');
  panels.innerHTML = '<table><tbody><tr data-uid="itm_testtr01"><td>Old</td></tr></tbody></table>';

  const newHtml = '<tr data-uid="itm_testtr01"><td>New</td></tr>';
  RV.replaceRow('itm_testtr01', newHtml);

  const tr = panels.querySelector('[data-uid="itm_testtr01"]');
  assert(!!tr, 'tr still present after replace');
  assert(tr && tr.querySelector('td') && tr.querySelector('td').textContent === 'New', 'tr content replaced');
})();

// ── Test 7: rerenderSection re-renders skill panel ────────────────────────────

(function test7() {
  console.log('\nTest 7: rerenderSection re-renders a skill panel when active');
  const { RV, document } = buildEnv();

  const result = RV.state.result;
  // Use the first fired skill that is not 'ams'
  const slug = (result.fired_skills || []).find(s => s !== 'ams');
  if (!slug) { console.log('  SKIP: no non-ams skill in fixture'); return; }

  RV.state.activeTab = slug;

  // Mark a flag by monkey-patching
  let renderCalled = false;
  const orig = RV.renderSkillPanel;
  RV.renderSkillPanel = function(s, c) { if (s === slug) renderCalled = true; orig.call(this, s, c); };

  RV.rerenderSection([slug]);

  assert(renderCalled, 'renderSkillPanel called for active skill tab');
  RV.renderSkillPanel = orig;
})();

// ── Test 8: rerenderSection re-renders AMS requirements card ─────────────────

(function test8() {
  console.log('\nTest 8: rerenderSection replaces AMS section card');
  const { document, RV } = buildEnv();

  const result = RV.state.result;
  // Ensure ams tab is rendered so cards exist
  RV.state.activeTab = 'ams';
  const panels = document.getElementById('tab-panels');

  // Full render to create the ams-container
  panels.innerHTML = '';
  RV.renderSkillPanel('ams', panels);

  const reqCardBefore = panels.querySelector('.ams-section--requirements');
  if (!reqCardBefore) {
    console.log('  SKIP: no .ams-section--requirements card rendered (no AMS items in fixture)');
    return;
  }

  // Add a synthetic item to requirements and re-render
  const amsOutput = (result.skill_outputs || []).find(s => s.skill_slug === 'ams');
  if (!amsOutput) { console.log('  SKIP: no ams output in fixture'); return; }
  amsOutput.items.push(makeItem('itm_newreq01', {
    artifact_type: 'req_classification',
    _meta: { uid: 'itm_newreq01', origin: 'user', version: 1, status: 'active', edited: false },
  }));

  RV.rerenderSection(['requirements']);

  const reqCardAfter = panels.querySelector('.ams-section--requirements');
  assert(!!reqCardAfter, 'requirements section card still present after rerender');
  // The card should contain the new item's uid
  const newRow = panels.querySelector('[data-uid="itm_newreq01"]');
  assert(!!newRow, 'new item row present after rerenderSection');
})();

// ── Test 9: stats-bar renders editState counts line (1.4) ────────────────────

(function test9() {
  console.log('\nTest 9: stats-bar renders editState counts line');
  const { RV, document } = buildEnv();

  RV.state.editState = { edited: 2, user_added: 3, removed: 1, active: 10 };
  RV.renderStats(RV.state.result);

  const editsEl = document.getElementById('stats-bar-edits');
  assert(!!editsEl, 'stats-bar-edits element rendered');
  assert(editsEl && editsEl.textContent === '2 edited · 3 added · 1 removed', 'counts line matches editState');

  // mutationSuccess updates editState and refreshes stats bar
  const result = RV.state.result;
  const uid = 'itm_countstest01';
  const item = makeItem(uid, {});
  result.spine.compliance.push(item);

  RV.mutationSuccess('compliance', uid, item, {
    revision: 6,
    edited: 4,
    user_added: 5,
    removed: 2,
  });

  const updatedEditsEl = document.getElementById('stats-bar-edits');
  assert(updatedEditsEl && updatedEditsEl.textContent === '4 edited · 5 added · 2 removed', 'counts line updated after mutationSuccess');
})();

// ── Test 10: Removed-items toggle show/hide (AT-18) ─────────────────────────

(function test10() {
  console.log('\nTest 10: Removed-items toggle show/hide (AT-18)');
  const { RV, document } = buildEnv();

  const panels = document.getElementById('tab-panels');
  const result = RV.state.result;
  if (!result.spine) result.spine = {};

  const activeItem = makeItem('itm_active01', { title: 'Active requirement', _meta: { uid: 'itm_active01', status: 'active', origin: 'ai' } });
  const removedItem = makeItem('itm_rem01', { title: 'Removed requirement', _meta: { uid: 'itm_rem01', status: 'removed', origin: 'ai' } });

  result.spine.risk = [activeItem, removedItem];
  RV.state.activeTab = 'risk';
  RV.state.showRemoved = false;

  RV.renderTabPanel('risk');

  // Initially showRemoved=false: active item present, removed item hidden
  let activeRow = panels.querySelector('[data-uid="itm_active01"]');
  let removedRow = panels.querySelector('[data-uid="itm_rem01"]');
  let toggleBtn = panels.querySelector('.toggle-removed-btn');

  assert(!!activeRow, 'active item visible');
  assert(!removedRow, 'removed item hidden by default');
  assert(!!toggleBtn && toggleBtn.textContent === 'Show removed (1)', 'footer toggle shows "Show removed (1)"');

  // Click toggle button
  toggleBtn.click();

  activeRow = panels.querySelector('[data-uid="itm_active01"]');
  removedRow = panels.querySelector('[data-uid="itm_rem01"]');
  toggleBtn = panels.querySelector('.toggle-removed-btn');

  assert(RV.state.showRemoved === true, 'showRemoved flipped to true');
  assert(!!removedRow, 'removed item now visible');
  assert(removedRow && removedRow.classList.contains('req-row--removed'), 'removed row has req-row--removed class');
  assert(!!removedRow.querySelector('[data-action="restore-item"]'), 'removed row has restore button');
  assert(!!toggleBtn && toggleBtn.textContent === 'Hide removed (1)', 'footer toggle shows "Hide removed (1)"');

  // Click toggle button again
  toggleBtn.click();

  removedRow = panels.querySelector('[data-uid="itm_rem01"]');
  toggleBtn = panels.querySelector('.toggle-removed-btn');

  assert(RV.state.showRemoved === false, 'showRemoved flipped back to false');
  assert(!removedRow, 'removed item hidden again');
  assert(!!toggleBtn && toggleBtn.textContent === 'Show removed (1)', 'footer toggle shows "Show removed (1)" again');
})();

// ── Test 11: Rate-card notice wording (AT-21) ────────────────────────────────

(function test11() {
  console.log('\nTest 11: Rate-card notice wording (AT-21)');
  const { RV, document } = buildEnv();

  const panels = document.getElementById('tab-panels');
  const result = RV.state.result;
  if (!result.skill_outputs) result.skill_outputs = [];

  // 1. Rate-card with no_roles_stated: true, no items
  let rc = result.skill_outputs.find(s => s.skill_slug === 'rate-card');
  if (!rc) {
    rc = { skill_slug: 'rate-card', no_roles_stated: true, items: [] };
    result.skill_outputs.push(rc);
  } else {
    rc.no_roles_stated = true;
    rc.items = [];
  }

  RV.state.activeTab = 'rate-card';
  panels.innerHTML = '';
  RV.renderSkillPanel('rate-card', panels);

  let notice = panels.querySelector('.notice');
  assert(!!notice && notice.textContent.includes('This RFP does not specify named roles or a rate card'), 'baseline notice when no roles stated');

  // 2. User adds a role
  rc.items.push(makeItem('itm_userrole01', {
    title: 'Lead Architect',
    _meta: { uid: 'itm_userrole01', origin: 'user', status: 'active', version: 1 },
  }));

  panels.innerHTML = '';
  RV.renderSkillPanel('rate-card', panels);

  notice = panels.querySelector('.notice');
  assert(!!notice && notice.textContent.includes('This RFP does not specify roles. The roles below were added manually.'), 'notice updated when user-added items present');
})();

// ── Test 12: Reset all edits (1.4) ──────────────────────────────────────────

await (async function test12() {
  console.log('\nTest 12: Reset all edits triggers POST /reset and reloads');
  const { RV, window } = buildEnv();

  let apiSendCalled = false;
  let sentUrl = '';
  let loadCalled = false;

  window.confirm = () => true;
  window.V2.apiSend = async (method, url) => {
    apiSendCalled = true;
    sentUrl = url;
    return {
      archived: 'working.123.bak.json',
      edit_state: { revision: 0, has_edits: false, edited: 0, user_added: 0, removed: 0 },
      message: 'Reset complete',
    };
  };

  RV.load = async () => {
    loadCalled = true;
  };

  await RV.resetEdits();

  assert(apiSendCalled && sentUrl.includes('/jobs/test-job/reset?confirm=true'), 'POST /reset?confirm=true called');
  assert(loadCalled, 'RV.load() called for full reload after reset');
  assert(RV.state.revision === 0, 'revision reset to 0');
})();

// ── Test 13: _renderHistoryEntries renders entries via textContent (AT-20) ───

(function test13() {
  console.log('\nTest 13: _renderHistoryEntries renders entries without innerHTML injection (AT-20)');
  const { RV, document } = buildEnv();

  // Create a minimal body element
  const body = document.createElement('div');
  body.id = 'history-panel-body';
  document.body.appendChild(body);

  const entries = [
    {
      ts: '2024-06-01T12:00:00Z',
      op: 'patch',
      uid: 'itm_abc123',
      id: 'DB-001',
      bucket: 'design-build',
      fields_changed: { title: { from: 'Old title', to: 'New title' } },
      actor: 'local',
      revision: 3,
    },
  ];

  RV._renderHistoryEntries(entries, false);

  const opEl = body.querySelector('.history-entry__op');
  assert(!!opEl && opEl.textContent === 'patch', 'op badge has correct textContent');

  const idEl = body.querySelector('.history-entry__id');
  assert(!!idEl && idEl.textContent === 'DB-001', 'id rendered via textContent');

  const entryRows = body.querySelectorAll('.history-entry');
  assert(entryRows.length === 1, 'one history-entry row rendered');

  const fieldsEl = body.querySelector('.history-entry__fields');
  assert(!!fieldsEl, 'fields_changed section rendered');
  assert(fieldsEl.textContent.includes('title'), 'field name present');
  assert(fieldsEl.textContent.includes('Old title'), 'from value present');
  assert(fieldsEl.textContent.includes('New title'), 'to value present');

  document.body.removeChild(body);
})();

// ── Test 14: _renderHistoryEntries XSS safety (AT-20) ────────────────────────

(function test14() {
  console.log('\nTest 14: _renderHistoryEntries XSS — malicious text stored as textContent');
  const { RV, document } = buildEnv();

  const body = document.createElement('div');
  body.id = 'history-panel-body';
  document.body.appendChild(body);

  const xss = '<img src=x onerror=alert(1)>';
  const entries = [
    {
      ts: '2024-06-01T12:00:00Z',
      op: 'patch',
      uid: 'itm_xss',
      id: xss,
      bucket: 'design-build',
      fields_changed: { title: { from: xss, to: xss } },
      actor: 'local',
      revision: 1,
    },
  ];

  RV._renderHistoryEntries(entries, false);

  // The XSS string must appear as raw text, not as an injected element
  const imgs = body.querySelectorAll('img');
  assert(imgs.length === 0, 'no <img> elements injected (XSS neutralised)');

  const idEl = body.querySelector('.history-entry__id');
  assert(!!idEl && idEl.textContent === xss, 'XSS string safely stored as textContent');

  document.body.removeChild(body);
})();

// ── Test 15: _renderHistoryEntries empty list shows "No edits yet." ───────────

(function test15() {
  console.log('\nTest 15: _renderHistoryEntries with empty list shows empty state');
  const { RV, document } = buildEnv();

  const body = document.createElement('div');
  body.id = 'history-panel-body';
  document.body.appendChild(body);

  RV._renderHistoryEntries([], false);

  const emptyEl = body.querySelector('.history-panel__empty');
  assert(!!emptyEl && emptyEl.textContent === 'No edits yet.', 'empty state message shown');

  document.body.removeChild(body);
})();

// ── Test 16: _renderHistoryEntries append mode adds to existing entries ────────

(function test16() {
  console.log('\nTest 16: _renderHistoryEntries append=true adds rows without clearing');
  const { RV, document } = buildEnv();

  const body = document.createElement('div');
  body.id = 'history-panel-body';
  document.body.appendChild(body);

  const entry = (op, id) => ({
    ts: '2024-06-01T12:00:00Z', op, uid: 'itm_x', id, bucket: 'b',
    fields_changed: {}, actor: 'local', revision: 1,
  });

  RV._renderHistoryEntries([entry('patch', 'DB-001')], false);
  RV._renderHistoryEntries([entry('add',   'DB-002')], true);

  const rows = body.querySelectorAll('.history-entry');
  assert(rows.length === 2, 'two rows after append');

  document.body.removeChild(body);
})();

// ── Test 17: openHistoryPanel creates panel and btn-history aria-expanded ─────

await (async function test17() {
  console.log('\nTest 17: openHistoryPanel opens panel and sets aria-expanded');
  const { RV, window, document } = buildEnv();

  // Add the btn-history button to DOM
  const btn = document.createElement('button');
  btn.id = 'btn-history';
  btn.setAttribute('aria-expanded', 'false');
  document.body.appendChild(btn);

  // Stub V2.apiGet to return empty history
  window.V2.apiGet = async (_url) => ({
    entries: [], total: 0, page: 1, page_size: 50,
  });

  await RV.openHistoryPanel();

  const panel = document.getElementById('history-panel');
  assert(!!panel, 'history-panel element created');
  assert(panel.style.display === 'flex', 'panel display set to flex');
  assert(btn.getAttribute('aria-expanded') === 'true', 'btn aria-expanded=true');
})();

// ── Test 18: closeHistoryPanel hides panel and resets aria-expanded ───────────

(function test18() {
  console.log('\nTest 18: closeHistoryPanel hides panel');
  const { RV, document } = buildEnv();

  // Create panel manually
  const panel = document.createElement('div');
  panel.id = 'history-panel';
  panel.style.display = 'flex';
  document.body.appendChild(panel);

  const btn = document.createElement('button');
  btn.id = 'btn-history';
  btn.setAttribute('aria-expanded', 'true');
  document.body.appendChild(btn);

  RV.closeHistoryPanel();

  assert(panel.style.display === 'none', 'panel hidden after closeHistoryPanel');
  assert(btn.getAttribute('aria-expanded') === 'false', 'btn aria-expanded=false after close');
})();

// ── Test 19: clearSelection unsets selection and removes bulk-bar ─────────────

(function test19() {
  console.log('\nTest 19: clearSelection unsets selection and removes bulk-bar');
  const { RV, document } = buildEnv();

  // Pre-populate selection
  RV.state.selection = new Set(['itm_a1', 'itm_a2']);
  RV._renderBulkBar();

  let bar = document.getElementById('bulk-bar');
  assert(!!bar, 'bulk-bar visible when selection non-empty');

  RV.clearSelection();

  bar = document.getElementById('bulk-bar');
  assert(RV.state.selection.size === 0, 'selection cleared');
  assert(!bar, 'bulk-bar removed after clearSelection');
})();

// ── Test 20: _renderBulkBar shows correct count text ─────────────────────────

(function test20() {
  console.log('\nTest 20: _renderBulkBar shows correct count text');
  const { RV, document } = buildEnv();

  RV.state.selection = new Set(['itm_x1', 'itm_x2', 'itm_x3']);
  RV._renderBulkBar();

  const bar = document.getElementById('bulk-bar');
  assert(!!bar, 'bulk-bar rendered');

  const count = bar.querySelector('.bulk-bar__count');
  assert(!!count && count.textContent === '3 items selected', 'count text correct for 3 items');
})();

// ── Test 21: _renderBulkBar singular form ────────────────────────────────────

(function test21() {
  console.log('\nTest 21: _renderBulkBar singular "1 item selected"');
  const { RV, document } = buildEnv();

  RV.state.selection = new Set(['itm_solo']);
  RV._renderBulkBar();

  const bar = document.getElementById('bulk-bar');
  const count = bar && bar.querySelector('.bulk-bar__count');
  assert(!!count && count.textContent === '1 item selected', 'singular form used for 1 item');
})();

// ── Test 22: _applyBulkResults updates item status in RV.state ───────────────

(function test22() {
  console.log('\nTest 22: _applyBulkResults updates item meta in RV.state');
  const { RV } = buildEnv();

  const result = RV.state.result;
  if (!result.spine) result.spine = {};

  const uid = 'itm_bulk_apply01';
  const item = makeItem(uid, {});
  result.spine.compliance = (result.spine.compliance || []).concat([item]);

  // Simulate bulk remove response
  const bulkResults = [{ uid, ok: true, meta: { status: 'removed', version: 2 } }];
  RV._applyBulkResults(bulkResults, { revision: 10 });

  const found = RV.findItemByUid(uid);
  assert(!!found, 'item still in state after _applyBulkResults');
  assert(found.item._meta.status === 'removed', 'item status updated to removed');
  assert(found.item._meta.version === 2, 'item version updated');
  assert(RV.state.revision === 10, 'revision updated from bulk edit_state');
})();

// ── Test 23: _applyBulkResults skips ok=false entries ────────────────────────

(function test23() {
  console.log('\nTest 23: _applyBulkResults skips ok=false entries');
  const { RV } = buildEnv();

  const result = RV.state.result;
  if (!result.spine) result.spine = {};

  const uid = 'itm_bulk_skip01';
  const item = makeItem(uid, {});
  result.spine.risk = (result.spine.risk || []).concat([item]);

  const originalStatus = item._meta.status;
  const bulkResults = [{ uid, ok: false, error: 'version mismatch', meta: { status: 'active', version: 1 } }];
  RV._applyBulkResults(bulkResults, { revision: 99 });

  const found = RV.findItemByUid(uid);
  assert(found && found.item._meta.status === originalStatus, 'item status unchanged for ok=false entry');
})();

// ── Test 24: bulkRemove calls POST /items/bulk and clears selection ───────────

await (async function test24() {
  console.log('\nTest 24: bulkRemove calls POST /items/bulk op=remove');
  const { RV, window } = buildEnv();

  const result = RV.state.result;
  if (!result.spine) result.spine = {};

  const uid = 'itm_bulkrem01';
  const item = makeItem(uid, {});
  result.spine.compliance = (result.spine.compliance || []).concat([item]);

  RV.state.selection = new Set([uid]);

  let calledMethod = '';
  let calledUrl = '';
  let calledBody = null;

  window.V2.apiSend = async (method, url, body) => {
    calledMethod = method;
    calledUrl = url;
    calledBody = body;
    return {
      results: [{ uid, ok: true, meta: { status: 'removed', version: 2 } }],
      edit_state: { revision: 5 },
      revision: 5,
    };
  };

  await RV.bulkRemove();

  assert(calledMethod === 'POST', 'POST method used');
  assert(calledUrl.includes('/items/bulk'), 'URL includes /items/bulk');
  assert(calledBody && calledBody.op === 'remove', 'op=remove in request body');
  assert(calledBody && calledBody.uids.includes(uid), 'uid in request body');
  assert(RV.state.selection.size === 0, 'selection cleared after bulkRemove');
})();

// ── Test 25: bulkRestore calls POST /items/bulk op=restore ───────────────────

await (async function test25() {
  console.log('\nTest 25: bulkRestore calls POST /items/bulk op=restore');
  const { RV, window } = buildEnv();

  const result = RV.state.result;
  if (!result.spine) result.spine = {};

  const uid = 'itm_bulkrest01';
  const item = makeItem(uid, { _meta: { uid, origin: 'ai', version: 1, status: 'removed', edited: false } });
  result.spine.risk = (result.spine.risk || []).concat([item]);

  RV.state.selection = new Set([uid]);

  let calledOp = '';
  window.V2.apiSend = async (method, url, body) => {
    calledOp = body && body.op;
    return {
      results: [{ uid, ok: true, meta: { status: 'active', version: 3 } }],
      edit_state: { revision: 6 },
      revision: 6,
    };
  };

  await RV.bulkRestore();

  assert(calledOp === 'restore', 'op=restore in request body');
  assert(RV.state.selection.size === 0, 'selection cleared after bulkRestore');
})();

// ── Test 26: _showWarningsBanner renders duplicate warning and dismiss ────────

(function test26() {
  console.log('\nTest 26: _showWarningsBanner renders duplicate warning banner and dismiss');
  const { RV, document } = buildEnv();

  // Build a minimal drawer DOM with the banner element
  const drawer = document.createElement('div');
  drawer.id = 'edit-drawer';
  const banner = document.createElement('div');
  banner.className = 'drawer__duplicate-banner';
  banner.style.display = 'none';
  drawer.appendChild(banner);
  document.body.appendChild(drawer);

  const warnings = [
    { type: 'possible_duplicate', uid: 'itm_abc123', id: 'DB-001', similarity: 0.95 },
  ];

  const shown = RV._showWarningsBanner(drawer, warnings);

  assert(shown === true, '_showWarningsBanner returns true when warnings present');
  assert(banner.style.display !== 'none', 'banner is visible');
  const li = banner.querySelector('li');
  assert(!!li, 'list item rendered');
  assert(li.textContent.includes('DB-001'), 'item id shown');
  assert(li.textContent.includes('95%'), 'similarity percentage shown');

  // Dismiss button hides banner
  const dismissBtn = banner.querySelector('.drawer__banner-dismiss');
  assert(!!dismissBtn, 'dismiss button present');
  dismissBtn.click();
  assert(banner.style.display === 'none', 'banner hidden after dismiss');

  document.body.removeChild(drawer);
})();

// ── Test 27: _showWarningsBanner hides banner when warnings empty ─────────────

(function test27() {
  console.log('\nTest 27: _showWarningsBanner hides banner when no warnings');
  const { RV, document } = buildEnv();

  const drawer = document.createElement('div');
  drawer.id = 'edit-drawer-27';
  const banner = document.createElement('div');
  banner.className = 'drawer__duplicate-banner';
  banner.style.display = '';
  drawer.appendChild(banner);
  document.body.appendChild(drawer);

  const shown = RV._showWarningsBanner(drawer, []);

  assert(shown === false, '_showWarningsBanner returns false for empty warnings');
  assert(banner.style.display === 'none', 'banner hidden for empty warnings');

  document.body.removeChild(drawer);
})();

// ── Test 28: _renderEnhanceDiff renders per-field before/after diff ──────────

(function test28() {
  console.log('\nTest 28: _renderEnhanceDiff renders before/after fields and rationale');
  const { RV, document } = buildEnv();

  // Inject a uid'd item into state
  const result = RV.state.result;
  if (!result.spine) result.spine = {};
  const uid = 'itm_enhance_t28';
  const item = makeItem(uid, {
    title: 'Old title',
    description: 'Old description',
    _meta: { uid, origin: 'ai', version: 1, status: 'active', edited: false },
  });
  result.spine.compliance = (result.spine.compliance || []).concat([item]);

  // Create a minimal panel element with the required structure
  const panel = document.createElement('div');
  panel.dataset.uid = uid;
  const diffArea = document.createElement('div');
  diffArea.className = 'enhance-panel__diff';
  panel.appendChild(diffArea);
  document.body.appendChild(panel);

  const proposal = {
    proposed: { title: 'New clearer title', description: 'Improved description' },
    rationale: 'Wording was ambiguous.',
    needs_info: ['Clarify timeline', 'Define acceptance criteria'],
    unverified_numbers: [],
    mode: 'clarify_wording',
  };

  RV._renderEnhanceDiff(panel, proposal);

  const fields = diffArea.querySelectorAll('.enhance-diff-field');
  assert(fields.length === 2, 'two field diffs rendered');

  // Check field name rendered
  const firstName = fields[0].querySelector('.enhance-diff-field__name');
  assert(!!firstName, 'field name element present');

  // Check before value (textContent, not innerHTML)
  const beforeText = fields[0].querySelector('.enhance-diff-col--before .enhance-diff-col__text');
  assert(!!beforeText, 'before text element present');
  // First field alphabetically is 'description' or 'title' — just check it's non-empty
  assert(beforeText.textContent === 'Old title' || beforeText.textContent === 'Old description', 'before value is original item field value');

  // Check after textarea
  const afterTA = fields[0].querySelector('.enhance-diff-col__textarea');
  assert(!!afterTA, 'editable after-textarea present');

  // Rationale
  const rationale = diffArea.querySelector('.enhance-rationale');
  assert(!!rationale, 'rationale section rendered');
  const rationaleText = rationale.querySelector('.enhance-rationale__text');
  assert(!!rationaleText && rationaleText.textContent === 'Wording was ambiguous.', 'rationale text correct');

  // needs_info
  const niWrap = diffArea.querySelector('.enhance-needs-info');
  assert(!!niWrap, 'needs_info section rendered');
  const niItems = niWrap.querySelectorAll('li');
  assert(niItems.length === 2, 'two needs_info items');
  assert(niItems[0].textContent === 'Clarify timeline', 'first needs_info item correct');

  document.body.removeChild(panel);
})();

// ── Test 29: _renderEnhanceDiff shows unverified_numbers banner ───────────────

(function test29() {
  console.log('\nTest 29: _renderEnhanceDiff shows unverified_numbers banner');
  const { RV, document } = buildEnv();

  const result = RV.state.result;
  if (!result.spine) result.spine = {};
  const uid = 'itm_enhance_t29';
  const item = makeItem(uid, { _meta: { uid, origin: 'ai', version: 1, status: 'active', edited: false } });
  result.spine.risk = (result.spine.risk || []).concat([item]);

  const panel = document.createElement('div');
  panel.dataset.uid = uid;
  const diffArea = document.createElement('div');
  diffArea.className = 'enhance-panel__diff';
  panel.appendChild(diffArea);
  document.body.appendChild(panel);

  const proposal = {
    proposed: { title: 'Updated title with 99% SLA' },
    rationale: 'Made measurable.',
    needs_info: [],
    unverified_numbers: ['99%'],
    mode: 'measurable',
  };

  RV._renderEnhanceDiff(panel, proposal);

  const banner = diffArea.querySelector('.enhance-unverified-banner');
  assert(!!banner, 'unverified-numbers banner rendered');
  assert(banner.textContent.includes('99%'), 'unverified number shown in banner');

  // No img injection (XSS safety)
  const imgs = diffArea.querySelectorAll('img');
  assert(imgs.length === 0, 'no <img> elements in diff (XSS safe)');

  document.body.removeChild(panel);
})();

// ── Test 30: _renderEnhanceDiff XSS safety — proposal text as textContent ─────

(function test30() {
  console.log('\nTest 30: _renderEnhanceDiff XSS — proposal text stored as textContent (AT-20)');
  const { RV, document } = buildEnv();

  const result = RV.state.result;
  if (!result.spine) result.spine = {};
  const uid = 'itm_enhance_t30';
  const item = makeItem(uid, { _meta: { uid, origin: 'ai', version: 1, status: 'active', edited: false } });
  result.spine.compliance = (result.spine.compliance || []).concat([item]);

  const panel = document.createElement('div');
  panel.dataset.uid = uid;
  const diffArea = document.createElement('div');
  diffArea.className = 'enhance-panel__diff';
  panel.appendChild(diffArea);
  document.body.appendChild(panel);

  const xss = '<img src=x onerror=alert(1)>';
  const proposal = {
    proposed: { title: xss },
    rationale: xss,
    needs_info: [xss],
    unverified_numbers: [xss],
    mode: 'clarify_wording',
  };

  RV._renderEnhanceDiff(panel, proposal);

  const imgs = diffArea.querySelectorAll('img');
  assert(imgs.length === 0, 'no <img> elements injected by XSS payload');

  // The XSS string should appear as literal text
  const afterTA = diffArea.querySelector('.enhance-diff-col__textarea');
  assert(!!afterTA && afterTA.value === xss, 'XSS payload in textarea as literal text');

  const rationaleText = diffArea.querySelector('.enhance-rationale__text');
  assert(!!rationaleText && rationaleText.textContent === xss, 'XSS in rationale as textContent');

  const niItem = diffArea.querySelector('.enhance-needs-info__list li');
  assert(!!niItem && niItem.textContent === xss, 'XSS in needs_info as textContent');

  document.body.removeChild(panel);
})();

// ── Test 31: Accept calls enhance/accept endpoint and calls mutationSuccess ────

await (async function test31() {
  console.log('\nTest 31: _acceptProposal calls /enhance/accept and updates state');
  const { RV, window, document } = buildEnv();

  const result = RV.state.result;
  if (!result.spine) result.spine = {};
  const uid = 'itm_enhance_t31';
  const item = makeItem(uid, {
    title: 'Before title',
    _meta: { uid, origin: 'ai', version: 1, status: 'active', edited: false, enhanced: false },
  });
  result.spine.compliance = (result.spine.compliance || []).concat([item]);

  // Create the enhance panel in DOM
  RV.openEnhancePanel(uid);
  const panel = document.getElementById('enhance-panel');
  assert(!!panel, 'enhance panel created by openEnhancePanel');
  assert(panel.style.display === 'flex', 'panel is visible');

  // Inject a proposal as if the enhance endpoint was called
  const proposal = {
    proposed: { title: 'Improved title' },
    rationale: 'Clearer.',
    needs_info: [],
    unverified_numbers: [],
    mode: 'clarify_wording',
  };
  panel._proposal    = proposal;
  panel._baseVersion = 1;

  // Render diff so accept can read textareas
  RV._renderEnhanceDiff(panel, proposal);
  const footer = panel.querySelector('.enhance-panel__footer');
  if (footer) footer.style.display = '';

  // Stub apiSend
  let calledUrl = '';
  let calledBody = null;
  const updatedItem = makeItem(uid, {
    title: 'Improved title',
    _meta: { uid, origin: 'ai', version: 2, status: 'active', edited: true, enhanced: true },
  });
  window.V2.apiSend = async (method, url, body) => {
    calledUrl = url;
    calledBody = body;
    return {
      item: updatedItem,
      edit_state: { revision: 3, edited: 1, user_added: 0, removed: 0 },
    };
  };

  await RV._acceptProposal();

  assert(calledUrl.includes('/enhance/accept'), 'accept endpoint called');
  assert(calledBody && calledBody.base_version === 1, 'base_version sent');
  assert(calledBody && calledBody.proposal && calledBody.proposal.proposed.title === 'Improved title', 'proposal sent with editable value');

  // State should be updated
  const found = RV.findItemByUid(uid);
  assert(!!found && found.item._meta.enhanced === true, 'item marked enhanced in state');
  assert(found && found.item.title === 'Improved title', 'title updated in state');

  // Panel should close
  assert(panel.style.display === 'none', 'panel closed after accept');
})();

// ── Test 32: Reject closes panel without calling the API ──────────────────────

(function test32() {
  console.log('\nTest 32: Reject closes panel without saving');
  const { RV, window, document } = buildEnv();

  const result = RV.state.result;
  if (!result.spine) result.spine = {};
  const uid = 'itm_enhance_t32';
  const item = makeItem(uid, { _meta: { uid, origin: 'ai', version: 1, status: 'active', edited: false } });
  result.spine.risk = (result.spine.risk || []).concat([item]);

  RV.openEnhancePanel(uid);
  const panel = document.getElementById('enhance-panel');
  assert(!!panel, 'panel opened');

  let apiCalled = false;
  window.V2.apiSend = async () => { apiCalled = true; return {}; };

  // Simulate reject
  RV._closeEnhancePanel();

  assert(panel.style.display === 'none', 'panel hidden after reject/close');
  assert(apiCalled === false, 'no API call made on reject');
})();

// ── Test 33: In-flight guard prevents concurrent enhance calls ────────────────

await (async function test33() {
  console.log('\nTest 33: In-flight guard blocks second enhance while one is pending');
  const { RV, window, document } = buildEnv();

  const result = RV.state.result;
  if (!result.spine) result.spine = {};
  const uid = 'itm_enhance_t33';
  const item = makeItem(uid, { _meta: { uid, origin: 'ai', version: 1, status: 'active', edited: false } });
  result.spine.compliance = (result.spine.compliance || []).concat([item]);

  RV.openEnhancePanel(uid);

  let callCount = 0;
  let resolveFirst;
  window.V2.apiSend = async () => {
    callCount++;
    return new Promise(resolve => { resolveFirst = resolve; });
  };

  // Start first enhance (won't resolve yet)
  const p1 = RV._submitEnhance();

  // Attempt second enhance while first is pending — should be blocked
  const p2 = RV._submitEnhance();

  // The guard should have prevented the second call from actually calling apiSend again
  // (callCount stays at 1 because the guard returns early)
  assert(callCount === 1, 'second _submitEnhance blocked by in-flight guard');

  // Resolve the first call
  if (resolveFirst) resolveFirst({ uid, base_version: 1, proposal: { proposed: {}, rationale: '', needs_info: [], unverified_numbers: [] } });
  await p1;
  await p2;

  // Guard should be clear after completion
  assert(RV._enhancePending === null, 'enhancePending cleared after completion');
})();

// ── Summary ───────────────────────────────────────────────────────────────────

console.log('\n────────────────────────────────────────────────');
console.log(`Results: ${passed} passed, ${failed} failed`);
if (failed > 0) process.exit(1);
else { console.log('All tests passed.'); process.exit(0); }

}

main().catch(err => {
  console.error(err);
  process.exit(1);
});
