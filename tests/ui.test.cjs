const assert = require('node:assert/strict');
const {test} = require('node:test');
const {readFileSync} = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

function weatherScript() {
  return readFileSync(path.join(__dirname, '../server/static/weather.js'), 'utf8');
}

async function render(file, data, ok=true, status=200) {
  const html = readFileSync(path.join(__dirname, '../server/static', file), 'utf8');
  const script = html.match(/<script>([\s\S]*?)<\/script>/)[1].replace('__WEATHER_UI__', weatherScript()).replace('__DRIVER_SLUG__', '"driver"').replace('__SESSION_ID__', '"session"');
  const root = {innerHTML: '', textContent: ''};
  const selector = {innerHTML:'', value:'', addEventListener() {}};
  const fuel = {innerHTML:'',textContent:''};
  let finish;
  const done = new Promise(resolve => { finish = resolve; });
  vm.runInNewContext(script, {
    document: {querySelector: value => value === '#focus' ? selector : value === '#fuel' ? fuel : root},
    URL, location:{href:'http://localhost/sessions/session/live'}, history:{replaceState() {}},
    fetch: async () => ({ok, status, json: async () => data}),
    setTimeout: finish,
  });
  await done;
  return root;
}

test('frontpage lists separate sessions with escaped metadata and links', async () => {
  assert.match((await render('index.html', [])).innerHTML, /No live sessions/);
  const root = await render('index.html', [{id:'one', track_name:'<img onerror=alert(1)>',
    session_type:'Race', participant_count:50}, {id:'two',track_name:'Same track'}]);
  assert.match(root.innerHTML, /&lt;img/);
  assert.doesNotMatch(root.innerHTML, /<img/);
  assert.match(root.innerHTML, /50 participants/);
  assert.match(root.innerHTML, /\/sessions\/one\/live/);
  assert.match(root.innerHTML, /\/sessions\/two\/live/);
});

test('driver page includes tyre readings, standings and offline/error states', async () => {
  const root = await render('live.html', {live:true, driver:{display_name:'<script>bad</script>'},
    session:{track_name:'Imola', session_type:'Race'}, participants:[{is_stintara_driver:true,
      display_name:'<b>Driver</b>', car_name:'Porsche', in_pits:true, position:1}],
    strategy:{fuel_l:42, tyre_remaining_fl:1, tyre_remaining_fr:0, tyre_pressure_fl_kpa:175,
      tyre_temp_inner_fl_c:80, tyre_temp_mid_fl_c:90, tyre_temp_outer_fl_c:100, tyre_brake_temp_fl_c:450}});
  for (const value of ['100.0%', '0.0%', '175.0 kPa', '80.0 °C', '450.0 °C', 'PIT', '42.0 L']) {
    assert.ok(root.innerHTML.includes(value), value);
  }
  assert.doesNotMatch(root.innerHTML, /<script>|<b>/);
  assert.match((await render('live.html', {live:false,driver:{display_name:'Driver'}})).innerHTML, /Offline/);
  assert.match((await render('live.html', null, false, 404)).textContent, /Driver not found/);
});

test('vehicle fallback identifies model, manufacturer, entry and class honestly', async () => {
  const participants = [
    {vehicle_model:'Model A', manufacturer:'Brand A', entry_name:'Entry A'},
    {manufacturer:'Brand B', entry_name:'Entry B'},
    {entry_name:'<Entry C>', vehicle_class:'GT3'},
    {car_name:'Legacy Entry'}, {vehicle_class:'LMP2'}, {},
  ];
  const root = await render('live.html', {live:true, driver:{display_name:'Driver'},
    session:{track_name:'Sebring International Raceway',session_type:'Practice'}, participants,
    strategy:{tyre_remaining_fl:.987, tyre_pressure_fl_kpa:null, tyre_temp_inner_fl_c:null}});
  for (const expected of ['Vehicle / entry', 'Model</div><div>Model A', 'Manufacturer</div><div>Brand B',
                          'Entry</div><div>&lt;Entry C&gt;', 'Entry</div><div>Legacy Entry', 'Class</div><div>LMP2', '98.7%', '<td>—</td>']) {
    assert.ok(root.innerHTML.includes(expected), expected);
  }
  assert.doesNotMatch(root.innerHTML, /<th>Car<\/th>|Entry A|Entry B|0\.0 kPa|-273\.1/);
});

test('each standings fallback is typed, escaped and independent of entry keywords', async () => {
  const cases = [
    [{vehicle_model:'<Model & "A">', manufacturer:'unused brand', entry_name:'unused entry'}, 'Model', '&lt;Model &amp; &quot;A&quot;&gt;'],
    [{manufacturer:"<Brand 'B'>", entry_name:'unused entry'}, 'Manufacturer', '&lt;Brand &#39;B&#39;&gt;'],
    [{entry_name:'BMW GT3 Custom Team', vehicle_class:'unused class'}, 'Entry', 'BMW GT3 Custom Team'],
    [{vehicle_model:' ', manufacturer:'', entry_name:' ', vehicle_class:'<LMGT3>'}, 'Class', '&lt;LMGT3&gt;'],
    [{car_class:'LMP2'}, 'Class', 'LMP2'],
  ];
  for (const [participant, label, value] of cases) {
    const root = await render('live.html', {live:true, driver:{display_name:'Driver'},
      session:{track_name:'Track', session_type:'Race'}, participants:[{...participant, is_stintara_driver:true}]});
    assert.ok(root.innerHTML.includes(`<td><div class="muted">${label}</div><div>${value}</div></td>`));
    assert.ok(root.innerHTML.includes(`${label}: ${value}`));
    assert.doesNotMatch(root.innerHTML, /unused|<Model|<Brand|<LMGT3>|<th>Car<\/th>/);
  }
  assert.match((await render('live.html', null, false, 500)).textContent, /Live data unavailable/);
});

