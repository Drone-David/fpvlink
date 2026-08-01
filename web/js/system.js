/* ═══════════════════════════════════════════════════════════
   FPVLink — System view
   Set once, then forget. Plus support.

   The two config controls here (goggles model, auto-connect) are bound by
   outputs.js, which owns the shared config draft.
═══════════════════════════════════════════════════════════ */

'use strict';

import { $, store, on, loadSystem, formatBytes, formatDuration, pushLog } from './state.js';
import { downloadDiagnostics } from './logs.js';

function row(key, value) {
  const div = document.createElement('div');
  div.className = 'kv-row';

  const k = document.createElement('span');
  k.className = 'kv-key';
  k.textContent = key;

  const v = document.createElement('span');
  v.className = 'kv-val';
  v.textContent = value ?? '—';

  div.append(k, v);
  return div;
}

function renderDevice() {
  const el = $('deviceList');
  if (!el) return;

  const sys = store.system || {};
  const s = store.stats || {};

  const storage = sys.storage_free_gb != null
    ? (sys.storage_total_gb ? `${sys.storage_free_gb} GB of ${sys.storage_total_gb} GB` : `${sys.storage_free_gb} GB`)
    : '—';

  el.replaceChildren(
    row('Host',            sys.host),
    row('Firmware',        sys.firmware_version ? `v${sys.firmware_version}` : null),
    row('Kernel',          sys.kernel),
    row('Uptime',          sys.host_uptime_seconds != null ? formatDuration(sys.host_uptime_seconds) : null),
    row('USB device',      sys.usb_device),
    row('Source format',   sys.source_format),
    row('Session recorded', s.bytes_received ? formatBytes(s.bytes_received) : formatBytes(sys.session_bytes)),
    row('Storage free',    storage),
    row('SOC temp',        s.soc_temp != null ? `${s.soc_temp} °C` : (sys.soc_temp != null ? `${sys.soc_temp} °C` : null)),
  );
}

export function initSystem() {
  $('diagBtn2').addEventListener('click', (e) => downloadDiagnostics(e.currentTarget));

  on('system', renderDevice);
  on('stats', renderDevice);

  renderDevice();

  loadSystem().catch((err) => pushLog(`[ERROR] Failed to read system info: ${err.message}`));

  // Facts that drift (uptime, temp, session bytes) — refreshed on a slow timer
  // rather than riding the 2 Hz stats broadcast.
  setInterval(() => {
    loadSystem().catch(() => { /* transient; the next tick retries */ });
  }, 30000);
}
