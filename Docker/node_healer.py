#!/usr/bin/env python3
"""
LKE Node Healer
Watches all cluster nodes across all pools for unhealthy conditions.

On detecting an unhealthy node:
  1. Derives the pool ID from the node name (lke{clusterID}-{poolID}-{hash})
  2. Immediately pre-provisions a replacement by resizing that pool +1
     - If count < max: resize count directly (room already exists)
     - If count == max: bump autoscaler max first, then resize count
  3. Waits UNHEALTHY_THRESHOLD_SECONDS (default 60s) — if node recovers, cancel
  4. Waits for the new node to be Ready
  5. Cordons the bad node (stop new pods scheduling onto it)
  6. Drains the bad node (evict existing pods immediately)
  7. Deletes the Linode instance via API
  8. Restores autoscaler max if it was raised (count restores itself via the delete)
"""

import logging
import os
import subprocess
import time
import threading

import requests
from kubernetes import client, config, watch

#  Logging 
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%SZ",
)
log = logging.getLogger("node-healer")

#  Config 
UNHEALTHY_THRESHOLD_SECONDS = int(os.getenv("UNHEALTHY_THRESHOLD_SECONDS", "60"))
COOLDOWN_SECONDS            = int(os.getenv("COOLDOWN_SECONDS", "600"))
DRY_RUN                     = os.getenv("DRY_RUN", "false").lower() == "true"
EXCLUDE_NODES               = set(filter(None, os.getenv("EXCLUDE_NODES", "").split(",")))
LINODE_TOKEN                = os.getenv("LINODE_TOKEN", "")
LINODE_CLUSTER_ID           = os.getenv("LINODE_CLUSTER_ID", "")
NEW_NODE_READY_TIMEOUT      = int(os.getenv("NEW_NODE_READY_TIMEOUT", "300"))
DRAIN_TIMEOUT               = int(os.getenv("DRAIN_TIMEOUT", "120"))

LINODE_API_BASE = "https://api.linode.com/v4"

#  Unhealthy conditions 
UNHEALTHY_CONDITIONS = {
    "Ready":              ["False", "Unknown"],
    "DiskPressure":       ["True"],
    "MemoryPressure":     ["True"],
    "NetworkUnavailable": ["True"],
    "PIDPressure":        ["True"],
}

#  Shared state
# node_name → first_seen_unhealthy_epoch
unhealthy_since: dict[str, float] = {}

# node_name → remediated_at_epoch (cooldown)
cooldown_tracker: dict[str, float] = {}

# node_name → original pool state dict captured when pre-provisioning was triggered.
# Stored so remediate_node can reuse it without calling preprovision() a second time.
preprovision_state: dict[str, dict] = {}

state_lock = threading.Lock()


#  Node name helpers 

def extract_pool_id(node_name: str) -> str | None:
    """
    Derive pool ID from node name.
    Format: lke{clusterID}-{poolID}-{hash}
    e.g.    lke270250-860364-53cf264d0000 → "860364"
    """
    parts = node_name.split("-")
    if len(parts) >= 2:
        return parts[1]
    log.error(f"[{node_name}] cannot parse pool ID from node name")
    return None


def extract_linode_id(node: client.V1Node) -> str | None:
    """Extract Linode instance ID from spec.providerID (linode://12345678)."""
    provider_id = node.spec.provider_id if node.spec else None
    if not provider_id or not provider_id.startswith("linode://"):
        log.error(f"[{node.metadata.name}] missing or invalid providerID: {provider_id}")
        return None
    return provider_id.replace("linode://", "").strip()


#  Linode API 

def linode_headers() -> dict:
    return {
        "Authorization": f"Bearer {LINODE_TOKEN}",
        "Content-Type": "application/json",
    }


def get_pool_info(pool_id: str) -> dict | None:
    """
    GET pool state for a specific pool.
    Returns { count, min, max } or None on failure.
    """
    url = f"{LINODE_API_BASE}/lke/clusters/{LINODE_CLUSTER_ID}/pools/{pool_id}"
    try:
        resp = requests.get(url, headers=linode_headers(), timeout=30)
        if resp.status_code == 200:
            data       = resp.json()
            count      = data.get("count", 0)
            autoscaler = data.get("autoscaler", {})
            min_count  = autoscaler.get("min", count)
            max_count  = autoscaler.get("max", count)
            log.info(f"[pool {pool_id}] count={count} min={min_count} max={max_count}")
            return {"count": count, "min": min_count, "max": max_count}
        log.error(f"[pool {pool_id}] GET failed {resp.status_code}: {resp.text}")
    except requests.RequestException as e:
        log.error(f"[pool {pool_id}] GET request failed: {e}")
    return None


