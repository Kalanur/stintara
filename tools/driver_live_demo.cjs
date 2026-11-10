// Wholly synthetic fixtures and a real-browser gate for the Driver Live page (server/static/live.html).
// No real captures, no live server: the page is served from the repository and /api/ is answered by this file.
// Run: node tools/driver_live_demo.cjs --viewports [--browser msedge|chrome]
// Needs Playwright (developer/CI tooling only, deliberately not a project dependency).
const fs = require('node:fs');
const path = require('node:path');
const assert = require('node:assert/strict');
const id = n => `00000000-0000-7000-8000-${String(n).padStart(12,'0')}`;

const sessionA = {id:id(900),track_name:'Synthetic Circuit A',session_type:'Race',simulator_provider:'lmu',
  weather:{current:{air_temp_c:21.5,track_temp_c:30.1,rain_intensity:0,wetness_avg:0,cloudiness:.2,wind_speed_mps:2.1},
    forecast:{timeline_complete:true,nodes:[0,25,50,75,100].map(p=>({position_percent:p,temperature_c:20+p/25,rain_chance:.1,humidity_percent:50,wind_direction:'N',wind_speed_mps:2}))}}};
const sessionB = {...sessionA,id:id(901),track_name:'Synthetic Circuit B'};

const strategy = {fuel_l:42.3,fuel_avg_consumption_lap:2.84,fuel_laps_remaining:14.9,
  ...Object.fromEntries(['fl','fr','rl','rr'].flatMap((c,i)=>[[`tyre_remaining_${c}`,.82-i*.01],[`tyre_pressure_${c}_kpa`,171+i],
    [`tyre_temp_inner_${c}_c`,84],[`tyre_temp_mid_${c}_c`,88],[`tyre_temp_outer_${c}_c`,91],[`tyre_brake_temp_${c}_c`,430+i]]))};

function participants(tick, focusPosition = 7, overrides = {}) {
  return Array.from({length:40},(_,i)=>{
    const position = i + 1, own = position === focusPosition;
    return {participant_id:id(i+1),display_name:own?'Synthetic Driver':`Entrant ${position}`,car_name:`Synthetic ${position}`,
      car_class:'HYPERCAR',position,total_laps:own?12:12 - (position > 20 ? 1 : 0),last_lap_time:91.2+(position%5)*.31,
      best_lap_time:90.1+position*.07,gap_to_leader:position===1?0:position*3.2+tick%3*.001,laps_behind_leader:0,
      in_pits:own?false:position===13,pitstops:1,finish_status:0,is_stintara_driver:own,...(own?{last_lap_time:91.512,best_lap_time:90.612,...overrides}:{})};
  });
}

function driverLive(scenario, tick = 0) {
  const base = {driver:{id:id(800),slug:'synthetic',display_name:'Synthetic Driver'},live:true,session:sessionA,strategy};
  const focus = {participant_id:id(7),status:'TRACK',position:7,gap_to_leader:22.4+tick%3*.001,gap_laps:null,interval:3.2,
    completed_laps:12,current_lap:13,last_lap_number:12,last_lap_time:91.512,last_lap:{lap_number:12,lap_time:91.512,sector1:29.101,sector2:34.226,sector3:28.185},
    best_lap_time:90.612,pitstops:1,last_pit_lap:6,player_sector:2,live_age_s:.3};
  switch (scenario) {
    case 'offline': return {driver:base.driver,live:false};
    case 'pit': return {...base,participants:participants(tick,7,{in_pits:true}),focus:{...focus,status:'PIT',player_sector:null}};
    case 'stale': return {...base,participants:participants(tick),focus:{...focus,live_age_s:42}};
    case 'unknown': return {...base,participants:participants(tick,7,{last_lap_time:null,best_lap_time:null,total_laps:null}),
      focus:{...focus,status:null,position:null,gap_to_leader:null,interval:null,completed_laps:null,current_lap:null,last_lap_number:null,last_lap_time:null,last_lap:null,
        best_lap_time:null,pitstops:null,last_pit_lap:null,player_sector:null}};
    case 'fin': case 'dnf': case 'dsq': {
      const code = {fin:1,dnf:2,dsq:3}[scenario];
      return {...base,participants:participants(tick,7,{finish_status:code}),
        focus:{...focus,status:scenario.toUpperCase(),current_lap:null,player_sector:null}};
    }
    case 'lagging': return {...base,participants:participants(tick,7,{total_laps:13,last_lap_time:92.004}),
      focus:{...focus,completed_laps:13,current_lap:14,last_lap_number:13,last_lap_time:92.004,last_lap:null}};
    case 'other-session': return {...base,session:sessionB,participants:participants(tick,3),
      focus:{...focus,participant_id:id(3),position:3,interval:1.1,current_lap:5,completed_laps:4,player_sector:3}};
    default: return {...base,participants:participants(tick),focus};
  }
}

