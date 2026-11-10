"""Reproducible micro-benchmark of lap checkpoint persistence (`main.store_lap_snapshots`).

Developer tooling, synthetic data only. Needs an isolated PostgreSQL database in TEST_DATABASE_URL; everything runs
in a throw-away schema that is dropped afterwards.

    TEST_DATABASE_URL=postgresql://user@127.0.0.1:5432/scratch python -m tools.bench_lap_sensors

Scenarios (10 cars per AllDriversUpdate, 40 warm-up and 300 measured iterations each, median / p95 / max in ms):
  first_insert     every car inserts a new lap row
  repeat_observer  all rows exist; a spectating agent repeats the frozen snapshot (all conflicts)
  repeat_owner     all rows exist and are complete; the owner repeats the frozen snapshot (all conflicts)
  complete_owner   the owner completes rows an observer wrote first (one locked read and update for its car)
"""
import asyncio
import os
import statistics
import time

import asyncpg

from server.app import main
from server.app.uuid7 import uuid7
from tests.test_convergence import roster, scoring

WHEELS = [dict(remaining=.9, temp_inner=80.0, temp_mid=85.0, temp_outer=90.0, pressure=170.0)] * 4
PLACEHOLDER = [dict(remaining=1.0)] * 4


def payload(laps, wheels_for_car0, clock):
    drivers = [dict(id=car, total_laps=laps, last_lap_time=90.5, position=car + 1, has_telemetry=True,
                    wheels=wheels_for_car0 if car == 0 else PLACEHOLDER, fuel_remaining=40.0 if car == 0 else 50.0,
                    fuel_capacity=100.0, damage={"dent_severity": [0]}) for car in range(10)]
    return dict(session_type="Race", session_time=clock, drivers=drivers)


async def agent(conn, name):
    driver, ident = uuid7(), uuid7()
    await conn.execute("INSERT INTO drivers(id,slug,display_name) VALUES($1,$2,$2)", driver, name)
    await conn.execute("INSERT INTO agents(id,driver_id,name,token_sha256) VALUES($1,$2,$3,$4)",
                       ident, driver, name, main.token_hash("bench-" + name))
    return {"id": ident, "driver_id": driver, "slug": name, "display_name": name}


async def measure(label, run, warmup=40, iterations=300):
    for i in range(warmup):
        await run(i)
    samples = []
    for i in range(iterations):
        started = time.perf_counter()
        await run(warmup + i)
        samples.append((time.perf_counter() - started) * 1000)
    samples.sort()
    print(f"{label:16} median {statistics.median(samples):6.2f}  p95 {samples[int(len(samples) * .95)]:6.2f}  "
          f"max {samples[-1]:6.2f} ms")


async def watch(owner, lap, clock):
    for laps in (lap - 1, lap):                         # the owner watches its car cross into `lap`
        frame = scoring(roster(), kind="Race", player=0, clock=clock)
        for vehicle in frame["vehicles"]:
            vehicle["total_laps"] = laps if vehicle["id"] == 0 else 0
        await main.handle_message(owner, "lmu", "scoring", frame)


async def run():
    url = os.environ["TEST_DATABASE_URL"]
    schema = "bench_" + uuid7().hex
    admin = await asyncpg.connect(url)
    await admin.execute(f'CREATE SCHEMA "{schema}"')
    main.pool = await asyncpg.create_pool(url, min_size=1, max_size=3, server_settings={"search_path": schema})
    main.runtime.clear()
    try:
        async with main.pool.acquire() as conn:
            await conn.execute(main.SCHEMA_FILE.read_text())
            owner, observer = await agent(conn, "owner"), await agent(conn, "observer")
        await main.handle_message(owner, "lmu", "session_info", {"track_name": "Bench Circuit"})
        await main.handle_message(owner, "lmu", "scoring", scoring(roster(), kind="Race", player=0, clock=100))
        await main.handle_message(observer, "lmu", "session_info", {"track_name": "Bench Circuit"})
        await main.handle_message(observer, "lmu", "scoring", scoring(roster(), kind="Race", player=1, clock=100))
        sid = main.runtime[str(owner["id"])]["session_id"]
        total = 40 + 300
        clock = 100

        async def first_insert(i):
            await watch(owner, i + 1, clock)
            started = time.perf_counter()
            await main.store_lap_snapshots(owner, sid, payload(i + 1, WHEELS, clock))
            return time.perf_counter() - started

        # first_insert times only the store call (the scoring frames of `watch` are not part of the measurement).
        samples = []
        for i in range(total):
            samples.append(await first_insert(i) * 1000)
        samples = sorted(samples[40:])
        print(f"{'first_insert':16} median {statistics.median(samples):6.2f}  p95 {samples[int(len(samples) * .95)]:6.2f}  "
              f"max {samples[-1]:6.2f} ms")
        last = total
        await measure("repeat_owner", lambda i: main.store_lap_snapshots(owner, sid, payload(last, WHEELS, clock)))
        await measure("repeat_observer", lambda i: main.store_lap_snapshots(observer, sid, payload(last, PLACEHOLDER, clock)))
        async with main.pool.acquire() as conn:
            await conn.execute("""UPDATE laps SET tyre_snapshot=NULL,tyre_remaining_fl=NULL,tyre_remaining_fr=NULL,
                tyre_remaining_rl=NULL,tyre_remaining_rr=NULL,fuel_remaining_l=NULL,fuel_capacity_l=NULL""")
        state = main.runtime[str(owner["id"])]
        state["lap_crossings"] = {0: [last]}

        async def complete(i):
            async with main.pool.acquire() as conn:   # re-open the row so every iteration performs a real completion
                await conn.execute("""UPDATE laps SET tyre_snapshot=NULL,fuel_remaining_l=NULL,fuel_capacity_l=NULL
                    WHERE lap_number=$1""", last)
            await main.store_lap_snapshots(owner, sid, payload(last, WHEELS, clock))
        await measure("complete_owner", complete)
    finally:
        await main.pool.close()
        await admin.execute(f'DROP SCHEMA "{schema}" CASCADE')
        await admin.close()


if __name__ == "__main__":
    asyncio.run(run())
