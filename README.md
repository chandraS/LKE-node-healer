# LKE Node Healer

## Overview

LKE Node Healer is a lightweight Python script running as a Kubernetes Deployment that watches all nodes in an LKE or LKE-E cluster. When a node is found to be externally cordoned (`Unschedulable=true`), the script annotates it with `lke.ops/needs-attention=true` so the ops team is alerted and can investigate the root cause manually.

The script does **not** auto-remediate. No nodes are drained or deleted. Remediation is left to the ops team.

---


In LKE, a node can become cordoned (`Unschedulable=true`) without being unhealthy from Kubernetes' perspective. 


In this state the node shows `Ready=True` but no pods can be scheduled onto it. It is effectively unusable and Kubernetes has no native mechanism to detect or remediate this automatically.

The healer detects this condition and annotates the node so the ops team is notified.

---

## How It Works

### 1. Watch nodes

The script opens a persistent streaming connection to the Kubernetes API server using the Kubernetes Python client. There is no polling loop — the API server pushes an event every time any node's status changes.

The watch stream reconnects automatically if the connection drops. On reconnect, the current state of all nodes is fetched before resuming the stream so no events are missed.

The script authenticates using the ServiceAccount token automatically mounted into the pod at `/var/run/secrets/kubernetes.io/serviceaccount/token`. No kubeconfig or kubectl is required.

### 2. Detect externally cordoned nodes

On each event, the script checks `node.spec.unschedulable`. If it is `true`, the node has been cordoned by something other than this script (this script never cordons nodes itself).

Common causes:

- Route controller failure due to a reserved IP in the pod CIDR (`FailedToCreateRoute`)
- Manual `kubectl cordon` by an operator
- An admission webhook or external controller

### 3. Annotate the node

When an externally cordoned node is detected, the script annotates it once with three annotations:

| Annotation | Value |
|---|---|
| `lke.ops/needs-attention` | `"true"` |
| `lke.ops/attention-reason` | Human readable description of the condition |
| `lke.ops/attention-since` | Unix timestamp when first detected |

The annotation is set only once per cordon event. Subsequent watch events for the same node are silently skipped until the node is uncordoned.

### 4. Clear annotation on recovery

When the node is uncordoned (`spec.unschedulable` flips to `false`), the script detects the change via the watch stream and removes all three annotations automatically.

### 5. Startup sync

On startup, the script scans all existing nodes for the `lke.ops/needs-attention=true` annotation before opening the watch stream. This ensures that if the script pod is rescheduled (e.g. after a node failure), it can still clear annotations from nodes that were annotated by the previous instance when those nodes are eventually uncordoned.

---

## Querying Affected Nodes

```bash
# List all nodes needing attention
kubectl get nodes -o json | \
  jq -r '.items[] | select(.metadata.annotations["lke.ops/needs-attention"] == "true") | .metadata.name'

# See the reason on a specific node
kubectl get node <node-name> -o jsonpath='{.metadata.annotations.lke\.ops/attention-reason}'

# See when it was first detected (unix timestamp)
kubectl get node <node-name> -o jsonpath='{.metadata.annotations.lke\.ops/attention-since}'

# See all needs-attention annotations on a node
kubectl describe node <node-name> | grep "lke.ops"
```


---

## Deployment

### Prerequisites

- A running LKE or LKE-E cluster
- `kubectl` configured to talk to the cluster
- Docker or equivalent to build and push the image

### 1. Build and push the image

```bash
docker build -t your-registry/lke-node-healer:latest .
docker push your-registry/lke-node-healer:latest
```

### 2. Update the image reference

Edit `k8s/deployment.yaml` and replace:

```yaml
image: your-registry/lke-node-healer:latest
```

### 3. Apply RBAC

```bash
kubectl apply -f k8s/rbac.yaml
```

### 4. Deploy

```bash
kubectl apply -f k8s/deployment.yaml
```

### 5. Verify

```bash
kubectl -n node-healer get pods
kubectl -n node-healer logs -f deployment/node-healer
```

Expected startup output:

```
LKE Node Healer started | DRY_RUN=false | EXCLUDE=none | mode=annotation-only (no auto-remediation)
startup sync — no previously annotated nodes found
Starting node watch stream...
```

---

## Configuration

All settings are environment variables in `k8s/deployment.yaml`.

| Variable | Description | Default |
|---|---|---|
| `DRY_RUN` | When `true`, logs intended actions without annotating any nodes | `false` |
| `EXCLUDE_NODES` | Comma-separated node names to never annotate | `""` |

---

## Testing

### Option 1 — DRY_RUN + kubectl cordon (zero risk)

```bash
# Enable dry-run
kubectl -n node-healer set env deployment/node-healer DRY_RUN=true

# Cordon a node
kubectl cordon <node-name>

# Watch logs — should see detection but no annotation set
kubectl -n node-healer logs -f deployment/node-healer

# Uncordon
kubectl uncordon <node-name>

# Disable dry-run
kubectl -n node-healer set env deployment/node-healer DRY_RUN=false
```

### Option 2 — Full test with annotation

```bash
# Cordon a node
kubectl cordon <node-name>

# Verify annotation is set
kubectl get node <node-name> -o jsonpath='{.metadata.annotations.lke\.ops/needs-attention}'
# output: true

# Uncordon the node
kubectl uncordon <node-name>

# Verify annotation is cleared
kubectl get node <node-name> -o jsonpath='{.metadata.annotations.lke\.ops/needs-attention}'
# output: (empty)
```

### Option 3 — Test startup sync (restart recovery)

```bash
# Cordon a node and let the script annotate it
kubectl cordon <node-name>

# Restart the script pod (simulates pod rescheduling after node failure)
kubectl -n node-healer rollout restart deployment/node-healer

# Watch logs on restart — should see sync picking up the existing annotation
kubectl -n node-healer logs -f deployment/node-healer
# Expected: startup sync — found 1 previously annotated node(s): <node-name>

# Uncordon the node — annotation should be cleared correctly
kubectl uncordon <node-name>
```

---

## Clearing Annotations Manually

If the script pod is unavailable and annotations need to be cleared manually:

```bash
kubectl annotate node <node-name> \
  lke.ops/needs-attention- \
  lke.ops/attention-reason- \
  lke.ops/attention-since-
```

---

## File Structure

```
lke-node-healer/
├── node_healer.py        # Main script
├── Dockerfile
├── requirements.txt
└── k8s/
    ├── rbac.yaml         # Namespace, ServiceAccount, ClusterRole, ClusterRoleBinding
    └── deployment.yaml   # Deployment
```
