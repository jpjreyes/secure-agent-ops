# Secure Agent Ops

A multi-agent incident-response team for Kubernetes, and the security layer that makes it safe to point at
production: every tool call goes through an MCP gateway with agent identity, Cedar policies on the actual
arguments, human approval bound to the exact calls, injection screening of telemetry, a tamper-evident audit log
and a kill switch. The model never holds a write credential: it proposes a fix, a person approves the exact calls,
code executes them.

Measured on **ITBench-AA** (IBM's SRE root-cause benchmark, with a held-out test split), on **19 faults injected
into a live OpenTelemetry Demo cluster**, and on a **log-injection attack suite** built from AgentDojo's templates.

## How it works

```
alert ─▶ commander ──▶ investigators ×4, in parallel (changes · logs · traces · metrics; read-only, one signal each)
            │                     │
            ▼                     ▼
        draft diagnosis ◀── findings with evidence
            │
            ▼
        verifier: rules on every candidate (cause / symptom / unrelated); code keeps causes nothing else explains
            │
            ▼
        executor: proposes a structured fix (action, why, precondition, undo) ─▶ pause for a person (terminal)
                                                                                 │ approves the exact calls
                                                                                 ▼
                                code: check preconditions ─▶ call the gateway with the executor's delegated token
                                                                                 │
   ┌──────────────────────────────────── MCP security gateway ──────────────────┴────────────────────────────────┐
   │ token (issuer, audience, expiry) · kill switch · rate limit · Cedar (agent, tool, delegation, approval, args) │
   │ approval = signed hashes of the exact calls · output cap · injection screening · hash-chained audit log      │
   └──────────────────────────────────────────────────────────────────────────────────────────────────────────────┘
                        │ read-only                                          │ the only write tools
                 telemetry MCP server                                cluster MCP server (idempotent, dry-run)
```

- **Orchestration:** LangGraph, every step checkpointed in Postgres; a crashed or stopped run resumes from its last
  step, and parallel investigators that finished are not redone (tested).
- **Agents:** Claude through the Agent SDK (Sonnet for commander, verifier and executor; Haiku for investigators).
- **Identity:** an OIDC-shaped issuer: agents authenticate with `private_key_jwt`, get 10-minute ES256 tokens;
  acting for a person goes through RFC 8693 token exchange (`sub` = the person, nested `act` = the agent chain).
  Any OIDC issuer (Okta, Entra ID) plugs in by configuration; the signing key lives in KMS on AWS.
- **Policies:** Cedar, schema-validated; safety properties checked exhaustively in CI (221k requests evaluated).

## Results

| | Single agent (A) | Team (C) |
|---|---|---|
| ITBench-AA dev, 10 scenarios × 1 | 0.60 mean score · root cause found 8/10 · $0.07 · 20 s | **0.75** · 8/10 · $0.21 · 93 s |
| **ITBench-AA test, 30 held-out scenarios × 5** | 0.53 [0.37, 0.69] · root cause 58% · 0.63 false positives/run · $0.08 · 21 s | **0.57** [0.41, 0.73] · 58% · **0.43** false positives/run · $0.21 · 87 s |
| Live faults (10 clean, symptomatic episodes of the 19) | 1.00 · 10/10 · $0.04 · 15 s | 1.00 · 10/10 · $0.19 · 91 s; **remediation 10/10 correct and safe** (1 correct escalation) |
| Harder live faults (7: two faults at once, or a fault + a harmless rollout) | 0.64 · $0.04 | 0.64 · $0.18; **remediation safe 7/7**, correct 5/7 (misses = the undiagnosed second fault) |

On the held-out test the team is ahead of the single agent by +0.04 [−0.01, +0.10], within the noise of 30
scenarios. The part that measurably works is the **verifier**: the same team without it scores 0.49, and adding it
is worth **+0.09 [+0.02, +0.16]** (paired, bootstrap 95% interval), all of it from cutting false positives by ~40%.
More agents don't find more root causes (~58% for every setup); checking each suspect against the other signals is
what makes the answer cleaner. Dev (10 scenarios × 1) had overstated the gap: that's what the held-out split is for
(DECISIONS.md D21).

A follow-up (exploratory, D22): **one agent + the same verifier** scores 0.572, identical to the full team (0.573),
at **73% of the cost and 44% of the time** ($0.15, 38 s). The pattern that pays off is generator + independent
verifier; parallel investigators add nothing at this incident size.

The single live faults are clean, with an obvious change: both setups hit the ceiling there, and the team
costs ~4× more for the same diagnosis. On the harder episodes both drop to 0.64 the same way (they stop at the first
clear cause and miss a second fault). The team's value on them is the verified, approved remediation; the harder
benchmark split carries the diagnosis comparison.

Gateway and security measurements:

| What | Result |
|---|---|
| Kill switch time-to-effect (revoke → next call refused) | median 15.6 ms, max 87 ms |
| Cedar decision per call | ~0.6 ms p50 |
| Injection screening false positives on real telemetry | 0 of 7,535 lines (all 40 benchmark incidents) |
| Policy safety properties | hold on all 221,184 requests the policies can distinguish; a deleted rule is caught |
| Log-injection suite (AgentDojo templates × 3 attacker goals × 10 incidents, 3 conditions = 90 runs) | **0/90 hijacked, 0/90 prompt leaks**, including the unprotected single agent; attacks cost the team ~0.07 accuracy (distraction). Instruction-style templates don't move today's models |
| Deceptive-evidence suite (fake failed-call logs, fake Kubernetes Events, a false "root cause" line, against a healthy service; 60 runs) | **single agent blamed the decoy 2/30, team 0/30**; no plan acted on it; accuracy A 0.53 vs 0.60 clean, team 0.67 vs 0.75 |

Cost is reported as **API-equivalent** (the token counts priced at API list prices); evaluations ran through the
Agent SDK on a Claude plan, in an isolated configuration, with the request verified byte-for-byte
([`evals/CAPTURE.md`](evals/CAPTURE.md)).

## Security

The headline threat is **telemetry injection**: anyone who can make a service log a line can put text in front of
the agents. The design limits the damage by construction rather than by prompt. See
[`THREAT_MODEL.md`](THREAT_MODEL.md) (STRIDE per component, every control mapped to its test).

| Control | Enforced by |
|---|---|
| No model ever holds a write credential | the executor only proposes; code executes approved calls |
| A person approves the exact calls | signed hashes of server + tool + arguments; another argument is not approved |
| Rules an approval can't override | Cedar: no scale below 1, platform services untouchable, writes only in the app namespace |
| Least privilege per agent | investigators can't even see write tools; delegation only commander → executor |
| Untrusted data marked | every telemetry result wrapped in a per-call tagged block; instruction-like lines flagged |
| Accountability | hash-chained audit log of every call and denial (edits detected); OpenTelemetry trace per incident |
| Stop it now | kill switch per agent, token or tool, checked on every request |

## Engineering decisions

Every decision with alternatives, reasons and numbers is in [`DECISIONS.md`](DECISIONS.md) (D1–D22), including the
mistakes testing caught: a concurrency bug in the telemetry server that had depressed the baseline from 0.60 to
0.35, a policy that named a tool that doesn't exist, faults that left state behind and contaminated later episodes.
The evaluation protocol was written before results: [`evals/PLAN.md`](evals/PLAN.md).

## Run it

```bash
uv sync
scripts/cluster.sh up                 # kind cluster on Colima + the OpenTelemetry Demo
scripts/dev_db.sh up                  # Postgres for the checkpointer
uv run python -m sao.faults list      # 19 faults; inject / status / revert
uv run python -m sao.faults.episode flag-payment-failure   # record a live incident
uv run python -m sao.incident runs/live/<episode> --dry-run # team + terminal approval + execution (server dry run)
uv run pytest -q                      # 113 tests; CI runs the ones that need no cluster or local data
```

Evaluations: `evals/run_itbench.py` (A/B/C, stop-safe, resumable), `evals/run_injection.py`, `evals/compare.py`.
