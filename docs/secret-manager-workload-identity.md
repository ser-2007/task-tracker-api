# Secret Manager + Workload Identity

**Status:** Verified
**Owner:** Serkan A.

## Summary

An isolated deployment of `task-tracker-api` retrieves a secret from
Google Cloud Secret Manager at runtime using GKE Workload Identity — no
static GCP key file exists anywhere in the container image, the cluster,
or the repository. The pod's identity is a direct, scoped binding between
a Kubernetes `ServiceAccount` and a dedicated GCP `ServiceAccount`, which
in turn has access to exactly one secret and nothing else.

## Why Workload Identity instead of a key file

The conventional alternative — mounting a downloaded GCP service account
JSON key as a Kubernetes `Secret` — means a long-lived credential sitting
on disk inside every pod that needs it: it can be exfiltrated from the
container filesystem, it doesn't expire on its own, and revoking it means
hunting down every place the file was copied to. Workload Identity removes
the file entirely. The pod authenticates by *being* a specific, annotated
Kubernetes ServiceAccount; GKE's metadata server exchanges that identity
for short-lived GCP credentials automatically, and the binding is scoped
to one namespace/ServiceAccount pair, not reusable by any other workload
in the cluster.

## Setup

### 1. Enable Workload Identity on the cluster

Not enabled by default on this cluster — confirmed empty before enabling:

```bash
gcloud container clusters describe devops-portfolio --zone us-central1-a \
  --format="value(workloadIdentityConfig.workloadPool)"
# (empty)

gcloud container clusters update devops-portfolio --zone us-central1-a \
  --workload-pool=project-3bbf5946-29ef-46e1-8ff.svc.id.goog

gcloud container node-pools update medium-pool \
  --cluster devops-portfolio --zone us-central1-a \
  --workload-metadata=GKE_METADATA
```

The node-pool update recreates nodes one at a time to switch them onto the
GKE metadata server; existing pods are rescheduled, nothing is lost.

### 2. Create the secret and a dedicated, narrowly-scoped GCP service account

```bash
gcloud secrets create task-tracker-db-password --data-file=- <<< "..."

gcloud iam service-accounts create task-tracker-secrets-demo \
  --display-name="task-tracker-api secrets demo (Workload Identity)"

gcloud secrets add-iam-policy-binding task-tracker-db-password \
  --member="serviceAccount:task-tracker-secrets-demo@project-3bbf5946-29ef-46e1-8ff.iam.gserviceaccount.com" \
  --role="roles/secretmanager.secretAccessor"
```

This GCP service account can read exactly one secret. It has no project-
wide Secret Manager access, no other IAM roles, and is used by no other
workload.

### 3. Bind a Kubernetes ServiceAccount to it

```bash
gcloud iam service-accounts add-iam-policy-binding \
  task-tracker-secrets-demo@project-3bbf5946-29ef-46e1-8ff.iam.gserviceaccount.com \
  --role roles/iam.workloadIdentityUser \
  --member "serviceAccount:project-3bbf5946-29ef-46e1-8ff.svc.id.goog[default/task-tracker-secrets-demo]"
```

The `--member` is scoped to one exact namespace/ServiceAccount pair
(`default/task-tracker-secrets-demo`) — no other ServiceAccount in the
cluster can assume this identity.

```yaml
# k8s/secrets-demo-serviceaccount.yaml
apiVersion: v1
kind: ServiceAccount
metadata:
  name: task-tracker-secrets-demo
  namespace: default
  annotations:
    iam.gke.io/gcp-service-account: task-tracker-secrets-demo@project-3bbf5946-29ef-46e1-8ff.iam.gserviceaccount.com
```

```bash
kubectl describe sa task-tracker-secrets-demo -n default
```
```
Annotations:  iam.gke.io/gcp-service-account: task-tracker-secrets-demo@project-3bbf5946-29ef-46e1-8ff.iam.gserviceaccount.com
```

### 4. An isolated Deployment using this identity

`k8s/secrets-demo-deployment.yaml` is its own `Deployment`, separate from
the canary `Rollout` (Scenario 2), the OOM demo (Scenario 3), and the
load-test `Deployment` (Scenario 4). It is the only workload in the
cluster with `serviceAccountName: task-tracker-secrets-demo` — no other
pod can reach this secret, by construction, not just by convention.

### 5. Application code

A feature-flagged endpoint (`SECRETS_DEMO_ENABLED`, off everywhere else)
fetches the secret once per process lifetime and caches only a metadata
fingerprint — never the value itself:

```python
@app.route("/secret-status")
def secret_status():
    if not SECRETS_DEMO_ENABLED:
        abort(404)
    status = _fetch_secret_status()
    return jsonify(
        secret_source="gcp-secret-manager",
        secret_name=GCP_SECRET_NAME,
        loaded=status["error"] is None,
        version=status["version"],
        fingerprint=status["fingerprint"],
        error=status["error"],
    ), 200
```

The fingerprint is a truncated SHA-256 of the secret value. It changes if
the secret's content changes, which is enough to prove a real value was
retrieved without the endpoint ever exposing, logging, or returning that
value.

## Verification

```bash
curl -s http://localhost:8083/secret-status
```
```json
{"error":null,"fingerprint":"6bbde1e07fd5","loaded":true,"secret_name":"task-tracker-db-password","secret_source":"gcp-secret-manager","version":"1"}
```

![Successful secret retrieval via Workload Identity](images/secret-status-response.png)

`loaded: true` with no `error` and a populated `version`/`fingerprint`
confirms the full chain worked: pod identity → Workload Identity → GCP
IAM → Secret Manager → back to the pod, with no key file at any point.

## Conclusions

- Workload Identity eliminates the most common failure mode of
  credential-based GCP access in Kubernetes: a static key file that
  outlives its usefulness, gets copied, and is hard to revoke.
- Scoping both the IAM binding (one namespace/ServiceAccount pair) and
  the secret's own IAM policy (one service account, one secret, one role)
  means this workload's blast radius, if compromised, is exactly one
  secret value — not the project's Secret Manager, and not any other
  workload's credentials.
- The pattern generalizes directly to a real credential (a database
  password, an API key) with no code change beyond the secret's name;
  the application never needs to know *how* it authenticated to GCP.
