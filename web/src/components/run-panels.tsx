"use client";

import { useState } from "react";
import { API, GOAL_TEXT, type AuditRecord, type RunDoc, type Step, fmtArgs, hits, shortEntity } from "@/lib/run";

export function Panel({ title, kicker, children, className = "" }: { title: string; kicker?: string; children: React.ReactNode; className?: string }) {
  return (
    <section className={`ticks border border-line bg-panel/80 p-4 backdrop-blur-[1px] sm:p-5 ${className}`}>
      <div className="mb-3 flex items-baseline justify-between gap-3">
        <h3 className="text-[15px] font-semibold tracking-tight">{title}</h3>
        {kicker && <span className="tag text-faint">{kicker}</span>}
      </div>
      {children}
    </section>
  );
}

const NODE_LABEL: Record<string, string> = {
  commander_plan: "Commander · plan",
  investigate: "Investigator",
  commander_synthesize: "Commander · draft",
  verify: "Verifier · rulings",
  plan_fix: "Executor · proposed fix",
  approve: "Approval step",
};

function Clamp({ text }: { text?: string }) {
  const [open, setOpen] = useState(false);
  if (!text) return null;
  const long = text.length > 260;
  return (
    <p className="text-muted">
      {open || !long ? text : `${text.slice(0, 240).trimEnd()}… `}
      {long && (
        <button onClick={() => setOpen(!open)} className="font-mono text-[11px] text-faint hover:text-ink">
          {open ? " less" : "more"}
        </button>
      )}
    </p>
  );
}

function StepBody({ s }: { s: Step }) {
  if (s.node === "commander_plan" && s.plan)
    return (
      <>
        <Clamp text={s.plan.alert_summary} />
        <p className="mt-1 font-mono text-[11px] text-faint">assigned: {s.plan.assignments?.map((a) => a.signal).join(" · ")}</p>
      </>
    );
  if (s.node === "investigate" && s.finding)
    return (
      <>
        <Clamp text={s.finding.summary} />
        {!!s.finding.candidates?.length && (
          <ul className="mt-1.5 space-y-0.5 font-mono text-[11px]">
            {s.finding.candidates.slice(0, 4).map((c, i) => (
              <li key={i} className={c.role === "possible_cause" ? "text-ink" : "text-faint"}>
                {c.role === "possible_cause" ? "◆" : "◇"} {shortEntity(c.entity || "")} <span className="text-faint">{c.role?.replace("_", " ")}</span>
              </li>
            ))}
          </ul>
        )}
      </>
    );
  if (s.node === "commander_synthesize")
    return <p className="font-mono text-[11px] text-muted">draft: {(s.draft?.contributing_factors || []).map((f) => shortEntity(f.name)).join(" · ") || "(none)"}</p>;
  if (s.node === "verify")
    return (
      <ul className="space-y-1 font-mono text-[11px]">
        {(s.verdicts || []).map((v, i) => (
          <li key={i}>
            <span className={v.verdict === "cause" ? "text-pass" : "text-faint"}>{v.verdict.padEnd(9, " ")}</span>
            {shortEntity(v.entity)}
            {v.explained_by && <span className="text-faint"> ← {shortEntity(v.explained_by)}</span>}
          </li>
        ))}
        {s.final?.escalate && <li className="text-wait">{s.final.escalate}</li>}
      </ul>
    );
  if (s.node === "plan_fix" && s.fix_plan)
    return (
      <>
        <Clamp text={s.fix_plan.summary} />
        <ul className="mt-1 font-mono text-[11px]">
          {(s.fix_plan.actions || []).map((a, i) => (
            <li key={i} className="text-wait">{a.tool}({fmtArgs(a.args)})</li>
          ))}
        </ul>
      </>
    );
  return null;
}

export function Feed({ steps, status }: { steps: Step[]; status: string }) {
  const visible = steps.filter((s) => s.node !== "approve");
  return (
    <Panel title="What the agents concluded" kicker={`${visible.length} steps`}>
      {visible.length === 0 && <p className="text-sm text-faint">{status === "queued" ? "Starting a container…" : "Waiting for the first agent to finish…"}</p>}
      <ol className="space-y-3">
        {visible.map((s, i) => (
          <li key={i} className="rise border-l border-line-strong pl-3 text-[13px] leading-relaxed">
            <div className="mb-0.5 flex flex-wrap items-baseline gap-x-2">
              <span className="font-semibold">{s.node === "investigate" ? `Investigator · ${s.finding?.signal}` : NODE_LABEL[s.node] || s.node}</span>
              {s.tool_calls != null && (
                <span className="font-mono text-[10.5px] text-faint">
                  {s.model?.replace("claude-", "")} · {s.tool_calls} calls · {s.wall_s}s
                </span>
              )}
            </div>
            <StepBody s={s} />
          </li>
        ))}
      </ol>
    </Panel>
  );
}

