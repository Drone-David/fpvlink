/* ═══════════════════════════════════════════════════════════
   FPVLink — Outputs view
   Everything you set up before a session. Never touched mid-flight.

   Owns the config draft for the whole app: the System tab edits two fields
   (goggles model, auto-connect) that live in the same config document, so both
   views read and write through here.
═══════════════════════════════════════════════════════════ */

'use strict';

import {
  $, store, on, API, saveConfig, loadLuts, loadConfig, pushLog,
} from './state.js';
import { renderDestinations } from './monitor.js';

// Keep in sync with STANDBY_CARDS in capture/pipeline.py and the validator
// array in web/js/../server.js — an id offered here that the pipeline doesn't
// know falls back to the default card without telling the operator why.
const STANDBY_CARDS = [
  { id: 'grounded',      label: 'Grounded', desc: 'Branded standby card' },
  { id: 'grounded_anim', label: 'Grounded (moving)', desc: 'Animated — shows the box is alive' },
  { id: 'bars',          label: 'Bars',     desc: 'SMPTE-style test pattern' },
  { id: 'black',         label: 'Black',    desc: 'Sync black' },
];

// Fields not backed by a form control.
let draftLutId = '';
let draftStandby = 'grounded';
let dirty = false;

// ─────────────────────────────────────────────
// Dirty tracking
// ─────────────────────────────────────────────
export function isDirty() { return dirty; }

export function markDirty() {
  dirty = true;
  renderSaveCard();
}

function clearDirty() {
  dirty = false;
  renderSaveCard();
}

/** Called by the router before switching away from Outputs or System. */
export function confirmLeave() {
  if (!dirty) return true;
  return confirm('You have unsaved configuration changes. Leave without saving?');
}

function renderSaveCard(justSaved) {
  const note = $('saveNote');
  const btn  = $('saveBtn');
  if (!note || !btn) return;

  if (justSaved) {
    note.textContent = 'Saved. The pipeline picked up the change without a restart.';
  } else if (dirty) {
    note.textContent = 'Unsaved changes. Outputs restart on save — about a 3 second blip on HDMI.';
  } else {
    note.textContent = 'Configuration matches what is running on the device.';
  }
  btn.disabled = !dirty;
}

// ─────────────────────────────────────────────
// Form <-> config
// ─────────────────────────────────────────────
function applyConfigToForm() {
  const cfg = store.config;
  if (!cfg) return;

  $('srtEnabled').checked  = !!cfg.srt_enabled;
  $('srtUrl').value        = cfg.srt_url  || '';
  $('rtmpEnabled').checked = !!cfg.rtmp_enabled;
  $('rtmpUrl').value       = cfg.rtmp_url || '';
  $('rtmpKey').value       = cfg.rtmp_key || '';
  $('ndiEnabled').checked  = !!cfg.ndi_enabled;
  $('ndiName').value       = cfg.ndi_name || '';
  $('hdmiLutEnabled').checked = !!cfg.hdmi_lut_enabled;

  // System tab fields — same config document.
  $('gogglesModel').value  = cfg.goggles_model || 'auto';
  $('autoConnect').checked = cfg.auto_connect !== false;

  draftLutId   = cfg.hdmi_lut_active_id || '';
  draftStandby = cfg.standby_card || 'grounded';

  syncBodies();
  renderLuts();
  renderStandby();
  clearDirty();
}

function readForm() {
  return {
    goggles_model:      $('gogglesModel').value,
    auto_connect:       $('autoConnect').checked,
    bitrate_mbps:       50,   // fixed at source rate; see the System tab note
    srt_enabled:        $('srtEnabled').checked,
    srt_url:            $('srtUrl').value.trim(),
    rtmp_enabled:       $('rtmpEnabled').checked,
    rtmp_url:           $('rtmpUrl').value.trim(),
    rtmp_key:           $('rtmpKey').value,
    ndi_enabled:        $('ndiEnabled').checked,
    ndi_name:           $('ndiName').value.trim(),
    hdmi_lut_enabled:   $('hdmiLutEnabled').checked,
    hdmi_lut_active_id: draftLutId,
    standby_card:       draftStandby,
  };
}

function syncBodies() {
  $('srtBody').hidden  = !$('srtEnabled').checked;
  $('rtmpBody').hidden = !$('rtmpEnabled').checked;
  $('ndiBody').hidden  = !$('ndiEnabled').checked;
  $('lutBody').hidden  = !$('hdmiLutEnabled').checked;
}

