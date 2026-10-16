const assert = require('node:assert/strict');
const {test} = require('node:test');
const {readFileSync} = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

async function render(file, data, ok=true, status=200) {
  const html = readFileSync(path.join(__dirname, '../server/static', file), 'utf8');
  const script = html.match(/<script>([\s\S]*?)<\/script>/)[1].replace('__DRIVER_SLUG__', '"driver"');
  const root = {innerHTML: '', textContent: ''};
  let finish;
  const done = new Promise(resolve => { finish = resolve; });
  vm.runInNewContext(script, {
    document: {querySelector: () => root},
    fetch: async () => ({ok, status, json: async () => data}),
    setTimeout: finish,
  });
  await done;
  return root;
}

test('frontpage shows empty state, vehicle, escaped names and safe link', async () => {
  assert.match((await render('index.html', [])).innerHTML, /No Stintara drivers/);
  const root = await render('index.html', [{display_name:'<img onerror=alert(1)>', track_name:'Spa',
    car_name:'Porsche', session_type:'Race', slug:'driver', position:1}]);
  assert.match(root.innerHTML, /&lt;img/);
  assert.doesNotMatch(root.innerHTML, /<img/);
  assert.match(root.innerHTML, /Porsche/);
  assert.match(root.innerHTML, /\/drivers\/driver\/live/);
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