def set_pool_count(pool_id: str, count: int, reason: str) -> bool:
    """PUT count on a pool — provisions or removes nodes."""
    url = f"{LINODE_API_BASE}/lke/clusters/{LINODE_CLUSTER_ID}/pools/{pool_id}"
    if DRY_RUN:
        log.info(f"[pool {pool_id}] DRY_RUN — would set count={count} ({reason})")
        return True
    try:
        resp = requests.put(url, headers=linode_headers(), json={"count": count}, timeout=30)
        if resp.status_code == 200:
            log.info(f"[pool {pool_id}] count set to {count}  ({reason})")
            return True
        log.error(f"[pool {pool_id}] set count failed {resp.status_code}: {resp.text}")
    except requests.RequestException as e:
        log.error(f"[pool {pool_id}] set count request failed: {e}")
    return False


def set_autoscaler_bounds(pool_id: str, min_count: int, max_count: int, reason: str) -> bool:
    """PUT autoscaler min/max — changes ceiling/floor only, does not add/remove nodes."""
    url = f"{LINODE_API_BASE}/lke/clusters/{LINODE_CLUSTER_ID}/pools/{pool_id}"
    if DRY_RUN:
        log.info(f"[pool {pool_id}] DRY_RUN — would set autoscaler min={min_count} max={max_count} ({reason})")
        return True
    try:
        resp = requests.put(
            url,
            headers=linode_headers(),
            json={"autoscaler": {"min": min_count, "max": max_count}},
            timeout=30,
        )
        if resp.status_code == 200:
            log.info(f"[pool {pool_id}] autoscaler set min={min_count} max={max_count} ({reason})")
            return True
        log.error(f"[pool {pool_id}] set autoscaler failed {resp.status_code}: {resp.text}")
    except requests.RequestException as e:
        log.error(f"[pool {pool_id}] set autoscaler request failed: {e}")
    return False


def delete_linode_instance(node_name: str, linode_id: str) -> bool:
    """DELETE /v4/linode/instances/{linodeId}."""
    if DRY_RUN:
        log.info(f"[{node_name}] DRY_RUN — would DELETE linode instance {linode_id}")
        return True
    if not LINODE_TOKEN:
        log.error(f"[{node_name}] LINODE_TOKEN not set")
        return False
    url = f"{LINODE_API_BASE}/linode/instances/{linode_id}"
    try:
        resp = requests.delete(url, headers=linode_headers(), timeout=30)
        if resp.status_code == 200:
            log.info(f"[{node_name}] step 5/5 — Linode instance {linode_id} deleted ")
            return True
        log.error(f"[{node_name}] delete failed {resp.status_code}: {resp.text}")
    except requests.RequestException as e:
        log.error(f"[{node_name}] delete request failed: {e}")
    return False


#  Kubernetes helpers 

def is_control_plane(node: client.V1Node) -> bool:
    labels = node.metadata.labels or {}
    return (
        "node-role.kubernetes.io/control-plane" in labels
        or "node-role.kubernetes.io/master" in labels
    )


def in_cooldown(node_name: str) -> bool:
    with state_lock:
        ts = cooldown_tracker.get(node_name)
        if ts and (time.time() - ts) < COOLDOWN_SECONDS:
            log.debug(f"[{node_name}] in cooldown — {int(COOLDOWN_SECONDS - (time.time() - ts))}s remaining")
            return True
        return False


# Messages that indicate a node is in a normal startup or teardown state —
# not a genuine failure. Conditions with these messages are skipped entirely.
SKIP_MESSAGES = [
    # Teardown — node is already being shut down by LKE
    "shutting down",
    "being deleted",
    # Startup — CNI/CSI not yet initialised on a newly provisioned node
    "cni plugin not initialized",
    "NetworkPluginNotReady",
    "CSINode is not yet initialized",
    # node is shutting down gracefully
    "node is shutting down",
]

def get_unhealthy_reason(node: client.V1Node) -> str | None:
    conditions = (node.status or client.V1NodeStatus()).conditions or []
    for cond in conditions:
        bad = UNHEALTHY_CONDITIONS.get(cond.type)
        if not bad or cond.status not in bad:
            continue

        message = cond.message or ""

        # Skip conditions whose message matches a known startup or teardown phrase
        if any(phrase.lower() in message.lower() for phrase in SKIP_MESSAGES):
            log.debug(
                f"[{node.metadata.name}] skipping {cond.type}={cond.status} "
                f"— startup/teardown message: {message}"
            )
            continue

        return f"{cond.type}={cond.status} ({message or 'no message'})"
    return None


