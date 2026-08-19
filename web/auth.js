/**
 * FPVLink — dashboard authentication.
 *
 * Deliberately small and dependency-free: node's crypto plus a signed cookie.
 * There is no session store, so a reboot at a flying field does not log the
 * phone in your pocket back out, provided FPVLINK_SESSION_SECRET is set.
 *
 * Threat model, stated so the choices below make sense: the attacker is
 * someone within radio range of the field AP, or on the venue LAN. They can
 * open a socket to :8080 and nothing more. They do NOT have a shell on the
 * box — if they did, they could read this file, the password, and the video
 * itself, and no amount of application auth would matter.
 *
 * That is why loopback is exempt (see isLoopback) and why the password may
 * live in plaintext in an 0640 root-owned env file next to the stream keys.
 */

'use strict';

const crypto = require('crypto');

const COOKIE_NAME = 'fpvlink_session';
const SESSION_TTL_MS = 30 * 24 * 60 * 60 * 1000;   // 30 days

// Failed logins tracked per source IP. A dashboard password typed on a phone
// in a field will be short, so an unthrottled login endpoint is a few hours of
// guessing on a LAN. Small numbers here are fine: nobody legitimately fails
// five times in a minute, and the lockout is per-IP and short enough that it
// cannot lock the owner out of their own box for long.
const MAX_FAILURES = 5;
const LOCKOUT_MS   = 60 * 1000;
const failures = new Map();   // ip -> { count, first }

function sha256(s) {
  return crypto.createHash('sha256').update(String(s), 'utf8').digest();
}

/**
 * Loopback requests bypass auth.
 *
 * scripts/capture-flag.py drives /api/capture/* over 127.0.0.1, and
 * collect-diagnostics.sh reads /api/diagnostics the same way. Both run on the
 * box itself, where the operator already has a shell. Requiring a password
 * there would buy nothing and break the field tooling.
 *
 * This is only safe because the app never trusts X-Forwarded-For: Express's
 * "trust proxy" is left at its default of false, so req.ip is the real socket
 * peer and a remote client cannot claim to be local by setting a header. If a
 * reverse proxy is ever put in front of this, revisit both facts together.
 */
function isLoopback(req) {
  const ip = (req.socket && req.socket.remoteAddress) || '';
  return ip === '127.0.0.1' || ip === '::1' || ip === '::ffff:127.0.0.1';
}

function b64url(buf) {
  return Buffer.from(buf).toString('base64')
    .replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '');
}

function createAuth({ password, sessionSecret }) {
  const enabled = Boolean(password);

  // No fixed secret means sessions do not survive a restart. That is a usable
  // fallback rather than a failure — you log in again — so it warns instead of
  // refusing to start.
  const secret = sessionSecret
    ? Buffer.from(String(sessionSecret), 'utf8')
    : crypto.randomBytes(32);

  const passwordDigest = enabled ? sha256(password) : null;

  function mintToken() {
    const payload = b64url(JSON.stringify({ exp: Date.now() + SESSION_TTL_MS }));
    const sig = b64url(crypto.createHmac('sha256', secret).update(payload).digest());
    return `${payload}.${sig}`;
  }

  function verifyToken(token) {
    if (typeof token !== 'string' || !token.includes('.')) return false;
    const [payload, sig] = token.split('.', 2);
    if (!payload || !sig) return false;

    const expected = b64url(crypto.createHmac('sha256', secret).update(payload).digest());
    // Compare digests rather than the strings themselves: timingSafeEqual
    // throws on length mismatch, which would itself leak length.
    if (!crypto.timingSafeEqual(sha256(sig), sha256(expected))) return false;

    try {
      const { exp } = JSON.parse(Buffer.from(payload.replace(/-/g, '+').replace(/_/g, '/'), 'base64'));
      return typeof exp === 'number' && Date.now() < exp;
    } catch {
      return false;
    }
  }

  function readCookie(req, name) {
    const raw = req.headers && req.headers.cookie;
    if (!raw) return null;
    for (const part of raw.split(';')) {
      const i = part.indexOf('=');
      if (i === -1) continue;
      if (part.slice(0, i).trim() === name) return decodeURIComponent(part.slice(i + 1).trim());
    }
    return null;
  }

  function hasValidSession(req) {
    return verifyToken(readCookie(req, COOKIE_NAME));
  }

  /** True if this request may proceed without a password. */
  function isAuthorized(req) {
    if (!enabled) return true;
    if (isLoopback(req)) return true;
    return hasValidSession(req);
  }

  function checkPassword(candidate) {
    if (!enabled) return false;
    // Both sides are hashed to a fixed 32 bytes first, so the comparison is
    // constant-time and does not depend on the candidate's length.
    return crypto.timingSafeEqual(sha256(candidate), passwordDigest);
  }

  function noteFailure(ip) {
    const now = Date.now();
    const rec = failures.get(ip);
    if (!rec || now - rec.first > LOCKOUT_MS) failures.set(ip, { count: 1, first: now });
    else rec.count += 1;
  }

  function isLockedOut(ip) {
    const rec = failures.get(ip);
    if (!rec) return false;
    if (Date.now() - rec.first > LOCKOUT_MS) { failures.delete(ip); return false; }
    return rec.count >= MAX_FAILURES;
  }

  function clearFailures(ip) { failures.delete(ip); }

  // The cookie is intentionally NOT marked Secure. The dashboard is served over
  // plain HTTP on a LAN or the field AP — there is no certificate and no way to
  // get one for 10.10.20.1 — so a Secure cookie would simply never be sent and
  // login would appear to silently fail. SameSite=Strict and HttpOnly still
  // apply and are what actually matter here.
  function cookieHeader(token) {
    const maxAge = Math.floor(SESSION_TTL_MS / 1000);
    return `${COOKIE_NAME}=${encodeURIComponent(token)}; HttpOnly; SameSite=Strict; Path=/; Max-Age=${maxAge}`;
  }

  function clearCookieHeader() {
    return `${COOKIE_NAME}=; HttpOnly; SameSite=Strict; Path=/; Max-Age=0`;
  }

  return {
    enabled,
    COOKIE_NAME,
    isLoopback,
    isAuthorized,
    hasValidSession,
    checkPassword,
    mintToken,
    verifyToken,
    cookieHeader,
    clearCookieHeader,
    noteFailure,
    isLockedOut,
    clearFailures,
  };
}

module.exports = { createAuth, isLoopback, COOKIE_NAME, SESSION_TTL_MS };