test('spectator renders full synthetic field, unknown gaps, escaping and offline state', async () => {
  const participants = Array.from({length:60}, (_, i) => ({id:String(i), display_name:'<Driver>',
    position:i+1, vehicle_class:'<GT3>', entry_name:'Entry', gap_to_leader:-2, interval:3,
    total_laps:0, last_lap_time:null, in_pits:i===0, driver_slug:i===0 ? 'test' : null}));
  const data = {live:true, session:{id:'session',track_name:'Synthetic track',session_type:'Race'},participants};
  const root = await render('session.html', data);
  assert.equal((root.innerHTML.match(/&lt;Driver&gt;/g)||[]).length, 60);
  assert.match(root.innerHTML, /Receiving scoring/);
  assert.match(root.innerHTML, /\/drivers\/test\/live/);
  assert.match(root.innerHTML, /<td>60<\/td>/);
  assert.match(root.innerHTML, /PIT/);
  assert.doesNotMatch(root.innerHTML, /<Driver>|<GT3>|-2|3 s/);
  assert.match((await render('session.html', {...data,live:false})).innerHTML, /No live scoring/);
  assert.match((await render('session.html', {...data,participants:[]})).innerHTML, /No participants/);
  assert.match((await render('session.html', null,false,404)).textContent, /Session not found/);
  assert.match((await render('session.html', null,false,500)).textContent, /Retrying/);
});


const syntheticId = i => `019a0000-0000-7000-8000-${String(i).padStart(12,'0')}`;
const syntheticField = count => Array.from({length:count}, (_, i) => ({id:syntheticId(i+1),
  display_name:'Synthetic Driver', position:i+1, vehicle_class:'Synthetic Class',
  total_laps:4, finish_status:0, pitstops:0, last_lap_time:123.456, best_lap_time:122.345, driver_slug:null}));
const sessionData = participants => ({live:true,session:{id:syntheticId(99),track_name:'Synthetic Track'},participants});
function spectatorScript() {
  return readFileSync(path.join(__dirname, '../server/static/session.html'), 'utf8')
    .match(/<script>([\s\S]*?)<\/script>/)[1].replace('__WEATHER_UI__', weatherScript()).replace('__SESSION_ID__', JSON.stringify(syntheticId(99)));
}
function helpers() {
  const context = vm.createContext({URL});
  vm.runInContext(spectatorScript().split('const sessionId =')[0], context);
  return context;
}
async function sessionHarness(data, query='', fuelReply=null) {
  const root = {innerHTML:'',textContent:''};
  const selector = {innerHTML:'',value:'',addEventListener(name, listener) { this[name] = listener; }};
  const scrolls = {'#full-scroll':{scrollLeft:31,scrollTop:400,focus() {this.focused=true;}},
    '#relative-scroll':{scrollLeft:17,scrollTop:20},'#comparison-panel':{scrollTop:12},'#weather-panel':{scrollTop:5},
    '#fuel-toggle':{focus() {this.focused=true;}}};
  const location = {href:'http://localhost/sessions/session/live'+query};
  const replacements = [], fuelRequests = [];
  const fuel = {html:'',text:'', get innerHTML() { return this.html; }, set innerHTML(value) { this.html=value; this.text=''; },
    get textContent() { return this.text; }, set textContent(value) { this.text=value; this.html=''; }};
  const details = {open:false};
  const storage=new Map();
  const localStorage={getItem:key=>storage.get(key)??null,setItem:(key,value)=>storage.set(key,value),removeItem:key=>storage.delete(key)};
  const planInput={value:''},planFeedback={textContent:''};
  const planForm={addEventListener(name,listener){this[name]=listener;}};
  const planAuto={addEventListener(name,listener){this[name]=listener;}};
  const relativeDetails = {open:false};
  const controls={'#planned-stop':planInput,'#plan-feedback':planFeedback,'#plan-form':planForm,'#plan-auto':planAuto};
  const document = {activeElement:null,querySelector: name => name==='#root' ? root : name==='#focus' ? selector : name==='#fuel' ? fuel : name==='#fuel-diagnostics' ? details : name==='#relative-details' ? relativeDetails : controls[name]||scrolls[name]};
  let queued, finish, currentData=data;
  let done = new Promise(resolve => { finish=resolve; });
  const context = vm.createContext({URL,location,localStorage,
    history:{replaceState(state,unused,url) { replacements.push(url); location.href=new URL(url,location.href).href; }},
    document,
    fetch:async url => {
      if (!url.includes('/strategy')) return {ok:true,json:async () => currentData};
      fuelRequests.push(url);
      const id=url.split('/').at(-2);
      const response=fuelReply ? await fuelReply(id) : {ok:true,json:async () => fuelData(id)};
      return {...response,json:async()=>{
        const data=await response.json();
        return data.resources?data:{session_id:data.session_id,participant_id:data.participant_id,
          status:'collecting_or_stale',resources:[],diagnostics:{fuel:data}};
      }};
    },
    setTimeout:callback => { queued=callback; finish(); },
  });
  vm.runInContext(spectatorScript(), context);
  await done;
  return {root,selector,location,replacements,scrolls,fuel,fuelRequests,details,relativeDetails,document,
    planInput,planFeedback,planForm,planAuto,storage,
    change(id) { selector.value=id; selector.change(); },
    async poll(data) {
      currentData=data;
      done=new Promise(resolve => { finish=resolve; });
      await queued(); await done;
    },
  };
}

test('focus resolution uses UUID membership, never duplicate names or positions', () => {
  const h=helpers(), field=syntheticField(8);
  assert.equal(h.resolveFocus(field, field[4].id), field[4].id);
  assert.equal(h.resolveFocus(field, field[5].id), field[5].id);
  assert.equal(h.resolveFocus(field, 'Synthetic Driver'), null);
  assert.equal(h.resolveFocus(field, syntheticId(88)), null);
  for (const bad of ['', '<script>', '5', '%ZZ', null]) assert.equal(h.resolveFocus(field,bad),null);
  assert.equal(h.resolveFocus(field,null,true),null);
  field[1].driver_slug='synthetic-driver';
  assert.equal(h.resolveFocus(field,null,true),field[1].id);
  assert.equal(h.resolveFocus(field,'invalid',true),null);
  field[2].driver_slug='synthetic-driver-two';
  assert.equal(h.resolveFocus(field,null,true),null);
});

test('relative window preserves API order and ties at middle, boundaries and small fields', () => {
  const h=helpers(), field=syntheticField(10);
  field[3].position=field[4].position=5;
  for (const [index,start,end] of [[4,1,8],[0,0,4],[9,6,10]]) {
    assert.deepEqual(Array.from(h.relativeWindow(field,field[index].id),p=>p.id),field.slice(start,end).map(p=>p.id));
  }
  assert.deepEqual(Array.from(h.relativeWindow(field.slice(0,3),field[1].id),p=>p.id),field.slice(0,3).map(p=>p.id));
  assert.equal(h.relativeWindow(field,syntheticId(90)).length,0);
  assert.equal(field[3].position,5);
  assert.equal(field[4].position,5);
});

