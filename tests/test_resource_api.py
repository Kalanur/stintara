"""Synthetic database evidence for energy transport and read-only planning."""
import json

import httpx
import pytest

from agent.pitwall import normalize
from server.app import main
from server.app.protocol import Telemetry
from server.app.uuid7 import uuid7
from tests.test_pit_finish import setup_field


async def prepared(db, available=True):
    agent, sid, scoring = await setup_field(db)
    scoring['vehicles'][0]['energy_current_fraction'] = .63
    await main.handle_message(agent, 'lmu', 'scoring', scoring)
    await main.handle_message(agent, 'lmu', 'session_info', dict(track_name='Synthetic Circuit', session_laps=38))
    kind, payload = normalize(dict(type='TelemetryUpdate', current_et=100, fuel=42, fuel_capacity=100,
                                   ve_available=available, ve_history=[1, .898, .796, .694, .592, .49, .4]))
    await main.handle_message(agent, 'lmu', kind, Telemetry.model_validate(payload).model_dump())
    async with db.acquire() as conn:
        pid = await conn.fetchval('SELECT id FROM session_participants WHERE session_id=$1', sid)
        for lap in range(20, 26):
            await conn.execute('''INSERT INTO laps(id,session_participant_id,lap_number,lap_time,fuel_remaining_l)
                VALUES($1,$2,$3,100,$4)''', uuid7(), pid, lap, 100-lap*2.8)
    return agent, sid, pid


async def test_resource_ingestion_api_readonly_membership_and_persisted_energy(db):
    _, sid, pid = await prepared(db)
    async with db.acquire() as conn:
        row = await conn.fetchrow('SELECT * FROM strategy_samples WHERE session_participant_id=$1', pid)
        assert row['energy_current_fraction'] == .63 and row['energy_available'] is True
        assert row['energy_kind'] == 'VIRTUAL_ENERGY'
        assert json.loads(row['energy_history']) == [1, .898, .796, .694, .592, .49]
        before = [await conn.fetch('SELECT * FROM '+table+' ORDER BY id')
                  for table in ['strategy_samples', 'session_participants', 'laps']]
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url='http://localhost') as client:
        path = f'/api/sessions/{sid}/participants/{pid}/strategy'
        response = await client.get(path, params={'planned_stop_lap': 32})
        assert response.status_code == 200
        p = response.json()
        assert p['status'] == 'stop_required' and p['planned_stop_safe']
        assert p['resources'][0]['consumption_per_lap'] == pytest.approx(2.8)
        assert p['resources'][1]['consumption_per_lap'] == pytest.approx(.102)
        assert p['limiting_resource'] == 'energy' and p['reserve_laps'] == 1
        assert p['provider_pit_settings']['ve_target_percent'] == 72
        assert p['provider_pit_settings']['fuel_ratio_l_per_percent_ve'] == .28
        for value in ['0', '-1', '31.5', 'NaN']:
            assert (await client.get(path, params={'planned_stop_lap':value})).status_code == 422
        assert (await client.get(path, params={'planned_stop_lap':28})).json()['status'] == 'invalid_plan'
        assert (await client.get(f'/api/sessions/{uuid7()}/participants/{pid}/strategy')).status_code == 404
        assert (await client.get(f'/api/sessions/{sid}/participants/{uuid7()}/strategy')).status_code == 404
        assert (await client.post(path)).status_code == 405
    async with db.acquire() as conn:
        after = [await conn.fetch('SELECT * FROM '+table+' ORDER BY id')
                 for table in ['strategy_samples', 'session_participants', 'laps']]
        assert before == after


@pytest.mark.parametrize('available', [False, None])
async def test_resource_capability_fuel_only_unknown(db, available):
    _, sid, pid = await prepared(db, available)
    p = await main.api_participant_strategy(sid, pid, 32)
    if available is None:
        assert p['status'] == 'capability_unknown' and p['provider_pit_settings'] is None
    else:
        assert len(p['resources']) == 1
        assert p['provider_pit_settings']['fuel_service_mode'] == 'TARGET_LEVEL'
        assert 've_target_percent' not in p['provider_pit_settings']


@pytest.mark.parametrize('finish', [1, 2, 3, 99])
async def test_resource_finish_no_recommendation(db, finish):
    _, sid, pid = await prepared(db)
    async with db.acquire() as conn:
        await conn.execute('UPDATE session_participants SET finish_status=$2 WHERE id=$1', pid, finish)
    p = await main.api_participant_strategy(sid, pid, 32)
    assert p['status'] == 'inactive' and p['provider_pit_settings'] is None
    assert p['resources'][0]['current'] == 42


async def test_resource_stale_session_clock_and_samples(db):
    agent, sid, pid = await prepared(db)
    async with db.acquire() as conn:
        await conn.execute("UPDATE strategy_samples SET recorded_at=recorded_at-interval '30 seconds'")
    p = await main.api_participant_strategy(sid, pid, 32)
    assert p['provider_pit_settings'] is None
    assert p['resources'][0]['range_laps'] is None
    main.runtime[str(agent['id'])]['last_scoring_at'] -= 30
    p = await main.api_participant_strategy(sid, pid, 32)
    assert p['status'] == 'inactive' and p['planned_stop_lap'] is None


async def test_resource_timed_projection_api(db):
    _, sid, pid = await prepared(db)
    async with db.acquire() as conn:
        await conn.execute('UPDATE sessions SET session_minutes=20 WHERE id=$1', sid)
    p = await main.api_participant_strategy(sid, pid, 32)
    assert p['finish_projection'] == dict(kind='timed_estimate', finish_lap=40, pace_seconds=100)
