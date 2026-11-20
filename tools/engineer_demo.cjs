// Wholly synthetic API presentation fixture. No real captures or production demo endpoint.
// Run: node tools/engineer_demo.cjs --json
// Optional browser check: node tools/engineer_demo.cjs --screenshot --browser msedge
// Real-viewport / orientation check: node tools/engineer_demo.cjs --viewports --browser msedge
const fs = require('node:fs');
const path = require('node:path');
const assert = require('node:assert/strict');
const epoch = '2026-01-01T12:00:00.000Z';
const id = n => `00000000-0000-7000-8000-${String(n).padStart(12,'0')}`;

// Synthetic stadium-shaped circuit (no real track data). Lap distance in metres, world plane (x, z).
const TRACK_LENGTH = 3000;
function circuitPoint(distance) {
  const d = ((distance % TRACK_LENGTH) + TRACK_LENGTH) % TRACK_LENGTH;
  const straight = TRACK_LENGTH * 0.3, radius = TRACK_LENGTH * 0.2 / Math.PI, arc = Math.PI * radius;
  if (d < straight) return [d, -radius];
  if (d < straight + arc) { const a = -Math.PI / 2 + (d - straight) / radius; return [straight + radius * Math.cos(a), radius * Math.sin(a)]; }
  if (d < 2 * straight + arc) return [straight - (d - straight - arc), radius];
  const a = Math.PI / 2 + (d - 2 * straight - arc) / radius;
  return [radius * Math.cos(a), radius * Math.sin(a)];
}
// Sector boundary marker as the server derives it: raw-coordinate point plus unit direction on the centerline.
function sectorMarker(boundary, distance, turn) {
  const [x0, z0] = turn(circuitPoint(distance)), [x1, z1] = turn(circuitPoint(distance + 1));
  const length = Math.hypot(x1 - x0, z1 - z0);
  return {boundary, point: [Number(x0.toFixed(1)), Number(z0.toFixed(1))], direction: [(x1 - x0) / length, (z1 - z0) / length]};
}
// Start/finish line as the server derives it from the closing arc (lap distance 0): synthetic point, unit direction, arc length.
function startFinishMarker(turn) {
  const marker = sectorMarker(1, 0, turn);
  return {point: marker.point, direction: marker.direction, arc_m: 24.5, basis: 'lap_distance'};
}
function syntheticTrackMap(participants, trackScenario = 'green') {
  const learning = trackScenario === 'learning';
  const step = 25, distances = [];
  for (let d = 0; d < TRACK_LENGTH * (learning ? 0.63 : 1) - 1; d += step) distances.push(d);
  const flags = {green: ['clear', 'clear', 'clear'], yellow: ['clear', 'local_yellow', 'clear'],
    learning: ['clear', 'local_yellow', 'clear'], fcy: ['clear', 'clear', 'clear'], vertical: ['clear', 'clear', 'clear'],
    stored: ['clear', 'clear', 'clear']}[trackScenario];
  // 'vertical' encodes the same circuit rotated by 90 degrees in the simulator frame; 'stored' is a session without
  // live runtime state (persisted geometry only).
  const turn = ([x, z]) => trackScenario === 'vertical' ? [-z, x] : [x, z];
  const vehicles = participants.map((p, i) => {
    // Traffic is clustered inside S2 for the yellow scenarios so the highlighted sector is meaningful.
    const distance = trackScenario === 'yellow' || trackScenario === 'learning' ? 1100 + (i * 23) % 800 : 2850 - i * 71;
    const [x, z] = turn(circuitPoint(distance));
    return {participant_id: p.id, x: Number(x.toFixed(1)), z: Number(z.toFixed(1)), in_pits: p.in_pits === true,
      position: p.position, name: p.display_name};
  });
  if (trackScenario === 'stored') return {
    geometry: {state: 'complete', complete: true, closed: true, progress: null, length_m: TRACK_LENGTH, source: 'stored',
      points: distances.map(d => circuitPoint(d).map(v => Number(v.toFixed(1)))),
      point_sectors: distances.map(d => d < 1000 ? 1 : d < 2000 ? 2 : 3), sector_boundaries_m: [1000, 2000],
      sector_markers: [sectorMarker(1, 1000, p => p), sectorMarker(2, 2000, p => p)], start_finish_marker: startFinishMarker(p => p)},
    vehicles: [], vehicles_age_s: null, vehicles_fresh: false, race_control: null, static_only: true};
  return {
    geometry: {state: learning ? 'learning' : 'complete', complete: !learning, closed: !learning,
      progress: learning ? 0.63 : null, length_m: TRACK_LENGTH,
      points: distances.map(d => turn(circuitPoint(d)).map(v => Number(v.toFixed(1)))),
      point_sectors: learning ? null : distances.map(d => d < 1000 ? 1 : d < 2000 ? 2 : 3),
      sector_boundaries_m: learning ? null : [1000, 2000],
      sector_markers: learning ? null : [sectorMarker(1, 1000, turn), sectorMarker(2, 2000, turn)],
      start_finish_marker: learning ? null : startFinishMarker(turn)},
    vehicles, vehicles_age_s: 0.2, vehicles_fresh: true,
    race_control: {phase: trackScenario === 'fcy' ? 'full_course_yellow' : 'green', sector_flags: flags,
      safety_car_active: trackScenario === 'fcy', player_sector: 2, diagnostics: {}, age_s: 0.2, fresh: true},
  };
}

