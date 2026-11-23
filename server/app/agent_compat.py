"""Agent identification and capability compatibility (additive; no persistence, no public API).

An agent may announce ``agent`` (version, channel, revision) and ``capabilities`` in ``agent_hello``. What an agent
*declares* and what the server actually *receives* are kept apart:

* ``assess`` turns the declaration into an answer for ``agent_welcome`` (only for agents that identified
  themselves; older agents get the unchanged welcome). The answer never claims verification: ``verified`` is false.
* ``EpochAudit`` looks at the real stream: how many messages of one connection carried a well-formed
  ``source_epoch``. Its verdict goes to the operator log only, never to a public API.

Nothing here changes how messages are processed. Unknown values are dropped (never echoed), declarations that are
malformed count as undeclared, and a missing capability only restricts the features that need it.
"""
import re
from dataclasses import dataclass

KNOWN_CAPABILITIES = frozenset({"source_epoch"})
REQUIRED_CAPABILITIES = ("source_epoch",)    # needed for trusted lap tyre/fuel checkpoints and lapped intervals
EPOCH_PATTERN = re.compile(r"[A-Za-z0-9_-]{8,64}")
MAX_HELLO_BYTES = 16 * 1024
MAX_CAPABILITIES = 32
AUDIT_AFTER = 20                              # messages of one connection before the one-time verdict

VERSION_PATTERN = re.compile(r"\d{1,4}\.\d{1,4}\.\d{1,4}")
REVISION_PATTERN = re.compile(r"[0-9a-f]{40}|unknown")
CAPABILITY_PATTERN = re.compile(r"[a-z][a-z0-9_]{0,31}")
CHANNELS = frozenset({"dev", "ci", "release"})

LIMIT_NO_EPOCH = "source_epoch_not_declared"
LIMIT_INVALID_INFO = "agent_info_invalid"


@dataclass(frozen=True)
class AgentInfo:
    declared: bool                    # a well-formed identification block (valid version) was received
    invalid: bool                     # something was present but malformed: treated as not declared
    version: str | None = None
    channel: str | None = None
    revision: str | None = None
    capabilities: frozenset = frozenset()   # known capabilities only
    unknown_capabilities: int = 0


LEGACY = AgentInfo(declared=False, invalid=False)


def parse(agent, capabilities):
    """Sanitised view of the optional hello fields. Never raises; nothing unvalidated is kept."""
    if agent is None and capabilities is None:
        return LEGACY
    if not isinstance(agent, dict):
        return AgentInfo(declared=False, invalid=True)
    version, channel, revision = agent.get("version"), agent.get("channel"), agent.get("revision")
    if not (isinstance(version, str) and VERSION_PATTERN.fullmatch(version)):
        return AgentInfo(declared=False, invalid=True)
    bad = False
    if not (isinstance(channel, str) and channel in CHANNELS):
        channel, bad = None, True
    if not (isinstance(revision, str) and REVISION_PATTERN.fullmatch(revision)):
        revision, bad = None, True
    known, unknown = set(), 0
    if capabilities is not None:
        if not isinstance(capabilities, list) or len(capabilities) > MAX_CAPABILITIES:
            bad = True
        else:
            for item in capabilities:
                if not (isinstance(item, str) and CAPABILITY_PATTERN.fullmatch(item)):
                    bad = True
                elif item in KNOWN_CAPABILITIES:
                    known.add(item)
                else:
                    unknown += 1
    return AgentInfo(declared=True, invalid=bad, version=version, channel=channel, revision=revision,
                     capabilities=frozenset(known), unknown_capabilities=unknown)


def limitations(info):
    """Codes for what the declaration does not support; empty means "declared compatible" (not verified)."""
    found = []
    if info.invalid:
        found.append(LIMIT_INVALID_INFO)
    if any(item not in info.capabilities for item in REQUIRED_CAPABILITIES):
        found.append(LIMIT_NO_EPOCH)
    return found


def assess(info):
    """Answer for ``agent_welcome``; None for agents that did not identify themselves (unchanged welcome)."""
    if not info.declared and not info.invalid:
        return None
    found = limitations(info)
    return {"status": "limited" if found else "compatible", "limitations": found,
            "required": list(REQUIRED_CAPABILITIES), "verified": False}


class EpochAudit:
    """Counts what one connection really sent as ``source_epoch``; independent of the declaration."""

    def __init__(self, declared_epoch):
        self.declared_epoch = bool(declared_epoch)
        self.valid = self.missing = self.invalid = 0
        self.reported = False

    @property
    def total(self):
        return self.valid + self.missing + self.invalid

    def observe(self, raw):
        """Count one message and return its epoch if well formed, else None (the server-side rule is unchanged)."""
        if raw is None:
            self.missing += 1
            return None
        if isinstance(raw, str) and EPOCH_PATTERN.fullmatch(raw):
            self.valid += 1
            return raw
        self.invalid += 1
        return None

    def verdict(self):
        """One-time classification after AUDIT_AFTER messages: verified | not_received | inconsistent, else None."""
        if self.reported or self.total < AUDIT_AFTER:
            return None
        self.reported = True
        if self.valid == 0:
            return "not_received"
        return "verified" if self.missing + self.invalid == 0 else "inconsistent"

    def counts(self):
        return {"epoch_valid": self.valid, "epoch_missing": self.missing, "epoch_invalid": self.invalid}
