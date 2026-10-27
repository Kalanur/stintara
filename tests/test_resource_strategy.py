"""Wholly synthetic resource math and provider mapping regressions."""
import math

import pytest

from agent.pitwall import normalize
from server.app.lmu_resources import lmu_resources, pit_settings
from server.app.protocol import Scoring, Telemetry
from server.app.resource_strategy import (
    PitServiceMode,
    energy_estimate,
    finish_projection,
    plan_resources,
    resource,
)


def resources(fuel=42, energy=.63, fuel_use=2.8, energy_use=.102, capacity=100):
    return [resource('fuel', fuel, capacity, fuel_use, True, 5, 'L', pit_service_mode='TARGET_LEVEL'),
            resource('energy', energy, 1, energy_use, True, 5, 'fraction', energy_kind='VIRTUAL_ENERGY')]


def plan(rs=None, end=38, completed=28, stop=32, **kw):
    return plan_resources(rs or resources(), completed, dict(kind='exact', finish_lap=end), manual_stop=stop, **kw)


@pytest.mark.parametrize('mode', list(PitServiceMode))
def test_core_optional_energy_and_service_mode_independent(mode):
    r = resources()[0]
    r['pit_service_mode'] = mode
    p = plan([r], end=48)
    assert p['effective_range_laps'] == pytest.approx(15)
    assert p['limiting_resource'] == 'fuel'
    assert p['next_stint_requirements'][0]['amount'] == pytest.approx(47.6)
    assert 'provider_pit_settings' not in p


def test_energy_window_threshold_median_refill_and_invalid_gap():
    assert energy_estimate([1, .9, .8])['consumption_per_lap'] is None
    e = energy_estimate([1, .9, .8, .7])
    assert e['consumption_per_lap'] == pytest.approx(.1)
    assert e['sample_count'] == 3
    e = energy_estimate([1, .99, .98, .88, .68, .38, .18, .08, .9, .8])
    assert e['sample_count'] == 5
    assert e['consumption_per_lap'] == pytest.approx(.2)
    assert energy_estimate([1, None, .7, True, .5])['sample_count'] == 0


@pytest.mark.parametrize('bad', [True, '0.5', math.nan, math.inf, -1, 1.01])
def test_energy_adapter_sanitizes_invalid_values_and_transport_rejects(bad):
    _, p = normalize(dict(type='ScoringUpdate', vehicles=[dict(id=1, virtual_energy=bad)], session_type='Race'))
    assert p['vehicles'][0]['energy_current_fraction'] is None
    Scoring.model_validate(p)
    with pytest.raises(ValueError):
        Telemetry.model_validate(dict(energy_history=[bad]))


@pytest.mark.parametrize('capability', [True, False, None])
def test_adapter_minimum_energy_contract(capability):
    _, p = normalize(dict(type='TelemetryUpdate', ve_available=capability, ve_history=[1, .9, .8, .7, .63]))
    tel = Telemetry.model_validate(p)
    assert tel.energy_available is capability
    assert tel.energy_history == [1, .9, .8, .7]  # Evolving current endpoint excluded.
    assert 've_history' not in p and 've_available' not in p
    _, p = normalize(dict(type='ScoringUpdate', vehicles=[dict(id=1, virtual_energy=0)], session_type='Race'))
    assert p['vehicles'][0]['energy_current_fraction'] == 0
    assert normalize(dict(type='ElectronicsUpdate', virtual_energy=.6)) is None


@pytest.mark.parametrize('fuel,energy,limit', [(42,.63,'energy'), (8,.8,'fuel'), (28,1,'fuel')])
def test_limiting_resource(fuel, energy, limit):
    p = plan(resources(fuel, energy, energy_use=.1))
    assert p['limiting_resource'] == limit
    assert p['effective_range_laps'] == min(fuel/2.8, energy/.1)
    assert p['balanced'] is (fuel == 28)


def test_safe_manual_stop_and_lmu_ratio_units():
    p = plan(resources(fuel=86, energy=.8, fuel_use=8.6, energy_use=.1), end=36)
    assert p['planned_stop_safe'] and p['reserve_laps'] == 1
    assert p['at_pit'][0]['amount'] == pytest.approx(51.6)
    settings = pit_settings(p)
    assert settings['fuel_service_mode'] == 'TARGET_LEVEL'
    assert settings['measured_fuel_ratio_l_per_percent_ve'] == pytest.approx(.86)
    assert settings['fuel_ratio_l_per_percent_ve'] == .86
    assert settings['ve_target_percent'] == 50
    assert settings['resulting_fuel_target_l'] == 43
    assert settings['expected_fuel_added_l'] == 0


