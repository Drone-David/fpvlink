/**
 * FPVLink Web Server
 * Express + WebSocket server for FPVLink streaming device management.
 * Serves the web UI and provides REST + WebSocket APIs for config,
 * status, and real-time stats.
 */

'use strict';

const express        = require('express');
const http           = require('http');
const WebSocket      = require('ws');
const path           = require('path');
const fs             = require('fs');
const { spawn, execFile } = require('child_process');
const readline       = require('readline');
const dgram          = require('dgram');
const multer         = require('multer');

// ─────────────────────────────────────────────
// Paths
// ─────────────────────────────────────────────
const ROOT_DIR    = path.resolve(__dirname, '..');
const SYSTEM_DIR  = path.join(ROOT_DIR, 'system');
const CONFIG_PATH = path.join(SYSTEM_DIR, 'config.json');
const WEB_DIR     = __dirname;

const LUTS_DIR = path.join(SYSTEM_DIR, 'luts');
const LUTS_MANIFEST = path.join(LUTS_DIR, 'manifest.json');
const LOGS_DIR = path.join(SYSTEM_DIR, 'logs', 'sessions');

// Ensure directories exist
if (!fs.existsSync(LUTS_DIR)) {
  fs.mkdirSync(LUTS_DIR, { recursive: true });
}
if (!fs.existsSync(LUTS_MANIFEST)) {
  fs.writeFileSync(LUTS_MANIFEST, '[]');
}
if (!fs.existsSync(LOGS_DIR)) {
  fs.mkdirSync(LOGS_DIR, { recursive: true });
}

function cleanupSessionLogs() {
  try {
    const files = fs.readdirSync(LOGS_DIR)
      .filter(f => f.startsWith('session_') && f.endsWith('.jsonl'))
      .sort(); // Lexicographical sort works for timestamp prefixes
    
    // Keep the last 50 sessions
    const MAX_SESSIONS = 50;
    if (files.length > MAX_SESSIONS) {
      const toDelete = files.slice(0, files.length - MAX_SESSIONS);
      for (const file of toDelete) {
        fs.unlinkSync(path.join(LOGS_DIR, file));
        logger.info(`Pruned old session log: ${file}`);
      }
    }
  } catch (e) {
    logger.error(`Failed to cleanup session logs: ${e.message}`);
  }
}

const storage = multer.diskStorage({
  destination: function (req, file, cb) {
    cb(null, LUTS_DIR)
  },
  filename: function (req, file, cb) {
    const ext = path.extname(file.originalname);
    cb(null, `lut_${Date.now()}_${Math.floor(Math.random()*1000)}${ext}`)
  }
})
const upload = multer({
  storage: storage,
  limits: { fileSize: 10 * 1024 * 1024 }, // 10MB limit
  fileFilter: (req, file, cb) => {
    if (path.extname(file.originalname).toLowerCase() === '.cube') {
      cb(null, true);
    } else {
      cb(new Error('Only .cube files are allowed'));
    }
  }
});

function readLutsManifest() {
  try {
    if (!fs.existsSync(LUTS_MANIFEST)) return [];
    return JSON.parse(fs.readFileSync(LUTS_MANIFEST, 'utf8'));
  } catch (err) {
    logger.error(`Failed to read LUTs manifest: ${err.message}`);
    return [];
  }
}

function writeLutsManifest(manifest) {
  fs.writeFileSync(LUTS_MANIFEST, JSON.stringify(manifest, null, 2), 'utf8');
}

const PORT = 8080;

// ─────────────────────────────────────────────
// Logger
// ─────────────────────────────────────────────
const LOG_BUFFER_MAX = 500;
const logBuffer = [];

function ts() {
  return new Date().toISOString();
}

function log(level, msg) {
  const line = `[${ts()}] [${level.toUpperCase()}] ${msg}`;
  console.log(line);
  logBuffer.push(line);
  if (logBuffer.length > LOG_BUFFER_MAX) logBuffer.shift();
  broadcastLog(line);
}

const logger = {
  info:  (m) => log('INFO',  m),
  warn:  (m) => log('WARN',  m),
  error: (m) => log('ERROR', m),
  debug: (m) => log('DEBUG', m),
};

// ─────────────────────────────────────────────
// Config helpers
// ─────────────────────────────────────────────
const CONFIG_DEFAULTS = {
  goggles_model:    'auto',    // 'auto' | 'v1v2' | 'goggles2'
  srt_enabled:      false,
  srt_url:          'srt://0.0.0.0:5000',
  rtmp_enabled:     false,
  rtmp_url:         'rtmp://live.twitch.tv/app',
  rtmp_key:         '',
  ndi_enabled:      false,
  ndi_name:         'FPVLink',
  hdmi_lut_enabled: false,
  hdmi_lut_active_id: '',
  bitrate_mbps:     50,
  device_name:      'FPVLink',
  firmware_version: '1.0.0',
  auto_connect:     true,
};

