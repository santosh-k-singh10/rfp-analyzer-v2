/* app/static/js/review-edit.js
   Edit actions for the review page.
   Loaded after review.js — uses window.RV and window.V2.
   RV.boot() is called by the inline script AFTER this file loads.
*/

'use strict';

(function () {

  // ── Convenience aliases ────────────────────────────────────────────────────
  // Fallback guards against a stale cached app.js without V2.esc.
  const esc = (window.V2 && typeof window.V2.esc === 'function')
    ? window.V2.esc
    : function _esc(str) {
        return String(str || '')
          .replace(/&/g, '&amp;').replace(/</g, '&lt;')
          .replace(/>/g, '&gt;').replace(/"/g, '&quot;');
      };

  // ── Field-spec cache: keyed by "<kind>:<key>" ───────────────────────────────
  const _fieldSpecCache = {};

  // ── Save-indicator helpers ─────────────────────────────────────────────────

  function _setSaveIndicator(state, rev) {
    const el = document.getElementById('save-indicator');
    if (!el) return;
    el.style.display = '';
    el.className = 'save-indicator save-indicator--' + state;
    if (state === 'saved')  { el.textContent = 'Saved · rev ' + rev; }
    if (state === 'saving') { el.textContent = 'Saving\u2026'; }
    if (state === 'failed') { el.textContent = 'Save failed \u2014 retry'; }
  }

  // ── State helpers ──────────────────────────────────────────────────────────

  // Return which bucket an item belongs to (skill slug or spine key).
  // Works from RV.state.result only.
  function _bucketForUid(uid) {
    const found = RV.findItemByUid(uid);
    return found ? found.bucket : null;
  }

  // Return the current item object from RV.state by uid.
  function _itemFromState(uid) {
    const found = RV.findItemByUid(uid);
    return found ? found.item : null;
  }

  // Return section name for a bucket using AMS_SECTION_MAP for AMS items.
  // For AMS items the section comes from artifact_type; for skill/spine the
  // bucket IS the section.
  function _sectionForItem(item, bucket) {
    if (bucket === 'ams' && item && item.artifact_type) {
      const entry = RV.AMS_SECTION_MAP[item.artifact_type];
      return entry ? entry.section : null;
    }
    return bucket;
  }

  // ── STATE SYNC ─────────────────────────────────────────────────────────────

  /**
   * mutationSuccess(bucket, uid, updatedItem, editState)
   *
   * Called after EVERY successful mutation.
   * 1. Updates RV.state.result in-memory (replace/insert the item in its bucket).
   * 2. Updates RV.state.revision from editState.revision.
   * 3. Updates save indicator.
   * 4. Calls RV.renderStats.
   *
   * Does NOT touch the DOM beyond the save indicator and stats bar.
   * Callers are responsible for the post-state DOM update (replaceRow / rerenderSection).
   */
  function mutationSuccess(bucket, uid, updatedItem, editState) {
    const result = RV.state.result;
    if (!result) return;

    const spineKeys = ['compliance', 'risk', 'ambiguity', 'commercial_structure'];

    if (spineKeys.includes(bucket)) {
      // Spine — replace item in spine[bucket] array
      const arr = result.spine[bucket] || [];
      const idx = arr.findIndex(i => i._meta && i._meta.uid === uid);
      if (idx !== -1) {
        arr[idx] = updatedItem;
      } else {
        arr.push(updatedItem);
        result.spine[bucket] = arr;
      }
    } else {
      // Skill — find or create the SkillOutput entry
      let so = (result.skill_outputs || []).find(s => s.skill_slug === bucket);
      if (!so) {
        // First add to a previously-empty bucket — create SkillOutput (C9)
        if (!result.skill_outputs) result.skill_outputs = [];
        so = { skill_slug: bucket, items: [] };
        result.skill_outputs.push(so);
        // Also add to fired_skills so the tab renders
        if (!result.fired_skills) result.fired_skills = [];
        if (!result.fired_skills.includes(bucket)) {
          result.fired_skills.push(bucket);
        }
      }
      const arr = so.items || [];
      const idx = arr.findIndex(i => i._meta && i._meta.uid === uid);
      if (idx !== -1) {
        arr[idx] = updatedItem;
      } else {
        arr.push(updatedItem);
        so.items = arr;
      }
    }

    // Persist revision and editState into state
    if (editState) {
      RV.state.editState = editState;
      if (editState.revision != null) {
        RV.state.revision = editState.revision;
      }
    }

    _setSaveIndicator('saved', RV.state.revision);
    RV.renderStats(result);
  }

  // ── DOM UPDATE HELPERS ─────────────────────────────────────────────────────

  /**
   * replaceRow(uid, newHtml)
   *
   * Locates the element with [data-uid="<uid>"] inside #tab-panels and replaces
   * it in-place. Handles both <li> and <tr> rows. Scroll position is preserved
   * because only the single element changes.
   */
  function replaceRow(uid, newHtml) {
    const panels = document.getElementById('tab-panels');
    if (!panels) return;
    const el = panels.querySelector('[data-uid="' + CSS.escape(uid) + '"]');
    if (!el) return;

    const tag = el.tagName.toLowerCase();
    if (tag === 'tr') {
      // Build a temporary table to parse the HTML
      const tmp = document.createElement('table');
      tmp.innerHTML = '<tbody>' + newHtml + '</tbody>';
      const newTr = tmp.querySelector('tr');
      if (newTr) el.parentNode.replaceChild(newTr, el);
    } else {
      // li or other block element
      const tmp = document.createElement('ul');
      tmp.innerHTML = newHtml;
      const newEl = tmp.firstElementChild;
      if (newEl) el.parentNode.replaceChild(newEl, el);
    }
  }

  /**
   * rerenderSection(sections)
   *
   * Re-renders one or more named sections inside the currently visible tab panel.
   * - For AMS sections: replaces the `.ams-section--<name>` card in the existing
   *   ams-container and refreshes the KPI cards (Artifact 1 header).
   * - For skill/spine tabs: re-renders the full panel (the panel IS the section).
   *
   * Scroll position is preserved by only touching the relevant card element.
   *
   * Callers pass both old and new section names when artifact_type changes (r3-2).
   */
  function rerenderSection(sections) {
    const panels = document.getElementById('tab-panels');
    if (!panels || !RV.state.result) return;

    const amsSections = new Set(['apps', 'requirements', 'sla', 'transition', 'penalties', 'clarifications']);
    const asmRenderers = {
      apps:           RV.renderAMSApps,
      requirements:   RV.renderAMSRequirements,
      sla:            RV.renderAMSSLAMatrix,
      transition:     RV.renderAMSTransition,
      penalties:      RV.renderAMSPenalties,
      clarifications: RV.renderAMSClarifications,
    };

    const spineKeys = new Set(['compliance', 'risk', 'ambiguity', 'commercial_structure']);

    // Collect unique sections; separate AMS from non-AMS
    const unique = [...new Set(sections)];

    for (const sectionName of unique) {
      if (amsSections.has(sectionName)) {
        // AMS path — replace only the relevant card
        const amsOutput = (RV.state.result.skill_outputs || []).find(s => s.skill_slug === 'ams');
        const allItems  = amsOutput ? (amsOutput.items || []) : [];

        const existing = panels.querySelector('.ams-section--' + sectionName);
        if (!existing) continue;

        // Build replacement card in a scratch container
        const scratch = document.createElement('div');
        asmRenderers[sectionName](allItems, scratch);
        const newCard = scratch.firstElementChild;
        if (newCard) existing.parentNode.replaceChild(newCard, existing);

        // Refresh the KPI grid (Artifact 1 card — always present)
        _refreshAMSKpis(allItems, panels);

      } else if (spineKeys.has(sectionName)) {
        // Spine tab — re-render the whole panel
        if (RV.state.activeTab === sectionName) {
          panels.innerHTML = '';
          RV.renderSpinePanel(sectionName, panels);
        }

      } else {
        // Skill tab — re-render the whole panel
        if (RV.state.activeTab === sectionName) {
          panels.innerHTML = '';
          RV.renderSkillPanel(sectionName, panels);
        }
      }
    }

    // Refresh tab footer toggle if present or if newly needed
    const footerBtn = panels.querySelector('.toggle-removed-btn');
    if (RV.state.activeTab) {
      let count = 0;
      if (spineKeys.has(RV.state.activeTab)) {
        const spine = RV.state.result.spine || {};
        count = (spine[RV.state.activeTab] || []).filter(i => i._meta && i._meta.status === 'removed').length;
      } else {
        const so = (RV.state.result.skill_outputs || []).find(s => s.skill_slug === RV.state.activeTab);
        count = (so ? (so.items || []) : []).filter(i => i._meta && i._meta.status === 'removed').length;
      }

      if (footerBtn) {
        if (count === 0 && !RV.state.showRemoved) {
          const footer = panels.querySelector('.tab-panel-footer');
          if (footer) footer.remove();
        } else {
          footerBtn.textContent = RV.state.showRemoved ? `Hide removed (${count})` : `Show removed (${count})`;
        }
      } else if (count > 0 || RV.state.showRemoved) {
        if (RV.renderTabFooter) {
          RV.renderTabFooter(count, panels);
        }
      }
    }
  }

  /**
   * Refresh only the KPI number elements inside the Artifact-1 card.
   * Avoids a full re-render of the apps card while keeping counts correct.
   */
  function _refreshAMSKpis(allItems, panels) {
    const active = RV.activeItems.bind(RV);

    function countActive(types) {
      return RV.activeItems(allItems.filter(i => types.includes(i.artifact_type))).length;
    }

    const kpis = {
      'ams-kpi-apps':           countActive(['overview_app']),
      'ams-kpi-reqs':           countActive(['req_classification','ticket_volume','tool','governance','deliverable','resource']),
      'ams-kpi-slas':           countActive(['sla_target','availability','rto_rpo','sr_change_sla']),
      'ams-kpi-trans':          countActive(['transition_phase','kt_requirement','exit_obligation']),
      'ams-kpi-penalties':      countActive(['penalty','liquidated_damages']),
      'ams-kpi-clarifications': countActive(['clarification_question','assumption_dependency']),
    };

    for (const [id, val] of Object.entries(kpis)) {
      const el = panels.querySelector('#' + id);
      if (el) el.textContent = val || '—';
    }
  }

  // ── BUILD ROW HTML ─────────────────────────────────────────────────────────

  /**
   * Build the replacement HTML for a single row after a field-edit PATCH.
   * For AMS table rows (tr) and skill/spine list rows (li).
   */
  function _rowHtmlForItem(item, bucket) {
    // AMS table rows (apps, sla): rendered as <tr>
    const amsTableSections = ['apps', 'sla'];
    const artifactType = item.artifact_type;

    if (bucket === 'ams' && artifactType) {
      const entry = RV.AMS_SECTION_MAP[artifactType];
      const section = entry ? entry.section : null;
      if (amsTableSections.includes(section)) {
        // Build a tr using the same logic as the renderers
        return _makeAMSTableRow(item, section);
      }
    }

    // Default: build an li via makeSkillRow (for AMS list sections, skill, spine)
    const isSpine = ['compliance', 'risk', 'ambiguity', 'commercial_structure'].includes(bucket);
    const li = isSpine ? RV.makeSpineRow(item, bucket) : RV.makeSkillRow(item, bucket);
    // Serialise to HTML string
    const tmp = document.createElement('ul');
    tmp.appendChild(li);
    return tmp.innerHTML;
  }

  function _makeAMSTableRow(item, section) {
    const uid     = item._meta && item._meta.uid ? item._meta.uid : '';
    const removed = item._meta && item._meta.status === 'removed';
    const rowStyle = removed ? 'border-bottom:1px solid #f0f2f5;opacity:0.55;' : 'border-bottom:1px solid #f0f2f5;';
    const uidAttr = uid ? ` data-uid="${esc(uid)}"` : '';
    const actions  = RV.makeActionCluster(item);
    const badges   = RV.makeStatusBadges(item);

    if (section === 'apps') {
      return `<tr style="${rowStyle}"${uidAttr}>` +
        `<td style="padding:6px 8px;font-family:var(--font-mono);font-size:11px;">${esc(item.id)}</td>` +
        `<td style="padding:6px 8px;font-weight:600;">${esc(item.title || '')}${badges}</td>` +
        `<td style="padding:6px 8px;">${esc(item.category || item.description || '—')}</td>` +
        `<td style="padding:6px 8px;"><span class="badge ${item.impact === 'high' ? 'badge--critical' : ''}">${esc(item.severity_level || item.impact || 'Standard')}</span></td>` +
        `<td style="padding:6px 8px;">${esc(item.target_value || '—')}</td>` +
        `<td style="padding:6px 8px;font-size:11px;color:#6b7280;">${esc(item.source_section || '')}</td>` +
        `<td style="padding:6px 8px;" class="ams-row-actions">${actions}</td>` +
        `</tr>`;
    }
    if (section === 'sla') {
      return `<tr style="${rowStyle}"${uidAttr}>` +
        `<td style="padding:6px 8px;font-family:var(--font-mono);font-size:11px;">${esc(item.id)}</td>` +
        `<td style="padding:6px 8px;"><span class="badge ${item.severity_level && item.severity_level.includes('1') ? 'badge--critical' : ''}">${esc(item.severity_level || item.priority || '—')}</span></td>` +
        `<td style="padding:6px 8px;font-weight:600;">${esc(item.title || '')}${badges}</td>` +
        `<td style="padding:6px 8px;">${esc(item.sla_type || item.artifact_type || '')}</td>` +
        `<td style="padding:6px 8px;font-weight:600;color:var(--color-ams);">${esc(item.target_value || '—')}</td>` +
        `<td style="padding:6px 8px;color:#b91c1c;">${esc(item.penalty_value || item.penalty_type || '—')}</td>` +
        `<td style="padding:6px 8px;font-size:11px;color:#6b7280;">${esc(item.source_section || '')}</td>` +
        `<td style="padding:6px 8px;" class="ams-row-actions">${actions}</td>` +
        `</tr>`;
    }
    return '';
  }

  // ── ITEM ACTIONS ───────────────────────────────────────────────────────────

  async function removeItem(uid) {
    const item = _itemFromState(uid);
    const bucket = _bucketForUid(uid);
    if (!bucket) return;
    const oldSection = _sectionForItem(item, bucket);

    _setSaveIndicator('saving', RV.state.revision);
    try {
      const resp = await V2.apiSend('DELETE', `/jobs/${window.JOB_ID}/items/${uid}`);
      mutationSuccess(bucket, uid, resp.item, resp.edit_state);
      rerenderSection([oldSection]);
    } catch (e) {
      _setSaveIndicator('failed', RV.state.revision);
      alert('Remove failed: ' + (e.message || e));
    }
  }

  async function restoreItem(uid) {
    const item = _itemFromState(uid);
    const bucket = _bucketForUid(uid);
    if (!bucket) return;
    const oldSection = _sectionForItem(item, bucket);

    _setSaveIndicator('saving', RV.state.revision);
    try {
      const resp = await V2.apiSend('POST', `/jobs/${window.JOB_ID}/items/${uid}/restore`);
      mutationSuccess(bucket, uid, resp.item, resp.edit_state);
      rerenderSection([oldSection]);
    } catch (e) {
      _setSaveIndicator('failed', RV.state.revision);
      alert('Restore failed: ' + (e.message || e));
    }
  }

  async function revertItem(uid) {
    const item = _itemFromState(uid);
    const bucket = _bucketForUid(uid);
    if (!bucket) return;
    const oldSection = _sectionForItem(item, bucket);

    _setSaveIndicator('saving', RV.state.revision);
    try {
      const resp = await V2.apiSend('POST', `/jobs/${window.JOB_ID}/items/${uid}/revert`);
      mutationSuccess(bucket, uid, resp.item, resp.edit_state);
      // Revert is a field-level operation — replace only the row
      replaceRow(uid, _rowHtmlForItem(resp.item, bucket));
    } catch (e) {
      _setSaveIndicator('failed', RV.state.revision);
      alert('Revert failed: ' + (e.message || e));
    }
  }

  // ── BULK OPERATIONS (2.1) ──────────────────────────────────────────────────

  /**
   * _renderBulkBar()
   * Inserts or removes the bulk action bar at the top of #tab-panels based on
   * whether RV.state.selection is non-empty.
   */
  function _renderBulkBar() {
    const panels = document.getElementById('tab-panels');
    if (!panels) return;

    // Remove existing bar first
    const existing = document.getElementById('bulk-bar');
    if (existing) existing.remove();

    const sel = RV.state.selection;
    if (!sel || sel.size === 0) return;

    const bar = document.createElement('div');
    bar.id = 'bulk-bar';
    bar.className = 'bulk-bar';

    const count = document.createElement('span');
    count.className = 'bulk-bar__count';
    count.textContent = sel.size + ' item' + (sel.size === 1 ? '' : 's') + ' selected';

    const removeBtn = document.createElement('button');
    removeBtn.type = 'button';
    removeBtn.className = 'bulk-bar__btn bulk-bar__btn--remove';
    removeBtn.dataset.action = 'bulk-remove';
    removeBtn.textContent = 'Remove selected';

    const restoreBtn = document.createElement('button');
    restoreBtn.type = 'button';
    restoreBtn.className = 'bulk-bar__btn bulk-bar__btn--restore';
    restoreBtn.dataset.action = 'bulk-restore';
    restoreBtn.textContent = 'Restore selected';

    const clearBtn = document.createElement('button');
    clearBtn.type = 'button';
    clearBtn.className = 'bulk-bar__btn bulk-bar__btn--clear';
    clearBtn.dataset.action = 'bulk-clear';
    clearBtn.textContent = 'Clear selection';

    bar.appendChild(count);
    bar.appendChild(removeBtn);
    bar.appendChild(restoreBtn);
    bar.appendChild(clearBtn);
    panels.insertBefore(bar, panels.firstChild);
  }

  /** Clear all selections and refresh bar. */
  function clearSelection() {
    RV.state.selection = new Set();
    // Uncheck all visible checkboxes
    const panels = document.getElementById('tab-panels');
    if (panels) {
      panels.querySelectorAll('.req-row__checkbox').forEach(cb => { cb.checked = false; });
      panels.querySelectorAll('.req-row--selected').forEach(row => {
        row.classList.remove('req-row--selected');
      });
    }
    _renderBulkBar();
  }

  /**
   * _applyBulkResults(results, editState)
   *
   * After a bulk API call, apply each successful result into RV.state without
   * a full refetch (r3-1).  Then re-render affected sections.
   */
  function _applyBulkResults(results, editState) {
    const sectionsToRerender = new Set();

    for (const entry of results) {
      if (!entry.ok) continue;
      const uid = entry.uid;
      const found = RV.findItemByUid(uid);
      if (!found) continue;

      const { item, bucket } = found;
      // Apply the new meta from the result
      if (item._meta) {
        item._meta.status  = entry.meta.status;
        item._meta.version = entry.meta.version;
      }

      const section = _sectionForItem(item, bucket);
      if (section) sectionsToRerender.add(section);
    }

    // Update global state
    if (editState) {
      RV.state.editState = editState;
      if (editState.revision != null) {
        RV.state.revision = editState.revision;
      }
    }

    _setSaveIndicator('saved', RV.state.revision);
    RV.renderStats(RV.state.result);

    // Re-render affected sections
    if (sectionsToRerender.size > 0) {
      rerenderSection([...sectionsToRerender]);
    }
  }

  async function bulkRemove() {
    const sel = RV.state.selection;
    if (!sel || sel.size === 0) return;
    const uids = [...sel];

    _setSaveIndicator('saving', RV.state.revision);
    try {
      const resp = await V2.apiSend(
        'POST',
        `/jobs/${window.JOB_ID}/items/bulk`,
        { uids, op: 'remove' },
      );
      clearSelection();
      _applyBulkResults(resp.results, resp.edit_state);
    } catch (e) {
      _setSaveIndicator('failed', RV.state.revision);
      alert('Bulk remove failed: ' + (e.message || e));
    }
  }

  async function bulkRestore() {
    const sel = RV.state.selection;
    if (!sel || sel.size === 0) return;
    const uids = [...sel];

    _setSaveIndicator('saving', RV.state.revision);
    try {
      const resp = await V2.apiSend(
        'POST',
        `/jobs/${window.JOB_ID}/items/bulk`,
        { uids, op: 'restore' },
      );
      clearSelection();
      _applyBulkResults(resp.results, resp.edit_state);
    } catch (e) {
      _setSaveIndicator('failed', RV.state.revision);
      alert('Bulk restore failed: ' + (e.message || e));
    }
  }

  // ── RESET EDITS (1.4) ──────────────────────────────────────────────────────

  async function resetEdits() {
    if (!confirm('Are you sure you want to reset all edits? This will archive your changes and restore the original AI analysis.')) {
      return;
    }
    _setSaveIndicator('saving', RV.state.revision);
    try {
      const resp = await V2.apiSend('POST', `/jobs/${window.JOB_ID}/reset?confirm=true`);
      if (resp && resp.edit_state) {
        RV.state.editState = resp.edit_state;
        RV.state.revision = resp.edit_state.revision || 0;
      }
      _setSaveIndicator('saved', RV.state.revision);
      // Full re-fetch and re-render since uids change after re-bootstrap
      if (typeof RV.load === 'function') {
        await RV.load();
      } else if (typeof RV.boot === 'function') {
        RV.boot();
      }
    } catch (e) {
      _setSaveIndicator('failed', RV.state.revision);
      alert('Reset failed: ' + (e.message || e));
    }
  }

  // ── EDIT DRAWER ────────────────────────────────────────────────────────────

  let _drawerUid    = null;   // null = add mode
  let _drawerBucket = null;
  let _drawerSpecs  = null;
  let _drawerPrimaryType  = null;
  let _drawerAllowedTypes = null;

  /**
   * Fetch field specs (cached per "<kind>:<key>").
   */
  async function _fetchFieldSpecs(bucket) {
    const spineKeys = ['compliance', 'risk', 'ambiguity', 'commercial_structure'];
    const kind = spineKeys.includes(bucket) ? 'spine' : 'skill';
    const cacheKey = kind + ':' + bucket;
    if (_fieldSpecCache[cacheKey]) return _fieldSpecCache[cacheKey];
    const specs = await V2.apiGet(`/jobs/${window.JOB_ID}/fields?kind=${encodeURIComponent(kind)}&key=${encodeURIComponent(bucket)}`);
    _fieldSpecCache[cacheKey] = specs;
    return specs;
  }

  /**
   * openDrawer(uid | null, bucket, primaryType?, allowedTypes?)
   *
   * uid === null  → add mode (POST /items)
   * uid !== null  → edit mode (PATCH /items/{uid})
   *
   * primaryType / allowedTypes are used for add-mode on AMS sections to
   * pre-select and restrict the artifact_type field.
   */
  async function openDrawer(uid, bucket, primaryType, allowedTypes) {
    _drawerUid    = uid;
    _drawerBucket = bucket;
    _drawerPrimaryType  = primaryType  || null;
    _drawerAllowedTypes = allowedTypes || null;

    const drawer = _ensureDrawer();
    _setDrawerLoading(drawer, true);
    drawer.style.display = 'flex';

    let specs;
    try {
      specs = await _fetchFieldSpecs(bucket);
    } catch (e) {
      _setDrawerLoading(drawer, false);
      drawer.querySelector('.drawer__error').textContent = 'Could not load fields: ' + (e.message || e);
      return;
    }
    _drawerSpecs = specs;
    _showWarningsBanner(drawer, null);

    const item = uid ? _itemFromState(uid) : null;
    _renderDrawerForm(drawer, specs, item);

    // For AI items in edit mode, scaffold "Show original" button
    if (uid && item && item._meta && item._meta.origin !== 'user') {
      _appendBaselineToggle(drawer, uid);
    }

    _setDrawerLoading(drawer, false);
  }

  /** Create or return the existing drawer overlay. */
  function _ensureDrawer() {
    let drawer = document.getElementById('edit-drawer');
    if (drawer) return drawer;

    drawer = document.createElement('div');
    drawer.id = 'edit-drawer';
    drawer.className = 'drawer';
    drawer.innerHTML = `
      <div class="drawer__backdrop"></div>
      <div class="drawer__panel">
        <div class="drawer__header">
          <span class="drawer__title"></span>
          <button class="drawer__close" type="button" aria-label="Close">✕</button>
        </div>
        <div class="drawer__loading" style="display:none;">Loading…</div>
        <div class="drawer__error" style="color:#b91c1c;padding:8px 0;font-size:13px;"></div>
        <form class="drawer__form" id="drawer-form" novalidate></form>
        <div class="drawer__footer">
          <button class="drawer__save btn btn--primary" type="button">Save</button>
          <button class="drawer__cancel btn" type="button">Cancel</button>
        </div>
        <div class="drawer__duplicate-banner" style="display:none;"></div>
      </div>
    `;

    // Close on backdrop click
    drawer.querySelector('.drawer__backdrop').addEventListener('click', closeDrawer);
    drawer.querySelector('.drawer__close').addEventListener('click', closeDrawer);
    drawer.querySelector('.drawer__cancel').addEventListener('click', closeDrawer);
    drawer.querySelector('.drawer__save').addEventListener('click', saveDrawer);

    document.body.appendChild(drawer);
    return drawer;
  }

  function closeDrawer() {
    const drawer = document.getElementById('edit-drawer');
    if (drawer) drawer.style.display = 'none';
    _drawerUid = null;
    _drawerBucket = null;
    _drawerSpecs  = null;
    _drawerPrimaryType  = null;
    _drawerAllowedTypes = null;
  }

  function _setDrawerLoading(drawer, loading) {
    drawer.querySelector('.drawer__loading').style.display = loading ? '' : 'none';
    drawer.querySelector('.drawer__form').style.display    = loading ? 'none' : '';
    drawer.querySelector('.drawer__footer').style.display  = loading ? 'none' : '';
  }

  function _renderDrawerForm(drawer, specs, item) {
    const isAdd = !item;
    drawer.querySelector('.drawer__title').textContent = isAdd ? 'Add item' : 'Edit item';
    drawer.querySelector('.drawer__error').textContent = '';

    const form = drawer.querySelector('#drawer-form');
    form.innerHTML = '';

    for (const spec of specs) {
      if (!spec.editable) continue;

      const group = document.createElement('div');
      group.className = 'field-group';

      const label = document.createElement('label');
      label.htmlFor = 'field-' + spec.name;
      label.textContent = spec.label + (spec.required ? ' *' : '');
      label.className = 'field-label';
      group.appendChild(label);

      if (spec.help) {
        const hint = document.createElement('div');
        hint.className = 'field-hint';
        hint.textContent = spec.help;
        group.appendChild(hint);
      }

      let input;
      const currentValue = item ? (item[spec.name] != null ? item[spec.name] : '') : (spec.name === 'artifact_type' && _drawerPrimaryType ? _drawerPrimaryType : '');

      if (spec.type === 'boolean') {
        input = document.createElement('input');
        input.type = 'checkbox';
        input.id   = 'field-' + spec.name;
        input.name = spec.name;
        input.checked = !!currentValue;
        input.className = 'field-input field-input--checkbox';

      } else if (spec.type === 'enum' && Array.isArray(spec.enum_values)) {
        input = document.createElement('select');
        input.id   = 'field-' + spec.name;
        input.name = spec.name;
        input.className = 'field-input field-input--select';

        // For artifact_type on AMS, restrict options to allowedTypes if provided
        let allowedValues = spec.enum_values;
        if (spec.name === 'artifact_type' && _drawerAllowedTypes && Array.isArray(_drawerAllowedTypes)) {
          allowedValues = spec.enum_values.filter(v => _drawerAllowedTypes.includes(v));
        }

        if (!spec.required) {
          const opt = document.createElement('option');
          opt.value = '';
          opt.textContent = '— select —';
          input.appendChild(opt);
        }
        for (const val of allowedValues) {
          const opt = document.createElement('option');
          opt.value = val;
          opt.textContent = val.replace(/_/g, ' ');
          opt.selected = String(currentValue) === val;
          input.appendChild(opt);
        }

      } else {
        // string / float / integer
        if (spec.type === 'string' && (!spec.enum_values || !spec.enum_values.length)) {
          input = document.createElement('textarea');
          input.rows = 2;
        } else {
          input = document.createElement('input');
          input.type = (spec.type === 'float' || spec.type === 'integer') ? 'number' : 'text';
          if (spec.type === 'float') input.step = '0.01';
        }
        input.id    = 'field-' + spec.name;
        input.name  = spec.name;
        input.value = currentValue != null ? String(currentValue) : '';
        input.className = 'field-input';
        if (spec.required) input.required = true;
      }

      group.appendChild(input);

      // Per-field error placeholder
      const err = document.createElement('div');
      err.className = 'field-error';
      err.id = 'field-err-' + spec.name;
      err.style.cssText = 'color:#b91c1c;font-size:12px;margin-top:2px;display:none;';
      group.appendChild(err);

      form.appendChild(group);
    }
  }

  /** Append a "Show original" toggle for AI items (lazily fetches baseline). */
  function _appendBaselineToggle(drawer, uid) {
    const form = drawer.querySelector('#drawer-form');
    const toggle = document.createElement('button');
    toggle.type = 'button';
    toggle.className = 'btn btn--link baseline-toggle';
    toggle.textContent = 'Show original AI values';
    let loaded = false;
    let visible = false;
    let baselineEl = null;

    toggle.addEventListener('click', async () => {
      if (!loaded) {
        toggle.textContent = 'Loading…';
        try {
          const data = await V2.apiGet(`/jobs/${window.JOB_ID}/items/${uid}/baseline`);
          loaded = true;
          baselineEl = document.createElement('div');
          baselineEl.className = 'baseline-preview';
          baselineEl.style.cssText = 'background:#f7f8fa;border:1px solid #e5e7eb;border-radius:4px;padding:8px 12px;font-size:12px;margin-top:8px;white-space:pre-wrap;';
          baselineEl.textContent = JSON.stringify(data.baseline, null, 2);
          form.insertAdjacentElement('afterend', baselineEl);
          visible = true;
          toggle.textContent = 'Hide original AI values';
        } catch (_) {
          toggle.textContent = 'Show original AI values';
        }
        return;
      }
      visible = !visible;
      if (baselineEl) baselineEl.style.display = visible ? '' : 'none';
      toggle.textContent = visible ? 'Hide original AI values' : 'Show original AI values';
    });

    form.insertAdjacentElement('afterend', toggle);
  }

  /** Collect form values from the drawer. */
  function _collectDrawerFields(specs) {
    const fields = {};
    for (const spec of specs) {
      if (!spec.editable) continue;
      const el = document.getElementById('field-' + spec.name);
      if (!el) continue;
      if (spec.type === 'boolean') {
        fields[spec.name] = el.checked;
      } else if (spec.type === 'float') {
        const v = el.value.trim();
        fields[spec.name] = v !== '' ? parseFloat(v) : null;
      } else if (spec.type === 'integer') {
        const v = el.value.trim();
        fields[spec.name] = v !== '' ? parseInt(v, 10) : null;
      } else {
        const v = el.value;
        fields[spec.name] = v !== '' ? v : null;
      }
    }
    return fields;
  }

  /** Clear all per-field error messages in the drawer. */
  function _clearDrawerErrors() {
    const drawer = document.getElementById('edit-drawer');
    if (!drawer) return;
    drawer.querySelector('.drawer__error').textContent = '';
    drawer.querySelectorAll('.field-error').forEach(el => {
      el.textContent = '';
      el.style.display = 'none';
    });
  }

  /**
   * Render non-blocking warnings (2.2) into the drawer's dismissable banner.
   * Entries are either strings or {type:"possible_duplicate", uid, id, similarity}.
   * Passing null/empty hides the banner. Returns true if the banner is shown.
   */
  function _showWarningsBanner(drawer, warnings) {
    const banner = drawer ? drawer.querySelector('.drawer__duplicate-banner') : null;
    if (!banner) return false;
    banner.textContent = '';
    if (!warnings || !warnings.length) {
      banner.style.display = 'none';
      return false;
    }
    const list = document.createElement('ul');
    list.style.cssText = 'margin:0;padding-left:16px;';
    for (const w of warnings) {
      const li = document.createElement('li');
      if (w && typeof w === 'object' && w.type === 'possible_duplicate') {
        const pct = Math.round((Number(w.similarity) || 0) * 100);
        li.textContent = 'Possible duplicate of ' + (w.id || w.uid || '') + ' (' + pct + '% similar)';
      } else {
        li.textContent = String(w);
      }
      list.appendChild(li);
    }
    const dismiss = document.createElement('button');
    dismiss.type = 'button';
    dismiss.className = 'btn btn--link drawer__banner-dismiss';
    dismiss.setAttribute('aria-label', 'Dismiss');
    dismiss.textContent = '✕';
    dismiss.style.cssText = 'float:right;';
    dismiss.addEventListener('click', () => { banner.style.display = 'none'; });
    banner.appendChild(dismiss);
    banner.appendChild(list);
    banner.style.display = '';
    return true;
  }

  /**
   * saveDrawer()
   *
   * Sends POST /items (add) or PATCH /items/{uid} (edit).
   * On success: mutationSuccess → DOM update.
   * On 409: shows merge prompt with current_item values.
   * On 422: shows per-field errors.
   */
  async function saveDrawer() {
    if (!_drawerSpecs || !_drawerBucket) return;
    _clearDrawerErrors();

    const fields = _collectDrawerFields(_drawerSpecs);
    const drawer = document.getElementById('edit-drawer');
    const errEl  = drawer ? drawer.querySelector('.drawer__error') : null;
    const bucket = _drawerBucket;
    const uid    = _drawerUid;

    _setSaveIndicator('saving', RV.state.revision);

    // ── API call (may throw 409 / 422 / network error) ─────────────────────
    let resp;
    try {
      if (!uid) {
        resp = await V2.apiSend('POST', `/jobs/${window.JOB_ID}/items`, { bucket, fields });
      } else {
        const currentItem = _itemFromState(uid);
        const baseVersion = currentItem && currentItem._meta ? currentItem._meta.version : 0;
        resp = await V2.apiSend('PATCH', `/jobs/${window.JOB_ID}/items/${uid}`, { base_version: baseVersion, fields });
      }
    } catch (e) {
      // API-level errors — these are genuine save failures.
      if (e.status === 409) {
        _handle409(e, fields, errEl);
      } else if (e.status === 422) {
        _handle422(e, errEl);
      } else {
        _setSaveIndicator('failed', RV.state.revision);
        if (errEl) errEl.textContent = 'Save failed: ' + (e.message || e);
      }
      return;
    }

    // ── API succeeded — update in-memory state then refresh DOM ───────────
    // Any error from here onwards is a rendering bug, NOT a save failure:
    // the data has already been persisted server-side.
    if (!uid) {
      // ADD
      const newUid = resp.item._meta.uid;
      mutationSuccess(bucket, newUid, resp.item, resp.edit_state);
      if (_showWarningsBanner(drawer, resp.warnings)) {
        _drawerUid = newUid;
        drawer.querySelector('.drawer__title').textContent = 'Edit item';
      } else {
        closeDrawer();
      }
      try {
        const newSection = _sectionForItem(resp.item, bucket);
        rerenderSection([newSection]);
      } catch (renderErr) {
        if (typeof console !== 'undefined') {
          console.error('[saveDrawer] rerenderSection failed (add)', renderErr);
        }
      }
    } else {
      // PATCH
      const currentItem = _itemFromState(uid);
      const newArtifactType = resp.item.artifact_type;
      const oldArtifactType = currentItem ? currentItem.artifact_type : null;
      const oldSection = _sectionForItem(currentItem, bucket);
      const newSection = _sectionForItem(resp.item, bucket);
      mutationSuccess(bucket, uid, resp.item, resp.edit_state);
      if (!_showWarningsBanner(drawer, resp.warnings)) closeDrawer();
      try {
        if (bucket === 'ams' && oldArtifactType !== newArtifactType) {
          const sections = [...new Set([oldSection, newSection].filter(Boolean))];
          rerenderSection(sections);
        } else {
          replaceRow(uid, _rowHtmlForItem(resp.item, bucket));
        }
      } catch (renderErr) {
        if (typeof console !== 'undefined') {
          console.error('[saveDrawer] DOM update failed (patch)', renderErr);
        }
      }
    }
  }

  /** Handle 409 version conflict — show merge prompt. */
  function _handle409(e, submittedFields, errEl) {
    _setSaveIndicator('failed', RV.state.revision);
    let detail = e.message || '';
    // V2.apiSend surfaces the detail object as the error message string
    // but the actual current_item is inside detail when it's an object.
    // We re-fetch the latest item from state as a fallback.
    const currentItem = _itemFromState(_drawerUid);
    const currentVersion = currentItem && currentItem._meta ? currentItem._meta.version : '?';

    const msg = `Conflict: this item was updated elsewhere (now at version ${currentVersion}).\n\nYour changes will overwrite the current values. Reload the item to review the latest, or save anyway.`;
    if (confirm(msg)) {
      // Re-send with the current version from state (now up to date if mutationSuccess was called)
      const latestVersion = currentItem && currentItem._meta ? currentItem._meta.version : 0;
      V2.apiSend('PATCH', `/jobs/${window.JOB_ID}/items/${_drawerUid}`, {
        base_version: latestVersion,
        fields: submittedFields,
      }).then(resp => {
        mutationSuccess(_drawerBucket, _drawerUid, resp.item, resp.edit_state);
        closeDrawer();
        replaceRow(_drawerUid, _rowHtmlForItem(resp.item, _drawerBucket));
      }).catch(err => {
        if (errEl) errEl.textContent = 'Save failed after retry: ' + (err.message || err);
      });
    }
  }

  /** Handle 422 validation error — show per-field messages. */
  function _handle422(e, errEl) {
    _setSaveIndicator('failed', RV.state.revision);
    // e.message is the stringified detail from apiSend
    let errors = [];
    try {
      // detail may be an array of {loc, msg} Pydantic objects, or a plain string
      const parsed = JSON.parse(e.message);
      if (Array.isArray(parsed)) {
        errors = parsed;
      } else if (Array.isArray(parsed.detail)) {
        errors = parsed.detail;
      }
    } catch (_) {
      if (errEl) errEl.textContent = 'Validation error: ' + (e.message || e);
      return;
    }
    for (const err of errors) {
      // loc is e.g. ["body", "fields", "title"] — last element is field name
      const loc = Array.isArray(err.loc) ? err.loc : [];
      const fieldName = loc[loc.length - 1];
      if (fieldName) {
        const fieldErr = document.getElementById('field-err-' + fieldName);
        if (fieldErr) {
          fieldErr.textContent = err.msg || 'Invalid value';
          fieldErr.style.display = '';
        }
      }
    }
    if (errEl && errors.length === 0) {
      errEl.textContent = 'Validation error: ' + (e.message || e);
    }
  }

  // ── ENHANCE PANEL (2.4) ───────────────────────────────────────────────────

  // One in-flight enhance per job: null when idle, uid string when pending.
  let _enhancePending = null;

  /**
   * openEnhancePanel(uid)
   *
   * Shows a floating panel with:
   *   1. Mode selector (4 modes; custom shows free-text instruction).
   *   2. "Enhance" button — disabled while a request is in flight.
   * After the proposal arrives, renders the per-field diff.
   */
  function openEnhancePanel(uid) {
    _ensureEnhancePanel();
    const panel = document.getElementById('enhance-panel');
    if (!panel) return;

    const item = _itemFromState(uid);
    const title = (item && (item.title || item.id)) || uid;

    // Header title
    const titleEl = panel.querySelector('.enhance-panel__title');
    if (titleEl) titleEl.textContent = 'Enhance: ' + title;

    // Reset panel state
    const modeSelect = panel.querySelector('#enhance-mode');
    if (modeSelect) modeSelect.value = 'clarify_wording';
    const instrWrap = panel.querySelector('.enhance-instruction-wrap');
    if (instrWrap) instrWrap.style.display = 'none';
    const instrEl = panel.querySelector('#enhance-instruction');
    if (instrEl) instrEl.value = '';
    const diffArea = panel.querySelector('.enhance-panel__diff');
    if (diffArea) diffArea.innerHTML = '';
    const errEl = panel.querySelector('.enhance-panel__error');
    if (errEl) { errEl.textContent = ''; errEl.style.display = 'none'; }
    const footer = panel.querySelector('.enhance-panel__footer');
    if (footer) footer.style.display = 'none';

    // Store uid on panel for submit handler
    panel.dataset.uid = uid;
    // Clear any previous proposal
    delete panel._proposal;
    delete panel._baseVersion;

    panel.style.display = 'flex';
  }

  function _ensureEnhancePanel() {
    if (document.getElementById('enhance-panel')) return;

    const panel = document.createElement('div');
    panel.id = 'enhance-panel';
    panel.className = 'enhance-panel';

    panel.innerHTML = `
      <div class="enhance-panel__backdrop"></div>
      <div class="enhance-panel__inner">
        <div class="enhance-panel__header">
          <span class="enhance-panel__title"></span>
          <button type="button" class="enhance-panel__close" aria-label="Close">✕</button>
        </div>
        <div class="enhance-panel__body">
          <div class="enhance-panel__error" style="display:none;"></div>
          <div class="field-group" style="margin-bottom:12px;">
            <label class="field-label" for="enhance-mode">Mode</label>
            <select id="enhance-mode" class="field-input field-input--select">
              <option value="clarify_wording">Clarify wording</option>
              <option value="measurable">Make measurable</option>
              <option value="clarification_question">Add clarification question</option>
              <option value="custom">Custom instruction</option>
            </select>
          </div>
          <div class="enhance-instruction-wrap field-group" style="display:none;margin-bottom:12px;">
            <label class="field-label" for="enhance-instruction">Instruction</label>
            <textarea id="enhance-instruction" class="field-input" rows="3" placeholder="Describe what you want the AI to do…"></textarea>
          </div>
          <button type="button" id="enhance-submit-btn" class="btn btn--primary btn--sm" style="margin-bottom:16px;">Enhance with AI</button>
          <div class="enhance-panel__diff"></div>
        </div>
        <div class="enhance-panel__footer" style="display:none;">
          <button type="button" id="enhance-accept-btn" class="btn btn--primary btn--sm">Accept</button>
          <button type="button" id="enhance-reject-btn" class="btn btn--sm">Reject</button>
        </div>
      </div>
    `;

    document.body.appendChild(panel);

    // Close on backdrop click
    panel.querySelector('.enhance-panel__backdrop').addEventListener('click', _closeEnhancePanel);
    panel.querySelector('.enhance-panel__close').addEventListener('click', _closeEnhancePanel);

    // Mode picker → show/hide instruction textarea
    panel.querySelector('#enhance-mode').addEventListener('change', function () {
      const instrWrap = panel.querySelector('.enhance-instruction-wrap');
      if (instrWrap) instrWrap.style.display = this.value === 'custom' ? '' : 'none';
    });

    // Submit → call enhance endpoint
    panel.querySelector('#enhance-submit-btn').addEventListener('click', function () {
      _submitEnhance();
    });

    // Accept
    panel.querySelector('#enhance-accept-btn').addEventListener('click', function () {
      _acceptProposal();
    });

    // Reject — close without saving
    panel.querySelector('#enhance-reject-btn').addEventListener('click', _closeEnhancePanel);
  }

  function _closeEnhancePanel() {
    const panel = document.getElementById('enhance-panel');
    if (panel) panel.style.display = 'none';
  }

  async function _submitEnhance() {
    const panel = document.getElementById('enhance-panel');
    if (!panel) return;

    const uid = panel.dataset.uid;
    if (!uid) return;

    // In-flight guard: one enhance at a time per job
    if (_enhancePending) return;
    _enhancePending = uid;

    const submitBtn = panel.querySelector('#enhance-submit-btn');
    const errEl     = panel.querySelector('.enhance-panel__error');
    const diffArea  = panel.querySelector('.enhance-panel__diff');
    const footer    = panel.querySelector('.enhance-panel__footer');
    const mode      = (panel.querySelector('#enhance-mode') || {}).value || 'clarify_wording';
    const instrEl   = panel.querySelector('#enhance-instruction');
    const instruction = (instrEl && mode === 'custom') ? instrEl.value.trim() : undefined;

    if (submitBtn) { submitBtn.disabled = true; submitBtn.textContent = 'Enhancing…'; }
    if (errEl)  { errEl.textContent = ''; errEl.style.display = 'none'; }
    if (diffArea) diffArea.innerHTML = '';
    if (footer) footer.style.display = 'none';

    try {
      const body = { mode };
      if (instruction) body.instruction = instruction;
      const resp = await V2.apiSend('POST', `/jobs/${window.JOB_ID}/items/${uid}/enhance`, body);

      // Store proposal for accept
      panel._proposal     = resp.proposal;
      panel._baseVersion  = resp.base_version;

      _renderEnhanceDiff(panel, resp.proposal);
      if (footer) footer.style.display = '';
    } catch (e) {
      if (errEl) {
        errEl.style.display = '';
        if (e.status === 502) {
          errEl.textContent = 'AI enhancement failed — the model did not return a usable proposal. Try again or choose a different mode.';
        } else if (e.status === 400) {
          errEl.textContent = 'Not allowed: ' + (e.message || 'check the mode for this item type.');
        } else {
          errEl.textContent = 'Error: ' + (e.message || String(e));
        }
      }
    } finally {
      _enhancePending = null;
      if (submitBtn) { submitBtn.disabled = false; submitBtn.textContent = 'Enhance with AI'; }
    }
  }

  /**
   * _renderEnhanceDiff(panel, proposal)
   *
   * Renders per-field before/after diff, rationale, needs_info list, and
   * unverified_numbers banner. All user-supplied text via textContent (AT-20).
   */
  function _renderEnhanceDiff(panel, proposal) {
    const diffArea = panel.querySelector('.enhance-panel__diff');
    if (!diffArea) return;
    diffArea.innerHTML = '';

    const uid      = panel.dataset.uid;
    const item     = _itemFromState(uid) || {};
    const proposed = proposal.proposed || {};

    // ── Unverified numbers banner ─────────────────────────────────────────────
    const unverified = Array.isArray(proposal.unverified_numbers) ? proposal.unverified_numbers : [];
    if (unverified.length) {
      const banner = document.createElement('div');
      banner.className = 'enhance-unverified-banner';
      const label = document.createElement('strong');
      label.textContent = 'Unverified numbers: ';
      banner.appendChild(label);
      const nums = document.createElement('span');
      nums.textContent = unverified.join(', ');
      banner.appendChild(nums);
      diffArea.appendChild(banner);
    }

    // ── Per-field diffs ───────────────────────────────────────────────────────
    const fieldNames = Object.keys(proposed);
    if (fieldNames.length === 0) {
      const empty = document.createElement('p');
      empty.className = 'enhance-panel__no-changes';
      empty.textContent = 'No field changes proposed.';
      diffArea.appendChild(empty);
    }

    for (const field of fieldNames) {
      const beforeVal = item[field] != null ? String(item[field]) : '';
      const afterVal  = proposed[field]    != null ? String(proposed[field])  : '';

      const group = document.createElement('div');
      group.className = 'enhance-diff-field';

      const lbl = document.createElement('div');
      lbl.className = 'enhance-diff-field__name';
      lbl.textContent = field.replace(/_/g, ' ');
      group.appendChild(lbl);

      const cols = document.createElement('div');
      cols.className = 'enhance-diff-cols';

      // Before
      const beforeWrap = document.createElement('div');
      beforeWrap.className = 'enhance-diff-col enhance-diff-col--before';
      const beforeLbl = document.createElement('div');
      beforeLbl.className = 'enhance-diff-col__label';
      beforeLbl.textContent = 'Before';
      const beforeText = document.createElement('div');
      beforeText.className = 'enhance-diff-col__text';
      beforeText.textContent = beforeVal;
      beforeWrap.appendChild(beforeLbl);
      beforeWrap.appendChild(beforeText);

      // After (editable textarea)
      const afterWrap = document.createElement('div');
      afterWrap.className = 'enhance-diff-col enhance-diff-col--after';
      const afterLbl = document.createElement('div');
      afterLbl.className = 'enhance-diff-col__label';
      afterLbl.textContent = 'After';
      const afterInput = document.createElement('textarea');
      afterInput.className = 'field-input enhance-diff-col__textarea';
      afterInput.rows = Math.max(2, Math.ceil(afterVal.length / 60));
      afterInput.dataset.field = field;
      afterInput.value = afterVal;
      afterWrap.appendChild(afterLbl);
      afterWrap.appendChild(afterInput);

      cols.appendChild(beforeWrap);
      cols.appendChild(afterWrap);
      group.appendChild(cols);
      diffArea.appendChild(group);
    }

    // ── Rationale ─────────────────────────────────────────────────────────────
    if (proposal.rationale) {
      const rationaleWrap = document.createElement('div');
      rationaleWrap.className = 'enhance-rationale';
      const rationaleLabel = document.createElement('div');
      rationaleLabel.className = 'enhance-rationale__label';
      rationaleLabel.textContent = 'Rationale';
      const rationaleText = document.createElement('div');
      rationaleText.className = 'enhance-rationale__text';
      rationaleText.textContent = proposal.rationale;
      rationaleWrap.appendChild(rationaleLabel);
      rationaleWrap.appendChild(rationaleText);
      diffArea.appendChild(rationaleWrap);
    }

    // ── needs_info list ───────────────────────────────────────────────────────
    const needsInfo = Array.isArray(proposal.needs_info) ? proposal.needs_info : [];
    if (needsInfo.length) {
      const niWrap = document.createElement('div');
      niWrap.className = 'enhance-needs-info';
      const niLabel = document.createElement('div');
      niLabel.className = 'enhance-needs-info__label';
      niLabel.textContent = 'Needs information';
      niWrap.appendChild(niLabel);
      const niList = document.createElement('ul');
      niList.className = 'enhance-needs-info__list';
      for (const item of needsInfo) {
        const li = document.createElement('li');
        li.textContent = String(item);
        niList.appendChild(li);
      }
      niWrap.appendChild(niList);
      diffArea.appendChild(niWrap);
    }
  }

  async function _acceptProposal() {
    const panel = document.getElementById('enhance-panel');
    if (!panel) return;

    const uid         = panel.dataset.uid;
    const baseVersion = panel._baseVersion;
    const proposal    = panel._proposal;
    if (!uid || proposal == null) return;

    // Collect potentially-edited after-values from the textareas
    const editedProposed = {};
    panel.querySelectorAll('.enhance-diff-col__textarea').forEach(function (ta) {
      editedProposed[ta.dataset.field] = ta.value;
    });
    // Merge with original proposal (preserves non-field keys like rationale etc.)
    const mergedProposal = Object.assign({}, proposal, { proposed: editedProposed });

    const acceptBtn = panel.querySelector('#enhance-accept-btn');
    const errEl     = panel.querySelector('.enhance-panel__error');
    if (acceptBtn) { acceptBtn.disabled = true; acceptBtn.textContent = 'Accepting…'; }
    if (errEl)  { errEl.textContent = ''; errEl.style.display = 'none'; }

    const bucket = _bucketForUid(uid);

    try {
      const resp = await V2.apiSend(
        'POST',
        `/jobs/${window.JOB_ID}/items/${uid}/enhance/accept`,
        { base_version: baseVersion, proposal: mergedProposal }
      );
      mutationSuccess(bucket, uid, resp.item, resp.edit_state);
      replaceRow(uid, _rowHtmlForItem(resp.item, bucket));
      _closeEnhancePanel();
      if (resp.warnings && resp.warnings.length) {
        // Show warnings briefly — re-use the duplicate banner logic if possible,
        // otherwise a simple alert for now (non-blocking)
        const msgs = resp.warnings.map(function (w) {
          if (w && w.type === 'possible_duplicate') {
            return 'Possible duplicate of ' + (w.id || w.uid || '') + ' (' + Math.round((w.similarity || 0) * 100) + '% similar)';
          }
          return String(w);
        });
        alert('Enhancement accepted.\n\nWarnings:\n' + msgs.join('\n'));
      }
    } catch (e) {
      if (acceptBtn) { acceptBtn.disabled = false; acceptBtn.textContent = 'Accept'; }
      if (errEl) {
        errEl.style.display = '';
        if (e.status === 409) {
          errEl.textContent = 'Conflict: the item was updated while you were reviewing the proposal. Please close and re-open Enhance.';
        } else if (e.status === 422) {
          errEl.textContent = 'Validation error: ' + (e.message || 'one or more proposed fields are invalid.');
        } else {
          errEl.textContent = 'Accept failed: ' + (e.message || String(e));
        }
      }
    }
  }

  // ── DELEGATED CLICK HANDLER ────────────────────────────────────────────────

  document.addEventListener('DOMContentLoaded', function () {
    const resetBtn = document.getElementById('btn-reset-edits');
    if (resetBtn) {
      resetBtn.addEventListener('click', resetEdits);
    }

    const panels = document.getElementById('tab-panels');
    if (!panels) return;

    panels.addEventListener('click', function (e) {
      const target = e.target.closest('[data-action]');
      if (!target) return;

      const action = target.dataset.action;
      const uid    = target.dataset.uid || null;

      switch (action) {
        case 'toggle-removed':
          RV.state.showRemoved = !RV.state.showRemoved;
          RV.renderTabPanel(RV.state.activeTab);
          break;

        case 'edit-item':
          if (uid) {
            const bucket = _bucketForUid(uid);
            if (bucket) openDrawer(uid, bucket);
          }
          break;

        case 'remove-item':
          if (uid) removeItem(uid);
          break;

        case 'restore-item':
          if (uid) restoreItem(uid);
          break;

        case 'enhance-item':
          if (uid) openEnhancePanel(uid);
          break;

        case 'revert-item':
          if (uid) revertItem(uid);
          break;

        case 'add-item': {
          const bucket       = target.dataset.bucket;
          const primaryType  = target.dataset.primaryType  || null;
          const allowedTypes = target.dataset.allowedTypes
            ? JSON.parse(target.dataset.allowedTypes)
            : null;
          if (bucket) openDrawer(null, bucket, primaryType, allowedTypes);
          break;
        }

        // Bulk selection (2.1)
        case 'select-item':
          if (uid) {
            if (!RV.state.selection) RV.state.selection = new Set();
            const row = target.closest('[data-uid]');
            if (target.checked) {
              RV.state.selection.add(uid);
              if (row) row.classList.add('req-row--selected');
            } else {
              RV.state.selection.delete(uid);
              if (row) row.classList.remove('req-row--selected');
            }
            _renderBulkBar();
          }
          break;

        case 'bulk-remove':
          bulkRemove();
          break;

        case 'bulk-restore':
          bulkRestore();
          break;

        case 'bulk-clear':
          clearSelection();
          break;
      }
    });
  });

  // ── EXPORT MENU ────────────────────────────────────────────────────────────

  (function initExportMenu() {
    const toggleBtn    = document.getElementById('btn-export');
    const dropdown     = document.getElementById('export-dropdown');
    const downloadBtn  = document.getElementById('btn-export-download');

    if (!toggleBtn || !dropdown || !downloadBtn) return;

    // Toggle dropdown open/close
    toggleBtn.addEventListener('click', function (e) {
      e.stopPropagation();
      const open = dropdown.style.display !== 'none';
      dropdown.style.display = open ? 'none' : 'block';
      toggleBtn.setAttribute('aria-expanded', String(!open));
    });

    // Close on outside click
    document.addEventListener('click', function (e) {
      if (!document.getElementById('export-menu').contains(e.target)) {
        dropdown.style.display = 'none';
        toggleBtn.setAttribute('aria-expanded', 'false');
      }
    });

    // Download button
    downloadBtn.addEventListener('click', function () {
      const format  = document.getElementById('export-format').value;
      const viewEl  = document.querySelector('input[name="export-view"]:checked');
      const view    = viewEl ? viewEl.value : 'edited';
      const removed = document.getElementById('export-include-removed').checked;

      const params = new URLSearchParams({
        format:          format,
        view:            view,
        include_removed: removed ? 'true' : 'false',
      });

      const url = '/jobs/' + window.JOB_ID + '/export?' + params.toString();

      // Trigger browser file download via a temporary anchor
      const a = document.createElement('a');
      a.href = url;
      a.style.display = 'none';
      document.body.appendChild(a);
      a.click();
      document.body.removeChild(a);

      dropdown.style.display = 'none';
      toggleBtn.setAttribute('aria-expanded', 'false');
    });
  })();

  // ── HISTORY PANEL ──────────────────────────────────────────────────────────

  const _HISTORY_PAGE_SIZE = 50;
  let _historyPage = 1;
  let _historyTotal = 0;
  let _historyLoading = false;

  /** Create or return the history panel element. */
  function _ensureHistoryPanel() {
    let panel = document.getElementById('history-panel');
    if (panel) return panel;

    panel = document.createElement('div');
    panel.id = 'history-panel';
    panel.className = 'history-panel';
    panel.setAttribute('role', 'dialog');
    panel.setAttribute('aria-label', 'Edit history');

    const drawer = document.createElement('div');
    drawer.className = 'history-panel__drawer';

    const header = document.createElement('div');
    header.className = 'history-panel__header';
    const titleEl = document.createElement('span');
    titleEl.className = 'history-panel__title';
    titleEl.textContent = 'Edit History';
    const closeBtn = document.createElement('button');
    closeBtn.className = 'history-panel__close';
    closeBtn.type = 'button';
    closeBtn.setAttribute('aria-label', 'Close history');
    closeBtn.textContent = '\u2715';
    header.appendChild(titleEl);
    header.appendChild(closeBtn);

    const body = document.createElement('div');
    body.id = 'history-panel-body';
    body.className = 'history-panel__body';

    const footer = document.createElement('div');
    footer.id = 'history-panel-footer';
    footer.className = 'history-panel__footer';
    footer.style.display = 'none';

    const loadMoreBtn = document.createElement('button');
    loadMoreBtn.id = 'history-load-more';
    loadMoreBtn.type = 'button';
    loadMoreBtn.className = 'btn btn--sm';
    loadMoreBtn.textContent = 'Load more';
    footer.appendChild(loadMoreBtn);

    const backdrop = document.createElement('div');
    backdrop.className = 'history-panel__backdrop';

    drawer.appendChild(header);
    drawer.appendChild(body);
    drawer.appendChild(footer);
    panel.appendChild(backdrop);
    panel.appendChild(drawer);
    document.body.appendChild(panel);

    // Event bindings
    backdrop.addEventListener('click', closeHistoryPanel);
    closeBtn.addEventListener('click', closeHistoryPanel);
    loadMoreBtn.addEventListener('click', function () {
      if (!_historyLoading) _loadHistoryPage(_historyPage + 1, false);
    });

    return panel;
  }

  /**
   * _renderHistoryEntries(entries, append)
   *
   * Renders history entries into #history-panel-body.
   * All user-controlled text goes through textContent (never innerHTML interpolation).
   */
  function _renderHistoryEntries(entries, append) {
    const body = document.getElementById('history-panel-body');
    if (!body) return;

    if (!append) body.innerHTML = '';

    if (!entries || entries.length === 0) {
      if (!append) {
        const empty = document.createElement('div');
        empty.className = 'history-panel__empty';
        empty.textContent = 'No edits yet.';
        body.appendChild(empty);
      }
      return;
    }

    for (const entry of entries) {
      const row = document.createElement('div');
      row.className = 'history-entry';

      // Top line: timestamp (right) + op badge + id
      const topLine = document.createElement('div');

      const tsEl = document.createElement('span');
      tsEl.className = 'history-entry__ts';
      // Format ISO timestamp to a short local string
      const ts = entry.ts ? new Date(entry.ts).toLocaleString() : '';
      tsEl.textContent = ts;
      topLine.appendChild(tsEl);

      const opEl = document.createElement('span');
      opEl.className = 'history-entry__op';
      opEl.textContent = entry.op || '';
      topLine.appendChild(opEl);

      const idEl = document.createElement('span');
      idEl.className = 'history-entry__id';
      idEl.textContent = entry.id || entry.uid || '';
      topLine.appendChild(idEl);

      row.appendChild(topLine);

      // Fields changed
      const fc = entry.fields_changed;
      if (fc && typeof fc === 'object') {
        const fieldNames = Object.keys(fc);
        if (fieldNames.length > 0) {
          const fcEl = document.createElement('div');
          fcEl.className = 'history-entry__fields';
          for (const fname of fieldNames) {
            const fLine = document.createElement('div');
            const change = fc[fname] || {};
            // Use textContent throughout — no innerHTML interpolation
            const fnameSpan = document.createElement('strong');
            fnameSpan.textContent = fname + ': ';
            fLine.appendChild(fnameSpan);
            const fromSpan = document.createElement('span');
            fromSpan.textContent = change.from != null ? String(change.from) : '\u2014';
            fLine.appendChild(fromSpan);
            const arrowSpan = document.createElement('span');
            arrowSpan.textContent = ' \u2192 ';
            fLine.appendChild(arrowSpan);
            const toSpan = document.createElement('span');
            toSpan.textContent = change.to != null ? String(change.to) : '\u2014';
            fLine.appendChild(toSpan);
            fcEl.appendChild(fLine);
          }
          row.appendChild(fcEl);
        }
      }

      body.appendChild(row);
    }
  }

  /**
   * _loadHistoryPage(page, reset)
   *
   * Fetches page N of the history log. If reset=true, clears existing entries.
   */
  async function _loadHistoryPage(page, reset) {
    if (_historyLoading) return;
    _historyLoading = true;

    const body   = document.getElementById('history-panel-body');
    const footer = document.getElementById('history-panel-footer');

    if (reset) {
      _historyPage = 1;
      _historyTotal = 0;
      if (body) {
        body.innerHTML = '';
        const loading = document.createElement('div');
        loading.className = 'history-panel__loading';
        loading.textContent = 'Loading\u2026';
        body.appendChild(loading);
      }
      if (footer) footer.style.display = 'none';
    }

    try {
      const params = new URLSearchParams({
        page:      String(page),
        page_size: String(_HISTORY_PAGE_SIZE),
      });
      const data = await V2.apiGet(
        '/jobs/' + window.JOB_ID + '/history?' + params.toString()
      );
      _historyPage  = data.page;
      _historyTotal = data.total;

      _renderHistoryEntries(data.entries || [], !reset);

      // Show/hide load-more
      const loaded = (_historyPage - 1) * _HISTORY_PAGE_SIZE + (data.entries || []).length;
      if (footer) {
        footer.style.display = loaded < _historyTotal ? '' : 'none';
      }
    } catch (e) {
      if (body) {
        const errEl = document.createElement('div');
        errEl.className = 'history-panel__empty';
        errEl.textContent = 'Failed to load history: ' + (e.message || e);
        if (reset) body.innerHTML = '';
        body.appendChild(errEl);
      }
    } finally {
      _historyLoading = false;
    }
  }

  function openHistoryPanel() {
    const panel = _ensureHistoryPanel();
    panel.style.display = 'flex';
    document.getElementById('btn-history') &&
      document.getElementById('btn-history').setAttribute('aria-expanded', 'true');
    _loadHistoryPage(1, true);
  }

  function closeHistoryPanel() {
    const panel = document.getElementById('history-panel');
    if (panel) panel.style.display = 'none';
    const btn = document.getElementById('btn-history');
    if (btn) btn.setAttribute('aria-expanded', 'false');
  }

  // Wire History button (it may exist before DOMContentLoaded if script runs late)
  document.addEventListener('DOMContentLoaded', function () {
    const histBtn = document.getElementById('btn-history');
    if (histBtn) histBtn.addEventListener('click', openHistoryPanel);
  });

  // ── EXPOSE ON window.RV ────────────────────────────────────────────────────
  // Extend the existing RV object so tests can call these directly.

  Object.assign(window.RV, {
    mutationSuccess,
    replaceRow,
    rerenderSection,
    openDrawer,
    saveDrawer,
    closeDrawer,
    removeItem,
    restoreItem,
    revertItem,
    resetEdits,
    openHistoryPanel,
    closeHistoryPanel,
    _renderHistoryEntries,
    _loadHistoryPage,
    // Bulk operations (2.1)
    bulkRemove,
    bulkRestore,
    clearSelection,
    _renderBulkBar,
    _applyBulkResults,
    // Duplicate warnings (2.2)
    _showWarningsBanner,
    // Enhance panel (2.4)
    openEnhancePanel,
    _closeEnhancePanel,
    _renderEnhanceDiff,
    _submitEnhance,
    _acceptProposal,
    get _enhancePending() { return _enhancePending; },
    set _enhancePending(v) { _enhancePending = v; },
  });

})();
