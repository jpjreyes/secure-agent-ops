"use client";

import { useCallback, useEffect, useState } from "react";
import { AttackConsole } from "@/components/attack-console";
import { AuditTamper } from "@/components/audit-tamper";
import { PushThrough } from "@/components/push-through";
import { Feed, KillSwitch, Outcome, Signature } from "@/components/run-panels";
import { Schematic } from "@/components/schematic";
import { usePlayback } from "@/lib/playback";
import { API, type RunDoc } from "@/lib/run";

const REPO = "https://github.com/jpreyesm03/secure-agent-ops";
const REPLAYS = [
  { id: "action", label: "Hijack the fix" },
  { id: "diagnosis", label: "Plant a false root cause" },
  { id: "exfil", label: "Steal the system prompt" },
];

export default function Home() {
  const [replayId, setReplayId] = useState("action");
  const [replay, setReplay] = useState<RunDoc | null>(null);
  const [liveId, setLiveId] = useState<string | null>(null);
  const [live, setLive] = useState<RunDoc | null>(null);

  useEffect(() => {
    const id = new URLSearchParams(window.location.search).get("run");
    // eslint-disable-next-line react-hooks/set-state-in-effect -- the URL is only readable after hydration
    if (id) setLiveId(id);
  }, []);

  useEffect(() => {
    fetch(`/replay-${replayId}.json`).then((r) => r.json()).then(setReplay).catch(() => setReplay(null));
  }, [replayId]);

  // poll the live run
  useEffect(() => {
    if (!liveId) return;
    let stop = false;
    const tick = async () => {
      const r = await fetch(`${API}/api/runs/${liveId}`, { cache: "no-store" }).catch(() => null);
      if (r?.ok) {
        const d: RunDoc = await r.json();
        if (!stop) setLive(d);
        if (["done", "killed", "error"].includes(d.status)) return;
      }
      if (!stop) setTimeout(tick, 2000);
    };
    tick();
    return () => { stop = true; };
  }, [liveId]);

  const started = useCallback((id: string) => {
    setLive(null);
    setLiveId(id);
    window.history.replaceState(null, "", `?run=${id}`);
    document.getElementById("board")?.scrollIntoView({ behavior: "smooth" });
  }, []);

  const mode: "live" | "replay" = liveId ? "live" : "replay";
  const doc = mode === "live" ? live : replay;
  const running = mode === "live" && (!live || ["queued", "running"].includes(live.status));

  return (
    <div className="mx-auto max-w-[1180px] px-4 pb-24 sm:px-6">
      <header className="flex items-center justify-between gap-4 border-b border-line py-4">
        <a href="#" className="font-mono text-[13px] font-semibold tracking-[0.18em]">SECURE AGENT OPS</a>
        <nav className="flex gap-4 font-mono text-[12px] text-muted sm:gap-6">
          <a href="#board" className="hover:text-ink">demo</a>
          <a href="#layers" className="hover:text-ink">layers</a>
          <a href="#results" className="hover:text-ink">results</a>
          <a href={REPO} className="hover:text-ink">github ↗</a>
        </nav>
      </header>

      <section className="pb-10 pt-12 sm:pt-16">
        <p className="tag text-stop">Incident response · AI agents · security gateway</p>
        <h1 className="mt-3 max-w-3xl text-[44px] font-semibold leading-[1.02] tracking-[-0.03em] sm:text-[64px]">Try to break it.</h1>
        <p className="mt-5 max-w-2xl text-[17px] leading-relaxed text-muted">
          A team of AI agents investigates a real Kubernetes outage and proposes a fix. You get to write into the logs
          they read. Every tool call goes through a security gateway; watch which layer stops your attack.
        </p>
      </section>

      <section className="ticks border border-line bg-panel/70 p-4 sm:p-6">
        <AttackConsole onStarted={started} disabled={running && !!liveId} />
      </section>

      <section id="board" className="scroll-mt-6 pt-10">
        <Board key={`${mode}-${doc?.id ?? "none"}`} doc={doc} mode={mode} running={running} replayId={replayId}
          setReplayId={setReplayId} back={() => { setLiveId(null); setLive(null); window.history.replaceState(null, "", "/"); }} />
      </section>

      <Layers />
      <Results />

      <footer className="mt-20 flex flex-wrap justify-between gap-3 border-t border-line pt-5 font-mono text-[11.5px] text-faint">
        <span>Juan Pablo Jose Reyes Martinez · Secure Agent Ops</span>
        <span>
          Claude (Agent SDK) · LangGraph · MCP · Cedar · AWS Lambda, KMS, DynamoDB, S3 Object Lock ·{" "}
          <a href={REPO} className="underline-offset-2 hover:text-ink hover:underline">source</a>
        </span>
      </footer>
    </div>
  );
}