// ─────────────────────────────────────────────
// Save / reset
// ─────────────────────────────────────────────
async function doSave({ silent = false } = {}) {
  const btn = $('saveBtn');
  const payload = readForm();

  if (btn) { btn.disabled = true; btn.textContent = 'Saving…'; }

  try {
    await saveConfig(payload);
    clearDirty();
    if (!silent) {
      renderSaveCard(true);
      if (btn) btn.textContent = 'Saved ✓';
      setTimeout(() => {
        if (btn) btn.textContent = 'Save & apply';
        renderSaveCard();
      }, 2600);
    } else if (btn) {
      btn.textContent = 'Save & apply';
    }
    pushLog('[OK] Configuration saved');
  } catch (err) {
    pushLog(`[ERROR] Save failed: ${err.message}`);
    dirty = true;
    if (btn) btn.textContent = 'Save & apply';
    renderSaveCard();
  }
}

async function doReset() {
  try {
    await loadConfig();
    applyConfigToForm();
    pushLog('[OK] Configuration reloaded from the device');
  } catch (err) {
    pushLog(`[ERROR] Reload failed: ${err.message}`);
  }
}

// ─────────────────────────────────────────────
// RTMP note — the failing output explains itself where you would fix it
// ─────────────────────────────────────────────
function renderRtmpNote(outputs) {
  const note  = $('rtmpNote');
  const input = $('rtmpKey');
  if (!note || !input) return;

  const rtmp = (outputs || []).find((o) => o.id === 'rtmp');
  const broken = rtmp && (rtmp.state === 'retrying' || rtmp.state === 'failed');

  if (broken) {
    const other = (outputs || []).find((o) => o.id === 'srt' && o.state === 'up');
    note.textContent = `${rtmp.detail || 'The server refused this key'}${rtmp.retry ? ` after ${rtmp.retry} attempts` : ''}.${other ? ' The SRT output is unaffected.' : ''}`;
    note.classList.add('is-warn');
    input.classList.add('is-warn');
  } else {
    note.textContent = 'Stored on the device; never sent back to the browser after save.';
    note.classList.remove('is-warn');
    input.classList.remove('is-warn');
  }
}

// ─────────────────────────────────────────────
// LUTs
// ─────────────────────────────────────────────
function formatLutMeta(lut) {
  const bits = [];
  if (lut.lut_3d_size) bits.push(`${lut.lut_3d_size}³`);
  if (lut.size_bytes)  bits.push(`${Math.round(lut.size_bytes / 1024)} KB`);
  return bits.join(' · ');
}

function lutRow({ id, name, meta, selected, deletable }) {
  const row = document.createElement('div');
  row.className = 'lut-row';

  const pick = document.createElement('button');
  pick.type = 'button';
  pick.className = `pick${selected ? ' is-on' : ''}`;
  pick.setAttribute('aria-pressed', String(selected));
  pick.addEventListener('click', () => selectLut(id));

  const radio = document.createElement('span');
  radio.className = 'pick-radio';

  const label = document.createElement('span');
  label.className = 'pick-name';
  label.textContent = name;

  pick.append(radio, label);

  if (meta) {
    const m = document.createElement('span');
    m.className = 'pick-meta';
    m.textContent = meta;
    pick.append(m);
  }

  row.append(pick);

  if (deletable) {
    const del = document.createElement('button');
    del.type = 'button';
    del.className = 'lut-del';
    del.setAttribute('aria-label', `Delete ${name}`);
    del.innerHTML = '<svg width="14" height="14" viewBox="0 0 16 16" fill="none" aria-hidden="true"><path d="M4 4l8 8m0-8l-8 8" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"/></svg>';
    del.addEventListener('click', () => deleteLut(id, name));
    row.append(del);
  }

  return row;
}

function renderLuts() {
  const list = $('lutList');
  if (!list) return;

  const rows = [
    lutRow({ id: '', name: 'None — passthrough', meta: '', selected: !draftLutId, deletable: false }),
    ...store.luts.map((lut) => lutRow({
      id: lut.id,
      name: lut.display_name,
      meta: formatLutMeta(lut),
      selected: lut.id === draftLutId,
      deletable: true,
    })),
  ];

  list.replaceChildren(...rows);

  const count = store.luts.length;
  $('lutCount').textContent = `${count} of 5 used`;
  const btn = $('lutUploadBtn');
  btn.disabled = count >= 5;
  btn.textContent = count >= 5 ? 'Maximum of 5 reached' : 'Upload .cube';
}

