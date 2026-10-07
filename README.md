
# Task Tracker API — GitOps CI/CD with Canary Rollout

A small Flask CRUD service deployed to GKE through a full GitOps pipeline:
GitHub Actions builds and scans the image, ArgoCD syncs the manifests, and
Argo Rollouts drives a metrics-gated canary release backed by live
Prometheus queries.

## Architecture

```
developer push → GitHub Actions (test → build → Trivy scan → push to GHCR)
                → bump image tag in k8s/rollout.yaml, commit back to main
                → ArgoCD detects drift, syncs k8s/ to the cluster
                → Argo Rollouts runs a canary release:
                    25% traffic → pause → AnalysisRun (Prometheus query
                    gate on success rate + p95 latency) → 50% → pause → 100%
```

**Stack:** Flask + SQLite, `prometheus-flask-exporter`, Docker, GitHub
Actions, Trivy, GHCR, GKE, ArgoCD, Argo Rollouts, kube-prometheus-stack.

## Service

- `GET/POST /tasks`, `GET /tasks/<id>`, `POST /tasks/<id>/complete`,
  `/health`, `/version`
- SQLite-backed, 7 passing unit tests (`tests/test_app.py`)
- `/metrics` exposes Prometheus counters and latency histograms per
  endpoint/status — this is what the AnalysisTemplate queries during a
  rollout, so the canary gate runs against real request data, not a stub

## Repository layout

```
app.py                          Flask service
tests/test_app.py               Unit tests
Dockerfile
.github/workflows/ci-cd.yml     CI/CD pipeline
k8s/rollout.yaml                Argo Rollouts canary spec
k8s/analysis-template.yaml      Prometheus-backed success-rate/latency gate
k8s/servicemonitor.yaml         Prometheus scrape config
argocd/application.yaml         ArgoCD Application (automated sync)
```

## Setup

### 1. Argo Rollouts controller

```bash
kubectl create namespace argo-rollouts
kubectl apply -n argo-rollouts --server-side \
  -f https://github.com/argoproj/argo-rollouts/releases/latest/download/install.yaml
kubectl get pods -n argo-rollouts
```

The `analysistemplates.argoproj.io` CRD is large enough that a plain
`kubectl apply` fails (`metadata.annotations: Too long: may not be more than 262144 bytes`, from the client-side `last-applied-configuration`
annotation). `--server-side` applies directly against the API server and
avoids the limit.

Install the CLI plugin (macOS, Homebrew):

```bash
brew install argoproj/tap/kubectl-argo-rollouts
```

A manually downloaded binary can mismatch the host architecture and fail
with `exec format error`; Homebrew avoids that.

### 2. Prometheus (kube-prometheus-stack)

```bash
helm repo add prometheus-community https://prometheus-community.github.io/helm-charts
helm repo update
kubectl create namespace monitoring
helm install kube-prometheus-stack prometheus-community/kube-prometheus-stack \
  --namespace monitoring \
  --set grafana.enabled=false \
  --set prometheus.prometheusSpec.resources.requests.memory=256Mi \
  --set prometheus.prometheusSpec.resources.limits.memory=512Mi
```

Grafana is disabled: the AnalysisTemplate queries Prometheus directly over
PromQL, so a dashboard UI isn't required for this pipeline, and the
bundled Grafana pod was unstable on the cluster's node sizing.

### 3. Image registry and CI/CD

1. Push this repository to `ghcr.io`-backed GitHub Actions (Settings →
   Actions → Workflow permissions → **Read and write permissions**, so CI
   can push images and commit manifest updates back to `main`)
2. Push to `main` and watch the Actions tab: `test` → `build-and-push`
   (Docker build, Trivy scan, push to GHCR) → `update-manifest` (bumps the
   image tag in `k8s/rollout.yaml` and commits it back)

![CI/CD pipeline passing](docs/images/ci-cd-green.png)

### 4. ArgoCD Application

```bash
kubectl apply -f argocd/application.yaml
kubectl get application task-tracker-api -n argocd
```

`syncPolicy.automated` (prune + selfHeal) keeps the cluster matched to
`k8s/` without manual syncs.

### 5. ServiceMonitor

```bash
kubectl apply -f k8s/servicemonitor.yaml
kubectl port-forward -n monitoring svc/kube-prometheus-stack-prometheus 9090
```

Confirm the target is up and the `job` label matches what
`k8s/analysis-template.yaml` queries:

![Prometheus target UP for task-tracker-api](docs/images/prometheus-target-up.png)

![Live metric data from the deployed service](docs/images/prometheus-metric-query.png)

### 6. Canary rollout

