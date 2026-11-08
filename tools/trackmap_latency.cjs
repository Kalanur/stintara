#!/usr/bin/env node
// Local Track Map latency probe: how old is a vehicle position when the browser shows it?
//
//   node tools/trackmap_latency.cjs --base http://127.0.0.1:8099 --session <session-uuid> [--seconds 40]
//        [--chrome <path to chrome/edge>] [--json]
//
// Needs a Stintara server that is receiving a *synthetic* stream (see docs/TRACKMAP_RACE_CONTROL.md, "Latency probe":
// `python -m tools.replay.scenarios latency-probe` -> compile -> stintara_replay). Never point it at real sessions.
// It drives a headless Chrome through the DevTools protocol (no extra packages) and records, inside the page:
//   * every response of the session poll (request start/end, `vehicles_age_s`, first vehicle x),
//   * the next DOM mutation and the next animation frame after each response.
// Clock rules: differences are only taken between values of the same clock. `vehicles_age_s` is a server-side
// duration and is never subtracted from a browser timestamp. The scoring cadence itself is not measurable from a
// 2 Hz poll; it follows from the compiler's agent throttle (0.2 s) and shows up as the maximum server age.
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

async function main() {
  const base = arg('base', 'http://127.0.0.1:8099'), session = arg('session'), seconds = Number(arg('seconds', 40));
  if (!session) throw new Error('--session is required');
  const chromePath = arg('chrome', process.env.CHROME || (process.platform === 'win32' ? '' : 'google-chrome'));
  if (!chromePath) throw new Error('pass --chrome <path to chrome.exe or msedge.exe> (or set CHROME) on Windows');
  const port = 9340 + Math.floor(Math.random() * 400);
  const profile = fs.mkdtempSync(path.join(os.tmpdir(), 'stintara-latency-'));
  const chrome = spawn(chromePath, ['--headless=new', '--disable-gpu', `--remote-debugging-port=${port}`,
    '--window-size=1920,911', `--user-data-dir=${profile}`, 'about:blank'], { stdio: 'ignore' });
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
    await send('Emulation.setDeviceMetricsOverride', { width: 1920, height: 911, deviceScaleFactor: 1, mobile: false });
    await send('Page.addScriptToEvaluateOnNewDocument', { source: `(() => {
      const probe = window.__probe = { resp: [], dom: [], paint: [] };
      const epoch = () => performance.timeOrigin + performance.now();
      const realFetch = window.fetch.bind(window);
      window.fetch = async (...args) => {
        const t0 = epoch(), response = await realFetch(...args), t1 = epoch();
        if (String(args[0]).match(/\\/api\\/sessions\\/[^/]+\\/live/)) {
          response.clone().json().then(d => { const v = (d.track_map && d.track_map.vehicles) || [];
            probe.resp.push({ t0, t1, age: d.track_map ? d.track_map.vehicles_age_s : null, x: v.length ? v[0].x : null }); }).catch(() => {});
        }
        return response;
      };
      new MutationObserver(() => { probe.dom.push(epoch()); requestAnimationFrame(() => probe.paint.push(epoch())); })
        .observe(document, { subtree: true, childList: true });
    })();` });
    await send('Page.navigate', { url: `${base}/sessions/${encodeURIComponent(session)}/live` });
    await sleep(seconds * 1000);
    const result = await send('Runtime.evaluate', { returnByValue: true, expression: 'JSON.stringify(window.__probe)' });
    const probe = JSON.parse(result.result.value);
    const report = analyse(probe.resp.filter(r => r.age !== null), probe.dom, probe.paint);
    console.log(process.argv.includes('--json') ? JSON.stringify(report) : JSON.stringify(report, null, 2));
    ws.close();
  } finally {
    chrome.kill();
    try { fs.rmSync(profile, { recursive: true, force: true }); } catch { /* the browser may still hold the profile */ }
  }
}

module.exports = { analyse, stats };
if (require.main === module) main().catch(error => { console.error(error.message); process.exitCode = 1; });