function readConfig() {
  try {
    if (!fs.existsSync(CONFIG_PATH)) {
      logger.warn(`config.json not found at ${CONFIG_PATH}, using defaults`);
      return { ...CONFIG_DEFAULTS };
    }
    const raw = fs.readFileSync(CONFIG_PATH, 'utf8');
    const parsed = JSON.parse(raw);
    const merged = { ...CONFIG_DEFAULTS, ...parsed };
    // Nested outputs.* is the source of truth the pipeline reads; project it
    // onto the flat keys the dashboard binds to so toggles reflect reality.
    const ndi = parsed.outputs && parsed.outputs.ndi;
    if (ndi) {
      if (typeof ndi.enabled === 'boolean') merged.ndi_enabled = ndi.enabled;
      if (typeof ndi.name === 'string')     merged.ndi_name    = ndi.name;
    }
    const srt = parsed.outputs && parsed.outputs.srt;
    if (srt) {
      if (typeof srt.enabled === 'boolean') merged.srt_enabled = srt.enabled;
      if (typeof srt.url === 'string')      merged.srt_url     = srt.url;
    }
    const rtmp = parsed.outputs && parsed.outputs.rtmp;
    if (rtmp) {
      if (typeof rtmp.enabled === 'boolean')   merged.rtmp_enabled = rtmp.enabled;
      if (typeof rtmp.url === 'string')        merged.rtmp_url     = rtmp.url;
      if (typeof rtmp.stream_key === 'string') merged.rtmp_key     = rtmp.stream_key;
    }
    return merged;
  } catch (err) {
    logger.error(`Failed to read config: ${err.message}`);
    return { ...CONFIG_DEFAULTS };
  }
}

function writeConfig(cfg) {
  if (!fs.existsSync(SYSTEM_DIR)) fs.mkdirSync(SYSTEM_DIR, { recursive: true });
  fs.writeFileSync(CONFIG_PATH, JSON.stringify(cfg, null, 2), 'utf8');
}

function validateConfig(body) {
  const errors = [];
  const GOGGLES_MODELS = ['auto', 'v1v2', 'goggles2', 'goggles3'];

  if (body.goggles_model !== undefined && !GOGGLES_MODELS.includes(body.goggles_model)) {
    errors.push(`goggles_model must be one of: ${GOGGLES_MODELS.join(', ')}`);
  }
  if (body.bitrate_mbps !== undefined) {
    const b = Number(body.bitrate_mbps);
    if (isNaN(b) || b < 2 || b > 50) errors.push('bitrate_mbps must be between 2 and 50');
  }
  if (body.srt_url !== undefined && typeof body.srt_url !== 'string') {
    errors.push('srt_url must be a string');
  }
  if (body.rtmp_url !== undefined && typeof body.rtmp_url !== 'string') {
    errors.push('rtmp_url must be a string');
  }
  if (body.rtmp_key !== undefined && typeof body.rtmp_key !== 'string') {
    errors.push('rtmp_key must be a string');
  }
  if (body.ndi_name !== undefined && typeof body.ndi_name !== 'string') {
    errors.push('ndi_name must be a string');
  }
  if (body.hdmi_lut_active_id !== undefined && typeof body.hdmi_lut_active_id !== 'string') {
    errors.push('hdmi_lut_active_id must be a string');
  }
  ['srt_enabled', 'rtmp_enabled', 'ndi_enabled', 'hdmi_lut_enabled', 'auto_connect'].forEach((k) => {
    if (body[k] !== undefined && typeof body[k] !== 'boolean') {
      errors.push(`${k} must be a boolean`);
    }
  });
  return errors;
}

// ─────────────────────────────────────────────
// Runtime state
// ─────────────────────────────────────────────
const state = {
  capture_enabled: false,
  pipeline_status: 'standby', // 'live', 'standby', 'offline'
  usb_status:     'disconnected',   // 'connected' | 'disconnected'
  fps:            0,
  bitrate_kbps:   0,
  latency_ms:     0,
  bytes_received: 0,
  dropped_frames: 0,
  resolution:     '—',
  startedAt:      null,
  // Hardware Telemetry
  soc_temp:              null,
  thermal_state:         'NORMAL',
  storage_free_gb:       null,
  storage_total_gb:      null,
  storage_usage_percent: null,
  storage_state:         'NORMAL',
};

let captureProcess  = null;
let pipelineProcess = null;
let standbyProcess  = null;
let sessionLogStream = null;
let sessionLogTick   = 0;
let currentSessionId = null;

