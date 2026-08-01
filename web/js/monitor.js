/* ═══════════════════════════════════════════════════════════
   FPVLink — Monitor view
   The flight view: is the chain live, where did it break, and pause.
═══════════════════════════════════════════════════════════ */

'use strict';

import {
  $, store, on, isLive, formatClock, formatBytes, pacingSeverity, setCapture, pushLog,
} from './state.js';

// ─────────────────────────────────────────────
// Preview + state badge
// ─────────────────────────────────────────────
function renderPreview(msg) {
  const img = $('previewImg');
  if (!msg || !msg.frame || !img) return;
  img.src = `data:image/jpeg;base64,${msg.frame}`;
  img.classList.add('has-frame');
  $('previewEmpty').hidden = true;
  renderStateBadge();
}

function renderStateBadge() {
  const badge = $('stateBadge');
  const word  = $('stateWord');
  const img   = $('previewImg');
  if (!badge) return;

  let cls, text;
  if (!store.wsConnected) {
    cls = 'is-nosignal'; text = 'NO SIGNAL';
  } else if (isLive() && store.previewStale) {
    // The pipeline claims live but frames stopped arriving — say so rather
    // than let a frozen picture read as healthy.
    cls = 'is-stale'; text = 'STALE';
  } else if (isLive()) {
    cls = 'is-live'; text = 'LIVE';
  } else if (!store.capturing) {
    cls = 'is-paused'; text = 'PAUSED';
  } else {
    cls = 'is-nosignal'; text = 'NO SIGNAL';
  }

  badge.className = `state-badge ${cls}`;
  word.textContent = text;
  if (img) img.classList.toggle('is-stale', cls === 'is-stale');

  $('stateClock').textContent = isLive() ? formatClock(store.stats.uptime_seconds) : '00:00:00';
}

// ─────────────────────────────────────────────
// HUD
// ─────────────────────────────────────────────
function renderHud(s) {
  const live = isLive();
  const fps  = Number(s.fps) || 0;

  $('hudFps').textContent     = live && fps > 0 ? fps.toFixed(0) : '—';
  $('hudBitrate').textContent = ((Number(s.bitrate_kbps) || 0) / 1000).toFixed(1);

  const latency = Number(s.latency_ms) || 0;
  $('hudLatency').textContent = live && latency > 0 ? latency.toFixed(0) : '—';
  $('healthLatencyVal').textContent = live && latency > 0 ? `${latency.toFixed(0)} ms` : '—';

  const age = s.bytes_age_ms;
  $('hudFrameAge').textContent = live && age != null ? `frame age ${Math.round(age)} ms` : 'frame age —';

  const p95 = Number(s.frame_gap_p95_ms) || 0;
  $('hudPacing').textContent = live && p95 > 0 ? `pacing p95 ${p95.toFixed(1)} ms` : 'pacing p95 —';
}

// ─────────────────────────────────────────────
// Signal chain
// ─────────────────────────────────────────────
const NODE_ORDER = ['goggles', 'bulk', 'h264', 'pipeline', 'outputs'];
const NODE_TITLE = {
  goggles: 'goggles', bulk: 'bulk data', h264: 'H.264',
  pipeline: 'pipeline', outputs: 'outputs',
};