function Board({ doc, mode, running, replayId, setReplayId, back }: {
  doc: RunDoc | null;
  mode: "live" | "replay";
  running: boolean;
  replayId: string;
  setReplayId: (id: string) => void;
  back: () => void;
}) {
  const pb = usePlayback(doc, mode);
  // panels show the run's end state only once the replay has caught up
  const shownDoc = doc && (mode === "live" || pb.finished) ? doc : null;
  return (
    <>
        <div className="mb-3 flex flex-wrap items-center gap-x-4 gap-y-2">
          <span className={`tag border px-2 py-0.5 ${mode === "live" ? "border-stop text-stop" : "border-line-strong text-muted"}`}>
            {mode === "live" ? (running ? <span className="blink">● live</span> : "live run") : "recorded run"}
          </span>
          {mode === "replay" ? (
            <div className="flex flex-wrap gap-1.5">
              {REPLAYS.map((r) => (
                <button key={r.id} onClick={() => setReplayId(r.id)}
                  className={`px-2 py-0.5 font-mono text-[11.5px] ${replayId === r.id ? "bg-panel-2 text-ink" : "text-faint hover:text-ink"}`}>
                  {r.label}
                </button>
              ))}
            </div>
          ) : (
            <button onClick={back}
              className="font-mono text-[11.5px] text-faint hover:text-ink">← back to the recorded runs</button>
          )}
          <span className="ml-auto font-mono text-[11.5px] text-faint tabular">
            {doc?.incident_info?.title ?? ""} {doc ? `· ${Math.round(pb.elapsed)} s` : ""} {mode === "live" && doc?.phase ? `· ${doc.phase}` : ""}
          </span>
          {mode === "replay" && (
            <span className="flex gap-2 font-mono text-[11.5px] text-muted">
              <button onClick={() => pb.setPlaying(!pb.playing)} className="hover:text-ink">{pb.playing ? "pause" : "play"}</button>
              <button onClick={pb.restart} className="hover:text-ink">restart</button>
              <button onClick={pb.skip} className="hover:text-ink">skip to end</button>
            </span>
          )}
        </div>

        <div className="ticks overflow-x-auto border border-line bg-panel/60 p-2 sm:p-4">
          <div className="min-w-[640px]">
            {doc ? (
              <Schematic doc={doc} records={pb.records} pulses={pb.pulses} steps={pb.steps} onPulseDone={pb.done} />
            ) : (
              <div className="grid h-80 place-items-center font-mono text-[12px] text-faint">{mode === "live" ? "Starting the run…" : "Loading the recorded run…"}</div>
            )}
          </div>
        </div>
        {doc?.attack && (
          <p className="mt-2 font-mono text-[11.5px] text-faint">
            planted: <span className="text-stop">{doc.attack.text.trim().slice(0, 160)}{doc.attack.text.length > 160 ? "…" : ""}</span>
          </p>
        )}
        {doc?.status === "error" && <p className="mt-3 text-[13px] text-stop">The run failed: {doc?.error}</p>}

        {doc && (
          <div className="mt-6 grid gap-5 lg:grid-cols-[minmax(0,1.1fr)_minmax(0,1fr)]">
            <Feed steps={pb.steps} status={doc.status} />
            <div className="grid content-start gap-5">
              <KillSwitch doc={doc} live={mode === "live"} />
              {shownDoc && <Outcome doc={shownDoc} />}
              {shownDoc && <Signature doc={shownDoc} />}
            </div>
          </div>
        )}
        {shownDoc && !running && (
          <div className="mt-5 grid gap-5 lg:grid-cols-2">
            <PushThrough doc={shownDoc} />
            <AuditTamper doc={shownDoc} />
          </div>
        )}
    </>
  );
}