def wait_for_new_node_ready(
    v1: client.CoreV1Api,
    pool_id: str,
    exclude_node: str,
    timeout: int,
    provisioned_after: float | None = None,
) -> bool:
    """
    Wait until a Ready node exists in pool_id that is not exclude_node.

    provisioned_after: epoch timestamp of when we triggered the pool resize.
    Only nodes created after this time are considered — avoids matching
    pre-existing healthy nodes in the same pool.
    If None, any Ready node in the pool (other than exclude_node) qualifies.
    """
    log.info(f"[pool {pool_id}] step 2/5 — waiting up to {timeout}s for replacement node to be Ready...")
    deadline = time.time() + timeout
    pool_prefix = f"lke{LINODE_CLUSTER_ID}-{pool_id}-"

    while time.time() < deadline:
        try:
            nodes = v1.list_node().items
            for node in nodes:
                name = node.metadata.name
                if name == exclude_node:
                    continue
                if not name.startswith(pool_prefix):
                    continue
                if is_control_plane(node):
                    continue

                # If we know when provisioning was triggered, only accept nodes
                # created after that point — they are the replacement we provisioned.
                # Use a 60s buffer to account for API timestamp imprecision.
                if provisioned_after is not None:
                    created_at = node.metadata.creation_timestamp.timestamp()
                    if created_at < provisioned_after - 60:
                        continue

                conditions = node.status.conditions or []
                for cond in conditions:
                    if cond.type == "Ready" and cond.status == "True":
                        age = time.time() - node.metadata.creation_timestamp.timestamp()
                        log.info(f"[pool {pool_id}] step 2/5 — replacement node {name} is Ready (age {age:.0f}s) ")
                        return True
        except Exception as e:
            log.warning(f"[pool {pool_id}] error listing nodes: {e}")
        time.sleep(15)

    log.warning(f"[pool {pool_id}] step 2/5 — replacement node not Ready within {timeout}s — proceeding anyway")
    return False


def cordon_node(v1: client.CoreV1Api, node_name: str):
    """Mark node as unschedulable — no new pods will be placed on it."""
    log.info(f"[{node_name}] step 3/5 — cordoning (no new pods will be scheduled onto this node)")
    if DRY_RUN:
        log.info(f"[{node_name}] DRY_RUN — skipping cordon")
        return
    v1.patch_node(node_name, {"spec": {"unschedulable": True}})


