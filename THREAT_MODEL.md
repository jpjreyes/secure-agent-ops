# Threat model

Scope: the incident-response team (LangGraph orchestrator, Claude agents), the MCP security gateway, the token
issuer, the upstream MCP servers (telemetry: read-only; cluster: the only write tools), the Kubernetes cluster and
its telemetry, the human approver, the audit log and the checkpoint database. Method: STRIDE per component, each
threat mapped to its control and to the test or measurement that shows the control works. Last reviewed 2026-11-12.

## System and trust boundaries

```
 on-call person ──approves exact calls──▶ issuer (signs approval)          ┌──────────── gateway ────────────┐
                                                                            │ verify token · kill switch ·    │
 orchestrator (LangGraph + Postgres) ──mints tokens──▶ issuer               │ rate limit · Cedar · approval · │
   │ starts agents with a token in their MCP connection header              │ screening · size cap · audit    │
   ▼                                                                        └───────┬───────────────┬─────────┘
 agents (Claude; read-only tools) ──MCP/HTTP + bearer──────────────────────────────▶│               │
 executor code (no model) ──MCP/HTTP + bearer + X-SAO-Approval─────────────────────▶│               │
                                                                    telemetry MCP (read)    cluster MCP (write)
                                                                            │                       │
                                                         recorder ◀── cluster telemetry ◀── Kubernetes (kind)
```

Boundaries: (1) **telemetry → agents**: logs, events and traces are written by any workload and by anyone who can
make a service log a line; this is the main untrusted input. (2) **agents → gateway**: the model's tool calls are
untrusted requests. (3) **person → approval**: the only path to a write. (4) **gateway → cluster**: the only
credentialed write path.

## Headline threat: telemetry injection

An attacker who can make any service log text (a crafted request whose payload ends up in an error log, a
compromised workload, a malicious dependency) plants instructions for the agents: blame the wrong service, propose a
harmful action, leak the prompt or credentials.

| Layer | Control | Evidence |
|---|---|---|
| The model can't act | No agent holds a write tool. The executor proposes; code executes only calls a person approved (D12) | `tests/test_remediation.py` (injected scale-to-zero approved and still blocked) |
| A person sees every write | Approval signs the exact calls (server, tool, arguments hash); a changed argument isn't approved | `tests/test_gateway.py::test_destructive_call_needs_approval_of_that_exact_call` |
| Rules the approval can't override | Cedar forbids: no scale below 1, platform services untouchable, writes only in the app namespace | `tests/test_policy_properties.py` (exhaustive, 221k requests) |
| Untrusted data marked | Every telemetry result wrapped in a per-call tagged block; agents told it is data | `tests/test_screening.py::test_spotlight_tag_cannot_be_closed_from_inside` |
| Suspicious lines flagged | Pattern screening flags instruction-like lines (never removes evidence); 0 flags on 7,535 real lines | `tests/test_screening.py`; **known gap:** misses AgentDojo's important_instructions template |
| Wrong diagnosis caught | Verifier rules on every candidate with evidence | measured in the log-injection suite (evals/PLAN.md) |
| Measured, not assumed | Log-injection suite: AgentDojo templates (not written by us) × 3 goals × 10 incidents, with screening on/off and an unprotected baseline | `evals/injection.py`, results in DECISIONS.md once run |

Residual risk: a convincing injection can still mislead the **diagnosis** (no control can make a model immune to
persuasive evidence); the design limits the damage to a wrong report and a wrong proposal that a person reviews.

## STRIDE by component

