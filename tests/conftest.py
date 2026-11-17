import os
import tempfile
import time

import pytest

# Every test session gets its own empty data folder (database + uploads). The AI cache stays shared.
os.environ["DATA_DIR"] = tempfile.mkdtemp(prefix="ledgerline-test-")

from fastapi.testclient import TestClient  # noqa: E402

from app import db  # noqa: E402
from app.main import app  # noqa: E402


@pytest.fixture(scope="session")
def client():
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture(autouse=True)
def clean_ledger():
    db.init_db()
    db.reset_demo_data()
    yield


def wait_for(client, run_id: int, timeout: float = 120) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        run = client.get(f"/api/runs/{run_id}").json()
        if run["status"] != "running":
            return run
        time.sleep(0.2)
    raise TimeoutError(f"run {run_id} did not finish")


def run_sample(client, name: str, mode: str = "local") -> dict:
    response = client.post("/api/runs", data={"sample": name, "mode": mode})
    assert response.status_code == 200, response.text
    return wait_for(client, response.json()["id"])


def failed(run: dict) -> dict[str, str]:
    """label -> severity of every failed check."""
    return {c["label"]: c["severity"] for c in run["result"]["checks"] if c["status"] == "fail"}