function uptimeSeconds() {
  if (!state.startedAt) return 0;
  return Math.floor((Date.now() - state.startedAt) / 1000);
}

// ─────────────────────────────────────────────
// Child process management
// ─────────────────────────────────────────────
function captureScript(cfg) {
  const model = (cfg.goggles_model || 'auto').toLowerCase();
  if (model === 'v1v2') {
    return { script: path.join(ROOT_DIR, 'capture', 'v1v2.py'), args: ['--capture'] };
  }
  if (model === 'goggles2' || model === 'goggles3') {
    const scriptPath = path.join(__dirname, '../capture/goggles2.py');
    const args = ['--teardown', '--setup', '--stream', '--verbose'];
    return { script: scriptPath, args: args };
  }
  // auto: default to goggles2.py since Goggles 2 is most common
  return { script: path.join(ROOT_DIR, 'capture', 'goggles2.py'), args: ['--teardown', '--setup', '--stream', '--verbose'] };
}


let sessionCloseTimeout = null;
let pipelineHeartbeatTimeout = null;
let captureBackoffMs = 1000;
let captureSpawnTime = 0;
let captureLoopActive = false;

// Internal API Server for pipeline POSTs
const internalApp = express();
internalApp.use(express.json());
internalApp.post('/internal/status', (req, res) => {
  const body = req.body || {};
  if (['live', 'standby'].includes(body.status)) {
    handlePipelineStatusChange(body.status);
  }
  // Live stats reported by the pipeline (see report_status_loop in pipeline.py)
  if (typeof body.fps === 'number')            state.fps            = body.fps;
  if (typeof body.bitrate_kbps === 'number')   state.bitrate_kbps   = body.bitrate_kbps;
  if (typeof body.resolution === 'string')     state.resolution     = body.resolution;
  if (typeof body.bytes_received === 'number') state.bytes_received = body.bytes_received;
  if (typeof body.dropped_frames === 'number') state.dropped_frames = body.dropped_frames;
  res.json({ok:true});
});
internalApp.listen(8081, '127.0.0.1', () => {
  logger.info('Internal API listening on 127.0.0.1:8081');
});

function handlePipelineStatusChange(newStatus) {
  // Heartbeat watchdog
  if (pipelineHeartbeatTimeout) clearTimeout(pipelineHeartbeatTimeout);
  pipelineHeartbeatTimeout = setTimeout(() => {
    logger.warn('Pipeline heartbeat lost. Marking offline.');
    handlePipelineStatusChange('offline');
  }, 6000);

  if (state.pipeline_status === newStatus) return;
  const oldStatus = state.pipeline_status;
  state.pipeline_status = newStatus;
  logger.info(`Pipeline status changed: ${oldStatus} -> ${newStatus}`);

  if (newStatus === 'live') {
    if (sessionCloseTimeout) {
      clearTimeout(sessionCloseTimeout);
      sessionCloseTimeout = null;
    }
    if (!currentSessionId) {
      startSessionLog();
    }
  } else if (newStatus === 'standby' || newStatus === 'offline') {
    if (currentSessionId && !sessionCloseTimeout) {
      sessionCloseTimeout = setTimeout(() => {
        stopSessionLog();
        sessionCloseTimeout = null;
      }, 5000); // 5s debounce
    }
  }
}

function startSessionLog() {
  currentSessionId = Date.now() + '_' + Math.random().toString(36).slice(2, 7);
  const cfg = readConfig();
  delete cfg.rtmp_key;
  try {
    sessionLogStream = fs.createWriteStream(path.join(LOGS_DIR, `session_${currentSessionId}.jsonl`), { flags: 'a' });
    sessionLogStream.on('error', (err) => {
      logger.error(`Session log error: ${err.message}`);
      sessionLogStream = null;
    });
    sessionLogStream.write(JSON.stringify({ event: 'stream_started', timestamp: new Date().toISOString(), sessionId: currentSessionId, config: cfg }) + '\n');
    state.startedAt = Date.now();
  } catch (e) {
    logger.error(`Failed to start session logging: ${e.message}`);
  }
}

function stopSessionLog() {
  if (sessionLogStream) {
    try {
      sessionLogStream.write(JSON.stringify({
        event: 'stream_ended',
        timestamp: new Date().toISOString(),
        summary: {
          uptime_seconds: uptimeSeconds(),
          bytes_received: state.bytes_received,
          dropped_frames: state.dropped_frames
        }
      }) + '\n');
      sessionLogStream.end();
    } catch(e) {}
    sessionLogStream = null;
  }
  currentSessionId = null;
  state.startedAt = null;
  cleanupSessionLogs();
}

