"""End to end through the HTTP API, using the prepared sample PDFs.

Local-parser mode needs no network, so these always run. The AI-mode test runs only
when a Gemini key is configured.
"""

import pytest

from app.config import get_settings
from tests.conftest import failed, run_sample, wait_for

EXPECTED = {
    "01_happy_sharma_office_supplies.pdf": "approved",
    "02_edge_split_po_overbilled_kaveri.pdf": "review",
    "03_edge_duplicate_resend_iyer.pdf": "rejected",
    "05_edge_vendor_impersonation_sharma.pdf": "rejected",
    "06_edge_price_creep_mehta.pdf": "review",
}


@pytest.mark.parametrize("name,decision", EXPECTED.items())
def test_sample_decisions_local(client, name, decision):
    run = run_sample(client, name, "local")
    assert run["status"] == "completed"
    assert run["decision"] == decision, run["headline"]


def test_happy_path_is_clean(client):
    run = run_sample(client, "01_happy_sharma_office_supplies.pdf")
    assert failed(run) == {}
    assert run["po_number"] == "PO-2026-0101"
    assert run["result"]["po_after"]["used_pct"] == 100.0


def test_split_po_counts_earlier_invoice(client):
    run = run_sample(client, "02_edge_split_po_overbilled_kaveri.pdf")
    assert failed(run) == {"PO value": "review", "Quantities": "review"}
    after = run["result"]["po_after"]
    assert (after["approved_before"], after["this_invoice"], after["after"]) == (250000, 180000, 430000)


def test_duplicate_is_caught_despite_different_spelling(client):
    run = run_sample(client, "03_edge_duplicate_resend_iyer.pdf")
    assert failed(run) == {"Not a duplicate": "reject"}
    assert "IFS-26-27-457" in run["headline"] or "already received" in run["headline"]
    assert run["result"]["vendor_message"] is not None


def test_impersonation_rejected_and_mail_goes_to_master_contact(client):
    run = run_sample(client, "05_edge_vendor_impersonation_sharma.pdf")
    assert failed(run) == {"Approved vendor": "reject", "Bank account": "review"}
    assert run["result"]["vendor_message"]["to"] == "accounts@sharmaoffice.example"


def test_price_creep_names_the_line_and_does_not_double_count(client):
    run = run_sample(client, "06_edge_price_creep_mehta.pdf")
    assert failed(run) == {"Unit prices": "review"}
    warn = [c for c in run["result"]["checks"] if c["label"] == "PO value"][0]
    assert warn["status"] == "warn"


def test_scanned_invoice_without_ai_goes_to_a_person(client):
    run = run_sample(client, "04_edge_scanned_no_po_deccan.pdf", "local")
    assert run["decision"] == "review"
    assert failed(run) == {"Document read": "review"}


def test_same_invoice_twice_is_rejected_second_time(client):
    assert run_sample(client, "01_happy_sharma_office_supplies.pdf")["decision"] == "approved"
    second = run_sample(client, "01_happy_sharma_office_supplies.pdf")
    assert second["decision"] == "rejected"
    assert failed(second)["Not a duplicate"] == "reject"


def test_reset_makes_the_demo_repeatable(client):
    run_sample(client, "01_happy_sharma_office_supplies.pdf")
    assert client.post("/api/reset").json() == {"ok": True}
    assert client.get("/api/runs").json() == []
    assert run_sample(client, "01_happy_sharma_office_supplies.pdf")["decision"] == "approved"


def test_reviewer_approval_updates_po_balance(client):
    held = run_sample(client, "02_edge_split_po_overbilled_kaveri.pdf")
    po = lambda: next(p for p in client.get("/api/reference").json()["purchase_orders"] if p["po_number"] == "PO-2026-0102")
    assert (po()["approved"], po()["pending"]) == (250000, 180000)
    reviewed = client.post(f"/api/runs/{held['id']}/review", json={"action": "approve", "note": "extra trips agreed"})
    assert reviewed.status_code == 200 and reviewed.json()["reviewer_action"] == "approve"
    assert (po()["approved"], po()["pending"]) == (430000, 0)
    again = client.post(f"/api/runs/{held['id']}/review", json={"action": "reject", "note": ""})
    assert again.status_code == 409


def test_only_held_invoices_can_be_reviewed(client):
    approved = run_sample(client, "01_happy_sharma_office_supplies.pdf")
    assert client.post(f"/api/runs/{approved['id']}/review", json={"action": "approve"}).status_code == 409


def test_live_stream_sends_every_stage_then_done(client):
    run_id = client.post("/api/runs", data={"sample": "01_happy_sharma_office_supplies.pdf", "mode": "local"}).json()["id"]
    with client.stream("GET", f"/api/runs/{run_id}/events") as response:
        body = "".join(response.iter_text())
    for stage in ("intake", "extract", "validate", "vendor", "duplicates", "po_match", "lines", "decide", "record"):
        assert f'"stage": "{stage}"' in body
    assert "event: done" in body


def test_rejects_non_pdf_upload(client):
    response = client.post("/api/runs", files={"file": ("notes.txt", b"hello", "text/plain")}, data={"mode": "local"})
    assert response.status_code == 400


def test_user_upload_runs_like_a_sample(client):
    data = (get_settings().samples_dir / "06_edge_price_creep_mehta.pdf").read_bytes()
    response = client.post("/api/runs", files={"file": ("mehta.pdf", data, "application/pdf")}, data={"mode": "local"})
    run = wait_for(client, response.json()["id"])
    assert run["source"] == "upload" and run["decision"] == "review"


@pytest.mark.skipif(not get_settings().ai_configured, reason="no Gemini key configured")
def test_ai_reads_the_scan_and_suggests_the_po(client):
    run = run_sample(client, "04_edge_scanned_no_po_deccan.pdf", "auto")
    assert run["extraction_method"] == "ai"
    assert run["decision"] == "review"
    assert failed(run) == {"PO found": "review"}
    assert run["po_number"] == "PO-2026-0104" and run["result"]["po_method"] == "inferred"
