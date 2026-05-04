#!/usr/bin/env python3
"""
LKE Node Healer
Watches all cluster nodes for externally cordoned (Unschedulable=true) conditions.

When a node is found to be Unschedulable=true, it annotates the node with
lke.ops/needs-attention=true so the ops team is alerted and can investigate
the root cause manually.

This script does NOT auto-remediate. No nodes are drained or deleted.
"""

import logging
import os
import time
import threading

from kubernetes import client, config, watch

#  Logging 
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%SZ",
)
log = logging.getLogger("node-healer")

#  Config 
DRY_RUN       = os.getenv("DRY_RUN", "false").lower() == "true"
EXCLUDE_NODES = set(filter(None, os.getenv("EXCLUDE_NODES", "").split(",")))

#  Shared state 
# node names that have already been annotated — avoids re-annotating on every event
annotated_nodes: set[str] = set()

state_lock = threading.Lock()


#  Helpers

def is_control_plane(node: client.V1Node) -> bool:
    labels = node.metadata.labels or {}
    return (
        "node-role.kubernetes.io/control-plane" in labels
        or "node-role.kubernetes.io/master" in labels
    )


def is_externally_cordoned(node: client.V1Node) -> bool:
    """
    Returns True if node.spec.unschedulable is True.
    This script never cordons nodes itself, so any cordoned node
    was cordoned externally.
    """
    return bool(node.spec and node.spec.unschedulable)


def annotate_node_needs_attention(v1: client.CoreV1Api, node_name: str, reason: str):
    """
    Annotate the node so the ops team knows it needs investigation.
    Sets:
      lke.ops/needs-attention  = "true"
      lke.ops/attention-reason = human readable reason
      lke.ops/attention-since  = unix timestamp when first detected
    """
    if DRY_RUN:
        log.info(f"[{node_name}] DRY_RUN — would annotate node as needing attention")
        return
    try:
        body = {
            "metadata": {
                "annotations": {
                    "lke.ops/needs-attention":  "true",
                    "lke.ops/attention-reason": reason,
                    "lke.ops/attention-since":  str(int(time.time())),
                }
            }
        }
        v1.patch_node(node_name, body)
        log.warning(
            f"[{node_name}] annotated lke.ops/needs-attention=true | reason: {reason}"
        )
    except Exception as e:
        log.error(f"[{node_name}] failed to annotate node: {e}")


def clear_attention_annotation(v1: client.CoreV1Api, node_name: str):
    """
    Remove needs-attention annotations when node is no longer cordoned.
    Setting annotation values to None removes them from the object.
    """
    if DRY_RUN:
        log.info(f"[{node_name}] DRY_RUN — would clear needs-attention annotation")
        return
    try:
        body = {
            "metadata": {
                "annotations": {
                    "lke.ops/needs-attention":  None,
                    "lke.ops/attention-reason": None,
                    "lke.ops/attention-since":  None,
                }
            }
        }
        v1.patch_node(node_name, body)
        log.info(f"[{node_name}] lke.ops/needs-attention annotation cleared")
    except Exception as e:
        log.error(f"[{node_name}] failed to clear annotation: {e}")


#  Node event handler 

def handle_node_event(v1: client.CoreV1Api, node: client.V1Node):
    name = node.metadata.name

    if is_control_plane(node):
        return
    if name in EXCLUDE_NODES:
        return

    if is_externally_cordoned(node):
        with state_lock:
            already_annotated = name in annotated_nodes

        if not already_annotated:
            with state_lock:
                annotated_nodes.add(name)
            reason = (
                f"Node is Unschedulable=true (externally cordoned). "
                f"Possible causes: FailedToCreateRoute (reserved IP in pod CIDR), "
                f"manual kubectl cordon, or admission webhook. "
                f"Investigate: kubectl describe node {name}"
            )
            annotate_node_needs_attention(v1, name, reason)
        else:
            log.debug(f"[{name}] externally cordoned — already annotated, waiting for ops")

    else:
        # Node is no longer cordoned — clear annotation if we previously set it
        with state_lock:
            was_annotated = name in annotated_nodes
            if was_annotated:
                annotated_nodes.discard(name)

        if was_annotated:
            log.info(f"[{name}] no longer cordoned — clearing needs-attention annotation")
            clear_attention_annotation(v1, name)


#  Main watch loop 

def sync_annotated_nodes(v1: client.CoreV1Api):
    """
    On startup, scan all existing nodes for the lke.ops/needs-attention annotation
    and repopulate annotated_nodes. This ensures that if the script restarts
    (pod rescheduled after node failure, rollout restart, crash), it can still
    clear annotations from nodes that were annotated by the previous instance
    when those nodes are eventually uncordoned.
    """
    try:
        nodes = v1.list_node().items
        found = []
        for node in nodes:
            annotations = node.metadata.annotations or {}
            if annotations.get("lke.ops/needs-attention") == "true":
                annotated_nodes.add(node.metadata.name)
                found.append(node.metadata.name)

        if found:
            log.info(
                f"startup sync — found {len(found)} previously annotated node(s): "
                f"{', '.join(found)} — will auto-clear when uncordoned"
            )
        else:
            log.info("startup sync — no previously annotated nodes found")

    except Exception as e:
        log.warning(f"startup sync failed: {e} — continuing without sync")


def watch_nodes():
    config.load_incluster_config()
    v1 = client.CoreV1Api()

    log.info(
        f"LKE Node Healer started | "
        f"DRY_RUN={DRY_RUN} | "
        f"EXCLUDE={EXCLUDE_NODES or 'none'} | "
        f"mode=annotation-only (no auto-remediation)"
    )

    # Repopulate annotated_nodes from existing cluster state before watching
    sync_annotated_nodes(v1)

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
                        annotated_nodes.discard(node_name)
                    continue

                if node.spec is None:
                    continue

                handle_node_event(v1, node)

        except Exception as e:
            log.error(f"Watch stream error: {e} — restarting in 10s")
            time.sleep(10)
        finally:
            w.stop()


if __name__ == "__main__":
    watch_nodes()
