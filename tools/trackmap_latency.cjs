#!/usr/bin/env node
// Local Track Map latency probe: how old is a vehicle position when the browser shows it, and how smoothly does it move?
//
//   node tools/trackmap_latency.cjs --base http://127.0.0.1:8099 --session <session-uuid> [--seconds 40] [--warmup 6]
//        [--viewport 1920x911] [--latency <ms added to every request>] [--legacy] [--chrome <path>] [--json]
//
// Needs a Stintara server that is receiving a *synthetic* stream (see docs/TRACKMAP_RACE_CONTROL.md, "Latency probe":
// `python -m tools.replay.scenarios latency-probe[-40]` -> compile -> stintara_replay). Never point it at real sessions.
// It drives a headless Chrome through the DevTools protocol (no extra packages) and records, inside the page:
//   * every response of the session poll (/live) and of the vehicles endpoint (/vehicles): request start/end,
//     `vehicles_age_s`, first vehicle x,
//   * every change of the *drawn* vehicle positions (a MutationObserver reads the first dots of `.map-area`), and the
//     next animation frame after it. Other dashboard updates that leave the dots unchanged are not counted.
// `--legacy` hides the server's `vehicle_refresh_ms` advertisement from the page so the same server and the same
// session can be measured with the full-/live-only behaviour (before) and with the vehicles refresh (after).
// `--latency` adds artificial request latency through the DevTools network emulation (not a real network).
// Clock rules: differences are only taken between values of the same clock. `vehicles_age_s` is a server-side
// duration and is never subtracted from a browser timestamp; it is added to a browser-side elapsed time instead.
'use strict';
const { spawn } = require('node:child_process');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');

function arg(name, fallback) {
  const i = process.argv.indexOf(`--${name}`);
  return i < 0 ? fallback : process.argv[i + 1];
}
const stats = values => {
  const v = values.filter(Number.isFinite).sort((a, b) => a - b);
  if (!v.length) return null;
  const q = p => v[Math.min(v.length - 1, Math.floor(p * (v.length - 1) + 0.5))];
  return { n: v.length, mean: v.reduce((a, b) => a + b, 0) / v.length, median: q(0.5), p95: q(0.95), max: v[v.length - 1] };
};
const round = o => o && Object.fromEntries(Object.entries(o).map(([k, x]) => [k, typeof x === 'number' ? Math.round(x * 10) / 10 : x]));

// Pure analysis of the recorded samples (exported for the unit test).
function analyse(resp, dom, paint) {
  const render = [], painted = [];
  for (const r of resp) {
    const d = dom.find(t => t >= r.t1);
    if (d !== undefined) render.push(d - r.t1);
    const p = paint.find(t => t >= r.t1);
    if (p !== undefined) painted.push(p - r.t1);
  }
  const polls = resp.slice(1).map((r, i) => r.t0 - resp[i].t0);
  // The page polls slower than scoring arrives, so individual messages cannot be counted from polls; the age of the
  // newest data at each poll (below) is what reveals the arrival cadence (its maximum is about one message interval).
  const changed = resp.filter((r, k) => k > 0 && r.x !== null && r.x !== resp[k - 1].x).length;
  const ages = resp.map(r => r.age * 1000);
  const fetchMs = resp.map(r => r.t1 - r.t0);
  const shown = resp.map((r, i) => r.age * 1000 + (painted[i] ?? render[i] ?? 0));
  const pollMean = polls.length ? polls.reduce((a, b) => a + b, 0) / polls.length : null;
  return {
    responses: resp.length,
    polls_with_new_position: changed,
    server_age_ms: round(stats(ages)),
    poll_interval_ms: round(stats(polls)),
    fetch_ms: round(stats(fetchMs)),
    dom_after_response_ms: round(stats(render)),
    paint_after_response_ms: round(stats(painted)),
    age_when_painted_ms: round(stats(shown)),
    // Model, not a measurement: a viewer looks at the last painted poll result, which keeps ageing until the next poll.
    expected_age_at_random_instant_ms: pollMean === null ? null : Math.round(stats(shown).mean + pollMean / 2),
    worst_case_age_ms: pollMean === null ? null : Math.round(stats(shown).max + pollMean),
  };
}

