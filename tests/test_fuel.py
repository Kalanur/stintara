from datetime import UTC, datetime, timedelta

import pytest

from server.app.fuel import checkpoint_transition, consumption_estimate, fuel_report

NOW = datetime(2026, 1, 1, tzinfo=UTC)


def checkpoints(fuels):
    return [dict(lap_number=i + 1, fuel_remaining_l=fuel, pitstops=0, in_pits=False)
            for i, fuel in enumerate(fuels)]


def test_clean_sorted_checkpoints_and_threshold():
    rows = checkpoints([50, 47.2, 44.4, 41.6])
    for count in range(4):
        report = consumption_estimate(rows[:count])
        assert report['estimate']['consumption_l_per_lap'] is None
        assert report['estimate']['status'] == 'collecting'
        assert report['estimate']['sample_count'] == 0
        assert report['estimate']['available_sample_count'] == max(0, count - 1)
        assert not any(s['used_for_estimate'] for s in report['samples'])
    report = consumption_estimate(list(reversed(rows)))
    assert report['estimate']['consumption_l_per_lap'] == pytest.approx(2.8)
    assert report['estimate']['status'] == 'ready'
    assert all(s['included'] and s['used_for_estimate'] for s in report['samples'])


def test_last_five_median_not_mean_and_no_outlier_filter():
    rows = checkpoints([200, 190, 180, 179, 177, 174, 170, 70])
    report = consumption_estimate(rows)
    assert report['estimate']['sample_count'] == 5
    assert report['estimate']['valid_sample_count_total'] == 7
    assert report['estimate']['consumption_l_per_lap'] == 3
    assert all(s['included'] for s in report['samples'])
    assert [s['consumption_l'] for s in report['samples'] if s['used_for_estimate']] == [1, 2, 3, 4, 100]


@pytest.mark.parametrize('bad', [None, '40', True, -1, float('nan'), float('inf'), -float('inf')])
@pytest.mark.parametrize('index', [0, 1])
def test_invalid_fuel_in_either_checkpoint(bad, index):
    rows = checkpoints([50, 40])
    rows[index]['fuel_remaining_l'] = bad
    sample = checkpoint_transition(*rows)
    assert not sample['included'] and sample['consumption_l'] is None
    assert sample['excluded_reason'] == 'missing_or_invalid_fuel'


@pytest.mark.parametrize('before,after', [(20, 50), (40, 40)])
def test_refuel_and_zero(before, after):
    assert checkpoint_transition(*checkpoints([before, after]))['excluded_reason'] == 'fuel_not_decreasing'


@pytest.mark.parametrize('index', [0, 1])
def test_pit_context(index):
    rows = checkpoints([50, 40])
    rows[index]['in_pits'] = True
    assert checkpoint_transition(*rows)['excluded_reason'] == 'pit_context'


def test_reason_priority_and_lap_gaps():
    rows = checkpoints([20, 50])
    rows[1].update(lap_number=3, pitstops=1, in_pits=True)
    assert checkpoint_transition(*rows)['excluded_reason'] == 'non_consecutive_laps'
    rows[1].update(lap_number=2, fuel_remaining_l=None)
    assert checkpoint_transition(*rows)['excluded_reason'] == 'missing_or_invalid_fuel'
    rows[1]['fuel_remaining_l'] = 50
    assert checkpoint_transition(*rows)['excluded_reason'] == 'pitstop_transition'
    rows[0]['pitstops'] = 1
    assert checkpoint_transition(*rows)['excluded_reason'] == 'pit_context'


def test_history_survives_pit_and_long_invalid_run():
    rows = checkpoints([50, 47, 44, 41, 80, 77, 74])
    for row in rows[4:]:
        row['pitstops'] = 1
    report = consumption_estimate(rows)
    assert report['estimate']['sample_count'] == 5
    assert report['estimate']['consumption_l_per_lap'] == 3
    assert report['samples'][3]['excluded_reason'] == 'pitstop_transition'
    rows += [dict(lap_number=i, fuel_remaining_l=None, pitstops=1, in_pits=False) for i in range(8, 28)]
    report = consumption_estimate(rows)
    assert len(report['samples']) == 12
    assert report['samples'][0]['to_lap'] == 16
    assert report['estimate']['sample_count'] == 5
    assert not any(s['used_for_estimate'] for s in report['samples'])


@pytest.mark.parametrize('age,fresh', [(0, True), (15, True), (15.001, False), (-1, False)])
def test_current_freshness_and_range(age, fresh):
    report = fuel_report(checkpoints([50, 47, 44, 41]),
                         dict(fuel_l=42, fuel_capacity_l=100, recorded_at=NOW-timedelta(seconds=age)), NOW)
    assert report['current']['fresh'] is fresh
    assert report['estimate']['estimated_range_laps'] == (14 if fresh else None)
    assert report['estimate']['consumption_l_per_lap'] == 3


@pytest.mark.parametrize('fuel,expected', [(0, 0), (None, None), (-1, None), (float('nan'), None), (float('inf'), None)])
def test_current_values(fuel, expected):
    report = fuel_report(checkpoints([50, 47, 44, 41]), dict(fuel_l=fuel, recorded_at=NOW), NOW)
    assert report['estimate']['estimated_range_laps'] == expected


def test_no_strategy_no_lap_fallback_or_provider_fallback():
    rows = checkpoints([50, 47, 44, 41])
    report = fuel_report(rows, None, NOW)
    assert report['current'] == dict(fuel_l=None, capacity_l=None, recorded_at=None, fresh=False)
    assert report['estimate']['estimated_range_laps'] is None
    strategy = dict(fuel_l=42, recorded_at=NOW, fuel_avg_consumption_lap=999, fuel_laps_remaining=888)
    other = fuel_report(rows, strategy, NOW)
    assert other['estimate']['consumption_l_per_lap'] == 3
    assert other['provider']['avg_consumption_l_per_lap'] == 999
    collecting = fuel_report(rows[:2], strategy, NOW)
    assert collecting['estimate']['consumption_l_per_lap'] is None
    assert collecting['estimate']['estimated_range_laps'] is None


def test_even_median_and_nonfinite_output_guard():
    assert consumption_estimate(checkpoints([100, 99, 97, 94, 84]))['estimate']['consumption_l_per_lap'] == 2.5
    rows = []
    for i in range(4):
        rows.extend([dict(lap_number=i*2+1, fuel_remaining_l=1e308, pitstops=i, in_pits=False),
                     dict(lap_number=i*2+2, fuel_remaining_l=0, pitstops=i, in_pits=False)])
    assert consumption_estimate(rows)['estimate']['consumption_l_per_lap'] == 1e308
    assert fuel_report(checkpoints([4e-308, 3e-308, 2e-308, 1e-308]),
                       dict(fuel_l=1e308, recorded_at=NOW), NOW)['estimate']['estimated_range_laps'] is None
