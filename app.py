"""
Task Tracker API — a small, genuinely functional Flask service used as the
deploy target for the GitOps/canary pipeline (Scenario 2). It is intentionally
real: a SQLite-backed CRUD service with its own business logic, not a
hello-world placeholder, so canary comparisons (v1 vs v2) are meaningful.
"""
import os
import sqlite3
from datetime import datetime, timezone

from flask import Flask, jsonify, request, abort
from prometheus_flask_exporter import PrometheusMetrics

APP_VERSION = os.environ.get("APP_VERSION", "v1")

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
    return jsonify(status="ok"), 200


@app.route("/version")
def version():
    return jsonify(version=APP_VERSION), 200


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


init_db()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080)