function spawnStandby(cfg) {
  if (!cfg.outputs?.hdmi?.enabled) return null;
  const connectorId = cfg.outputs?.hdmi?.connector_id || 217;
  logger.info(`Spawning standby pipeline on connector ${connectorId}`);
  
  const proc = spawn('gst-launch-1.0', [
    'videotestsrc', 'pattern=smpte',
    '!', 'video/x-raw,width=1920,height=1080',
    '!', 'videoconvert',
    '!', 'video/x-raw,format=BGRx',
    '!', 'kmssink', `connector-id=${connectorId}`, 'sync=false'
  ]);

  proc.on('error', (err) => logger.error(`Standby process error: ${err.message}`));
  proc.on('close', (code) => logger.info(`Standby process exited with code ${code}`));
  
  return proc;
}

async function killProcessAsync(proc) {
  if (!proc || proc.killed) return;
  // If already exited, it won't have a pid or will have exited
  if (proc.exitCode !== null || proc.signalCode !== null) return;

  return new Promise((resolve) => {
    let timeout;
    const onExit = () => {
      clearTimeout(timeout);
      resolve();
    };
    proc.once('exit', onExit);
    
    try {
      proc.kill('SIGTERM');
    } catch (e) {
      onExit();
      return;
    }

    timeout = setTimeout(() => {
      logger.warn(`Process ${proc.pid} did not exit after SIGTERM, escalating to SIGKILL`);
      try {
        proc.kill('SIGKILL');
      } catch (e) {}
    }, 3000);
  });
}

function parseStatLine(line) {
  let updated = false;

  if (line.includes('[STATS]')) {
    try {
      const jsonStr = line.substring(line.indexOf('{'));
      const stats = JSON.parse(jsonStr);
      // We only merge valid fields
      if (stats.fps !== undefined) state.fps = stats.fps;
      if (stats.bitrate_kbps !== undefined) state.bitrate_kbps = stats.bitrate_kbps;
      if (stats.latency_ms !== undefined) state.latency_ms = stats.latency_ms;
      if (stats.dropped_frames !== undefined) state.dropped_frames = stats.dropped_frames;
      if (stats.resolution !== undefined) state.resolution = stats.resolution;
      return true;
    } catch (e) {
      logger.error(`Failed to parse stats JSON: ${e.message}`);
      return false;
    }
  }

  const patterns = {
    fps:            /fps=([\d.]+)/,
    bitrate_kbps:   /bitrate_kbps=([\d.]+)/,
    latency_ms:     /latency_ms=([\d.]+)/,
    bytes_received: /bytes=([\d]+)/,
    usb_status:     /usb=(connected|disconnected)/,
  };

  for (const [key, re] of Object.entries(patterns)) {
    const m = line.match(re);
    if (m) {
      state[key] = key === 'usb_status' ? m[1] : parseFloat(m[1]);
      updated = true;
    }
  }
  return updated;
}

async function superviseCaptureLoop() {
  if (captureLoopActive) return;
  captureLoopActive = true;
  state.capture_enabled = true;
  captureBackoffMs = 1000;

  while (state.capture_enabled) {
    const cfg = readConfig();
    captureSpawnTime = Date.now();
    captureProcess = spawnCapture(cfg);
    
    await new Promise((resolve) => {
      captureProcess.on('close', resolve);
      captureProcess.on('error', resolve);
    });

    captureProcess = null;
    state.usb_status = 'disconnected';

    if (!state.capture_enabled) break;

    const runTime = Date.now() - captureSpawnTime;
    if (runTime > 10000) {
      captureBackoffMs = 1000; // Success reset
    } else {
      captureBackoffMs = Math.min(captureBackoffMs * 2, 8000);
    }
    logger.warn(`Capture exited. Respawning in ${captureBackoffMs}ms...`);
    await new Promise(r => setTimeout(r, captureBackoffMs));
  }
  captureLoopActive = false;
}

async function enableCapture() {
  if (state.capture_enabled) return;
  logger.info('Enabling capture supervision...');
  superviseCaptureLoop(); // async background
}

async function disableCapture() {
  if (!state.capture_enabled) return;
  logger.info('Disabling capture supervision...');
  state.capture_enabled = false;
  if (captureProcess) {
    await killProcessAsync(captureProcess);
    captureProcess = null;
  }
}

// Initial Boot Setup for Gadget
execFile('sudo', ['python3', path.join(ROOT_DIR, 'capture', 'goggles2.py'), '--setup', '--if-absent'], (error, stdout, stderr) => {
  if (error) {
    logger.error(`Boot gadget setup failed: ${error.message}`);
  } else {
    logger.info('Boot gadget setup checked/completed.');
  }
});