def drain_node(node_name: str):
    """
    Evict all pods from the node immediately.
    DaemonSet pods are ignored (they reschedule automatically).
    """
    log.info(f"[{node_name}] step 4/5 — draining (evicting pods to replacement node, timeout {DRAIN_TIMEOUT}s)")
    if DRY_RUN:
        log.info(f"[{node_name}] DRY_RUN — skipping drain")
        return
    cmd = [
        "kubectl", "drain", node_name,
        "--ignore-daemonsets",
        "--delete-emptydir-data",
        "--force",
        f"--timeout={DRAIN_TIMEOUT}s",
        "--grace-period=30",
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        log.warning(f"[{node_name}] step 4/5 — drain exited {result.returncode}: {result.stderr.strip()}")
    else:
        log.info(f"[{node_name}] step 4/5 — drain complete  (pods evicted)")


#  Remediation 

def preprovision(pool_id: str, node_name: str) -> dict | None:
    """
    Pre-provision a replacement node for the given pool.
    Returns a dict with original pool state so cleanup knows what to restore.
    Returns None if pre-provisioning failed or was not possible.

    Path A (count < max): resize count directly — ceiling already has room.
    Path B (count == max): bump autoscaler max first, then resize count.
    """
    pool_info = get_pool_info(pool_id)
    if not pool_info:
        log.error(f"[{node_name}] could not fetch pool info for pool {pool_id}")
        return None

    count     = pool_info["count"]
    min_count = pool_info["min"]
    max_count = pool_info["max"]
    bumped_max = False

    if count < max_count:
        # Path A — room exists, resize directly
        log.info(f"[{node_name}] pool {pool_id} has room ({count}/{max_count}) — resizing count directly")
        ok = set_pool_count(pool_id, count + 1, reason=f"pre-provision for {node_name}")
    else:
        # Path B — at max, bump ceiling first then resize
        log.info(f"[{node_name}] pool {pool_id} at max ({count}/{max_count}) — bumping autoscaler max first")
        ok = set_autoscaler_bounds(pool_id, min_count, max_count + 1, reason=f"raise ceiling for {node_name}")
        if ok:
            ok = set_pool_count(pool_id, count + 1, reason=f"pre-provision for {node_name}")
            bumped_max = ok

    if not ok:
        log.error(f"[{node_name}] pre-provisioning failed")
        return None

    return {
        "pool_id":        pool_id,
        "count":          count,
        "min":            min_count,
        "max":            max_count,
        "bumped_max":     bumped_max,
        "provisioned_at": time.time(),   # used to identify the new node by creation time
    }


def cleanup_pool(original: dict, node_name: str):
    """
    Restore pool to original state after remediation.
    - Count restores itself when the Linode instance is deleted — do NOT touch count.
    - Only restore autoscaler max if we bumped it (Path B).
    """
    if not original["bumped_max"]:
        log.info(f"[{node_name}] cleanup — no autoscaler changes to restore")
        return

    pool_id   = original["pool_id"]
    min_count = original["min"]
    max_count = original["max"]

    log.info(f"[{node_name}] cleanup — restoring pool {pool_id} autoscaler max to {max_count}")
    set_autoscaler_bounds(
        pool_id, min_count, max_count,
        reason=f"restore after {node_name} remediation"
    )


def remediate_node(v1: client.CoreV1Api, node: client.V1Node, reason: str):
    """
    Full remediation sequence (runs in a background thread):
      1. Reuse pre-provision state captured at t=0 (do NOT call preprovision again)
      2. Wait for new node to be Ready
      3. Cordon bad node
      4. Drain bad node (pods move off immediately)
      5. Delete Linode instance
      6. Restore autoscaler bounds if changed
    """
    name      = node.metadata.name
    linode_id = extract_linode_id(node)
    pool_id   = extract_pool_id(name)

    if not linode_id or not pool_id:
        log.error(f"[{name}] missing linode ID or pool ID — cannot remediate")
        return

    log.warning(f"[{name}] ── node removal sequence started | pool={pool_id} | reason: {reason}")

    #  Step 1: retrieve pre-provision state captured at t=0 

    with state_lock:
        original_pool_state = preprovision_state.pop(name, None)

    if original_pool_state:
        log.info(f"[{name}] step 1/5 — replacement node already provisioning since t=0 (original pool count: {original_pool_state['count']})")
        #  Step 2: wait for new node to be Ready
        # Pass provisioned_at so we only match the node we actually provisioned,
        # not pre-existing healthy nodes in the same pool.
        wait_for_new_node_ready(
            v1,
            pool_id=pool_id,
            exclude_node=name,
            timeout=NEW_NODE_READY_TIMEOUT,
            provisioned_after=original_pool_state.get("provisioned_at"),
        )
    elif LINODE_CLUSTER_ID:
        # Pre-provisioning at t=0 either failed or hasn't completed yet.
        # Attempt it now as a fallback — this is the only case where we call
        # preprovision() here, and only once.
        log.warning(f"[{name}] step 1/5 — no pre-provision state found (healer may have restarted) — attempting now")
        original_pool_state = preprovision(pool_id, name)
        if original_pool_state:
            wait_for_new_node_ready(
                v1,
                pool_id=pool_id,
                exclude_node=name,
                timeout=NEW_NODE_READY_TIMEOUT,
                provisioned_after=original_pool_state.get("provisioned_at"),
            )
        else:
            log.warning(f"[{name}] step 1/5 — pre-provisioning failed — will delete directly, LKE auto-heal will replace")
    else:
        log.warning(f"[{name}] step 1/5 — LINODE_CLUSTER_ID not set, skipping pre-provisioning")

    #  Step 3: cordon 
    try:
        cordon_node(v1, name)
    except Exception as e:
        log.warning(f"[{name}] step 3/5 — cordon failed: {e} — proceeding to drain anyway")

    #  Step 4: drain
    drain_node(name)

    #  Step 5: delete Linode instance
    log.warning(f"[{name}] step 5/5 — deleting Linode instance {linode_id} via API")
    deleted = delete_linode_instance(name, linode_id)

    if not deleted:
        log.error(f"[{name}] step 5/5 — Linode API delete failed — pool NOT restored to avoid under-provisioning")
        return

    #  Step 6: restore autoscaler bounds if we changed them 
    if original_pool_state:
        cleanup_pool(original_pool_state, name)

    log.info(f"[{name}] ── node removal sequence complete ")



def handle_node_event(v1: client.CoreV1Api, node: client.V1Node):
    name = node.metadata.name

    if is_control_plane(node):
        return
    if name in EXCLUDE_NODES:
        return
    if in_cooldown(name):
        return

    reason = get_unhealthy_reason(node)

    with state_lock:
        if reason:
            first_seen = unhealthy_since.setdefault(name, time.time())
            elapsed    = time.time() - first_seen

            log.info(
                f"[{name}] unhealthy: {reason} — "
                f"persisting for {elapsed:.0f}s / {UNHEALTHY_THRESHOLD_SECONDS}s threshold"
            )

            # Pre-provision immediately on first detection — only once per node
            if name not in preprovision_state and LINODE_CLUSTER_ID:
                # Mark immediately (before thread starts) to prevent duplicate triggers
                preprovision_state[name] = {}   # placeholder — thread will fill real value
                pool_id = extract_pool_id(name)
                if pool_id:
                    def _do_preprovision(pool_id=pool_id, name=name):
                        result = preprovision(pool_id, name)
                        with state_lock:
                            if name in preprovision_state:
                                # Only store if node is still being tracked
                                preprovision_state[name] = result or {}
                    t = threading.Thread(target=_do_preprovision, daemon=True)
                    t.start()

            # Threshold exceeded — start full remediation
            if elapsed >= UNHEALTHY_THRESHOLD_SECONDS:
                log.warning(f"[{name}] unhealthy for {elapsed:.0f}s — threshold exceeded, starting node removal sequence")
                cooldown_tracker[name] = time.time()
                unhealthy_since.pop(name, None)
                # preprovision_state[name] is consumed inside remediate_node

                t = threading.Thread(
                    target=remediate_node,
                    args=(v1, node, reason),
                    daemon=True,
                )
                t.start()

        else:
            # Node recovered before threshold
            if name in unhealthy_since:
                log.info(f"[{name}] recovered before threshold — cancelling")
                unhealthy_since.pop(name, None)

                # If pre-provisioning ran, undo it using the stored original state
                original = preprovision_state.pop(name, None)
                if original and original.get("count") is not None:
                    pool_id = original["pool_id"]
                    log.info(f"[{name}] node recovered — removing pre-provisioned node from pool {pool_id}")
                    # Resize count back down — the extra node is unneeded
                    set_pool_count(
                        pool_id,
                        original["count"],   # original count before we added 1
                        reason=f"{name} recovered, removing pre-provisioned node"
                    )
                    # Restore autoscaler max if we bumped it
                    if original.get("bumped_max"):
                        set_autoscaler_bounds(
                            pool_id,
                            original["min"],
                            original["max"],
                            reason=f"{name} recovered, restoring autoscaler max"
                        )



def watch_nodes():
    if not LINODE_TOKEN and not DRY_RUN:
        log.error("LINODE_TOKEN not set and DRY_RUN=false — refusing to start")
        raise SystemExit(1)

    if not LINODE_CLUSTER_ID:
        log.warning("LINODE_CLUSTER_ID not set — pre-provisioning disabled")

    config.load_incluster_config()
    v1 = client.CoreV1Api()

    log.info(
        f"LKE Node Healer started | "
        f"DRY_RUN={DRY_RUN} | "
        f"THRESHOLD={UNHEALTHY_THRESHOLD_SECONDS}s | "
        f"COOLDOWN={COOLDOWN_SECONDS}s | "
        f"CLUSTER={LINODE_CLUSTER_ID or 'not set'} | "
        f"NEW_NODE_READY_TIMEOUT={NEW_NODE_READY_TIMEOUT}s | "
        f"DRAIN_TIMEOUT={DRAIN_TIMEOUT}s"
    )

    while True:
        w = watch.Watch()
        try:
            log.info("Starting node watch stream...")
            for event in w.stream(v1.list_node, timeout_seconds=300):
                event_type = event["type"]
                node       = event["object"]
                node_name  = node.metadata.name

                if event_type == "DELETED":
                    with state_lock:
                        unhealthy_since.pop(node_name, None)
                        preprovision_state.pop(node_name, None)
                    continue

                if node.status is None or node.status.conditions is None:
                    continue

                handle_node_event(v1, node)

        except Exception as e:
            log.error(f"Watch stream error: {e} — restarting in 10s")
            time.sleep(10)
        finally:
            w.stop()


if __name__ == "__main__":
    watch_nodes()
