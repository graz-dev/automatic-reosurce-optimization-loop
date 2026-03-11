# Autonomic Resource Optimization Loop

A closed-loop system that continuously right-sizes Kubernetes workload resources
by observing real metrics, evaluating them through an OPA policy engine, and
opening a **GitOps pull request** with the updated manifest — no manual
intervention, no direct cluster patching.

---

## How it works

| Step | Component | What happens |
|------|-----------|--------------|
| **0** | **OPA policy read** | The optimizer fetches workload coordinates (`namespace`, `deployment`, `container`, `manifest_path`) and all sizing knobs from the per-workload Rego file. The CronJob carries **no workload-specific config** beyond `WORKLOAD=petclinic`. |
| **1** | **Deployment inspection** | The Kubernetes API is queried for the current resource limits and to detect JVM workloads (by image name or env vars such as `JDK_JAVA_OPTIONS`). |
| **2** | **Prometheus metrics** | For JVM workloads: `jvm_memory_used_bytes` heap and non-heap are queried separately; total = heap + off-heap. For generic workloads: `container_memory_working_set_bytes`. CPU is P95 over the `ANALYSIS_WINDOW`. |
| **3** | **OPA decision** | The per-workload Rego endpoint (`/v1/data/resource/workloads/{name}/result`) injects the `policy_config` from the Rego file and delegates to the master sizing engine. Python sends only observational data — never policy knobs. |
| **4** | **GitHub PR** | If OPA returns `action=resize`, the optimizer creates a branch, patches the manifest with `ruamel.yaml` (comment-preserving, surgical diff), and opens a PR with a diagnostics table. |

---

## Repository layout

```
.
├── setup.sh                                 # One-shot cluster bootstrap (./setup.sh | --teardown)
├── app/
│   ├── namespace.yaml
│   ├── petclinic.yaml                       # OTel-instrumented Spring PetClinic
│   └── load-test/
│       ├── k6-diurnal.js                    # Sine-wave 10-min diurnal load test
│       └── k6-job.yaml
├── infra/
│   ├── kind/kind-config.yaml                # 3-node Kind cluster (pinned v1.32.0)
│   ├── monitoring/
│   │   ├── kube-prometheus-values.yaml      # Prometheus + Grafana Helm values
│   │   └── otel-collector.yaml             # OTLP receiver → Prometheus exporter
│   └── opa/
│       └── opa-deployment.yaml              # OPA REST server + initContainer fix
└── optimizer/
    ├── optimizer.py                         # Python orchestrator (Steps 0–4)
    ├── policy/
    │   ├── resources.rego                   # Master sizing engine  *** DO NOT EDIT ***
    │   └── workloads/
    │       └── petclinic.rego               # Per-workload knobs (SRE-managed)
    └── k8s/
        ├── rbac.yaml                        # ServiceAccount + ClusterRole
        └── cronjob.yaml                     # CronJob every 30 min
```

---

## Prerequisites

| Tool | Version tested |
|------|----------------|
| Docker | ≥ 24 |
| Kind | ≥ 0.23 |
| kubectl | ≥ 1.29 |
| Helm | ≥ 3.14 |

```bash
brew install kind kubectl helm
```

---

## Quick start

```bash
git clone https://github.com/graz-dev/automatic-reosurce-optimization-loop.git
cd automatic-reosurce-optimization-loop

# (Optional) GitHub PR support — create a PAT with repo scope
echo "ghp_yourtoken" > gh-token.key   # ignored by .gitignore

# Bootstrap everything
chmod +x setup.sh && ./setup.sh
```

`setup.sh` does in order:

1. Starts a local Docker registry on `localhost:5001`
2. Creates a 3-node Kind cluster (`resource-optimizer`) with node role labels
3. Creates `optimizer-github` Secret from `gh-token.key` (if present)
4. Installs `kube-prometheus-stack` via Helm into `monitoring`
5. Deploys the OpenTelemetry Collector
6. Deploys OPA; builds the `opa-policies` ConfigMap from all `*.rego` files
7. Deploys Spring PetClinic into `microservices-demo`
8. Launches the K6 diurnal load test Job
9. Deploys the optimizer RBAC, ConfigMap, and CronJob

### Tear down

```bash
./setup.sh --teardown
```

### Access the UIs

```bash
# Prometheus  →  http://localhost:9090
kubectl port-forward -n monitoring svc/kube-prometheus-stack-prometheus 9090:9090

# Grafana  →  http://localhost:3000  (admin / admin)
kubectl port-forward -n monitoring svc/kube-prometheus-stack-grafana 3000:80
```

### Trigger the optimizer manually

```bash
kubectl create job -n monitoring --from=cronjob/resource-optimizer optimizer-manual-1

# Follow logs
kubectl logs -n monitoring -l job-name=optimizer-manual-1 -f
```

---

## Implementation details

### OPA policy architecture — two-layer design

The policy is split into two layers so that **the platform team owns the
engine** and **each application team owns its knobs**.

```
optimizer/policy/
├── resources.rego          # Master engine — platform team, never edited by SREs
└── workloads/
    └── petclinic.rego      # Workload policy — application/SRE team
```