function computeChain(s) {
  const live = isLive();
  const capturing = store.capturing;
  const outputs = Array.isArray(s.outputs) ? s.outputs : [];

  // Goggles — the gadget enumerated at all.
  const goggles = s.usb_link
    ? { state: 'ok', detail: gogglesLabel() }
    : { state: capturing ? 'err' : 'off', detail: capturing ? 'not enumerated' : 'paused' };

  // Bulk data — bytes moving on the bulk endpoint. usb_data is a bare boolean,
  // so staleness comes from how long bytes_received has been unchanged.
  let bulk;
  if (!capturing) {
    bulk = { state: 'off', detail: 'paused' };
  } else if (!s.usb_data) {
    bulk = { state: 'err', detail: 'no data' };
  } else if (s.bytes_age_ms != null && s.bytes_age_ms > 3000) {
    bulk = { state: 'err', detail: 'stalled' };
  } else {
    const mbs = (Number(s.bitrate_kbps) || 0) / 8000;
    bulk = { state: 'ok', detail: mbs > 0 ? `${mbs.toFixed(1)} MB/s` : 'flowing' };
  }

  // H.264 — valid video confirmed.
  const h264 = !capturing
    ? { state: 'off', detail: 'standby' }
    : s.usb_stream
      ? { state: 'ok', detail: 'locked' }
      : { state: bulk.state === 'err' ? 'off' : 'err', detail: 'no frames' };

  // Pipeline — encoding, and how well.
  let pipeline;
  if (!live) {
    pipeline = { state: 'off', detail: capturing ? 'standby' : 'standby card' };
  } else if (s.thermal_state === 'CRITICAL') {
    pipeline = { state: 'err', detail: 'throttled' };
  } else {
    const sev = pacingSeverity(Number(s.frame_gap_p95_ms) || 0, Number(s.fps) || 0);
    if (sev === 'err')       pipeline = { state: 'err',  detail: 'stalling' };
    else if (sev === 'warn') pipeline = { state: 'warn', detail: 'jittery' };
    else                     pipeline = { state: 'ok',   detail: 'encoding' };
  }

  // Outputs — how many enabled destinations are actually up.
  const enabled = outputs.filter((o) => o.enabled);
  let outs;
  if (enabled.length === 0) {
    outs = { state: 'off', detail: 'none enabled' };
  } else {
    const up = enabled.filter((o) => o.state === 'up').length;
    const failed = enabled.filter((o) => o.state === 'failed').length;
    const detail = `${up} of ${enabled.length} up`;
    if (up === enabled.length)  outs = { state: 'ok',   detail };
    else if (failed > 0 && up === 0) outs = { state: 'err', detail };
    else if (!live)             outs = { state: 'off',  detail: 'idle' };
    else                        outs = { state: 'warn', detail };
  }

  return { goggles, bulk, h264, pipeline, outputs: outs };
}

function renderChain(s) {
  const chain = computeChain(s);
  const root  = $('chain');
  if (!root) return;

  const nodes = root.querySelectorAll('.chain-node');
  const links = root.querySelectorAll('.chain-link');

  NODE_ORDER.forEach((key, i) => {
    const node = nodes[i];
    if (!node) return;
    const st = chain[key];
    node.className = `chain-node is-${st.state}`;
    node.querySelector('.chain-detail').textContent = st.detail;

    // The connector takes the upstream node's colour. There are four links for
    // five nodes — the last node has none, so no dash hangs off the right edge.
    if (i < links.length) links[i].className = `chain-link is-${st.state}`;
  });

  renderVerdict(chain);
}

/**
 * One verdict for the whole chain: the worst severity present, naming the
 * first node at that severity. The prototype could show a red node under an
 * amber "degraded at outputs" — the fault has to be named where it is.
 */
function renderVerdict(chain) {
  const el = $('chainVerdict');
  if (!el) return;

  const states = NODE_ORDER.map((k) => ({ key: k, ...chain[k] }));
  const firstErr  = states.find((n) => n.state === 'err');
  const firstWarn = states.find((n) => n.state === 'warn');
  const anyOk     = states.some((n) => n.state === 'ok');

  let cls, text;
  if (firstErr) {
    cls = 'is-err';  text = `break at ${NODE_TITLE[firstErr.key]}`;
  } else if (firstWarn) {
    cls = 'is-warn'; text = `degraded at ${NODE_TITLE[firstWarn.key]}`;
  } else if (!store.capturing) {
    cls = 'is-off';  text = 'standby';
  } else if (anyOk && states.every((n) => n.state === 'ok')) {
    cls = 'is-ok';   text = 'all stages healthy';
  } else {
    cls = 'is-off';  text = 'standby';
  }

  el.className = `chain-verdict ${cls}`;
  el.textContent = text;
}

