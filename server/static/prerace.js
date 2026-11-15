// Shared pre-race planner UI (#66), injected into Driver Live (compact summary) and Engineer (detailed planner).
// The plan lives only in this browser (localStorage, per driver slug, never per session): it is a manual
// planning aid, not session telemetry, and survives the Qualifying -> Race session change because it is bound
// to nothing but the driver. All calculation happens on the server (GET /api/drivers/{slug}/prerace).
const PreRaceUI = (() => {
  const FIELDS = ['duration_minutes', 'race_laps', 'lap_time_s', 'fuel_per_lap_l', 've_per_lap_percent',
                  'tank_capacity_l', 'reserve_laps'];
  const LABELS = {duration_minutes: 'Race duration (min)', race_laps: 'Race laps', lap_time_s: 'Lap time (s)',
                  fuel_per_lap_l: 'Fuel per lap (L)', ve_per_lap_percent: 'VE per lap (%)',
                  tank_capacity_l: 'Tank capacity (L)', reserve_laps: 'Safety reserve (laps)'};
  const ISSUES = {
    missing_race_length: 'Race duration or lap count is missing', missing_lap_time: 'Lap time is missing',
    missing_fuel_per_lap: 'Fuel per lap is missing', missing_ve_per_lap: 'VE per lap is missing for this VE car',
    ve_capability_unknown: 'Unknown whether this car uses Virtual Energy', tank_capacity_unknown: 'Tank capacity unknown, range not checked',
    invalid_value: 'Invalid value', conflicting_race_length: 'Duration and lap count contradict each other',
    ve_rate_without_ve: 'VE rate given although VE is switched off'};
  const STATUS = {ready: 'Start recommendation', ready_capacity_unchecked: 'Start recommendation (tank not checked)',
                  stop_required: 'One start load does not cover the race', incomplete: 'No recommendation: data missing',
                  invalid_input: 'No recommendation: invalid input', conflicting_input: 'No recommendation: contradictory input'};
  const e = value => String(value ?? '—').replace(/[&<>"']/g, c => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[c]));
  const n = (value, digits = 1, unit = '') => Number.isFinite(value) ? value.toFixed(digits) + unit : '—';
  const key = slug => `stintara:prerace:v1:${slug}`;
  const empty = () => ({v: 1, mode: 'time', values: {}, ve_capable: null, sources: {}, context: null});

  function read(storage, slug) {
    try {
      const raw = JSON.parse(storage?.getItem(key(slug)) || 'null');
      if (!raw || raw.v !== 1 || typeof raw.values !== 'object' || !raw.values) return empty();
      const state = empty();
      state.mode = raw.mode === 'laps' ? 'laps' : 'time';
      for (const f of FIELDS) if (Number.isFinite(raw.values[f])) state.values[f] = raw.values[f];
      state.ve_capable = typeof raw.ve_capable === 'boolean' ? raw.ve_capable : null;
      for (const [f, s] of Object.entries(raw.sources || {}))
        if (['manual', 'observed', 'confirmed'].includes(s)) state.sources[f] = s;
      const c = raw.context;
      state.context = c && typeof c === 'object' ? {track_name: String(c.track_name ?? '').slice(0, 200), car_name: String(c.car_name ?? '').slice(0, 200)} : null;
      return state;
    } catch { return empty(); }
  }
  function write(storage, slug, state) {
    try { if (!storage) return false; storage.setItem(key(slug), JSON.stringify(state)); return true; } catch { return false; }
  }
  function clear(storage, slug) { try { storage?.removeItem(key(slug)); return true; } catch { return false; } }

  // Only the length field of the selected mode is sent, so the UI itself never produces contradictory input.
  function query(state) {
    const p = new URLSearchParams(), skip = state.mode === 'laps' ? 'duration_minutes' : 'race_laps', used = [];
    for (const f of FIELDS) {
      if (f === skip || !Number.isFinite(state.values[f])) continue;
      p.set(f, String(state.values[f]));
      used.push(`${f}:${state.sources[f] || 'manual'}`);
    }
    if (state.ve_capable !== null) { p.set('ve_capable', String(state.ve_capable)); used.push(`ve_capable:${state.sources.ve_capable || 'manual'}`); }
    p.set('sources', used.join(','));
    return p.toString();
  }
  async function load(slug, state, fetchFn) {
    const response = await fetchFn(`/api/drivers/${encodeURIComponent(slug)}/prerace?${query(state)}`, {cache: 'no-store'});
    if (!response.ok) throw new Error('pre-race plan unavailable');
    const data = await response.json();
    if (data?.driver?.slug !== slug) throw new Error('pre-race plan belongs to another driver');
    return data;
  }
  // A saved plan is never re-bound to a session by track or time; a differing context is only reported.
  function contextWarning(state, context) {
    if (!state.context || !context) return null;
    const diff = [];
    if (state.context.track_name && context.track_name && state.context.track_name !== context.track_name) diff.push(`track ${state.context.track_name} → ${context.track_name}`);
    if (state.context.car_name && context.car_name && state.context.car_name !== context.car_name) diff.push(`car ${state.context.car_name} → ${context.car_name}`);
    return diff.length ? `Plan was made for a different context (${diff.join(', ')}). Check the inputs.` : null;
  }
  const lengthText = plan => {
    const d = plan.distance;
    return d?.kind === 'timed' ? `${n(d.duration_minutes, 0)} min · timed` : d?.kind === 'laps' ? `${d.race_laps} laps` : '—';
  };
  const lapsText = (plan, long = false) => {
    const d = plan.distance;
    if (!d || d.laps_planning === null) return '—';
    return d.kind === 'laps' ? `${d.laps_planning} laps` : `${d.laps_min}–${d.laps_planning} laps${long ? ' (planning bound)' : ''}`;
  };
  function quality(plan, state, context) {
    const manual = Object.entries(plan.sources || {}).filter(([, s]) => s === 'manual').map(([f]) => LABELS[f] || f);
    const observed = Object.keys(plan.sources || {}).filter(f => plan.sources[f] !== 'manual');
    const parts = [];
    if (manual.length) parts.push(`Manual: ${manual.length} input${manual.length === 1 ? '' : 's'}`);
    if (observed.length) parts.push(`Adopted from observation: ${observed.length}`);
    const lines = parts.length ? [parts.join(' · ')] : [];
    for (const i of plan.issues || []) lines.push(`${ISSUES[i.code] || i.code}${i.field && i.code === 'invalid_value' ? `: ${LABELS[i.field] || i.field}` : ''}`);
    const warning = contextWarning(state, context);
    if (warning) lines.push(warning);
    return lines;
  }
  const startText = plan => {
    if (plan.start) return `${n(plan.start.fuel_l)} L`;
    return plan.status === 'stop_required' ? 'More than one load' : '—';
  };
  const veText = plan => plan.start?.ve_percent != null ? `${n(plan.start.ve_percent)} %` : plan.energy?.capable === false ? 'No VE car' : '—';

  // Driver Live: compact, read-only. No form, no evidence.
  function summary(resp, state, engineerHref) {
    const plan = resp.plan, hasInput = Object.keys(state.values).length > 0 || state.ve_capable !== null;
    const link = engineerHref ? `<a href="${e(engineerHref)}">Open planner →</a>` : '';
    if (!hasInput) return `<section class="panel pr-panel" aria-label="Race prep"><h2>Race prep</h2><p class="muted">No race plan. Enter race length, pace and consumption in the Engineer pre-race planner. ${link}</p></section>`;
    const cls = plan.status === 'ready' ? 'pr-ok' : plan.status === 'ready_capacity_unchecked' ? 'pr-warn' : 'pr-bad';
    const lines = quality(plan, state, resp.context);
    return `<section class="panel pr-panel" aria-label="Race prep"><h2>Race prep <small class="muted">· estimate</small></h2>
      <div class="pr-grid"><div><div class="muted">Race</div><div class="value">${e(lengthText(plan))}</div></div>
      <div><div class="muted">Expected laps</div><div class="value">${e(lapsText(plan))}</div></div>
      <div><div class="muted">Start fuel</div><div class="value ${cls}">${e(startText(plan))}</div></div>
      <div><div class="muted">Start VE</div><div class="value">${e(veText(plan))}</div></div>
      <div><div class="muted">Reserve</div><div class="value">${e(n(plan.reserve_laps, 1))} lap</div></div></div>
      <p class="${cls}"><strong>${e(STATUS[plan.status] || 'Pre-race plan unavailable')}</strong></p>
      ${lines.length ? `<ul class="pr-notes muted">${lines.map(l => `<li>${e(l)}</li>`).join('')}</ul>` : ''}${link}</section>`;
  }

  function suggestion(field, ev, state) {
    if (!ev) return '';
    const map = {
      lap_time_s: [ev.lap_time?.value, 3, `median of ${ev.lap_time?.sample_count} clean laps`],
      fuel_per_lap_l: [ev.fuel_per_lap?.value, 2, `median of ${ev.fuel_per_lap?.sample_count} valid laps`],
      ve_per_lap_percent: [ev.ve_per_lap_percent?.value, 2, `${ev.ve_per_lap_percent?.sample_count} VE laps`],
      tank_capacity_l: [ev.tank_capacity_l, 1, 'from car telemetry'],
      duration_minutes: [ev.race_rules?.duration_minutes, 1, 'confirmed race rules'],
      race_laps: [ev.race_rules?.race_laps, 0, 'confirmed race rules'],
    }[field];
    if (!map || !Number.isFinite(map[0])) return '';
    const caveat = ev.representative_of_race === true || field === 'tank_capacity_l' || field.startsWith('race') || field === 'duration_minutes' ? '' : ` · ${e(ev.session_type)} session, not race-representative`;
    return `<span class="muted pr-hint">Observed ${e(n(map[0], map[1]))} (${e(map[2])}${caveat}) <button type="button" data-adopt="${e(field)}" data-value="${e(map[0].toFixed(map[1]))}">Use</button></span>`;
  }
  const input = (field, state, ev) => {
    const v = state.values[field], src = state.sources[field];
    return `<div class="pr-field"><label for="pr-${field}">${e(LABELS[field])}${src ? ` <small class="muted">· ${e(src)}</small>` : ''}</label>
      <input id="pr-${field}" data-field="${field}" inputmode="decimal" autocomplete="off" value="${Number.isFinite(v) ? e(v) : ''}">${suggestion(field, ev, state)}</div>`;
  };
  // Engineer: complete planner.
  function engineer(resp, state) {
    const plan = resp.plan, ev = resp.evidence, laps = state.mode === 'laps';
    const veChoice = state.ve_capable === null ? 'unknown' : state.ve_capable ? 'yes' : 'no';
    const veEvidence = typeof ev?.ve_capable === 'boolean' && ev.ve_capable !== state.ve_capable
      ? `<span class="muted pr-hint">Telemetry says ${ev.ve_capable ? 'VE car' : 'no VE'} <button type="button" data-adopt-ve="${ev.ve_capable}">Use</button></span>` : '';
    const rules = ev ? (ev.race_rules?.available ? `Race rules confirmed: ${ev.race_rules.race_laps ? `${ev.race_rules.race_laps} laps` : `${n(ev.race_rules.duration_minutes, 1)} min`}`
      : `Race length: not available${ev.session_type === 'Race' ? '' : ' — the upcoming race is not described by the current session; enter it manually'}`) : 'No live session of this driver: manual inputs only';
    const fuel = plan.fuel, energy = plan.energy;
    const lines = quality(plan, state, resp.context);
    const cls = plan.status === 'ready' ? 'pr-ok' : plan.status === 'ready_capacity_unchecked' ? 'pr-warn' : 'pr-bad';
    return `<form id="pr-form" class="pr-form" novalidate>
      <p class="muted">${e(rules)}</p>
      <fieldset class="pr-mode"><legend>Race length</legend>
        <label><input type="radio" name="pr-mode" value="time" ${laps ? '' : 'checked'}> Timed</label>
        <label><input type="radio" name="pr-mode" value="laps" ${laps ? 'checked' : ''}> Lap limit</label></fieldset>
      ${input(laps ? 'race_laps' : 'duration_minutes', state, ev)}${laps ? '' : input('lap_time_s', state, ev)}${laps ? input('lap_time_s', state, ev) : ''}
      ${input('fuel_per_lap_l', state, ev)}
      <div class="pr-field"><label for="pr-ve">Virtual Energy car</label><select id="pr-ve"><option value="unknown" ${veChoice === 'unknown' ? 'selected' : ''}>Unknown</option>
        <option value="yes" ${veChoice === 'yes' ? 'selected' : ''}>Yes</option><option value="no" ${veChoice === 'no' ? 'selected' : ''}>No</option></select>${veEvidence}</div>
      ${state.ve_capable === true ? input('ve_per_lap_percent', state, ev) : ''}
      ${input('tank_capacity_l', state, ev)}${input('reserve_laps', state, ev)}
      <div class="pr-actions"><button type="submit">Calculate</button><button type="button" id="pr-clear">Clear plan</button><span id="pr-feedback" class="muted" role="status"></span></div>
      <p class="muted">Manual planning values. Nothing is stored on the server and LMU pit settings are not changed.</p></form>
    <div class="pr-result" aria-label="Pre-race result">
      <p class="${cls}"><strong>${e(STATUS[plan.status] || 'Pre-race plan unavailable')}</strong></p>
      <dl class="pr-dl"><dt>Race</dt><dd>${e(lengthText(plan))}</dd><dt>Expected laps</dt><dd>${e(lapsText(plan, true))}</dd>
      <dt>Pace used</dt><dd>${plan.distance?.pace_s ? `${n(plan.distance.pace_s, 3)} s (planning ${n(plan.pace_margin * 100, 0)} % faster)` : '—'}</dd>
      ${fuel ? `<dt>Fuel</dt><dd>${n(fuel.per_lap_l, 2)} L/lap × (${plan.distance.laps_planning} + ${n(plan.reserve_laps, 1)} reserve) = ${n(fuel.required_l)} L${fuel.tank_capacity_l ? ` · tank ${n(fuel.tank_capacity_l)} L = ${n(fuel.full_tank_laps)} laps` : ' · tank unknown'}</dd>
        ${fuel.single_stint_feasible === false ? `<dt>Shortfall</dt><dd class="pr-bad">${n(fuel.shortfall_l)} L more than one full tank</dd>` : ''}` : ''}
      ${energy?.capable ? `<dt>Virtual Energy</dt><dd>${n(energy.per_lap_percent, 2)} %/lap → ${n(energy.required_percent)} % of 100 %${energy.limiting_resource ? ` · limits: ${e(energy.limiting_resource)}` : ''}</dd>
        ${energy.single_stint_feasible === false ? `<dt>VE shortfall</dt><dd class="pr-bad">${n(energy.shortfall_percent)} % more than a full VE tank</dd>` : ''}` : ''}
      <dt>Start</dt><dd><strong class="${cls}">${e(startText(plan))}${plan.start?.ve_percent != null ? ` · ${e(n(plan.start.ve_percent))} % VE` : ''}</strong></dd></dl>
      ${lines.length ? `<ul class="pr-notes muted">${lines.map(l => `<li>${e(l)}</li>`).join('')}</ul>` : ''}
      <p class="muted">Estimate, not a guarantee: formation, out-lap and in-lap are covered only by the reserve. Pit-stop planning stays in the live Strategy below.</p></div>`;
  }

  // Wires one container. `ctx.slug()` names the driver, `ctx.context()` returns the latest session context.
  function bind(box, ctx) {
    const rerun = () => ctx.refresh(true);
    box.addEventListener('submit', event => {
      event.preventDefault();
      const slug = ctx.slug(); if (!slug) return;
      const state = read(ctx.storage, slug), feedback = box.querySelector('#pr-feedback');
      state.mode = box.querySelector('input[name="pr-mode"]:checked')?.value === 'laps' ? 'laps' : 'time';
      for (const field of box.querySelectorAll('[data-field]')) {
        const f = field.dataset.field, text = field.value.trim().replace(',', '.');
        if (text === '') { delete state.values[f]; delete state.sources[f]; continue; }
        const value = Number(text);
        if (!Number.isFinite(value)) { if (feedback) feedback.textContent = `${LABELS[f]}: not a number`; return; }
        if (state.values[f] !== value) state.sources[f] = 'manual';
        state.values[f] = value;
      }
      const ve = box.querySelector('#pr-ve')?.value;
      const capable = ve === 'yes' ? true : ve === 'no' ? false : null;
      if (capable !== state.ve_capable) state.sources.ve_capable = 'manual';
      state.ve_capable = capable;
      if (capable !== true) { delete state.values.ve_per_lap_percent; delete state.sources.ve_per_lap_percent; }
      const context = ctx.context();
      state.context = context ? {track_name: context.track_name || '', car_name: context.car_name || ''} : state.context;
      if (!write(ctx.storage, slug, state) && feedback) feedback.textContent = 'Browser storage unavailable: plan not saved';
      rerun();
    });
    box.addEventListener('click', event => {
      const target = event.target, slug = ctx.slug(); if (!slug || !target?.dataset) return;
      const state = read(ctx.storage, slug);
      if (target.id === 'pr-clear') { clear(ctx.storage, slug); rerun(); return; }
      if (target.dataset.adopt) {
        const f = target.dataset.adopt, value = Number(target.dataset.value);
        if (!Number.isFinite(value)) return;
        state.values[f] = value;
        state.sources[f] = f === 'duration_minutes' || f === 'race_laps' || f === 'tank_capacity_l' ? 'confirmed' : 'observed';
        if (f === 'race_laps') state.mode = 'laps'; else if (f === 'duration_minutes') state.mode = 'time';
      } else if (target.dataset.adoptVe) {
        state.ve_capable = target.dataset.adoptVe === 'true'; state.sources.ve_capable = 'observed';
      } else return;
      const context = ctx.context();
      if (context) state.context = {track_name: context.track_name || '', car_name: context.car_name || ''};
      write(ctx.storage, slug, state);
      rerun();
    });
  }
  return {read, write, clear, query, load, summary, engineer, bind, contextWarning, FIELDS};
})();
