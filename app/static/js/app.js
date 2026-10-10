/* app/static/js/app.js
   Shared helpers for V2 UI */

'use strict';

// ── API helpers ────────────────────────────────────────────────────────────

async function apiGet(url) {
  const resp = await fetch(url, { headers: { 'Accept': 'application/json' } });
  if (!resp.ok) throw new Error(`GET ${url} → ${resp.status}`);
  return resp.json();
}

async function apiPostFile(url, file) {
  const form = new FormData();
  form.append('file', file);
  const resp = await fetch(url, { method: 'POST', body: form });
  if (!resp.ok) {
    const err = await resp.json().catch(() => ({ detail: resp.statusText }));
    throw new Error(err.detail || resp.statusText);
  }
  return resp.json();
}

// ── Polling helper ─────────────────────────────────────────────────────────

function pollJob(jobId, onUpdate, onComplete, onFail, intervalMs = 2000) {
  const interval = setInterval(async () => {
    try {
      const data = await apiGet(`/jobs/${jobId}`);
      onUpdate(data);
      if (data.status === 'complete') {
        clearInterval(interval);
        onComplete(data);
      } else if (data.status === 'failed') {
        clearInterval(interval);
        onFail(data.error || 'Analysis failed');
      }
    } catch (e) {
      clearInterval(interval);
      onFail(e.message);
    }
  }, intervalMs);
  return interval;
}

// ── Tag helpers ────────────────────────────────────────────────────────────

const ARCHETYPE_CLASSES = {
  'design-build':    'design-build',
  'ams':             'ams',
  'rate-card':       'rate-card',
  'packaged':        'packaged',
  'packaged-sap':    'packaged',
  'packaged-salesforce': 'packaged',
  'packaged-oracle': 'packaged',
  'packaged-other':  'packaged',
};

function skillClass(slug) {
  return ARCHETYPE_CLASSES[slug] || 'spine';
}

function makeTag(text, cls) {
  const span = document.createElement('span');
  span.className = `tag tag--${cls}`;
  span.textContent = text;
  return span;
}

// ── Format helpers ─────────────────────────────────────────────────────────

function priorityLabel(p) {
  const map = { must: 'MUST', should: 'SHOULD', could: 'COULD', wont: "WON'T" };
  return map[p] || p;
}

function confidencePct(c) {
  return Math.round((c || 0) * 100);
}

// ── Confidence bar ─────────────────────────────────────────────────────────

function makeConfidenceBar(confidence) {
  const pct = confidencePct(confidence);
  const wrap = document.createElement('span');
  wrap.className = 'confidence-bar';
  wrap.innerHTML = `
    <span class="confidence-bar__track">
      <span class="confidence-bar__fill" style="width:${pct}%"></span>
    </span>
    <span>${pct}%</span>
  `;
  return wrap;
}

// ── String helpers ─────────────────────────────────────────────────────────

function esc(str) {
  return String(str || '')
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}

// ── JSON API helper ─────────────────────────────────────────────────────────

async function apiSend(method, url, body) {
  const resp = await fetch(url, {
    method,
    headers: { 'Content-Type': 'application/json', 'Accept': 'application/json' },
    body: body != null ? JSON.stringify(body) : undefined,
  });
  if (!resp.ok) {
    // Surface 409/422 response bodies for optimistic-concurrency / validation errors
    let detail = resp.statusText;
    try {
      const payload = await resp.json();
      detail = payload.detail || payload.message || detail;
    } catch (_) {}
    const err = new Error(detail);
    err.status = resp.status;
    throw err;
  }
  return resp.json();
}

// ── Expose globally ────────────────────────────────────────────────────────

window.V2 = {
  apiGet, apiPostFile, apiSend, pollJob,
  skillClass, makeTag, makeConfidenceBar,
  priorityLabel, confidencePct,
  esc,
};