function engineerDemo(scenario = 'ready', trackScenario = 'green') {
  const participants = Array.from({length:40}, (_,i) => ({
    id:id(i+1),local_vehicle_id:i,display_name:`Synthetic Driver ${String(i+1).padStart(2,'0')}`,
    vehicle_class:'GT3',entry_name:`Synthetic Entry ${i+1}`,team_name:'Synthetic Team',car_number:null,
    position:i+1,total_laps:28,last_lap_time:102.381+i*.1,best_lap_time:101.904+i*.1,
    gap_to_leader:i*1.3,gap_laps:0,interval:i?1.3:null,interval_preceding_id:i?id(i):null,
    in_pits:i===1,pitstops:0,last_pit_lap:null,finish_status:0,
    sector_lap_number:28,sector1:30.5,sector2:32,sector3:39.881,
  }));
  Object.assign(participants[2],{pitstops:1,last_pit_lap:12});
  Object.assign(participants[3],{pitstops:2,last_pit_lap:27});
  for (const [index,status] of [[4,1],[5,2],[6,3]]) Object.assign(participants[index],{finish_status:status,in_pits:true,pitstops:1});
  Object.assign(participants[7],{pitstops:1}); // Joined after the first stop; lap unknown.
  const session = {id:id(99),track_name:'Synthetic Circuit',session_type:'Race',simulator_provider:'lmu',
    last_seen_at:epoch,started_at:epoch,weather:{
      current:{air_temp_c:21,track_temp_c:29,rain_intensity:.03,wetness_avg:.18,wetness_min:.1,wetness_max:.24,cloudiness:.45,wind_speed_mps:3},
      forecast:{timeline_complete:true,nodes:Array.from({length:5},(_,i)=>({position_percent:i*25,
        temperature_c:21+i,rain_chance:[0,.05,.1,.35,.6][i],humidity_percent:50,wind_direction:'NE',wind_speed_mps:3}))},
    }};
  const strategy={session_id:session.id,participant_id:id(3),simulator_provider:'lmu',status:'stop_required',
    completed_laps:28,finish_projection:{kind:'exact',finish_lap:38},reserve_laps:1,plan_source:'manual',
    resources:[{resource_kind:'fuel',current:70,capacity:100,consumption_per_lap:8.6,range_laps:70/8.6,state:'ready',
      fresh:true,available:true,sample_count:5,units:'L',pit_service_mode:'TARGET_LEVEL'},
      {resource_kind:'energy',energy_kind:'VIRTUAL_ENERGY',current:.63,capacity:1,consumption_per_lap:.102,
        range_laps:.63/.102,state:'ready',fresh:true,available:true,sample_count:5,units:'fraction',pit_service_mode:'TARGET_LEVEL'}],
    limiting_resource:'energy',effective_range_laps:.63/.102,latest_safe_stop_lap:33,planned_stop_lap:32,
    planned_stop_safe:true,shortfall:[],additional_stop_required:false,
    at_pit:[{resource_kind:'fuel',amount:35.6},{resource_kind:'energy',amount:.222}],
    next_stint_requirements:[{resource_kind:'fuel',amount:60.2},{resource_kind:'energy',amount:.714}],
    provider_pit_settings:{provider:'lmu',fuel_service_mode:'TARGET_LEVEL',energy_kind:'VIRTUAL_ENERGY',
      ve_target_percent:72,measured_fuel_ratio_l_per_percent_ve:8.6/10.2,fuel_ratio_l_per_percent_ve:.85,
      resulting_fuel_target_l:61.2,projected_fuel_out_l:61.2,expected_fuel_added_l:25.6,capacity_feasible:true,additional_stop_required:false}};
  if(scenario==='ve-collecting') {
    // Real LMU captures: ve_history is null, so VE is live but no completed-lap consumption exists yet.
    Object.assign(strategy,{status:'collecting_or_stale',limiting_resource:null,effective_range_laps:null,
      latest_safe_stop_lap:null,planned_stop_lap:null,planned_stop_safe:null,at_pit:[],next_stint_requirements:[],
      provider_pit_settings:null});
    Object.assign(strategy.resources[0],{state:'ready'});
    Object.assign(strategy.resources[1],{state:'collecting',consumption_per_lap:null,range_laps:null,available:false,sample_count:2});
  }
  return {strategy,focus:id(3),data:{live:true,session,participants,timing:{configured_laps:38,elapsed_seconds:2880},
      track_map:syntheticTrackMap(participants,trackScenario)},
    fuel:{session_id:session.id,participant_id:id(3),
      estimate:{status:'ready',consumption_l_per_lap:2.8,sample_count:5,estimated_range_laps:15},
      current:{fresh:true,fuel_l:42,capacity_l:100,recorded_at:epoch},provider:{},samples:[]}};
}

