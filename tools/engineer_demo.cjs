// Wholly synthetic API presentation fixture. No real captures or production demo endpoint.
// Run: node tools/engineer_demo.cjs --json
// Optional browser check: node tools/engineer_demo.cjs --screenshot --browser msedge
const fs = require('node:fs');
const path = require('node:path');
const assert = require('node:assert/strict');
const epoch = '2026-01-01T12:00:00.000Z';
const id = n => `00000000-0000-7000-8000-${String(n).padStart(12,'0')}`;

function engineerDemo() {
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
    resources:[{resource_kind:'fuel',current:70,capacity:100,consumption_per_lap:8.6,range_laps:70/8.6,
      fresh:true,available:true,sample_count:5,units:'L',pit_service_mode:'TARGET_LEVEL'},
      {resource_kind:'energy',energy_kind:'VIRTUAL_ENERGY',current:.63,capacity:1,consumption_per_lap:.102,
        range_laps:.63/.102,fresh:true,available:true,sample_count:5,units:'fraction',pit_service_mode:'TARGET_LEVEL'}],
    limiting_resource:'energy',effective_range_laps:.63/.102,latest_safe_stop_lap:33,planned_stop_lap:32,
    planned_stop_safe:true,shortfall:[],additional_stop_required:false,
    at_pit:[{resource_kind:'fuel',amount:35.6},{resource_kind:'energy',amount:.222}],
    next_stint_requirements:[{resource_kind:'fuel',amount:60.2},{resource_kind:'energy',amount:.714}],
    provider_pit_settings:{provider:'lmu',fuel_service_mode:'TARGET_LEVEL',energy_kind:'VIRTUAL_ENERGY',
      ve_target_percent:72,measured_fuel_ratio_l_per_percent_ve:8.6/10.2,fuel_ratio_l_per_percent_ve:.85,
      resulting_fuel_target_l:61.2,projected_fuel_out_l:61.2,expected_fuel_added_l:25.6,capacity_feasible:true,additional_stop_required:false}};
  return {strategy,focus:id(3),data:{live:true,session,participants,timing:{configured_laps:38,elapsed_seconds:2880}},
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

module.exports={engineerDemo};
if(require.main===module) {
  if(process.argv.includes('--screenshot')) {
    const index=process.argv.indexOf('--browser');
    screenshot(index<0?undefined:process.argv[index+1]).catch(error=>{console.error(error);process.exitCode=1;});
  } else if(process.argv.includes('--json')) console.log(JSON.stringify(engineerDemo(),null,2));
  else { console.error('Use --json or --screenshot [--browser msedge|chrome].'); process.exitCode=1; }
}
