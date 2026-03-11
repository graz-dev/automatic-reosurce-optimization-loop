import http from 'k6/http';
import { check, sleep } from 'k6';

// ---------------------------------------------------------------------------
// Configuration – override via environment variables:
//
//   PEAK_MULTIPLIER   scales all RPS values (default: 1.0 → peak = 300 RPS)
//                     e.g. PEAK_MULTIPLIER=0.5  → peak = 150 RPS
//                          PEAK_MULTIPLIER=2.0  → peak = 600 RPS
//
//   CYCLE_MINUTES     total duration of the compressed diurnal cycle
//                     (default: 30 minutes)
//                     e.g. CYCLE_MINUTES=60  → 1-hour cycle
//                          CYCLE_MINUTES=10  → 10-minute quick run
//
// Example:
//   BASE_URL=http://localhost:8080 PEAK_MULTIPLIER=0.5 CYCLE_MINUTES=60 \
//     k6 run diurnal-load-test.js
// ---------------------------------------------------------------------------

const BASE_URL      = __ENV.BASE_URL       || 'http://localhost:8080';
const PEAK_MULT     = parseFloat(__ENV.PEAK_MULTIPLIER || '1.0');
const CYCLE_MINUTES = parseFloat(__ENV.CYCLE_MINUTES   || '10');

const BASE_PEAK_RPS = 300;

// ---------------------------------------------------------------------------
// Diurnal shape anchors: [cumulative cycle fraction, fraction of peak RPS]
//
// Compressed 24-hour traffic pattern (real-world hour → CYCLE_MINUTES):
//   00:00-06:00  night trough    ~5 % of peak
//   06:00-09:00  morning ramp-up
//   09:00-12:00  morning peak    100 % of peak
//   12:00-14:00  midday dip       60 % of peak
//   14:00-17:00  afternoon peak  100 % of peak
//   17:00-20:00  evening ramp-down
//   20:00-24:00  night trough    ~5 % of peak
//
// A sine-wave is used to interpolate smoothly between anchors so that
// the load generator produces natural-looking peaks and valleys instead
// of hard step changes, triggering meaningful CPU/memory oscillations.
// ---------------------------------------------------------------------------

// Build a smooth sine-wave stage list between shape anchors.
// Between each pair of anchors the RPS follows a half-sine arc so the
// transition is smooth rather than linear.
function buildSineStages(shape, cycleMinutes, peakRps) {
  const stages = [];
  const SINE_STEPS = 6; // interpolation points per segment
  for (let i = 1; i < shape.length; i++) {
    const [prevFrac, prevRpsFrac] = shape[i - 1];
    const [curFrac,  curRpsFrac]  = shape[i];
    const segDurSec = (curFrac - prevFrac) * cycleMinutes * 60;
    const stepSec   = Math.max(1, Math.round(segDurSec / SINE_STEPS));
    for (let s = 1; s <= SINE_STEPS; s++) {
      // Sine interpolation: 0 → π maps prevRpsFrac → curRpsFrac
      const t          = s / SINE_STEPS;
      const sineFactor = (1 - Math.cos(Math.PI * t)) / 2;
      const rpsFrac    = prevRpsFrac + (curRpsFrac - prevRpsFrac) * sineFactor;
      stages.push({
        target:   Math.max(1, Math.round(rpsFrac * peakRps)),
        duration: `${stepSec}s`,
      });
    }
  }
  return stages;
}

// Each entry: [cumulative cycle fraction, RPS fraction of peak]
const SHAPE = [
  [ 0/24, 0.05],  // 00:00 – night trough
  [ 6/24, 0.05],  // 06:00 – end of night trough
  [ 9/24, 1.00],  // 09:00 – morning peak
  [12/24, 0.60],  // 12:00 – midday dip
  [14/24, 0.60],  // 14:00 – start of afternoon ramp
  [17/24, 1.00],  // 17:00 – afternoon peak
  [20/24, 0.30],  // 20:00 – evening ramp-down
  [24/24, 0.05],  // 24:00 – back to night trough
];


const PEAK_RPS = Math.round(BASE_PEAK_RPS * PEAK_MULT);
const STAGES   = buildSineStages(SHAPE, CYCLE_MINUTES, PEAK_RPS);

// VU headroom: peak_RPS × assumed p99 latency (2s) with a safety buffer
const MAX_VUS = Math.ceil(PEAK_RPS * 2 * 1.5);

console.log(`Diurnal cycle: ${CYCLE_MINUTES} min | Peak RPS: ${PEAK_RPS} | Max VUs: ${MAX_VUS}`);

export const options = {
  noConnectionReuse: true,  // Ensure load balancing across pods

  scenarios: {
    diurnal: {
      executor:        'ramping-arrival-rate',
      startRate:       Math.max(1, Math.round(SHAPE[0][1] * PEAK_RPS)),
      timeUnit:        '1s',
      preAllocatedVUs: Math.ceil(MAX_VUS * 0.2),
      maxVUs:          MAX_VUS,
      stages:          STAGES,
    },
  },

  thresholds: {
    http_req_duration: ['p(95)<500', 'p(99)<1000'],
    http_req_failed:   ['rate<0.01'],
  },
};

// ---------------------------------------------------------------------------
// Workload – weighted endpoint pool (Spring PetClinic defaults)
// ---------------------------------------------------------------------------

const ENDPOINTS = [
  { url: '/',              weight: 30 },
  { url: '/owners',        weight: 20 },
  { url: '/owners/find',   weight: 15 },
  { url: '/owners/1',      weight: 10 },
  { url: '/pets/new',      weight:  5 },
  { url: '/vets.html',     weight: 10 },
  { url: '/actuator/health', weight: 10 },
];

const WEIGHTED_POOL = ENDPOINTS.flatMap(e => Array(e.weight).fill(e.url));

function pickEndpoint() {
  return WEIGHTED_POOL[Math.floor(Math.random() * WEIGHTED_POOL.length)];
}

export default function () {
  const url = `${BASE_URL}${pickEndpoint()}`;

  const res = http.get(url, {
    tags:    { name: url },
    timeout: '10s',
  });

  check(res, {
    'status 2xx':     r => r.status >= 200 && r.status < 300,
    'duration <500ms': r => r.timings.duration < 500,
  });

  if (res.status >= 500) sleep(0.5);
}