async function screenshot(channel) {
  // Optional developer tooling only; deliberately not a project/runtime dependency.
  const {chromium} = require('playwright');
  const repo = path.resolve(__dirname,'..'), output = path.join(repo,'build');
  fs.mkdirSync(output,{recursive:true});
  const demo=engineerDemo();
  const html=fs.readFileSync(path.join(repo,'server/static/session.html'),'utf8')
    .replace('__SESSION_ID__',JSON.stringify(demo.data.session.id))
    .replace('__WEATHER_UI__',fs.readFileSync(path.join(repo,'server/static/weather.js'),'utf8'))
    .replace('__BUILD_LABEL__','Stintara · synthetic demonstration');
  const browser=await chromium.launch({headless:true,...(channel?{channel}:{})});
  try {
    const page=await browser.newPage({viewport:{width:1920,height:1080},reducedMotion:'reduce'});
    await page.addInitScript(({epoch,sid,pid})=>{Date.now=()=>Date.parse(epoch);
      localStorage.setItem(`stintara:strategy:v1:${sid}:${pid}`,'32');
    },{epoch,sid:demo.data.session.id,pid:demo.focus});
    await page.route('**/*',route=>route.fulfill(route.request().url().includes('/api/') ?
      {json:route.request().url().includes('/strategy')?demo.strategy:demo.data} : {contentType:'text/html',body:html}));
    await page.goto(`http://stintara.example.invalid/sessions/synthetic/live?focus=${demo.focus}`);
    await page.locator('#fuel strong').first().waitFor();
    // Verify the real DOM disclosure/focus survives a polling replacement.
    await page.locator('#weather-toggle').click();
    await page.evaluate(()=>{window.previousWeatherDetails=document.querySelector('#weather-details');});
    await page.waitForFunction(()=>!window.previousWeatherDetails.isConnected);
    assert.equal(await page.locator('#weather-details').evaluate(el=>el.open),true);
    assert.equal(await page.evaluate(()=>document.activeElement.id),'weather-toggle');
    await page.locator('#weather-toggle').click();
    for (const [width,height] of [[1920,1080],[1920,900],[1920,800],[2560,1300],[900,900],[1920,799],[1200,760],[1600,760],[1800,800]]) {
      await page.setViewportSize({width,height});
      const dimensions=await page.evaluate(()=>({width:document.documentElement.scrollWidth,
        height:document.documentElement.scrollHeight,rows:document.querySelectorAll('#full-scroll tbody tr').length,
        visibleRows:[...document.querySelectorAll('#full-scroll tbody tr')].filter(row=>{
          const r=row.getBoundingClientRect(),f=document.querySelector('#full-scroll').getBoundingClientRect();
          const header=document.querySelector('#full-scroll thead').getBoundingClientRect();
          return r.top>=Math.max(f.top,header.bottom) && r.bottom<=f.bottom;
        }).length}));
      assert.equal(dimensions.rows,40);
      assert.ok(dimensions.width<=width);
      if((width>=1200 && height>=951) || (width>=1800 && height>=800)) {
        assert.ok(dimensions.height<=height);
        const panels=await page.evaluate(()=>{
          const weather=document.querySelector('#weather-panel'), field=document.querySelector('#full-scroll');
          return {weatherHeight:weather.clientHeight,weatherContent:weather.scrollHeight,
            fieldHeight:field.clientHeight,fieldContent:field.scrollHeight,
            mapHeight:document.querySelector('.map-panel').getBoundingClientRect().height,
            strategyHeight:document.querySelector('.fuel-panel').clientHeight,
            strategyBottom:document.querySelector('.fuel-panel').getBoundingClientRect().bottom,
            weatherBottom:weather.getBoundingClientRect().bottom};
        });
        assert.ok(panels.weatherHeight<210,'Weather recovers vertical space');
        assert.ok(panels.weatherContent<=panels.weatherHeight && panels.weatherBottom<=height,'Collapsed weather fits without internal scrolling or clipping');
        assert.ok(panels.fieldContent>panels.fieldHeight,'Full Field scrolls internally');
        assert.ok(panels.mapHeight>=100,'Map retains useful room');
        assert.ok(panels.strategyHeight>=178 && panels.strategyBottom<=height,JSON.stringify(panels));
      } else {
        assert.ok(dimensions.height>height,'Stacked fallback permits page scrolling');
        assert.equal(await page.locator('.fuel-panel').evaluate(el=>el.scrollHeight<=el.clientHeight),true);
      }
      const pitSettings=await page.evaluate(()=>{
        const el=document.querySelector('.resource-settings');
        const panel=el.closest('.fuel-panel'), box=el.getBoundingClientRect(), bounds=panel.getBoundingClientRect();
        const controls=document.querySelector('.plan-control').getBoundingClientRect();
        return {position:getComputedStyle(el).position,text:el.textContent,scrollTop:panel.scrollTop,
          inside:box.top>=bounds.top+panel.clientTop && box.bottom<=bounds.top+panel.clientTop+panel.clientHeight &&
            box.left>=bounds.left+panel.clientLeft && box.right<=bounds.left+panel.clientLeft+panel.clientWidth,
          clearOfControls:box.top>=controls.bottom,noHorizontalScroll:panel.scrollWidth<=panel.clientWidth};
      });
      if(width>=1800 && height>=800 && height<=950) {
        assert.equal(pitSettings.scrollTop,0,'Pit settings visible at initial Strategy scroll position');
        assert.equal(pitSettings.inside,true,'Entire pit settings block inside visible Strategy viewport');
        assert.equal(pitSettings.clearOfControls,true,'Pit settings do not overlap plan controls');
        assert.match(pitSettings.text,/SET VE TO 72%/);
        assert.match(pitSettings.text,/FUEL RATIO 0.85/);
        assert.equal(pitSettings.noHorizontalScroll,true);
      } else assert.notEqual(pitSettings.position,'sticky','No sticky settings outside compact desktop');
      const suffix=(width===1920 && height===1080) || width===900 ? width : `${width}x${height}`;
      const file=path.join(output,`engineer-mfd-weather-${suffix}.png`);
      await page.screenshot({path:file,fullPage:true});
      console.log(JSON.stringify({file,...dimensions}));
    }
  } finally { await browser.close(); }
}

async function screenshotVeCollecting(channel) {
  const {chromium} = require('playwright');
  const repo = path.resolve(__dirname,'..'), output = path.join(repo,'build');
  fs.mkdirSync(output,{recursive:true});
  const demo=engineerDemo('ve-collecting');
  const html=fs.readFileSync(path.join(repo,'server/static/session.html'),'utf8')
    .replace('__SESSION_ID__',JSON.stringify(demo.data.session.id))
    .replace('__WEATHER_UI__',fs.readFileSync(path.join(repo,'server/static/weather.js'),'utf8'))
    .replace('__BUILD_LABEL__','Stintara · synthetic demonstration');
  const browser=await chromium.launch({headless:true,...(channel?{channel}:{})});
  try {
    const page=await browser.newPage({viewport:{width:1920,height:1080},reducedMotion:'reduce'});
    await page.addInitScript(({epoch})=>{Date.now=()=>Date.parse(epoch);},{epoch});
    await page.route('**/*',route=>route.fulfill(route.request().url().includes('/api/') ?
      {json:route.request().url().includes('/strategy')?demo.strategy:demo.data} : {contentType:'text/html',body:html}));
    await page.goto(`http://stintara.example.invalid/sessions/synthetic/live?focus=${demo.focus}`);
    await page.locator('.resource-grid article').nth(1).waitFor();
    for (const [width,height] of [[1920,1080],[1920,900]]) {
      await page.setViewportSize({width,height});
      const panel=await page.locator('.fuel-panel').evaluate(el=>{const r=el.getBoundingClientRect();
        return {text:el.innerText,bottom:r.bottom,scrollOverflow:el.scrollHeight>el.clientHeight+1,
          pageWidth:document.documentElement.scrollWidth};});
      assert.match(panel.text,/Collecting samples/i);
      assert.match(panel.text,/Virtual Energy[\s\S]*63\.0%[\s\S]*consumption unknown[\s\S]*Collecting laps · 2\/3 valid/);
      assert.doesNotMatch(panel.text,/SET (VE|FUEL) TO/,'No pit settings without VE consumption');
      assert.ok(panel.pageWidth<=width && panel.bottom<=height,JSON.stringify(panel));
      const file=path.join(output,`engineer-ve-collecting-${width}x${height}.png`);
      await page.screenshot({path:file,fullPage:true});
      console.log(JSON.stringify({file,bottom:panel.bottom,scrollOverflow:panel.scrollOverflow}));
    }
  } finally { await browser.close(); }
}

