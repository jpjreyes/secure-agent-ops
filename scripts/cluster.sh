#!/usr/bin/env bash
# Local demo environment: kind cluster on Colima + the OpenTelemetry Demo (Astronomy Shop).
#   scripts/cluster.sh up       create the cluster (if missing) and install/upgrade the demo
#   scripts/cluster.sh status   nodes, pods, memory
#   scripts/cluster.sh ui       port-forward the shop (localhost:18080) and Grafana/Jaeger via the proxy
#   scripts/cluster.sh down     delete the cluster
set -euo pipefail
cd "$(dirname "$0")/.."

# Colima only: Docker Desktop is never used for this project.
export DOCKER_HOST="unix://${HOME}/.colima/default/docker.sock"
CLUSTER=sao
CTX="kind-${CLUSTER}"
NS=otel-demo
CHART_VERSION=0.42.6   # opentelemetry-demo chart (app 3.1.0)

need() { command -v "$1" >/dev/null || { echo "missing: $1"; exit 1; }; }
need kind; need kubectl; need helm

up() {
  docker info >/dev/null 2>&1 || { echo "Colima is not running: colima start"; exit 1; }
  if ! kind get clusters 2>/dev/null | grep -qx "$CLUSTER"; then
    kind create cluster --config infra/kind/cluster.yaml --wait 120s
  fi
  helm repo add open-telemetry https://open-telemetry.github.io/opentelemetry-helm-charts >/dev/null 2>&1 || true
  helm repo update open-telemetry >/dev/null
  helm upgrade --install astronomy open-telemetry/opentelemetry-demo --version "$CHART_VERSION" \
    --kube-context "$CTX" --namespace "$NS" --create-namespace -f infra/otel-demo/values.yaml
  echo "waiting for pods (image pulls can take several minutes the first time)..."
  local start=$SECONDS total ready
  while :; do
    total=$(kubectl --context "$CTX" -n "$NS" get pods --no-headers 2>/dev/null | wc -l | tr -d ' ')
    ready=$(kubectl --context "$CTX" -n "$NS" get pods --no-headers 2>/dev/null | awk '{split($2,a,"/"); if (a[1]==a[2] && $3=="Running") n++} END {print n+0}')
    printf "\r  %s/%s pods ready (%ds)" "$ready" "$total" $((SECONDS - start))
    [[ "$total" -gt 0 && "$ready" -eq "$total" ]] && break
    (( SECONDS - start > 1500 )) && { echo; echo "timeout; see: scripts/cluster.sh status"; exit 1; }
    sleep 10
  done
  echo; echo "demo is up. scripts/cluster.sh ui → http://localhost:18080"
}

status() {
  kubectl --context "$CTX" get nodes
  kubectl --context "$CTX" -n "$NS" get pods
  docker stats --no-stream --format '{{.Name}} {{.MemUsage}}' | grep -E "${CLUSTER}|db-1" || true
}

ui() {
  echo "shop http://localhost:18080 · grafana /grafana · jaeger /jaeger/ui · flags /feature"
  kubectl --context "$CTX" -n "$NS" port-forward svc/frontend-proxy 18080:8080
}

down() { kind delete cluster --name "$CLUSTER"; }

case "${1:-}" in up) up ;; status) status ;; ui) ui ;; down) down ;; *) sed -n '2,7p' "$0"; exit 1 ;; esac