function gogglesLabel() {
  const model = store.config?.goggles_model || 'auto';
  const names = { v1v2: 'V1/V2', goggles2: 'G2', goggles3: 'G3', auto: 'auto' };
  return `${names[model] || model} · USB`;
}

// ─────────────────────────────────────────────
// Traces
// ─────────────────────────────────────────────
const TRACE_COLOR = {
  ok: '#22d3ee', warn: '#fbbf24', err: '#f87171', off: '#4d5666',
};

/** y = 31 - clamp((v-lo)/(hi-lo), 0, 1) * 27, x evenly across 0–200. */
function polyPoints(values, lo, hi) {
  if (!values.length) return '';
  if (values.length === 1) values = [values[0], values[0]];
  const step = 200 / (values.length - 1);
  return values
    .map((v, i) => {
      const t = Math.min(1, Math.max(0, (v - lo) / (hi - lo)));
      return `${(i * step).toFixed(1)},${(31 - t * 27).toFixed(1)}`;
    })
    .join(' ');
}

function renderTraces(s) {
  const h = store.history;

  const bitrate = h.bitrate;
  $('traceBitrate').setAttribute('points', polyPoints(bitrate, 0, 52));
  $('traceBitrate').setAttribute('stroke', TRACE_COLOR.ok);
  $('traceBitrateNow').textContent = bitrate.length ? bitrate[bitrate.length - 1].toFixed(1) : '—';
  $('traceBitrateNow').style.color = TRACE_COLOR.ok;

  const pacing = h.pacing;
  const sev = pacingSeverity(Number(s.frame_gap_p95_ms) || 0, Number(s.fps) || 0);
  const pacingColor = TRACE_COLOR[sev] || TRACE_COLOR.off;
  $('tracePacing').setAttribute('points', polyPoints(pacing, 0, 40));
  $('tracePacing').setAttribute('stroke', pacingColor);
  $('tracePacingNow').textContent = pacing.length && pacing[pacing.length - 1] > 0
    ? pacing[pacing.length - 1].toFixed(1) : '—';
  $('tracePacingNow').style.color = pacingColor;

  const dropped = h.dropped;
  const anyDropped = dropped.some((v) => v > 0);
  const dropColor = anyDropped ? TRACE_COLOR.warn : TRACE_COLOR.off;
  $('traceDropped').setAttribute('points', polyPoints(dropped, 0, 8));
  $('traceDropped').setAttribute('stroke', dropColor);
  $('traceDroppedNow').textContent = dropped.length ? String(dropped[dropped.length - 1]) : '0';
  $('traceDroppedNow').style.color = dropColor;
}

// ─────────────────────────────────────────────
// Destinations (shared with the Outputs tab's LIVE STATUS card)
// ─────────────────────────────────────────────
const DEST_LABEL = { srt: 'SRT', rtmp: 'RTMP', ndi: 'NDI' };

const DEST_RENDER = {
  off:        { row: 'is-disabled', sub: 'disabled',    val: ''           },
  idle:       { row: 'is-idle',     sub: null,          val: 'idle'       },
  up:         { row: 'is-up',       sub: null,          val: 'up'         },
  connecting: { row: 'is-warn',     sub: null,          val: 'connecting' },
  retrying:   { row: 'is-warn',     sub: null,          val: 'retry'      },
  failed:     { row: 'is-err',      sub: null,          val: 'failed'     },
};