def test_round_up_no_hidden_factor_and_add_informational():
    p = plan(resources(fuel=42, fuel_use=8.61, energy_use=.101), end=36)
    s = pit_settings(p)
    assert s['fuel_ratio_l_per_percent_ve'] == .86
    assert s['ve_target_percent'] == 51
    assert s['resulting_fuel_target_l'] == pytest.approx(43.86)
    assert s['expected_fuel_added_l'] == pytest.approx(43.86 - (42-4*8.61))


@pytest.mark.parametrize('rs,cause', [(resources(fuel=8), 'fuel'), (resources(energy=.2), 'energy')])
def test_unsafe_preserves_negative_projection(rs, cause):
    p = plan(rs)
    assert p['status'] == 'unsafe_plan' and not p['planned_stop_safe']
    assert any(s['resource_kind'] == cause and s['laps'] > 0 for s in p['shortfall'])
    assert next(r['amount'] for r in p['at_pit'] if r['resource_kind'] == cause) < 0


def test_capacities_and_fuel_only_target():
    p = plan(end=60)
    s = pit_settings(p)
    assert p['additional_stop_required'] and s['ve_target_percent'] == 100
    assert s['additional_stop_required']
    p = plan(resources(capacity=10))
    assert pit_settings(p)['capacity_feasible'] is False
    p = plan([resources(capacity=10)[0]])
    s = pit_settings(p)
    assert 've_target_percent' not in s and s['fuel_target_l'] == pytest.approx(19.6)
    assert s['additional_stop_required']


def test_auto_stop_finish_no_stop_and_box():
    p = plan(stop=None)
    assert p['latest_safe_stop_lap'] == 33 and p['planned_stop_lap'] == 33
    assert plan(end=31, stop=None)['status'] == 'no_stop_required'
    p = plan(resources(energy=.1), stop=None)
    assert p['status'] == 'box_this_lap' and p['planned_stop_lap'] == 29
    p = plan(resources(energy=.1), end=29, stop=None)
    assert p['status'] == 'finish_shortfall' and p['planned_stop_lap'] is None


@pytest.mark.parametrize('stop', [0, -1, 28, 38, 39, 30.5, True])
def test_invalid_manual_stop(stop):
    assert plan(stop=stop)['status'] == 'invalid_plan'


@pytest.mark.parametrize('status', [1, 2, 3, 99])
def test_finished_and_unknown_not_active(status):
    p = plan(active=status == 0)
    assert p['status'] == 'inactive' and pit_settings(p) is None


def test_stale_unknown_capability_and_unavailable_finish():
    rs = resources()
    rs[0] = resource('fuel', 42, 100, 2.8, False, 5, 'L')
    assert plan(rs)['status'] == 'collecting_or_stale'
    assert plan(capability_known=False)['status'] == 'capability_unknown'
    assert plan(end=None)['status'] == 'finish_unknown'


def test_finish_laps_timed_upper_bound_and_pace():
    assert finish_projection(28, dict(configured_laps=48), [])['finish_lap'] == 48
    t = dict(configured_minutes=80, elapsed_seconds=2880, configured_laps=999)
    f = finish_projection(28, t, [100, 102, 101, None, -1, math.nan])
    assert f == dict(kind='timed_estimate', finish_lap=49, pace_seconds=101)
    assert finish_projection(28, t, [100, 101])['kind'] == 'unavailable'
    assert finish_projection(28, t, [100]*3, race=False)['kind'] == 'unavailable'
    assert finish_projection(28, dict(configured_minutes=1, elapsed_seconds=70), [100]*3)['finish_lap'] == 29


def test_lmu_capability_is_not_inferred_from_zero():
    fuel = dict(current=dict(fuel_l=42, capacity_l=100, fresh=True),
                estimate=dict(consumption_l_per_lap=2.8, sample_count=5))
    rs, known = lmu_resources(fuel, dict(energy_current_fraction=0))
    assert not known and len(rs) == 2
    rs, known = lmu_resources(fuel, dict(energy_available=False))
    assert known and len(rs) == 1 and rs[0]['pit_service_mode'] == 'TARGET_LEVEL'
