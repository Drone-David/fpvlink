/* ═══════════════════════════════════════════════════════════
   FPVLink — shared client state
   WebSocket transport, stores, ring buffers, and the log model.
   Views subscribe with on(event, fn); nothing here touches the DOM.
═══════════════════════════════════════════════════════════ */

'use strict';

export const $   = (id) => document.getElementById(id);
export const API = (path) => `/api${path}`;

const RECONNECT_MS = 3000;

// The server holds 500 lines; matching it here makes the severity filters
// useful. The old client kept 100, which a single respawn loop could flush.
const LOG_MAX = 500;

const HISTORY_LEN = 30;

// The pipeline posts stats every 2s (report_status_loop in capture/pipeline.py)
// but server.js rebroadcasts every 500ms, so three of every four messages are
// duplicates. Sampling per message would make the "60s" traces cover 15s.
const STATS_PER_SAMPLE = 4;

export const store = {
  stats:        {},
  config:       null,      // as last fetched from the device
  configDraft:  null,      // what the form currently holds
  luts:         [],
  logs:         [],        // newest first: { ts, level, msg, repeat, key }
  system:       null,
  capturing:    false,
  wsConnected:  false,
  lastPreviewAt: 0,
  previewStale:  false,
  history: { bitrate: [], pacing: [], dropped: [] },
};

// ─────────────────────────────────────────────
// Tiny pub/sub
// ─────────────────────────────────────────────
const listeners = new Map();

export function on(evt, fn) {
  if (!listeners.has(evt)) listeners.set(evt, new Set());
  listeners.get(evt).add(fn);
  return () => listeners.get(evt).delete(fn);
}

export function emit(evt, payload) {
  const set = listeners.get(evt);
  if (!set) return;
  for (const fn of set) {
    try { fn(payload); } catch (err) { console.error(`[${evt}]`, err); }
  }
}

// ─────────────────────────────────────────────
// Derived helpers
// ─────────────────────────────────────────────

/** The one liveness test. `stats.streaming` was never sent by the server. */
export function isLive() {
  return store.stats.pipeline_status === 'live';
}

export function formatClock(seconds) {
  const s = Math.max(0, Math.floor(Number(seconds) || 0));
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  return [h, m, s % 60].map((v) => String(v).padStart(2, '0')).join(':');
}

export function formatBytes(bytes) {
  const b = Number(bytes) || 0;
  if (b >= 1e9) return `${(b / 1e9).toFixed(2)} GB`;
  if (b >= 1e6) return `${(b / 1e6).toFixed(1)} MB`;
  return `${(b / 1e3).toFixed(0)} KB`;
}

export function formatDuration(seconds) {
  const s = Math.max(0, Math.floor(Number(seconds) || 0));
  const d = Math.floor(s / 86400);
  const h = Math.floor((s % 86400) / 3600);
  const m = Math.floor((s % 3600) / 60);
  if (d > 0) return `${d}d ${String(h).padStart(2, '0')}:${String(m).padStart(2, '0')}`;
  return `${String(h).padStart(2, '0')}:${String(m).padStart(2, '0')}`;
}

/**
 * Pacing thresholds are relative to the ideal inter-frame gap (1000/fps), not
 * a fixed millisecond count — source framerate varies by goggles model and
 * resolution, so a hardcoded 21/25 ms would be wrong at anything but 60 fps.
 */
export function pacingSeverity(p95, fps) {
  if (!(p95 > 0) || !(fps > 0)) return 'off';
  const ideal = 1000 / fps;
  if (p95 > ideal * 1.5)  return 'err';
  if (p95 > ideal * 1.25) return 'warn';
  return 'ok';
}

// ─────────────────────────────────────────────
// Log model
// ─────────────────────────────────────────────

/**
 * Content-based severity detection, carried over verbatim — it deliberately
 * outranks the level tag, because server.js wraps ALL child-process output in
 * [INFO]. A real failure like "sudo: a password is required" arrives tagged
 * [INFO], and keying off the tag painted those failures success-green in the
 * one console a user relies on to find out why capture broke.
 */
