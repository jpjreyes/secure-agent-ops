"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { AuditRecord, RunDoc } from "./run";

const MAX_PULSES = 14;

// Replay: a recorded run on a compressed clock (whole run in ~`seconds`). Live: everything received so far, with
// new tool calls queued as pulses so a burst doesn't draw fifty at once. Key the calling component by run and mode.
export function usePlayback(doc: RunDoc | null, mode: "replay" | "live", seconds = 48) {
  const [clock, setClock] = useState(mode === "replay" ? 0 : 1); // 0..1 progress for replay
  const [playing, setPlaying] = useState(true);
  const [pulses, setPulses] = useState<AuditRecord[]>([]);
  const shown = useRef<Set<number>>(new Set());
  const queue = useRef<AuditRecord[]>([]);

  const span = useMemo(() => {
    if (!doc) return { t0: 0, t1: 1 };
    const ts = [...doc.audit.map((r) => r.ts), ...doc.steps.map((s) => s.t)].filter(Boolean);
    const t0 = doc.started ?? Math.min(...ts);
    const t1 = Math.max(...ts, t0 + 1);
    return { t0, t1 };
  }, [doc]);

  // replay clock
  useEffect(() => {
    if (mode !== "replay" || !playing) return;
    const id = setInterval(() => setClock((c) => Math.min(1, c + 0.1 / seconds)), 100);
    return () => clearInterval(id);
  }, [mode, playing, seconds]);

  const now = mode === "replay" ? span.t0 + clock * (span.t1 - span.t0) : Infinity;
  const records = useMemo(() => (doc ? doc.audit.filter((r) => r.ts <= now) : []), [doc, now]);
  const steps = useMemo(() => (doc ? doc.steps.filter((s) => s.t <= now) : []), [doc, now]);

  // enqueue tool calls not yet animated
  useEffect(() => {
    for (const r of records) {
      if (shown.current.has(r.seq)) continue;
      shown.current.add(r.seq);
      if (r.event === "call" || (r.event === "auth" && r.decision === "deny")) queue.current.push(r);
    }
  }, [records]);

  // drain the queue at a readable rate; denials jump the queue
  useEffect(() => {
    const id = setInterval(() => {
      setPulses((p) => {
        if (!queue.current.length || p.length >= MAX_PULSES) return p;
        const i = queue.current.findIndex((r) => r.decision === "deny");
        const [next] = queue.current.splice(i >= 0 ? i : 0, 1);
        return [...p, next];
      });
    }, 160);
    return () => clearInterval(id);
  }, []);

  const done = useCallback((seq: number) => setPulses((p) => p.filter((r) => r.seq !== seq)), []);
  const finished = mode === "replay" ? clock >= 1 : !!doc && !["queued", "running"].includes(doc.status);

  return {
    records, steps, pulses, done, finished, clock, playing,
    setPlaying, restart: () => { shown.current = new Set(); queue.current = []; setPulses([]); setClock(0); setPlaying(true); },
    skip: () => setClock(1),
    elapsed: Math.max(0, Math.min(now, span.t1) - span.t0),
  };
}