function spawnCapture(cfg) {
  const { script, args } = captureScript(cfg);
  logger.info(`Spawning capture process: sudo python3 ${script} ${args.join(' ')}`);

  const proc = spawn('sudo', ['python3', script, ...args], {
    env: { ...process.env, FPVLINK_CONFIG: CONFIG_PATH, HOME: '/home/fpvlink' },
    stdio: ['pipe', 'pipe', 'pipe'],
  });

  proc.stdout.on('data', (data) => {
    // Forward raw video data to pipeline stdin
    if (pipelineProcess && pipelineProcess.stdin && pipelineProcess.stdin.writable) {
      try {
        pipelineProcess.stdin.write(data);
      } catch (e) {
        // Ignore synchronous stream errors like EPIPE during shutdown
      }
    }
  });

  const captureStderr = readline.createInterface({ input: proc.stderr });
  captureStderr.on('line', (line) => {
    logger.info(`[capture] ${line}`);
    if (line.includes('[FPVLink] Goggles connected')) {
      state.usb_status = 'connected';
    } else if (line.includes('[FPVLink] Waiting for goggles USB connection')) {
      state.usb_status = 'disconnected';
    }

    // Parse basic stats for the dashboard UI
    if (line.includes('[STATS]')) {
      try {
        const jsonStr = line.substring(line.indexOf('{'));
        const stats = JSON.parse(jsonStr);
        if (stats.fps !== undefined) state.fps = stats.fps;
        if (stats.bitrate_kbps !== undefined) state.bitrate_kbps = stats.bitrate_kbps;
      } catch (e) {}
    }
  });

  proc.on('close', (code) => {
    logger.warn(`Capture process exited with code ${code}`);
    if (state.streaming) {
      state.usb_status = 'disconnected';
      state.streaming  = false;
    }
  });

  proc.on('error', (err) => {
    logger.error(`Capture process error: ${err.message}`);
  });

  return proc;
}


// (pipeline and standby are now managed by systemd)
const app = express();
app.use(express.json());
app.use(express.static(WEB_DIR));

// GET /api/status
app.get('/api/status', (req, res) => {
  const cfg = readConfig();
  res.json({
    capture_enabled: state.capture_enabled,
    pipeline_status: state.pipeline_status,
    usb_status:     state.usb_status,
    fps:            state.fps,
    bitrate_kbps:   state.bitrate_kbps,
    latency_ms:     state.latency_ms,
    bytes_received: state.bytes_received,
    uptime_seconds: uptimeSeconds(),
    device_name:    cfg.device_name,
    firmware_version: cfg.firmware_version,
  });
});

// GET /api/config
app.get('/api/config', (req, res) => {
  res.json(readConfig());
});

// POST /api/config
app.post('/api/config', (req, res) => {
  const errors = validateConfig(req.body);
  if (errors.length > 0) {
    return res.status(400).json({ error: 'Validation failed', details: errors });
  }
  const current = readConfig();
  const updated  = { ...current, ...req.body };
  // Mirror the flat NDI/SRT/RTMP keys the dashboard sends into nested
  // outputs.*, which is what capture/pipeline.py reads. The pipeline watches
  // this and restarts itself to apply (server.js has no systemctl privilege).
  // Preserve any other outputs.* stanzas (rtsp/hdmi) already in the file.
  updated.outputs = { ...(current.outputs || {}), ...(req.body.outputs || {}) };
  updated.outputs.ndi = {
    ...(updated.outputs.ndi || {}),
    enabled: !!updated.ndi_enabled,
    name: typeof updated.ndi_name === 'string' && updated.ndi_name.trim() ? updated.ndi_name.trim() : 'FPVLink',
  };
  updated.outputs.srt = {
    ...(updated.outputs.srt || {}),
    enabled: !!updated.srt_enabled,
    url: typeof updated.srt_url === 'string' ? updated.srt_url.trim() : (current.outputs?.srt?.url || ''),
  };
  updated.outputs.rtmp = {
    ...(updated.outputs.rtmp || {}),
    enabled: !!updated.rtmp_enabled,
    url: typeof updated.rtmp_url === 'string' ? updated.rtmp_url.trim() : (current.outputs?.rtmp?.url || ''),
    stream_key: typeof updated.rtmp_key === 'string' ? updated.rtmp_key : (current.outputs?.rtmp?.stream_key || ''),
  };
  try {
    writeConfig(updated);
    logger.info('Config updated');
    res.json({ ok: true, config: updated });
  } catch (err) {
    logger.error(`Config write failed: ${err.message}`);
    res.status(500).json({ error: 'Failed to write config' });
  }
});

// POST /api/stream/start
app.post('/api/capture/enable', async (req, res) => {
  try {
    await enableCapture();
    res.json({ ok: true, message: 'Capture enabled' });
  } catch (err) {
    if (err.message === 'Transition in progress') {
      logger.warn('Start stream rejected: Transition in progress');
      return res.status(409).json({ error: err.message });
    }
    logger.error(`Start stream failed: ${err.message}`);
    res.status(500).json({ error: err.message });
  }
});