```bash
kubectl argo rollouts get rollout task-tracker-api --watch
```

A push to `main` that changes the app triggers CI/CD, which updates the
image tag, which ArgoCD syncs, which starts the canary. Steps: `25%` →
60s pause → AnalysisRun (Prometheus-gated) → `50%` → 60s pause → `100%`.

![Rollout completed: Healthy, AnalysisRun Successful](docs/images/rollout-healthy-final.png)

### 7. Rollback proof (next)

Ship a deliberately broken version (artificial latency or error rate in
`/health`) and confirm the AnalysisTemplate catches it, aborting the
rollout and returning all traffic to the stable revision automatically.

## Operational notes

Real issues hit and resolved while building this out, kept here as a
working incident log rather than a sanitized happy path.

**CRD size limit on install.** `kubectl apply` failed on the Argo Rollouts
CRDs with a 262144-byte annotation limit. Fixed with
`kubectl apply --server-side`.

**Node capacity, round one.** On the initial `e2-micro` node, mandatory
GKE system add-ons (`kube-dns`, `gke-metrics-agent`, `kube-state-metrics`,
CSI drivers) alone consumed ~99% of allocatable memory before any
application workload was scheduled. Disabling individual add-ons (e.g.
Cloud Logging) didn't help — GKE's reconciler backfilled the freed
capacity with another managed component within seconds. Resolved by
resizing the node pool to `e2-small`, which dropped memory pressure to
~54%:

```bash
gcloud container node-pools create larger-pool \
  --cluster devops-portfolio --zone us-central1-a \
  --machine-type e2-small --num-nodes 1
kubectl cordon <old-node-name>
kubectl drain <old-node-name> --ignore-daemonsets --delete-emptydir-data
gcloud container node-pools delete default-pool \
  --cluster devops-portfolio --zone us-central1-a
```

**Node capacity, round two.** As ArgoCD, Argo Rollouts, kube-prometheus-stack,
and the application accumulated on a single `e2-small` node, memory
requests climbed to 97% and CPU to 79%. This manifested as
`kubectl port-forward` failing with `error dialing backend: No agent available` — the `konnectivity-agent` (GKE's control-plane↔node tunnel)
was in `CrashLoopBackOff`, and even `kubectl logs` on it failed, since
fetching logs depends on the same broken tunnel. Diagnosed via direct node
SSH + `crictl` (bypassing the API server tunnel entirely), which surfaced
the real cause: `containerd` itself was timing out on CRI calls
(`DeadlineExceeded`) under resource pressure. Resolved by upgrading to
`e2-medium` and, when that still saturated CPU under the combined
workload, resizing the pool to 2 nodes:

```bash
gcloud container clusters resize devops-portfolio \
  --node-pool medium-pool --num-nodes 2 --zone us-central1-a
```

**AnalysisTemplate type mismatch aborted the first real rollout.** The
first live canary run failed with
`invalid operation: >= (mismatched types []float64 and float64)` and Argo
Rollouts correctly aborted the rollout, returning all traffic to stable
with zero user-facing impact. Root cause: Argo Rollouts' Prometheus
provider can parse an instant-vector result as `[]float64` even with a
single series, which then fails type comparison against a scalar
`successCondition`. Fixed by wrapping both PromQL queries in `scalar(...)`.

**`retry` does not re-resolve an updated AnalysisTemplate.** After fixing
the template, retrying the same rollout (`kubectl argo rollouts retry`)
reproduced the identical, pre-fix error three times in a row. Argo
Rollouts resolves and freezes the AnalysisTemplate content the first time
a canary step's analysis phase starts; `retry` replays that frozen copy
rather than re-reading the template. The fix only took effect once a new
revision was shipped (a fresh code change, committed and pushed), which
triggered a new analysis resolution — confirmed by `Successful` with 6/6
measurements passing.

**Trivy found a real, fixable CVE.** With `ignore-unfixed: true` filtering
out upstream-unfixed OS CVEs, one actionable finding remained:
`CVE-2026-103111` in `libpcre2-8-0`, fixed upstream but not yet present in
the base image. Fixed in the `Dockerfile` with
`apt-get update && apt-get upgrade -y` at build time.

## Known limitations

- **SQLite + `emptyDir`**: not shared or persisted across pods/rollouts.
  Acceptable for demonstrating the pipeline; a production setup would use
  Cloud SQL or an equivalent managed database.
- **Basic canary, no traffic-routing layer**: the cluster has no
  Istio/NGINX Ingress, so Argo Rollouts approximates canary weighting via
  replica ratio under a single Service rather than true weighted traffic
  splitting.
-
