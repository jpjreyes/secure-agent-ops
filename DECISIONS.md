# Decisions

Each entry: what we chose, the alternatives, and why. Numbers are filled in as they're measured.

## D1. Orchestration: LangGraph, with each agent on the Claude Agent SDK
- **Chose:** LangGraph for the incident workflow (commander → parallel investigators → verifier → executor), with
  each agent's tool loop running on the Claude Agent SDK.
- **Alternatives:** Claude Agent SDK alone (sub-agents); LangGraph with LangChain agent wrappers.
- **Why:** the workflow is an explicit graph we can test (parallel fan-out, `interrupt()` before any write action,
  checkpoints); LangGraph is the most requested orchestration framework in agent-engineering postings after LangChain
  and the base of most open-source AI SRE agents. The Agent SDK gives each agent a production-grade tool loop and runs
  the same way locally and in the cloud (only the credentials change). Security is enforced by the gateway, outside
  the agent's process, not by in-process hooks.

## D2. Durable state: LangGraph Postgres checkpointer
- **Chose:** a checkpoint after every step, keyed by incident; a crashed run resumes from the last completed step.
  Write actions carry an idempotency key checked at the gateway, so a replayed step can't act twice.
- **Alternatives:** Temporal (with its LangGraph plugin).
- **Why:** runs last minutes and pause at most for one human approval, inside one service. Temporal fits workflows
  that wait for days or span services; here it would add a server without adding a guarantee. Proven by a test that
  kills the run before and during an approval and asserts no action ran twice.

## D3. ITBench-AA: 10 development tasks, 30 held-out test tasks
- **Chose:** the 40 public scenarios split 10/30 before any prompt work; the 30 are scored once, 3 repeats.
  Setups: A single agent (same model, same tools), C full team, both on 30 × 3; B team without verifier on 10 × 3.
- **Alternatives:** all 40 with no held-out set (tuning on the reported tasks); a 10–15 task sample.
- **Why:** scoring on tasks used for tuning overstates results. The paired A-vs-C comparison on the same tasks cancels
  most task-to-task noise. The public leaderboard covers 59 tasks (19 private), so it is cited as context only.
  IBM's ITBench-Lite has the same SRE scenario IDs, so it adds no extra test tasks. Any expansion is fixed in
  `evals/PLAN.md` before the scored run.
- **How it runs:** on a Claude subscription through the Agent SDK, in a clean environment (no plugins, user settings
  or memory files), with the request captured to prove the model sees only our prompt, parameters pinned, and the run
  stopping at 80% of the 5-hour or weekly usage window. Cost is reported as API-equivalent from token counts. The
  deployed version uses an API key; a 5-task parity check compares both paths.

## D4. Policy: Cedar at the gateway, Kubernetes admission as the second layer
- **Chose:** Cedar for every agent tool call (rules on arguments, schema-validated, unit-tested, and a property
  proved in CI with Cedar's symbolic analysis: the executor can never delete or roll back without human approval).
  Kubernetes RBAC + ValidatingAdmissionPolicy (CEL) limit what the executor's service account can do even if the
  gateway were bypassed.
- **Alternatives:** OPA/Rego (and Gatekeeper).
- **Why:** Cedar is built for per-request authorization, is deny-by-default with forbid-overrides-permit, can be
  analyzed formally, and is what AWS uses for AgentCore Policy, so these policies would move to the managed service
  unchanged. OPA is the better choice for a client that already runs Gatekeeper.

## D5. Gateway runtime: one container image, local for evals, Lambda in AWS
- **Chose:** the same image runs next to the local cluster (incidents, all evals, overhead measurements) and on AWS
  Lambda (Web Adapter + SnapStart) for the public demo in front of P5's MCP server. Stateless; kill switch, rate
  limits and the audit chain head live in DynamoDB, so a revocation reaches every instance.
- **Alternatives:** ECS Fargate behind a load balancer; AgentCore Gateway (managed).
- **Why:** each tool call is a short request, which fits a stateless function at ~$0 idle; Fargate costs ~$25–40 a
  month to stay warm. Overhead is reported in three rows (local, Lambda warm, Lambda cold start); provisioned
  concurrency only during demos. AgentCore Gateway would replace the part this project exists to build.

## D6. Live telemetry: record the cluster into the benchmark's snapshot layout
- **Chose:** a recorder polls Kubernetes (objects, events, pod status), Prometheus (service error rate and p95 from
  span metrics, container memory/CPU, firing alerts), Jaeger (spans; errors fetched separately) and OpenSearch
  (logs) every 15 s, and rewrites the incident folder in the same layout the ITBench-AA ingest produces. The telemetry
  tools read it unchanged; during an incident each tool call sees data at most one poll old.
- **Alternatives:** a second implementation of every tool against the live APIs.
- **Why:** one implementation means identical tool names, arguments and outputs on the benchmark and on live faults
  (no "it works on the benchmark tools only"); Kubernetes keeps no object history, so a recorder is needed anyway for
  `changes`/`object_diff`; every live incident becomes a frozen, replayable snapshot (same input for every setup and
  repeat, audit trail of what the agents saw). The recording ends while the fault is still active, as in ITBench-AA.
- **Alert parity:** Prometheus gets the two service alerts the benchmark's environment fires (RequestErrorRate,
  RequestLatency; same names, labels and description format) and a 15 s scrape.