// POST /api/stream/stop
app.post('/api/capture/disable', async (req, res) => {
  try {
    const ip = req.headers['x-forwarded-for'] || req.socket.remoteAddress;
    const ua = req.headers['user-agent'] || 'unknown';
    logger.warn(`[WATCHDOG HUNT] /api/stream/stop requested by IP: ${ip}, User-Agent: ${ua}`);
    await disableCapture();
    res.json({ ok: true, message: 'Capture disabled' });
  } catch (err) {
    if (err.message === 'Transition in progress') {
      logger.warn('Stop stream rejected: Transition in progress');
      return res.status(409).json({ error: err.message });
    }
    logger.error(`Stop stream failed: ${err.message}`);
    res.status(500).json({ error: err.message });
  }
});

// GET /api/luts
app.get('/api/luts', (req, res) => {
  res.json(readLutsManifest());
});

// POST /api/lut-upload
app.post('/api/lut-upload', upload.single('lutFile'), (req, res) => {
  try {
    const manifest = readLutsManifest();
    if (manifest.length >= 5) {
      if (req.file) fs.unlinkSync(req.file.path);
      return res.status(409).json({ error: 'Maximum 5 LUTs allowed. Please delete one first.' });
    }

    if (!req.file) {
      return res.status(400).json({ error: 'No file uploaded' });
    }

    // Basic file validation (check for LUT_3D_SIZE)
    const content = fs.readFileSync(req.file.path, 'utf8');
    if (!content.includes('LUT_3D_SIZE')) {
      fs.unlinkSync(req.file.path);
      return res.status(400).json({ error: 'Invalid 3D LUT file' });
    }

    const id = path.basename(req.file.filename, path.extname(req.file.filename));
    const newLut = {
      id,
      original_filename: req.file.originalname,
      display_name: req.body.displayName || req.file.originalname,
      uploaded_at: new Date().toISOString()
    };

    manifest.push(newLut);
    writeLutsManifest(manifest);
    logger.info(`LUT uploaded: ${newLut.display_name}`);
    res.json({ ok: true, lut: newLut, manifest });
  } catch (err) {
    logger.error(`LUT upload failed: ${err.message}`);
    res.status(500).json({ error: err.message });
  }
});

// DELETE /api/luts/:id
app.delete('/api/luts/:id', (req, res) => {
  try {
    let manifest = readLutsManifest();
    const id = req.params.id;
    const lut = manifest.find(l => l.id === id);
    if (!lut) return res.status(404).json({ error: 'LUT not found' });

    // Remove file
    const filePath = path.join(LUTS_DIR, `${id}.cube`);
    if (fs.existsSync(filePath)) fs.unlinkSync(filePath);

    manifest = manifest.filter(l => l.id !== id);
    writeLutsManifest(manifest);

    // If it was the active LUT, clear it from config
    const config = readConfig();
    if (config.hdmi_lut_active_id === id) {
      config.hdmi_lut_active_id = '';
      writeConfig(config);
    }

    logger.info(`LUT deleted: ${lut.display_name}`);
    res.json({ ok: true, manifest, config });
  } catch (err) {
    logger.error(`LUT delete failed: ${err.message}`);
    res.status(500).json({ error: err.message });
  }
});

// GET /api/logs
app.get('/api/logs', (req, res) => {
  const lines = logBuffer.slice(-200);
  res.json({ lines });
});

// GET /api/diagnostics — bundles journal history, service status, DRM plane
// state, and redacted config into a tarball for field troubleshooting (see
// scripts/collect-diagnostics.sh). This is the only supported way for a user
// with no internet access to hand support something useful: it only needs
// local LAN reachability to the dashboard, same as every other control here.
// Generated on demand and deleted right after it's sent — nothing accumulates.
app.get('/api/diagnostics', (req, res) => {
  const scriptPath = path.join(ROOT_DIR, 'scripts', 'collect-diagnostics.sh');
  const tarPath = path.join('/tmp', `fpvlink-diagnostics-${Date.now()}.tar.gz`);

  execFile('bash', [scriptPath, tarPath], { timeout: 20000, maxBuffer: 1024 * 1024 }, (error, stdout, stderr) => {
    if (error) {
      logger.error(`Diagnostics collection failed: ${error.message}${stderr ? ' | ' + stderr.trim() : ''}`);
      return res.status(500).json({ error: 'Diagnostics collection failed' });
    }
    // The script's contract: stdout's last line is the tarball path; every
    // progress message goes to stderr.
    const producedPath = stdout.trim().split('\n').pop();
    if (!producedPath || !fs.existsSync(producedPath)) {
      logger.error(`Diagnostics script produced no output file (stdout: "${stdout.trim()}")`);
      return res.status(500).json({ error: 'Diagnostics collection produced no output' });
    }

    const downloadName = `fpvlink-diagnostics-${new Date().toISOString().replace(/[:.]/g, '-')}.tar.gz`;
    logger.info(`Diagnostics bundle generated: ${downloadName}`);
    res.download(producedPath, downloadName, (downloadErr) => {
      if (downloadErr) logger.error(`Diagnostics download stream error: ${downloadErr.message}`);
      fs.unlink(producedPath, () => {});
    });
  });
});