async function screenshotTrackScenarios(channel) {
  const {chromium} = require('playwright');
  const repo = path.resolve(__dirname,'..'), output = path.join(repo,'build');
  fs.mkdirSync(output,{recursive:true});
  const browser=await chromium.launch({headless:true,...(channel?{channel}:{})});
  try {
    for (const scenario of ['green','yellow','learning','fcy']) {
      const demo=engineerDemo('ready',scenario);
      const html=fs.readFileSync(path.join(repo,'server/static/session.html'),'utf8')
        .replace('__SESSION_ID__',JSON.stringify(demo.data.session.id))
        .replace('__WEATHER_UI__',fs.readFileSync(path.join(repo,'server/static/weather.js'),'utf8'))
        .replace('__BUILD_LABEL__','Stintara · synthetic demonstration');
      const page=await browser.newPage({viewport:{width:1920,height:1080},reducedMotion:'reduce'});
      await page.addInitScript(({epoch,sid,pid})=>{Date.now=()=>Date.parse(epoch);
        localStorage.setItem(`stintara:strategy:v1:${sid}:${pid}`,'32');},{epoch,sid:demo.data.session.id,pid:demo.focus});
      await page.route('**/*',route=>route.fulfill(route.request().url().includes('/api/') ?
        {json:route.request().url().includes('/strategy')?demo.strategy:demo.data} : {contentType:'text/html',body:html}));
      await page.goto(`http://stintara.example.invalid/sessions/synthetic/live?focus=${demo.focus}`);
      await page.locator('.map-area svg').waitFor();
      for (const [width,height] of [[1920,1080],[1920,900],[1920,800],[2560,1300]]) {
        await page.setViewportSize({width,height});
        const result=await page.evaluate(()=>{
          const area=document.querySelector('.map-area'), svg=area.querySelector('svg'), box=svg.getBoundingClientRect();
          const panel=document.querySelector('.map-panel').getBoundingClientRect();
          const settings=document.querySelector('.resource-settings')?.getBoundingClientRect();
          return {text:document.querySelector('.map-panel').innerText,html:document.querySelector('.map-panel').innerHTML,
            svgW:box.width,svgH:box.height,areaScroll:area.scrollHeight>area.clientHeight+1||area.scrollWidth>area.clientWidth+1,
            pageW:document.documentElement.scrollWidth,panelBottom:panel.bottom,panelRight:panel.right,
            focus:document.querySelectorAll('.trk-focus').length,yellowPaths:document.querySelectorAll('.trk-yellow').length,
            cars:document.querySelectorAll('.trk-car,.trk-focus').length,
            settingsVisible:settings?settings.bottom<=window.innerHeight+1&&settings.top>=0:null,
            scrollH:document.documentElement.scrollHeight};});
        const where=`${scenario} ${width}x${height}`;
        assert.doesNotMatch(result.html,/NaN|Infinity|undefined/,where);
        assert.equal(result.areaScroll,false,`${where}: no scrollbars inside the map`);
        assert.ok(result.svgW>60&&result.svgH>40,`${where}: map has usable size ${result.svgW}x${result.svgH}`);
        assert.ok(result.pageW<=width,where);
        assert.equal(result.focus,1,`${where}: exactly one highlighted focus driver`);
        assert.equal(result.cars,40,where);
        if (width>=1800&&height>=800&&height<=1100) assert.ok(result.scrollH<=height,`${where}: no page scroll`);
        if (width>=1800&&height>=800) assert.equal(result.settingsVisible,true,`${where}: PIT SETTINGS reachable`);
        if (scenario==='green') { assert.match(result.text,/GREEN/); assert.equal(result.yellowPaths,0); assert.doesNotMatch(result.text,/YELLOW|SAFETY|STOPPED/); }
        if (scenario==='yellow') { assert.match(result.text,/S2 LOCAL YELLOW/); assert.ok(result.yellowPaths>=1); assert.doesNotMatch(result.text,/S1 LOCAL|S3 LOCAL/); }
        if (scenario==='learning') { assert.match(result.text,/LEARNING TRACK · 63%/); assert.equal(result.yellowPaths,0); assert.match(result.text,/S2 LOCAL YELLOW/); assert.match(result.text,/flag only/); }
        if (scenario==='fcy') { assert.match(result.text,/FULL COURSE YELLOW/); assert.match(result.text,/SAFETY CAR ACTIVE/); }
        await page.screenshot({path:path.join(output,`engineer-trackmap-${scenario}-${width}x${height}.png`),fullPage:true});
        console.log(JSON.stringify({scenario,width,height,svg:`${Math.round(result.svgW)}x${Math.round(result.svgH)}`,settingsVisible:result.settingsVisible,scrollH:result.scrollH}));
      }
      await page.close();
    }
  } finally { await browser.close(); }
}

// Real browser viewports measured on 2026-11-07 (CSS px, DPR 1) plus stress sizes. Developer tooling only.
const REAL_VIEWPORTS = [[1920, 911, 'real 1080p display'], [2560, 1271, 'real 1440p display'],
  [1920, 900, 'stress'], [1920, 800, 'stress'], [2560, 1300, 'stress']];
