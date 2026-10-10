/* app/static/js/review.js
   Review page logic — wrapped in an IIFE that assigns window.RV.

   Load order (from review.html):
     1. inline: window.JOB_ID = '{{ job_id }}';
     2. app.js        — V2 helpers (V2.esc, V2.apiSend, etc.)
     3. review.js     — this file (assigns window.RV)
     4. review-edit.js — edit/mutate actions (uses window.RV)
     5. inline: RV.boot();
*/

'use strict';

(function () {
  // ── Read JOB_ID injected by the server ──────────────────────────────────
  const JOB_ID = window.JOB_ID;

  // ── Use V2.esc for HTML escaping ─────────────────────────────────────────
  // Fallback inline implementation guards against a stale cached app.js that
  // pre-dates the V2.esc export (produces "esc is not a function" errors).
  const esc = (window.V2 && typeof window.V2.esc === 'function')
    ? window.V2.esc
    : function _esc(str) {
        return String(str || '')
          .replace(/&/g, '&amp;').replace(/</g, '&lt;')
          .replace(/>/g, '&gt;').replace(/"/g, '&quot;');
      };

  // ── Shared state ──────────────────────────────────────────────────────────
  const state = {
    result: null,
    activeTab: null,
    revision: 0,
    showRemoved: false,
    selection: new Set(),
    editState: null,
  };

  // ── AMS section map — single source of truth (r3-2) ─────────────────────
  // Maps artifact_type values to their section name, primary type, and the
  // full set of allowed types for that section.
  const AMS_SECTION_MAP = {
    // Artifact 1 — Executive Summary / Apps
    overview_app: {
      section: 'apps',
      primaryType: 'overview_app',
      allowedTypes: ['overview_app'],
    },
    // Artifact 2 — Requirement Register
    req_classification: {
      section: 'requirements',
      primaryType: 'req_classification',
      allowedTypes: ['req_classification', 'ticket_volume', 'tool', 'governance', 'deliverable', 'resource'],
    },
    ticket_volume: {
      section: 'requirements',
      primaryType: 'req_classification',
      allowedTypes: ['req_classification', 'ticket_volume', 'tool', 'governance', 'deliverable', 'resource'],
    },
    tool: {
      section: 'requirements',
      primaryType: 'req_classification',
      allowedTypes: ['req_classification', 'ticket_volume', 'tool', 'governance', 'deliverable', 'resource'],
    },
    governance: {
      section: 'requirements',
      primaryType: 'req_classification',
      allowedTypes: ['req_classification', 'ticket_volume', 'tool', 'governance', 'deliverable', 'resource'],
    },
    deliverable: {
      section: 'requirements',
      primaryType: 'req_classification',
      allowedTypes: ['req_classification', 'ticket_volume', 'tool', 'governance', 'deliverable', 'resource'],
    },
    resource: {
      section: 'requirements',
      primaryType: 'req_classification',
      allowedTypes: ['req_classification', 'ticket_volume', 'tool', 'governance', 'deliverable', 'resource'],
    },
    // Artifact 3 — SLA Matrix
    sla_target: {
      section: 'sla',
      primaryType: 'sla_target',
      allowedTypes: ['sla_target', 'availability', 'rto_rpo', 'sr_change_sla'],
    },
    availability: {
      section: 'sla',
      primaryType: 'sla_target',
      allowedTypes: ['sla_target', 'availability', 'rto_rpo', 'sr_change_sla'],
    },
    rto_rpo: {
      section: 'sla',
      primaryType: 'sla_target',
      allowedTypes: ['sla_target', 'availability', 'rto_rpo', 'sr_change_sla'],
    },
    sr_change_sla: {
      section: 'sla',
      primaryType: 'sla_target',
      allowedTypes: ['sla_target', 'availability', 'rto_rpo', 'sr_change_sla'],
    },
    // Artifact 4 — Transition
    transition_phase: {
      section: 'transition',
      primaryType: 'transition_phase',
      allowedTypes: ['transition_phase', 'kt_requirement', 'exit_obligation'],
    },
    kt_requirement: {
      section: 'transition',
      primaryType: 'transition_phase',
      allowedTypes: ['transition_phase', 'kt_requirement', 'exit_obligation'],
    },
    exit_obligation: {
      section: 'transition',
      primaryType: 'transition_phase',
      allowedTypes: ['transition_phase', 'kt_requirement', 'exit_obligation'],
    },
    // Artifact 5 — Penalties
    penalty: {
      section: 'penalties',
      primaryType: 'penalty',
      allowedTypes: ['penalty', 'liquidated_damages'],
    },
    liquidated_damages: {
      section: 'penalties',
      primaryType: 'penalty',
      allowedTypes: ['penalty', 'liquidated_damages'],
    },
    // Artifact 6 — Clarifications
    clarification_question: {
      section: 'clarifications',
      primaryType: 'clarification_question',
      allowedTypes: ['clarification_question', 'assumption_dependency'],
    },
    assumption_dependency: {
      section: 'clarifications',
      primaryType: 'clarification_question',
      allowedTypes: ['clarification_question', 'assumption_dependency'],
    },
  };

  // ── Skill label map — populated from /skills endpoint at boot ───────────
  // Keyed by slug, value is display_name from the registry.
  // No hardcoded labels: adding a skill folder auto-populates this map.
  let SKILL_LABELS = {};

  const SPINE_TABS = [
    { id: 'compliance',           label: 'Compliance',          icon: '⚖' },
    { id: 'risk',                 label: 'Risk',                icon: '⚠' },
    { id: 'ambiguity',            label: 'Ambiguity',           icon: '?' },
    { id: 'commercial_structure', label: 'Commercial Structure', icon: '₹' },
  ];

  // ── Fetch skill registry labels ──────────────────────────────────────────
  // Fetches /skills on page load and builds the slug→display_name map.
  // Falls back to the raw slug if the endpoint is unreachable.
  async function loadSkillLabels() {
    try {
      const data = await V2.apiGet('/skills');
      (data.skills || []).forEach(s => {
        SKILL_LABELS[s.slug] = s.display_name || s.slug;
      });
    } catch (_) {
      // Non-fatal — raw slugs will be used as fallback labels
    }
  }

  // ── Fetch and render ─────────────────────────────────────────────────────

  async function load() {
    // Load registry labels first so tabs have correct names on first render
    await loadSkillLabels();
    try {
      const data = await V2.apiGet(`/jobs/${JOB_ID}`);
      if (data.status === 'complete' && data.result) {
        state.editState = data.edit_state || null;
        if (data.edit_state && data.edit_state.revision != null) {
          state.revision = data.edit_state.revision;
        }
        render(data.result, data.file_name);
      } else if (data.status === 'failed') {
        showError(data.error || 'Analysis failed');
      } else {
        // Still running — poll
        V2.pollJob(JOB_ID,
          () => {},
          (d) => {
            state.editState = d.edit_state || null;
            if (d.edit_state && d.edit_state.revision != null) {
              state.revision = d.edit_state.revision;
            }
            render(d.result, d.file_name);
          },
          (err) => showError(err),
        );
      }
    } catch (e) {
      showError(e.message);
    }
  }

  function showError(msg) {
    document.getElementById('loading-state').style.display = 'none';
    document.getElementById('error-state').style.display = '';
    document.getElementById('error-state').textContent = `Error: ${msg}`;
  }

  function render(result, fileName) {
    state.result = result;
    document.getElementById('loading-state').style.display = 'none';
    document.getElementById('results-state').style.display = '';

    // fired_skills lives on the result directly; fall back to metadata for
    // older job records that stored it only there.
    if (!result.fired_skills || !result.fired_skills.length) {
      result.fired_skills = (result.metadata && result.metadata.fired_skills) || [];
    }

    document.getElementById('page-title').textContent = fileName || 'RFP Analysis';
    document.getElementById('page-meta').textContent =
      `Job ${JOB_ID} · ${result.fired_skills.length ? result.fired_skills.join(', ') : '—'}`;

    renderStats(result);
    renderTabBar(result);
    renderRail(result.fired_skills || []);

    // Show first tab — wrap in try/catch so a rendering error in the first
    // tab doesn't replace the whole page with #error-state.
    const firstSkill = (result.fired_skills || [])[0];
    const firstTab = firstSkill || 'compliance';
    try {
      window.switchTab(firstTab);
    } catch (err) {
      if (typeof console !== 'undefined') {
        console.error('[render] switchTab error for tab "' + firstTab + '"', err);
      }
      // Show a non-fatal inline error inside the tab panel rather than
      // replacing the whole page.
      const panels = document.getElementById('tab-panels');
      if (panels) {
        panels.innerHTML = '<div style="padding:32px;color:#b91c1c;font-size:13px;">Failed to render tab \u201c' +
          firstTab + '\u201d: ' + (err && err.message ? err.message : String(err)) + '</div>';
      }
    }
  }

  function renderStats(result) {
    const bar = document.getElementById('stats-bar');
    if (!bar) return;
    const skillTotal = (result.skill_outputs || []).reduce((s, so) => s + activeItems(so.items || []).length, 0);
    const spine = result.spine || {};

    let editSummaryHtml = '';
    const es = state.editState;
    if (es) {
      const edited = es.edited || 0;
      const added = es.user_added != null ? es.user_added : (es.added || 0);
      const removed = es.removed || 0;
      editSummaryHtml = `<div class="stats-bar__edits" id="stats-bar-edits" style="width:100%;font-size:12px;color:#6B7C75;margin-top:6px;padding-top:6px;border-top:1px solid var(--border);">${edited} edited · ${added} added · ${removed} removed</div>`;
    }

    bar.innerHTML = `
      <div class="stat"><div class="stat__value">${skillTotal}</div><div class="stat__label">Skill Items</div></div>
      <div class="stat"><div class="stat__value">${activeItems(spine.compliance||[]).length}</div><div class="stat__label">Compliance</div></div>
      <div class="stat"><div class="stat__value">${activeItems(spine.risk||[]).length}</div><div class="stat__label">Risks</div></div>
      <div class="stat"><div class="stat__value">${activeItems(spine.ambiguity||[]).length}</div><div class="stat__label">Ambiguities</div></div>
      <div class="stat"><div class="stat__value">${activeItems(spine.commercial_structure||[]).length}</div><div class="stat__label">Commercial</div></div>
      ${editSummaryHtml}
    `;
  }

  function renderTabBar(result) {
    const bar = document.getElementById('tab-bar');
    bar.innerHTML = '';

    // Skill tabs (dynamic from fired_skills)
    (result.fired_skills || []).forEach(slug => {
      const label = SKILL_LABELS[slug] || slug;
      const cls   = V2.skillClass(slug);
      bar.insertAdjacentHTML('beforeend',
        `<button class="tab tab--${cls}" data-tab="${slug}" onclick="switchTab('${slug}')">${label}</button>`
      );
    });

    // Spine tabs (always shown)
    SPINE_TABS.forEach(t => {
      bar.insertAdjacentHTML('beforeend',
        `<button class="tab tab--spine" data-tab="${t.id}" onclick="switchTab('${t.id}')">${t.icon} ${t.label}</button>`
      );
    });
  }

  function renderRail(firedSlugs) {
    const section = document.getElementById('rail-skills-section');
    const list    = document.getElementById('rail-skills-list');
    if (!firedSlugs.length) { section.style.display = 'none'; return; }
    section.style.display = '';
    list.innerHTML = '';
    firedSlugs.forEach(slug => {
      const label = SKILL_LABELS[slug] || slug;
      const cls   = V2.skillClass(slug);
      const el = document.createElement('a');
      el.className = 'rail-item';
      el.href = '#';
      el.dataset.tab = slug;
      el.onclick = (e) => { e.preventDefault(); window.switchTab(slug); };
      el.innerHTML = `
        <span class="rail-item__dot" style="background:var(--color-${cls})"></span>
        ${label}
      `;
      list.appendChild(el);
    });
  }

  // ── switchTab ────────────────────────────────────────────────────────────

  window.switchTab = function(tabId) {
    state.activeTab = tabId;

    // Highlight active tab
    document.querySelectorAll('.tab-bar .tab').forEach(t => {
      t.classList.toggle('tab--active', t.dataset.tab === tabId);
    });
    document.querySelectorAll('.rail-item[data-tab]').forEach(el => {
      el.classList.toggle('rail-item--active', el.dataset.tab === tabId);
    });

    renderTabPanel(tabId);
  };

  function renderTabPanel(tabId) {
    const panels = document.getElementById('tab-panels');
    panels.innerHTML = '';

    if (!state.result) return;

    const spineIds = SPINE_TABS.map(t => t.id);
    if (spineIds.includes(tabId)) {
      renderSpinePanel(tabId, panels);
    } else {
      renderSkillPanel(tabId, panels);
    }
  }

  // ── Skill panel ──────────────────────────────────────────────────────────

  function _renderTabFooter(removedCount, container) {
    if (removedCount === 0 && !state.showRemoved) return;
    const footer = document.createElement('div');
    footer.className = 'tab-panel-footer';
    footer.style.cssText = 'margin-top:24px;padding-top:12px;border-top:1px solid var(--border);display:flex;align-items:center;justify-content:space-between;font-size:13px;color:#6b7280;';
    const label = state.showRemoved ? `Hide removed (${removedCount})` : `Show removed (${removedCount})`;
    footer.innerHTML = `<button type="button" class="btn btn--link toggle-removed-btn" data-action="toggle-removed">${esc(label)}</button>`;
    container.appendChild(footer);
  }

  function renderSkillPanel(slug, container) {
    // Add button rendered BEFORE any early returns (C9/F15)
    if (slug !== 'ams') {
      const addBtn = document.createElement('button');
      addBtn.className = 'add-item-btn';
      addBtn.dataset.action = 'add-item';
      addBtn.dataset.bucket = slug;
      addBtn.textContent = '+ Add requirement';
      container.appendChild(addBtn);
    }

    const skillOutput = (state.result.skill_outputs || []).find(s => s.skill_slug === slug);
    if (!skillOutput) {
      container.insertAdjacentHTML('beforeend', '<div class="empty">No data for this skill.</div>');
      _renderTabFooter(0, container);
      return;
    }

    const allItems = skillOutput.items || [];
    const removedCount = allItems.filter(i => i._meta && i._meta.status === 'removed').length;
    const visibleItems = activeItems(allItems);
    const hasUserAdded = visibleItems.some(i => i._meta && i._meta.origin === 'user');

    // Rate card: no_roles_stated notice
    if (skillOutput.no_roles_stated) {
      if (hasUserAdded) {
        container.insertAdjacentHTML('beforeend',
          '<div class="notice">This RFP does not specify roles. The roles below were added manually.</div>'
        );
      } else {
        container.insertAdjacentHTML('beforeend',
          '<div class="notice">This RFP does not specify named roles or a rate card. ' +
          'The vendor is expected to propose their own team structure and rates.</div>'
        );
      }
    }

    if (!visibleItems.length) {
      container.insertAdjacentHTML('beforeend', '<div class="empty">No items extracted for this skill.</div>');
      _renderTabFooter(removedCount, container);
      return;
    }

    if (slug === 'ams') {
      renderAMSArtifacts(visibleItems, container);
      _renderTabFooter(removedCount, container);
      return;
    }

    const ul = document.createElement('ul');
    ul.className = 'req-list';
    visibleItems.forEach(item => ul.appendChild(makeSkillRow(item, slug)));
    container.appendChild(ul);

    _renderTabFooter(removedCount, container);
  }

  // ── AMS section renderers (r2-2, r3-2) ──────────────────────────────────
  // Each renderer handles one artifact card. They are also callable individually
  // by rerenderSection() in review-edit.js.

  // Helper: build the action cluster for a row (li or tr).
  // Returns an HTML string. No event logic — that lives in review-edit.js (1.3b).
  function _makeActionCluster(item) {
    const uid     = item._meta && item._meta.uid ? item._meta.uid : '';
    const removed = item._meta && item._meta.status === 'removed';
    if (!uid) return '';
    const editBtn    = `<button class="row-action-btn" data-action="edit-item" data-uid="${esc(uid)}" type="button">Edit</button>`;
    const enhanceBtn = `<button class="row-action-btn row-action-btn--enhance" data-action="enhance-item" data-uid="${esc(uid)}" type="button">Enhance</button>`;
    const removeBtn  = `<button class="row-action-btn row-action-btn--remove" data-action="remove-item" data-uid="${esc(uid)}" type="button">Remove</button>`;
    const restoreBtn = `<button class="row-action-btn row-action-btn--restore" data-action="restore-item" data-uid="${esc(uid)}" type="button">Restore</button>`;
    const revertBtn  = `<button class="row-action-btn" data-action="revert-item" data-uid="${esc(uid)}" type="button">Revert</button>`;
    const hasEdits   = item._meta && item._meta.edited;
    if (removed) {
      return `<span class="row-actions">${restoreBtn}</span>`;
    }
    return `<span class="row-actions">${editBtn}${enhanceBtn}${hasEdits ? revertBtn : ''}${removeBtn}</span>`;
  }

  // Helper: build status badges for a row.
  function _makeStatusBadges(item) {
    if (!item._meta) return '';
    const parts = [];
    if (item._meta.origin === 'user') {
      parts.push('<span class="row-badge row-badge--user-added">User-added</span>');
    }
    if (item._meta.edited) {
      const fields = Array.isArray(item._meta.edited_fields) && item._meta.edited_fields.length
        ? item._meta.edited_fields.join(', ')
        : '';
      const tip = fields ? ` title="Edited fields: ${esc(fields)}"` : '';
      parts.push(`<span class="row-badge row-badge--edited"${tip}>Edited</span>`);
    }
    if (item._meta.enhanced) {
      parts.push('<span class="row-badge row-badge--enhanced">AI-enhanced</span>');
    }
    return parts.join('');
  }

  // Helper: make add button for an AMS section using AMS_SECTION_MAP.
  function _makeAMSAddButton(sectionName) {
    // Find the primaryType for this section
    const entry = Object.values(AMS_SECTION_MAP).find(e => e.section === sectionName);
    if (!entry) return '';
    const primaryType = esc(entry.primaryType);
    const allowed     = esc(JSON.stringify(entry.allowedTypes));
    return `<button class="add-item-btn" data-action="add-item" data-bucket="ams" ` +
      `data-primary-type="${primaryType}" data-allowed-types="${allowed}">+ Add item</button>`;
  }

  // Artifact 1 — Executive Summary + In-Scope Apps table
  function renderAMSApps(allItems, container) {
    // KPI counts come from all section renderers; this renderer builds the whole
    // Artifact-1 card (header + KPI grid + apps table).
    const apps          = activeItems(allItems.filter(i => i.artifact_type === 'overview_app'));
    const reqs          = activeItems(allItems.filter(i => ['req_classification','ticket_volume','tool','governance','deliverable','resource'].includes(i.artifact_type)));
    const slas          = activeItems(allItems.filter(i => ['sla_target','availability','rto_rpo','sr_change_sla'].includes(i.artifact_type)));
    const trans         = activeItems(allItems.filter(i => ['transition_phase','kt_requirement','exit_obligation'].includes(i.artifact_type)));
    const penalties     = activeItems(allItems.filter(i => ['penalty','liquidated_damages'].includes(i.artifact_type)));
    const clarifications= activeItems(allItems.filter(i => ['clarification_question','assumption_dependency'].includes(i.artifact_type)));

    const card = document.createElement('div');
    card.className = 'card ams-section ams-section--apps';
    card.dataset.amsSectionName = 'apps';
    card.style.cssText = 'padding:20px;border-left:4px solid var(--color-ams);background:var(--surface);border-radius:6px;border:1px solid var(--border);';

    // Build apps table rows using DOM (AT-20: no raw innerHTML with item fields)
    const appRowsHtml = apps.map(a => {
      const uid     = a._meta && a._meta.uid ? ` data-uid="${esc(a._meta.uid)}"` : '';
      const removed = a._meta && a._meta.status === 'removed';
      const rowStyle = removed ? 'border-bottom:1px solid #f0f2f5;opacity:0.55;' : 'border-bottom:1px solid #f0f2f5;';
      return `<tr style="${rowStyle}"${uid}>` +
        `<td style="padding:6px 8px;font-family:var(--font-mono);font-size:11px;">${esc(a.id)}</td>` +
        `<td style="padding:6px 8px;font-weight:600;">${esc(a.title)}${_makeStatusBadges(a)}</td>` +
        `<td style="padding:6px 8px;">${esc(a.category || a.description || '—')}</td>` +
        `<td style="padding:6px 8px;"><span class="badge ${a.impact === 'high' ? 'badge--critical' : ''}">${esc(a.severity_level || a.impact || 'Standard')}</span></td>` +
        `<td style="padding:6px 8px;">${esc(a.target_value || '—')}</td>` +
        `<td style="padding:6px 8px;font-size:11px;color:#6b7280;">${esc(a.source_section)}</td>` +
        `<td style="padding:6px 8px;" class="ams-row-actions">${_makeActionCluster(a)}</td>` +
        `</tr>`;
    }).join('');

    const appsTable = apps.length ? `
      <div style="margin-top:12px;">
        <h4 style="font-size:13px;text-transform:uppercase;color:#4b5563;margin-bottom:8px;">In-Scope Applications &amp; Environments</h4>
        <table style="width:100%;font-size:12px;border-collapse:collapse;">
          <thead>
            <tr style="text-align:left;border-bottom:1px solid var(--border);color:#6b7280;">
              <th style="padding:6px 8px;">ID</th>
              <th style="padding:6px 8px;">Application</th>
              <th style="padding:6px 8px;">Category / Function</th>
              <th style="padding:6px 8px;">Criticality</th>
              <th style="padding:6px 8px;">Target / Spec</th>
              <th style="padding:6px 8px;">Section</th>
              <th style="padding:6px 8px;"></th>
            </tr>
          </thead>
          <tbody>${appRowsHtml}</tbody>
        </table>
      </div>
    ` : '';

    card.innerHTML = `
      <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:16px;">
        <h3 style="font-size:16px;font-weight:600;margin:0;">Artifact 1 · AMS Executive Summary</h3>
        <span class="tag tag--ams" style="font-weight:600;">Managed Services Overview</span>
      </div>
      <div style="display:grid;grid-template-columns:repeat(auto-fit, minmax(130px, 1fr));gap:12px;margin-bottom:16px;" id="ams-kpi-grid">
        <div style="background:var(--paper);padding:10px 14px;border-radius:4px;border:1px solid var(--border);">
          <div style="font-size:11px;color:#6b7280;text-transform:uppercase;">Apps in Scope</div>
          <div style="font-size:18px;font-weight:700;" id="ams-kpi-apps">${apps.length || '—'}</div>
        </div>
        <div style="background:var(--paper);padding:10px 14px;border-radius:4px;border:1px solid var(--border);">
          <div style="font-size:11px;color:#6b7280;text-transform:uppercase;">L1/L2/L3 Scope</div>
          <div style="font-size:18px;font-weight:700;" id="ams-kpi-reqs">${reqs.length || '—'}</div>
        </div>
        <div style="background:var(--paper);padding:10px 14px;border-radius:4px;border:1px solid var(--border);">
          <div style="font-size:11px;color:#6b7280;text-transform:uppercase;">SLAs &amp; DR</div>
          <div style="font-size:18px;font-weight:700;" id="ams-kpi-slas">${slas.length || '—'}</div>
        </div>
        <div style="background:var(--paper);padding:10px 14px;border-radius:4px;border:1px solid var(--border);">
          <div style="font-size:11px;color:#6b7280;text-transform:uppercase;">Transition Items</div>
          <div style="font-size:18px;font-weight:700;" id="ams-kpi-trans">${trans.length || '—'}</div>
        </div>
        <div style="background:var(--paper);padding:10px 14px;border-radius:4px;border:1px solid var(--border);">
          <div style="font-size:11px;color:#6b7280;text-transform:uppercase;">Penalties / LD</div>
          <div style="font-size:18px;font-weight:700;color:#b91c1c;" id="ams-kpi-penalties">${penalties.length || '0'}</div>
        </div>
        <div style="background:var(--paper);padding:10px 14px;border-radius:4px;border:1px solid var(--border);">
          <div style="font-size:11px;color:#6b7280;text-transform:uppercase;">Clarifications</div>
          <div style="font-size:18px;font-weight:700;color:#d97706;" id="ams-kpi-clarifications">${clarifications.length || '0'}</div>
        </div>
      </div>
      ${_makeAMSAddButton('apps')}
      ${appsTable}
    `;
    container.appendChild(card);
  }

  // Artifact 2 — Requirement Register
  function renderAMSRequirements(allItems, container) {
    const reqTypes = ['req_classification','ticket_volume','tool','governance','deliverable','resource'];
    const items = activeItems(allItems.filter(i => reqTypes.includes(i.artifact_type)));

    const card = document.createElement('div');
    card.className = 'card ams-section ams-section--requirements';
    card.dataset.amsSectionName = 'requirements';
    card.style.cssText = 'padding:20px;background:var(--surface);border-radius:6px;border:1px solid var(--border);';

    const heading = document.createElement('h3');
    heading.style.cssText = 'font-size:15px;font-weight:600;margin-bottom:12px;';
    heading.textContent = 'Artifact 2 · AMS Requirement Register (L1 / L2 / L3 & Operations)';
    card.appendChild(heading);

    const addBtn = document.createElement('button');
    addBtn.className = 'add-item-btn';
    addBtn.dataset.action = 'add-item';
    addBtn.dataset.bucket = 'ams';
    addBtn.dataset.primaryType = 'req_classification';
    addBtn.dataset.allowedTypes = JSON.stringify(AMS_SECTION_MAP.req_classification.allowedTypes);
    addBtn.textContent = '+ Add requirement';
    card.appendChild(addBtn);

    if (items.length) {
      const ul = document.createElement('ul');
      ul.className = 'req-list';
      items.forEach(i => ul.appendChild(makeSkillRow(i, 'ams')));
      card.appendChild(ul);
    } else {
      const empty = document.createElement('div');
      empty.className = 'empty';
      empty.textContent = 'No specific L1-L3 requirements isolated.';
      card.appendChild(empty);
    }
    container.appendChild(card);
  }

  // Artifact 3 — SLA Matrix
  function renderAMSSLAMatrix(allItems, container) {
    const slaTypes = ['sla_target','availability','rto_rpo','sr_change_sla'];
    const slas = activeItems(allItems.filter(i => slaTypes.includes(i.artifact_type)));

    const card = document.createElement('div');
    card.className = 'card ams-section ams-section--sla';
    card.dataset.amsSectionName = 'sla';
    card.style.cssText = 'padding:20px;background:var(--surface);border-radius:6px;border:1px solid var(--border);';

    const heading = document.createElement('h3');
    heading.style.cssText = 'font-size:15px;font-weight:600;margin-bottom:12px;';
    heading.textContent = 'Artifact 3 · SLA Matrix, Availability & Disaster Recovery';
    card.appendChild(heading);

    const addBtn = document.createElement('button');
    addBtn.className = 'add-item-btn';
    addBtn.dataset.action = 'add-item';
    addBtn.dataset.bucket = 'ams';
    addBtn.dataset.primaryType = 'sla_target';
    addBtn.dataset.allowedTypes = JSON.stringify(AMS_SECTION_MAP.sla_target.allowedTypes);
    addBtn.textContent = '+ Add SLA';
    card.appendChild(addBtn);

    if (slas.length) {
      const slaRowsHtml = slas.map(s => {
        const uid     = s._meta && s._meta.uid ? ` data-uid="${esc(s._meta.uid)}"` : '';
        const removed = s._meta && s._meta.status === 'removed';
        const rowStyle = removed ? 'border-bottom:1px solid #f0f2f5;opacity:0.55;' : 'border-bottom:1px solid #f0f2f5;';
        return `<tr style="${rowStyle}"${uid}>` +
          `<td style="padding:6px 8px;font-family:var(--font-mono);font-size:11px;">${esc(s.id)}</td>` +
          `<td style="padding:6px 8px;"><span class="badge ${s.severity_level && s.severity_level.includes('1') ? 'badge--critical' : ''}">${esc(s.severity_level || s.priority || '—')}</span></td>` +
          `<td style="padding:6px 8px;font-weight:600;">${esc(s.title)}${_makeStatusBadges(s)}</td>` +
          `<td style="padding:6px 8px;">${esc(s.sla_type || s.artifact_type)}</td>` +
          `<td style="padding:6px 8px;font-weight:600;color:var(--color-ams);">${esc(s.target_value || '—')}</td>` +
          `<td style="padding:6px 8px;color:#b91c1c;">${esc(s.penalty_value || s.penalty_type || '—')}</td>` +
          `<td style="padding:6px 8px;font-size:11px;color:#6b7280;">${esc(s.source_section)}</td>` +
          `<td style="padding:6px 8px;" class="ams-row-actions">${_makeActionCluster(s)}</td>` +
          `</tr>`;
      }).join('');
      card.insertAdjacentHTML('beforeend', `
        <table style="width:100%;font-size:12px;border-collapse:collapse;margin-top:8px;">
          <thead>
            <tr style="text-align:left;border-bottom:1px solid var(--border);color:#6b7280;">
              <th style="padding:6px 8px;">ID</th>
              <th style="padding:6px 8px;">Priority / Level</th>
              <th style="padding:6px 8px;">SLA Title &amp; Commitment</th>
              <th style="padding:6px 8px;">Type</th>
              <th style="padding:6px 8px;">Target Value</th>
              <th style="padding:6px 8px;">Penalty / LD</th>
              <th style="padding:6px 8px;">Section</th>
              <th style="padding:6px 8px;"></th>
            </tr>
          </thead>
          <tbody>${slaRowsHtml}</tbody>
        </table>
      `);
    } else {
      card.insertAdjacentHTML('beforeend', '<div class="empty">No specific SLA targets extracted.</div>');
    }
    container.appendChild(card);
  }

  // Artifact 4 — Transition & Exit
  function renderAMSTransition(allItems, container) {
    const transTypes = ['transition_phase','kt_requirement','exit_obligation'];
    const items = activeItems(allItems.filter(i => transTypes.includes(i.artifact_type)));

    const card = document.createElement('div');
    card.className = 'card ams-section ams-section--transition';
    card.dataset.amsSectionName = 'transition';
    card.style.cssText = 'padding:20px;background:var(--surface);border-radius:6px;border:1px solid var(--border);';

    const heading = document.createElement('h3');
    heading.style.cssText = 'font-size:15px;font-weight:600;margin-bottom:12px;';
    heading.textContent = 'Artifact 4 · Transition Assessment & Knowledge Transfer';
    card.appendChild(heading);

    const addBtn = document.createElement('button');
    addBtn.className = 'add-item-btn';
    addBtn.dataset.action = 'add-item';
    addBtn.dataset.bucket = 'ams';
    addBtn.dataset.primaryType = 'transition_phase';
    addBtn.dataset.allowedTypes = JSON.stringify(AMS_SECTION_MAP.transition_phase.allowedTypes);
    addBtn.textContent = '+ Add transition item';
    card.appendChild(addBtn);

    if (items.length) {
      const ul = document.createElement('ul');
      ul.className = 'req-list';
      items.forEach(i => ul.appendChild(makeSkillRow(i, 'ams')));
      card.appendChild(ul);
    } else {
      const empty = document.createElement('div');
      empty.className = 'empty';
      empty.textContent = 'No transition or KT requirements extracted.';
      card.appendChild(empty);
    }
    container.appendChild(card);
  }

  // Artifact 5 — Penalties
  function renderAMSPenalties(allItems, container) {
    const items = activeItems(allItems.filter(i => ['penalty','liquidated_damages'].includes(i.artifact_type)));

    const card = document.createElement('div');
    card.className = 'card ams-section ams-section--penalties';
    card.dataset.amsSectionName = 'penalties';
    card.style.cssText = 'padding:20px;background:var(--surface);border-radius:6px;border:1px solid var(--border);';

    const heading = document.createElement('h3');
    heading.style.cssText = 'font-size:15px;font-weight:600;margin-bottom:12px;color:#b91c1c;';
    heading.textContent = 'Artifact 5 · Penalty & Commercial Risk Register';
    card.appendChild(heading);

    const addBtn = document.createElement('button');
    addBtn.className = 'add-item-btn';
    addBtn.dataset.action = 'add-item';
    addBtn.dataset.bucket = 'ams';
    addBtn.dataset.primaryType = 'penalty';
    addBtn.dataset.allowedTypes = JSON.stringify(AMS_SECTION_MAP.penalty.allowedTypes);
    addBtn.textContent = '+ Add penalty';
    card.appendChild(addBtn);

    if (items.length) {
      const ul = document.createElement('ul');
      ul.className = 'req-list';
      items.forEach(i => ul.appendChild(makeSkillRow(i, 'ams')));
      card.appendChild(ul);
    } else {
      const empty = document.createElement('div');
      empty.className = 'empty';
      empty.textContent = 'No specific SLA penalties or liquidated damages clauses found.';
      card.appendChild(empty);
    }
    container.appendChild(card);
  }

  // Artifact 6 — Clarifications
  function renderAMSClarifications(allItems, container) {
    const items = activeItems(allItems.filter(i => ['clarification_question','assumption_dependency'].includes(i.artifact_type)));

    const card = document.createElement('div');
    card.className = 'card ams-section ams-section--clarifications';
    card.dataset.amsSectionName = 'clarifications';
    card.style.cssText = 'padding:20px;background:var(--surface);border-radius:6px;border:1px solid var(--border);';

    const heading = document.createElement('h3');
    heading.style.cssText = 'font-size:15px;font-weight:600;margin-bottom:12px;color:#d97706;';
    heading.textContent = 'Artifact 6 · Clarification Register & Missing Information';
    card.appendChild(heading);

    const addBtn = document.createElement('button');
    addBtn.className = 'add-item-btn';
    addBtn.dataset.action = 'add-item';
    addBtn.dataset.bucket = 'ams';
    addBtn.dataset.primaryType = 'clarification_question';
    addBtn.dataset.allowedTypes = JSON.stringify(AMS_SECTION_MAP.clarification_question.allowedTypes);
    addBtn.textContent = '+ Add clarification';
    card.appendChild(addBtn);

    if (items.length) {
      const ul = document.createElement('ul');
      ul.className = 'req-list';
      items.forEach(i => ul.appendChild(makeSkillRow(i, 'ams')));
      card.appendChild(ul);
    } else {
      const empty = document.createElement('div');
      empty.className = 'empty';
      empty.textContent = 'No missing information or clarification questions flagged.';
      card.appendChild(empty);
    }
    container.appendChild(card);
  }

  // Coordinator — replaces the old monolithic renderAMSArtifacts (r2-2)
  function renderAMSArtifacts(items, container) {
    // items has already been filtered through activeItems() by the caller
    const wrap = document.createElement('div');
    wrap.className = 'ams-container';
    wrap.style.cssText = 'display:flex;flex-direction:column;gap:24px;';

    const renderers = [
      renderAMSApps,
      renderAMSRequirements,
      renderAMSSLAMatrix,
      renderAMSTransition,
      renderAMSPenalties,
      renderAMSClarifications,
    ];
    for (const fn of renderers) {
      try {
        fn(items, wrap);
      } catch (err) {
        // Surface the error inside a placeholder card so the rest of the
        // sections still render.
        const errCard = document.createElement('div');
        errCard.className = 'card';
        errCard.style.cssText = 'padding:16px;border:1px solid #fca5a5;background:#fef2f2;border-radius:6px;color:#b91c1c;font-size:13px;';
        errCard.textContent = 'Section failed to render: ' + (err && err.message ? err.message : String(err));
        wrap.appendChild(errCard);
        if (typeof console !== 'undefined') {
          console.error('[renderAMSArtifacts] section error in ' + fn.name, err);
        }
      }
    }

    container.appendChild(wrap);
  }

  function makeSkillRow(item, slug) {
    const meta    = item._meta || {};
    const uid     = meta.uid || '';
    const removed = meta.status === 'removed';

    const li = document.createElement('li');
    li.className = 'req-row' + (removed ? ' req-row--removed' : '');
    if (uid) li.dataset.uid = uid;

    const cls = V2.skillClass(slug);
    const tagText = item.artifact_type || item.type || item.sla_type || item.item_type || slug;
    const tag = V2.makeTag(tagText.replace(/_/g, ' '), cls);

    // Bulk-selection checkbox (2.1)
    if (uid) {
      const cb = document.createElement('input');
      cb.type = 'checkbox';
      cb.className = 'req-row__checkbox';
      cb.dataset.action = 'select-item';
      cb.dataset.uid = uid;
      if (window.RV && window.RV.state.selection && window.RV.state.selection.has(uid)) {
        cb.checked = true;
        li.classList.add('req-row--selected');
      }
      li.appendChild(cb);
    }

    const body = document.createElement('div');
    body.className = 'req-row__body';

    const title       = item.title || item.role_title || '';
    const desc        = item.description || item.target_value || '';
    const priority    = item.priority || '';
    const impact      = item.impact || '';
    const section     = item.source_section || '';
    const conf        = item.confidence;
    const excerpt     = item.raw_excerpt || '';
    const srcType     = item.source_type || 'text';
    const srcDoc      = item.source_document || '';
    const docRole     = item.document_role || 'primary';
    const isStaffing  = item.type === 'staffing_mandate';
    const headcount   = item.min_headcount;
    const resp        = item.primary_responsibility || '';
    const supLevel    = item.support_level && item.support_level !== 'not_applicable' ? item.support_level : null;
    const itemId      = item.id || '';

    body.innerHTML = `
      <div class="req-row__title">
        ${itemId ? `<span style="font-family:var(--font-mono);font-size:11px;color:#6b7280;margin-right:6px;">${esc(itemId)}</span>` : ''}
        ${esc(title)}${_makeStatusBadges(item)}
        ${supLevel ? `<span class="badge" style="background:#e0f2fe;color:#0369a1;font-weight:600;margin-left:6px;">${esc(supLevel)}</span>` : ''}
        ${isStaffing && headcount != null ? `<span class="headcount-badge" title="Minimum headcount">${headcount} required</span>` : ''}
        ${_makeActionCluster(item)}
      </div>
      ${isStaffing && resp ? `<div class="req-row__desc">${esc(resp)}</div>` : (desc ? `<div class="req-row__desc">${esc(desc)}</div>` : '')}
      ${item.clarification_question ? `<div class="req-row__desc" style="font-style:italic;color:#d97706;">❓ ${esc(item.clarification_question)}</div>` : ''}
      <div class="req-row__meta">
        ${impact ? `<span class="badge ${impact === 'high' ? 'badge--critical' : ''}">${esc(impact.toUpperCase())} IMPACT</span>` : ''}
        ${priority ? `<span>${V2.priorityLabel(priority)}</span>` : ''}
        ${section  ? `<span>${esc(section)}</span>` : ''}
        ${srcDoc && docRole !== 'primary' ? `<span class="source-doc-tag source-doc-tag--${docRole}" title="From ${esc(srcDoc)}">📎 ${esc(srcDoc)}</span>` : ''}
        ${srcType === 'diagram' ? `<span>${V2.makeTag('diagram','diagram').outerHTML}</span>` : ''}
        ${conf != null ? V2.makeConfidenceBar(conf).outerHTML : ''}
      </div>
      ${excerpt ? `<div class="req-row__excerpt">"${esc(excerpt)}"</div>` : ''}
    `;

    li.appendChild(tag);
    li.appendChild(body);
    return li;
  }

  // ── Spine panel ──────────────────────────────────────────────────────────

  function renderSpinePanel(tabId, container) {
    // Add button rendered BEFORE any early returns (C9/F15)
    const addBtn = document.createElement('button');
    addBtn.className = 'add-item-btn';
    addBtn.dataset.action = 'add-item';
    addBtn.dataset.bucket = tabId;
    addBtn.textContent = '+ Add item';
    container.appendChild(addBtn);

    const spine = state.result.spine || {};
    const allItems = spine[tabId] || [];
    const removedCount = allItems.filter(i => i._meta && i._meta.status === 'removed').length;
    const items = activeItems(allItems);

    if (!items.length) {
      container.insertAdjacentHTML('beforeend', '<div class="empty">No items found.</div>');
      _renderTabFooter(removedCount, container);
      return;
    }

    const ul = document.createElement('ul');
    ul.className = 'req-list';
    items.forEach(item => ul.appendChild(makeSpineRow(item, tabId)));
    container.appendChild(ul);

    _renderTabFooter(removedCount, container);
  }

  function makeSpineRow(item, tabId) {
    const meta    = item._meta || {};
    const uid     = meta.uid || '';
    const removed = meta.status === 'removed';

    const li = document.createElement('li');
    li.className = 'req-row' + (removed ? ' req-row--removed' : '');
    if (uid) li.dataset.uid = uid;

    // Bulk-selection checkbox (2.1)
    if (uid) {
      const cb = document.createElement('input');
      cb.type = 'checkbox';
      cb.className = 'req-row__checkbox';
      cb.dataset.action = 'select-item';
      cb.dataset.uid = uid;
      if (window.RV && window.RV.state.selection && window.RV.state.selection.has(uid)) {
        cb.checked = true;
        li.classList.add('req-row--selected');
      }
      li.appendChild(cb);
    }

    const tagMap = {
      compliance:          'compliance',
      risk:                'risk',
      ambiguity:           'ambiguity',
      commercial_structure:'cs',
    };
    const tag = V2.makeTag(tabId.replace(/_/g, ' '), tagMap[tabId] || 'spine');

    const body = document.createElement('div');
    body.className = 'req-row__body';

    const title       = item.title || item.pricing_dimension || '';
    const desc        = item.description || '';
    const section     = item.source_section || '';
    const priority    = item.priority || '';
    const excerpt     = item.raw_excerpt || '';
    const srcType     = item.source_type || 'text';
    const conf        = item.confidence;
    const clarQ       = item.clarification_question || '';
    const srcDoc      = item.source_document || '';
    const docRole     = item.document_role || 'primary';
    const resolved    = item.resolved === true;
    const clarifiedBy = item.clarified_by || '';

    // For ambiguity items: show resolved/unresolved badge when it's the ambiguity tab
    const isAmbiguity = tabId === 'ambiguity';
    const resolvedBadge = isAmbiguity
      ? (resolved
          ? `<span class="qna-resolved-badge" title="Resolved by ${esc(clarifiedBy)}">✓ Resolved${clarifiedBy ? ' · ' + esc(clarifiedBy) : ''}</span>`
          : `<span class="qna-unresolved-badge">⚠ Needs clarification</span>`)
      : '';

    body.innerHTML = `
      <div class="req-row__title">${esc(title)}${_makeStatusBadges(item)}${_makeActionCluster(item)}</div>
      ${desc   ? `<div class="req-row__desc">${esc(desc)}</div>` : ''}
      ${clarQ  ? `<div class="req-row__desc" style="font-style:italic;color:var(--color-rate-card)">❓ ${esc(clarQ)}</div>` : ''}
      <div class="req-row__meta">
        ${resolvedBadge}
        ${priority ? `<span>${V2.priorityLabel(priority)}</span>` : ''}
        ${section  ? `<span>${esc(section)}</span>` : ''}
        ${srcDoc && docRole !== 'primary' ? `<span class="source-doc-tag source-doc-tag--${docRole}" title="From ${esc(srcDoc)}">📎 ${esc(srcDoc)}</span>` : ''}
        ${srcType === 'diagram' ? `<span>${V2.makeTag('diagram','diagram').outerHTML}</span>` : ''}
        ${conf != null ? V2.makeConfidenceBar(conf).outerHTML : ''}
      </div>
      ${excerpt ? `<div class="req-row__excerpt">"${esc(excerpt)}"</div>` : ''}
    `;

    li.appendChild(tag);
    li.appendChild(body);
    return li;
  }

  // ── findItemByUid — walks all item arrays ─────────────────────────────────

  function findItemByUid(uid) {
    if (!state.result) return null;

    // Walk skill_outputs
    for (const so of (state.result.skill_outputs || [])) {
      for (const item of (so.items || [])) {
        if (item._meta && item._meta.uid === uid) {
          return { item, bucket: so.skill_slug };
        }
      }
    }

    // Walk spine categories
    const spine = state.result.spine || {};
    const spineKeys = ['compliance', 'risk', 'ambiguity', 'commercial_structure'];
    for (const key of spineKeys) {
      for (const item of (spine[key] || [])) {
        if (item._meta && item._meta.uid === uid) {
          return { item, bucket: key };
        }
      }
    }

    return null;
  }

  // ── activeItems — filter removed items ───────────────────────────────────

  function activeItems(arr) {
    if (!Array.isArray(arr)) return [];
    if (state.showRemoved) return arr;
    return arr.filter(item => !(item._meta && item._meta.status === 'removed'));
  }

  // ── Boot — called by the inline script after all JS files have loaded ─────

  function boot() {
    load();
  }

  // ── Export surface ────────────────────────────────────────────────────────

  window.RV = {
    state,
    AMS_SECTION_MAP,
    boot,
    load,
    render,
    renderTabPanel,
    renderSkillPanel,
    renderSpinePanel,
    renderTabFooter: _renderTabFooter,
    renderAMSArtifacts,
    renderAMSApps,
    renderAMSRequirements,
    renderAMSSLAMatrix,
    renderAMSTransition,
    renderAMSPenalties,
    renderAMSClarifications,
    makeSkillRow,
    makeSpineRow,
    findItemByUid,
    activeItems,
    renderStats,
    makeActionCluster: _makeActionCluster,
    makeStatusBadges:  _makeStatusBadges,
  };

})();
