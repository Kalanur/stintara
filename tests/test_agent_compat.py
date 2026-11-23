"""Agent identification, declared capabilities and the received-epoch audit (synthetic data only)."""
import pytest

from agent import compat, version
from server.app import agent_compat as ac

SHA = "0123456789abcdef0123456789abcdef01234567"
GOOD = {"version": "0.3.0", "channel": "release", "revision": SHA}


def test_legacy_hello_is_unknown_and_gets_no_answer():
    info = ac.parse(None, None)
    assert info is ac.LEGACY and not info.declared and not info.invalid
    assert ac.assess(info) is None


def test_declared_epoch_support_is_compatible_but_never_verified():
    info = ac.parse(GOOD, ["source_epoch"])
    assert info.declared and not info.invalid and info.capabilities == {"source_epoch"}
    assert ac.assess(info) == {"status": "compatible", "limitations": [], "required": ["source_epoch"],
                               "verified": False}


def test_identified_agent_without_epoch_capability_is_limited():
    for capabilities in (None, [], ["something_else"]):
        answer = ac.assess(ac.parse(GOOD, capabilities))
        assert answer["status"] == "limited" and answer["limitations"] == ["source_epoch_not_declared"]
        assert answer["verified"] is False


@pytest.mark.parametrize("agent,capabilities", [
    ("0.3.0", ["source_epoch"]), ([], ["source_epoch"]), ({"version": "x.y"}, ["source_epoch"]),
    ({"version": 3}, ["source_epoch"]), (None, ["source_epoch"]), ({}, None),
    ({"version": "0.3.0" * 100}, ["source_epoch"]),
])
def test_malformed_identification_counts_as_undeclared_and_limited(agent, capabilities):
    info = ac.parse(agent, capabilities)
    assert info.invalid and not info.capabilities
    answer = ac.assess(info)
    assert answer["status"] == "limited"
    assert set(answer["limitations"]) == {"agent_info_invalid", "source_epoch_not_declared"}


def test_partly_malformed_capabilities_do_not_confirm_anything_unknown():
    info = ac.parse(GOOD, ["source_epoch", "Bad Name", 7, "future_feature"])
    assert info.invalid and info.unknown_capabilities == 1 and info.capabilities == {"source_epoch"}
    assert "agent_info_invalid" in ac.assess(info)["limitations"]
    assert ac.parse(GOOD, ["x"] * (ac.MAX_CAPABILITIES + 1)).invalid
    assert ac.parse({**GOOD, "channel": "nightly"}, ["source_epoch"]).invalid


def test_unknown_future_capability_is_ignored_not_echoed():
    info = ac.parse(GOOD, ["source_epoch", "future_feature"])
    assert not info.invalid and info.unknown_capabilities == 1
    assert "future_feature" not in repr(ac.assess(info)) and "future_feature" not in repr(info)


def test_audit_classifies_received_values_independently_of_the_declaration():
    audit = ac.EpochAudit(declared_epoch=True)
    assert audit.observe("abcdefgh12345678") == "abcdefgh12345678"
    for raw in (None, "short", "has space in it", 12345678, "x" * 65):
        assert audit.observe(raw) is None
    assert audit.counts() == {"epoch_valid": 1, "epoch_missing": 1, "epoch_invalid": 4}


@pytest.mark.parametrize("values,expected", [
    (["abcdefgh12345678"] * ac.AUDIT_AFTER, "verified"),
    ([None] * ac.AUDIT_AFTER, "not_received"),
    (["short"] * ac.AUDIT_AFTER, "not_received"),
    (["abcdefgh12345678"] * (ac.AUDIT_AFTER - 1) + [None], "inconsistent"),
])
def test_audit_verdict_is_reported_once_after_enough_messages(values, expected):
    audit = ac.EpochAudit(declared_epoch=False)
    verdicts = []
    for value in values:
        audit.observe(value)
        verdicts.append(audit.verdict())
    assert verdicts[:-1] == [None] * (len(values) - 1) and verdicts[-1] == expected
    audit.observe(None)
    assert audit.verdict() is None


def test_agent_declares_only_what_it_implements():
    assert version.CAPABILITIES == ("source_epoch",)
    assert set(version.CAPABILITIES) <= ac.KNOWN_CAPABILITIES
    info = version.hello_info("release", SHA)
    assert info == {"version": "0.3.0", "channel": "release", "revision": SHA}
    assert ac.parse(info, list(version.CAPABILITIES)).capabilities == {"source_epoch"}
    assert ac.assess(ac.parse(version.hello_info(), list(version.CAPABILITIES)))["status"] == "compatible"
    assert version.banner("dev", "unknown") == "Stintara Agent 0.3.0 (build dev, revision unknown)"


def test_agent_shows_only_whitelisted_text_from_the_server():
    answer = {"status": "limited", "limitations": ["source_epoch_not_declared", "print-me\nsecret-token"]}
    text = "\n".join(compat.compat_lines({"type": "agent_welcome", "compat": answer}))
    assert "source_epoch" in text and "restricted" in text and "secret-token" not in text
    ok = "\n".join(compat.compat_lines({"compat": {"status": "compatible", "limitations": []}}))
    assert "not confirmed" in ok
    for welcome in ({"type": "agent_welcome"}, {"compat": "x"}, {"compat": {"status": "bogus-status"}}, None, []):
        assert compat.compat_lines(welcome) == ["Server compatibility: not reported by this server (unknown)"]