// Map-specific analysis. `resp`: responses of either endpoint {t0, t1, age (s), kind}. `steps`: changes of the drawn
// dots {t (mutation), p (next animation frame)}; all browser times (ms, one clock). For a viewer looking at the map
// at instant τ the displayed data is the newest painted step before τ; its age is the server age of the response
// that produced it (a duration) plus the browser time elapsed since that response arrived. Sampled every 5 ms.
function analyseMap(resp, steps, windowMs) {
  const byTime = resp.slice().sort((a, b) => a.t1 - b.t1);
  const shown = [];
  for (const step of steps) {
    let source = null;
    for (const r of byTime) { if (r.t1 <= step.t) source = r; else break; }
    if (source) shown.push({ paint: step.p ?? step.t, base: source.age * 1000, t1: source.t1 });
  }
  const gaps = shown.slice(1).map((s, i) => s.paint - shown[i].paint);
  const instant = [];
  for (let k = 0, t = shown.length ? shown[0].paint : 0; shown.length && t < shown[shown.length - 1].paint; t += 5) {
    while (k + 1 < shown.length && shown[k + 1].paint <= t) k++;
    instant.push(shown[k].base + (t - shown[k].t1));
  }
  return {
    visible_updates: steps.length,
    visible_updates_per_s: windowMs > 0 ? Math.round(steps.length / (windowMs / 1000) * 10) / 10 : null,
    step_interval_ms: round(stats(gaps)),
    age_at_paint_ms: round(stats(shown.map(s => s.base + (s.paint - s.t1)))),
    age_at_random_instant_ms: round(stats(instant)),
  };
}

