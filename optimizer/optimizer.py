#!/usr/bin/env python3
"""
Autonomic Resource Optimizer — optimizer.py

Closed-loop controller that:
  0. Reads workload coordinates (namespace, deployment, container, manifest path)
     from the per-workload OPA policy — the CronJob carries NO workload-specific
     config beyond the policy key.
  1. Inspects the target Deployment in k8s to detect JVM workloads.
  2. Queries Prometheus for CPU and memory metrics (JVM-aware, configurable window).
  3. Posts observational data to OPA; the workload Rego injects policy_config and
     delegates sizing to the master policy (resource/optimizer).
  4. Opens a GitHub PR with the updated resource manifest (and -Xmx for JVM).

Environment variables — infrastructure (CronJob template, workload-agnostic):
  PROMETHEUS_URL        Prometheus API base URL
  OPA_URL               OPA REST server base URL
  WORKLOAD              Policy key: maps to resource/workloads/<key>.rego
                        (hyphens are normalized to underscores for Rego identifiers)
  ANALYSIS_WINDOW       Prometheus look-back window  (default: 30m)

  Workload identity (namespace, deployment, container, manifest_path) and all
  policy knobs live exclusively in optimizer/policy/workloads/<key>.rego.

Environment variables — GitHub PR:
  GITHUB_TOKEN          Personal access token (repo scope)
  GITHUB_REPO           Target repository in "owner/repo" format
  GITHUB_BASE_BRANCH    Base branch for the PR                    (default: main)

  DRY_RUN               Set to "true" to skip PR creation
"""

import base64
import io
import json
import logging
import os
import re
import sys
from datetime import datetime, timezone

import requests
from ruamel.yaml import YAML
from kubernetes import client as k8s_client
from kubernetes import config as k8s_config

# ── Configuration ─────────────────────────────────────────────────────────────

PROMETHEUS_URL     = os.getenv("PROMETHEUS_URL",
                               "http://kube-prometheus-stack-prometheus.monitoring:9090")
OPA_URL            = os.getenv("OPA_URL", "http://opa.monitoring:8181")
WORKLOAD           = os.getenv("WORKLOAD", "petclinic")   # OPA policy key
ANALYSIS_WINDOW    = os.getenv("ANALYSIS_WINDOW", "30m")

# GitHub PR
GITHUB_TOKEN       = os.getenv("GITHUB_TOKEN", "")
GITHUB_REPO        = os.getenv("GITHUB_REPO", "")
GITHUB_BASE_BRANCH = os.getenv("GITHUB_BASE_BRANCH", "main")
GITHUB_API_URL     = "https://api.github.com"

DRY_RUN = os.getenv("DRY_RUN", "false").lower() == "true"

# Workload coordinates — set at runtime from OPA policy (see get_workload_config)
TARGET_NAMESPACE  = ""
TARGET_DEPLOYMENT = ""
CONTAINER_NAME    = ""
MANIFEST_PATH     = ""

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
log = logging.getLogger(__name__)


# ── 0. Workload Config from OPA ───────────────────────────────────────────────

def get_workload_config() -> dict:
    """Fetch the full policy_config for the current workload from OPA.

    This is a plain data read (GET, no input body) — OPA returns the constant
    policy_config object defined in resource/workloads/<WORKLOAD>.rego.
    """
    key = WORKLOAD.replace("-", "_")
    url = f"{OPA_URL}/v1/data/resource/workloads/{key}/policy_config"
    try:
        resp = requests.get(url, timeout=10)
        resp.raise_for_status()
        config = resp.json().get("result")
        if not config:
            raise RuntimeError(
                f"OPA returned no policy_config for WORKLOAD='{WORKLOAD}'. "
                f"Does optimizer/policy/workloads/{key}.rego exist and is it loaded?"
            )
        return config
    except RuntimeError:
        raise
    except Exception as exc:
        raise RuntimeError(f"OPA policy_config fetch failed: {exc}") from exc


