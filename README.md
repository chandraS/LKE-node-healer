LKE Node Healer
Overview
A single Python script running as a Kubernetes Deployment that watches all nodes in the cluster. When a node becomes unhealthy and stays that way for 60 seconds, the script starts a removal sequence, pre-provisioning a replacement, draining the bad node, and deleting the Linode instance. LKE auto-heal keeps the pool count correct automatically.

How It Works
Step 1: Watching nodes
The script opens a persistent streaming connection to the Kubernetes API server. The API server pushes an event to the script every time any node's status changes, there is no polling loop. Under normal operation this stream stays open indefinitely, receiving kubelet heartbeat events every 10 seconds per node.

Step 2: Detecting unhealthy conditions
On each event, the script checks the node's conditions. A node is considered unhealthy if any of the following are true:
•	Ready = False or Unknown
•	DiskPressure = True
•	MemoryPressure = True
•	NetworkUnavailable = True
•	PIDPressure = True

Conditions caused by normal node startup (CNI not initialised, CSI not ready) or shutdown (Calico shutting down) are ignored since these are not genuine failures.

Step 3: Pre-provisioning a replacement (t = 0)
The moment a node is first seen as unhealthy, the script calls the Linode API to resize the node pool by 1. This starts provisioning a replacement node immediately, before the 60-second threshold even expires. The pool count goes from N to N+1.

If the pool is already at its autoscaler maximum, the script raises the autoscaler max by 1 first, then resizes the count. This allows the extra node to be provisioned even when the pool is full.


Step 4: Waiting for the threshold (60 seconds)
The script waits to see if the node recovers on its own. The 60-second timer started when the node was first seen as unhealthy. If the node recovers before 60 seconds, the pre-provisioned node is removed and everything returns to normal.

Step 5: Node removal sequence
If the node is still unhealthy after 60 seconds, the script starts the removal sequence:
1.	Wait for the replacement node to be Ready, provisioning started at t=0 so it has a head start.
2.	Cordon the bad node, the scheduler stops placing new pods on it.
3.	Drain the bad node, all existing pods are evicted and immediately reschedule onto the replacement node.
4.	Delete the Linode instance via the Linode API.
5.	Restore the autoscaler max if it was raised. The pool count drops back to the original value automatically when the instance is deleted.



Deployment

1. Build and push the image
docker build -t your-registry/lke-node-healer:latest .
docker push your-registry/lke-node-healer:latest

2. Update the image reference
Edit k8s/deployment.yaml and replace the image field:
image: your-registry/lke-node-healer:latest

3. Apply RBAC
kubectl apply -f k8s/rbac.yaml

4. Create the Linode API token secret
kubectl -n node-healer create secret generic linode-api-token \
  --from-literal=token=YOUR_LINODE_TOKEN_HERE

5. Set the cluster ID
Find your cluster ID in the Linode Cloud Manager URL or from the node names (lke{clusterID}-{poolID}-{hash}). Set it in the deployment:
kubectl -n node-healer set env deployment/node-healer LINODE_CLUSTER_ID=270250

6. Deploy
kubectl apply -f k8s/deployment.yaml

7. Verify
kubectl -n node-healer get pods
kubectl -n node-healer logs -f deployment/node-healer

Expected output on startup:
LKE Node Healer started | DRY_RUN=false | THRESHOLD=60s | COOLDOWN=600s | CLUSTER=270250
Starting node watch stream...


Configuration
All settings are environment variables in k8s/deployment.yaml. 

•	UNHEALTHY_THRESHOLD_SECONDS - how long a node must stay unhealthy before removal starts. Default 60.
•	COOLDOWN_SECONDS - how long to ignore a node after deletion, prevents acting on the same node twice. Default 600.
•	NEW_NODE_READY_TIMEOUT - how long to wait for the replacement node to become Ready. Default 300.
•	DRAIN_TIMEOUT - how long kubectl drain waits before giving up. Default 120.
•	LINODE_CLUSTER_ID - LKE cluster ID. Required for pre-provisioning.
•	LINODE_TOKEN - Linode API token. Injected from the Kubernetes Secret.
•	EXCLUDE_NODES - comma-separated node names to never touch.
•	DRY_RUN - set to true to log all actions without calling any API. Safe for testing.




Testing
The safest way to test is to set DRY_RUN=true first, then power off a worker node from Linode Cloud Manager. Watch the logs to confirm detection, threshold counting, and the removal sequence. Then set DRY_RUN=false and repeat with a real node.

kubectl -n node-healer set env deployment/node-healer DRY_RUN=true

To revert:
kubectl -n node-healer set env deployment/node-healer DRY_RUN=false