async function main() {
  const base = arg('base', 'http://127.0.0.1:8099'), session = arg('session'), seconds = Number(arg('seconds', 40));
  const warmup = Number(arg('warmup', 6)), legacy = process.argv.includes('--legacy'), extraLatency = Number(arg('latency', 0));
  const [width, height] = arg('viewport', '1920x911').split('x').map(Number);
  if (!session) throw new Error('--session is required');
  const chromePath = arg('chrome', process.env.CHROME || (process.platform === 'win32' ? '' : 'google-chrome'));
  if (!chromePath) throw new Error('pass --chrome <path to chrome.exe or msedge.exe> (or set CHROME) on Windows');
  const port = 9340 + Math.floor(Math.random() * 400);
  const profile = fs.mkdtempSync(path.join(os.tmpdir(), 'stintara-latency-'));
  const chrome = spawn(chromePath, ['--headless=new', '--disable-gpu', `--remote-debugging-port=${port}`,
    `--window-size=${width},${height}`, `--user-data-dir=${profile}`, 'about:blank'], { stdio: 'ignore' });
  const sleep = ms => new Promise(r => setTimeout(r, ms));
  try {
    let page;
    for (let i = 0; i < 60 && !page; i++) {
      try { page = (await (await fetch(`http://127.0.0.1:${port}/json`)).json()).find(t => t.type === 'page'); } catch { /* starting */ }
      if (!page) await sleep(250);
    }
    if (!page) throw new Error('browser did not start');
    const ws = new WebSocket(page.webSocketDebuggerUrl);
    await new Promise(r => ws.addEventListener('open', r));
    let id = 0; const pending = new Map();
    ws.addEventListener('message', e => { const m = JSON.parse(e.data); if (m.id && pending.has(m.id)) { pending.get(m.id)(m.result); pending.delete(m.id); } });
    const send = (method, params = {}) => new Promise(r => { const i = ++id; pending.set(i, r); ws.send(JSON.stringify({ id: i, method, params })); });
    await send('Page.enable');
    await send('Emulation.setDeviceMetricsOverride', { width, height, deviceScaleFactor: 1, mobile: false });
    if (extraLatency > 0) {
      await send('Network.enable');
      await send('Network.emulateNetworkConditions', { offline: false, latency: extraLatency, downloadThroughput: -1, uploadThroughput: -1 });
    }
    await send('Page.addScriptToEvaluateOnNewDocument', { source: `(() => {
      const legacy = ${legacy ? 'true' : 'false'};
      const probe = window.__probe = { resp: [], steps: [] };
      const epoch = () => performance.timeOrigin + performance.now();
      const realFetch = window.fetch.bind(window);
      window.fetch = async (...args) => {
        const t0 = epoch(), response = await realFetch(...args), t1 = epoch();
        const url = String(args[0]);
        const full = /\\/api\\/sessions\\/[^/]+\\/live/.test(url), light = /\\/api\\/sessions\\/[^/]+\\/vehicles/.test(url);
        if (!full && !light) return response;
        const body = await response.clone().json().catch(() => null);
        if (body) {
          const track = full ? body.track_map : body;
          const v = (track && track.vehicles) || [];
          if (track && track.vehicles_age_s !== null && track.vehicles_age_s !== undefined) {
            probe.resp.push({ t0, t1, kind: full ? 'live' : 'vehicles', age: track.vehicles_age_s, x: v.length ? v[0].x : null });
          }
          if (full && legacy && track) {
            delete track.vehicle_refresh_ms;
            return new Response(JSON.stringify(body), { status: response.status, headers: response.headers });
          }
        }
        return response;
      };
      let last = null;
      const signature = () => Array.from(document.querySelectorAll('.map-area circle')).slice(0, 4)
        .map(c => c.getAttribute('cx') + ',' + c.getAttribute('cy')).join(';');
      new MutationObserver(() => {
        const sig = signature();
        if (!sig || sig === last) return;
        last = sig;
        const step = { t: epoch(), p: null };
        probe.steps.push(step);
        requestAnimationFrame(() => { step.p = epoch(); });
      }).observe(document, { subtree: true, childList: true, attributes: true });
    })();` });
    await send('Page.navigate', { url: `${base}/sessions/${encodeURIComponent(session)}/live` });
    await sleep((seconds + warmup) * 1000);
    const result = await send('Runtime.evaluate', { returnByValue: true, expression: 'JSON.stringify(window.__probe)' });
    const probe = JSON.parse(result.result.value);
    const start = (probe.resp[0] ? probe.resp[0].t0 : 0) + warmup * 1000;   // warm-up: page load, first renders, JIT
    const resp = probe.resp.filter(r => r.t0 >= start), steps = probe.steps.filter(s => s.t >= start && s.p !== null);
    const primary = resp.filter(r => r.kind === (resp.some(x => x.kind === 'vehicles') ? 'vehicles' : 'live'));
    const report = {
      mode: legacy ? 'legacy (full /live polling only)' : 'auto (as advertised by the server)',
      viewport: `${width}x${height}`, added_latency_ms: extraLatency, window_s: seconds,
      requests_per_s: { live: resp.filter(r => r.kind === 'live').length / seconds, vehicles: resp.filter(r => r.kind === 'vehicles').length / seconds },
      ...analyse(primary, steps.map(s => s.t), steps.map(s => s.p)),
      map: analyseMap(resp, steps, seconds * 1000),
    };
    console.log(process.argv.includes('--json') ? JSON.stringify(report) : JSON.stringify(report, null, 2));
    ws.close();
  } finally {
    chrome.kill();
    try { fs.rmSync(profile, { recursive: true, force: true }); } catch { /* the browser may still hold the profile */ }
  }
}

module.exports = { analyse, analyseMap, stats };
if (require.main === module) main().catch(error => { console.error(error.message); process.exitCode = 1; });
