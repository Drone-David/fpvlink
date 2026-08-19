const test   = require('node:test');
const assert = require('node:assert');
const { createAuth } = require('./auth.js');

const mk = (over = {}) => createAuth({ password: 'hunter2', sessionSecret: 'fixed-secret', ...over });

const req = (opts = {}) => ({
  socket:  { remoteAddress: opts.ip || '192.168.1.50' },
  headers: opts.cookie ? { cookie: opts.cookie } : {},
});

test('a minted token verifies', () => {
  const a = mk();
  assert.strictEqual(a.verifyToken(a.mintToken()), true);
});

test('a tampered signature is rejected', () => {
  const a = mk();
  const t = a.mintToken();
  assert.strictEqual(a.verifyToken(t.slice(0, -3) + 'aaa'), false);
});

test('a token minted under a different secret is rejected', () => {
  const t = mk().mintToken();
  const other = createAuth({ password: 'hunter2', sessionSecret: 'a-different-secret' });
  assert.strictEqual(other.verifyToken(t), false);
});

test('garbage tokens are rejected without throwing', () => {
  const a = mk();
  for (const bad of ['', 'nope', 'a.b', '.', 'x.', null, undefined, 42, {}]) {
    assert.strictEqual(a.verifyToken(bad), false);
  }
});

test('an expired token is rejected', () => {
  const a = mk();
  // exp in the past, signed correctly — only the expiry should fail it.
  const crypto = require('crypto');
  const b64url = (b) => Buffer.from(b).toString('base64')
    .replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '');
  const payload = b64url(JSON.stringify({ exp: Date.now() - 1000 }));
  const sig = b64url(crypto.createHmac('sha256', Buffer.from('fixed-secret', 'utf8')).update(payload).digest());
  assert.strictEqual(a.verifyToken(`${payload}.${sig}`), false);
});

test('the right password is accepted and the wrong one is not', () => {
  const a = mk();
  assert.strictEqual(a.checkPassword('hunter2'), true);
  assert.strictEqual(a.checkPassword('hunter3'), false);
  assert.strictEqual(a.checkPassword(''), false);
  assert.strictEqual(a.checkPassword('hunter2 '), false);
});

test('a remote request with no cookie is not authorized', () => {
  assert.strictEqual(mk().isAuthorized(req()), false);
});

test('a remote request with a valid cookie is authorized', () => {
  const a = mk();
  assert.strictEqual(a.isAuthorized(req({ cookie: `fpvlink_session=${a.mintToken()}` })), true);
});

test('loopback bypasses auth on every form of the address', () => {
  const a = mk();
  for (const ip of ['127.0.0.1', '::1', '::ffff:127.0.0.1']) {
    assert.strictEqual(a.isAuthorized(req({ ip })), true, ip);
  }
});

test('an address merely starting with 127 is not treated as loopback', () => {
  // Guards against a startsWith('127.') style check creeping in.
  assert.strictEqual(mk().isAuthorized(req({ ip: '127.0.0.1.evil.com' })), false);
});

test('a spoofed X-Forwarded-For does not grant loopback', () => {
  const a = mk();
  const r = req();
  r.headers['x-forwarded-for'] = '127.0.0.1';
  assert.strictEqual(a.isAuthorized(r), false);
});

test('cookies are parsed out of a crowded header', () => {
  const a = mk();
  const t = a.mintToken();
  assert.strictEqual(a.isAuthorized(req({ cookie: `theme=dark; fpvlink_session=${t}; tz=UTC` })), true);
});

test('a similarly named cookie is not mistaken for the session', () => {
  const a = mk();
  const t = a.mintToken();
  assert.strictEqual(a.isAuthorized(req({ cookie: `xfpvlink_session=${t}` })), false);
});

test('with no password configured, auth is off and everything is authorized', () => {
  const a = createAuth({ password: '', sessionSecret: 's' });
  assert.strictEqual(a.enabled, false);
  assert.strictEqual(a.isAuthorized(req()), true);
  assert.strictEqual(a.checkPassword(''), false, 'must never accept a login when disabled');
});

test('lockout trips after five failures and clears on success', () => {
  const a = mk();
  const ip = '10.0.0.9';
  for (let i = 0; i < 4; i++) a.noteFailure(ip);
  assert.strictEqual(a.isLockedOut(ip), false, 'four failures is not a lockout');
  a.noteFailure(ip);
  assert.strictEqual(a.isLockedOut(ip), true, 'five failures is');
  a.clearFailures(ip);
  assert.strictEqual(a.isLockedOut(ip), false);
});

test('lockout is per address', () => {
  const a = mk();
  for (let i = 0; i < 5; i++) a.noteFailure('10.0.0.1');
  assert.strictEqual(a.isLockedOut('10.0.0.1'), true);
  assert.strictEqual(a.isLockedOut('10.0.0.2'), false);
});

test('the session cookie is HttpOnly, SameSite=Strict, and not Secure', () => {
  // Not Secure is deliberate: the dashboard is plain HTTP on a LAN or the field
  // AP, and a Secure cookie would never be sent, so login would appear to fail.
  const h = mk().cookieHeader('tok');
  assert.match(h, /HttpOnly/);
  assert.match(h, /SameSite=Strict/);
  assert.doesNotMatch(h, /Secure/);
});
