/* ═══════════════════════════════════════════════════════════
   FPVLink — Logs view
   Confirm and triage. Newest first, filtered by severity.
═══════════════════════════════════════════════════════════ */

'use strict';

import { $, store, on, API, clearLogs, logCounts, pushLog } from './state.js';

const LEVEL_TAG = { err: 'ERR', warn: 'WARN', ok: 'OK', info: 'INFO', debug: 'DEBUG' };

// Which severities are shown. Empty means "all" — the ALL chip clears filters.
const filters = new Set();
let wrap = false;

function bucketOf(level) {
  if (level === 'err' || level === 'warn') return level;
  return 'info';   // INFO buckets INFO, OK and DEBUG together
}

function visibleLogs() {
  if (filters.size === 0) return store.logs;
  return store.logs.filter((l) => filters.has(bucketOf(l.level)));
}

function renderChips() {
  const c = logCounts();
  $('chipAll').textContent  = `ALL ${c.all}`;
  $('chipErr').textContent  = `ERR ${c.err}`;
  $('chipWarn').textContent = `WARN ${c.warn}`;
  $('chipInfo').textContent = `INFO ${c.info}`;

  $('chipAll').classList.toggle('is-on', filters.size === 0);
  ['err', 'warn', 'info'].forEach((lvl) => {
    $(`chip${lvl[0].toUpperCase()}${lvl.slice(1)}`).classList.toggle('is-on', filters.has(lvl));
  });
}

function renderBody() {
  const body = $('logBody');
  if (!body) return;

  const rows = visibleLogs();
  body.classList.toggle('is-wrap', wrap);

  if (!rows.length) {
    body.replaceChildren(Object.assign(document.createElement('div'), {
      className: 'log-empty',
      textContent: store.logs.length ? 'No lines match the current filter.' : 'Waiting for log output…',
    }));
  } else {
    body.replaceChildren(...rows.map((l) => {
      const row = document.createElement('div');
      row.className = 'log-row';

      const ts = document.createElement('span');
      ts.className = 'log-ts';
      ts.textContent = l.ts;

      const lvl = document.createElement('span');
      lvl.className = `log-lvl lvl-${l.level}`;
      lvl.textContent = LEVEL_TAG[l.level] || 'INFO';

      const msg = document.createElement('span');
      msg.className = `log-msg lv-${l.level}`;
      msg.textContent = l.repeat > 1 ? `${l.msg}  ×${l.repeat}` : l.msg;

      row.append(ts, lvl, msg);
      return row;
    }));
  }

  $('logCount').textContent = `${rows.length} of ${store.logs.length} lines shown`;
}

function render() {
  renderChips();
  renderBody();
}

async function copyLogs() {
  const btn = $('copyBtn');
  const text = visibleLogs()
    .map((l) => `${l.ts} ${LEVEL_TAG[l.level]} ${l.msg}${l.repeat > 1 ? ` ×${l.repeat}` : ''}`)
    .join('\n');

  try {
    await navigator.clipboard.writeText(text);
    btn.textContent = 'Copied';
  } catch {
    btn.textContent = 'Copy failed';
  }
  setTimeout(() => { btn.textContent = 'Copy'; }, 1600);
}

/**
 * Diagnostics bundle — the only way a user with no internet can hand support
 * something useful. Shared by the Logs toolbar and the System tab.
 */
export async function downloadDiagnostics(btn) {
  const original = btn.textContent;
  btn.disabled = true;
  btn.textContent = 'Collecting…';

  try {
    const res = await fetch(API('/diagnostics'));
    if (!res.ok) {
      const body = await res.json().catch(() => ({}));
      throw new Error(body.error || `HTTP ${res.status}`);
    }
    const blob = await res.blob();
    const disposition = res.headers.get('Content-Disposition') || '';
    const match = disposition.match(/filename="([^"]+)"/);
    const filename = match ? match[1] : 'fpvlink-diagnostics.tar.gz';

    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = filename;
    document.body.appendChild(a);
    a.click();
    a.remove();
    URL.revokeObjectURL(url);

    btn.textContent = `${filename} ✓`;
    pushLog(`[OK] Diagnostics bundle downloaded: ${filename}`);
    setTimeout(() => { btn.textContent = original; }, 2600);
  } catch (err) {
    pushLog(`[ERROR] Diagnostics download failed: ${err.message}`);
    btn.textContent = original;
  } finally {
    btn.disabled = false;
  }
}

export function initLogs() {
  $('chipAll').addEventListener('click', () => { filters.clear(); render(); });

  ['err', 'warn', 'info'].forEach((lvl) => {
    const id = `chip${lvl[0].toUpperCase()}${lvl.slice(1)}`;
    $(id).addEventListener('click', () => {
      if (filters.has(lvl)) filters.delete(lvl);
      else filters.add(lvl);
      render();
    });
  });

  $('wrapBtn').addEventListener('click', () => {
    wrap = !wrap;
    $('wrapBtn').classList.toggle('is-on', wrap);
    $('wrapBtn').setAttribute('aria-pressed', String(wrap));
    renderBody();
  });

  $('copyBtn').addEventListener('click', copyLogs);

  $('clearBtn').addEventListener('click', () => {
    clearLogs();
    pushLog('[INFO] Console cleared');
  });

  $('diagBtn').addEventListener('click', (e) => downloadDiagnostics(e.currentTarget));

  on('logs', render);
  render();
}
