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

test('validateConfig enforces boolean flags', (t) => {
  const errors = server.validateConfig({ srt_enabled: 'yes', ndi_enabled: true });
  assert.deepStrictEqual(errors, ['srt_enabled must be a boolean']);
});

test('validateConfig accumulates multiple errors', (t) => {
  const errors = server.validateConfig({ goggles_model: 'nope', bitrate_mbps: 999 });
  assert.strictEqual(errors.length, 2);
});