**`resources.rego` — master sizing engine**

Implements the full sizing algorithm. Receives observational input from Python
and `policy_config` injected by the workload Rego via `with`:

```
headroom factor  = 1 + headroom_pct / 100
rec_cpu_m        = round(cpu_cores × 1000 × headroom)
rec_mem_mib      = round(total_mem_mb × headroom)
safe_*           = clamp(rec_*, min, max)          # safety bounds
max_heap_mib     = min(safe_mem, heap_mb × headroom)  # JVM only
action = "resize" when cpu_delta_pct > delta_threshold_pct
                    OR mem_delta_pct > delta_threshold_pct
```

**`workloads/petclinic.rego` — per-workload policy**

The only file an SRE touches to onboard or tune a workload:

```rego
policy_config = {
    "target": {
        "namespace":     "microservices-demo",
        "deployment":    "petclinic",
        "container":     "petclinic",
        "manifest_path": "app/petclinic.yaml",
    },
    "headroom_pct":        10,   # +10 % above P95 observed usage
    "delta_threshold_pct": 10,   # ignore changes < 10 %
    "cpu_min_m":   100,  "cpu_max_m":   8000,
    "mem_min_mib": 128,  "mem_max_mib": 8192,
    "requests_cpu_ratio": 0.5,   # request = 50 % of limit
    "requests_mem_ratio": 0.7,
}

result = r {
    r = data.resource.optimizer.result with input.policy_config as policy_config
}
```

The Rego `with` keyword injects `policy_config` at evaluation time without
modifying the master rule, keeping the engine and the knobs strictly separated.

### JVM-aware sizing

For JVM workloads the optimizer fetches **real heap and non-heap metrics** from
Prometheus instead of applying a hardcoded overhead factor:

```
Container limit  = (heap_observed + offheap_observed) × headroom
-Xmx             = heap_observed × headroom
```

Prometheus metrics sourced from the OpenTelemetry Java agent:

| Metric | Label filter | Meaning |
|--------|-------------|---------|
| `petclinic_jvm_memory_used_bytes` | `jvm_memory_type="heap"` | Current heap usage |
| `petclinic_jvm_memory_used_bytes` | `jvm_memory_type="non_heap"` | Metaspace + code cache |

The metric name prefix (`petclinic_`) comes from the OTel Collector's Prometheus
exporter `namespace` setting, which matches the container name by convention.

### Surgical PR diffs with ruamel.yaml

The optimizer patches manifests using **`ruamel.yaml`** instead of
`PyYAML`, which preserves:

- YAML comments
- Original indentation and quoting style
- Key order and blank lines

The resulting PR diff contains only the lines that actually change
(resources, `-Xmx`). Example from a real run:

```diff
-              value: "-Xmx512m"
+              value: "-Xmx135m"
           resources:
             requests:
-              cpu: 500m
-              memory: 1Gi
+              cpu: 50m
+              memory: 227Mi
             limits:
-              cpu: 2000m
-              memory: 2Gi
+              cpu: 629m
+              memory: 319Mi
```

### Configurable analysis window

The `ANALYSIS_WINDOW` env var controls how far back Prometheus looks when
computing P95 CPU and peak JVM memory.

| Value | Behaviour |
|-------|-----------|
| `5m`  | Reacts fast to traffic spikes; more volatile recommendations |
| `30m` | Default — balances responsiveness and stability |
| `1h`  | Stable, conservative — good for workloads with slow ramp-ups |

### CronJob — environment variables

All variables in `optimizer/k8s/cronjob.yaml`. Policy knobs live in Rego, not here.

| Variable | Default | Description |
|----------|---------|-------------|
| `WORKLOAD` | `petclinic` | Maps to `resource/workloads/<value>.rego` in OPA |
| `PROMETHEUS_URL` | `http://kube-prometheus-stack-prometheus.monitoring:9090` | Prometheus API |
| `OPA_URL` | `http://opa.monitoring:8181` | OPA REST server |
| `ANALYSIS_WINDOW` | `30m` | Look-back window for Prometheus queries |
| `GITHUB_TOKEN` | *(from Secret)* | PAT with `repo` scope |
| `GITHUB_REPO` | *(from Secret)* | `owner/repo` — auto-derived from `git remote` by `setup.sh` |
| `GITHUB_BASE_BRANCH` | `master` | Branch the PR targets |
| `DRY_RUN` | `false` | Log decisions without opening a PR |

---

## Onboarding a new workload

No changes to the CronJob, Python code, or master Rego are needed.
The only step is creating a new per-workload Rego file:

```bash
cp optimizer/policy/workloads/petclinic.rego \
   optimizer/policy/workloads/myapp.rego
```

Edit the new file — change `package`, `target`, and sizing knobs:

```rego
package resource.workloads.myapp

policy_config = {
    "target": {
        "namespace":     "my-team-ns",
        "deployment":    "myapp",
        "container":     "myapp",
        "manifest_path": "app/myapp.yaml",
    },
    "headroom_pct":        15,
    "delta_threshold_pct": 5,
    ...
}

result = r {
    r = data.resource.optimizer.result with input.policy_config as policy_config
}
```

