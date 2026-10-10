/**
 * tests/capture_ams_snapshot.js
 *
 * Runs the CURRENT (unmodified) renderAMSArtifacts from review.js against
 * the golden fixture and writes the output to tests/fixtures/ams_render_snapshot.html.
 *
 * Usage: node tests/capture_ams_snapshot.js
 *
 * Also usable as a verifier:
 *   node tests/capture_ams_snapshot.js --verify
 * Exits 1 if the current render doesn't match the saved snapshot (whitespace-normalised).
 */

'use strict';

const fs   = require('fs');
const path = require('path');
const { JSDOM } = require('jsdom');

// ── Paths ────────────────────────────────────────────────────────────────────
const ROOT        = path.resolve(__dirname, '..');
const FIXTURE     = path.join(ROOT, 'tests', 'fixtures', 'result.json');
const APP_JS      = path.join(ROOT, 'app', 'static', 'js', 'app.js');
const REVIEW_JS   = path.join(ROOT, 'app', 'static', 'js', 'review.js');
const SNAPSHOT    = path.join(ROOT, 'tests', 'fixtures', 'ams_render_snapshot.html');

const verify = process.argv.includes('--verify');

// ── Set up JSDOM environment ─────────────────────────────────────────────────
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
</body></html>`, {
  runScripts: 'dangerously',
  resources: 'usable',
  url: 'http://localhost/',
});

const { window } = dom;
const { document } = window;

// Stub fetch so loadSkillLabels doesn't throw
window.fetch = () => Promise.reject(new Error('no fetch in test'));
window.JOB_ID = 'test-job';

// ── Load app.js then review.js ───────────────────────────────────────────────
function runScript(filePath) {
  const code = fs.readFileSync(filePath, 'utf8');
  // Use vm via eval inside the window context via JSDOM runScripts
  const scriptEl = document.createElement('script');
  scriptEl.textContent = code;
  document.head.appendChild(scriptEl);
}

runScript(APP_JS);
runScript(REVIEW_JS);

// ── Load fixture and render AMS section ─────────────────────────────────────
const result = JSON.parse(fs.readFileSync(FIXTURE, 'utf8'));
const RV     = window.RV;

if (!RV) {
  console.error('ERROR: window.RV not set after loading review.js');
  process.exit(1);
}

// Bootstrap state
RV.state.result       = result;
RV.state.showRemoved  = false;

// Find AMS items (mirrors renderSkillPanel logic)
const amsOutput = (result.skill_outputs || []).find(s => s.skill_slug === 'ams');
if (!amsOutput) {
  console.error('ERROR: no ams skill_output in fixture');
  process.exit(1);
}

const container = document.getElementById('tab-panels');
container.innerHTML = '';

// Call through the same path as the real page: renderSkillPanel → renderAMSArtifacts
RV.renderSkillPanel('ams', container);

const rendered = container.innerHTML;

// ── Normalise whitespace for comparison ──────────────────────────────────────
function normalise(html) {
  return html
    .replace(/\r\n/g, '\n')
    .replace(/[ \t]+/g, ' ')
    .replace(/\n\s*/g, '\n')
    .trim();
}

if (verify) {
  if (!fs.existsSync(SNAPSHOT)) {
    console.error('ERROR: snapshot file not found:', SNAPSHOT);
    process.exit(1);
  }
  const saved    = fs.readFileSync(SNAPSHOT, 'utf8');
  const normNow  = normalise(rendered);
  const normSaved = normalise(saved);
  if (normNow !== normSaved) {
    console.error('SNAPSHOT MISMATCH — AMS render changed after refactor.');
    // Show a short diff hint
    const nowLines   = normNow.split('\n');
    const savedLines = normSaved.split('\n');
    const maxLines = Math.max(nowLines.length, savedLines.length);
    let firstDiff = -1;
    for (let i = 0; i < maxLines; i++) {
      if ((nowLines[i] || '') !== (savedLines[i] || '')) {
        firstDiff = i;
        break;
      }
    }
    if (firstDiff >= 0) {
      console.error(`  First diff at line ${firstDiff + 1}:`);
      console.error(`  SAVED: ${(savedLines[firstDiff] || '').substring(0, 120)}`);
      console.error(`  NOW  : ${(nowLines[firstDiff]   || '').substring(0, 120)}`);
    }
    process.exit(1);
  }
  console.log('OK — AMS render matches snapshot.');
  process.exit(0);
} else {
  fs.writeFileSync(SNAPSHOT, rendered, 'utf8');
  console.log('Snapshot written to:', SNAPSHOT);
  console.log('Byte length:', Buffer.byteLength(rendered, 'utf8'));
}