export function renderDestinations(containerId, outputs) {
  const el = $(containerId);
  if (!el) return;

  const list = Array.isArray(outputs) ? outputs : [];
  if (!list.length) {
    el.innerHTML = '<div class="dest-sub">No outputs configured.</div>';
    return;
  }

  el.replaceChildren(...list.map((o) => {
    const spec = DEST_RENDER[o.state] || DEST_RENDER.idle;

    const row = document.createElement('div');
    row.className = `dest-row ${spec.row}`;

    const dot = document.createElement('span');
    dot.className = 'dest-dot';

    const mid = document.createElement('div');
    mid.className = 'dest-mid';

    const name = document.createElement('div');
    name.className = 'dest-name';
    name.textContent = DEST_LABEL[o.id] || o.id;

    const sub = document.createElement('div');
    sub.className = 'dest-sub';
    let subText = spec.sub !== null ? spec.sub : (o.detail || '');
    if (o.state === 'retrying' && o.retry) subText = `${o.detail || 'retrying'} · retry ${o.retry}`;
    sub.textContent = subText;

    mid.append(name, sub);

    const val = document.createElement('div');
    val.className = 'dest-val';
    if (o.state === 'up' && o.bitrate_kbps) {
      val.textContent = `${(o.bitrate_kbps / 1000).toFixed(1)}M`;
    } else {
      val.textContent = spec.val;
    }

    row.append(dot, mid, val);
    return row;
  }));
}

// ─────────────────────────────────────────────
// Box health
// ─────────────────────────────────────────────
function renderHealth(s) {
  const temp = s.soc_temp;
  if (temp != null) {
    $('healthTempVal').textContent = `${temp} °C`;
    const fill = $('healthTempFill');
    fill.style.width = `${Math.min(100, Math.max(0, temp))}%`;
    // Bands are the meter's own; the banner keys off thermal_state so the two
    // never disagree about whether the box is actually in trouble.
    fill.style.background = temp > 82 ? 'var(--err)' : temp > 65 ? 'var(--warn)' : 'var(--ok)';
  } else {
    $('healthTempVal').textContent = '—';
    $('healthTempFill').style.width = '0';
  }

  const free = s.storage_free_gb;
  const total = s.storage_total_gb;
  if (free != null) {
    $('healthStoreVal').textContent = total ? `${free} GB` : `${free} GB`;
    const usedPct = total ? Math.min(100, Math.max(0, ((total - free) / total) * 100)) : 0;
    $('healthStoreFill').style.width = `${usedPct}%`;
    $('healthStoreFill').style.background = s.storage_state === 'CRITICAL'
      ? 'var(--err)' : s.storage_state === 'WARN' ? 'var(--warn)' : 'var(--ok)';
  } else {
    $('healthStoreVal').textContent = '—';
    $('healthStoreFill').style.width = '0';
  }

  // Bytes received and resolution are reference facts, not glanceable health —
  // demoted here from the tiles they used to occupy.
  $('healthSession').textContent = `session ${formatBytes(s.bytes_received)}`;
  $('healthResolution').textContent = s.resolution && s.resolution !== '—' ? s.resolution : '—';
}

// ─────────────────────────────────────────────
// Action button — hold to confirm on pause only
// ─────────────────────────────────────────────
const HOLD_MS      = 500;
const HOLD_TICK_MS = 16;

let holdTimer = null;
let holdStart = 0;
let holdFired = false;

function renderAction() {
  const btn = $('actionBtn');
  if (!btn) return;

  const capturing = store.capturing;
  btn.classList.toggle('is-paused', !capturing);
  $('actionLabel').textContent = capturing ? 'PAUSE CAPTURE' : 'RESUME CAPTURE';
  $('actionHint').textContent  = capturing ? 'hold to confirm while live' : 'starts immediately';
  btn.setAttribute('aria-label', capturing ? 'Pause capture' : 'Resume capture');

  const auto = store.config?.auto_connect !== false;
  $('actionCaption').textContent = auto
    ? 'Auto-connect on · resumes when goggles enumerate'
    : 'Auto-connect off · capture starts only from here';
}

function resetHold() {
  if (holdTimer) clearInterval(holdTimer);
  holdTimer = null;
  holdStart = 0;
  $('actionProgress').style.width = '0';
}

