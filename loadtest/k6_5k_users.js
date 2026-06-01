// k6 load test for the Aeris backend.
//
// Target: 5000 concurrent crew users, 30 minute sustained load.
//
// Run:
//   k6 run --vus=2500 --duration=30m loadtest/k6_5k_users.js
//   # or with the built-in ramping schedule below:
//   k6 run loadtest/k6_5k_users.js
//
// Environment variables:
//   BASE_URL      Base URL of the backend (default https://aerotax-backend.onrender.com)
//   AUTH_TOKEN    Optional Bearer token to use for write endpoints.
//                 If unset, write endpoints are skipped (read-only mode).
//   K6_OUT        Output format, e.g. --out json=summary.json
//
// Traffic mix per iteration:
//   70%  read   (wall feed, news, friends-today, dependencies probe)
//   25%  write  (wall post, wall comment, like)
//   5%   heavy  (tax-pipeline submit)
//
// Thresholds (build fails if violated):
//   p95 latency  < 2000 ms across all reads
//   error_rate   < 1 %
//   http_req_failed < 1 %

import http from 'k6/http';
import { check, group, sleep } from 'k6';
import { Counter, Rate, Trend } from 'k6/metrics';
import { randomIntBetween } from 'https://jslib.k6.io/k6-utils/1.4.0/index.js';

// --------------------------------------------------------------------------
// Config
// --------------------------------------------------------------------------
const BASE_URL = __ENV.BASE_URL || 'https://aerotax-backend.onrender.com';
const TOKEN    = __ENV.AUTH_TOKEN || '';
const HAS_AUTH = TOKEN.length > 0;

export const options = {
    discardResponseBodies: false,
    scenarios: {
        crew_load: {
            executor: 'ramping-vus',
            startVUs: 0,
            stages: [
                { duration: '5m',  target: 500  },  // ramp to 500
                { duration: '10m', target: 2500 },  // ramp to 2500
                { duration: '30m', target: 2500 },  // hold
                { duration: '5m',  target: 0    },  // ramp down
            ],
            gracefulRampDown: '30s',
        },
    },
    thresholds: {
        http_req_duration: ['p(95)<2000'],
        http_req_failed:   ['rate<0.01'],
        'errors':          ['rate<0.01'],
        'http_req_duration{tag:read}':  ['p(95)<1500'],
        'http_req_duration{tag:write}': ['p(95)<2500'],
        'http_req_duration{tag:heavy}': ['p(95)<8000'],
        'http_req_duration{tag:status}':['p(95)<800'],
    },
    summaryTrendStats: ['avg', 'min', 'med', 'p(90)', 'p(95)', 'p(99)', 'max'],
};

// --------------------------------------------------------------------------
// Custom metrics
// --------------------------------------------------------------------------
const errors          = new Rate('errors');
const readLatency     = new Trend('read_latency_ms', true);
const writeLatency    = new Trend('write_latency_ms', true);
const heavyLatency    = new Trend('heavy_latency_ms', true);
const writeAttempts   = new Counter('write_attempts');
const writeRateLimited= new Counter('write_rate_limited');

// --------------------------------------------------------------------------
// Helpers
// --------------------------------------------------------------------------
function authHeaders(extra) {
    const h = {
        'Content-Type': 'application/json',
        'User-Agent': 'aeris-k6-loadtest/1.0',
    };
    if (HAS_AUTH) h['Authorization'] = `Bearer ${TOKEN}`;
    return Object.assign(h, extra || {});
}

function recordResult(res, latencyMetric) {
    const ok = res.status >= 200 && res.status < 400;
    errors.add(!ok && res.status !== 429);  // 429 is expected under load
    latencyMetric.add(res.timings.duration);
    return ok;
}

// --------------------------------------------------------------------------
// Scenario steps
// --------------------------------------------------------------------------
function readWave() {
    group('reads', function () {
        const reads = [
            { url: `${BASE_URL}/status`,                       tag: 'status'  },
            { url: `${BASE_URL}/api/wall/feed?limit=20`,       tag: 'read'    },
            { url: `${BASE_URL}/api/news/today`,               tag: 'read'    },
            { url: `${BASE_URL}/api/crew-graph/friends-today`, tag: 'read'    },
            { url: `${BASE_URL}/api/forum/threads?limit=20`,   tag: 'read'    },
            { url: `${BASE_URL}/api/aircraft-health/recent?limit=10`, tag: 'read' },
        ];
        // pick 2-3 of them per iteration so the mix matches the brief
        const picks = randomIntBetween(2, 3);
        for (let i = 0; i < picks; i++) {
            const pick = reads[Math.floor(Math.random() * reads.length)];
            const res = http.get(pick.url, {
                headers: authHeaders(),
                tags: { tag: pick.tag, endpoint: pick.url.replace(BASE_URL, '') },
                timeout: '10s',
            });
            check(res, {
                'read 2xx/3xx/429': (r) => (r.status >= 200 && r.status < 400) || r.status === 429,
            });
            recordResult(res, readLatency);
        }
    });
}

