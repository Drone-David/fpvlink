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
const { spawn }      = require('child_process');
const readline       = require('readline');
const dgram          = require('dgram');

// ─────────────────────────────────────────────
// Paths
// ─────────────────────────────────────────────
const ROOT_DIR    = path.resolve(__dirname, '..');
const SYSTEM_DIR  = path.join(ROOT_DIR, 'system');
const CONFIG_PATH = path.join(SYSTEM_DIR, 'config.json');
const WEB_DIR     = __dirname;

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
  record_enabled:   false,
  bitrate_mbps:     50,
  device_name:      'FPVLink',
  firmware_version: '1.0.0',
};

function readConfig() {
  try {
    if (!fs.existsSync(CONFIG_PATH)) {
      logger.warn(`config.json not found at ${CONFIG_PATH}, using defaults`);
      return { ...CONFIG_DEFAULTS };
    }
    const raw = fs.readFileSync(CONFIG_PATH, 'utf8');
    return { ...CONFIG_DEFAULTS, ...JSON.parse(raw) };
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
  ['srt_enabled', 'rtmp_enabled', 'record_enabled'].forEach((k) => {
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
  streaming:      false,
  usb_status:     'disconnected',   // 'connected' | 'disconnected'
  fps:            0,
  bitrate_kbps:   0,
  latency_ms:     0,
  bytes_received: 0,
  dropped_frames: 0,
  resolution:     '—',
  startedAt:      null,
};

let captureProcess  = null;
let pipelineProcess = null;
let standbyProcess  = null;
let isTransitioning = false;

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
    parseStatLine(line);
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

function spawnPipeline(cfg) {
  const script = path.join(ROOT_DIR, 'pipeline', 'pipeline.py');
  logger.info(`Spawning pipeline process: python3 ${script}`);

  const env = {
    ...process.env,
    FPVLINK_CONFIG:   CONFIG_PATH,
    FPVLINK_BITRATE:  String(cfg.bitrate_mbps || 50),
    FPVLINK_SRT:      cfg.srt_enabled  ? '1' : '0',
    FPVLINK_SRT_URL:  cfg.srt_url  || '',
    FPVLINK_RTMP:     cfg.rtmp_enabled ? '1' : '0',
    FPVLINK_RTMP_URL: cfg.rtmp_url || '',
    FPVLINK_RTMP_KEY: cfg.rtmp_key || '',
    FPVLINK_RECORD:   cfg.record_enabled ? '1' : '0',
  };

  const proc = spawn('python3', [script], {
    env,
    stdio: ['pipe', 'pipe', 'pipe'],
  });

  proc.stdin.on('error', (err) => {
    if (err.code === 'EPIPE') {
      logger.debug('Pipeline stdin closed (EPIPE)');
    } else {
      logger.error(`Pipeline stdin error: ${err.message}`);
    }
  });

  const pipelineStdout = readline.createInterface({ input: proc.stdout });
  pipelineStdout.on('line', (line) => {
    logger.info(`[pipeline] ${line}`);
    parseStatLine(line);
  });

  const pipelineStderr = readline.createInterface({ input: proc.stderr });
  pipelineStderr.on('line', (line) => {
    logger.warn(`[pipeline:err] ${line}`);
  });

  proc.on('close', (code) => {
    logger.warn(`Pipeline process exited with code ${code}`);
    if (state.streaming) state.streaming = false;
  });

  proc.on('error', (err) => {
    logger.error(`Pipeline process error: ${err.message}`);
  });

  return proc;
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

/**
 * Parse stat lines emitted by capture / pipeline scripts.
 * Supports legacy key-value format and new JSON [STATS] format.
 */
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

async function startStream() {
  if (isTransitioning) throw new Error('Transition in progress');
  if (state.streaming) throw new Error('Already streaming');
  
  isTransitioning = true;
  try {
    const cfg = readConfig();
    logger.info('Starting stream...');

    if (standbyProcess) {
      await killProcessAsync(standbyProcess);
      standbyProcess = null;
    }

    pipelineProcess = spawnPipeline(cfg);
    // Small delay so pipeline is ready to receive data
    await new Promise((r) => setTimeout(r, 300));
    captureProcess  = spawnCapture(cfg);

    state.streaming  = true;
    state.startedAt  = Date.now();
    state.usb_status = 'connected';
    logger.info('Stream started');
  } finally {
    isTransitioning = false;
  }
}

async function stopStream() {
  if (isTransitioning) throw new Error('Transition in progress');
  isTransitioning = true;
  
  try {
    logger.info('Stopping stream...');
    
    if (captureProcess) {
      await killProcessAsync(captureProcess);
      captureProcess = null;
    }
    if (pipelineProcess) {
      await killProcessAsync(pipelineProcess);
      pipelineProcess = null;
    }
    
    state.streaming      = false;
    state.startedAt      = null;
    state.fps            = 0;
    state.bitrate_kbps   = 0;
    state.latency_ms     = 0;
    state.bytes_received = 0;
    state.dropped_frames = 0;
    state.resolution     = '—';
    state.usb_status     = 'disconnected';
    
    const cfg = readConfig();
    standbyProcess = spawnStandby(cfg);
    
    logger.info('Stream stopped');
  } finally {
    isTransitioning = false;
  }
}

// ─────────────────────────────────────────────
// Express app
// ─────────────────────────────────────────────
const app = express();
app.use(express.json());
app.use(express.static(WEB_DIR));

// GET /api/status
app.get('/api/status', (req, res) => {
  const cfg = readConfig();
  res.json({
    streaming:      state.streaming,
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
app.post('/api/stream/start', async (req, res) => {
  try {
    await startStream();
    res.json({ ok: true, message: 'Stream started' });
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
app.post('/api/stream/stop', async (req, res) => {
  try {
    const ip = req.headers['x-forwarded-for'] || req.socket.remoteAddress;
    const ua = req.headers['user-agent'] || 'unknown';
    logger.warn(`[WATCHDOG HUNT] /api/stream/stop requested by IP: ${ip}, User-Agent: ${ua}`);
    await stopStream();
    res.json({ ok: true, message: 'Stream stopped' });
  } catch (err) {
    if (err.message === 'Transition in progress') {
      logger.warn('Stop stream rejected: Transition in progress');
      return res.status(409).json({ error: err.message });
    }
    logger.error(`Stop stream failed: ${err.message}`);
    res.status(500).json({ error: err.message });
  }
});

// GET /api/logs
app.get('/api/logs', (req, res) => {
  const lines = logBuffer.slice(-200);
  res.json({ lines });
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

// Push real-time stats every 500 ms
setInterval(() => {
  broadcast({
    type:           'stats',
    fps:            state.fps,
    bitrate_kbps:   state.bitrate_kbps,
    latency_ms:     state.latency_ms,
    dropped_frames: state.dropped_frames,
    resolution:     state.resolution,
    usb_status:     state.usb_status,
    streaming:      state.streaming,
    bytes_received: state.bytes_received,
    uptime_seconds: uptimeSeconds(),
  });
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
    if (state.streaming && !isTransitioning) await stopStream();
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
    standbyProcess = spawnStandby(cfg);
  });
}

module.exports = {
  parseStatLine,
  state
};
