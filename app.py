"""
Task Tracker API — a small, genuinely functional Flask service used as the
deploy target for the GitOps/canary pipeline (Scenario 2). It is intentionally
real: a SQLite-backed CRUD service with its own business logic, not a
hello-world placeholder, so canary comparisons (v1 vs v2) are meaningful.
"""
import hashlib
import os
import random
import sqlite3
from datetime import datetime, timezone

from flask import Flask, jsonify, request, abort
from prometheus_flask_exporter import PrometheusMetrics

APP_VERSION = os.environ.get("APP_VERSION", "v1")

# Fault injection, off by default (FAULT_RATE=0). Used once, deliberately,
# to ship a broken canary and prove the AnalysisTemplate catches it and
# Argo Rollouts aborts automatically — see README "Rollback proof".
FAULT_RATE = float(os.environ.get("FAULT_RATE", "0"))

# Memory-leak endpoint, off by default. Used once, deliberately, on a
# separate low-memory-limit Deployment (k8s/oom-demo.yaml) to trigger and
# document a real OOMKilled event — see docs/oomkilled-postmortem.md.
# Never enabled on the main canary Rollout.
LEAK_ENABLED = os.environ.get("ENABLE_LEAK_ENDPOINT", "false").lower() == "true"
_leak_store = []

# Secret Manager demo, off by default. Used once, deliberately, on a
# separate Deployment (k8s/secrets-demo-deployment.yaml) bound to a
# dedicated Kubernetes ServiceAccount via Workload Identity -- no static
# GCP key file anywhere in the image or the cluster. See
# docs/secret-manager-workload-identity.md.
SECRETS_DEMO_ENABLED = os.environ.get("SECRETS_DEMO_ENABLED", "false").lower() == "true"
GCP_PROJECT_ID = os.environ.get("GCP_PROJECT_ID", "")
GCP_SECRET_NAME = os.environ.get("GCP_SECRET_NAME", "task-tracker-db-password")
_secret_cache = {"fetched": False, "fingerprint": None, "version": None, "error": None}


def _fetch_secret_status():
    # Fetched lazily, once, and cached for the life of the process -- not
    # re-fetched on every request. The secret's VALUE is never stored,
    # logged, or returned: only a short SHA-256 fingerprint, which proves a
    # real value was retrieved without exposing it.
    if _secret_cache["fetched"]:
        return _secret_cache
    try:
        from google.cloud import secretmanager

        client = secretmanager.SecretManagerServiceClient()
        name = f"projects/{GCP_PROJECT_ID}/secrets/{GCP_SECRET_NAME}/versions/latest"
        response = client.access_secret_version(request={"name": name})
        value = response.payload.data.decode("utf-8")
        _secret_cache["fingerprint"] = hashlib.sha256(value.encode("utf-8")).hexdigest()[:12]
        _secret_cache["version"] = response.name.rsplit("/", 1)[-1]
        _secret_cache["error"] = None
    except Exception as exc:  # noqa: BLE001 -- surfaced in /secret-status, not raised
        _secret_cache["error"] = str(exc)
    _secret_cache["fetched"] = True
    return _secret_cache

app = Flask(__name__)


def _db_path():
    # Read lazily (not cached at import time) so tests can point this at a
    # temp file per-test without reimporting the module.
    return os.environ.get("DB_PATH", "/data/tasks.db")

# Exposes /metrics in Prometheus format (request counts, latency histograms
# per endpoint/status) — this is what the Rollout's AnalysisTemplate queries
# during canary steps, so the pipeline's pass/fail is based on real traffic.
metrics = PrometheusMetrics(app, group_by="endpoint")
metrics.info("task_tracker_app_info", "Task Tracker API build info", version=APP_VERSION)


def get_db():
    conn = sqlite3.connect(_db_path())
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    db_dir = os.path.dirname(_db_path())
    if db_dir:
        os.makedirs(db_dir, exist_ok=True)
    conn = get_db()
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS tasks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            completed INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            completed_at TEXT
        )
        """
    )
    conn.commit()
    conn.close()


@app.route("/health")
def health():
    if FAULT_RATE and random.random() < FAULT_RATE:
        return jsonify(status="error"), 500
    return jsonify(status="ok"), 200


@app.route("/version")
def version():
    return jsonify(version=APP_VERSION), 200


@app.route("/leak", methods=["POST"])
def leak():
    # Deliberately unbounded growth: each call appends a chunk that is
    # never freed, to reproduce a real memory-leak-style OOMKill against a
    # tight container memory limit. Disabled unless ENABLE_LEAK_ENDPOINT=true.
    if not LEAK_ENABLED:
        abort(404)
    chunk_mb = int(request.args.get("mb", "10"))
    _leak_store.append(bytearray(chunk_mb * 1024 * 1024))
    return jsonify(
        leaked_chunks=len(_leak_store),
        approx_leaked_mb=len(_leak_store) * chunk_mb,
    ), 200


@app.route("/secret-status")
def secret_status():
    # Deliberately never returns the secret value -- only whether a real
    # value was retrieved from Secret Manager, its version, and a
    # fingerprint that proves retrieval without exposing the content.
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


@app.route("/tasks", methods=["GET"])
def list_tasks():
    conn = get_db()
    rows = conn.execute("SELECT * FROM tasks ORDER BY id DESC").fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows]), 200


@app.route("/tasks", methods=["POST"])
def create_task():
    data = request.get_json(silent=True) or {}
    title = data.get("title", "").strip()
    if not title:
        abort(400, description="title is required")

    conn = get_db()
    cur = conn.execute(
        "INSERT INTO tasks (title, completed, created_at) VALUES (?, 0, ?)",
        (title, datetime.now(timezone.utc).isoformat()),
    )
    conn.commit()
    task_id = cur.lastrowid
    row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
    conn.close()
    return jsonify(dict(row)), 201


@app.route("/tasks/<int:task_id>", methods=["GET"])
def get_task(task_id):
    conn = get_db()
    row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
    conn.close()
    if row is None:
        abort(404, description="task not found")
    return jsonify(dict(row)), 200


@app.route("/tasks/<int:task_id>/complete", methods=["POST"])
def complete_task(task_id):
    conn = get_db()
    row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
    if row is None:
        conn.close()
        abort(404, description="task not found")

    conn.execute(
        "UPDATE tasks SET completed = 1, completed_at = ? WHERE id = ?",
        (datetime.now(timezone.utc).isoformat(), task_id),
    )
    conn.commit()
    row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
    conn.close()
    return jsonify(dict(row)), 200


_db_initialized = False


@app.before_request
def _ensure_db_initialized():
    # Lazy, one-time init instead of calling init_db() at import time.
    # Import-time init_db() would try to create /data as soon as the
    # module loads — including during test collection and CI, before
    # anything has a chance to point DB_PATH at a writable location —
    # which is exactly what broke the GitHub Actions run (PermissionError
    # on /data under the CI runner's non-root user).
    global _db_initialized
    if not _db_initialized:
        init_db()
        _db_initialized = True


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080)