async function fireCapture(enabled) {
  const btn = $('actionBtn');
  btn.disabled = true;
  try {
    await setCapture(enabled);
    renderAction();
  } catch (err) {
    pushLog(`[ERROR] Capture ${enabled ? 'resume' : 'pause'} failed: ${err.message}`);
  } finally {
    btn.disabled = false;
    resetHold();
  }
}

/**
 * Progress is computed from elapsed wall time rather than by counting ticks.
 * Timer callbacks get throttled (power saving, slow devices, background tabs),
 * and a tick-counted hold would then silently demand several seconds of the
 * operator instead of the half second it promises — throttling now costs
 * smoothness, not correctness.
 *
 * A timer rather than requestAnimationFrame, because rAF is suspended entirely
 * while the page is not visible and would leave a started hold frozen.
 */
function startHold() {
  if (!store.capturing || holdTimer) return;
  holdFired = false;
  holdStart = performance.now();

  holdTimer = setInterval(() => {
    const pct = Math.min(100, ((performance.now() - holdStart) / HOLD_MS) * 100);
    $('actionProgress').style.width = `${pct}%`;
    if (pct >= 100) {
      holdFired = true;
      resetHold();
      fireCapture(false);
    }
  }, HOLD_TICK_MS);
}

function bindAction() {
  const btn = $('actionBtn');
  if (!btn) return;

  btn.addEventListener('pointerdown', (e) => {
    if (e.button !== 0) return;
    if (store.capturing) { e.preventDefault(); startHold(); }
  });

  ['pointerup', 'pointerleave', 'pointercancel'].forEach((evt) => {
    btn.addEventListener(evt, () => { if (!holdFired) resetHold(); });
  });

  btn.addEventListener('click', (e) => {
    // detail === 0 means keyboard activation. Keyboard users get a confirm
    // dialog rather than being asked to hold a key down.
    const keyboard = e.detail === 0;

    if (!store.capturing) { fireCapture(true); return; }

    if (keyboard) {
      if (confirm('Pause the live stream?')) fireCapture(false);
    }
    // Pointer clicks are handled entirely by the hold gesture.
  });
}

// ─────────────────────────────────────────────
// Recent log peek
// ─────────────────────────────────────────────
const RECENT_LINE_H = 20;   // 10px/1.4 line + 6px gap
const RECENT_MIN    = 4;
const RECENT_MAX    = 14;

/**
 * The card stretches to fill whatever the left column leaves over, so the line
 * count follows the space rather than being fixed at four. Measuring the list
 * is safe despite it holding the lines: it is a flex:1 child with
 * overflow:hidden, so its height comes from the column, not from its content.
 */
function recentCapacity(el) {
  const h = el.clientHeight;
  if (!h) return RECENT_MIN;
  return Math.max(RECENT_MIN, Math.min(RECENT_MAX, Math.floor(h / RECENT_LINE_H)));
}

function renderRecent() {
  const el = $('recentList');
  if (!el) return;

  const lines = store.logs.slice(0, recentCapacity(el));
  if (!lines.length) {
    el.innerHTML = '<div class="recent-line lv-debug">No log output yet.</div>';
    return;
  }

  el.replaceChildren(...lines.map((l) => {
    const div = document.createElement('div');
    div.className = `recent-line lv-${l.level}`;
    div.textContent = `${l.ts}  ${l.msg}${l.repeat > 1 ? `  ×${l.repeat}` : ''}`;
    return div;
  }));
}

// ─────────────────────────────────────────────
// Wiring
// ─────────────────────────────────────────────
export function initMonitor() {
  bindAction();
  renderAction();
  renderStateBadge();
  renderRecent();
  renderDestinations('destList', []);

  on('stats', (s) => {
    renderHud(s);
    renderChain(s);
    renderTraces(s);
    renderHealth(s);
    renderDestinations('destList', s.outputs);
    renderStateBadge();
    renderAction();
  });

  on('preview', renderPreview);
  on('preview-stale', renderStateBadge);
  on('ws', renderStateBadge);
  on('capture', renderAction);
  on('config', renderAction);
  on('logs', renderRecent);
}