export function detectLogLevel(line) {
  const lower = line.toLowerCase();
  if (/\[error\]|exception|traceback|critical|\bfailed\b|\berror\b|password is required|permission denied|not permitted|no such|cannot |can't /.test(lower)) return 'err';
  if (/\[warn\]|warning|respawn|retry/.test(lower)) return 'warn';
  if (/\[ok\]|started|connected|saved|success|listening/.test(lower)) return 'ok';
  if (/\[debug\]/.test(lower)) return 'debug';
  return 'info';
}

const ISO_PREFIX = /^\[(\d{4}-\d{2}-\d{2}T[\d:.]+Z)\]\s*/;

// The level moves into its own column, so strip the tag from the message body.
// Covers OK/ERR too: those are only ever emitted by this client's own pushLog
// calls, but they would otherwise show up doubled next to the level chip.
const LEVEL_TAG = /^\[(INFO|WARN|WARNING|ERROR|ERR|DEBUG|OK)\]\s*/i;

/** Strip the timestamp so lines differing only by time collapse together. */
function messageKey(line) {
  return line.replace(ISO_PREFIX, '');
}

function parseLine(raw) {
  const line = String(raw);
  const level = detectLogLevel(line);

  let rest = line;
  let ts = null;

  const iso = rest.match(ISO_PREFIX);
  if (iso) {
    const d = new Date(iso[1]);
    if (!isNaN(d)) ts = d;
    rest = rest.slice(iso[0].length);
  }
  rest = rest.replace(LEVEL_TAG, '');

  const stamp = (ts || new Date()).toTimeString().slice(0, 8);
  return { ts: stamp, level, msg: rest, repeat: 1, key: messageKey(line) };
}

/**
 * Append a line, collapsing an unbroken run of the same message into one entry
 * with a ×N count. Without this a stuck failure loop floods — and silently
 * evicts — the whole buffer. The list is newest-first, so the run being
 * collapsed is at index 0.
 */
export function pushLog(raw) {
  const entry = parseLine(raw);
  const head = store.logs[0];

  if (head && head.key === entry.key) {
    head.repeat += 1;
    head.ts = entry.ts;
    emit('logs');
    return;
  }

  store.logs.unshift(entry);
  if (store.logs.length > LOG_MAX) store.logs.length = LOG_MAX;
  emit('logs');
}

export function clearLogs() {
  store.logs.length = 0;
  emit('logs');
}

export function logCounts() {
  const c = { all: 0, err: 0, warn: 0, info: 0 };
  for (const l of store.logs) {
    c.all += 1;
    if (l.level === 'err') c.err += 1;
    else if (l.level === 'warn') c.warn += 1;
    else c.info += 1;   // info buckets INFO, OK and DEBUG together
  }
  return c;
}

// ─────────────────────────────────────────────
// Ring buffers for the traces
// ─────────────────────────────────────────────
let statsTick = 0;

function pushSample(buf, value) {
  buf.push(value);
  if (buf.length > HISTORY_LEN) buf.shift();
}

function sampleHistory(s) {
  statsTick += 1;
  if (statsTick % STATS_PER_SAMPLE !== 0) return;
  pushSample(store.history.bitrate, (Number(s.bitrate_kbps) || 0) / 1000);
  pushSample(store.history.pacing,  Number(s.frame_gap_p95_ms) || 0);
  pushSample(store.history.dropped, Number(s.dropped_frames) || 0);
}

// ─────────────────────────────────────────────
// WebSocket
// ─────────────────────────────────────────────
let ws = null;
let retryTimer = null;

export function connectWS() {
  if (ws && (ws.readyState === WebSocket.OPEN || ws.readyState === WebSocket.CONNECTING)) return;

  const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
  ws = new WebSocket(`${proto}//${location.host}/ws`);

  ws.addEventListener('open', () => {
    store.wsConnected = true;
    clearTimeout(retryTimer);
    emit('ws');
  });

  ws.addEventListener('message', (event) => {
    let msg;
    try { msg = JSON.parse(event.data); } catch { return; }

    if (msg.type === 'stats') {
      store.stats = msg;
      if (typeof msg.capture_enabled === 'boolean') store.capturing = msg.capture_enabled;
      sampleHistory(msg);
      emit('stats', msg);
    } else if (msg.type === 'log') {
      pushLog(msg.line);
    } else if (msg.type === 'log_batch') {
      // Batches arrive oldest-first; push in order so the newest ends up at the
      // head, and so run-collapsing sees the same sequence the server emitted.
      (msg.lines || []).forEach(pushLog);
    } else if (msg.type === 'preview') {
      store.lastPreviewAt = Date.now();
      emit('preview', msg);
    }
  });

  ws.addEventListener('close', () => {
    store.wsConnected = false;
    emit('ws');
    clearTimeout(retryTimer);
    retryTimer = setTimeout(connectWS, RECONNECT_MS);
  });

  ws.addEventListener('error', () => ws.close());
}

// ─────────────────────────────────────────────
// REST
// ─────────────────────────────────────────────
async function getJSON(path) {
  const res = await fetch(API(path));
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  return res.json();
}

export async function loadConfig() {
  const cfg = await getJSON('/config');
  store.config = cfg;
  store.configDraft = { ...cfg };
  emit('config');
  return cfg;
}

export async function loadLuts() {
  store.luts = await getJSON('/luts');
  emit('luts');
  return store.luts;
}

export async function loadSystem() {
  store.system = await getJSON('/system');
  emit('system');
  return store.system;
}

export async function loadStatus() {
  const stat = await getJSON('/status');
  store.capturing = !!stat.capture_enabled;
  if (stat.pipeline_status) store.stats.pipeline_status = stat.pipeline_status;
  emit('status', stat);
  return stat;
}

export async function loadInitialLogs() {
  try {
    const body = await getJSON('/logs');
    (body.lines || []).forEach(pushLog);
  } catch { /* the WebSocket will deliver them instead */ }
}

export async function saveConfig(payload) {
  const res = await fetch(API('/config'), {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  });
  const body = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error((body.details || [body.error || `HTTP ${res.status}`]).join(', '));
  store.config = body.config || payload;
  store.configDraft = { ...store.config };
  emit('config');
  return body;
}

export async function setCapture(enabled) {
  const res = await fetch(API(enabled ? '/capture/enable' : '/capture/disable'), { method: 'POST' });
  const body = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(body.error || `HTTP ${res.status}`);
  store.capturing = enabled;
  emit('capture');
  return body;
}

// ─────────────────────────────────────────────
// Preview staleness watchdog
// A frozen last frame must never imply everything is fine — that false
// confidence is the exact trap this feature exists to avoid.
// ─────────────────────────────────────────────
setInterval(() => {
  if (store.lastPreviewAt === 0) return;
  const stale = Date.now() - store.lastPreviewAt > 2000;
  if (stale !== store.previewStale) {
    store.previewStale = stale;
    emit('preview-stale');
  }
}, 500);