async function verifyViewports(channel) {
  const {chromium} = require('playwright');
  const repo = path.resolve(__dirname,'..'), output = path.join(repo,'build');
  fs.mkdirSync(output,{recursive:true});
  const browser=await chromium.launch({headless:true,...(channel?{channel}:{})});
  try {
    for (const scenario of ['green','vertical','stored']) {
      const demo=engineerDemo('ready',scenario);
      const html=fs.readFileSync(path.join(repo,'server/static/session.html'),'utf8')
        .replace('__SESSION_ID__',JSON.stringify(demo.data.session.id))
        .replace('__WEATHER_UI__',fs.readFileSync(path.join(repo,'server/static/weather.js'),'utf8'))
        .replace('__BUILD_LABEL__','Stintara · synthetic demonstration');
      const page=await browser.newPage({viewport:{width:1920,height:911},reducedMotion:'reduce'});
      try {
      await page.addInitScript(({epoch,sid,pid})=>{Date.now=()=>Date.parse(epoch);
        localStorage.setItem(`stintara:strategy:v1:${sid}:${pid}`,'32');},{epoch,sid:demo.data.session.id,pid:demo.focus});
      await page.route('**/*',route=>route.fulfill(route.request().url().includes('/api/') ?
        {json:route.request().url().includes('/strategy')?demo.strategy:demo.data} : {contentType:'text/html',body:html}));
      await page.goto(`http://stintara.example.invalid/sessions/synthetic/live?focus=${demo.focus}`);
      await page.locator('.map-area svg').waitFor();
      await page.locator('#fuel strong').first().waitFor();
      for (const [width,height,label] of REAL_VIEWPORTS) {
        await page.setViewportSize({width,height});
        await page.waitForTimeout(1300);   // two polls: map labels are sized for the measured map area
        const m=await page.evaluate(()=>{
          const rect=sel=>{const e=document.querySelector(sel);if(!e)return null;const r=e.getBoundingClientRect();
            return {height:r.height,width:r.width,clipped:e.scrollHeight>e.clientHeight+1,hidden:Math.max(0,e.scrollHeight-e.clientHeight)};};
          const field=document.querySelector('#full-scroll'),f=field.getBoundingClientRect(),head=field.querySelector('thead').getBoundingClientRect();
          const rows=[...field.querySelectorAll('tbody tr')].filter(row=>{const r=row.getBoundingClientRect();return r.top>=head.bottom-1&&r.bottom<=f.bottom+1;}).length;
          const box=document.querySelector('.map-area svg').getBoundingClientRect();
          const base=document.querySelector('.trk-base').getBBox();
          const names=['.session-header','.focus-panel','.standings-panel','.map-panel','.fuel-panel','#weather-panel','#comparison-panel'];
          const boxes=names.map(n=>[n,document.querySelector(n).getBoundingClientRect()]);
          const overlaps=[];
          for(let i=0;i<boxes.length;i++)for(let j=i+1;j<boxes.length;j++){const a=boxes[i][1],b=boxes[j][1];
            if(a.left<b.right-1&&b.left<a.right-1&&a.top<b.bottom-1&&b.top<a.bottom-1)overlaps.push(boxes[i][0]+' x '+boxes[j][0]);}
          const settings=document.querySelector('.resource-settings')?.getBoundingClientRect();
          const toggle=document.querySelector('#orientation-toggle');
          // Labels, rivals and sector ticks (Trackmap V2): measured on screen, not inferred from markup.
          const rectOf=e=>{const r=e.getBoundingClientRect();return {l:r.left,r:r.right,t:r.top,b:r.bottom};};
          const labels=[...document.querySelectorAll('.map-area .trk-label')].map(e=>({role:e.getAttribute('class').split(' ')[1],text:e.textContent,font:parseFloat(getComputedStyle(e).fontSize),...rectOf(e)}));
          const dots=[...document.querySelectorAll('.map-area circle.trk-car, .map-area circle.trk-focus')].map(rectOf);
          const path=document.querySelector('.trk-base'),ctm=path.getScreenCTM(),total=path.getTotalLength();
          const samples=Array.from({length:600},(_,i)=>{const q=path.getPointAtLength(total*i/600);return [q.x*ctm.a+ctm.e,q.y*ctm.d+ctm.f];});
          const ticks=[...document.querySelectorAll('.map-area .trk-sector')].map(e=>{const r=e.getBoundingClientRect(),cx=(r.left+r.right)/2,cy=(r.top+r.bottom)/2;
            return {length:Math.hypot(r.width,r.height),offPath:Math.min(...samples.map(([x,y])=>Math.hypot(x-cx,y-cy)))};});
          const sfEl=document.querySelector('.map-area .trk-sf');
          const sf=sfEl?(()=>{const r=sfEl.getBoundingClientRect(),cx=(r.left+r.right)/2,cy=(r.top+r.bottom)/2;return {count:document.querySelectorAll('.map-area .trk-sf').length,length:Math.hypot(r.width,r.height),offPath:Math.min(...samples.map(([x,y])=>Math.hypot(x-cx,y-cy))),...rectOf(sfEl)};})():null;
          return {labels,dots,ticks,sf,docW:document.documentElement.scrollWidth,docH:document.documentElement.scrollHeight,rows,
            h2h:rect('#comparison-panel'),weather:rect('#weather-panel'),strategy:rect('.fuel-panel'),map:rect('.map-panel'),
            svg:{w:box.width,h:box.height},trackAspect:base.width/base.height,overlaps,
            settingsInside:settings?settings.top>=0&&settings.bottom<=innerHeight+1:null,
            toggle:toggle?toggle.getBoundingClientRect().right<=document.querySelector('.map-panel').getBoundingClientRect().right:null,
            text:document.querySelector('.map-panel').innerText};});
        const where=`${scenario} ${width}x${height} (${label})`;
        assert.ok(m.docW<=width,`${where}: no horizontal scrolling`);
        assert.ok(m.docH<=height,`${where}: no document scrolling (${m.docH}>${height})`);
        assert.deepEqual(m.overlaps,[],`${where}: panels overlap`);
        assert.ok(m.rows>=(height>=1200?14:height>=900?7:5),`${where}: Full Field shows ${m.rows} rows`);
        assert.ok(m.h2h.hidden<=8,`${where}: Head-to-Head cut by ${m.h2h.hidden}px`);
        assert.equal(m.weather.clipped,false,`${where}: Weather clipped`);
        assert.equal(m.settingsInside,true,`${where}: PIT SETTINGS visible`);
        assert.ok(m.svg.w>300&&m.svg.h>120,`${where}: map size`);
        assert.ok(m.map.height<=Math.max(height*0.45,200),`${where}: map not oversized (${Math.round(m.map.height)}px)`);
        if (width>=2560) assert.equal(m.strategy.clipped,false,`${where}: Strategy fully visible`);
        if (scenario==='stored') {
          assert.match(m.text,/STORED TRACK/); assert.match(m.text,/POSITIONS UNAVAILABLE/);
          assert.doesNotMatch(m.text,/GREEN|RACE CONTROL/);
        } else assert.equal(m.toggle,true,`${where}: orientation control inside the panel`);
        assert.ok(m.trackAspect>1,`${where}: circuit is laid out horizontally (${m.trackAspect.toFixed(2)})`);
        // Trackmap V2: two sector ticks on the centerline; focus label first; no label hides a label or the focus dot.
        assert.equal(m.ticks.length,2,`${where}: two sector boundary ticks`);
        for (const tick of m.ticks) assert.ok(tick.offPath<4&&tick.length>6,`${where}: tick sits on the centerline (${tick.offPath.toFixed(1)}px off)`);
        // Start/finish (#71): one line on the centerline, longer than a sector tick, labelled in the header, never under a label.
        assert.ok(m.sf&&m.sf.count===1,`${where}: exactly one start/finish line`);
        assert.ok(m.sf.offPath<4&&m.sf.length>m.ticks[0].length,`${where}: start/finish on the centerline and longer than a sector tick (${m.sf.offPath.toFixed(1)}px off)`);
        assert.match(m.text,/Start\/Ziel \(aus Rundenabstand\)/);
        const overlap=(a,b)=>a.l<b.r&&b.l<a.r&&a.t<b.b&&b.t<a.b;
        assert.ok(m.labels.every(l=>!overlap(l,m.sf)),`${where}: no label covers the start/finish line`);
        if (scenario==='stored') assert.equal(m.labels.length,0,`${where}: stored track has no vehicle labels`);
        else {
          const focusLabel=m.labels.find(l=>l.role==='focus');
          assert.equal(focusLabel?.text,'P3',`${where}: focus labelled P3`);
          assert.ok(m.labels.every(l=>l.b-l.t>=9),`${where}: labels stay readable (${m.labels.map(l=>(l.b-l.t).toFixed(1))}px high)`);
          for(let i=0;i<m.labels.length;i++)for(let j=i+1;j<m.labels.length;j++)assert.equal(overlap(m.labels[i],m.labels[j]),false,`${where}: labels ${m.labels[i].text}/${m.labels[j].text} overlap`);
          const shrink=d=>({l:d.l+1,r:d.r-1,t:d.t+1,b:d.b-1});
          for (const label of m.labels) assert.equal(m.dots.some(d=>overlap(label,shrink(d))),false,`${where}: label ${label.text} covers a vehicle dot`);
          assert.ok(m.labels.every(l=>['focus','ahead','behind'].includes(l.role)),`${where}: only focus and direct rivals are labelled`);
        }
        console.log(JSON.stringify({where,rows:m.rows,docH:m.docH,map:Math.round(m.map.height),svg:`${Math.round(m.svg.w)}x${Math.round(m.svg.h)}`,
          strategyHidden:Math.round(m.strategy.hidden),h2hHidden:Math.round(m.h2h.hidden),aspect:Number(m.trackAspect.toFixed(2)),
          labels:m.labels.map(l=>`${l.text}:${Math.round(l.b-l.t)}px`).join(' '),ticks:m.ticks.length}));
        await page.screenshot({path:path.join(output,`engineer-v02-${scenario}-${width}x${height}.png`),fullPage:true});
      }
      if (scenario==='vertical') {
        await page.setViewportSize({width:1920,height:911});
        await page.locator('#orientation-toggle').click();
        assert.match(await page.locator('#orientation-toggle').innerText(),/ORIGINAL/);
        const original=await page.evaluate(()=>{const b=document.querySelector('.trk-base').getBBox();return b.width/b.height;});
        assert.ok(original<1,`ORIGINAL keeps the simulator orientation (aspect ${original.toFixed(2)})`);
        await page.reload(); await page.locator('.map-area svg').waitFor();
        assert.match(await page.locator('#orientation-toggle').innerText(),/ORIGINAL/,'per-track choice persists across reloads');
        await page.locator('#orientation-toggle').click();
      }
      } catch (error) {
        // A failure on the first viewport must still leave a useful artifact for GitHub Actions.
        await page.screenshot({path:path.join(output,`engineer-v02-failure-${scenario}.png`),fullPage:true}).catch(() => {});
        throw error;
      } finally {
        await page.close();
      }
    }
  } finally { await browser.close(); }
}

