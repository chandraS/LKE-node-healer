
# LKE Node Healer

## Overview

LKE Node Healer is a single Python script that runs as a Kubernetes Deployment and watches all nodes in an LKE cluster. When a node becomes unhealthy and remains unhealthy for 60 seconds, the script begins a removal workflow: it pre-provisions a replacement node, drains the unhealthy node, and deletes the corresponding Linode instance.

LKE auto-heal then keeps the pool size aligned automatically.

---

## How It Works

### 1. Watch nodes

The script opens a persistent streaming connection to the Kubernetes API server.

Instead of polling, it receives node status change events directly from the API server. Under normal conditions, this stream remains open indefinitely and receives kubelet heartbeat-related updates roughly every 10 seconds per node.

### 2. Detect unhealthy conditions

On each event, the script evaluates the node's conditions.

A node is treated as unhealthy if any of the following is true:

- `Ready = False` or `Unknown`
- `DiskPressure = True`
- `MemoryPressure = True`
- `NetworkUnavailable = True`
- `PIDPressure = True`

The script ignores conditions that happen during normal node startup or shutdown, such as:

- CNI not initialized
- CSI not ready
- Calico shutting down

These are not considered real failure states.

### 3. Pre-provision a replacement (`t = 0`)

As soon as a node is first detected as unhealthy, the script calls the Linode API to increase the node pool size by 1.

This starts provisioning a replacement immediately, even before the 60-second unhealthy threshold has elapsed.

The pool size temporarily changes from:

- `N` → `N + 1`

If the pool is already at its autoscaler maximum, the script first raises the autoscaler maximum by 1, then increases the node count. This ensures a replacement node can still be created even when the pool is at capacity.

### 4. Wait for the threshold (60 seconds)

The script gives the unhealthy node a chance to recover on its own.

The 60-second timer begins when the node is first observed as unhealthy. If the node recovers before the threshold is reached, the pre-provisioned replacement is removed and the cluster returns to its original state.

### 5. Run the node removal sequence

If the node is still unhealthy after 60 seconds, the script begins the removal workflow:

1. Wait for the replacement node to become `Ready`
2. Cordon the unhealthy node so no new pods are scheduled onto it
3. Drain the unhealthy node so existing pods are evicted and rescheduled
4. Delete the Linode instance through the Linode API
5. Restore the autoscaler maximum if it was temporarily increased

When the unhealthy instance is deleted, the pool count returns automatically to its original size.

---

## Deployment

### 1. Build and push the image

```bash
docker build -t your-registry/lke-node-healer:latest .
docker push your-registry/lke-node-healer:latest
````

### 2. Update the image reference

Edit `K8s/deployment.yaml` and replace the image field:

```yaml
image: your-registry/lke-node-healer:latest
```

### 3. Apply RBAC

```bash
kubectl apply -f K8s/rbac.yaml
```

### 4. Create the Linode API token secret

```bash
kubectl -n node-healer create secret generic linode-api-token \
  --from-literal=token=YOUR_LINODE_TOKEN_HERE
```

### 5. Set the cluster ID

Find your cluster ID from either:

* the Linode Cloud Manager URL, or
* the node naming format: `lke{clusterID}-{poolID}-{hash}`

Then set it on the Deployment:

```bash
kubectl -n node-healer set env deployment/node-healer LINODE_CLUSTER_ID=270250
```

### 6. Deploy

```bash
kubectl apply -f K8s/deployment.yaml
```

### 7. Verify

```bash
kubectl -n node-healer get pods
kubectl -n node-healer logs -f deployment/node-healer
```

Expected startup output:

```text
LKE Node Healer started | DRY_RUN=false | THRESHOLD=60s | COOLDOWN=600s | CLUSTER=270250
Starting node watch stream...
```

---

## Configuration

All settings are configured as environment variables in `k8s/deployment.yaml`.

| Variable                      | Description                                                      | Default  |
| ----------------------------- | ---------------------------------------------------------------- | -------- |
| `UNHEALTHY_THRESHOLD_SECONDS` | Time a node must remain unhealthy before removal begins          | `60`     |
| `COOLDOWN_SECONDS`            | Time to ignore a node after deletion to avoid acting on it twice | `600`    |
| `NEW_NODE_READY_TIMEOUT`      | Maximum time to wait for the replacement node to become `Ready`  | `300`    |
| `DRAIN_TIMEOUT`               | How long `kubectl drain` waits before giving up                  | `120`    |
| `LINODE_CLUSTER_ID`           | LKE cluster ID used for pre-provisioning                         | Required |
| `LINODE_TOKEN`                | Linode API token injected from the Kubernetes Secret             | Required |
| `EXCLUDE_NODES`               | Comma-separated list of node names that should never be touched  | Optional |
| `DRY_RUN`                     | When `true`, logs all actions without calling APIs               | `false`  |

---

## Testing

The safest way to test is to enable dry-run mode first.

### Enable dry-run mode

```bash
kubectl -n node-healer set env deployment/node-healer DRY_RUN=true
```

Then manually power off a worker node from Linode Cloud Manager and watch the logs to confirm:

* unhealthy node detection
* threshold timing
* replacement pre-provisioning logic
* removal workflow logging

Once the behavior looks correct, disable dry-run mode and repeat the test with a real node.

### Disable dry-run mode

```bash
kubectl -n node-healer set env deployment/node-healer DRY_RUN=false
```

---

## Expected Behavior Summary

When a node fails:

1. The script detects the unhealthy condition
2. A replacement node is provisioned immediately
3. The script waits 60 seconds to allow recovery
4. If the node does not recover:

   * the replacement must become ready
   * the bad node is cordoned
   * the bad node is drained
   * the Linode instance is deleted
5. The pool returns to its intended size automatically

---