const LAYERS = [
  ["Identity", "Each agent has its own key and gets 10-minute tokens (OAuth client credentials, RFC 8693 delegation). The issuer signs them with a key that never leaves AWS KMS.", "agents never share a credential"],
  ["Kill switch", "Per agent, token, tool or run. The gateway re-reads it on every request; there is no session to outlive it.", "next call refused · 15.6 ms median locally"],
  ["Cedar policy", "Rules on the actual arguments: investigators can't even see write tools, nothing scales below one replica, platform services are off limits.", "properties hold on all 221,184 distinguishable requests"],
  ["Screening", "Every telemetry result is wrapped as untrusted data; instruction-like lines are flagged before the model sees them.", "0 false flags on 7,535 real log lines"],
  ["Verifier", "A separate agent tests each suspect: cause, or a symptom of something upstream? Only causes nothing else explains survive.", "+0.09 on held-out incidents, false positives −40%"],
  ["Signature", "The executor proposes; a person signs the exact calls (server, tool and arguments, hashed). Change one argument and the signature no longer matches.", "no model ever holds a write credential"],
  ["Audit", "Every decision is a hash-chained record; the final log is copied to S3 Object Lock in compliance mode.", "edits and deletions detected at the exact line"],
  ["Tracing", "One OpenTelemetry trace per incident: the gateway's decisions nest inside each agent's span, with tokens and cost per agent.", "every call explainable after the fact"],
];

function Layers() {
  return (
    <section id="layers" className="scroll-mt-6 pt-20">
      <p className="tag text-faint">The layers, in order</p>
      <h2 className="mt-2 max-w-2xl text-[30px] font-semibold leading-tight tracking-[-0.02em]">Prompts aren&apos;t a security boundary. These are.</h2>
      <div className="mt-8 grid gap-px border border-line bg-line sm:grid-cols-2 lg:grid-cols-4">
        {LAYERS.map(([name, body, num], i) => (
          <div key={name} className="bg-bg p-5">
            <div className="flex items-baseline justify-between">
              <h3 className="text-[16px] font-semibold">{name}</h3>
              <span className="font-mono text-[11px] text-faint">{String(i + 1).padStart(2, "0")}</span>
            </div>
            <p className="mt-2 text-[13.5px] leading-relaxed text-muted">{body}</p>
            <p className="mt-3 font-mono text-[11.5px] text-pass">{num}</p>
          </div>
        ))}
      </div>
    </section>
  );
}

const RESULTS = [
  ["Log-injection attacks (AgentDojo templates × 3 goals × 10 incidents × 3 setups)", "0 / 90 hijacked · 0 / 90 prompt leaks"],
  ["Planted false evidence (fake logs and Kubernetes Events against a healthy service)", "team fooled 0 / 30 · single agent 2 / 30"],
  ["ITBench-AA held-out test (30 incidents × 5 runs, 390 runs)", "team 0.57 · single agent 0.53 · the verifier adds +0.09"],
  ["Live faults injected into a real cluster (17 episodes)", "proposed fixes safe 17 / 17 · correct 15 / 17"],
  ["This deployment vs the evaluated system (5 incidents, same scores?)", "5 / 5 identical · mean 0.70 = 0.70"],
];

function Results() {
  return (
    <section id="results" className="scroll-mt-6 pt-20">
      <p className="tag text-faint">Measured, not claimed</p>
      <h2 className="mt-2 text-[30px] font-semibold leading-tight tracking-[-0.02em]">Results</h2>
      <div className="mt-6 border-t border-line">
        {RESULTS.map(([what, res]) => (
          <div key={what} className="grid gap-1 border-b border-line py-3.5 sm:grid-cols-[minmax(0,1.3fr)_minmax(0,1fr)] sm:gap-6">
            <span className="text-[14px] text-muted">{what}</span>
            <span className="font-mono text-[13px]">{res}</span>
          </div>
        ))}
      </div>
      <p className="mt-4 max-w-3xl text-[12.5px] leading-relaxed text-faint">
        Evaluations ran through the Claude Agent SDK on a Claude plan in an isolated configuration; costs are
        API-equivalent. The team is not significantly better than one agent on accuracy (+0.04, within the noise); the
        verifier is what measurably works. Details, intervals and every decision are in the repository.
      </p>
    </section>
  );
}