// Pre-Race planner overlay (#66): opens over the dashboard without changing its layout and stays inside small viewports.
async function verifyPrerace(channel) {
  const {chromium} = require('playwright');
  const repo = path.resolve(__dirname,'..'), output = path.join(repo,'build');
  fs.mkdirSync(output,{recursive:true});
  const demo=engineerDemo('ready');
  demo.data.participants=demo.data.participants.map(p=>p.id===demo.focus?{...p,driver_slug:'synthetic',car_name:'Synthetic Car'}:p);
  const plan={status:'incomplete',recommendation_available:false,start:null,fuel:null,energy:null,reserve_laps:1,pace_margin:.02,sources:{},
    issues:[{code:'missing_lap_time',field:'lap_time_s',severity:'missing'}],
    distance:{kind:'timed',duration_minutes:60,laps_min:null,laps_planning:null,pace_s:null}};
  const evidence={session_type:'Qualify',representative_of_race:null,race_rules:{available:false},lap_time:{value:101.2,sample_count:5},
    fuel_per_lap:{value:2.5,sample_count:4},ve_per_lap_percent:{value:null},ve_capable:false,tank_capacity_l:90};
  const html=fs.readFileSync(path.join(repo,'server/static/session.html'),'utf8')
    .replace('__SESSION_ID__',JSON.stringify(demo.data.session.id))
    .replace('__WEATHER_UI__',fs.readFileSync(path.join(repo,'server/static/weather.js'),'utf8'))
    .replace('/*__PRERACE_UI__*/',fs.readFileSync(path.join(repo,'server/static/prerace.js'),'utf8'))
    .replace('__BUILD_LABEL__','Stintara · synthetic demonstration');
  const browser=await chromium.launch({headless:true,...(channel?{channel}:{})});
  try {
    const page=await browser.newPage({viewport:{width:1920,height:900},reducedMotion:'reduce'});
    const problems=[];
    page.on('pageerror',e=>problems.push(e.message));
    await page.route('**/*',route=>{
      const url=route.request().url();
      if(!url.includes('/api/')) return route.fulfill({contentType:'text/html',body:html});
      if(url.includes('/prerace')) return route.fulfill({json:{driver:{slug:'synthetic'},plan,evidence,context:{session_id:demo.data.session.id,track_name:'Synthetic Circuit',car_name:'Synthetic Car'}}});
      return route.fulfill({json:url.includes('/strategy')?demo.strategy:demo.data});
    });
    await page.goto(`http://stintara.example.invalid/sessions/synthetic/live?focus=${demo.focus}`);
    await page.locator('#prerace-toggle').waitFor();
    const layout=()=>page.evaluate(()=>({h:document.documentElement.scrollHeight,fuel:document.querySelector('.fuel-panel').getBoundingClientRect().top}));
    for (const [width,height] of [[1920,900],[1366,768],[1024,640],[960,600]]) {
      await page.setViewportSize({width,height});
      await page.waitForTimeout(300);
      const before=await layout();
      if(!await page.locator('#prerace-panel').isVisible()) await page.locator('#prerace-toggle').click();
      await page.locator('#pr-form').waitFor();
      const m=await page.evaluate(()=>{const r=document.querySelector('#prerace-panel').getBoundingClientRect();
        return {left:r.left,right:r.right,top:r.top,bottom:r.bottom,docW:document.documentElement.scrollWidth,
          text:document.querySelector('#prerace-panel').innerText,expanded:document.querySelector('#prerace-toggle').getAttribute('aria-expanded')};});
      assert.equal(m.expanded,'true');
      assert.ok(m.left>=0&&m.right<=width&&m.top>=0&&m.bottom<=height,`${width}x${height}: planner inside the viewport ${JSON.stringify(m)}`);
      assert.ok(m.docW<=width,`${width}x${height}: no horizontal scrolling`);
      assert.match(m.text,/not available[\s\S]*enter it manually/);
      assert.match(m.text,/Qualify session, not race-representative/);
      assert.match(m.text,/No recommendation: data missing/); assert.match(m.text,/Lap time is missing/);
      assert.doesNotMatch(m.text,/Start\s+\d/);
      await page.screenshot({path:path.join(output,`engineer-prerace-${width}x${height}.png`)});
      await page.keyboard.press('Escape');
      assert.equal(await page.locator('#prerace-panel').isVisible(),false,'Escape closes the planner');
      // The overlay never moves the existing dashboard.
      assert.deepEqual(await layout(),before,`${width}x${height}: dashboard layout unchanged by the planner`);
    }
    // Typing survives the periodic refresh; adopting an observation fills the field with its origin.
    await page.setViewportSize({width:1920,height:900});
    await page.locator('#prerace-toggle').click(); await page.locator('#pr-form').waitFor();
    await page.waitForTimeout(400);   // the opening refresh replaces the previous form once
    await page.locator('#pr-duration_minutes').fill('45');
    await page.waitForTimeout(1500);
    assert.equal(await page.locator('#pr-duration_minutes').inputValue(),'45','typed value is not overwritten by refresh');
    await page.locator('button[data-adopt="lap_time_s"]').click();
    await page.waitForFunction(()=>/observed/.test(document.querySelector('label[for="pr-lap_time_s"]')?.textContent||''),null,{timeout:4000});
    assert.equal(await page.locator('#pr-lap_time_s').inputValue(),'101.200');
    assert.equal(await page.locator('#pr-duration_minutes').inputValue(),'45','Use keeps the other unsaved input');
    await page.locator('#pr-form button[type=submit]').click();
    await page.waitForFunction(()=>/observed/.test(document.querySelector('label[for="pr-lap_time_s"]')?.textContent||'')&&!/unsaved/.test(document.querySelector('label[for="pr-lap_time_s"]').textContent),null,{timeout:4000});
    assert.equal(await page.locator('#pr-duration_minutes').inputValue(),'45','saved value shown after Calculate');
    assert.match(await page.locator('label[for="pr-lap_time_s"]').innerText(),/observed/);
    // "← Driver Live" returns to the focused driver; without a driver slug it is not offered.
    const back=page.locator('#prerace-back');
    assert.equal(await back.isVisible(),true,'back link visible for a registered focus driver');
    assert.equal(await back.getAttribute('href'),'/drivers/synthetic/live');
    demo.data.participants=demo.data.participants.map(p=>p.id===demo.focus?{...p,driver_slug:null}:p);
    await page.locator('#prerace-close').click(); await page.locator('#prerace-toggle').click();
    await page.waitForFunction(()=>document.querySelector('#prerace-back').hidden,null,{timeout:6000});
    demo.data.participants=demo.data.participants.map(p=>p.id===demo.focus?{...p,driver_slug:'synthetic'}:p);
    await page.locator('#prerace-close').click(); await page.locator('#prerace-toggle').click();
    await page.waitForFunction(()=>!document.querySelector('#prerace-back').hidden,null,{timeout:6000});
    await Promise.all([page.waitForURL('**/drivers/synthetic/live'),back.click()]);
    assert.deepEqual(problems,[],'no JavaScript errors');
    console.log('engineer pre-race gate passed');
  } finally { await browser.close(); }
}