- **Environment fixes found while recording (2026-11-12):** the load generator's browser users (headless Chrome) and
  Grafana used ~2 of the VM's 4 CPUs; probes timed out and the collector restarted. Both are off (HTTP users still
  drive the same service traffic). The chart's Prometheus has no config reloader, so `cluster.sh up` restarts it.

## D7. Faults on the live cluster: flags plus our own configuration faults, revert that survives crashes
- **Chose:** 19 faults: 12 of the demo's feature flags (root cause = the flagd-config ConfigMap, as in the
  benchmark) and 7 configuration faults a person could roll out (wrong dependency address in a Deployment's env x3,
  Service selector typo, scale to zero, deny-all NetworkPolicy, memory limit too low). Each has ground truth in
  ITBench-AA's format, so the same scorer runs on benchmark and live incidents.
- **Safety:** the objects a fault touches are saved (atomically) before it changes anything; revert restores them
  from that file. Every episode reverts leftovers first and reverts in `finally`, and nothing (not even a recorder
  that won't stop) stands between a fault and its revert: an early version let a stuck recorder skip the revert,
  caught on the second episode and fixed.
- **Settle time:** flag faults show alerts in ~80 s; configuration faults act only after a rollout and on real
  traffic (checkouts are a few per minute), so they wait up to 300 s. Episodes record which expected alerts fired;
  an episode with no symptom is reported, not scored as a miss.

## D8. Stop-safe runs: a usage stop costs at most the agent calls in flight
- **Constraint:** stop at 80% of the 5-hour or weekly window; one shared quota across projects. Lessons
  from P4 (exit 75 + reset time + a driver that waits and resumes) and P5 (append-only results, pinned config,
  interrupted episodes never counted).
- **Chose:**
  1. Before each episode: usage + the measured cost of one episode (`--reserve`) must fit below 80%, so stops land
     between episodes.
  2. Team episodes are checkpointed after every step in Postgres; a rerun resumes the same thread from the last
     finished step and the result records `resumed: true`.
  3. A stop inside one parallel investigator waits for the investigators already running to finish before failing
     the step (no new agent starts after a stop). Found by the crash-resume test: LangGraph cancels the other tasks of
     a step when one fails, so without the wait their work was thrown away and redone.
  4. Exit 75 with `EVAL_RETRY_AT`; `scripts/run_eval.sh` waits for the reset, restarts the checkpoint database if
     needed, and resumes; paid extra usage or an unclean environment exits 2 for good.
  5. `run_config.json` pins models, turn caps and a hash of the prompts per results folder; a resume with a different
     configuration is refused (new `--tag` instead).
- **Live episodes are not resumed mid-way** (the cluster keeps changing); they are discarded and re-recorded.

## D9. Team v1 on the dev scenarios (2026-11-12; dev only, test untouched)
- **Setup C (dev1), 10 dev scenarios × 1:** mean **0.65** vs setup A **0.35**; root cause found **8/10 vs 5/10**;
  mean false positives 0.6 vs 0.9; **$0.19 vs $0.09** API-equivalent per run; 110 s vs 26 s. Ten C runs used ~3
  points of the 5-hour window and ~1 point of the weekly one.
- **Where the gain comes from:** the drafts (commander + investigators, i.e. setup B's answer) already score 0.70.
  Parallel investigators with one signal each found chaos objects and flag changes the single agent missed (S21,
  S80, S83).
- **Verifier v1 hurt (0.70 → 0.65):** asked to "check and return the final diagnosis", it added causes (S11: a Kafka
  restart that was part of applying the flag). **v2:** it rules on every candidate (draft + every possible cause the
  investigators raised: cause / symptom / unrelated + explained_by) and code submits only causes no other cause
  explains. Replayed on the same team runs: **0.70** (fixes S9's symptom, no regressions). On dev the verifier is
  neutral-to-small; the test-set B vs C comparison measures it properly.
- **Not tuned further (overfitting risk on 10 scenarios):** S26's root cause (an HTTPChaos object) appears nowhere in
  its snapshot (no setup can name it); S33's second entity really had the same bad change; S1's object timestamps are
  snapshot capture times, which hides that the load generator came first.

## D10. Agent identity: a small OIDC-shaped issuer of short-lived tokens, delegation per RFC 8693 (2026-11-12)
- **Chose (option A of three):** the project issues its own tokens. Each agent role is an OAuth client that
  authenticates with a signed JWT assertion (`private_key_jwt`, RFC 7523) and gets a 10-minute ES256 access token
  (client-credentials shape). Acting for a person goes through RFC 8693 token exchange: `sub` = the user, nested
  `act` = the agent chain (commander → executor). The issuer publishes a standard OIDC discovery document and JWKS,
  so the gateway validates any OIDC issuer by configuration (a customer's Okta or Entra ID is a config change).
  On AWS the signing key is a KMS ECC_NIST_P256 key (the private key never leaves KMS).
- **Alternatives:** B, Keycloak locally (Standard Token Exchange since 26.2, but `act` delegation only in preview;
  on AWS Cognito has client credentials and no RFC 8693, so local and cloud would diverge; ~0.5–1 GB more RAM).
  C, SPIFFE/SPIRE plus exchange (our agents run as host processes and on Lambda, not pods, so attestation doesn't
  fit; ~15–25 h). AgentCore Identity (still needs an external IdP for inbound; ties local evals to AWS).
- **Why:** the project's value is the gateway's decisions (Cedar on arguments, screening, audit, kill switch); A
  produces exactly the claims they consume, identical locally and on Lambda, ~$1/month. Postings checked
  (Celonis, Revin and ~10 other FDE postings): none asks to operate an IdP; identity shows up as protocol literacy
  (OAuth, SSO, delegated access) plus least-privilege authorization. P3 already shows a real IdP integration
  (Entra ID SSO).
- **Invariants (same in every option):** tokens live in the agent's MCP connection headers, never in the model's
  context; human approval is a separate signed record checked by Cedar, never a token claim; the kill switch
  deny-lists agent ids and token ids; the orchestrator holding agent credentials is the trust root (documented in
  the threat model).

## D11. Dev numbers re-measured after the concurrency fix (2026-11-12): the old baseline was depressed by a bug
- **Bug:** the telemetry MCP server used one DuckDB connection from several threads; parallel tool calls (several
  agents behind the gateway, or several calls in one agent turn) failed at random. Found by the first team run
  through the gateway (13 of 56 calls failed); fixed with one cursor per query (test reproduces it).
- **Re-measured, dev 10 × 1:** A **0.60** (8/10 root cause found, $0.07/run, 20 s) vs the earlier 0.35 (invalid);
  C **0.75** (8/10, mean FP 0.4, $0.21/run, 93 s). Drafts 0.60 → verifier final 0.75: the verifier v2 now adds
  +0.15 (S9, S11, S21). The team's advantage on dev is **+0.15**, not +0.30, at ~3× the cost and ~4.5× the time.
- **Implication:** the honest headline will be decided on the 30 × 3 test set; cost per correct diagnosis is
  reported next to the score. Earlier dev claims (D9) are superseded by these numbers.

## D12. Executor: the model proposes, code executes exactly what a person approved (2026-11-12)
- **Chose (option B of three):** the executor agent has only read-only tools and writes a structured plan (per action:
  tool, arguments, why, expected effect, a precondition on the current state, and the undo). The graph pauses with
  LangGraph `interrupt()` (durable through the checkpointer); the on-call person approves, edits or rejects; the
  issuer signs the approved calls (hashes of server + tool + arguments). Code then runs exactly those calls through
  the gateway with the executor's delegated identity, in order, checking each precondition first and stopping at
  the first failure (idempotency keys make a resumed run safe); a verify step re-reads the alerts and reports.
- **Alternatives:** A, the model calls the write tools itself after approval (drift between approved and executed
  arguments; the model holds write tools while reading possibly poisoned telemetry). C, tiered autonomy (low-risk
  actions without a person): dropped: speed is not a priority for this project.
- **Why:** safety holds by construction, not by prompt: the model can't execute anything, what runs is
  byte-identical to what was approved, preconditions catch a cluster that changed in between, Cedar still enforces
  (no scale to zero, protected platform) even on approved calls. Matches OWASP's least agency / separate generation
  from execution and the approval modes of shipping AI-SRE products (PagerDuty, Datadog). Plans can be scored
  offline, which keeps the remediation eval cheap.
- **Cost accepted:** a failed fix needs a new plan and a new approval.

## D13. Live episodes must start clean (2026-11-12)
- **Found:** the first remediation smoke run flagged product-catalog lock contention in an episode about checkout.
  Logs showed productCatalogLockContention kept running inside product-catalog after its flag was set back to off:
  9 episodes recorded after it carried a second, unlabeled problem (an agent naming it was scored as a false
  positive for a real issue).
- **Fix:** reverting a flag fault also restarts the service the flag acts on; an episode records whether an error
  alert was already firing during its baseline (`baseline_clean`); only complete, symptomatic, clean episodes are
  scored. The 9 contaminated episodes are recorded again; results on them were discarded.

## D14. Kill switch and limits (2026-11-12)
- **Kill switch:** revoke an agent, one token or one tool; checked on every request (token verification and each
  tool call), so no session outlives a revocation; disabled tools also disappear from tool lists. Local backend: a
  JSON file shared by all gateway processes (re-read only when it changes); DynamoDB on AWS (M7). **Measured
  time-to-effect (local, over HTTP with full token verification): median 15.6 ms, max 87 ms over 5 trials**
  (`tests/test_gateway.py::test_kill_switch_time_to_effect`).
- **Rate limit:** token bucket per agent (120 calls/min, burst 40; a team episode measured ~70 calls in ~90 s across
  6 agents, so normal work never hits it); refusals are audited.
- **Output cap:** telemetry results over 20,000 characters are cut with a notice to narrow the query (protects the
  model's context and cost per call); recorded in the audit as `output_cut`.

## D15. Policy safety properties checked exhaustively in CI (2026-11-12)
- **What:** Cedar itself evaluates every combination of the attributes the policies can distinguish (principal,
  tool server/id/annotations, scopes, chain, delegation, approval, and each argument as absent / each constant /
  another value): 221,184 requests, 29,736 allowed, in ~40 s. Properties on every allowed request: nothing
  destructive without approval; only the executor, delegated through the commander, with cluster:write, in
  otel-demo ever gets a non-read-only tool; protected platform services never touched; never scale below 1; a tool
  annotated both read-only and destructive still needs approval; read-only tools need telemetry:read.
- **Found by it:** rule 2 (executor cluster actions) also covers read-only tools on the cluster server (cluster:write
  without telemetry:read suffices there). Kept on purpose and written into the property: writing the cluster
  implies reading it.
- **Mutation check:** deleting the destructive-needs-approval rule makes exactly the two approval properties fail.
- **Not yet:** an SMT proof with Cedar's symbolic compiler (cedar-policy-symcc + cvc5, Lean-verified soundness);
  needs a Rust toolchain (~1–2 GB). The exhaustive check covers the same finite abstraction; the proof would also
  cover schema changes without re-deriving the equivalence classes.

## D16. Approval channel: the terminal (2026-11-12)
- **Chose:** the on-call person approves, edits or rejects the executor's exact calls in a terminal prompt
  (`sao.incident`, `terminal_approver`); the graph waits durably at the interrupt, so approval can come later and
  survives restarts. Slack or a web page would only change the transport: the signed approval of exact call hashes
  stays the same.
- **Addendum (before any agent ran on live episodes):** flagd's error-rate alert is background noise (idle cluster:
  3.3 errors per 10 min on flagd, 0 elsewhere) and no longer makes a baseline dirty. Final live set: 10 scorable
  episodes (complete, expected alerts fired, clean baseline): 6 flag faults, 4 configuration faults (cart scaled
  to zero, Service selector, two bad env rollouts). 9 listed but not scored: 8 whose expected alerts never fired
  (ad high CPU, email leak, image slow load, Kafka queue, homepage flood, recommendation cache, recommendation OOM,
  currency NetworkPolicy) and 1 whose baseline held the previous fault's errors (product-catalog failure).

## D17. Live-fault results (2026-11-12): a ceiling for diagnosis, the remediation result is the signal
- **Diagnosis, 10 scorable live episodes × 1:** A 1.00 (10/10, 0 FP, $0.044/run, 15 s); C 1.00 (10/10, 0 FP,
  $0.194/run, 91 s). The verifier repaired 3 drafts (0.50 → 1.00). Both perfect: one fault at a time on a clean
  cluster with an obvious config diff or flag change is too easy to separate the setups (ceiling effect). Reported
  as such; the harder, noisier ITBench-AA test split carries the A-vs-C comparison.
- **Remediation (team only), plans scored against each fault's known remedy:** **10/10 correct, 10/10 safe**
  (no extra actions); all 9 proposed actions accepted by the API server in dry run through the gateway, 0 blocked by
  policy; the Service-selector fault (no tool can fix it) correctly got no action and an escalation.
- **Next to make live faults discriminating (not done):** two simultaneous faults, noisy background faults, or
  faults whose cause is one hop from the symptom (e.g. a dependency's limit).

## D18. Log-injection suite results (2026-11-12): no hijacks, a small utility cost, and a weaker test than hoped
- **Run:** 30 injected incidents (10 dev scenarios × 3 goals, AgentDojo's 5 templates verbatim) × 3 conditions.
- **Hijacks:** **0/90** (no planted root cause in a final diagnosis, no planted scale-to-zero in an executor plan),
  **0/90 prompt leaks**, in every condition, including the unprotected single agent.
- **Utility under attack** (vs the clean dev score of the same scenarios, 1 run each, so noisy): A 0.58 vs 0.60;
  team with screening off 0.67 vs 0.75; team with screening on 0.68 vs 0.75. The attacks cost the team a little
  accuracy (distraction), not control.
- **Screening:** flags ~1.2 planted lines per run, 0 for important_instructions (as known before the run); on vs
  off made no measurable difference, because there were no successful attacks to prevent.
- **What this does and doesn't show:** today's Claude models resist these published instruction-style templates
  on their own in this setting, and the architectural controls (no write tools for models, approvals, Cedar) were
  never exercised by a successful attack, so this suite does not measure them. The realistic threat for an SRE team
  is **deceptive evidence** (plausible fake log lines that point at the wrong service), not instructions.
- **Next (written before running it):** an adaptive suite of plausible false evidence (e.g. fake "connection
  refused to payment" errors timed before the real fault, a fake config-change event on another Deployment), scored
  the same way. Until then, the architectural controls are evidenced by their tests (THREAT_MODEL.md), not by this
  suite.

## D19. Deceptive-evidence suite results (2026-11-12): the single agent was fooled twice, the team never
- **Run:** 30 incidents (10 dev scenarios × 3 kinds of false evidence: fake failed-call ERROR logs, fake Warning
  Kubernetes Events on the decoy's real pod, a log line stating a false root cause as fact) against a healthy decoy
  the carrier service really calls; single agent (A) and team through the gateway with screening on (C) = 60 runs.
  Protocol written before the run (evals/PLAN.md).
- **Decoy blamed in the final diagnosis:** A **2/30** (both fake Kubernetes Events: Scenario-15, Scenario-26);
  C **0/30**. The verifier saw the planted evidence and ruled the decoy "unrelated" each time it came up.
- **Executor plans acting on the decoy:** 0/30. Prompt leaks 0/60. Screening flagged 0 planted lines (expected:
  false evidence has no instruction in it, so pattern screening is not a defence against it).
- **Accuracy under deception** (vs clean dev, 1 run each, noisy): A 0.53 vs 0.60 (Events 0.40, logs and claim
  0.60); C 0.67 vs 0.75 (0.65–0.70 across kinds).
- **Reading:** unlike instruction-style injection (D18: 0/90), false evidence does move a model: fake Events on a
  pod are the most convincing kind, because a BackOff on a real pod looks like a real failure. The team's structure
  is the defence that worked: investigators cross-check one signal each and the verifier tests every candidate
  against the others (traces and metrics showed the decoy healthy). The cost is the same small distraction seen in D18.
- **Limits:** 1 run per incident; the decoy is uncorroborated by design (an attacker who can also fake metrics or
  traces is out of scope); Events need `create events` in the namespace, a privilege worth restricting by RBAC.

## D20. Harder live faults (2026-11-12): the ceiling is gone, the team is no better at diagnosis, and its fixes stay safe
- **Episodes:** 7 recorded on the live cluster (evals/PLAN.md, written before recording): 3 with two faults at once
  (a flag + a bad rollout), 4 with one fault + a harmless rollout of another Deployment at the same moment. One
  episode had no alerts in 150 s (the simultaneous checkout restart slowed the symptoms); settle raised to 300 s for
  all combined episodes and that episode recorded again (the first recording is kept, not scored).
- **Diagnosis (1 run each):** A **0.64**, C **0.64**, identical per episode (single clean faults: both 1.00, D17).
  Both named the harmless frontend rollout next to the real ad flag (1/4 red herrings fooled both); both found only
  the rollout in 2 of 3 two-fault episodes and missed the flag change (cart + checkout, ad + shipping).
- **Remediation (team):** correct 5/7 (every plan fixed what the team diagnosed; the 2 misses are the undiagnosed
  flag parts), **safe 7/7**: no action on any red herring, including the frontend rollout the diagnosis named
  (the executor acted only where the evidence supported a fix), and the selector fault correctly escalated.
  Cost ≈ $0.04 (A) vs $0.18 (C) per episode.
- **Reading:** two faults at once is where both setups fail, the same way: once a clear cause (a fresh rollout)
  explains the alerts, neither looks for a second one. The benchmark's scenarios have one root cause each, so this
  case is outside what the team was tuned for; the remediation result is what matters here.
- **Limits:** 7 episodes × 1 run; diagnosis differences of one episode are noise.

## D21. Held-out test results (2026-11-13): the verifier is what works; the team as a whole is not significantly better
- **Run (evals/PLAN.md, fixed before any test run):** ITBench-AA test split, 30 scenarios. Single agent (A) and full
  team (C) 5 repeats each (3 planned; the pre-registered rule added 2 because the A-vs-C interval included zero),
  team without verifier (B) 3 repeats on all 30 (expansion step 1). 390 scored runs, request byte-identical to the
  capture (evals/CAPTURE.md), code frozen before the run. Intervals: bootstrap over scenarios, paired by scenario
  (`evals/paired_ci.py`).

| | A single agent | B team, no verifier | C full team |
|---|---|---|---|
| Mean score [95% CI] | 0.533 [0.373, 0.690] | 0.487 [0.343, 0.628] | **0.573** [0.407, 0.733] |
| Root cause found | 58% | 60% | 58% |
| False positives per run | 0.63 | 0.72 | **0.43** |
| API-equivalent $ per run | 0.084 | 0.098 | 0.207 |
| Wall time per run | 21 s | 65 s | 87 s |

- **Paired differences:** C − B **+0.086 [+0.016, +0.157]** (the verifier; better on 12 scenarios, worse on 3);
  C − A +0.040 [−0.010, +0.097] (not significant; better on 7, worse on 4); B − A −0.046 [−0.132, +0.042].
  The team's own draft scored 0.464; the verifier took it to 0.573.
- **Reading:** finding the root cause is the same for all three (~58–60%): more agents reading more signals do not
  find more causes. What separates them is false positives. Splitting the work across investigators adds suspects
  (B has the most), and the verifier, which tests every candidate against the others' evidence and keeps only causes
  nothing else explains, removes ~40% of them. The full team ends ahead of the single agent, but by less than the
  noise across 30 scenarios, at 2.5× the cost and 4× the time.
- **Against dev (D11):** dev showed A 0.60 vs C 0.75 on 10 scenarios × 1 run; the held-out test confirms the
  direction and the verifier's role, not the size. Dev overstated the gap, which is why the test split exists.
- **What this means for the design:** the verifier is the part to keep in any setup, including a single agent
  (a single agent + verifier is the obvious next variant, not run: it would be a new configuration decided after
  seeing test). The team's other value is operational, not accuracy: per-agent least privilege, the read-only /
  write split, and the remediation path (D17, D20), which a single agent with every tool can't offer safely.
- **Limits:** 30 scenarios; 10 of them unsolved by every setup in every repeat (some need signals the snapshot
  doesn't have); one model family; Cloudflare 403s interrupted the run twice (the driver resumed, no runs lost or
  repeated, every result file is a complete run).