Then rebuild the OPA ConfigMap (or re-run `setup.sh`):

```bash
kubectl create configmap opa-policies \
  --from-file=resources.rego=optimizer/policy/resources.rego \
  --from-file=myapp.rego=optimizer/policy/workloads/myapp.rego \
  --namespace monitoring --dry-run=client -o yaml | kubectl apply -f -

kubectl rollout restart deployment/opa -n monitoring
```

Launch the optimizer for the new workload:

```bash
kubectl create job -n monitoring --from=cronjob/resource-optimizer myapp-run \
  --overrides='{"spec":{"template":{"spec":{"containers":[{"name":"optimizer","env":[{"name":"WORKLOAD","value":"myapp"}]}]}}}}'
```

---

## Vision — the optimizer as a Platform Engineering component

### The problem at scale

In a typical organisation running dozens of microservices across multiple teams,
Kubernetes resource allocation is decided once at initial deployment and rarely
revisited. The result is a predictable pattern: **overprovisioning for safety**
— services that consume 200 m CPU and 256 MiB are allocated 2 CPU and 2 Gi
because nobody wants an OOM kill at 3 AM. Cluster utilisation rates of 10–20 %
are common.

The root cause is not negligence; it is **missing tooling and missing
incentives**. Application teams don't have easy visibility into actual usage,
and the cost of getting it wrong is asymmetric (too little → incident, too much
→ waste that nobody notices).

An autonomic optimization loop solves this at the platform level, removing the
burden from individual teams while preserving their autonomy.

### The target architecture

A single shared optimizer infrastructure (Prometheus, OPA, CronJobs) serves
multiple application teams. Each team owns only its Rego policy file — the
rest is invisible to them.

### Separation of concerns

| Persona | Owns | Does NOT touch |
|---------|------|----------------|
| **Platform team** | `resources.rego` (master engine), OPA deployment, CronJob template, Prometheus | Per-workload Rego files, application manifests |
| **SRE / App team** | `workloads/<name>.rego` — sizing knobs and target coordinates | Python code, master Rego, infrastructure |
| **Developer** | Reviews and merges the optimizer PR — same as any code change | All of the above |

This maps naturally to a GitOps workflow: the platform team manages the
engine via their own repo; application teams manage their Rego in their own
repo (or a shared `platform-policies` repo with CODEOWNERS per workload).

### GitOps and human-in-the-loop

The optimizer intentionally **does not patch the cluster directly**. Every
sizing recommendation becomes a pull request:

- Developers see the change in the same review tool they use for code
- The diff is minimal and readable (5 lines, not 90)
- CI can run validation (e.g. `kubeval`, `conftest`) before merge
- Rejected PRs create a natural audit trail
- A human can override any recommendation by closing the PR and adjusting
  the Rego knobs

For teams that want full automation, merging can be delegated to a bot
(Renovate, Mergify) once the PR passes CI — the platform team controls the
auto-merge policy centrally.

### Policy as code — governance at scale

OPA Rego policies are the governance layer. The platform team can encode
organisation-wide constraints in the master `resources.rego` that no workload
policy can override:

```rego
# Example platform-level guard in resources.rego
# No workload can request more than 50 % of a node's allocatable CPU
deny[msg] {
    safe_cpu_m > 4000
    not input.policy_config.oversize_approved
    msg := "CPU limit exceeds 4000m — set oversize_approved=true in policy_config"
}
```

Per-workload Rego files can only tune knobs within the bounds the platform
allows. This is the key difference from traditional admission controllers:
the constraints are **declared alongside the workload** and version-controlled
with it, rather than being opaque cluster-wide webhook rules.

### Multi-cluster and multi-environment

The same architecture extends naturally to multiple clusters:

- **One Prometheus per cluster** (already standard with kube-prometheus-stack)
- **One OPA per cluster** or a centralised policy server shared across clusters
- **One optimizer CronJob per workload per cluster**, with `ANALYSIS_WINDOW`
  tuned per environment (e.g. `4h` in production, `15m` in staging)
- PRs can target different branches (`main` for production, `staging` for
  staging), with environment-specific Rego knobs

### Feedback loops and continuous improvement

Once the optimizer is running across a fleet, it produces a continuous stream
of structured signals:

```
PR title: chore(optimizer): resize payments-service (2026-03-11T17:20)
CPU: 2000m → 629m  (−68 %)
MEM: 2Gi → 319Mi   (−85 %)
```

These signals can feed:
- **Cost reporting**: aggregate all merged optimizer PRs to compute savings
- **Anomaly detection**: a workload that suddenly needs a large upsize is a
  signal worth alerting on, even before it causes an OOM
- **Capacity planning**: trend the recommended values over weeks to predict
  when a service will hit its class-of-service ceiling
- **Policy tuning**: if a workload triggers a resize every run, tighten
  `delta_threshold_pct`; if it never triggers, `headroom_pct` may be too high