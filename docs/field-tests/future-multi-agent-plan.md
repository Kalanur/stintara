# Future test plan – multiple agents and concurrent sessions

These scenarios need two or more Stintara agents (or two machines) and are **not** part of the Sunday 2026-11-22 field test, in
which Marcel is the only driver with LMU, Pitwall and a Stintara agent (see [2026-11-22.md](2026-11-22.md)). The basic mapping of
the single active agent to its driver stays in the Sunday test (`#63` single-agent rows). This file only collects what to test later.

Nothing here is verified. Each item needs its own preparation, evidence and, where behaviour changes, automated tests.

## Prerequisites

* Two independently provisioned agents with their own drivers, each on its own PC with LMU and Pitwall.
* Agreement on which test covers which issue, and a reviewed build that contains the intended identity/convergence changes.
* Evidence template of the Sunday test (Part E) and the same severity vocabulary.

## Scenarios

| ID | Issue | Scenario | Expected behaviour to verify |
| --- | --- | --- | --- |
| F-01 | #24 | Two agents observe the **same** LMU session (Practice first, then a race) | Both observations converge on one Stintara session; no duplicate live sessions; the participant set is the same |
| F-02 | #24 | Two agents in **different splits** of the same event on the same track | Sessions stay separate; no merge by track, time or display names |
| F-03 | #63 | Two agents, first scoring frame with a stale player vehicle id (slot switch) | Each driver maps to the car they actually drive; neither blocks the other; ambiguous ownership fails closed |
| F-04 | #63 | One agent reconnects shortly during a session | No duplicate session and no second linked participant for that driver |
| F-05 | #70 | Two live sessions at once (for example a Daily and a Championship race) | The landing page shows which verified Stintara driver belongs to which session |
| F-06 | #69 | A second agent whose driver drives the race and reports "no strategy" | Compare acquisition (observations and samples), estimator readiness and UI per agent, never mixing observers |
| F-07 | #46 | Lifecycle transitions with two observers | One physical race stays one session per observer scope; no tail duplicates |
| F-08 | – | Split isolation | Data of one split never appears in another driver's pages |

## Evidence per scenario

Session IDs of every involved session, agent versions, server revision, observation start/end per agent, participant mapping per
driver (counts and ids only, no names), the Sunday Part E fields, and the matching issue comment.
