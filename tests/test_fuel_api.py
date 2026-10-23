from datetime import timedelta

import httpx

from server.app import main
from server.app.uuid7 import uuid7
from tests.conftest import scoring_session


async def test_fuel_http_uuid_validation():
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url='http://localhost') as client:
        assert (await client.get(f'/api/sessions/not-a-uuid/participants/{uuid7()}/fuel')).status_code == 422
        assert (await client.get(f'/api/sessions/{uuid7()}/participants/not-a-uuid/fuel')).status_code == 422


async def test_fuel_api_membership_checkpoints_latest_strategy_and_readonly(db):
    _, session, _ = await scoring_session(db, 'synthetic')
    _, other_session, _ = await scoring_session(db, 'other-synthetic')
    async with db.acquire() as conn:
        participants = await conn.fetch('SELECT id,local_vehicle_id FROM session_participants WHERE session_id=$1', session)
        own = next(p['id'] for p in participants if p['local_vehicle_id'] == 7)
        unresolved = next(p['id'] for p in participants if p['local_vehicle_id'] == 8)
        for participant in [own, unresolved]:
            for lap in [4, 1, 3, 2]:
                await conn.execute('''INSERT INTO laps(id,session_participant_id,lap_number,fuel_remaining_l,pitstops,in_pits)
                    VALUES($1,$2,$3,$4,0,false)''', uuid7(), participant, lap, 53 - 3 * lap)
        now = await conn.fetchval('SELECT now()')
        for age, fuel, provider in [(0, 24, 9), (60, 99, 88)]:
            await conn.execute('''INSERT INTO strategy_samples(id,session_participant_id,recorded_at,fuel_l,fuel_capacity_l,
                fuel_avg_consumption_lap,fuel_laps_remaining,fuel_stint_number,fuel_stint_laps,fuel_stint_consumption_l)
                VALUES($1,$2,$3,$4,100,$5,7,2,6,18)''', uuid7(), own, now-timedelta(seconds=age), fuel, provider)
        before = [await conn.fetch('SELECT * FROM '+table+' ORDER BY id') for table in ['laps', 'strategy_samples', 'session_participants']]
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url='http://localhost') as client:
        path = f'/api/sessions/{session}/participants/{own}/fuel'
        response = await client.get(path)
        assert response.status_code == 200
        data = response.json()
        assert data['session_id'] == str(session) and data['participant_id'] == str(own)
        assert data['estimate']['consumption_l_per_lap'] == 3
        assert data['estimate']['estimated_range_laps'] == 8
        assert data['current']['fuel_l'] == 24 and data['current']['fresh']
        assert data['current']['capacity_l'] == 100
        assert data['provider'] == dict(avg_consumption_l_per_lap=9, laps_remaining=7,
                                       stint_number=2, stint_laps=6, stint_consumption_l=18)
        assert [s['to_lap'] for s in data['samples']] == [2, 3, 4]
        data = (await client.get(f'/api/sessions/{session}/participants/{unresolved}/fuel')).json()
        assert data['estimate']['consumption_l_per_lap'] == 3
        assert data['estimate']['estimated_range_laps'] is None
        assert data['current'] == dict(fuel_l=None, capacity_l=None, recorded_at=None, fresh=False)
        assert all(value is None for value in data['provider'].values())
        for sid, pid in [(other_session, own), (uuid7(), own), (session, uuid7())]:
            assert (await client.get(f'/api/sessions/{sid}/participants/{pid}/fuel')).status_code == 404
    async with db.acquire() as conn:
        after = [await conn.fetch('SELECT * FROM '+table+' ORDER BY id') for table in ['laps', 'strategy_samples', 'session_participants']]
        assert before == after
        assert await conn.fetchval('SELECT driver_id FROM session_participants WHERE id=$1', unresolved) is None
        await conn.execute("UPDATE strategy_samples SET recorded_at=recorded_at-interval '30 seconds'")
    data = await main.api_participant_fuel(session, own)
    assert data['current']['fresh'] is False and data['current']['fuel_l'] == 24
    assert data['estimate']['estimated_range_laps'] is None
    assert data['estimate']['consumption_l_per_lap'] == 3

    async with db.acquire() as conn:
        await conn.execute("INSERT INTO strategy_samples(id,session_participant_id,fuel_l) VALUES($1,$2,NULL)", uuid7(), own)
    missing = await main.api_participant_fuel(session, own)
    assert missing["current"]["fuel_l"] is None
    assert missing["estimate"]["estimated_range_laps"] is None
    assert all(value is None for value in missing["provider"].values())
