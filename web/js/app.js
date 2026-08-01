/* ═══════════════════════════════════════════════════════════
   FPVLink — shell
   Tab routing, the single condition banner, and boot.
═══════════════════════════════════════════════════════════ */

'use strict';

import {
  $, store, on, connectWS, loadConfig, loadStatus, loadInitialLogs,
  isLive, pacingSeverity,
} from './state.js';
import { initMonitor } from './monitor.js';
import { initOutputs, confirmLeave, isDirty } from './outputs.js';
import { initLogs } from './logs.js';
import { initSystem } from './system.js';

const TABS = ['monitor', 'outputs', 'logs', 'system'];
let activeTab = 'monitor';

// ─────────────────────────────────────────────
// Routing
// ─────────────────────────────────────────────
function showTab(name, { fromHash = false } = {}) {
  if (!TABS.includes(name)) name = 'monitor';
  if (name === activeTab) return;

  // Leaving a form with unsaved edits is the one place a tab switch can lose
  // work, so it asks first.
  if ((activeTab === 'outputs' || activeTab === 'system') && isDirty() && !confirmLeave()) {
    location.hash = `#${activeTab}`;
    return;
  }

  activeTab = name;

  TABS.forEach((t) => {
    const tab  = $(`tab-${t}`);
    const view = $(`view-${t}`);
    const on_  = t === name;
    if (tab)  tab.setAttribute('aria-selected', String(on_));
    if (view) view.hidden = !on_;
  });

  if (!fromHash) location.hash = `#${name}`;
}

function initRouter() {
  TABS.forEach((t) => {
    $(`tab-${t}`).addEventListener('click', () => showTab(t));
  });

  // In-page shortcuts: "Edit" on Destinations, "All logs" on Recent.
  document.querySelectorAll('[data-goto]').forEach((btn) => {
    btn.addEventListener('click', () => showTab(btn.dataset.goto));
  });

  // Arrow-key movement across the tablist, as expected of role="tablist".
  const tablist = document.querySelector('.tabs');
  tablist.addEventListener('keydown', (e) => {
    const i = TABS.indexOf(activeTab);
    let next = null;
    if (e.key === 'ArrowRight') next = TABS[(i + 1) % TABS.length];
    else if (e.key === 'ArrowLeft') next = TABS[(i - 1 + TABS.length) % TABS.length];
    else if (e.key === 'Home') next = TABS[0];
    else if (e.key === 'End') next = TABS[TABS.length - 1];
    if (!next) return;
    e.preventDefault();
    showTab(next);
    $(`tab-${next}`).focus();
  });

  window.addEventListener('hashchange', () => {
    showTab(location.hash.replace('#', ''), { fromHash: true });
  });

  // Land where the reload left off.
  const initial = location.hash.replace('#', '');
  if (TABS.includes(initial) && initial !== 'monitor') {
    activeTab = 'monitor';
    showTab(initial, { fromHash: true });
  }

  window.addEventListener('beforeunload', (e) => {
    if (!isDirty()) return;
    e.preventDefault();
    e.returnValue = '';
  });
}

// ─────────────────────────────────────────────
// Condition banner
// One slot, highest severity only. The old build had a separate offline banner
// and critical banner that could stack; the WebSocket case is folded in here as
// just another error condition.
// ─────────────────────────────────────────────
function computeCondition() {
  const s = store.stats || {};

  if (!store.wsConnected) {
    return { sev: 'err', text: 'WebSocket disconnected — retrying connection…', hint: 'check the network' };
  }

  if (store.capturing && s.bytes_age_ms != null && s.bytes_age_ms > 3000) {
    const secs = Math.floor(s.bytes_age_ms / 1000);
    return {
      sev: 'err',
      text: `No video from the goggles — bulk endpoint stalled for ${secs}s`,
      hint: 'check the USB-C cable',
    };
  }

  if (s.thermal_state === 'CRITICAL') {
    return {
      sev: 'err',
      text: `SOC at ${s.soc_temp}°C — throttling. Encoder may stall.`,
      hint: 'improve airflow',
    };
  }

  if (s.storage_state === 'CRITICAL') {
    return {
      sev: 'err',
      text: `Storage exhausted — ${s.storage_free_gb} GB free`,
      hint: 'free space on the device',
    };
  }

  const p95 = Number(s.frame_gap_p95_ms) || 0;
  const fps = Number(s.fps) || 0;
  if (isLive() && pacingSeverity(p95, fps) !== 'ok' && pacingSeverity(p95, fps) !== 'off') {
    const ideal = (1000 / fps).toFixed(1);
    return {
      sev: 'warn',
      text: `Frame pacing p95 is ${p95.toFixed(1)} ms, well over the ${ideal} ms ideal gap`,
      hint: 'see pacing trace',
    };
  }

  if (!store.capturing) {
    return {
      sev: 'info',
      text: 'Capture paused — standby card is on the HDMI output',
      hint: 'press resume to go live',
    };
  }

  return null;
}

function renderBanner() {
  const el = $('banner');
  const cond = computeCondition();

  if (!cond) {
    el.hidden = true;
    return;
  }

  el.hidden = false;
  el.className = `banner is-${cond.sev}`;
  $('bannerText').textContent = cond.text;
  $('bannerHint').textContent = cond.hint || '';
}

// ─────────────────────────────────────────────
// Responsive: the HUD sheds its third metric below 700px, so ingest→HDMI has
// to reappear inside Box health or the number is simply gone on a phone.
// ─────────────────────────────────────────────
function initResponsive() {
  const mq = window.matchMedia('(max-width: 700px)');
  const apply = () => { $('healthLatencyRow').hidden = !mq.matches; };
  apply();
  mq.addEventListener('change', apply);
}

// ─────────────────────────────────────────────
// Boot
// ─────────────────────────────────────────────
async function init() {
  initRouter();
  initResponsive();

  // Config first: the Outputs form and several Monitor labels read from it.
  try {
    await loadConfig();
  } catch (err) {
    console.error('config load failed', err);
  }

  try {
    const stat = await loadStatus();
    $('ftrVersion').textContent = `v${stat.firmware_version || '1.0.0'}`;
    $('hdrDevice').textContent = `${stat.device_name || 'fpvlink'} · v${stat.firmware_version || '1.0.0'}`;
  } catch (err) {
    console.error('status load failed', err);
  }

  // Prefer the real hostname over the configured display name once /api/system
  // answers — the header is identifying a box on the network, not a product.
  on('system', () => {
    const sys = store.system || {};
    if (sys.host) {
      $('hdrDevice').textContent = `${sys.host} · v${sys.firmware_version || '1.0.0'}`;
    }
  });

  initMonitor();
  initOutputs();
  initLogs();
  initSystem();

  on('stats', renderBanner);
  on('ws', renderBanner);
  on('capture', renderBanner);
  renderBanner();

  await loadInitialLogs();
  connectWS();
}

document.addEventListener('DOMContentLoaded', init);