// ─────────────────────────────────────────────
// WebSocket server
// ─────────────────────────────────────────────
const server = http.createServer(app);
const wss    = new WebSocket.Server({ server, path: '/ws' });

const wsClients = new Set();

wss.on('connection', (ws, req) => {
  const ip = req.socket.remoteAddress;
  logger.info(`WebSocket client connected: ${ip}`);
  wsClients.add(ws);

  // Send last 100 log lines immediately on connect
  const recent = logBuffer.slice(-100);
  ws.send(JSON.stringify({ type: 'log_batch', lines: recent }));

  ws.on('close', () => {
    wsClients.delete(ws);
    logger.info(`WebSocket client disconnected: ${ip}`);
  });

  ws.on('error', (err) => {
    logger.warn(`WebSocket error from ${ip}: ${err.message}`);
    wsClients.delete(ws);
  });
});

function broadcast(msg) {
  const data = JSON.stringify(msg);
  for (const ws of wsClients) {
    if (ws.readyState === WebSocket.OPEN) {
      ws.send(data, (err) => {
        if (err) wsClients.delete(ws);
      });
    }
  }
}

function broadcastLog(line) {
  broadcast({ type: 'log', line });
}

// ─────────────────────────────────────────────
// Hardware Telemetry Polling
// ─────────────────────────────────────────────

// Thermal Polling (every 2s)
setInterval(async () => {
  try {
    const rawTemp = await fs.promises.readFile('/sys/class/thermal/thermal_zone0/temp', 'utf8');
    const tempC = parseInt(rawTemp, 10) / 1000;
    if (!isNaN(tempC)) {
      state.soc_temp = parseFloat(tempC.toFixed(1));
      
      if (state.thermal_state === 'CRITICAL') {
        if (state.soc_temp < 82) {
          state.thermal_state = 'WARN';
          logger.warn(`SOC Temperature recovered to WARN state (${state.soc_temp}°C)`);
        }
      } else if (state.thermal_state === 'WARN') {
        if (state.soc_temp >= 85) {
          state.thermal_state = 'CRITICAL';
          logger.error(`CRITICAL: SOC Temperature is ${state.soc_temp}°C (Throttling)`);
        } else if (state.soc_temp < 75) {
          state.thermal_state = 'NORMAL';
          logger.info(`SOC Temperature recovered to NORMAL (${state.soc_temp}°C)`);
        }
      } else {
        // NORMAL
        if (state.soc_temp >= 85) {
          state.thermal_state = 'CRITICAL';
          logger.error(`CRITICAL: SOC Temperature is ${state.soc_temp}°C (Throttling)`);
        } else if (state.soc_temp >= 80) {
          state.thermal_state = 'WARN';
          logger.warn(`WARNING: SOC Temperature is high (${state.soc_temp}°C)`);
        }
      }
    }
  } catch (err) {
    // Silently ignore if file doesn't exist (e.g. dev environment)
    state.soc_temp = null;
    state.thermal_state = 'NORMAL';
  }
}, 2000);