function writeWave() {
    if (!HAS_AUTH) return;  // skip writes if no token provided

    group('writes', function () {
        const ops = [
            // wall post
            () => {
                writeAttempts.add(1);
                const body = JSON.stringify({
                    body: `k6 load test post ${Date.now()}`,
                    layover_iata: 'FRA',
                });
                const res = http.post(`${BASE_URL}/api/wall/post`, body, {
                    headers: authHeaders(),
                    tags: { tag: 'write', endpoint: '/api/wall/post' },
                    timeout: '10s',
                });
                if (res.status === 429) writeRateLimited.add(1);
                recordResult(res, writeLatency);
            },
            // wall comment
            () => {
                writeAttempts.add(1);
                const body = JSON.stringify({
                    post_id: 'k6_placeholder',
                    body: `k6 comment ${Date.now()}`,
                });
                const res = http.post(`${BASE_URL}/api/wall/comment`, body, {
                    headers: authHeaders(),
                    tags: { tag: 'write', endpoint: '/api/wall/comment' },
                    timeout: '10s',
                });
                if (res.status === 429) writeRateLimited.add(1);
                recordResult(res, writeLatency);
            },
            // like
            () => {
                writeAttempts.add(1);
                const body = JSON.stringify({ post_id: 'k6_placeholder' });
                const res = http.post(`${BASE_URL}/api/wall/like`, body, {
                    headers: authHeaders(),
                    tags: { tag: 'write', endpoint: '/api/wall/like' },
                    timeout: '10s',
                });
                if (res.status === 429) writeRateLimited.add(1);
                recordResult(res, writeLatency);
            },
        ];
        ops[Math.floor(Math.random() * ops.length)]();
    });
}

function heavyWave() {
    if (!HAS_AUTH) return;
    group('heavy', function () {
        // Tax pipeline submit -- in the load test we POST to a no-op probe
        // endpoint that exercises the queue+token path without actually
        // burning Sonnet tokens. The probe should still hit Supabase + the
        // Cloud Tasks enqueue path.
        const res = http.post(
            `${BASE_URL}/api/job/probe`,
            JSON.stringify({ probe: true }),
            {
                headers: authHeaders(),
                tags: { tag: 'heavy', endpoint: '/api/job/probe' },
                timeout: '30s',
            },
        );
        recordResult(res, heavyLatency);
    });
}

// --------------------------------------------------------------------------
// Main VU function: 70/25/5 read/write/heavy mix
// --------------------------------------------------------------------------
export default function () {
    const r = Math.random();
    if (r < 0.70) {
        readWave();
    } else if (r < 0.95) {
        writeWave();
    } else {
        heavyWave();
    }
    // think-time: 1-4 sec between iterations to model a real user
    sleep(randomIntBetween(1, 4));
}

// --------------------------------------------------------------------------
// End-of-test summary
// --------------------------------------------------------------------------
export function handleSummary(data) {
    return {
        'stdout': textSummary(data),
        '/tmp/aeris_k6_summary.json': JSON.stringify(data, null, 2),
    };
}

function textSummary(data) {
    const m = data.metrics;
    const fmt = (v) => (v === undefined ? 'n/a' : Math.round(v));
    const lines = [
        '',
        '=== Aeris backend load test summary ===',
        `  http_req_duration  p95   : ${fmt(m.http_req_duration && m.http_req_duration.values && m.http_req_duration.values['p(95)'])} ms`,
        `  http_req_duration  p99   : ${fmt(m.http_req_duration && m.http_req_duration.values && m.http_req_duration.values['p(99)'])} ms`,
        `  http_req_failed    rate  : ${(m.http_req_failed && m.http_req_failed.values ? (m.http_req_failed.values.rate * 100).toFixed(2) : 'n/a')} %`,
        `  errors             rate  : ${(m.errors && m.errors.values ? (m.errors.values.rate * 100).toFixed(2) : 'n/a')} %`,
        `  read_latency       p95   : ${fmt(m.read_latency_ms && m.read_latency_ms.values && m.read_latency_ms.values['p(95)'])} ms`,
        `  write_latency      p95   : ${fmt(m.write_latency_ms && m.write_latency_ms.values && m.write_latency_ms.values['p(95)'])} ms`,
        `  heavy_latency      p95   : ${fmt(m.heavy_latency_ms && m.heavy_latency_ms.values && m.heavy_latency_ms.values['p(95)'])} ms`,
        `  write_attempts           : ${m.write_attempts && m.write_attempts.values ? m.write_attempts.values.count : 0}`,
        `  write_rate_limited (429) : ${m.write_rate_limited && m.write_rate_limited.values ? m.write_rate_limited.values.count : 0}`,
        '',
    ];
    return lines.join('\n');
}