const VIEWPORTS = [[1920,911,'real 1080p display'],[2560,1271,'real 1440p display']];
const PANELS = '.dash > .panel, .side-col > .panel';

async function verifyDriverLive(channel) {
  const {chromium} = require('playwright');
  const repo = path.resolve(__dirname,'..'), output = path.join(repo,'build');
  fs.mkdirSync(output,{recursive:true});
  const html = fs.readFileSync(path.join(repo,'server/static/live.html'),'utf8')
    .replace('__DRIVER_SLUG__',JSON.stringify('synthetic'))
    .replace('__WEATHER_UI__',fs.readFileSync(path.join(repo,'server/static/weather.js'),'utf8'))
    .replace('__BUILD_LABEL__','Stintara · synthetic demonstration');
  const browser = await chromium.launch({headless:true,...(channel?{channel}:{})});
  const page = await browser.newPage({viewport:{width:1920,height:911},reducedMotion:'reduce'});
  const problems = [], state = {scenario:'green',tick:0,status:200,requests:0};
  page.on('console',m=>{ if (m.type()==='error' && !/status of 500/.test(m.text())) problems.push(`console: ${m.text()}`); });
  page.on('pageerror',e=>problems.push(`pageerror: ${e.message}`));
  await page.route('**/*',route => {
    if (!route.request().url().includes('/api/')) return route.fulfill({contentType:'text/html',body:html});
    state.requests++;
    if (state.status !== 200) return route.fulfill({status:state.status,json:{}});
    return route.fulfill({json:driverLive(state.scenario,++state.tick)});
  });
  const text = () => page.locator('#root').innerText();
  const measure = () => page.evaluate(panelSelector => {
    const box = e => e.getBoundingClientRect();
    const panels = [...document.querySelectorAll(panelSelector)];
    const overlaps = [];
    for (let i = 0; i < panels.length; i++) for (let j = i + 1; j < panels.length; j++) {
      const a = box(panels[i]), b = box(panels[j]);
      if (a.left < b.right - 1 && b.left < a.right - 1 && a.top < b.bottom - 1 && b.top < a.bottom - 1) overlaps.push(`${i}x${j}`);
    }
    const clipped = [...document.querySelectorAll('.focus-strip .card, .focus-strip .big, .focus-strip .small, .focus-strip .label, .sector')]
      .filter(e => e.scrollWidth > e.clientWidth + 1).map(e => e.className + ':' + e.textContent.trim().slice(0, 20));
    const small = [...document.querySelectorAll('.dash *')].filter(e => e.children.length === 0 && e.textContent.trim() &&
      parseFloat(getComputedStyle(e).fontSize) < 12).map(e => e.tagName + ':' + e.textContent.trim().slice(0, 20));
    const side = document.querySelector('.side-col');
    const sectors = [...document.querySelectorAll('.sector')].map(e => box(e));
    return {docW:document.documentElement.scrollWidth,docH:document.documentElement.scrollHeight,overlaps,clipped,small:small.slice(0,5),
      sideHidden:side.scrollHeight - side.clientHeight,panelCount:panels.length,
      bottom:Math.max(...panels.map(p => box(p).bottom)),sectorsVisible:sectors.length === 3 && sectors.every(r => r.width > 40 && r.bottom <= innerHeight),
      rows:document.querySelectorAll('#standings-scroll tbody tr').length,
      standingsOverflow:document.querySelector('#standings-scroll').scrollHeight > document.querySelector('#standings-scroll').clientHeight};
  }, PANELS);
  try {
    await page.goto('http://stintara.example.invalid/drivers/synthetic/live');
    await page.locator('.focus-strip').waitFor();
    for (const [width,height,label] of VIEWPORTS) {
      await page.setViewportSize({width,height});
      await page.waitForTimeout(700);
      const m = await measure(), where = `${width}x${height} (${label})`;
      assert.ok(m.docW <= width,`${where}: no horizontal scrolling`);
      assert.ok(m.docH <= height,`${where}: no document scrolling (${m.docH} > ${height})`);
      assert.equal(m.panelCount,5,`${where}: header, focus, standings, tyres, weather`);
      assert.deepEqual(m.overlaps,[],`${where}: panels overlap`);
      assert.deepEqual(m.clipped,[],`${where}: clipped focus text`);
      assert.deepEqual(m.small,[],`${where}: text below 12 px`);
      assert.ok(m.sideHidden <= 1,`${where}: tyres/weather column cut by ${m.sideHidden}px`);
      assert.ok(m.bottom <= height,`${where}: panels end inside the viewport`);
      assert.equal(m.sectorsVisible,true,`${where}: S1/S2/S3 visible`);
      assert.ok(m.standingsOverflow,`${where}: standings scroll internally, not the page`);
      const t = await text();
      for (const expected of ['TRACK','P7','+22.4','13','12 completed','1:31.512','1:30.612','29.101','34.226','28.185','S2 · now','1 stop · last stop L6','42.3 L','82.0%'])
        assert.ok(t.includes(expected),`${where}: shows ${expected}`);
      await page.screenshot({path:path.join(output,`driver-live-${width}x${height}.png`),fullPage:true});
      console.log(JSON.stringify({where,docH:m.docH,rows:m.rows,sideHidden:m.sideHidden}));
    }

    await page.setViewportSize({width:1920,height:911});
    // 500 ms refresh keeps the open disclosure, keyboard focus and the standings scroll position, while really rebuilding the DOM.
    await page.locator('#weather-forecast > summary').click();
    await page.evaluate(() => { window.previousFocusCard = document.querySelector('.focus-strip'); document.querySelector('#standings-scroll').scrollTop = 160; });
    const scrolled = await page.evaluate(() => document.querySelector('#standings-scroll').scrollTop);
    assert.ok(scrolled > 0,'standings scrolled');
    await page.waitForFunction(() => !window.previousFocusCard.isConnected,null,{timeout:4000});
    assert.equal(await page.locator('#weather-forecast').evaluate(e => e.open),true,'details stay open across the refresh');
    assert.equal(await page.evaluate(() => document.activeElement?.parentElement?.id),'weather-forecast','summary keeps focus');
    assert.equal(await page.evaluate(() => document.querySelector('#standings-scroll').scrollTop),scrolled,'scroll position kept');
    await page.locator('#weather-forecast > summary').click();
    // The open forecast must not push the page into scrolling or overlap at the reference viewport.
    await page.locator('#weather-forecast > summary').click();
    const open = await measure();
    assert.ok(open.docH <= 911 && !open.overlaps.length,'open forecast: no document scrolling or overlap');
    await page.locator('#weather-forecast > summary').click();

    // Scenarios: each is a complete replacement of the fixture; none may leave placeholders or errors behind.
    const settle = async scenario => { state.scenario = scenario; const before = state.requests; await page.waitForFunction(() => true);
      while (state.requests < before + 2) await page.waitForTimeout(100); await page.waitForTimeout(150); };
    await settle('pit'); assert.match(await text(),/PIT/); assert.doesNotMatch(await text(),/S2 · now/);
    for (const [scenario,label] of [['fin','FIN'],['dnf','DNF'],['dsq','DSQ']]) {
      await settle(scenario);
      assert.ok(await page.locator(`.focus-strip .badge:text-is("${label}")`).count() === 1,`${label} badge`);
      assert.doesNotMatch(await text(),/S2 · now/,`${label}: no current sector`);
      const m = await measure(); assert.ok(m.docH <= 911 && !m.overlaps.length,`${label}: layout intact`);
    }
    await settle('lagging');
    const lagging = await text();
    assert.ok(lagging.includes('1:32.004') && lagging.includes('L13'),'live time and number shown');
    assert.doesNotMatch(lagging,/29\.101|34\.226|28\.185|Last timed lap sectors · lap/,'no sectors of another lap');
    await settle('stale');
    assert.match(await text(),/stale or unavailable/); assert.doesNotMatch(await text(),/S2 · now/);
    let m = await measure(); assert.ok(m.docH <= 911 && !m.overlaps.length,'stale banner: no document scrolling or overlap');
    await settle('unknown');
    const unknown = await text();
    assert.doesNotMatch(unknown,/NaN|undefined|null|0:00|\bP\d/); assert.match(unknown,/Stops —/);
    assert.equal(await page.locator('.sector.now').count(),0);
    m = await measure(); assert.ok(m.docH <= 911 && !m.overlaps.length,'unknown values: layout intact');
    await page.screenshot({path:path.join(output,'driver-live-unknown.png'),fullPage:true});

    // live:false, then recovery; a failing endpoint, then recovery.
    await settle('offline');
    assert.match(await text(),/Offline/); assert.equal(await page.locator('.focus-strip').count(),0);
    await settle('green'); await page.locator('.focus-strip').waitFor(); assert.match(await text(),/TRACK/);
    state.status = 500; await page.waitForFunction(() => /Live data unavailable/.test(document.querySelector('#root').textContent),null,{timeout:4000});
    state.status = 200; await page.locator('.focus-strip').waitFor({timeout:4000});

    // Switching to another session replaces everything, including the full-field link.
    await settle('other-session');
    const switched = await text();
    assert.match(switched,/Synthetic Circuit B/); assert.doesNotMatch(switched,/Synthetic Circuit A/);
    assert.ok(switched.includes('P3') && switched.includes('+1.100 s'),'focus follows the new session');
    assert.match(await page.locator('#full-field-link').getAttribute('href'),new RegExp(`${id(901)}/live\\?focus=${id(3)}$`));
    await settle('green');
    assert.match(await text(),/Synthetic Circuit A/);
    assert.match(await page.locator('#full-field-link').getAttribute('href'),new RegExp(`${id(900)}/live\\?focus=${id(7)}$`));
    assert.deepEqual(problems,[],'no JavaScript console errors');
    console.log('driver live gate passed');
  } catch (error) {
    await page.screenshot({path:path.join(output,'driver-live-failure.png'),fullPage:true}).catch(() => {});
    throw error;
  } finally { await browser.close(); }
}

module.exports = {driverLive};
if (require.main === module) {
  if (process.argv.includes('--viewports')) {
    const index = process.argv.indexOf('--browser');
    verifyDriverLive(index < 0 ? undefined : process.argv[index+1]).catch(error => { console.error(error); process.exitCode = 1; });
  } else if (process.argv.includes('--json')) console.log(JSON.stringify(driverLive('green'),null,2));
  else { console.error('Use --viewports [--browser msedge|chrome] or --json.'); process.exitCode = 1; }
}