test('head-to-head requires unique exact adjacent valid provider positions', () => {
  const h=helpers(), field=syntheticField(7), focus=field[4];
  assert.equal(h.uniquePositionNeighbor(field,focus,-1),field[3]);
  assert.equal(h.uniquePositionNeighbor(field,focus,1),field[5]);
  assert.equal(h.uniquePositionNeighbor([...field].reverse(),focus,-1),field[3]);
  assert.equal(h.uniquePositionNeighbor([...field].reverse(),focus,1),field[5]);
  assert.equal(h.uniquePositionNeighbor(field,field[0],-1),null);
  assert.equal(h.uniquePositionNeighbor(field,field[6],1),null);
  for (const value of [null,undefined,0,-1,1.5,'5',NaN,Infinity,true]) {
    for (const offset of [-1,1]) assert.equal(h.uniquePositionNeighbor(field,{...focus,position:value},offset),null);
  }
  assert.equal(h.uniquePositionNeighbor([...field,{...field[3],id:syntheticId(30)}],focus,-1),null);
  assert.equal(h.uniquePositionNeighbor([...field,{...field[5],id:syntheticId(31)}],focus,1),null);
  assert.equal(h.uniquePositionNeighbor([...field,{...focus,id:syntheticId(32)}],focus,-1),field[3]);
  assert.equal(h.uniquePositionNeighbor([...field,{...focus,id:syntheticId(32)}],focus,1),field[5]);
  assert.equal(h.uniquePositionNeighbor(field.filter(p=>p.position!==4),focus,-1),null);
  assert.equal(h.uniquePositionNeighbor(field.filter(p=>p.position!==6),focus,1),null);
});