def _init_workload_target(config: dict) -> None:
    """Populate module-level workload coordinates from the OPA policy_config."""
    global TARGET_NAMESPACE, TARGET_DEPLOYMENT, CONTAINER_NAME, MANIFEST_PATH
    t = config.get("target", {})
    TARGET_NAMESPACE  = t.get("namespace",     "")
    TARGET_DEPLOYMENT = t.get("deployment",    "")
    CONTAINER_NAME    = t.get("container",     TARGET_DEPLOYMENT)
    MANIFEST_PATH     = t.get("manifest_path", f"app/{TARGET_DEPLOYMENT}.yaml")
    if not TARGET_NAMESPACE or not TARGET_DEPLOYMENT:
        raise RuntimeError(
            "policy_config.target must define 'namespace' and 'deployment'. "
            f"Got: {t}"
        )


# ── 1. Workload Inspection ────────────────────────────────────────────────────

def _load_k8s_config() -> None:
    """Load in-cluster config, fall back to kubeconfig for local testing."""
    try:
        k8s_config.load_incluster_config()
    except k8s_config.ConfigException:
        k8s_config.load_kube_config()


def get_deployment():
    """Fetch the Deployment object from the Kubernetes API."""
    return k8s_client.AppsV1Api().read_namespaced_deployment(
        name=TARGET_DEPLOYMENT, namespace=TARGET_NAMESPACE,
    )


def get_current_limits(deployment) -> dict:
    """Extract current resource limits for the target container."""
    for ctr in deployment.spec.template.spec.containers:
        if ctr.name == CONTAINER_NAME:
            limits = (ctr.resources.limits or {}) if ctr.resources else {}
            return {
                "cpu": limits.get("cpu", "1000m"),
                "mem": limits.get("memory", "1Gi"),
            }
    raise RuntimeError(f"Container '{CONTAINER_NAME}' not found in '{TARGET_DEPLOYMENT}'")


def is_java_workload(deployment) -> bool:
    """Return True if the workload is a JVM application.

    Detection strategy (in order):
      1. Image name contains 'java', 'jdk', 'jre', or 'spring'.
      2. Container env includes JAVA_OPTS, JAVA_TOOL_OPTIONS, or JDK_JAVA_OPTIONS.
    """
    java_env_vars = {"JAVA_OPTS", "JAVA_TOOL_OPTIONS", "JDK_JAVA_OPTIONS"}
    for ctr in deployment.spec.template.spec.containers:
        image = (ctr.image or "").lower()
        if any(kw in image for kw in ("java", "jdk", "jre", "spring")):
            log.info("Java workload detected via image: %s", ctr.image)
            return True
        if ctr.env:
            for env_var in ctr.env:
                if env_var.name in java_env_vars:
                    log.info("Java workload detected via env var: %s", env_var.name)
                    return True
    return False


# ── 2. Metrics Ingestion ──────────────────────────────────────────────────────

def _prom_query(query: str) -> float | None:
    """Execute an instant Prometheus query and return the first scalar result."""
    try:
        resp = requests.get(
            f"{PROMETHEUS_URL}/api/v1/query",
            params={"query": query},
            timeout=15,
        )
        resp.raise_for_status()
        results = resp.json().get("data", {}).get("result", [])
        if not results:
            log.warning("No data for query: %s", query)
            return None
        return float(results[0]["value"][1])
    except Exception as exc:
        log.error("Prometheus query failed [%s]: %s", query, exc)
        return None


