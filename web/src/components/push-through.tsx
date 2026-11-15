"use client";

import { useState } from "react";
import { API, type RunDoc, fmtArgs } from "@/lib/run";
import { Panel } from "./run-panels";

type Result = {
  attempt: string;
  decision: "allow" | "deny";
  policies: string[];
  approval?: string;
  decide_ms?: number;
  result: string;
  sent: { tool: string; args: Record<string, unknown> };
};

const ATTEMPTS: { id: string; label: string; expect: string }[] = [
  { id: "unsigned", label: "Run it without a signature", expect: "the executor sends the proposed call as is" },
  { id: "edited", label: "Sign it, then change one argument", expect: "a valid signature, a different call" },
  { id: "investigator", label: "Send it as an investigator", expect: "signed, but from a read-only agent" },
  { id: "scale0", label: "Signed: scale payment to zero", expect: "a person signs something harmful" },
  { id: "signed", label: "Sign exactly what was proposed", expect: "the only path that goes through" },
];

const WHY: Record<string, string> = {
  "destructive-needs-approval": "a write needs a person's signature for this exact call",
  "no-scale-to-zero": "scaling to zero is an outage; no signature overrides this rule",
  "protect-platform": "the telemetry stack and flag service are off limits",
  "executor-cluster-actions": "executor, acting for the on-call person, signed, in the app namespace",
};

export function PushThrough({ doc }: { doc: RunDoc }) {
  const call = doc.approval?.calls?.[0];
  const [results, setResults] = useState<Result[]>([]);
  const [busy, setBusy] = useState<string | null>(null);
  const [err, setErr] = useState<string | null>(null);
  if (!call) return null;

  const run = async (attempt: string) => {
    setBusy(attempt);
    setErr(null);
    const r = await fetch(`${API}/api/push`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ attempt, incident: doc.incident, run_id: doc.id, call: { tool: call.tool, args: call.args } }),
    }).catch(() => null);
    const d = r ? await r.json().catch(() => null) : null;
    if (!r?.ok || !d) setErr(d?.error || "Could not reach the gateway.");
    else setResults((x) => [d, ...x.filter((y) => y.attempt !== attempt)]);
    setBusy(null);
  };

  return (
    <Panel title="Now push the fix through anyway" kicker="real gateway · simulated cluster">
      <p className="text-[13px] text-muted">
        No agent can call a write tool by itself, so a normal run never reaches the policy layer. Try it here: each
        button sends one call through a fresh copy of the gateway (identity, Cedar, signature check) to a cluster that
        only records what would have run.
      </p>
      <div className="mt-3 grid gap-1.5 sm:grid-cols-2">
        {ATTEMPTS.map((a) => (
          <button key={a.id} onClick={() => run(a.id)} disabled={!!busy}
            className={`border px-3 py-2 text-left transition disabled:opacity-50 ${a.id === "signed" ? "border-pass/60 hover:bg-pass-soft" : "border-line hover:border-stop hover:bg-stop-soft"}`}>
            <span className="block text-[13px] font-semibold">{busy === a.id ? "sending…" : a.label}</span>
            <span className="block text-[11.5px] text-faint">{a.expect}</span>
          </button>
        ))}
      </div>
      {err && <p className="mt-2 text-[12px] text-stop">{err}</p>}
      {results.length > 0 && (
        <ul className="mt-3 space-y-2">
          {results.map((r) => {
            const pol = r.policies?.[0] || "default-deny";
            return (
              <li key={r.attempt} className={`rise border-l-2 py-1 pl-3 text-[12.5px] ${r.decision === "allow" ? "border-pass" : "border-stop"}`}>
                <div className="flex flex-wrap items-baseline gap-x-2">
                  <span className={`font-mono text-[12px] font-semibold ${r.decision === "allow" ? "text-pass" : "text-stop"}`}>
                    {r.decision === "allow" ? "ALLOWED" : "DENIED"}
                  </span>
                  <span className="font-mono text-[11.5px]">{pol}</span>
                  {r.decide_ms != null && <span className="font-mono text-[10.5px] text-faint">{r.decide_ms} ms</span>}
                </div>
                <p className="text-muted">{WHY[pol] || r.result}</p>
                <p className="mt-0.5 font-mono text-[10.5px] text-faint">
                  sent {r.sent.tool}({fmtArgs(r.sent.args)}){r.decision === "allow" ? ` → ${r.result}` : ""}
                </p>
              </li>
            );
          })}
        </ul>
      )}
    </Panel>
  );
}
