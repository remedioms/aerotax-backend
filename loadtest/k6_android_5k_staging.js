// Android staging capacity test. It models 5,000 registered users with a
// conservative active-session peak of <=500 VUs, not 5,000 simultaneous users.
// Required: ALLOW_AEROX_STAGING_LOAD=1, BASE_URL, AEROX_STAGING_URL, and
// LOAD_ACCOUNTS_FILE. Account file: [{"label":"loadtest-001","token":"..."}].

import http from 'k6/http';
import { check, sleep } from 'k6';
import { Rate, Trend } from 'k6/metrics';
import { randomIntBetween } from 'https://jslib.k6.io/k6-utils/1.4.0/index.js';

const readSuccess = new Rate('android_me_read_success');
const appErrors = new Rate('android_me_application_errors');
const readLatency = new Trend('android_me_read_latency_ms', true);
const writeSuccess = new Rate('android_me_write_success');

function required(name) {
  const value = __ENV[name];
  if (!value) throw new Error(`${name} is required`);
  return value;
}

function normalizedUrl(raw) {
  const parsed = new URL(raw);
  if (parsed.protocol !== 'https:') throw new Error('BASE_URL must use HTTPS');
  if (parsed.username || parsed.password || parsed.pathname !== '/' || parsed.search || parsed.hash) {
    throw new Error('BASE_URL must be an origin without credentials, path, query, or fragment');
  }
  return parsed.origin;
}

function isLocalHost(host) {
  return host === 'localhost' || host === '::1' || host === '127.0.0.1' || /^127\./.test(host) ||
    /^10\./.test(host) || /^192\.168\./.test(host) || /^172\.(1[6-9]|2\d|3[0-1])\./.test(host);
}

function assertSafeTarget(base) {
  if (__ENV.ALLOW_AEROX_STAGING_LOAD !== '1') {
    throw new Error('Refusing load test: set ALLOW_AEROX_STAGING_LOAD=1 after confirming staging');
  }
  if (base !== normalizedUrl(required('AEROX_STAGING_URL'))) {
    throw new Error('BASE_URL and AEROX_STAGING_URL must match exactly');
  }
  const host = new URL(base).hostname.toLowerCase().replace(/\.$/, '');
  const forbidden = host === 'aerosteuer.de' || host.endsWith('.aerosteuer.de') ||
    host.endsWith('.onrender.com') || host === 'render.com' || host.endsWith('.render.com') ||
    host.endsWith('.run.app');
  if (forbidden) throw new Error('Refusing known production/legacy Render/Cloud Run target');
  if (isLocalHost(host) && __ENV.ALLOW_LOCAL_STAGING !== '1') {
    throw new Error('Refusing localhost/private target without ALLOW_LOCAL_STAGING=1');
  }
}

const BASE_URL = normalizedUrl(required('BASE_URL'));
assertSafeTarget(BASE_URL);
const MAX_VUS = Number(__ENV.MAX_VUS || 500);
const MIN_RPS = Number(__ENV.MIN_RPS || 20);
if (!Number.isInteger(MAX_VUS) || MAX_VUS < 1 || MAX_VUS > 500) throw new Error('MAX_VUS must be 1 through 500');
if (!Number.isFinite(MIN_RPS) || MIN_RPS <= 0) throw new Error('MIN_RPS must be > 0');
const WRITE_PERCENT = Number(__ENV.TEST_ACCOUNT_WRITE_PERCENT || 0);
const WRITES_ENABLED = __ENV.ALLOW_TEST_ACCOUNT_WRITES === '1';
if (!Number.isFinite(WRITE_PERCENT) || WRITE_PERCENT < 0 || WRITE_PERCENT > 5) throw new Error('TEST_ACCOUNT_WRITE_PERCENT must be 0 through 5');
if (WRITE_PERCENT > 0 && !WRITES_ENABLED) throw new Error('Writes require ALLOW_TEST_ACCOUNT_WRITES=1');