// Strategy UX (#72): state explanations, now/range captions and the legend in a real browser, also on small displays.
async function verifyStrategyUx(channel) {
  const {chromium} = require('playwright');
  const repo=path.resolve(__dirname,'..');
  const demo=engineerDemo('ve-collecting');
  const html=fs.readFileSync(path.join(repo,'server/static/session.html'),'utf8')
    .replace('__SESSION_ID__',JSON.stringify(demo.data.session.id))
    .replace('__WEATHER_UI__',fs.readFileSync(path.join(repo,'server/static/weather.js'),'utf8'))
    .replace('/*__PRERACE_UI__*/',fs.readFileSync(path.join(repo,'server/static/prerace.js'),'utf8'))
    .replace('__BUILD_LABEL__','Stintara · synthetic demonstration');
  const browser=await chromium.launch({headless:true,...(channel?{channel}:{})});
  try {
    const page=await browser.newPage({viewport:{width:1920,height:1080},reducedMotion:'reduce'});
    const problems=[]; page.on('pageerror',e=>problems.push(e.message));
    await page.route('**/*',route=>{const url=route.request().url();
      if(!url.includes('/api/')) return route.fulfill({contentType:'text/html',body:html});
      if(url.includes('/prerace')) return route.fulfill({status:404,json:{}});
      return route.fulfill({json:url.includes('/strategy')?demo.strategy:demo.data});});
    await page.goto(`http://stintara.example.invalid/sessions/synthetic/live?focus=${demo.focus}`);
    await page.locator('.resource-grid article').nth(1).waitFor();
    for (const [width,height] of [[1920,1080],[1366,768],[1024,640],[960,600]]) {
      await page.setViewportSize({width,height}); await page.waitForTimeout(300);
      const where=`${width}x${height}`;
      const text=await page.locator('.fuel-panel').innerText();
      assert.match(text,/Waiting for: VE is collecting laps \(2\/3 valid\)\. A valid lap is a full lap without a pit stop\./,`${where}: collecting explained`);
      assert.match(text,/63\.0%\s*now/,`${where}: current level captioned`);
      assert.match(text,/consumption unknown/); assert.doesNotMatch(text,/SET (VE|FUEL) TO/);
      // Legend: closed by default, opens by touch/click, reachable inside the panel, no horizontal scrolling, survives the live refresh.
      assert.equal(await page.locator('#strategy-help').evaluate(e=>e.open),false,`${where}: legend closed by default`);
      await page.locator('#strategy-help-toggle').scrollIntoViewIfNeeded(); await page.locator('#strategy-help-toggle').click();
      await page.waitForTimeout(150);
      const open=await page.evaluate(()=>{const d=document.querySelector('#strategy-help'),li=[...d.querySelectorAll('li')].find(e=>/Not the pre-race plan/.test(e.textContent));
        li.scrollIntoView({block:'nearest'});const r=li.getBoundingClientRect(),panel=document.querySelector('.fuel-panel').getBoundingClientRect();
        window.__previous=document.querySelector('.resource-grid');
        return {open:d.open,visible:r.top>=panel.top-1&&r.bottom<=panel.bottom+1,docW:document.documentElement.scrollWidth};});
      assert.equal(open.open,true); assert.ok(open.visible,`${where}: legend text reachable inside the panel`); assert.ok(open.docW<=width,`${where}: no horizontal scrolling`);
      await page.waitForFunction(()=>!window.__previous.isConnected,null,{timeout:8000});
      assert.equal(await page.locator('#strategy-help').evaluate(e=>e.open),true,`${where}: legend stays open across the refresh`);
      await page.locator('#strategy-help-toggle').click();
    }
    // Real session facts reach the widget: a Qualifying session explains why there is no recommendation.
    demo.strategy.status='inactive'; demo.data.session.session_type='Qualifying';
    await page.waitForFunction(()=>/Qualifying session: the next-stop plan runs only in a live Race\. For the start load use the Pre-Race planner\./.test(document.querySelector('.fuel-panel').innerText),null,{timeout:8000});
    assert.deepEqual(problems,[],'no JavaScript errors');
    console.log('engineer strategy UX gate passed');
  } finally { await browser.close(); }
}