test('URL focus selects unresolved participant and survives position/order/name changes', async () => {
  const field=syntheticField(8), id=field[4].id;
  const page=await sessionHarness(sessionData(field),`?focus=${id}&view=timing#field`);
  assert.equal(page.selector.value,id);
  assert.match(page.root.innerHTML,/class="focus"/);
  assert.equal((page.root.innerHTML.match(/class="focus"/g)||[]).length,3);
  assert.doesNotMatch(page.root.innerHTML,/Driver live/);
  const changed=field.map(p=>({...p,position:9-p.position,display_name:'Driver Alpha'})).reverse();
  await page.poll(sessionData(changed));
  assert.equal(page.selector.value,id);
  assert.equal(new URL(page.location.href).searchParams.get('focus'),id);
  assert.equal(page.scrolls['#full-scroll'].scrollLeft,31);
  assert.equal(page.scrolls['#relative-scroll'].scrollLeft,17);
  await page.poll(sessionData(changed.filter(p=>p.id!==id)));
  assert.equal(page.selector.value,'');
  assert.equal(new URL(page.location.href).searchParams.has('focus'),false);
  assert.match(page.location.href,/view=timing#field$/);
  await page.poll(sessionData([{...field[0],driver_slug:'synthetic'}]));
  assert.equal(page.selector.value,'');
});

test('manual selection updates URL without reload, clearing never auto-selects on poll', async () => {
  const field=syntheticField(4);
  const page=await sessionHarness(sessionData(field));
  assert.equal(page.selector.value,'');
  page.change(field[1].id);
  assert.equal(new URL(page.location.href).searchParams.get('focus'),field[1].id);
  page.change(field[2].id);
  assert.equal(page.selector.value,field[2].id);
  assert.equal(new URL(page.location.href).searchParams.get('focus'),field[2].id);
  page.change('');
  assert.equal(new URL(page.location.href).searchParams.has('focus'),false);
  await page.poll(sessionData([{...field[0],driver_slug:'synthetic'}]));
  assert.equal(page.selector.value,'');
});

test('default is unique registered participant only; invalid URL suppresses default', async () => {
  const field=syntheticField(3); field[1].driver_slug='synthetic';
  assert.equal((await sessionHarness(sessionData(field))).selector.value,field[1].id);
  for (const query of ['?focus=', '?focus=%3Cscript%3E','?focus=%ZZ',`?focus=${syntheticId(88)}`]) {
    const page=await sessionHarness(sessionData(field),query);
    assert.equal(page.selector.value,'');
    assert.match(page.root.innerHTML,/Full field/);
  }
  field[2].driver_slug='synthetic-two';
  assert.equal((await sessionHarness(sessionData(field))).selector.value,'');
});

test('new widgets escape provider strings and always show unknown intervals', async () => {
  const field=syntheticField(3).map(p=>({...p,display_name:'<img onerror="x">',vehicle_class:'<svg>',
    entry_name:'<entry>',team_name:'<team>',interval:12345,gap_to_leader:-9876}));
  const page=await sessionHarness({...sessionData(field),session:{track_name:'<track>'}},`?focus=${field[1].id}`);
  assert.doesNotMatch(page.root.innerHTML,/<img|<svg|<entry|<team|<track|12345|-9876/);
  assert.doesNotMatch(page.selector.innerHTML,/<img/);
  for (const escaped of ['&lt;img','&lt;svg&gt;','&lt;entry&gt;','&lt;team&gt;','&lt;track&gt;']) assert.ok(page.root.innerHTML.includes(escaped));
  assert.equal((page.root.innerHTML.match(/Interval to focus<\/dt><dd>—/g)||[]).length,3);
  assert.match(page.root.innerHTML,/not physical order/);
});

test('driver spectator link uses only the unique own participant ID', async () => {
  const id=syntheticId(2), session={id:syntheticId(99)};
  const participants=[{participant_id:syntheticId(1),display_name:'Synthetic Driver'},
    {participant_id:id,is_stintara_driver:true,display_name:'Synthetic Driver'}];
  const data={live:true,driver:{display_name:'Synthetic Driver'},session,participants};
  assert.ok((await render('live.html',data)).innerHTML.includes(`/sessions/${session.id}/live?focus=${id}`));
  for (const candidates of [[],participants.map(p=>({...p,is_stintara_driver:true})),[{is_stintara_driver:true}]]) {
    assert.doesNotMatch((await render('live.html',{...data,participants:candidates})).innerHTML,/\?focus=/);
  }
});


function fuelData(id, count=3) {
  return {session_id:syntheticId(99),participant_id:id,
    estimate:{status:count<3 ? 'collecting' : 'ready',sample_count:count<3 ? 0 : count,available_sample_count:count,minimum_samples:3,window_size:5,
      consumption_l_per_lap:count<3 ? null : 2.8,estimated_range_laps:count<3 ? null : 15},
    current:{fuel_l:42,capacity_l:100,recorded_at:'2026-01-01T00:00:00Z',fresh:true},
    provider:{avg_consumption_l_per_lap:99,laps_remaining:88,stint_number:2,stint_laps:6,stint_consumption_l:18},
    samples:[{from_lap:1,to_lap:2,consumption_l:2.8,included:true,used_for_estimate:count>=3,excluded_reason:null},
      {from_lap:2,to_lap:3,consumption_l:-20,included:false,used_for_estimate:false,excluded_reason:'pitstop_transition'}]};
}
const settleFuel = () => new Promise(resolve => setImmediate(resolve));

test('fuel follows UUID focus, clears on switch/disappearance and never loads without focus', async () => {
  const field=syntheticField(3), page=await sessionHarness(sessionData(field));
  assert.equal(page.fuelRequests.length,0);
  assert.match(page.fuel.textContent,/Choose a participant/);
  page.change(field[0].id); await settleFuel();
  assert.ok(page.fuelRequests.at(-1).endsWith(`/participants/${field[0].id}/strategy`));
  assert.match(page.fuel.innerHTML,/Stintara consumption: <strong>2.80 L\/lap/);
  page.change(field[1].id);
  assert.equal(page.fuel.innerHTML,'');
  assert.match(page.fuel.textContent,/Loading/);
  await settleFuel();
  assert.ok(page.fuelRequests.at(-1).endsWith(`/participants/${field[1].id}/strategy`));
  await page.poll(sessionData(field.filter(p=>p.id!==field[1].id)));
  assert.equal(page.fuel.innerHTML,'');
  assert.match(page.fuel.textContent,/Choose a participant/);
});

test('fuel collecting, ready, stale and provider comparison remain distinct', () => {
  const h=helpers();
  for (const count of [0,1,2]) {
    const html=h.fuelWidget(fuelData(syntheticId(1),count));
    assert.ok(html.includes(`Collecting fuel data · ${count} / 3 valid samples`));
    assert.match(html,/Stintara consumption: <strong>—/);
    assert.match(html,/Estimated range: <strong>—/);
    assert.match(html,/Provider consumption: 99.00 L\/lap/);
  }
  const data=fuelData(syntheticId(1)), html=h.fuelWidget(data);
  assert.match(html,/Current fuel: <strong>42.00 L/);
  assert.match(html,/Stintara consumption: <strong>2.80 L\/lap/);
  assert.match(html,/Estimated range: <strong>15.0 laps/);
  assert.match(html,/Valid samples used: 3/);
  assert.match(html,/Provider comparison/);
  assert.match(html,/not part of the Stintara calculation/);
  assert.match(html,/Fuel sample diagnostics/);
  assert.match(html,/pitstop_transition/);
  assert.doesNotMatch(html,/Fuel to finish|Refuel recommendation|Finish margin/i);
  data.current.fresh=false;
  const stale=h.fuelWidget(data);
  assert.match(stale,/Estimated range: <strong>—/);
  assert.match(stale,/Stintara consumption: <strong>2.80 L\/lap/);
  assert.match(stale,/unavailable or stale/);
  data.current.fuel_l=null; data.provider={};
  assert.doesNotMatch(h.fuelWidget(data),/Provider comparison/);
});

test('fuel rejects late responses across A-B-A focus switches and clears errors', async () => {
  const field=syntheticField(2), pending=[];
  const page=await sessionHarness(sessionData(field),'',id=>new Promise(resolve=>pending.push({id,resolve})));
  page.change(field[0].id);
  page.change(field[1].id);
  page.change(field[0].id);
  const answer=(index,fuel)=>pending[index].resolve({ok:true,json:async()=>{
    const data=fuelData(pending[index].id); data.current.fuel_l=fuel; return data;
  }});
  answer(2,30); await settleFuel();
  answer(0,10); answer(1,20); await settleFuel();
  assert.match(page.fuel.innerHTML,/Current fuel: <strong>30.00 L/);
  assert.doesNotMatch(page.fuel.innerHTML,/Current fuel: <strong>(10|20).00 L/);
  page.change(field[1].id); pending[3].resolve({ok:false}); await settleFuel();
  assert.equal(page.fuel.innerHTML,'');
  assert.match(page.fuel.textContent,/unavailable/);
  page.change(field[0].id); page.change(''); answer(4,50); await settleFuel();
  assert.match(page.fuel.textContent,/Choose a participant/);
  assert.equal(page.fuel.innerHTML,'');
});

test('fuel diagnostics escape data and details stay open through polling', async () => {
  const data=fuelData(syntheticId(1));
  data.samples[0].excluded_reason='<img onerror="x">';
  data.current.recorded_at='<script>';
  const html=helpers().fuelWidget(data);
  assert.doesNotMatch(html,/<img|<script>/);
  assert.match(html,/&lt;img/);
  const field=syntheticField(1), page=await sessionHarness(sessionData(field),`?focus=${field[0].id}`);
  page.details.open=true;
  await page.poll(sessionData(field));
  assert.equal(page.details.open,true);
});


test('race full-field gaps, lap deficits and signed H2H use validated adjacency', async () => {
  const field=syntheticField(3).map((p,i)=>({...p,position:11+i,gap_to_leader:[14.3,14.8,16.1][i],
    gap_laps:null,laps_behind_leader:0,interval:i ? [null,.5,1.3][i] : null,
    interval_preceding_id:i ? syntheticId(i) : null}));
  const data={...sessionData(field),session:{session_type:'Race',track_name:'Synthetic Circuit'},
    timing:{configured_laps:20,configured_minutes:null,elapsed_seconds:3661.9}};
  const page=await sessionHarness(data,`?focus=${field[1].id}`);
  for (const value of ['14.8 s','0.5 s','Interval to focus</dt><dd>-0.5 s',
    'Interval to focus</dt><dd>+1.3 s','20 laps','1:01:01','Completed laps']) assert.ok(page.root.innerHTML.includes(value),value);
  const h=helpers();
  assert.equal(h.intervalToFocus(field[1],field[1],true),null);
  assert.equal(h.intervalToFocus(field[0],field[1],false),null);
  assert.equal(h.intervalToFocus({...field[2],interval_preceding_id:syntheticId(90)},field[1],true),null);
  assert.equal(h.gapText({gap_laps:2,gap_to_leader:-8},true),'2 L');
  for (const value of [-2,NaN,Infinity,null]) assert.equal(h.gapText({gap_to_leader:value},true),'—');
  assert.equal(h.gapText({gap_to_leader:0},true),'0.0 s');
  assert.equal(h.gapText({gap_laps:1},false),'—');
  assert.equal(h.intervalText({gap_laps:1,gap_to_leader:4,interval:1},true),'—');
});

test('session clock and focused persisted sectors label completed laps without projections', async () => {
  const field=syntheticField(1);
  Object.assign(field[0],{total_laps:0,sector_lap_number:2,sector1:30.5,sector2:31.25,sector3:null});
  const data={...sessionData(field),session:{session_type:'Race'},
    timing:{configured_minutes:30,configured_laps:null,elapsed_seconds:0}};
  const page=await sessionHarness(data,`?focus=${field[0].id}`);
  for (const value of ['30 min','0:00:00','0 completed laps','checkpoint lap 2','0:30.500','0:31.250'])
    assert.ok(page.root.innerHTML.includes(value),value);
  assert.doesNotMatch(page.root.innerHTML,/remaining laps|Current lap|Remaining time|official remaining/i);
  await page.poll({...data,live:false,timing:{...data.timing,elapsed_seconds:123}});
  assert.match(page.root.innerHTML,/Elapsed session time: —/);
  const h=helpers();
  for (const value of [null,-1,Infinity]) assert.equal(h.clockText(value),'—');
  assert.match(h.focusTiming([{...field[0],sector1:-1,sector2:Infinity}],field[0].id),/S1<div class="value">—/);
});

test('single-class detection requires the entire nonempty field and preserves source strings', () => {
  const h=helpers();
  for (const classes of [[], ['GT3',null], ['GT3',undefined], ['GT3',''], ['GT3','  '], ['GT3','Hypercar'], ['GT3','gt3']]) {
    assert.equal(h.singleClass(classes.map(vehicle_class => ({vehicle_class}))),null);
  }
  const field=[{vehicle_class:' GT3 '},{vehicle_class:'GT3'}];
  assert.equal(h.singleClass(field),'GT3');
  assert.equal(field[0].vehicle_class,' GT3 ');
});

test('single class appears once; full field, relative and H2H keep class for mixed or unknown fields', async () => {
  const field=syntheticField(3).map(p => ({...p,vehicle_class:'GT3'}));
  const h=await sessionHarness(sessionData(field),`?focus=${field[1].id}`);
  assert.equal((h.root.innerHTML.match(/GT3/g)||[]).length,1);
  assert.doesNotMatch(h.root.innerHTML,/<th[^>]*>Class<\/th>|<dt>Class<\/dt>/);
  const full=h.root.innerHTML.split('<h2>Full field</h2>')[1];
  assert.equal((full.match(/<th scope=/g)||[]).length,9);
  for (const value of ['Hypercar', null, '', '  ']) {
    const mixed=field.map(p => ({...p})); mixed[2].vehicle_class=value;
    await h.poll(sessionData(mixed));
    assert.match(h.root.innerHTML,/<th scope="col">Class<\/th>/);
    assert.match(h.root.innerHTML,/<th>Class<\/th>/);
    assert.match(h.root.innerHTML,/<dt>Class<\/dt>/);
    assert.equal((h.root.innerHTML.split('<h2>Full field</h2>')[1].match(/<th scope=/g)||[]).length,10);
  }
  await h.poll(sessionData([]));
  assert.match(h.root.innerHTML,/colspan="10"/);
  const escaped=await render('session.html',sessionData([{vehicle_class:'<GT3>',display_name:'<Driver>'}]));
  assert.match(escaped.innerHTML,/&lt;GT3&gt;/);
  assert.doesNotMatch(escaped.innerHTML,/<GT3>|<Driver>/);
});

function syntheticWeather() {
  return {current:{air_temp_c:24,track_temp_c:41,rain_intensity:0,wetness_avg:.1,
    wetness_min:0,wetness_max:.2,cloudiness:.25,wind_speed_mps:3},
    forecast:{timeline_complete:true,nodes:Array.from({length:5},(_,i) => ({position_percent:i*25,
      temperature_c:24+i,rain_chance:0,humidity_percent:50,wind_direction:'NE',wind_speed_mps:2}))}};
}

test('both weather views format current and forecast units; stale session labels last stored', async () => {
  const weather=syntheticWeather();
  for (const file of ['session.html','live.html']) {
    const data={...sessionData([]),driver:{display_name:'Synthetic Driver'}}; data.session.weather=weather;
    const html=(await render(file,data)).innerHTML;
    for (const text of ['Current conditions','Air: 24.0 °C','Track: 41.0 °C','Rain: 0 %',
      'Track wetness: 10 %','Cloudiness: 25 %','Wind: 3.0 m/s','Start','25 %','50 %','75 %','Finish','Humidity: 50.0 %','NE 2.0 m/s']) assert.ok(html.includes(text),text);
    assert.equal((html.match(/<li>/g)||[]).length,5);
  }
  const data=sessionData([]); data.live=false; data.session.weather=weather;
  const html=(await render('session.html',data)).innerHTML;
  assert.match(html,/Last stored conditions/); assert.doesNotMatch(html,/Current conditions/);
});

test('partial weather has no invented timeline; missing and hostile fields render safely', () => {
  const h=helpers(), weather=syntheticWeather();
  weather.forecast={timeline_complete:false,nodes:[{temperature_c:24},{rain_chance:.2,wind_direction:'<img onerror=alert(1)>'}]};
  let html=h.weatherWidget(weather,true);
  assert.match(html,/<strong>Forecast 1<\/strong>/); assert.match(html,/<strong>Forecast 2<\/strong>/);
  assert.doesNotMatch(html,/<strong>(Start|Finish|25 %|50 %|75 %)<\/strong>|<img/);
  assert.equal((html.match(/<li>/g)||[]).length,2);
  for (const value of [undefined,null,{}, {current:{air_temp_c:NaN,track_temp_c:Infinity,rain_intensity:true,cloudiness:'<script>'}}]) {
    html=h.weatherWidget(value,false);
    assert.match(html,/Forecast unavailable/); assert.match(html,/Air: —/);
    assert.doesNotMatch(html,/NaN|Infinity|undefined|<script>|\[object Object\]/);
  }
});

test('driver forecast expansion survives the existing refresh loop', async () => {
  const root={innerHTML:'',textContent:''}, details={open:true};
  let next, done;
  const data={live:true,driver:{display_name:'Synthetic'},session:{weather:syntheticWeather()},participants:[]};
  const script=readFileSync(path.join(__dirname,'../server/static/live.html'),'utf8')
    .match(/<script>([\s\S]*?)<\/script>/)[1].replace('__WEATHER_UI__',weatherScript()).replace('__DRIVER_SLUG__','"synthetic"');
  const wait=new Promise(resolve => { done=resolve; });
  vm.runInNewContext(script,{document:{querySelector: id => id==='#root' ? root : details},
    fetch:async () => ({ok:true,json:async () => data}),setTimeout:fn => {next=fn;done();}});
  await wait;
  assert.equal(details.open,true);
  assert.match(root.innerHTML,/<details id="weather-forecast">/);
  details.open=false;
  await next();
  assert.equal(details.open,false);
});

test('compact focus status reuses timing rules, escapes names and distinguishes pit state', () => {
  const h=helpers(), p={...syntheticField(1)[0],display_name:'<img onerror="x">',position:8,
    gap_to_leader:14.8,interval:.5,total_laps:23,last_lap_time:102.381,best_lap_time:101.904,in_pits:false};
  let html=h.focusStatus([p],p.id,true);
  for (const value of ['Focus: &lt;img onerror=&quot;x&quot;&gt;','P8','Gap 14.8 s','Interval 0.5 s',
    '23 completed laps','Last 1:42.381','Best 1:41.904','TRACK']) assert.ok(html.includes(value),value);
  assert.doesNotMatch(html,/<img|<script>/);
  assert.match(h.focusStatus([{...p,in_pits:true}],p.id,true),/<span class="badge pit">PIT<\/span>/);
  assert.doesNotMatch(h.focusStatus([{...p,in_pits:null}],p.id,true),/TRACK|PIT/);
  for (const id of [null,'',syntheticId(90),'not-a-uuid']) assert.equal(h.focusStatus([p],id,true),'');
  assert.equal(h.focusStatus([],p.id,true),'');
  for (const race of [true,false]) {
    html=h.focusStatus([{...p,gap_to_leader:-1,interval:null}],p.id,race);
    assert.match(html,/Gap —/); assert.match(html,/Interval —/);
  }
  html=h.focusStatus([p],p.id,false);
  assert.match(html,/Gap —/); assert.match(html,/Interval —/);
  html=h.focusStatus([{...p,gap_laps:1}],p.id,true);
  assert.match(html,/Gap 1 L/); assert.match(html,/Interval —/);
});

test('focus summary follows UUID selection and precedes larger sections without removing context', async () => {
  const field=syntheticField(2), data={...sessionData(field),timing:{configured_minutes:80,elapsed_seconds:120}};
  data.session.session_type='Race';
  const page=await sessionHarness(data);
  assert.doesNotMatch(page.root.innerHTML,/aria-label="Focus status"/);
  page.change(field[0].id);
  let html=page.root.innerHTML;
  assert.ok(html.indexOf('class="status"') < html.indexOf('aria-label="Focus status"'));
  assert.ok(html.indexOf('aria-label="Focus status"') < html.indexOf('aria-label="Weather"'));
  for (const value of ['80 min','0:02:00','Focused timing','Relative','Head-to-Head','Full field']) assert.ok(html.includes(value),value);
  await page.poll({...data,participants:[field[1]]});
  assert.doesNotMatch(page.root.innerHTML,/aria-label="Focus status"/);
});

test('checkpoint age accepts absolute timestamps only and bounds clock noise and old ages', () => {
  const h=helpers(), now=Date.parse('2026-10-25T12:00:00Z');
  for (const [timestamp,expected] of [
    ['2026-10-25T11:59:59.500000Z','<1 s ago'],['2026-10-25T11:59:56Z','4 s ago'],
    ['2026-10-25T11:58:00Z','2 min ago'],['2026-10-25T11:00:00Z','1 h ago'],
    ['2026-10-25T13:59:56+02:00','4 s ago'],['2026-10-25T12:00:00.500Z','<1 s ago'],
    ['2026-10-24T12:00:00Z','24+ h ago'],['2026-10-25T12:00:02Z','unknown'],
    [null,'unknown'],[undefined,'unknown'],['invalid','unknown'],[0,'unknown'],
    ['2026-10-25T11:59:56','unknown'],['2026-10-25','unknown'],
    ['2026-02-30T00:00:00Z','unknown'],['2026-10-25T24:00:00Z','unknown'],
    ['2026-10-25T12:00:00+99:00','unknown'],['2026-10-25T12:00:00-00:00','unknown'],
  ]) assert.equal(h.checkpointAge(timestamp,now),expected,String(timestamp));
  for (const value of [NaN,Infinity,null]) assert.equal(h.checkpointAge('2026-10-25T12:00:00Z',value),'unknown');
});

test('checkpoint age never determines live or finished state', () => {
  const h=helpers(), now=Date.parse('2026-10-25T12:00:00Z');
  assert.equal(h.feedStatus({live:true,session:{last_seen_at:'2026-10-25T11:58:00Z'}},now),
    'Receiving scoring · last checkpoint 2 min ago');
  assert.equal(h.feedStatus({live:false,session:{last_seen_at:'2026-10-25T11:59:59.5Z'}},now),
    'No live scoring — showing last stored standings · last checkpoint <1 s ago');
  assert.match(h.feedStatus({live:false,session:{}},now),/last checkpoint unknown/);
});

test('engineer dashboard keeps live and stored cues explicit alongside existing widgets', async () => {
  const field=syntheticField(50), data=sessionData(field);
  data.session.weather=syntheticWeather();
  const page=await sessionHarness(data,`?focus=${field[7].id}`);
  const html=page.root.innerHTML;
  assert.match(html,/class="feed feed-live"/);
  assert.match(html,/class="live-dot" aria-hidden="true"/);
  assert.match(html,/>LIVE<\/span>/);
  for(const text of ['50 participants','Full field','Focus status','Head-to-Head','Relative standings',
    'Current conditions','Track geometry not available yet','Flags / Race Control / Car Status · not available yet']) assert.ok(html.includes(text),text);
  assert.match(page.fuel.innerHTML,/Current fuel: <strong>42.00 L/);
  await page.poll({...data,live:false});
  assert.match(page.root.innerHTML,/class="feed feed-stale"/);
  assert.match(page.root.innerHTML,/>STORED<\/span>/);
  assert.match(page.root.innerHTML,/No live scoring — showing last stored standings/);
  assert.match(page.root.innerHTML,/Last stored conditions/);
  assert.doesNotMatch(page.root.innerHTML,/feed-live|>LIVE<\/span>/);
  assert.match(page.root.innerHTML,/50 entries/);
});

test('Full Field highlights only PIT while focused driver retains explicit TRACK state', async () => {
  const field=syntheticField(3);
  field[0].in_pits=false;
  field[1].in_pits=true;
  field[2].in_pits=null;
  const page=await sessionHarness(sessionData(field),`?focus=${field[0].id}`);
  const full=page.root.innerHTML.split('<h2>Full field</h2>')[1];
  assert.match(full,/<span class="badge pit">PIT<\/span>/);
  assert.doesNotMatch(full,/TRACK|badge track/);
  assert.match(full,/<td>—<\/td><\/tr>/);
  assert.match(page.root.innerHTML,/<span class="badge track">TRACK<\/span>/);
  const source=readFileSync(path.join(__dirname,'../server/static/session.html'),'utf8');
  assert.match(source,/<h2>Strategy <small class="muted">· Experimental<\/small><\/h2>/);
  assert.doesNotMatch(source,/Fuel \/ Strategy/);
});

test('dashboard disclosures, keyboard scroll focus and both scroll axes survive polling', async () => {
  const data=sessionData(syntheticField(50)), page=await sessionHarness(data);
  page.relativeDetails.open=true;
  page.document.activeElement={id:'full-scroll'};
  await page.poll(data);
  assert.equal(page.relativeDetails.open,true);
  assert.equal(page.scrolls['#full-scroll'].scrollTop,400);
  assert.equal(page.scrolls['#full-scroll'].scrollLeft,31);
  assert.equal(page.scrolls['#full-scroll'].focused,true);
  assert.equal(page.scrolls['#relative-scroll'].scrollTop,20);
  assert.equal(page.scrolls['#weather-panel'].scrollTop,5);
  page.change(data.participants[0].id);
  await settleFuel();
  page.document.activeElement={id:'fuel-toggle'};
  await page.poll(data);
  assert.equal(page.scrolls['#fuel-toggle'].focused,true);
});

test('fuel headline values remain separate from collapsed provider diagnostics', () => {
  const html=helpers().fuelWidget(fuelData(syntheticId(1)));
  const [primary,diagnostics]=html.split('<details id="fuel-diagnostics">');
  for(const value of ['Current fuel:','Stintara consumption:','Estimated range:','Valid samples used:']) assert.ok(primary.includes(value));
  assert.doesNotMatch(primary,/Provider consumption|Strategy checkpoint/);
  assert.match(diagnostics,/Provider comparison/);
  assert.match(diagnostics,/Fuel sample diagnostics/);
});

test('dashboard source bounds desktop panels, keeps build identity and supports reduced motion', () => {
  const html=readFileSync(path.join(__dirname,'../server/static/session.html'),'utf8');
  const css=html.match(/<style>([\s\S]*?)<\/style>/)[1];
  assert.match(css,/height:100dvh/);
  assert.match(css,/grid-template-rows:auto minmax\(0,1fr\) auto/);
  assert.match(css,/\.table-scroll\{[^}]*overflow:auto/);
  assert.match(css,/position:sticky;top:0/);
  assert.match(css,/@media\(prefers-reduced-motion:reduce\)/);
  assert.match(css,/animation:none!important;transition:none!important/);
  assert.match(css,/\.feed-live \.live-dot\{animation:live-pulse/);
  assert.doesNotMatch(css,/\.feed-stale[^}]*animation:live-pulse/);
  assert.match(html,/<footer>__BUILD_LABEL__<\/footer>/);
  assert.match(html,/tabindex="0" role="region" aria-label="Full field standings"/);
  assert.match(html,/<label for="focus">Focus participant<\/label>/);
});

test('pit and finish states take precedence without contaminating timing columns', async () => {
  const {engineerDemo}=require('../tools/engineer_demo.cjs');
  const demo=engineerDemo();
  assert.deepEqual(demo,engineerDemo());
  const page=await sessionHarness(demo.data,`?focus=${demo.focus}`);
  const full=page.root.innerHTML.split('<h2>Full field</h2>')[1];
  assert.equal((full.match(/<tr class=/g)||[]).length,40);
  for(const text of ['>Stops</th>','>Status</th>','1 · L12','2 · L27','>PIT</span>','>FIN</span>','>DNF</span>','>DSQ</span>']) assert.ok(full.includes(text),text);
  assert.equal((full.match(/>PIT<\/span>/g)||[]).length,1); // Finish overrides contradictory pit occupancy.
  assert.doesNotMatch(full,/TRACK/);
  assert.match(page.root.innerHTML,/1 stop · Last stop L12/);
  const rows=full.match(/<tr class=[\s\S]*?<\/tr>/g);
  for(const row of rows) {
    const cells=Array.from(row.matchAll(/<td[^>]*>([\s\S]*?)<\/td>/g),m=>m[1]);
    assert.doesNotMatch(cells[5]+cells[6],/FIN|DNF|DSQ|PIT/); // Single-class Gap / Interval.
  }
  page.change(demo.data.participants[5].id);
  assert.match(page.root.innerHTML.split('aria-label="Focus status"')[1].split('</p>')[0],/>DNF<\/span>/);
});

test('unknown stop counts and unsupported finish codes do not acquire guessed meanings', () => {
  const h=helpers();
  for(const value of [null,undefined,-1,1.5,'2',true,NaN,Infinity]) assert.equal(h.stopsText({pitstops:value}),'—');
  assert.equal(h.stopsText({pitstops:1,last_pit_lap:null}),'1');
  assert.equal(h.stopsText({pitstops:0,last_pit_lap:12}),'0');
  assert.equal(h.stopsText({pitstops:2,last_pit_lap:27},true),'2 stops · Last stop L27');
  for(const value of [null,undefined,-1,99,'1',true]) {
    assert.equal(h.participantStatus({finish_status:value,in_pits:false},true),'—');
  }
  assert.match(h.participantStatus({finish_status:0,in_pits:true}),/>PIT<\/span>/);
  assert.equal(h.participantStatus({finish_status:0,in_pits:false}),'—');
});
const strategyDemo = () => structuredClone(require('../tools/engineer_demo.cjs').engineerDemo().strategy);

test('LMU resource presentation has explicit units, limiting VE and actionable settings', () => {
  const d=strategyDemo(),html=helpers().strategyWidget(d);
  for(const text of ['Virtual Energy','LIMIT','63.0%','10.2%/lap','6.2 laps','SET VE TO 72%',
    'FUEL RATIO 0.85','Fuel carried ≈ 61.2 L','Expected added ≈ 25.6 L','Reserve 1.0 lap','Latest resource-safe stop L33']) assert.ok(html.includes(text),text);
  assert.doesNotMatch(html,/current ratio|Optimal stop|NaN|undefined|Infinity/);
});

test('fuel-only LMU settings omit irrelevant VE and future providers do not inherit LMU labels', () => {
  const d=strategyDemo(); d.resources=d.resources.slice(0,1);d.limiting_resource='fuel';
  d.provider_pit_settings={provider:'lmu',energy_kind:'NONE',fuel_target_l:42,expected_fuel_added_l:20};
  const html=helpers().strategyWidget(d);
  assert.match(html,/SET FUEL TO 42.0 L/); assert.doesNotMatch(html,/SET VE|FUEL RATIO|Virtual Energy/);
  d.simulator_provider='synthetic-provider';d.resources[0].pit_service_mode='ADD_AMOUNT';d.provider_pit_settings=null;
  assert.doesNotMatch(helpers().strategyWidget(d),/LMU PIT SETTINGS|SET FUEL TO|SET VE|FUEL RATIO/);
});

test('resource warnings distinguish Fuel and Energy shortfall, capacity and additional stops', () => {
  const d=strategyDemo();d.status='unsafe_plan';
  d.shortfall=[{resource_kind:'fuel',laps:1.2},{resource_kind:'energy',laps:.7}];
  d.provider_pit_settings.capacity_feasible=false;d.provider_pit_settings.additional_stop_required=true;
  const html=helpers().strategyWidget(d);
  for(const text of ['UNSAFE PLAN','Fuel short by 1.2 laps','VE short by 0.7 laps','ADDITIONAL STOP REQUIRED','Tank capacity insufficient']) assert.ok(html.includes(text));
  assert.match(html,/strategy-danger/);
});

test('finish estimates, balanced and no-stop states remain explicit', () => {
  const d=strategyDemo();d.finish_projection.kind='timed_estimate';d.balanced=true;
  assert.match(helpers().strategyWidget(d),/Projected finish ≈ L38/);
  assert.match(helpers().strategyWidget(d),/BALANCED/);
  d.status='no_stop_required';d.provider_pit_settings=null;
  const html=helpers().strategyWidget(d);
  assert.match(html,/strategy-safe/);assert.match(html,/No resource stop required/);assert.doesNotMatch(html,/SET VE/);
  d.status='box_this_lap';assert.match(helpers().strategyWidget(d),/BOX THIS LAP/);
});

test('unknown capability and unavailable values render safely without provider string injection', () => {
  const d=strategyDemo();d.status='capability_unknown';d.provider_pit_settings=null;
  d.resources[0].current=null;d.resources[0].sample_count='<img onerror=x>';
  const html=helpers().strategyWidget(d);
  assert.match(html,/Energy capability unknown/);assert.match(html,/&lt;img/);
  assert.doesNotMatch(html,/<img|NaN|Infinity|undefined|SET VE/);
});

test('versioned local plans isolate session and participant and reject malformed storage', () => {
  const h=helpers(),map=new Map(),storage={getItem:k=>map.get(k),setItem:(k,v)=>map.set(k,v),removeItem:k=>map.delete(k)};
  const sid=syntheticId(99),a=syntheticId(1),b=syntheticId(2);
  assert.ok(h.savePlan(storage,sid,a,32));assert.equal(h.readPlan(storage,sid,a),32);
  assert.equal(h.readPlan(storage,sid,b),null);assert.equal(h.readPlan(storage,syntheticId(98),a),null);
  assert.ok([...map.keys()][0].startsWith('stintara:strategy:v1:'));assert.doesNotMatch([...map.keys()][0],/lmu/i);
  for(const bad of [0,-1,2.5,NaN,'32',true]) assert.equal(h.savePlan(storage,sid,a,bad),false);
  map.set(h.strategyKey(sid,a),'malformed');assert.equal(h.readPlan(storage,sid,a),null);
  assert.ok(h.savePlan(storage,sid,a,null));assert.equal(map.size,0);
  assert.equal(h.savePlan(null,sid,a,32),false);
});

test('manual Apply, focus restore and AUTO use only read-only strategy requests', async () => {
  const field=syntheticField(2),page=await sessionHarness(sessionData(field));
  page.change(field[0].id);await settleFuel();
  page.planInput.value='8';page.planForm.submit({preventDefault(){}});await settleFuel();
  assert.match(page.fuelRequests.at(-1),/strategy\?planned_stop_lap=8$/);
  page.change(field[1].id);await settleFuel();assert.equal(page.planInput.value,'');
  page.planInput.value='9';page.planForm.submit({preventDefault(){}});await settleFuel();
  page.change(field[0].id);await settleFuel();assert.equal(page.planInput.value,8);
  page.planInput.value='4';page.planForm.submit({preventDefault(){}});
  assert.match(page.planFeedback.textContent,/later than completed/);
  page.planAuto.click();await settleFuel();assert.equal(page.planInput.value,'');
  assert.ok(page.fuelRequests.at(-1).endsWith('/strategy'));
});
