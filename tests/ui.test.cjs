const assert = require('node:assert/strict');
const {test} = require('node:test');
const {readFileSync} = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

async function render(file, data, ok=true, status=200) {
  const html = readFileSync(path.join(__dirname, '../server/static', file), 'utf8');
  const script = html.match(/<script>([\s\S]*?)<\/script>/)[1].replace('__DRIVER_SLUG__', '"driver"').replace('__SESSION_ID__', '"session"');
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
  total_laps:4, last_lap_time:123.456, best_lap_time:122.345, driver_slug:null}));
const sessionData = participants => ({live:true,session:{id:syntheticId(99),track_name:'Synthetic Track'},participants});
function spectatorScript() {
  return readFileSync(path.join(__dirname, '../server/static/session.html'), 'utf8')
    .match(/<script>([\s\S]*?)<\/script>/)[1].replace('__SESSION_ID__', JSON.stringify(syntheticId(99)));
}
function helpers() {
  const context = vm.createContext({URL});
  vm.runInContext(spectatorScript().split('const sessionId =')[0], context);
  return context;
}
async function sessionHarness(data, query='', fuelReply=null) {
  const root = {innerHTML:'',textContent:''};
  const selector = {innerHTML:'',value:'',addEventListener(name, listener) { this[name] = listener; }};
  const scrolls = {'#full-scroll':{scrollLeft:31},'#relative-scroll':{scrollLeft:17}};
  const location = {href:'http://localhost/sessions/session/live'+query};
  const replacements = [], fuelRequests = [];
  const fuel = {html:'',text:'', get innerHTML() { return this.html; }, set innerHTML(value) { this.html=value; this.text=''; },
    get textContent() { return this.text; }, set textContent(value) { this.text=value; this.html=''; }};
  const details = {open:false};
  let queued, finish, currentData=data;
  let done = new Promise(resolve => { finish=resolve; });
  const context = vm.createContext({URL,location,
    history:{replaceState(state,unused,url) { replacements.push(url); location.href=new URL(url,location.href).href; }},
    document:{querySelector: name => name==='#root' ? root : name==='#focus' ? selector : name==='#fuel' ? fuel : name==='#fuel-diagnostics' ? details : scrolls[name]},
    fetch:async url => {
      if (!url.endsWith('/fuel')) return {ok:true,json:async () => currentData};
      fuelRequests.push(url);
      const id=url.split('/').at(-2);
      return fuelReply ? fuelReply(id) : {ok:true,json:async () => fuelData(id)};
    },
    setTimeout:callback => { queued=callback; finish(); },
  });
  vm.runInContext(spectatorScript(), context);
  await done;
  return {root,selector,location,replacements,scrolls,fuel,fuelRequests,details,
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
  assert.ok(page.fuelRequests.at(-1).endsWith(`/participants/${field[0].id}/fuel`));
  assert.match(page.fuel.innerHTML,/Stintara consumption: 2.80 L\/lap/);
  page.change(field[1].id);
  assert.equal(page.fuel.innerHTML,'');
  assert.match(page.fuel.textContent,/Loading/);
  await settleFuel();
  assert.ok(page.fuelRequests.at(-1).endsWith(`/participants/${field[1].id}/fuel`));
  await page.poll(sessionData(field.filter(p=>p.id!==field[1].id)));
  assert.equal(page.fuel.innerHTML,'');
  assert.match(page.fuel.textContent,/Choose a participant/);
});

test('fuel collecting, ready, stale and provider comparison remain distinct', () => {
  const h=helpers();
  for (const count of [0,1,2]) {
    const html=h.fuelWidget(fuelData(syntheticId(1),count));
    assert.ok(html.includes(`Collecting fuel data · ${count} / 3 valid samples`));
    assert.match(html,/Stintara consumption: —/);
    assert.match(html,/Estimated range: —/);
    assert.match(html,/Provider consumption: 99.00 L\/lap/);
  }
  const data=fuelData(syntheticId(1)), html=h.fuelWidget(data);
  assert.match(html,/Current fuel: 42.00 L/);
  assert.match(html,/Stintara consumption: 2.80 L\/lap/);
  assert.match(html,/Estimated range: 15.0 laps/);
  assert.match(html,/Valid samples used: 3/);
  assert.match(html,/Provider comparison/);
  assert.match(html,/not part of the Stintara calculation/);
  assert.match(html,/Fuel sample diagnostics/);
  assert.match(html,/pitstop_transition/);
  assert.doesNotMatch(html,/Fuel to finish|Refuel recommendation|Finish margin/i);
  data.current.fresh=false;
  const stale=h.fuelWidget(data);
  assert.match(stale,/Estimated range: —/);
  assert.match(stale,/Stintara consumption: 2.80 L\/lap/);
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
  assert.match(page.fuel.innerHTML,/Current fuel: 30.00 L/);
  assert.doesNotMatch(page.fuel.innerHTML,/Current fuel: (10|20).00 L/);
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
