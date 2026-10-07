import os
import tempfile

import pytest


import app as app_module


@pytest.fixture
def client():
    db_fd, db_path = tempfile.mkstemp()
    os.environ["DB_PATH"] = db_path
    app_module.init_db()

    app_module.app.config["TESTING"] = True
    with app_module.app.test_client() as client:
        yield client

    os.close(db_fd)
    os.unlink(db_path)


def test_health(client):
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.get_json()["status"] == "ok"


def test_version(client):
    resp = client.get("/version")
    assert resp.status_code == 200
    assert "version" in resp.get_json()


def test_create_and_list_task(client):
    resp = client.post("/tasks", json={"title": "Write README"})
    assert resp.status_code == 201
    body = resp.get_json()
    assert body["title"] == "Write README"
    assert body["completed"] == 0

    resp = client.get("/tasks")
    assert resp.status_code == 200
    tasks = resp.get_json()
    assert len(tasks) == 1
    assert tasks[0]["title"] == "Write README"


def test_create_task_requires_title(client):
    resp = client.post("/tasks", json={})
    assert resp.status_code == 400


def test_complete_task(client):
    created = client.post("/tasks", json={"title": "Ship canary demo"}).get_json()
    task_id = created["id"]

    resp = client.post(f"/tasks/{task_id}/complete")
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["completed"] == 1
    assert body["completed_at"] is not None


def test_complete_missing_task_returns_404(client):
    resp = client.post("/tasks/9999/complete")
    assert resp.status_code == 404


def test_get_missing_task_returns_404(client):
    resp = client.get("/tasks/9999")
    assert resp.status_code == 404


def test_leak_endpoint_disabled_by_default(client):
    resp = client.post("/leak")
    assert resp.status_code == 404