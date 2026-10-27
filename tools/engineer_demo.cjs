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
      current:{air_temp_c:24,track_temp_c:41,rain_intensity:0,wetness_avg:0,cloudiness:.25,wind_speed_mps:3},
      forecast:{timeline_complete:true,nodes:Array.from({length:5},(_,i)=>({position_percent:i*25,
        temperature_c:24+i,rain_chance:i*.05,humidity_percent:50,wind_direction:'NE',wind_speed_mps:3}))},
    }};
  return {focus:id(3),data:{live:true,session,participants,timing:{configured_minutes:80,elapsed_seconds:2880}},
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
    await page.addInitScript(value=>{Date.now=()=>Date.parse(value);},epoch);
    await page.route('**/*',route=>route.fulfill(route.request().url().includes('/api/') ?
      {json:route.request().url().endsWith('/fuel')?demo.fuel:demo.data} : {contentType:'text/html',body:html}));
    await page.goto(`http://stintara.example.invalid/sessions/synthetic/live?focus=${demo.focus}`);
    await page.locator('#fuel strong').first().waitFor();
    for (const [width,height] of [[1920,1080],[900,900]]) {
      await page.setViewportSize({width,height});
      const dimensions=await page.evaluate(()=>({width:document.documentElement.scrollWidth,
        height:document.documentElement.scrollHeight,rows:document.querySelectorAll('#full-scroll tbody tr').length}));
      assert.equal(dimensions.rows,40);
      assert.ok(dimensions.width<=width);
      if(width===1920) assert.ok(dimensions.height<=height);
      const file=path.join(output,`engineer-pit-finish-${width}.png`);
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