def get_metrics(is_java: bool) -> dict:
    """Return CPU and memory metrics for the target container.

    For JVM workloads heap and off-heap are retrieved separately from
    jvm_memory_used_bytes so that:
      - total_mem_mb = heap_mb + offheap_mb  (drives the container limit)
      - heap_mb                               (drives -Xmx via OPA)

    For non-JVM workloads total_mem_mb comes from container_memory_working_set_bytes.

    All time-series are evaluated over ANALYSIS_WINDOW.
    """
    ns  = TARGET_NAMESPACE
    ctr = CONTAINER_NAME
    win = ANALYSIS_WINDOW

    cpu_query = (
        f'quantile_over_time(0.95,'
        f'rate(container_cpu_usage_seconds_total{{container="{ctr}",namespace="{ns}"}}[1m])'
        f'[{win}:1s])'
    )
    cpu_cores = _prom_query(cpu_query)
    if cpu_cores is None:
        raise RuntimeError("Could not retrieve CPU metrics from Prometheus.")

    if is_java:
        # OTel Java agent exports jvm_memory_used_bytes; the Prometheus exporter
        # in the OTel Collector prepends the workload name as a namespace prefix
        # (e.g. "petclinic_jvm_memory_used_bytes").  The label is jvm_memory_type
        # (not area) with values "heap" and "non_heap".
        metric_prefix = ctr  # OTel collector namespace = container/service name
        heap_query = (
            f'max(max_over_time('
            f'sum by (service_instance_id) ({metric_prefix}_jvm_memory_used_bytes{{jvm_memory_type="heap"}})'
            f'[{win}:1m]))'
        )
        offheap_query = (
            f'max(max_over_time('
            f'sum by (service_instance_id) ({metric_prefix}_jvm_memory_used_bytes{{jvm_memory_type="non_heap"}})'
            f'[{win}:1m]))'
        )
        heap_bytes    = _prom_query(heap_query)
        offheap_bytes = _prom_query(offheap_query)
        if heap_bytes is None or offheap_bytes is None:
            raise RuntimeError(
                "Could not retrieve JVM memory metrics. "
                "Is jvm_memory_used_bytes exposed by the workload?"
            )
        heap_mib    = heap_bytes    / (1024 ** 2)
        offheap_mib = offheap_bytes / (1024 ** 2)
        log.info(
            "Observed  CPU P95=%.3f cores | Heap=%.1f MiB | Off-heap=%.1f MiB | Total=%.1f MiB",
            cpu_cores, heap_mib, offheap_mib, heap_mib + offheap_mib,
        )
        return {
            "cpu_cores":    cpu_cores,
            "heap_mb":      heap_mib,
            "offheap_mb":   offheap_mib,
            "total_mem_mb": heap_mib + offheap_mib,
        }
    else:
        mem_query = (
            f'max_over_time('
            f'container_memory_working_set_bytes{{container="{ctr}",namespace="{ns}"}}'
            f'[{win}])'
        )
        mem_bytes = _prom_query(mem_query)
        if mem_bytes is None:
            raise RuntimeError("Could not retrieve memory metrics from Prometheus.")
        mem_mib = mem_bytes / (1024 ** 2)
        log.info("Observed  CPU P95=%.3f cores | Memory=%.1f MiB", cpu_cores, mem_mib)
        return {"cpu_cores": cpu_cores, "total_mem_mb": mem_mib}


# ── 3. Policy Decision via OPA ────────────────────────────────────────────────

def ask_opa(metrics: dict, current_limits: dict, is_java: bool) -> dict:
    """POST workload context to the per-workload OPA endpoint and return the
    policy decision document.

    The workload Rego (resource/workloads/<name>.rego) owns the policy_config
    knobs and delegates sizing to the master (resource/optimizer). Python sends
    only observational data — no knobs.
    """
    payload = {
        "input": {
            "metrics":        metrics,
            "current_limits": current_limits,
            "is_java":        is_java,
        }
    }
    log.info("OPA input:\n%s", json.dumps(payload["input"], indent=2))

    key = WORKLOAD.replace("-", "_")
    url = f"{OPA_URL}/v1/data/resource/workloads/{key}/result"

    try:
        resp = requests.post(url, json=payload, timeout=10)
        resp.raise_for_status()
        decision = resp.json().get("result")
        if not decision:
            raise RuntimeError(
                f"OPA returned no decision for WORKLOAD='{WORKLOAD}'. "
                f"Does optimizer/policy/workloads/{key}.rego exist and is it loaded?"
            )
        log.info("OPA decision:\n%s", json.dumps(decision, indent=2))
        return decision
    except RuntimeError:
        raise
    except Exception as exc:
        raise RuntimeError(f"OPA request failed: {exc}") from exc


# ── 4. GitHub PR ──────────────────────────────────────────────────────────────