module.exports={engineerDemo};
if(require.main===module) {
  if(process.argv.includes('--viewports')) {
    const index=process.argv.indexOf('--browser');
    verifyViewports(index<0?undefined:process.argv[index+1]).then(()=>verifyPrerace(index<0?undefined:process.argv[index+1])).then(()=>verifyStrategyUx(index<0?undefined:process.argv[index+1])).catch(error=>{console.error(error);process.exitCode=1;});
  } else if(process.argv.includes('--screenshot') && process.argv.includes('--track-scenarios')) {
    const index=process.argv.indexOf('--browser');
    screenshotTrackScenarios(index<0?undefined:process.argv[index+1]).catch(error=>{console.error(error);process.exitCode=1;});
  } else if(process.argv.includes('--screenshot') && process.argv.includes('--ve-collecting')) {
    const index=process.argv.indexOf('--browser');
    screenshotVeCollecting(index<0?undefined:process.argv[index+1]).catch(error=>{console.error(error);process.exitCode=1;});
  } else if(process.argv.includes('--screenshot')) {
    const index=process.argv.indexOf('--browser');
    screenshot(index<0?undefined:process.argv[index+1]).catch(error=>{console.error(error);process.exitCode=1;});
  } else if(process.argv.includes('--json')) console.log(JSON.stringify(engineerDemo(),null,2));
  else { console.error('Use --json or --screenshot [--browser msedge|chrome].'); process.exitCode=1; }
}