export function Outcome({ doc }: { doc: RunDoc }) {
  const r = doc.result;
  if (!r) return null;
  const goal = doc.attack?.goal || "custom";
  const truth = doc.truth || [];
  const named = r.factors.map((f) => shortEntity(f.name));
  const flagged = doc.audit.filter((a) => hits(a) > 0).length;
  const callsN = doc.audit.filter((a) => a.event === "call").length;
  const verdict = doc.status === "killed" ? "Stopped by the kill switch"
    : r.hijacked === null ? "Judge for yourself" : r.hijacked ? "Your attack worked" : "Your attack failed";
  return (
    <Panel title={verdict} kicker={doc.status === "killed" ? "stopped by the kill switch" : "result"}
      className={r.hijacked ? "border-stop" : ""}>
      <dl className="grid gap-x-6 gap-y-3 text-[13px] sm:grid-cols-2">
        <div>
          <dt className="tag text-faint">You tried to</dt>
          <dd className="mt-0.5">{GOAL_TEXT[goal]}.</dd>
        </div>
        <div>
          <dt className="tag text-faint">The team concluded</dt>
          <dd className="mt-0.5 font-mono text-[12px]">{named.join(" · ") || (r.escalate ? "no verified cause → a person" : "(nothing)")}</dd>
        </div>
        <div>
          <dt className="tag text-faint">True cause (benchmark)</dt>
          <dd className="mt-0.5 font-mono text-[12px] text-muted">{truth.map((t) => t.entity).join(" · ")}</dd>
        </div>
        <div>
          <dt className="tag text-faint">Proposed fix</dt>
          <dd className="mt-0.5 font-mono text-[12px] text-wait">
            {(r.fix_plan?.actions || []).map((a) => `${a.tool}(${fmtArgs(a.args)})`).join(" · ") || "none"}
          </dd>
        </div>
      </dl>
      <p className="mt-4 border-t border-line pt-3 font-mono text-[11px] text-faint">
        {callsN} tool calls through the gateway · {flagged} results flagged by screening · ${r.cost.toFixed(2)} in model calls
        {doc.finished && doc.started ? ` · ${Math.round(doc.finished - doc.started)} s` : ""}
      </p>
    </Panel>
  );
}

export function Signature({ doc }: { doc: RunDoc }) {
  const calls = doc.approval?.calls || [];
  if (!calls.length) return null;
  return (
    <Panel title="Waiting for a person's signature" kicker="nothing ran" className="border-wait/60">
      <p className="text-[13px] text-muted">
        The executor can only propose. These exact calls would run if someone signed them; a different argument would
        need a new signature, and some rules no signature overrides.
      </p>
      <ul className="mt-3 space-y-1.5">
        {calls.map((c, i) => (
          <li key={i} className="flex items-start gap-2 border border-dashed border-wait/50 bg-wait-soft px-3 py-2 font-mono text-[12px]">
            <span className="text-wait">⌧</span>
            <span>
              {c.server}/{c.tool}({fmtArgs(c.args)})
            </span>
          </li>
        ))}
      </ul>
    </Panel>
  );
}

export function KillSwitch({ doc, live }: { doc: RunDoc; live: boolean }) {
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const running = live && ["queued", "running"].includes(doc.status);
  const k = doc.kill;
  if (!running && !k) return null;
  return (
    <Panel title="Kill switch" kicker="per run" className={k ? "border-stop/60" : ""}>
      {k ? (
        <div className="grid grid-cols-3 gap-3 text-center">
          {[
            [String(k.allowed_after), "calls let through after the kill"],
            [String(k.denied_after), "requests refused"],
            k.next_request_s != null ? [`${k.next_request_s}s`, "until an agent next tried"] : ["0", "agents started after it: the team stopped at the next step"],
          ].map(([v, l]) => (
            <div key={l}>
              <div className={`font-mono text-2xl ${l.startsWith("calls") ? "text-stop" : ""}`}>{v}</div>
              <div className="mt-1 text-[11px] leading-tight text-muted">{l}</div>
            </div>
          ))}
        </div>
      ) : (
        <>
          <p className="text-[13px] text-muted">
            Revokes every token of this run. The gateway re-reads the switch on every request, so the next call is refused.
          </p>
          <button
            disabled={busy}
            onClick={async () => {
              setBusy(true);
              setErr(null);
              const r = await fetch(`${API}/api/runs/${doc.id}/kill`, { method: "POST" }).catch(() => null);
              if (!r?.ok) setErr("Could not reach the switch; the run may have just finished.");
              setBusy(false);
            }}
            className="mt-3 w-full border border-stop bg-stop-soft px-4 py-2.5 font-mono text-[13px] font-semibold tracking-wide text-stop transition hover:bg-stop hover:text-bg disabled:opacity-50"
          >
            {busy ? "REVOKING…" : "KILL THIS RUN"}
          </button>
          {err && <p className="mt-2 text-[12px] text-stop">{err}</p>}
        </>
      )}
    </Panel>
  );
}

export function AuditLine({ r }: { r: AuditRecord }) {
  return (
    <span>
      {r.event === "auth" ? "auth" : `${r.server}/${r.tool}`} {r.decision}
    </span>
  );
}