/** Selecting a LUT applies immediately, matching the previous build. */
async function selectLut(id) {
  draftLutId = id;
  renderLuts();
  await doSave({ silent: true });
}

async function deleteLut(id, name) {
  if (!confirm(`Delete the LUT "${name}"?`)) return;
  try {
    const res = await fetch(API(`/luts/${id}`), { method: 'DELETE' });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const data = await res.json();
    store.luts = data.manifest || [];
    if (draftLutId === id) {
      draftLutId = '';
      await doSave({ silent: true });
    }
    renderLuts();
    pushLog(`[OK] Deleted LUT ${name}`);
  } catch (err) {
    pushLog(`[ERROR] Delete LUT failed: ${err.message}`);
  }
}

async function uploadLut(files) {
  if (!files || !files.length) return;
  const file = files[0];
  const btn = $('lutUploadBtn');

  if (!file.name.toLowerCase().endsWith('.cube')) {
    pushLog('[ERROR] LUT upload rejected: only .cube files are allowed');
    return;
  }

  btn.disabled = true;
  btn.textContent = 'Uploading…';

  const fd = new FormData();
  fd.append('lutFile', file);
  fd.append('displayName', file.name.replace(/\.cube$/i, ''));

  try {
    const res = await fetch(API('/lut-upload'), { method: 'POST', body: fd });
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || `HTTP ${res.status}`);
    store.luts = data.manifest || [];
    renderLuts();
    pushLog(`[OK] Uploaded LUT ${file.name}`);
  } catch (err) {
    pushLog(`[ERROR] LUT upload failed: ${err.message}`);
    renderLuts();
  } finally {
    $('lutFileInput').value = '';
  }
}

// ─────────────────────────────────────────────
// Standby / test card
// ─────────────────────────────────────────────
function renderStandby() {
  const row = $('standbyRow');
  if (!row) return;

  row.replaceChildren(...STANDBY_CARDS.map((card) => {
    const btn = document.createElement('button');
    btn.type = 'button';
    btn.className = `standby-btn${card.id === draftStandby ? ' is-on' : ''}`;
    btn.setAttribute('aria-pressed', String(card.id === draftStandby));

    const name = document.createElement('div');
    name.className = 'standby-name';
    name.textContent = card.label;

    const desc = document.createElement('div');
    desc.className = 'standby-desc';
    desc.textContent = card.desc;

    btn.append(name, desc);
    btn.addEventListener('click', async () => {
      draftStandby = card.id;
      renderStandby();
      await doSave({ silent: true });
    });
    return btn;
  }));
}

// ─────────────────────────────────────────────
// Wiring
// ─────────────────────────────────────────────
export function initOutputs() {
  // Any edit to a config-bearing control marks the draft dirty. The two System
  // tab controls are included deliberately — same config document.
  const controls = [
    'srtEnabled', 'srtUrl', 'rtmpEnabled', 'rtmpUrl', 'rtmpKey',
    'ndiEnabled', 'ndiName', 'hdmiLutEnabled', 'gogglesModel', 'autoConnect',
  ];

  controls.forEach((id) => {
    const el = $(id);
    if (!el) return;
    const evt = el.type === 'checkbox' || el.tagName === 'SELECT' ? 'change' : 'input';
    el.addEventListener(evt, () => {
      syncBodies();
      markDirty();
    });
  });

  $('saveBtn').addEventListener('click', () => doSave());
  $('resetBtn').addEventListener('click', doReset);

  $('lutUploadBtn').addEventListener('click', () => $('lutFileInput').click());
  $('lutFileInput').addEventListener('change', (e) => uploadLut(e.target.files));

  on('config', applyConfigToForm);
  on('luts', renderLuts);
  on('stats', (s) => {
    renderDestinations('destListCfg', s.outputs);
    renderRtmpNote(s.outputs);
  });

  applyConfigToForm();
  renderStandby();
  loadLuts().catch((err) => pushLog(`[ERROR] Failed to load LUTs: ${err.message}`));
}