// Storage Polling (every 15s)
setInterval(() => {
  // Monitor root partition for low space (passive warning)
  execFile('df', ['-k', '/'], { timeout: 1000 }, (error, stdout, stderr) => {
    if (error) {
      // Could be a timeout. Gracefully degrade.
      state.storage_free_gb = null;
      state.storage_total_gb = null;
      state.storage_usage_percent = null;
      state.storage_state = 'NORMAL';
      return;
    }
    
    const lines = stdout.trim().split('\n');
    if (lines.length > 1) {
      const parts = lines[1].trim().split(/\s+/);
      if (parts.length >= 5) {
        const totalK = parseInt(parts[1], 10);
        const freeK = parseInt(parts[3], 10);
        let usePctStr = parts[4].replace('%', '');
        if (parts.length > 5 && parts[4] === '-') {
           usePctStr = parts[5].replace('%', ''); // handle some df output formats
        }
        const usePct = parseInt(usePctStr, 10);
        
        if (!isNaN(totalK) && !isNaN(freeK)) {
          state.storage_total_gb = parseFloat((totalK / 1024 / 1024).toFixed(2));
          state.storage_free_gb = parseFloat((freeK / 1024 / 1024).toFixed(2));
          state.storage_usage_percent = isNaN(usePct) ? 0 : usePct;
          
          if (state.storage_state === 'CRITICAL') {
            if (state.storage_free_gb > 2.5) {
              state.storage_state = 'WARN';
              logger.warn(`Storage recovered to WARN state (${state.storage_free_gb} GB free)`);
            }
          } else if (state.storage_state === 'WARN') {
            if (state.storage_free_gb <= 2.0) {
              state.storage_state = 'CRITICAL';
              logger.error(`CRITICAL: Storage almost full (${state.storage_free_gb} GB free).`);
            } else if (state.storage_free_gb > 5.0 && state.storage_usage_percent < 90) {
              state.storage_state = 'NORMAL';
              logger.info(`Storage recovered to NORMAL (${state.storage_free_gb} GB free)`);
            }
          } else {
            // NORMAL
            if (state.storage_free_gb <= 2.0) {
              state.storage_state = 'CRITICAL';
              logger.error(`CRITICAL: Storage almost full (${state.storage_free_gb} GB free).`);
            } else if (state.storage_free_gb <= 5.0 || state.storage_usage_percent >= 90) {
              state.storage_state = 'WARN';
              logger.warn(`WARNING: Storage is low (${state.storage_free_gb} GB free, ${state.storage_usage_percent}%)`);
            }
          }
        }
      }
    }
  });
}, 15000);

// Push real-time stats every 500 ms
setInterval(() => {
  const statsPayload = {
    fps:            state.fps,
    bitrate_kbps:   state.bitrate_kbps,
    latency_ms:     state.latency_ms,
    dropped_frames: state.dropped_frames,
    resolution:     state.resolution,
    usb_status:     state.usb_status,
    bytes_received: state.bytes_received,
    uptime_seconds: uptimeSeconds(),
    
    // Hardware Telemetry
    soc_temp:              state.soc_temp,
    thermal_state:         state.thermal_state,
    storage_free_gb:       state.storage_free_gb,
    storage_total_gb:      state.storage_total_gb,
    storage_usage_percent: state.storage_usage_percent,
    storage_state:         state.storage_state,
  };
  
  broadcast({
    type:           'stats',
    ...statsPayload,
    capture_enabled: state.capture_enabled,
    pipeline_status: state.pipeline_status,
  });
  
  if (state.streaming && sessionLogStream) {
    sessionLogTick++;
    if (sessionLogTick >= 4) { // Log to disk every 2 seconds
      sessionLogTick = 0;
      try {
        sessionLogStream.write(JSON.stringify({
          event: 'stats',
          timestamp: new Date().toISOString(),
          ...statsPayload
        }) + '\n');
      } catch (e) {
        logger.error(`Failed to write stats to session log: ${e.message}`);
      }
    }
  }
}, 500);

// ─────────────────────────────────────────────
// UDP Preview Server
// ─────────────────────────────────────────────
const udpServer = dgram.createSocket('udp4');
udpServer.on('message', (msg) => {
  if (state.streaming) {
    broadcast({ type: 'preview', frame: msg.toString('base64') });
  }
});
udpServer.on('error', (err) => {
  logger.error(`UDP server error: ${err.message}`);
});
udpServer.bind(9002, '127.0.0.1');

// ─────────────────────────────────────────────
// Graceful shutdown
// ─────────────────────────────────────────────
function shutdown(signal) {
  logger.info(`Received ${signal}, shutting down...`);
  (async () => {
    if (state.streaming && !isTransitioning) await disableCapture();
    if (sessionLogStream) {
      try { sessionLogStream.end(); } catch (e) {}
      sessionLogStream = null;
    }
    if (standbyProcess) await killProcessAsync(standbyProcess);
    server.close(() => {
      logger.info('HTTP server closed');
      process.exit(0);
    });
  })().catch(e => {
    logger.error(`Error during shutdown: ${e.message}`);
    process.exit(1);
  });
  setTimeout(() => process.exit(1), 5000).unref();
}

process.on('SIGTERM', () => shutdown('SIGTERM'));
process.on('SIGINT',  () => shutdown('SIGINT'));

// ─────────────────────────────────────────────
// Start
// ─────────────────────────────────────────────
if (require.main === module) {
  server.listen(PORT, '0.0.0.0', () => {
    logger.info(`FPVLink server listening on http://0.0.0.0:${PORT}`);
    logger.info(`Config path: ${CONFIG_PATH}`);
    
    const cfg = readConfig();
    // standbyProcess = spawnStandby(cfg); // Handled by systemd

    if (cfg.auto_connect !== false) {
      enableCapture();
    }
  });
}

module.exports = {
  state
};