| Component | Threat (STRIDE) | Control | Evidence / residual |
|---|---|---|---|
| Agent ↔ gateway | **S** an agent or attacker poses as another agent | ES256 tokens from our issuer, audience-bound, 10 min; agents authenticate with private_key_jwt (D10) | `tests/test_identity.py` (forged, wrong key, `none`/HS256, wrong audience, untrusted issuer refused) |
| Agent ↔ gateway | **E** an investigator calls a write tool | Cedar: non-read-only tools only for the delegated executor; tool lists filtered | exhaustive property; `test_tool_lists_are_filtered_per_agent` |
| Agent ↔ gateway | **E** escalate through delegation | Only allowed hand-offs (commander → executor); scopes = agent's ∩ user's grant | `test_chain_can_only_continue_to_allowed_agents`, `test_delegation_never_exceeds_the_user` |
| Agent ↔ gateway | **I** token leaked through the model | Tokens live in the MCP connection header; the model never sees them (request capture) | `evals/CAPTURE.md` |
| Agent ↔ gateway | **D** runaway agent floods tools | Per-agent rate limit; output cap 20k chars | `test_rate_limit_refuses_a_runaway_agent`, `test_oversized_output_is_cut_with_a_notice` |
| Agent ↔ gateway | **R** "the agent did it" disputes | Hash-chained audit of every list/call/denial with agent, user, chain, token id, args hash, policy | `test_audit_chain_verifies_and_detects_edits` |
| Issuer | **S/R** replayed client assertion | Single-use jti, 5-min max lifetime, audience = token endpoint | `test_replayed_assertion_is_refused` |
| Issuer | **I** signing key stolen | Local: file 0600 outside git; AWS: KMS key, private key never leaves KMS | residual: local key on disk |
| Approval | **T** approval reused for another action | Signed hash of exact calls; approver must be the person the executor acts for | `test_destructive_call_needs_approval_of_that_exact_call` |
| Approval | **E** approval fatigue (rubber-stamping) | Plan shows why, expected effect, precondition, undo; one review per plan | residual: human factor |
| Executor code | **T** cluster changed after approval | Precondition per action checked before calling; stop at first failure | `test_changed_state_stops_before_acting` |
| Executor code | **D/T** double execution on resume | Stable idempotency keys; only successful actions consume a key | `tests/test_cluster_actions.py` |
| Cluster MCP | **E** argument outside the app | Namespace pinned by code; Cedar requires otel-demo; protected names | `test_calls_pin_namespace_and_stable_keys`, exhaustive property |
| Telemetry MCP | **T** injection in tool output | See headline section | suite |
| Telemetry MCP | **D** concurrent calls corrupt results | One DuckDB cursor per query | `test_concurrent_calls_on_one_server_all_succeed` (fails without the fix) |
| Gateway | **E** a compromised agent keeps acting | Kill switch per agent/token/tool, checked on every request | measured time-to-effect: median 15.6 ms (D14) |
| Gateway | **T** policies drift from intent | Schema-validated policies; tool names in policies must exist; exhaustive safety properties in CI | `tests/test_policy.py`, `tests/test_policy_properties.py` |
| Audit log | **T/R** history rewritten | Hash chain (edits, deletions, reordering detected); AWS: S3 Object Lock (M7) | residual locally: whoever controls the host can truncate the tail |
| Orchestrator | **S/E** orchestrator compromised | It holds every agent's key: it is the trust root. Mitigations: per-agent keys (revocable), KMS on AWS, approvals still needed for writes | residual: a compromised orchestrator can impersonate agents but can't approve writes without the approver's signature… unless it also controls the issuer's signing key (separate it on AWS: approval signing key only in the approval service) |
| Checkpoint DB | **T** state tampered between steps | Local Postgres on loopback; residual: no integrity check on checkpoints | future: sign checkpoints |
| Eval runner | **I/T** evals silently billed to an API key or contaminated by local config | Guard refuses API keys, unknown hooks; clean config; request capture | `src/sao/llm/guard.py`, `evals/CAPTURE.md` |

## Known gaps and next steps

1. Pattern screening misses paraphrased or templated injections (measured: AgentDojo's important_instructions).
   Next: measure the suite; consider a classifier screen (a small model judging "is this an instruction to an
   agent") with its own false-positive measurement.
2. Approval signing and token signing share the issuer key locally. On AWS: separate keys, the approval key only in
   the approval service.
3. No SMT proof of the policies yet (exhaustive check instead; D15).
4. Checkpoints are not integrity-protected.
5. The audit log can be truncated by whoever controls the host; on AWS it goes to S3 with Object Lock.