def _gh_headers() -> dict:
    return {
        "Authorization":        f"Bearer {GITHUB_TOKEN}",
        "Accept":               "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def _gh_get(url: str, **kwargs) -> requests.Response:
    r = requests.get(url, headers=_gh_headers(), timeout=15, **kwargs)
    r.raise_for_status()
    return r


def _gh_post(url: str, **kwargs) -> requests.Response:
    r = requests.post(url, headers=_gh_headers(), timeout=15, **kwargs)
    r.raise_for_status()
    return r


def _gh_put(url: str, **kwargs) -> requests.Response:
    r = requests.put(url, headers=_gh_headers(), timeout=15, **kwargs)
    r.raise_for_status()
    return r


def _get_branch_sha(branch: str) -> str:
    data = _gh_get(
        f"{GITHUB_API_URL}/repos/{GITHUB_REPO}/git/ref/heads/{branch}"
    ).json()
    return data["object"]["sha"]


def _get_file(path: str, ref: str) -> tuple[str, str]:
    """Fetch file content and its blob SHA from GitHub. Returns (content, sha)."""
    data = _gh_get(
        f"{GITHUB_API_URL}/repos/{GITHUB_REPO}/contents/{path}",
        params={"ref": ref},
    ).json()
    return base64.b64decode(data["content"]).decode("utf-8"), data["sha"]


def _patch_manifest(content: str, decision: dict, is_java: bool) -> str:
    """Surgically patch only resource limits/requests (and JDK_JAVA_OPTIONS for
    JVM workloads) in the manifest, preserving all comments and formatting.

    Uses ruamel.yaml for a comment-preserving round-trip so the resulting PR
    diff is minimal — only the changed values appear in the diff.
    """
    ryaml = YAML()
    ryaml.preserve_quotes = True
    ryaml.explicit_start = True   # keep `---` before every document
    ryaml.width = 4096  # prevent unwanted line wrapping
    # Match the 4-space list indentation used in the manifests:
    #   key:
    #     - item   (dash at column key+4, value at key+6)
    ryaml.indent(mapping=2, sequence=4, offset=2)

    docs = list(ryaml.load_all(content))

    for doc in docs:
        if not doc or doc.get("kind") != "Deployment":
            continue
        for ctr in doc["spec"]["template"]["spec"]["containers"]:
            if ctr["name"] != CONTAINER_NAME:
                continue

            # ── resource limits & requests ─────────────────────────────────
            ctr["resources"]["limits"]["cpu"]    = decision["new_limits"]["cpu"]
            ctr["resources"]["limits"]["memory"] = decision["new_limits"]["memory"]
            ctr["resources"]["requests"]["cpu"]    = decision["new_requests"]["cpu"]
            ctr["resources"]["requests"]["memory"] = decision["new_requests"]["memory"]

            # ── JVM heap cap via JDK_JAVA_OPTIONS ─────────────────────────
            if is_java and decision.get("max_heap_mib"):
                xmx = f"-Xmx{decision['max_heap_mib']}m"
                for env_entry in ctr.get("env", []):
                    if env_entry.get("name") in ("JDK_JAVA_OPTIONS", "JAVA_OPTS",
                                                  "JAVA_TOOL_OPTIONS"):
                        val = env_entry.get("value", "")
                        env_entry["value"] = (
                            re.sub(r"-Xmx\S+", xmx, val)
                            if "-Xmx" in val
                            else f"{val} {xmx}".strip()
                        )
                        break

    buf = io.StringIO()
    ryaml.dump_all(docs, buf)
    return buf.getvalue()


def open_github_pr(decision: dict, is_java: bool, metrics: dict) -> str:
    """Commit the updated manifest to a new branch and open a PR.
    Returns the PR URL."""
    if not GITHUB_TOKEN or not GITHUB_REPO:
        raise RuntimeError("GITHUB_TOKEN and GITHUB_REPO must be set for PR creation.")

    ts     = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    branch = f"optimizer/resize-{TARGET_DEPLOYMENT}-{ts}"

    base_sha            = _get_branch_sha(GITHUB_BASE_BRANCH)
    content, file_sha   = _get_file(MANIFEST_PATH, GITHUB_BASE_BRANCH)
    new_content         = _patch_manifest(content, decision, is_java)

    # Create branch
    _gh_post(
        f"{GITHUB_API_URL}/repos/{GITHUB_REPO}/git/refs",
        json={"ref": f"refs/heads/{branch}", "sha": base_sha},
    )

    # Commit updated manifest
    _gh_put(
        f"{GITHUB_API_URL}/repos/{GITHUB_REPO}/contents/{MANIFEST_PATH}",
        json={
            "message": f"chore(optimizer): resize {TARGET_DEPLOYMENT} — {ts}",
            "content": base64.b64encode(new_content.encode()).decode(),
            "sha":     file_sha,
            "branch":  branch,
        },
    )

    # Build PR body
    diag     = decision.get("diagnostics", {})
    java_row = (
        f"\n| **JVM `-Xmx`** | `{decision['max_heap_mib']}Mi` | |"
        if is_java and decision.get("max_heap_mib") else ""
    )
    headroom = diag.get("headroom_pct", "—")

    jvm_diag = ""
    if is_java and decision.get("max_heap_mib"):
        heap_mb    = round(metrics.get("heap_mb", 0))
        offheap_mb = round(metrics.get("offheap_mb", 0))
        jvm_diag = f"\nHeap observed:   {heap_mb} MiB\nOff-heap:        {offheap_mb} MiB"

    body = f"""\
## Resource resize — `{TARGET_DEPLOYMENT}`

| | CPU | Memory |
|---|---|---|
| **New limit**   | `{decision['new_limits']['cpu']}` | `{decision['new_limits']['memory']}` |
| **New request** | `{decision['new_requests']['cpu']}` | `{decision['new_requests']['memory']}` |{java_row}

### Diagnostics
```
CPU observed:    {diag.get('cpu_observed_m')} m      delta: {diag.get('cpu_delta_pct')} %
Memory observed: {diag.get('mem_observed_mib')} MiB  delta: {diag.get('mem_delta_pct')} %{jvm_diag}
Analysis window: {ANALYSIS_WINDOW}
Headroom:        {headroom} %
```

_Generated by the Autonomic Resource Optimizer._"""

    pr = _gh_post(
        f"{GITHUB_API_URL}/repos/{GITHUB_REPO}/pulls",
        json={
            "title": f"chore(optimizer): resize {TARGET_DEPLOYMENT} ({ts})",
            "body":  body,
            "head":  branch,
            "base":  GITHUB_BASE_BRANCH,
        },
    ).json()
    return pr["html_url"]


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    log.info("=" * 60)
    log.info("Resource Optimizer  workload=%s  window=%s  dry_run=%s",
             WORKLOAD, ANALYSIS_WINDOW, DRY_RUN)
    log.info("=" * 60)

    # Step 0 — fetch workload coordinates and policy knobs from OPA
    policy = get_workload_config()
    _init_workload_target(policy)
    log.info("Workload target: %s/%s (container=%s  manifest=%s)",
             TARGET_NAMESPACE, TARGET_DEPLOYMENT, CONTAINER_NAME, MANIFEST_PATH)

    _load_k8s_config()

    # Step 1 — inspect deployment (must precede metrics to know if JVM)
    deployment     = get_deployment()
    current_limits = get_current_limits(deployment)
    is_java        = is_java_workload(deployment)
    log.info("Current limits: cpu=%s  memory=%s  java=%s",
             current_limits["cpu"], current_limits["mem"], is_java)

    # Step 2 — fetch metrics (java-aware, window-parameterised)
    metrics = get_metrics(is_java)

    # Step 3 — OPA decision
    decision = ask_opa(
        metrics        = metrics,
        current_limits = {"cpu": current_limits["cpu"], "mem": current_limits["mem"]},
        is_java        = is_java,
    )

    action = decision.get("action", "no_change")
    log.info("OPA action: %s", action)

    if action != "resize":
        log.info("No resize needed — current limits are within the acceptable range.")
        return

    # Step 4 — open GitHub PR
    if DRY_RUN:
        log.info("[DRY RUN] Would open PR with:\n%s", json.dumps(decision, indent=2))
        return

    pr_url = open_github_pr(decision, is_java, metrics)
    log.info("PR opened: %s", pr_url)
    log.info("Resource Optimizer done.")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        log.error("Fatal error: %s", exc)
        sys.exit(1)
