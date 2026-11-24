#!/usr/bin/env bash
# Back up this repo to OneDrive: full git history as one bundle file + a browsable snapshot of the working tree.
# Runs after every commit (git post-commit hook) and on demand. Secrets (.env) and rebuildable folders are skipped.
# Restore:  git clone secure-agent-ops.bundle secure-agent-ops && cd secure-agent-ops && uv sync && (cd web && npm ci)
set -euo pipefail
cd "$(dirname "$0")/.."
DEST="${SAO_BACKUP_DIR:-$HOME/Library/CloudStorage/OneDrive-UW/JP/FDE_Consulting/proyectos/p1_secure_agent_ops}"
[ -d "$(dirname "$DEST")" ] || { echo "backup: OneDrive folder not found, skipped"; exit 0; }
mkdir -p "$DEST/snapshot"

git bundle create "$DEST/secure-agent-ops.bundle.tmp" --all 2>/dev/null
mv -f "$DEST/secure-agent-ops.bundle.tmp" "$DEST/secure-agent-ops.bundle"

rsync -a --delete \
  --exclude '.git/' --exclude '.venv/' --exclude 'node_modules/' --exclude '.next/' \
  --exclude '__pycache__/' --exclude 'runs/identity/' --exclude 'runs/capture/' --exclude 'data/' --exclude '.terraform/' --exclude '*.tfstate*' --exclude '.pytest_cache/' --exclude '.ruff_cache/' \
  --exclude '.env' --exclude '.env.local' --exclude 'web/.env.local' \
  ./ "$DEST/snapshot/"

cat > "$DEST/README.md" <<EOF
# P1 Secure Agent Ops: backup

The working repo lives at \`~/other/secure-agent-ops\` (outside OneDrive) and on GitHub:
https://github.com/jpjreyes/secure-agent-ops.

This folder is an automatic backup, refreshed after every commit:
- \`secure-agent-ops.bundle\`: the full git history in one file.
- \`snapshot/\`: the current files, including uncommitted work (no .venv, node_modules, build output, benchmark data (re-download with evals/ingest.py) or .env secrets).

Restore on a new computer:
    git clone secure-agent-ops.bundle secure-agent-ops
    cd secure-agent-ops && uv sync && (cd web && npm ci)
    uv run python -m evals.ingest   # the ITBench-AA data

Last backup: $(date '+%Y-%m-%d %H:%M %Z') · commit $(git rev-parse --short HEAD)
EOF
echo "backup: $DEST ($(git rev-parse --short HEAD))"
