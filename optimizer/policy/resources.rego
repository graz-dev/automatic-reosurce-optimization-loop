package resource.optimizer

# =============================================================================
# Autonomic Resource Optimizer — Master Policy  *** DO NOT EDIT ***
#
# This package contains the sizing engine shared by all workloads.
# SREs do not modify this file. To tune a workload, create (or edit) a
# per-workload policy under resource/workloads/<name>.rego — that file
# defines policy_config and delegates here via:
#
#   result = data.resource.optimizer.result with input.policy_config as policy_config
#
# Input schema (sent by the Python optimizer, NO policy_config needed):
#   {
#     "metrics": {
#       "cpu_cores":    <float>,
#       "total_mem_mb": <float>,   # heap+offheap (JVM) or working_set (generic)
#       "heap_mb":      <float>    # present only when is_java = true
#     },
#     "current_limits": { "cpu": "1000m", "mem": "2Gi" },
#     "is_java": <bool>
#   }
#
# policy_config is injected by the workload policy via `with`, not by Python:
#   {
#     "headroom_pct":        10,   # % overprovisioning above observed usage
#     "delta_threshold_pct": 10,   # min delta % to trigger a resize
#     "cpu_min_m":           100,
#     "cpu_max_m":           8000,
#     "mem_min_mib":         128,
#     "mem_max_mib":         16384,
#     "requests_cpu_ratio":  0.5,
#     "requests_mem_ratio":  0.7
#   }
#
# Output (result):
#   {
#     "action":       "resize" | "no_change",
#     "new_limits":   { "cpu": "...", "memory": "..." },
#     "new_requests": { "cpu": "...", "memory": "..." },
#     "max_heap_mib": <int>,   # -Xmx value for JVM workloads (0 for generic)
#     "diagnostics":  { ... }
#   }
# =============================================================================

default action       = "no_change"
default max_heap_mib = 0

# ── Parsing helpers ───────────────────────────────────────────────────────────

# CPU string → millicores  ("500m" → 500, "2" → 2000)
cpu_millis(s) = v {
    endswith(s, "m")
    v = to_number(trim_suffix(s, "m"))
}
cpu_millis(s) = v {
    not endswith(s, "m")
    v = to_number(s) * 1000
}

# Memory string → MiB  ("2Gi" → 2048, "512Mi" → 512, raw bytes → MiB)
mem_mib(s) = v {
    endswith(s, "Gi")
    v = to_number(trim_suffix(s, "Gi")) * 1024
}
mem_mib(s) = v {
    endswith(s, "Mi")
    not endswith(s, "Gi")
    v = to_number(trim_suffix(s, "Mi"))
}
mem_mib(s) = v {
    not endswith(s, "Gi")
    not endswith(s, "Mi")
    v = to_number(s) / 1048576
}

# ── Headroom factor ───────────────────────────────────────────────────────────
# The single SRE-tunable knob: observed × (1 + pct/100)
headroom = 1 + (input.policy_config.headroom_pct / 100)

# ── Current limits (parsed) ───────────────────────────────────────────────────
current_cpu_m   = cpu_millis(input.current_limits.cpu)
current_mem_mib = mem_mib(input.current_limits.mem)

# ── Recommended resources ─────────────────────────────────────────────────────
rec_cpu_m   = round(input.metrics.cpu_cores    * 1000 * headroom)
rec_mem_mib = round(input.metrics.total_mem_mb        * headroom)

# ── Safety clamp ──────────────────────────────────────────────────────────────
safe_cpu_m   = min([input.policy_config.cpu_max_m,   max([input.policy_config.cpu_min_m,   rec_cpu_m])])
safe_mem_mib = min([input.policy_config.mem_max_mib, max([input.policy_config.mem_min_mib, rec_mem_mib])])

# ── Requests ──────────────────────────────────────────────────────────────────
safe_cpu_req_m   = max([50, round(safe_cpu_m   * input.policy_config.requests_cpu_ratio)])
safe_mem_req_mib = max([64, round(safe_mem_mib * input.policy_config.requests_mem_ratio)])

# ── Change detection ──────────────────────────────────────────────────────────
# Resize only when the delta exceeds the configured threshold — avoids
# noisy continuous patching for minor fluctuations.
cpu_delta_pct = abs(safe_cpu_m   - current_cpu_m)   / current_cpu_m   * 100 { current_cpu_m   > 0 }
cpu_delta_pct = 0                                                             { current_cpu_m   = 0 }

mem_delta_pct = abs(safe_mem_mib - current_mem_mib) / current_mem_mib * 100 { current_mem_mib > 0 }
mem_delta_pct = 0                                                             { current_mem_mib = 0 }

action = "resize" { cpu_delta_pct > input.policy_config.delta_threshold_pct }
action = "resize" { mem_delta_pct > input.policy_config.delta_threshold_pct }

# ── JVM heap sizing ───────────────────────────────────────────────────────────
# max_heap_mib is the -Xmx value: observed heap × headroom, never exceeding
# the container memory limit.
# Proof: container_limit = (heap + offheap) × headroom ≥ heap × headroom = max_heap ✓
max_heap_mib = min([safe_mem_mib, round(input.metrics.heap_mb * headroom)]) {
    input.is_java = true
}

# ── Output document ───────────────────────────────────────────────────────────
result = {
    "action":       action,
    "new_limits": {
        "cpu":    sprintf("%vm",  [safe_cpu_m]),
        "memory": sprintf("%vMi", [safe_mem_mib]),
    },
    "new_requests": {
        "cpu":    sprintf("%vm",  [safe_cpu_req_m]),
        "memory": sprintf("%vMi", [safe_mem_req_mib]),
    },
    "max_heap_mib": max_heap_mib,
    "diagnostics": {
        "target":           input.policy_config.target,
        "cpu_observed_m":   round(input.metrics.cpu_cores * 1000),
        "mem_observed_mib": round(input.metrics.total_mem_mb),
        "cpu_delta_pct":    round(cpu_delta_pct),
        "mem_delta_pct":    round(mem_delta_pct),
        "headroom_pct":     input.policy_config.headroom_pct,
    },
}
