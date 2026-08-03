const test = require('node:test');
const assert = require('node:assert');
const server = require('./server.js');

test('validateConfig accepts an empty body (no fields to validate)', (t) => {
  const errors = server.validateConfig({});
  assert.deepStrictEqual(errors, []);
});

test('validateConfig rejects an unknown goggles_model', (t) => {
  const errors = server.validateConfig({ goggles_model: 'goggles99' });
  assert.strictEqual(errors.length, 1);
  assert.match(errors[0], /goggles_model must be one of/);
});

test('validateConfig enforces bitrate_mbps range', (t) => {
  assert.deepStrictEqual(server.validateConfig({ bitrate_mbps: 1 }), ['bitrate_mbps must be between 2 and 50']);
  assert.deepStrictEqual(server.validateConfig({ bitrate_mbps: 51 }), ['bitrate_mbps must be between 2 and 50']);
  assert.deepStrictEqual(server.validateConfig({ bitrate_mbps: 20 }), []);
});

test('validateConfig rejects a non-string srt_url', (t) => {
  const errors = server.validateConfig({ srt_url: 12345 });
  assert.deepStrictEqual(errors, ['srt_url must be a string']);
});

test('validateConfig rejects an unknown standby_card', (t) => {
  const errors = server.validateConfig({ standby_card: 'rainbow' });
  assert.strictEqual(errors.length, 1);
  assert.match(errors[0], /standby_card must be one of/);
});

test('validateConfig accepts every card the pipeline can render', (t) => {
  // These ids must match STANDBY_CARDS in capture/pipeline.py. A card the
  // validator rejects can't be selected at all; one it accepts but the
  // pipeline doesn't know silently falls back to the default.
  for (const card of ['grounded', 'grounded_anim', 'bars', 'black']) {
    assert.deepStrictEqual(server.validateConfig({ standby_card: card }), [],
      `expected ${card} to be accepted`);
  }
});

test('validateConfig enforces boolean flags', (t) => {
  const errors = server.validateConfig({ srt_enabled: 'yes', ndi_enabled: true });
  assert.deepStrictEqual(errors, ['srt_enabled must be a boolean']);
});

test('validateConfig accumulates multiple errors', (t) => {
  const errors = server.validateConfig({ goggles_model: 'nope', bitrate_mbps: 999 });
  assert.strictEqual(errors.length, 2);
});

// ─────────────────────────────────────────────
// parseLutSize
// ─────────────────────────────────────────────
test('parseLutSize reads the grid size from a .cube header', (t) => {
  assert.strictEqual(server.parseLutSize('TITLE "x"\nLUT_3D_SIZE 33\n0 0 0\n'), 33);
});

test('parseLutSize tolerates leading whitespace and later lines', (t) => {
  assert.strictEqual(server.parseLutSize('#comment\n  LUT_3D_SIZE 17\n'), 17);
});

test('parseLutSize returns null when the header is absent', (t) => {
  assert.strictEqual(server.parseLutSize('DOMAIN_MIN 0 0 0\n0 0 0\n'), null);
});

// ─────────────────────────────────────────────
// buildOutputs
// ─────────────────────────────────────────────
test('buildOutputs marks disabled outputs off, never idle', (t) => {
  server.outputReports.clear();
  server.state.pipeline_status = 'live';
  const out = server.buildOutputs({ srt_enabled: false, rtmp_enabled: false, ndi_enabled: false });
  assert.deepStrictEqual(out.map((o) => o.state), ['off', 'off', 'off']);
  assert.ok(out.every((o) => o.enabled === false));
});

test('buildOutputs derives up when live and idle when not', (t) => {
  server.outputReports.clear();
  const cfg = { srt_enabled: true, srt_url: 'srt://host:5000' };

  server.state.pipeline_status = 'live';
  assert.strictEqual(server.buildOutputs(cfg)[0].state, 'up');

  server.state.pipeline_status = 'standby';
  assert.strictEqual(server.buildOutputs(cfg)[0].state, 'idle');
});

test('buildOutputs flags derived rows so the UI does not imply real reporting', (t) => {
  server.outputReports.clear();
  server.state.pipeline_status = 'live';
  const srt = server.buildOutputs({ srt_enabled: true })[0];
  assert.strictEqual(srt.reported, false);
});

test('buildOutputs prefers a fresh report over derivation', (t) => {
  server.outputReports.clear();
  server.state.pipeline_status = 'live';
  server.outputReports.set('rtmp', {
    state: 'retrying', detail: 'handshake refused', retry: 3, at: Date.now(),
  });
  const rtmp = server.buildOutputs({ rtmp_enabled: true, rtmp_url: 'rtmp://x' })[1];
  assert.strictEqual(rtmp.state, 'retrying');
  assert.strictEqual(rtmp.detail, 'handshake refused');
  assert.strictEqual(rtmp.retry, 3);
  assert.strictEqual(rtmp.reported, true);
});

test('buildOutputs ignores a stale report rather than showing a dead reporter as up', (t) => {
  server.outputReports.clear();
  server.state.pipeline_status = 'standby';
  server.outputReports.set('rtmp', {
    state: 'up', detail: 'rtt 12ms', at: Date.now() - 60000,
  });
  const rtmp = server.buildOutputs({ rtmp_enabled: true, rtmp_url: 'rtmp://x' })[1];
  assert.strictEqual(rtmp.state, 'idle');
  assert.strictEqual(rtmp.reported, false);
});
