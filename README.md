# Autonomic Resource Optimization Loop

A closed-loop system that continuously right-sizes Kubernetes workload resources
by observing real metrics, evaluating them through an OPA policy engine, and
patching the target Deployment — all without manual intervention.

```
┌──────────────┐  OTLP/HTTP  ┌─────────────────┐  scrape  ┌────────────┐
│  PetClinic   │────────────▶│  OTel Collector  │─────────▶│ Prometheus │
│  (JVM app)   │             │  (monitoring ns) │          └─────┬──────┘
└──────────────┘             └─────────────────┘                │ query
                                                                 ▼
                                                     ┌───────────────────┐
                                                     │   optimizer.py    │
                                                     │   (CronJob/5min)  │
                                                     └────────┬──────────┘
                                                              │ POST /v1/data
                                                              ▼
                                                     ┌────────────────┐
                                                     │      OPA       │
                                                     │ resources.rego │
                                                     └────────┬───────┘
                                                              │ decision
                                                              ▼
                                                   kubectl patch deployment
```

## How it works

| Step | Component | What happens |
|------|-----------|--------------|
| 1 | **PetClinic + OTel agent** | The Java application exports JVM and HTTP metrics to the OTel Collector via OTLP/HTTP every 5 s |
| 2 | **OTel Collector** | Receives OTLP metrics and re-exposes them as a Prometheus scrape endpoint on port 8889 |
| 3 | **Prometheus** | Scrapes the OTel Collector every 15 s; stores CPU and memory time-series |
| 4 | **optimizer.py** | Every 5 minutes queries Prometheus for P95 CPU cores and P95 memory (MiB) |
| 5 | **Workload detection** | Inspects the Deployment's image name and env vars to determine whether the workload is a JVM application (adds 15 % JVM off-heap overhead) |
| 6 | **OPA policy** | Receives `{metrics, current_limits, is_java, policy_config}` and returns recommended limits with headroom. A resize is only triggered when the delta exceeds 10 % |
| 7 | **kubectl patch** | If OPA returns `action=resize`, the optimizer patches the Deployment in-place |

## Repository layout

```
.
├── setup.sh                          # One-shot cluster bootstrap
├── infra/
│   ├── kind/kind-config.yaml         # 3-node Kind cluster definition
│   ├── monitoring/
│   │   ├── kube-prometheus-values.yaml  # Prometheus + Grafana Helm values
│   │   └── otel-collector.yaml          # OTel Collector Deployment + Service
│   └── opa/
│       └── opa-deployment.yaml          # OPA server Deployment + Service
├── app/
│   ├── namespace.yaml                # microservices-demo namespace
│   ├── petclinic.yaml                # OTel-instrumented Spring PetClinic
│   ├── petclinic-hpa.yaml            # HPA (replica scaling, CPU target 50 %)
│   └── load-test/
│       ├── k6-diurnal.js             # K6 diurnal load test (sine-wave shape)
│       └── k6-job.yaml               # Kubernetes Job that runs the test
└── optimizer/
    ├── optimizer.py                  # Python optimizer bot
    ├── policy/
    │   └── resources.rego            # OPA Rego policy
    └── k8s/
        ├── rbac.yaml                 # ServiceAccount + ClusterRole
        └── cronjob.yaml              # CronJob (every 5 min)
```

## Prerequisites

| Tool | Version tested | Install |
|------|----------------|---------|
| Docker | ≥ 24 | https://docs.docker.com/get-docker/ |
| Kind | ≥ 0.23 | `brew install kind` |
| kubectl | ≥ 1.29 | `brew install kubectl` |
| Helm | ≥ 3.14 | `brew install helm` |

## Quick start

```bash
# Clone the repo
git clone https://github.com/graz-dev/automatic-resource-optimization-loop.git
cd automatic-resource-optimization-loop

# Bootstrap everything (cluster + infra + app + optimizer)
chmod +x setup.sh
./setup.sh
```

`setup.sh` performs the following steps automatically:

1. Creates a local Docker registry on `localhost:5001`
2. Creates a 3-node Kind cluster (`resource-optimizer`) with node role labels
3. Installs `kube-prometheus-stack` via Helm into the `monitoring` namespace
4. Deploys the OpenTelemetry Collector
5. Deploys OPA and uploads the Rego policy from `optimizer/policy/resources.rego`
6. Deploys Spring PetClinic with OTel instrumentation
7. Creates the K6 ConfigMap and launches the diurnal load test Job
8. Deploys the optimizer RBAC and CronJob

### Access the UIs

```bash
# Prometheus (http://localhost:9090)
kubectl port-forward -n monitoring svc/kube-prometheus-stack-prometheus 9090:9090

# Grafana (http://localhost:3000 — admin / admin)
kubectl port-forward -n monitoring svc/kube-prometheus-stack-grafana 3000:80
```

### Watch the optimizer in action

```bash
# Tail live optimizer logs (runs every 5 minutes)
kubectl logs -n monitoring -l app=resource-optimizer --tail=80 -f

# Observe resource limit changes on the Deployment
kubectl get deployment petclinic -n microservices-demo -o \
  jsonpath='{.spec.template.spec.containers[0].resources}' | jq .
```

### Tear down

```bash
./setup.sh --teardown
```

---

## Component details

### Load test — K6 diurnal pattern

`app/load-test/k6-diurnal.js` compresses a 24-hour traffic cycle into
**10 minutes** (configurable via `CYCLE_MINUTES`).

Traffic shape is defined by anchors that represent real-world hours:

| Real hour | Load |
|-----------|------|
| 00:00–06:00 | 5 % (night trough) |
| 06:00–09:00 | Ramp up |
| 09:00–12:00 | 100 % (morning peak) |
| 12:00–14:00 | 60 % (midday dip) |
| 14:00–17:00 | 100 % (afternoon peak) |
| 17:00–20:00 | Ramp down |
| 20:00–24:00 | 5 % (night trough) |

A **sine-wave interpolation** is applied between every pair of anchors so that
transitions are smooth and the resulting CPU/memory oscillations look realistic
rather than step-shaped.

### OPA policy — `optimizer/policy/resources.rego`

The policy receives:

```json
{
  "input": {
    "metrics":        { "cpu_cores": 0.45, "mem_mb": 480 },
    "current_limits": { "cpu": "2000m", "mem": "2Gi" },
    "is_java":        true,
    "policy_config":  { "headroom_multiplier": 1.2 }
  }
}
```

And returns:

```json
{
  "result": {
    "action": "resize",
    "new_limits":   { "cpu": "621m",  "memory": "663Mi" },
    "new_requests": { "cpu": "310m",  "memory": "464Mi" },
    "diagnostics":  { "cpu_delta_pct": 69, "mem_delta_pct": 68, ... }
  }
}
```

Key policy rules:
- **Java overhead**: adds 15 % to recommended memory to account for JVM off-heap
- **Headroom multiplier**: applied on top of P95 observed usage (default 1.2×)
- **Noise filter**: resize is only triggered when CPU or memory delta exceeds 10 %
- **Safety clamp**: CPU capped to [100m, 8000m], memory to [128Mi, 16384Mi]
- **Requests**: set to 50 % of CPU limit and 70 % of memory limit

### Optimizer bot — `optimizer/optimizer.py`

Environment variables (all optional, with sensible defaults):

| Variable | Default | Description |
|----------|---------|-------------|
| `PROMETHEUS_URL` | `http://kube-prometheus-stack-prometheus.monitoring:9090` | Prometheus API base URL |
| `OPA_URL` | `http://opa.monitoring:8181` | OPA REST server URL |
| `TARGET_NAMESPACE` | `microservices-demo` | Namespace of the Deployment |
| `TARGET_DEPLOYMENT` | `petclinic` | Name of the Deployment |
| `CONTAINER_NAME` | `petclinic` | Container name inside the pod |
| `HEADROOM_MULTIPLIER` | `1.2` | Safety headroom factor passed to OPA |
| `DRY_RUN` | `false` | Log decisions without applying patches |

Set `DRY_RUN=true` in `optimizer/k8s/cronjob.yaml` to observe the loop without
modifying live resources.
