# What the model receives on the subscription path (request capture, 2026-11-12)

`uv run python -m evals.capture` routes the Agent SDK's CLI through a request-logging proxy on 127.0.0.1 (bodies
only; the auth header is never logged) and checks every request of one setup-A agent and one team investigator on a
dev scenario. Claude Agent SDK for Python (bundled CLI 2.1.321).

| Check | Result |
|---|---|
| System prompt | 3 blocks: a billing header line, the SDK's standard line ("You are a Claude agent, built on Anthropic's Claude Agent SDK."), then **ours, byte-identical** (A: the task prompt; investigator: its role prompt) |
| First user message | exactly ours; no text from hooks, plugins, memory files or settings in any request (the org-managed SessionStart sentence never reaches the model) |
| Tools | our telemetry tools with full schemas + `StructuredOutput` (the SDK's JSON-schema answer tool) + an inert `DeferredToolPlaceholder` (`defer_loading: true`, never shown to the model). An investigator gets only its signal's 7 tools |
| Model | the pinned id (`claude-sonnet-5-5`, `claude-haiku-5-5`) on every request |
| Parameters | `max_tokens` 128000; `thinking: {type: adaptive}`; `output_config.effort` high (Sonnet) / medium (Haiku); context management clears old thinking blocks; streaming |
| Side requests | one short session-title request per agent run (separate call, no tools, not part of the agent's conversation); it uses a little quota and does not affect answers |

**Consequence for the API path (deploy parity check):** the deployed version runs the same Agent SDK with an API key,
so it sends the same system blocks, tools and parameters (the billing header differs). The parity check (5 tasks ×
3 on both paths) compares scores, not requests.