const ACCOUNTS = JSON.parse(open(required('LOAD_ACCOUNTS_FILE')));
if (!Array.isArray(ACCOUNTS) || ACCOUNTS.length < MAX_VUS) {
  throw new Error('LOAD_ACCOUNTS_FILE must contain at least one distinct staging account per VU');
}
for (const account of ACCOUNTS) {
  if (!account || typeof account.token !== 'string' || !account.token || typeof account.label !== 'string' || !/^loadtest-[a-z0-9_-]+$/i.test(account.label)) {
    throw new Error('Each account needs a non-secret loadtest-* label and a token');
  }
}

const READS = ['/api/me/profile', '/api/me/entitlement', '/api/me/friends', '/api/me/friends-today', '/api/me/push/prefs'];
export const options = {
  discardResponseBodies: true,
  scenarios: { android_staging_active_sessions: { executor: 'ramping-vus', startVUs: 0,
    stages: [{ duration: '5m', target: Math.min(50, MAX_VUS) }, { duration: '10m', target: Math.min(250, MAX_VUS) }, { duration: '20m', target: MAX_VUS }, { duration: '5m', target: 0 }],
    gracefulRampDown: '30s' } },
  thresholds: {
    http_req_failed: ['rate<0.01'], http_req_duration: ['p(95)<1200', 'p(99)<2500'], http_reqs: [`rate>${MIN_RPS}`], checks: ['rate>0.99'],
    android_me_read_success: ['rate>0.99'], android_me_application_errors: ['rate<0.005'], android_me_read_latency_ms: ['p(95)<1000', 'p(99)<2000'], android_me_write_success: ['rate>0.99'],
  },
};

function accountForVu() { return ACCOUNTS[(__VU - 1) % ACCOUNTS.length]; }
function headers(account) { return { Authorization: `Bearer ${account.token}`, Accept: 'application/json', 'X-AeroX-Loadtest': 'android-staging-5k' }; }
function read(account, path) {
  const response = http.get(`${BASE_URL}${path}`, { headers: headers(account), tags: { surface: 'android_me', operation: 'read', route: path }, timeout: '10s' });
  const ok = response.status >= 200 && response.status < 300;
  check(response, { 'authenticated /api/me read is 2xx': () => ok });
  readSuccess.add(ok); appErrors.add(!ok); readLatency.add(response.timings.duration);
}
function idempotentTestAccountWrite(account) {
  // Sole optional mutation: stable push prefs on purpose-made staging accounts.
  // It never creates posts, chats, uploads, jobs, payments, or provider work.
  const response = http.post(`${BASE_URL}/api/me/push/prefs`, JSON.stringify({ prefs: { dm: false, group_message: false, friend_request: false, friend_accepted: false, roster_change: false, community: false } }), {
    headers: { ...headers(account), 'Content-Type': 'application/json' }, tags: { surface: 'android_me', operation: 'idempotent_test_write', route: '/api/me/push/prefs' }, timeout: '10s' });
  const ok = response.status >= 200 && response.status < 300;
  check(response, { 'test-account preference write is 2xx': () => ok });
  writeSuccess.add(ok); appErrors.add(!ok);
}
export function setup() {
  const response = http.get(`${BASE_URL}/api/health`, { headers: { 'X-AeroX-Loadtest': 'android-staging-5k' }, tags: { surface: 'staging_gate', operation: 'health', route: '/api/health' }, timeout: '10s' });
  if (response.status !== 200) throw new Error('Staging health gate failed');
}
export default function () {
  const account = accountForVu();
  read(account, READS[randomIntBetween(0, READS.length - 1)]);
  read(account, READS[randomIntBetween(0, READS.length - 1)]);
  if (WRITES_ENABLED && WRITE_PERCENT > 0 && Math.random() * 100 < WRITE_PERCENT) idempotentTestAccountWrite(account);
  sleep(randomIntBetween(2, 5));
}
