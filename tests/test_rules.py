"""Rules that have no sample PDF: feed the pipeline a hand-made extraction and check the decision."""

import hashlib

import pytest

from app import db, pipeline
from app.models import InvoiceExtraction, LineItem, TaxLine
from app.config import get_settings

PDF = (get_settings().samples_dir / "01_happy_sharma_office_supplies.pdf").read_bytes()


def base(**changes) -> InvoiceExtraction:
    data = dict(
        vendor_name="Sharma Office Supplies Pvt Ltd", vendor_gstin="27AAKCS4417M1ZW", buyer_gstin="27AAFCN5521E1ZO",
        invoice_number="SOS/2026/2000", invoice_date="2026-11-10", po_reference="PO-2026-0101", currency="INR",
        line_items=[LineItem(description="A4 copier paper 75 GSM (ream of 500)", quantity=100, unit_price=260, amount=26000)],
        subtotal=26000, tax_lines=[TaxLine(tax_type="CGST", rate_percent=9, amount=2340),
                                   TaxLine(tax_type="SGST", rate_percent=9, amount=2340)],
        total=30680, bank_account_number="001205004471",
    )
    data.update(changes)
    return InvoiceExtraction(**data)


def decide(monkeypatch, extraction: InvoiceExtraction, salt: str = "") -> tuple[str, dict]:
    monkeypatch.setattr(pipeline, "extract_with_gemini", lambda pdf, use_cache=True: (extraction, {"model": "test", "cached": False}))
    digest = hashlib.sha256(PDF + salt.encode()).hexdigest()
    run_id = db.create_run("test.pdf", digest, "", "test", "auto")
    pipeline.run_pipeline(run_id, PDF, digest, "auto")
    run = db.get_run(run_id)
    assert run["status"] == "completed", run["error"]
    return run["decision"], {c["label"]: c["severity"] for c in run["result"]["checks"] if c["status"] == "fail"}


def test_partial_invoice_within_po_is_approved(monkeypatch):
    assert decide(monkeypatch, base()) == ("approved", {})


def test_small_price_variance_passes_with_note(monkeypatch):
    ext = base(line_items=[LineItem(description="A4 copier paper 75 GSM", quantity=100, unit_price=264, amount=26400)],
               subtotal=26400, tax_lines=[TaxLine(tax_type="CGST", rate_percent=9, amount=2376),
                                          TaxLine(tax_type="SGST", rate_percent=9, amount=2376)], total=31152)
    assert decide(monkeypatch, ext) == ("approved", {})


def test_wrong_gst_type(monkeypatch):
    ext = base(tax_lines=[TaxLine(tax_type="IGST", rate_percent=18, amount=4680)])
    assert decide(monkeypatch, ext) == ("review", {"GST type": "review"})


def test_totals_that_do_not_add_up(monkeypatch):
    assert decide(monkeypatch, base(total=31680)) == ("review", {"Totals add up": "review"})


def test_missing_invoice_number_asks_the_vendor(monkeypatch):
    decision, fails = decide(monkeypatch, base(invoice_number=None))
    assert decision == "review" and fails["Required fields"] == "review"


def test_future_date(monkeypatch):
    assert decide(monkeypatch, base(invoice_date="2099-01-01")) == ("review", {"Invoice date": "review"})


def test_billed_to_another_company(monkeypatch):
    decision, fails = decide(monkeypatch, base(buyer_gstin="27AAPFU0939F1ZV"))
    assert decision == "rejected" and fails["Billed to us"] == "reject"


def test_unknown_vendor(monkeypatch):
    ext = base(vendor_name="Shree Ganesh Traders", vendor_gstin="27AAPFU0939F1ZV")
    assert decide(monkeypatch, ext) == ("rejected", {"Approved vendor": "reject"})


def test_blocked_vendor(monkeypatch):
    ext = base(vendor_name="Bharat Print House", vendor_gstin="27AABFB7712Q1Z4", po_reference=None,
               bank_account_number=None)
    decision, fails = decide(monkeypatch, ext)
    assert decision == "rejected" and fails["Vendor status"] == "reject"


def test_po_of_another_vendor(monkeypatch):
    decision, fails = decide(monkeypatch, base(po_reference="PO-2026-0102"))
    assert decision == "rejected" and fails["PO found"] == "reject"


def test_closed_po(monkeypatch):
    decision, fails = decide(monkeypatch, base(po_reference="PO-2026-0106"))
    assert decision == "review" and fails["PO found"] == "review"


def test_unknown_po_number(monkeypatch):
    assert decide(monkeypatch, base(po_reference="PO-2026-0999")) == ("review", {"PO found": "review"})


def test_item_not_on_po(monkeypatch):
    ext = base(line_items=[LineItem(description="Coffee machine", quantity=1, unit_price=26000, amount=26000)])
    decision, fails = decide(monkeypatch, ext)
    assert decision == "review" and fails["Lines on PO"] == "review"


def test_near_duplicate_under_new_number(monkeypatch):
    assert decide(monkeypatch, base(), "a")[0] == "approved"
    decision, fails = decide(monkeypatch, base(invoice_number="SOS/2026/2001", invoice_date="2026-11-12"), "b")
    assert decision == "review" and fails["Not a duplicate"] == "review"


def test_resubmitted_rejected_invoice_goes_to_review(monkeypatch):
    assert decide(monkeypatch, base(total=31680), "a")[0] == "review"   # held (maths)
    # a person rejects it, then the vendor sends a corrected one under the same number
    run_id = db.list_runs()[0]["id"]
    pipeline.review_run(run_id, "reject", "totals wrong")
    decision, fails = decide(monkeypatch, base(), "b")
    assert decision == "review" and fails["Not a duplicate"] == "review"


@pytest.mark.parametrize("error", ["the Gemini API key was rejected", "Gemini returned HTTP 503"])
def test_ai_failure_falls_back_to_local_parser(monkeypatch, error):
    def broken(pdf, use_cache=True):
        raise pipeline.AiExtractionError(error)
    monkeypatch.setattr(pipeline, "extract_with_gemini", broken)
    digest = hashlib.sha256(PDF + error.encode()).hexdigest()
    run_id = db.create_run("x.pdf", digest, "", "test", "auto")
    pipeline.run_pipeline(run_id, PDF, digest, "auto")
    run = db.get_run(run_id)
    assert run["extraction_method"] == "local_parser"
    assert run["decision"] == "approved"


@pytest.mark.parametrize("missing", ["unit_price", "quantity"])
def test_unreadable_line_numbers_are_never_approved(monkeypatch, missing):
    item = dict(description="A4 copier paper 75 GSM (ream of 500)", quantity=100, unit_price=260, amount=26000)
    item[missing] = None
    decision, fails = decide(monkeypatch, base(line_items=[LineItem(**item)]), missing)
    assert decision == "review" and fails["Line values readable"] == "review"
