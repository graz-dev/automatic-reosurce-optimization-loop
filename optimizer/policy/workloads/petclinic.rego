package resource.workloads.petclinic

# =============================================================================
# Petclinic workload policy — SRE-managed
#
# This is the ONLY file an SRE needs to create/edit to onboard a workload.
# It defines:
#   - WHERE the workload lives (policy_config.target)
#   - HOW it should be sized   (policy_config knobs)
#
# All calculation logic lives in resource.optimizer (resources.rego).
# To onboard a new workload: copy this file, change the package declaration
# and adjust policy_config. No changes needed to the CronJob or Python code.
# =============================================================================

# ── SRE-tunable knobs ─────────────────────────────────────────────────────────
policy_config = {
    # ── Workload identity ──────────────────────────────────────────────────────
    # The optimizer reads this block first (before touching k8s or Prometheus)
    # so the CronJob needs no workload-specific env vars beyond WORKLOAD=petclinic.
    "target": {
        "namespace":     "microservices-demo",
        "deployment":    "petclinic",
        "container":     "petclinic",
        "manifest_path": "app/petclinic.yaml",  # path in the Git repo for the PR
    },

    # ── Sizing knobs ───────────────────────────────────────────────────────────
    # Percentage of overprovisioning applied on top of observed usage.
    # e.g. 10 → limits = observed_p95 × 1.10
    "headroom_pct": 10,

    # Minimum delta (%) vs current limits before a resize PR is opened.
    "delta_threshold_pct": 10,

    # Safety bounds — the optimizer will never size outside these ranges.
    "cpu_min_m":   100,
    "cpu_max_m":   8000,
    "mem_min_mib": 128,
    "mem_max_mib": 8192,

    # requests = limits × ratio (per resource type).
    "requests_cpu_ratio": 0.5,
    "requests_mem_ratio": 0.7,
}

# ── Delegate to master ────────────────────────────────────────────────────────
# Evaluates the master sizing rules with this workload's policy_config injected
# via the `with` override — the rest of `input` (metrics, current_limits,
# is_java) flows through unchanged from the HTTP request.
result = data.resource.optimizer.result with input.policy_config as policy_config
