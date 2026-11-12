"""Download ITBench-AA scenarios and store them compactly (raw is 31 GB; disk is limited).

One scenario at a time: download the raw TSVs → convert to Parquet (zstd) → check row counts → delete the raw files.
Peak extra disk ≈ one scenario (≤ 1.3 GB raw).

Layout (agents only ever see snapshots/; the answers live elsewhere so no tool can leak them):
  data/itbench-aa/snapshots/<Scenario-N>/{events,objects,logs,traces,metrics}.parquet + alerts/*.json
  data/itbench-aa/truth/<Scenario-N>.yaml

Run:  uv run python evals/ingest.py --split dev      (or --split test, --split all, --ids Scenario-3 Scenario-16)
"""

from __future__ import annotations

import argparse
import json
import shutil
import time
from pathlib import Path

import duckdb
from huggingface_hub import HfApi, hf_hub_download

ROOT = Path(__file__).resolve().parents[1]
BASE = ROOT / "data" / "itbench-aa"
RAW = BASE / "raw"
SNAP = BASE / "snapshots"
TRUTH = BASE / "truth"
REPO = "ArtificialAnalysis/ITBench-AA"

TABLES = {  # raw file → parquet name
    "k8s_events_raw.tsv": "events",
    "k8s_objects_raw.tsv": "objects",
    "otel_logs_raw.tsv": "logs",
    "otel_traces_raw.tsv": "traces",
}


def _timestamps_ok(con: duckdb.DuckDBPyConnection, dst: Path) -> bool:
    """A misparsed file shifts columns, so its timestamp column stops parsing. Require ≥95% valid timestamps."""
    cols = [c[0] for c in con.execute(f"DESCRIBE SELECT * FROM '{dst}'").fetchall()]
    ts = next((c for c in cols if c.lower() == "timestamp"), None)
    if ts is None:
        return False
    total, good = con.execute(f'SELECT count(*), count(try_cast("{ts}" AS TIMESTAMP)) FROM \'{dst}\'').fetchone()
    return total > 0 and good >= 0.95 * total


def tsv_to_parquet(con: duckdb.DuckDBPyConnection, src: str, dst: Path, many: bool = False) -> int:
    """Two formats exist: older scenarios are plain TSV, newer ones quote fields CSV-style ("" escapes).
    Try quoted first, then unquoted; typed first, then all-text. Keep the first parse whose timestamps are valid."""
    base = "delim='\\t', header=true, sample_size=200000"
    extra = ", filename=true, union_by_name=true" if many else ""
    quoted = f"{base}, quote='\"', escape='\"'"  # quoted values may span lines (no null_padding then)
    plain = f"{base}, quote='', escape='', null_padding=true"
    attempts = [quoted, quoted + ", all_varchar=true", plain, plain + ", all_varchar=true"]
    errors = []
    for o in attempts:
        try:
            con.execute(f"COPY (SELECT * FROM read_csv({src!r}, {o}{extra})) TO '{dst}' "
                        "(FORMAT parquet, COMPRESSION zstd)")
            if _timestamps_ok(con, dst):
                return con.execute(f"SELECT count(*) FROM '{dst}'").fetchone()[0]
            errors.append(f"{o}: timestamps did not parse")
        except duckdb.Error as e:
            errors.append(f"{o}: {str(e)[:120]}")
    raise RuntimeError(f"could not convert {src}:\n  " + "\n  ".join(errors))


def raw_rows(paths: list[Path]) -> int:
    n = 0
    for p in paths:
        with p.open("rb") as f:
            n += sum(1 for _ in f) - 1  # minus header
    return n


def ingest(sid: str, files: list[str], truth_yaml: str) -> tuple[int, int]:
    out = SNAP / sid
    if (out / ".done").exists():
        return 0, 0
    out.mkdir(parents=True, exist_ok=True)
    TRUTH.mkdir(parents=True, exist_ok=True)
    (TRUTH / f"{sid}.yaml").write_text(truth_yaml)

    raw_bytes = 0
    for f in files:
        hf_hub_download(REPO, f, repo_type="dataset", local_dir=RAW)
        raw_bytes += (RAW / f).stat().st_size
    d = RAW / "sre" / sid
    con = duckdb.connect()
    for name, table in TABLES.items():
        if (d / name).exists():
            n = tsv_to_parquet(con, str(d / name), out / f"{table}.parquet")
            if n != raw_rows([d / name]):
                print(f"    note: {sid}/{name}: {n} rows parsed vs {raw_rows([d / name])} lines (multi-line values)")
    metric_files = sorted((d / "metrics").glob("*.tsv"))
    if metric_files:
        tsv_to_parquet(con, str(d / "metrics" / "*.tsv"), out / "metrics.parquet", many=True)
    if (d / "alerts").exists():
        shutil.copytree(d / "alerts", out / "alerts", dirs_exist_ok=True)
    for extra in d.glob("*.json"):  # e.g. status.json, alerts_in_alerting_state_*.json
        shutil.copy(extra, out / extra.name)
    con.close()
    shutil.rmtree(d)
    (out / ".done").write_text("ok")
    return raw_bytes, sum(p.stat().st_size for p in out.rglob("*") if p.is_file())


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--split", choices=["dev", "test", "all"])
    ap.add_argument("--ids", nargs="*")
    a = ap.parse_args()
    split = json.loads((ROOT / "evals" / "split.json").read_text())
    ids = a.ids or (split["dev"] + split["test"] if a.split == "all" else split[a.split or "dev"])

    rows = {json.loads(line)["scenario_id"]: json.loads(line)
            for line in (BASE / "sre" / "data.jsonl").read_text().splitlines() if line.strip()}
    files = [s.rfilename for s in HfApi().dataset_info(REPO, files_metadata=False).siblings]
    t0, raw_tot, pq_tot = time.time(), 0, 0
    for i, sid in enumerate(ids, 1):
        mine = [f for f in files if f.startswith(f"sre/{sid}/") and not f.endswith("ground_truth.yaml")]
        r, p = ingest(sid, mine, rows[sid]["ground_truth_yaml"])
        raw_tot, pq_tot = raw_tot + r, pq_tot + p
        el = time.time() - t0
        eta = el / i * (len(ids) - i)
        print(f"[{i}/{len(ids)}] {sid}: raw {r / 1e6:,.0f} MB -> {p / 1e6:,.0f} MB"
              f"  (elapsed {el / 60:.1f} min, ETA {eta / 60:.1f} min)", flush=True)
    print(f"done: raw {raw_tot / 1e9:.2f} GB stored as {pq_tot / 1e9:.2f} GB")


if __name__ == "__main__":
    main()
