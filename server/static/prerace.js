// Shared pre-race planner UI (#66), injected into Driver Live (compact summary) and Engineer (detailed planner).
// The plan lives only in this browser (localStorage, per driver slug, never per session): it is a manual
// planning aid, not session telemetry, and survives the Qualifying -> Race session change because it is bound
// to nothing but the driver. Every value keeps its own origin and the car/track it was entered for (`stamps`);
// values stamped for another car or track are flagged and must be confirmed again. Unsaved form input lives in
// an in-memory draft until "Calculate". All calculation happens on the server (GET /api/drivers/{slug}/prerace).
const PreRaceUI = (() => {
  const FIELDS = ['duration_minutes', 'race_laps', 'lap_time_s', 'fuel_per_lap_l', 've_per_lap_percent',
                  'tank_capacity_l', 'reserve_laps'];
  const LABELS = {duration_minutes: 'Race duration (min)', race_laps: 'Race laps', lap_time_s: 'Lap time (s)',
                  fuel_per_lap_l: 'Fuel per lap (L)', ve_per_lap_percent: 'VE per lap (%)',
                  tank_capacity_l: 'Tank capacity (L)', reserve_laps: 'Safety reserve (laps)', ve_capable: 'VE capability'};
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
  const empty = () => ({v: 1, mode: 'time', values: {}, ve_capable: null, sources: {}, stamps: {}});
  const stampOf = c => c && typeof c === 'object' ? {track_name: String(c.track_name ?? '').slice(0, 200), car_name: String(c.car_name ?? '').slice(0, 200)} : null;
  const drafts = new Map();   // unsaved form state per driver slug, in memory only

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
      // Older plans carried one plan-level stamp: it applies to every value that existed then.
      const legacy = stampOf(raw.context);
      for (const f of [...FIELDS, 've_capable']) {
        const stamp = stampOf(raw.stamps?.[f]) || (legacy && (f === 've_capable' ? state.ve_capable !== null : f in state.values) ? legacy : null);
        if (stamp) state.stamps[f] = stamp;
      }
      return state;
    } catch { return empty(); }
  }
  function write(storage, slug, state) {
    try { if (!storage) return false; storage.setItem(key(slug), JSON.stringify(state)); return true; } catch { return false; }
  }
  function clear(storage, slug) { drafts.delete(slug); try { storage?.removeItem(key(slug)); return true; } catch { return false; } }

  // Fields whose stamp (car/track) differs from the current context. Unknown on either side is not a difference.
  // The single definition of which fields are part of the plan: the race-length field of the chosen mode and,
  // only for a VE car, the VE consumption. `draft` (optional) is the unsaved form state: the Engineer form uses
  // it, the calculation query and Driver Live pass none and therefore use the saved state only.
  function activeFields(state, draft) {
    const laps = (draft?.mode ?? state.mode) === 'laps';
    const ve = draft && Object.hasOwn(draft, 've') ? draft.ve === 'yes' : state.ve_capable === true;
    return new Set(FIELDS.filter(f => f === (laps ? 'duration_minutes' : 'race_laps') ? false : f === 've_per_lap_percent' ? ve : true)
      .concat('ve_capable'));
  }
  function staleFields(state, context, draft) {
    const current = stampOf(context), stale = [], active = activeFields(state, draft);
    if (!current) return stale;
    for (const f of [...FIELDS, 've_capable']) {
      const s = state.stamps?.[f];
      if (!active.has(f)) continue;                      // hidden: not part of the plan
      if (!s || (f === 've_capable' ? state.ve_capable === null : !(f in state.values))) continue;
      if ((s.track_name && current.track_name && s.track_name !== current.track_name)
          || (s.car_name && current.car_name && s.car_name !== current.car_name)) stale.push(f);
    }
    return stale;
  }

  // Only the length field of the selected mode is sent, so the UI itself never produces contradictory input.
  function query(state) {
    const p = new URLSearchParams(), active = activeFields(state), used = [];
    for (const f of FIELDS) {
      if (!active.has(f) || !Number.isFinite(state.values[f])) continue;
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
    const stale = staleFields(state, context);
    if (stale.length) lines.push(`Not verified for this car/track: ${stale.map(f => LABELS[f]).join(', ')}. Confirm in the Engineer planner.`);
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
    const stale = staleFields(state, resp.context).length > 0;
    const cls = plan.status === 'ready' && !stale ? 'pr-ok' : plan.status === 'ready' || plan.status === 'ready_capacity_unchecked' ? 'pr-warn' : 'pr-bad';
    const lines = quality(plan, state, resp.context);
    // The link sits in the heading row: on a short display (Chromebook) it must stay reachable above the details.
    return `<section class="panel pr-panel" aria-label="Race prep"><div class="pr-title"><h2>Race prep <small class="muted">· estimate</small></h2>${link}</div>
      <div class="pr-grid"><div><div class="muted">Race</div><div class="value">${e(lengthText(plan))}</div></div>
      <div><div class="muted">Expected laps</div><div class="value">${e(lapsText(plan))}</div></div>
      <div><div class="muted">Start fuel</div><div class="value ${cls}">${e(startText(plan))}</div></div>
      <div><div class="muted">Start VE</div><div class="value">${e(veText(plan))}</div></div>
      <div><div class="muted">Reserve</div><div class="value">${e(n(plan.reserve_laps, 1))} lap</div></div></div>
      <p class="${cls}"><strong>${e(STATUS[plan.status] || 'Pre-race plan unavailable')}</strong></p>
      ${lines.length ? `<ul class="pr-notes muted">${lines.map(l => `<li>${e(l)}</li>`).join('')}</ul>` : ''}</section>`;
  }

  function suggestion(field, ev, sessionId) {
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
    return `<span class="muted pr-hint">Observed ${e(n(map[0], map[1]))} (${e(map[2])}${caveat}) <button type="button" data-adopt="${e(field)}" data-value="${e(map[0].toFixed(map[1]))}" data-session="${e(sessionId)}">Use</button></span>`;
  }
  // Engineer: complete planner. `opts`: {sessionId, context, draft}. The form shows the draft over the saved state.
  function engineer(resp, state, opts = {}) {
    const plan = resp.plan, draft = opts.draft || {text: {}, adopted: {}};
    const evidenceSession = resp.context?.session_id ?? null;
    const evidenceOk = !opts.sessionId || (evidenceSession !== null && evidenceSession === opts.sessionId);
    const ev = evidenceOk ? resp.evidence : null;
    const active = activeFields(state, draft), laps = active.has('race_laps');
    const veChoice = draft.ve ?? (state.ve_capable === null ? 'unknown' : state.ve_capable ? 'yes' : 'no');
    const stale = staleFields(state, opts.context, draft), staleSaved = staleFields(state, opts.context);   // form: draft; result: saved plan
    const input = field => {
      const unsaved = Object.hasOwn(draft.text, field), v = unsaved ? draft.text[field] : state.values[field];
      const src = draft.adopted[field]?.source ? `${draft.adopted[field].source}, unsaved` : unsaved ? 'manual, unsaved' : state.sources[field];
      const old = !unsaved && stale.includes(field) ? ' <small class="pr-bad">· other car/track</small>' : '';
      const confirm = old ? ` <button type="button" data-confirm="${field}">Still valid</button>` : '';
      return `<div class="pr-field"><label for="pr-${field}">${e(LABELS[field])}${src ? ` <small class="muted">· ${e(src)}</small>` : ''}${old}</label>
        <input id="pr-${field}" data-field="${field}" inputmode="decimal" autocomplete="off" value="${unsaved ? e(v) : Number.isFinite(v) ? e(v) : ''}">${confirm}${suggestion(field, ev, evidenceSession)}</div>`;
    };
    const veEvidence = typeof ev?.ve_capable === 'boolean' && (ev.ve_capable ? 'yes' : 'no') !== veChoice
      ? `<span class="muted pr-hint">Telemetry says ${ev.ve_capable ? 'VE car' : 'no VE'} <button type="button" data-adopt-ve="${ev.ve_capable}" data-session="${e(evidenceSession)}">Use</button></span>` : '';
    const veStale = !Object.hasOwn(draft, 've') && stale.includes('ve_capable') ? ' <small class="pr-bad">· other car/track</small> <button type="button" data-confirm="ve_capable">Still valid</button>' : '';
    const rules = !resp.evidence ? 'No live session of this driver: manual inputs only'
      : !evidenceOk ? 'Suggestions belong to another session of this driver and are not applicable here'
      : ev.race_rules?.available ? `Race rules confirmed: ${ev.race_rules.race_laps ? `${ev.race_rules.race_laps} laps` : `${n(ev.race_rules.duration_minutes, 1)} min`}`
      : `Race length: not available${ev.session_type === 'Race' ? '' : ' — the upcoming race is not described by the current session; enter it manually'}`;
    const fuel = plan.fuel, energy = plan.energy;
    const lines = quality(plan, state, opts.context);
    const cls = plan.status === 'ready' && !staleSaved.length ? 'pr-ok' : plan.status === 'ready' || plan.status === 'ready_capacity_unchecked' ? 'pr-warn' : 'pr-bad';
    return `<form id="pr-form" class="pr-form" novalidate>
      <p class="muted">${e(rules)}</p>
      <fieldset class="pr-mode"><legend>Race length</legend>
        <label><input type="radio" name="pr-mode" value="time" ${laps ? '' : 'checked'}> Timed</label>
        <label><input type="radio" name="pr-mode" value="laps" ${laps ? 'checked' : ''}> Lap limit</label></fieldset>
      ${input(laps ? 'race_laps' : 'duration_minutes')}${input('lap_time_s')}${input('fuel_per_lap_l')}
      <div class="pr-field"><label for="pr-ve">Virtual Energy car${veStale}</label><select id="pr-ve"><option value="unknown" ${veChoice === 'unknown' ? 'selected' : ''}>Unknown</option>
        <option value="yes" ${veChoice === 'yes' ? 'selected' : ''}>Yes</option><option value="no" ${veChoice === 'no' ? 'selected' : ''}>No</option></select>${veEvidence}</div>
      ${active.has('ve_per_lap_percent') ? input('ve_per_lap_percent') : ''}
      ${input('tank_capacity_l')}${input('reserve_laps')}
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

  const newDraft = () => ({text: {}, adopted: {}});
  // Saved state with the draft applied. Only values the user changed or adopted get a new origin and stamp;
  // untouched values keep theirs. Returns {state} or {error}.
  function applyDraft(saved, draft, context) {
    const state = {...saved, values: {...saved.values}, sources: {...saved.sources}, stamps: {...saved.stamps}};
    const stamp = stampOf(context);
    const touch = (f, source, at) => { state.sources[f] = source; const s = stampOf(at) || stamp; if (s) state.stamps[f] = s; else delete state.stamps[f]; };
    if (draft.mode) state.mode = draft.mode;
    // Fields hidden by the chosen race-length mode or by "no VE" are neither validated nor saved; their draft
    // text is kept (see `inactive`) so switching back restores it.
    const active = activeFields(saved, draft), inactive = new Set(FIELDS.filter(f => !active.has(f)));
    for (const [f, raw] of Object.entries(draft.text)) {
      if (inactive.has(f)) continue;
      const text = String(raw).trim().replace(',', '.');
      if (text === '') { delete state.values[f]; delete state.sources[f]; delete state.stamps[f]; continue; }
      const value = Number(text);
      if (!Number.isFinite(value)) return {error: `${LABELS[f]}: not a number`};
      const adopted = draft.adopted[f];
      if (adopted || state.values[f] !== value) { state.values[f] = value; touch(f, adopted?.source || 'manual', adopted?.context); }
    }
    if (Object.hasOwn(draft, 've')) {
      const capable = draft.ve === 'yes' ? true : draft.ve === 'no' ? false : null;
      if (capable !== state.ve_capable || draft.adopted.ve_capable) { state.ve_capable = capable; touch('ve_capable', draft.adopted.ve_capable?.source || 'manual', draft.adopted.ve_capable?.context); }
    }
    return {state, inactive};
  }

  // Wires one container. ctx: storage, slug(), context() (current car/track), sessionId() (viewed session),
  // refresh(force) (reload from the server), rerender() (redraw from cached data, no request).
  function bind(box, ctx) {
    const draftOf = slug => { if (!drafts.has(slug)) drafts.set(slug, newDraft()); return drafts.get(slug); };
    box.addEventListener('input', event => {
      const slug = ctx.slug(), f = event.target?.dataset?.field; if (!slug || !f) return;
      const d = draftOf(slug); d.text[f] = event.target.value; delete d.adopted[f];
    });
    box.addEventListener('change', event => {
      const slug = ctx.slug(), t = event.target; if (!slug || !t) return;
      const d = draftOf(slug);
      if (t.id === 'pr-ve') { d.ve = t.value; delete d.adopted.ve_capable; ctx.rerender(); }
      else if (t.name === 'pr-mode') { d.mode = t.value === 'laps' ? 'laps' : 'time'; ctx.rerender(); }
    });
    box.addEventListener('submit', event => {
      event.preventDefault();
      const slug = ctx.slug(); if (!slug) return;
      const feedback = box.querySelector('#pr-feedback'), d = draftOf(slug);
      const done = applyDraft(read(ctx.storage, slug), d, ctx.context());
      if (done.error) { if (feedback) feedback.textContent = done.error; return; }
      if (!write(ctx.storage, slug, done.state)) { if (feedback) feedback.textContent = 'Browser storage unavailable: plan not saved'; return; }
      const next = newDraft();
      for (const f of done.inactive) if (Object.hasOwn(d.text, f)) { next.text[f] = d.text[f]; if (d.adopted[f]) next.adopted[f] = d.adopted[f]; }
      drafts.set(slug, next);
      ctx.refresh(true);
    });
    box.addEventListener('click', event => {
      const t = event.target, slug = ctx.slug(); if (!slug || !t?.dataset) return;
      if (t.id === 'pr-clear') { clear(ctx.storage, slug); ctx.refresh(true); return; }
      if (t.dataset.confirm) {            // explicit re-confirmation: re-stamp only this value for the current context
        const state = read(ctx.storage, slug), s = stampOf(ctx.context());
        if (s && (t.dataset.confirm === 've_capable' || t.dataset.confirm in state.values)) { state.stamps[t.dataset.confirm] = s; write(ctx.storage, slug, state); }
        ctx.refresh(true); return;
      }
      const adoptsValue = Boolean(t.dataset.adopt), adoptsVe = t.dataset.adoptVe !== undefined;
      if (!adoptsValue && !adoptsVe) return;
      // Suggestions apply only to the session being viewed; a button from another session's answer is ignored.
      if (!ctx.sessionId() || t.dataset.session !== ctx.sessionId()) { ctx.refresh(true); return; }
      const d = draftOf(slug), context = stampOf(ctx.context());
      if (adoptsValue) {
        const f = t.dataset.adopt, value = Number(t.dataset.value);
        if (!FIELDS.includes(f) || !Number.isFinite(value)) return;
        // Only this field changes; everything else already typed stays in the draft untouched.
        d.text[f] = t.dataset.value;
        d.adopted[f] = {source: ['duration_minutes', 'race_laps', 'tank_capacity_l'].includes(f) ? 'confirmed' : 'observed', context};
        if (f === 'race_laps') d.mode = 'laps'; else if (f === 'duration_minutes') d.mode = 'time';
      } else {
        d.ve = t.dataset.adoptVe === 'true' ? 'yes' : 'no'; d.adopted.ve_capable = {source: 'observed', context};
      }
      ctx.rerender();
    });
  }
  const working = slug => drafts.get(slug) || newDraft();
  return {read, write, clear, query, load, summary, engineer, bind, staleFields, activeFields, applyDraft, working, FIELDS};
})();
