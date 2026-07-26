const test = require('node:test');
const assert = require('node:assert');
const server = require('./server.js');

test('parseStatLine with legacy space-separated values', (t) => {
  server.state.fps = 0;
  server.state.usb_status = 'disconnected';
  
  const line = 'fps=59.94 bitrate_kbps=8420 latency_ms=12 bytes=102400 usb=connected';
  const updated = server.parseStatLine(line);
  
  assert.strictEqual(updated, true);
  assert.strictEqual(server.state.fps, 59.94);
  assert.strictEqual(server.state.bitrate_kbps, 8420);
  assert.strictEqual(server.state.latency_ms, 12);
  assert.strictEqual(server.state.bytes_received, 102400);
  assert.strictEqual(server.state.usb_status, 'connected');
});

test('parseStatLine with new JSON [STATS] format', (t) => {
  server.state.dropped_frames = 0;
  server.state.resolution = '—';
  
  const line = '[STATS] {"fps": 60.0, "bitrate_kbps": 10000, "dropped_frames": 5, "resolution": "1920x1080"}';
  const updated = server.parseStatLine(line);
  
  assert.strictEqual(updated, true);
  assert.strictEqual(server.state.fps, 60.0);
  assert.strictEqual(server.state.bitrate_kbps, 10000);
  assert.strictEqual(server.state.dropped_frames, 5);
  assert.strictEqual(server.state.resolution, '1920x1080');
});

test('parseStatLine with missing fields in JSON ignores them', (t) => {
  server.state.fps = 30.0; // existing state
  
  const line = '[STATS] {"dropped_frames": 10}';
  const updated = server.parseStatLine(line);
  
  assert.strictEqual(updated, true);
  assert.strictEqual(server.state.fps, 30.0); // should not be overwritten
  assert.strictEqual(server.state.dropped_frames, 10);
});

test('parseStatLine with malformed JSON logs error and returns false', (t) => {
  // Mock logger to prevent console spam during test if needed
  const line = '[STATS] {"fps": 60.0, "broken_json": }';
  const updated = server.parseStatLine(line);
  
  assert.strictEqual(updated, false);
});

test('parseStatLine with non-matching line returns false', (t) => {
  const line = 'INFO: Pipeline started normally';
  const updated = server.parseStatLine(line);
  
  assert.strictEqual(updated, false);
});
