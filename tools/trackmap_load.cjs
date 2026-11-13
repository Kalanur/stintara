#!/usr/bin/env node
// Multi-viewer load probe for the Track Map endpoints (synthetic data only; never point it at a real session).
//
//   node tools/trackmap_load.cjs --base http://127.0.0.1:8099 --session <uuid> --viewers 10 --seconds 20
//        [--endpoint vehicles|live] [--interval-ms 250] [--json]
//
// Every viewer polls like the page does: one request in flight, the next one after the interval. It reports request
// rate, errors and response time per request as seen by the client. Server-side cost (CPU, PostgreSQL transactions)
// is not visible from here; docs/TRACKMAP_RACE_CONTROL.md describes how it was sampled around a run.
'use strict';

function arg(name, fallback) {
  const i = process.argv.indexOf(`--${name}`);
  return i < 0 ? fallback : process.argv[i + 1];
}
const percentile = (sorted, p) => sorted.length ? sorted[Math.min(sorted.length - 1, Math.floor(p * (sorted.length - 1) + 0.5))] : null;
const round = x => x === null ? null : Math.round(x * 10) / 10;

function summarise(latencies, errors, seconds, viewers) {
  const sorted = latencies.slice().sort((a, b) => a - b);
  return {
    viewers, seconds, requests: sorted.length, errors,
    requests_per_s: round(sorted.length / seconds),
    response_ms: { median: round(percentile(sorted, 0.5)), p95: round(percentile(sorted, 0.95)), max: round(sorted[sorted.length - 1] ?? null) },
  };
}

async function main() {
  const base = arg('base', 'http://127.0.0.1:8099'), session = arg('session');
  const viewers = Number(arg('viewers', 10)), seconds = Number(arg('seconds', 20)), interval = Number(arg('interval-ms', 250));
  const endpoint = arg('endpoint', 'vehicles');
  if (!session) throw new Error('--session is required');
  if (!['vehicles', 'live'].includes(endpoint)) throw new Error('--endpoint must be vehicles or live');
  const url = `${base}/api/sessions/${encodeURIComponent(session)}/${endpoint}`;
  const latencies = []; let errors = 0;
  const end = performance.now() + seconds * 1000;
  const viewer = async () => {
    await new Promise(r => setTimeout(r, Math.random() * interval));   // viewers are not phase-locked
    while (performance.now() < end) {
      const t0 = performance.now();
      try {
        const response = await fetch(url, { cache: 'no-store' });
        await response.arrayBuffer();
        if (response.ok) latencies.push(performance.now() - t0); else errors++;
      } catch { errors++; }
      await new Promise(r => setTimeout(r, Math.max(0, interval - (performance.now() - t0))));
    }
  };
  await Promise.all(Array.from({ length: viewers }, viewer));
  const report = { endpoint, interval_ms: interval, ...summarise(latencies, errors, seconds, viewers) };
  console.log(process.argv.includes('--json') ? JSON.stringify(report) : JSON.stringify(report, null, 2));
}

module.exports = { summarise };
if (require.main === module) main().catch(error => { console.error(error.message); process.exitCode = 1; });
