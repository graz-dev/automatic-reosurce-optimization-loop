#!/usr/bin/env bash
# =============================================================================
# setup.sh — Bootstrap the Autonomic Resource Optimization Loop demo
#
# Creates a Kind cluster, installs Prometheus + OTel Collector + OPA, deploys
# the Spring PetClinic application, and starts the optimizer CronJob.
#
# Usage:
#   ./setup.sh           # Full setup
#   ./setup.sh --teardown # Delete the cluster and registry
# =============================================================================
set -euo pipefail

# ── Config ────────────────────────────────────────────────────────────────────
CLUSTER_NAME="resource-optimizer"
REGISTRY_NAME="local-registry"
REGISTRY_PORT="5001"
NS_APP="microservices-demo"
NS_MONITORING="monitoring"

# ── Colours ───────────────────────────────────────────────────────────────────
GREEN='\033[0;32m'; YELLOW='\033[1;33m'; RED='\033[0;31m'; NC='\033[0m'
log()  { echo -e "${GREEN}[+]${NC} $*"; }
warn() { echo -e "${YELLOW}[!]${NC} $*"; }
die()  { echo -e "${RED}[✗]${NC} $*" >&2; exit 1; }

# ── Teardown ──────────────────────────────────────────────────────────────────
if [[ "${1:-}" == "--teardown" ]]; then
  warn "Deleting Kind cluster '${CLUSTER_NAME}' and local registry…"
  kind delete cluster --name "${CLUSTER_NAME}" 2>/dev/null || true
  docker rm -f "${REGISTRY_NAME}" 2>/dev/null || true
  log "Teardown complete."
  exit 0
fi

# ── Prerequisites ─────────────────────────────────────────────────────────────
log "Checking prerequisites…"
for cmd in kind kubectl helm docker; do
  command -v "${cmd}" &>/dev/null || die "'${cmd}' not found. Please install it first."
done
log "All prerequisites found."

# ── 1. Local Docker registry ──────────────────────────────────────────────────
log "Creating local Docker registry '${REGISTRY_NAME}' on port ${REGISTRY_PORT}…"
if docker inspect "${REGISTRY_NAME}" &>/dev/null; then
  warn "Registry '${REGISTRY_NAME}' already running — skipping."
else
  docker run -d --restart=always \
    -p "127.0.0.1:${REGISTRY_PORT}:5000" \
    --name "${REGISTRY_NAME}" \
    registry:2
fi

# ── 2. Kind cluster ────────────────────────────────────────────────────────────
log "Creating Kind cluster '${CLUSTER_NAME}'…"
if kind get clusters 2>/dev/null | grep -q "^${CLUSTER_NAME}$"; then
  warn "Cluster '${CLUSTER_NAME}' already exists — skipping creation."
else
  kind create cluster --name "${CLUSTER_NAME}" --config infra/kind/kind-config.yaml
fi

# Connect local registry to Kind network (idempotent)
docker network connect kind "${REGISTRY_NAME}" 2>/dev/null || true

# Advertise the registry inside the cluster
kubectl apply -f - <<EOF
apiVersion: v1
kind: ConfigMap
metadata:
  name: local-registry-hosting
  namespace: kube-public
data:
  localRegistryHosting.v1: |
    host: "localhost:${REGISTRY_PORT}"
    help: "https://kind.sigs.k8s.io/docs/user/local-registry/"
EOF

# ── 3. Namespaces ──────────────────────────────────────────────────────────────
log "Creating namespaces…"
kubectl create namespace "${NS_APP}"        --dry-run=client -o yaml | kubectl apply -f -
kubectl create namespace "${NS_MONITORING}" --dry-run=client -o yaml | kubectl apply -f -

# ── 3a. GitHub secret for the optimizer PR ────────────────────────────────────
if [[ -f "gh-token.key" ]]; then
  log "Creating optimizer-github secret from gh-token.key…"
  GH_TOKEN=$(tr -d '[:space:]' < gh-token.key)
  GH_REPO=$(git remote get-url origin 2>/dev/null \
    | sed -E 's|.*github\.com[:/]([^/]+/[^.]+)(\.git)?.*|\1|')
  kubectl create secret generic optimizer-github \
    --from-literal=token="${GH_TOKEN}" \
    --from-literal=repo="${GH_REPO}" \
    --namespace "${NS_MONITORING}" \
    --dry-run=client -o yaml | kubectl apply -f -
  log "Secret optimizer-github created (repo=${GH_REPO})."
else
  warn "gh-token.key not found — skipping optimizer-github secret."
  warn "Create it manually before the CronJob runs:"
  warn "  kubectl create secret generic optimizer-github --from-literal=token=<PAT> --from-literal=repo=<owner/repo> -n ${NS_MONITORING}"
fi

# ── 4. kube-prometheus-stack (Helm) ────────────────────────────────────────────
log "Adding Helm repos…"
helm repo add prometheus-community https://prometheus-community.github.io/helm-charts 2>/dev/null || true
helm repo update

log "Installing kube-prometheus-stack…"
helm upgrade --install kube-prometheus-stack \
  prometheus-community/kube-prometheus-stack \
  --namespace "${NS_MONITORING}" \
  --values infra/monitoring/kube-prometheus-values.yaml \
  --wait --timeout 10m

# ── 4a. Grafana dashboard (auto-provisioned via sidecar ConfigMap) ─────────────
# Grafana's sidecar watches for ConfigMaps labelled grafana_dashboard=1 in the
# monitoring namespace and hot-loads them — no manual import required.
log "Provisioning PetClinic Grafana dashboard…"
kubectl create configmap petclinic-dashboard \
  --from-file=petclinic-dashboard.json=infra/monitoring/petclinic-dashboard.json \
  --namespace "${NS_MONITORING}" \
  --dry-run=client -o yaml | kubectl apply -f -
kubectl label configmap petclinic-dashboard grafana_dashboard=1 \
  --namespace "${NS_MONITORING}" --overwrite

# ── 5. OpenTelemetry Collector ─────────────────────────────────────────────────
log "Deploying OpenTelemetry Collector…"
kubectl apply -f infra/monitoring/otel-collector.yaml

# ── 6. OPA ────────────────────────────────────────────────────────────────────
log "Deploying Open Policy Agent…"

# Build ConfigMap args from all Rego files (master + per-workload).
# OPA loads every .rego file from /policies at startup, so just adding a new
# workload file here is enough — no changes needed to the OPA deployment.
REGO_ARGS=("--from-file=resources.rego=optimizer/policy/resources.rego")
for f in optimizer/policy/workloads/*.rego; do
  [[ -f "$f" ]] && REGO_ARGS+=("--from-file=$(basename "$f")=$f")
done

kubectl create configmap opa-policies \
  "${REGO_ARGS[@]}" \
  --namespace "${NS_MONITORING}" \
  --dry-run=client -o yaml | kubectl apply -f -

kubectl apply -f infra/opa/opa-deployment.yaml

# ── 7. Application (Spring PetClinic) ─────────────────────────────────────────
log "Deploying Spring PetClinic…"
kubectl apply -f app/namespace.yaml
kubectl apply -f app/petclinic.yaml

# ── 8. K6 load test ───────────────────────────────────────────────────────────
log "Uploading K6 diurnal test script…"
kubectl create configmap k6-script \
  --from-file=script.js=app/load-test/k6-diurnal.js \
  --namespace "${NS_APP}" \
  --dry-run=client -o yaml | kubectl apply -f -

log "Launching K6 load test Job…"
kubectl apply -f app/load-test/k6-job.yaml

# ── 9. Optimizer CronJob ──────────────────────────────────────────────────────
log "Deploying Resource Optimizer…"

# Upload the Python script as a ConfigMap
kubectl create configmap optimizer-script \
  --from-file=optimizer.py=optimizer/optimizer.py \
  --namespace "${NS_MONITORING}" \
  --dry-run=client -o yaml | kubectl apply -f -

kubectl apply -f optimizer/k8s/rbac.yaml
kubectl apply -f optimizer/k8s/cronjob.yaml

# ── Done ──────────────────────────────────────────────────────────────────────
log "Setup complete!"
echo ""
echo "  Useful commands:"
echo "  ─────────────────────────────────────────────────────────────"
echo "  kubectl get pods -n ${NS_APP}"
echo "  kubectl get pods -n ${NS_MONITORING}"
echo ""
echo "  # Prometheus UI"
echo "  kubectl port-forward -n ${NS_MONITORING} svc/kube-prometheus-stack-prometheus 9090:9090"
echo ""
echo "  # Grafana UI — no port-forward needed (NodePort via Kind)"
echo "  open http://localhost:3000   # admin / admin"
echo ""
echo "  # Watch optimizer logs"
echo "  kubectl logs -n ${NS_MONITORING} -l app=resource-optimizer --tail=50 -f"
echo "  ─────────────────────────────────────────────────────────────"
