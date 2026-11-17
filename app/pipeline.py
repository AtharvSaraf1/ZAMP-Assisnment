"""The invoice pipeline: PDF in -> stages -> decision out, with every stage logged as it happens.

Stages: intake -> read -> validate -> vendor -> duplicates -> PO match -> line match -> decide -> record.

Each stage adds Checks. A failed check carries a severity:
  "reject"  -> the invoice must not be paid (duplicate, unknown vendor, impersonation ...)
  "review"  -> a person must look (over-billing, price variance, missing PO ...)
The decision is a fixed rule over the checks; the AI only reads the document.
"""

import json
import logging
import threading
import time
import traceback
from dataclasses import asdict, dataclass, field
from datetime import date, timedelta

import fitz

from app import db, rules
from app.config import get_settings
from app.extract.gemini import AiExtractionError, extract_with_gemini
from app.extract.local_parser import parse_pdf, text_layer_chars
from app.identifiers import (
    STATE_NAMES, clean_gstin, gstin_problem, invoice_number_key, name_key, po_number_key, similarity,
    text_similarity,
)
from app.master import PurchaseOrder, Vendor, load_master
from app.models import InvoiceExtraction

logger = logging.getLogger(__name__)
_ledger_lock = threading.Lock()  # ledger-dependent stages run one invoice at a time

MODES = {"auto": "AI (cached)", "ai_fresh": "AI (fresh call)", "local": "Local parser only"}


def inr(value: float | None) -> str:
    if value is None:
        return "-"
    sign = "-" if value < 0 else ""
    whole, frac = f"{abs(value):.2f}".split(".")
    if len(whole) > 3:
        head, tail, groups = whole[:-3], whole[-3:], []
        while len(head) > 2:
            groups.insert(0, head[-2:])
            head = head[:-2]
        whole = ",".join(([head] if head else []) + groups + [tail])
    return f"{sign}Rs {whole}.{frac}"


@dataclass
class Check:
    stage: str
    label: str
    status: str                      # pass | warn | fail
    detail: str
    severity: str | None = None      # reject | review   (only when status == fail)
    short: str | None = None         # few words for the decision headline
    route: str | None = None         # who should act: vendor | po_owner | ap_lead | onboarding
    vendor_ask: str | None = None    # what to ask the vendor, if anything
    evidence: dict = field(default_factory=dict)


@dataclass
class Context:
    run_id: int
    pdf: bytes
    file_hash: str
    mode: str
    checks: list[Check] = field(default_factory=list)
    pages: int = 0
    scanned: bool = False
    readable: bool = True
    ext: InvoiceExtraction | None = None
    vendor: Vendor | None = None
    identity_mismatch: bool = False
    po: PurchaseOrder | None = None
    po_method: str | None = None
    subtotal_ex_tax: float | None = None
    tax_total: float = 0.0
    line_rows: list[dict] = field(default_factory=list)
    po_after: dict | None = None
    result: dict = field(default_factory=dict)

    def add(self, stage: str, label: str, status: str, detail: str, **extra) -> Check:
        check = Check(stage=stage, label=label, status=status, detail=detail, **extra)
        self.checks.append(check)
        return check

    def fail(self, stage, label, detail, severity, short, **extra) -> Check:
        return self.add(stage, label, "fail", detail, severity=severity, short=short, **extra)


def _day(value: str | None) -> date | None:
    try:
        return date.fromisoformat(value) if value else None
    except ValueError:
        return None


class Skip(Exception):
    """Raised by a stage that cannot run because an earlier stage found nothing to work with."""


# ------------------------------------------------------------------ stages

def stage_intake(ctx: Context) -> tuple[str, dict]:
    try:
        document = fitz.open(stream=ctx.pdf, filetype="pdf")
    except Exception:
        ctx.readable = False
        ctx.fail("intake", "Readable PDF", "The file could not be opened as a PDF.", "review", "file is not a readable PDF",
                 route="vendor", vendor_ask="The attachment could not be opened. Please resend the invoice as a PDF.")
        return "Not a readable PDF", {}
    ctx.pages = document.page_count
    chars = text_layer_chars(document)
    ctx.scanned = chars < 40
    kind = "scanned image, no text layer" if ctx.scanned else f"digital PDF, {chars} characters of text"
    ctx.add("intake", "Readable PDF", "pass", f"{ctx.pages} page(s); {kind}.")
    return f"{ctx.pages} page(s), {kind}", {"pages": ctx.pages, "scanned": ctx.scanned, "text_chars": chars,
                                            "sha256": ctx.file_hash[:16]}


def stage_extract(ctx: Context) -> tuple[str, dict]:
    if not ctx.readable:
        raise Skip("nothing to read")
    company_gstin = load_master().company["gstin"]
    info: dict = {"mode": MODES.get(ctx.mode, ctx.mode)}

    if ctx.mode in ("auto", "ai_fresh"):
        try:
            ctx.ext, ai_info = extract_with_gemini(ctx.pdf, use_cache=ctx.mode == "auto")
            info.update(method="ai", model=ai_info["model"], cached=ai_info["cached"])
        except AiExtractionError as exc:
            info["ai_error"] = str(exc)
            ctx.add("extract", "AI reader available", "warn", f"AI could not read the invoice ({exc}).")

    if ctx.ext is None and not ctx.scanned:
        ctx.ext = parse_pdf(ctx.pdf, company_gstin)
        info["method"] = "local_parser"
        if ctx.mode != "local":
            ctx.add("extract", "Fallback reader", "warn", "Read with the local rule-based parser instead of AI.")

    if ctx.ext is None:
        why = "the local parser cannot read images" if ctx.mode == "local" else "the AI reader was unavailable"
        ctx.fail("extract", "Document read", f"Scanned invoice and {why}. A person must key in the details.",
                 "review", "scanned invoice could not be read", route="ap_lead")
        return "Could not read the scanned invoice", info

    ext = ctx.ext
    filled = sum(1 for value in ext.model_dump().values() if value not in (None, [], ""))
    source = "AI" if info.get("method") == "ai" else "local parser"
    ctx.add("extract", "Document read", "pass",
            f"Read by {source}: {filled} fields, {len(ext.line_items)} line item(s).")
    for note in ext.unreadable_notes:
        ctx.add("extract", "Reader note", "warn", note)
    db.update_run(ctx.run_id, extraction_method=info.get("method"), vendor_name=ext.vendor_name,
                  invoice_number=ext.invoice_number, total=ext.total, extracted_json=ext.model_dump_json())
    cached = " (from cache)" if info.get("cached") else ""
    return f"{source}{cached}: {ext.vendor_name or 'unknown vendor'}, {ext.invoice_number or 'no invoice no.'}, " \
           f"{inr(ext.total)}", {**info, "fields": ext.model_dump()}


def stage_validate(ctx: Context) -> tuple[str, dict]:
    ext = ctx.ext
    if ext is None:
        raise Skip("no extracted data")
    company = load_master().company
    s = "validate"

    # Required fields
    asks = {
        "invoice_number": "an invoice number",
        "invoice_date": "the invoice date",
        "total": "the invoice total",
    }
    missing = [label for name, label in asks.items() if getattr(ext, name) in (None, "")]
    if not ext.line_items:
        missing.append("itemised lines")
    if not (ext.vendor_name or ext.vendor_gstin):
        missing.append("the vendor's name and GSTIN")
    if missing:
        ctx.fail(s, "Required fields", f"Missing on the invoice: {', '.join(missing)}.", "review",
                 f"missing {missing[0]}", route="vendor",
                 vendor_ask=f"Please send a corrected invoice that shows {', '.join(missing)}.")
    else:
        ctx.add(s, "Required fields", "pass", "Invoice number, date, total, vendor and line items are all present.")

    # Addressed to us?
    buyer = clean_gstin(ext.buyer_gstin)
    if buyer and buyer != company["gstin"]:
        ctx.fail(s, "Billed to us", f"Invoice is billed to GSTIN {buyer}, not ours ({company['gstin']}).", "reject",
                 "billed to a different company", route="vendor",
                 vendor_ask=f"The invoice is addressed to GSTIN {buyer}. Please reissue it to {company['name']}, "
                            f"GSTIN {company['gstin']}.")
    elif buyer:
        ctx.add(s, "Billed to us", "pass", f"Buyer GSTIN {buyer} is ours.")
    else:
        ctx.add(s, "Billed to us", "warn", "Our GSTIN is not printed on the invoice; input tax credit may be at risk.")

    # Vendor GSTIN
    problem = gstin_problem(ext.vendor_gstin)
    if problem:
        ctx.fail(s, "Vendor GSTIN valid", f"{problem} ('{ext.vendor_gstin or ''}').", "review", "vendor GSTIN invalid",
                 route="vendor", vendor_ask="The GSTIN on the invoice is missing or invalid. Please confirm your GSTIN.")
    else:
        state = STATE_NAMES.get(clean_gstin(ext.vendor_gstin)[:2], "state " + clean_gstin(ext.vendor_gstin)[:2])
        ctx.add(s, "Vendor GSTIN valid", "pass", f"{clean_gstin(ext.vendor_gstin)} has a valid check digit ({state}).")

    if ext.currency and ext.currency.upper() != "INR":
        ctx.fail(s, "Currency", f"Invoice is in {ext.currency}; this process handles INR only.", "review",
                 "foreign currency", route="ap_lead")

    # Dates
    today = date.today()
    invoice_day = _day(ext.invoice_date)
    if invoice_day:
        age = (today - invoice_day).days
        if age < 0:
            ctx.fail(s, "Invoice date", f"Dated {invoice_day:%d %b %Y}, which is in the future.", "review",
                     "future-dated invoice", route="vendor", vendor_ask="The invoice date is in the future. "
                     "Please send a correctly dated invoice.")
        elif age > rules.MAX_INVOICE_AGE_DAYS:
            ctx.fail(s, "Invoice date", f"Dated {invoice_day:%d %b %Y}, {age} days ago (limit "
                     f"{rules.MAX_INVOICE_AGE_DAYS}).", "review", "invoice is too old", route="ap_lead")
        else:
            ctx.add(s, "Invoice date", "pass", f"Dated {invoice_day:%d %b %Y} ({age} days ago).")

    # Arithmetic: lines, subtotal, tax, total
    slack = rules.ARITHMETIC_SLACK
    problems = []
    for index, item in enumerate(ext.line_items, start=1):
        if None not in (item.quantity, item.unit_price, item.amount):
            if abs(item.quantity * item.unit_price - item.amount) > slack:
                problems.append(f"line {index}: {item.quantity:g} x {inr(item.unit_price)} = "
                                f"{inr(item.quantity * item.unit_price)}, printed {inr(item.amount)}")
    lines_sum = round(sum(item.amount or 0 for item in ext.line_items), 2)
    ctx.tax_total = round(sum(tax.amount or 0 for tax in ext.tax_lines), 2)
    rate = next((tax.rate_percent for tax in ext.tax_lines if tax.rate_percent), None)
    if ext.tax_inclusive:
        if ext.total is not None and abs(lines_sum - ext.total) > slack:
            problems.append(f"lines add up to {inr(lines_sum)} but total says {inr(ext.total)}")
        base_total = ext.total if ext.total is not None else lines_sum
        if not ctx.tax_total and rate:
            ctx.tax_total = round(base_total - base_total / (1 + rate / 100), 2)
        ctx.subtotal_ex_tax = round(base_total - ctx.tax_total, 2)
    else:
        subtotal = ext.subtotal if ext.subtotal is not None else lines_sum
        if ext.line_items and abs(lines_sum - subtotal) > slack:
            problems.append(f"lines add up to {inr(lines_sum)} but sub-total says {inr(subtotal)}")
        if ext.total is not None and abs(subtotal + ctx.tax_total - ext.total) > slack:
            problems.append(f"sub-total {inr(subtotal)} + tax {inr(ctx.tax_total)} = {inr(subtotal + ctx.tax_total)}, "
                            f"but total says {inr(ext.total)}")
        ctx.subtotal_ex_tax = subtotal
    if problems:
        ctx.fail(s, "Totals add up", "Re-adding the invoice does not match: " + "; ".join(problems) + ".", "review",
                 "invoice maths does not add up", route="vendor",
                 vendor_ask="The amounts on the invoice do not add up (" + "; ".join(problems) + "). "
                            "Please send a corrected invoice.")
    else:
        ctx.add(s, "Totals add up", "pass", f"Lines, tax ({inr(ctx.tax_total)}) and total ({inr(ext.total)}) all agree"
                + (" (prices include GST)." if ext.tax_inclusive else "."))

    # GST type: same state -> CGST+SGST, different state -> IGST
    vendor_state = clean_gstin(ext.vendor_gstin)[:2]
    kinds = {tax.tax_type for tax in ext.tax_lines}
    if vendor_state and kinds and kinds != {"GST"}:
        intra = vendor_state == company["state_code"]
        expected = {"CGST", "SGST"} if intra else {"IGST"}
        where = "the same state" if intra else f"another state ({STATE_NAMES.get(vendor_state, vendor_state)})"
        if kinds != expected:
            ctx.fail(s, "GST type", f"Vendor is in {where}, so the invoice should charge {' + '.join(sorted(expected))}; "
                     f"it charges {' + '.join(sorted(kinds))}. Input tax credit would be lost.", "review",
                     "wrong GST type", route="vendor",
                     vendor_ask=f"GST should be charged as {' + '.join(sorted(expected))} for this supply. "
                                "Please reissue the invoice with the correct tax split.")
        else:
            ctx.add(s, "GST type", "pass", f"Vendor in {where} charges {' + '.join(sorted(kinds))}, as expected.")

    failed = [c for c in ctx.checks if c.stage == s and c.status == "fail"]
    return ("All document checks passed" if not failed else f"{len(failed)} problem(s) on the document"), {
        "subtotal_ex_tax": ctx.subtotal_ex_tax, "tax_total": ctx.tax_total}


def stage_vendor(ctx: Context) -> tuple[str, dict]:
    ext = ctx.ext
    if ext is None:
        raise Skip("no extracted data")
    s = "vendor"
    vendors = list(load_master().vendors.values())
    gstin = clean_gstin(ext.vendor_gstin)
    by_gstin = next((v for v in vendors if gstin and v.gstin == gstin), None)
    scored = sorted(((similarity(name_key(ext.vendor_name), name_key(v.legal_name)), v) for v in vendors),
                    key=lambda pair: pair[0], reverse=True)
    name_score, by_name = scored[0] if scored else (0, None)
    if name_score < rules.VENDOR_NAME_MATCH:
        by_name = None

    if by_gstin:
        ctx.vendor = by_gstin
        ctx.add(s, "Approved vendor", "pass", f"GSTIN matches {by_gstin.legal_name} ({by_gstin.vendor_id}).")
        if similarity(name_key(ext.vendor_name), name_key(by_gstin.legal_name)) < rules.VENDOR_NAME_MATCH:
            ctx.add(s, "Vendor name", "warn", f"Name on invoice '{ext.vendor_name}' differs from the master name "
                    f"'{by_gstin.legal_name}'. GSTIN is the stronger identifier, so the match stands.")
    elif by_name and gstin:
        ctx.vendor, ctx.identity_mismatch = by_name, True
        ctx.fail(s, "Approved vendor", f"The name matches approved vendor {by_name.legal_name} ({by_name.vendor_id}), "
                 f"but GSTIN {gstin} is not theirs (master has {by_name.gstin}). This is how vendor impersonation "
                 "looks.", "reject", "GSTIN does not belong to the named vendor", route="ap_lead",
                 vendor_ask=f"We received an invoice in your name carrying GSTIN {gstin}, which does not match our "
                            "records. Please confirm whether you issued it.",
                 evidence={"invoice_gstin": gstin, "master_gstin": by_name.gstin})
    elif by_name:
        ctx.vendor = by_name
        ctx.fail(s, "Approved vendor", f"Matched {by_name.legal_name} by name only; no GSTIN on the invoice to "
                 "confirm identity.", "review", "vendor identified by name only", route="vendor",
                 vendor_ask="Please send the invoice with your GSTIN printed on it.")
    else:
        ctx.fail(s, "Approved vendor", f"'{ext.vendor_name}' (GSTIN {gstin or 'not printed'}) is not in the approved "
                 "vendor master. New vendors must be onboarded before they can be paid.", "reject",
                 "vendor is not approved", route="onboarding",
                 vendor_ask="You are not yet registered as a supplier with us. Please complete vendor onboarding "
                            "before invoicing.")
        return f"Unknown vendor: {ext.vendor_name or gstin or '?'}", {}

    vendor = ctx.vendor
    if not ctx.identity_mismatch:
        db.update_run(ctx.run_id, vendor_name=vendor.legal_name)  # show the master name, not the printed spelling
    if vendor.status != "approved":
        ctx.fail(s, "Vendor status", f"{vendor.legal_name} is marked '{vendor.status}' in the vendor master.",
                 "reject", f"vendor is {vendor.status}", route="ap_lead")
    else:
        ctx.add(s, "Vendor status", "pass", f"{vendor.legal_name} is active; payment terms {vendor.payment_terms_days} days.")

    account = (ext.bank_account_number or "").strip()
    if account and account != vendor.bank_account:
        ctx.fail(s, "Bank account", f"Invoice asks for payment to account ...{account[-4:]} "
                 f"({ext.bank_ifsc or 'IFSC not printed'}); master has ...{vendor.bank_account[-4:]} "
                 f"({vendor.bank_ifsc}). Confirm by calling the vendor on the number in our master, not the one on "
                 "the invoice.", "review", "bank account changed", route="ap_lead",
                 evidence={"invoice_account": account, "master_account": vendor.bank_account})
    elif account:
        ctx.add(s, "Bank account", "pass", f"Account ...{account[-4:]} matches the vendor master.")
    else:
        ctx.add(s, "Bank account", "pass", "No bank details printed; the master account will be used.")

    return f"{vendor.legal_name} ({vendor.vendor_id})" + (", but the GSTIN is not theirs" if ctx.identity_mismatch else ""), {
        "vendor_id": vendor.vendor_id, "legal_name": vendor.legal_name}


def stage_duplicates(ctx: Context) -> tuple[str, dict]:
    ext, vendor = ctx.ext, ctx.vendor
    if ext is None or vendor is None:
        raise Skip("needs a known vendor")
    s = "duplicates"
    key = invoice_number_key(ext.invoice_number) if ext.invoice_number else None
    history = db.ledger_for_vendor(vendor.vendor_id)
    exact = [entry for entry in history if key and entry["invoice_key"] == key]
    live = [entry for entry in exact if entry["status"] in ("approved", "pending")]
    found = False

    if live:
        entry = live[0]
        spelled = "" if entry["invoice_number"] == ext.invoice_number else \
            f" (written '{entry['invoice_number']}' then, '{ext.invoice_number}' now)"
        note = f" - {entry['note']}" if entry.get("note") else ""
        ctx.fail(s, "Not a duplicate", f"Invoice {ext.invoice_number} was already received{spelled}: dated "
                 f"{entry['invoice_date']}, {inr(entry['total'])}, status {entry['status']}{note}.", "reject",
                 "duplicate of an invoice already received", route="vendor",
                 vendor_ask=f"Invoice {entry['invoice_number']} was already received and is {entry['status']}"
                            f"{' (' + entry['note'] + ')' if entry.get('note') else ''}. Please do not resend it.",
                 evidence={"ledger_id": entry["id"]})
        found = True
    elif exact:
        ctx.fail(s, "Not a duplicate", f"We rejected invoice {exact[0]['invoice_number']} earlier; this looks like a "
                 "resubmission. Check that the earlier problem is fixed.", "review", "resubmission of a rejected invoice",
                 route="ap_lead")
        found = True

    same_file = [e for e in db.ledger_by_hash(ctx.file_hash) if e["status"] != "rejected" and e not in exact]
    if same_file and not found:
        ctx.fail(s, "Not a duplicate", f"This exact file was processed before as invoice {same_file[0]['invoice_number']}.",
                 "reject", "same file processed before", route="vendor")
        found = True

    if not found and ext.total is not None and _day(ext.invoice_date):
        for entry in history:
            if entry["status"] == "rejected" or entry["total"] is None or not _day(entry["invoice_date"]):
                continue
            days = abs((_day(ext.invoice_date) - _day(entry["invoice_date"])).days)
            if abs(entry["total"] - ext.total) <= rules.ARITHMETIC_SLACK and days <= rules.NEAR_DUPLICATE_DAYS:
                ctx.fail(s, "Not a duplicate", f"Same vendor, same total {inr(ext.total)}, {days} day(s) apart from "
                         f"invoice {entry['invoice_number']} - possibly the same bill under a new number.", "review",
                         "possible re-send under a new number", route="ap_lead")
                found = True
                break

    if not found:
        ctx.add(s, "Not a duplicate", "pass", f"No earlier invoice from this vendor with the same number, file, or "
                f"amount and date ({len(history)} earlier invoice(s) checked).")
    return ("Duplicate found" if found else f"No duplicate among {len(history)} earlier invoice(s) from this vendor"), {}


def _pair_lines(ctx: Context, po: PurchaseOrder) -> list[tuple[int, int, float]]:
    """Greedy best-first pairing of invoice lines to PO lines by description similarity."""
    pairs = sorted(
        ((text_similarity(item.description, line.description), i, line.line_no)
         for i, item in enumerate(ctx.ext.line_items) for line in po.lines),
        reverse=True,
    )
    used_inv, used_po, result = set(), set(), []
    for score, i, line_no in pairs:
        if score < rules.LINE_MATCH or i in used_inv or line_no in used_po:
            continue
        used_inv.add(i)
        used_po.add(line_no)
        result.append((i, line_no, score))
    return result


def stage_po_match(ctx: Context) -> tuple[str, dict]:
    ext, vendor = ctx.ext, ctx.vendor
    if ext is None or vendor is None:
        raise Skip("needs a known vendor")
    s = "po_match"
    pos = load_master().purchase_orders

    if ext.po_reference:
        key = po_number_key(ext.po_reference)
        po = pos.get(key) if key else None
        if po is None:
            ctx.fail(s, "PO found", f"PO reference '{ext.po_reference}' does not exist in the procurement system.",
                     "review", "PO number not found", route="vendor",
                     vendor_ask=f"The PO number on the invoice ('{ext.po_reference}') is not in our system. "
                                "Please confirm the correct PO number.")
            return f"PO '{ext.po_reference}' not found", {}
        if po.vendor_id != vendor.vendor_id:
            ctx.fail(s, "PO found", f"{po.po_number} was raised on {load_master().vendors[po.vendor_id].legal_name}, "
                     f"not on {vendor.legal_name}.", "reject", "PO belongs to another vendor", route="ap_lead")
            return f"{po.po_number} belongs to another vendor", {}
        ctx.po, ctx.po_method = po, "explicit"
        written = "" if ext.po_reference == po.po_number else f" (printed as '{ext.po_reference}')"
        if po.status != "open":
            ctx.fail(s, "PO found", f"{po.po_number}{written} is {po.status}; nothing more should be billed on it.",
                     "review", "PO is closed", route="po_owner")
        else:
            ctx.add(s, "PO found", "pass", f"{po.po_number}{written}: {po.description} - owner {po.owner}.")
        return f"{po.po_number}{written}, {po.status}", {"po_number": po.po_number, "method": "explicit"}

    # No PO printed: score the vendor's open POs by how many invoice lines they cover.
    candidates = []
    for po in pos.values():
        if po.vendor_id != vendor.vendor_id or po.status != "open":
            continue
        matched = len(_pair_lines(ctx, po))
        share = matched / max(1, len(ext.line_items))
        closeness = 1 - min(1, abs((ctx.subtotal_ex_tax or 0) - po.subtotal) / max(po.subtotal, 1))
        candidates.append({"po_number": po.po_number, "matched": matched, "lines": len(ext.line_items),
                           "share": round(share, 2), "value_closeness": round(closeness, 2), "po": po})
    candidates.sort(key=lambda c: (c["share"], c["value_closeness"]), reverse=True)
    shown = [{k: v for k, v in c.items() if k != "po"} for c in candidates]

    if candidates and candidates[0]["share"] >= rules.INFER_PO_MIN_SCORE:
        best = candidates[0]
        ctx.po, ctx.po_method = best["po"], "inferred"
        runner = f" Next best: {candidates[1]['po_number']} ({candidates[1]['matched']} of {candidates[1]['lines']})." \
            if len(candidates) > 1 else ""
        ctx.fail(s, "PO found", f"No PO number on the invoice. Best match among {len(candidates)} open PO(s) of this "
                 f"vendor: {best['po_number']} - {best['matched']} of {best['lines']} lines match.{runner} "
                 f"Suggested, not assumed: {best['po'].owner} (PO owner) should confirm.", "review",
                 f"no PO on invoice (suggested {best['po_number']})", route="po_owner",
                 evidence={"candidates": shown})
        return f"No PO printed, suggested {best['po_number']} ({best['matched']}/{best['lines']} lines)", {
            "po_number": best["po_number"], "method": "inferred", "candidates": shown}

    ctx.fail(s, "PO found", "No PO number on the invoice and none of the vendor's open POs matches its lines.",
             "review", "no PO and none could be matched", route="vendor",
             vendor_ask="Please send the invoice again quoting our purchase order number.",
             evidence={"candidates": shown})
    return "No PO printed and no open PO matches", {"candidates": shown}


def stage_lines(ctx: Context) -> tuple[str, dict]:
    ext, po = ctx.ext, ctx.po
    if ext is None or po is None:
        raise Skip("needs a matched PO")
    s = "lines"
    consumed = db.po_consumption(po.po_number)
    billed_qty = consumed["approved_qty_by_line"]
    pairs = {i: (line_no, score) for i, line_no, score in _pair_lines(ctx, po)}
    po_lines = {line.line_no: line for line in po.lines}

    rows, unmatched, over_price, small_price, over_qty, unreadable = [], [], [], [], [], []
    price_impact = 0.0
    for i, item in enumerate(ext.line_items):
        row = {"description": item.description, "quantity": item.quantity, "unit_price": item.unit_price,
               "amount": item.amount}
        if i not in pairs:
            unmatched.append(item.description)
            rows.append({**row, "status": "fail", "note": "not on the PO"})
            continue
        line = po_lines[pairs[i][0]]
        if item.unit_price is None or item.quantity is None:
            unreadable.append(item.description)
            rows.append({**row, "status": "fail", "note": "price or quantity not readable", "po_line_no": line.line_no,
                         "po_description": line.description, "po_quantity": line.quantity,
                         "po_unit_price": line.unit_price, "billed_before": billed_qty.get(line.line_no, 0),
                         "amount_ex_tax": item.amount or 0})
            continue
        price = item.unit_price
        if ext.tax_inclusive:
            price = round(price / (1 + line.tax_rate / 100), 2)
        variance = round((price - line.unit_price) / line.unit_price * 100, 2) if line.unit_price else 0
        before = billed_qty.get(line.line_no, 0)
        qty = item.quantity
        status, notes = "pass", []
        if variance > rules.PRICE_TOLERANCE_PCT:
            status = "fail"
            impact = round((price - line.unit_price) * qty, 2)
            price_impact += impact
            over_price.append(f"'{line.description}' at {inr(price)} vs PO {inr(line.unit_price)} (+{variance:g}%, "
                              f"{inr(impact)} extra)")
            notes.append(f"price +{variance:g}%")
        elif variance > 0.1:
            status = "warn"
            small_price.append(f"'{line.description}' +{variance:g}%")
            notes.append(f"price +{variance:g}% (within {rules.PRICE_TOLERANCE_PCT:g}%)")
        if before + qty > line.quantity + 1e-9:
            status = "fail"
            over_qty.append(f"'{line.description}': ordered {line.quantity:g}, already billed {before:g}, this invoice "
                            f"{qty:g} -> {before + qty:g} ({before + qty - line.quantity:g} over)")
            notes.append("quantity over PO")
        rows.append({**row, "status": status, "note": ", ".join(notes) or "matches", "po_line_no": line.line_no,
                     "po_description": line.description, "po_quantity": line.quantity, "po_unit_price": line.unit_price,
                     "unit_price_ex_tax": price, "variance_pct": variance, "billed_before": before,
                     "amount_ex_tax": round(price * qty, 2), "similarity": round(pairs[i][1], 2)})
    ctx.line_rows = rows

    if unmatched:
        ctx.fail(s, "Lines on PO", f"{len(unmatched)} line(s) not found on {po.po_number}: " + "; ".join(unmatched) + ".",
                 "review", "items not on the PO", route="po_owner")
    else:
        ctx.add(s, "Lines on PO", "pass", f"All {len(rows)} line(s) pair with lines on {po.po_number}.")

    if unreadable:
        ctx.fail(s, "Line values readable", "Unit price or quantity could not be read for: " + "; ".join(unreadable)
                 + ". Nothing is approved on a guessed number.", "review", "line values unreadable", route="ap_lead")

    if over_price:
        ctx.fail(s, "Unit prices", "Above the {:g}% tolerance: ".format(rules.PRICE_TOLERANCE_PCT) + "; ".join(over_price)
                 + ".", "review", "price above PO", route="po_owner")
    elif small_price:
        ctx.add(s, "Unit prices", "warn", "Small variance within tolerance: " + "; ".join(small_price) + ".")
    elif rows and not unmatched and not unreadable:
        ctx.add(s, "Unit prices", "pass", "Every unit price equals the PO price"
                + (" (after removing the included GST)." if ext.tax_inclusive else "."))

    # Cumulative value across all invoices on this PO (split billing)
    this_value = ctx.subtotal_ex_tax or 0
    before_value = consumed["approved_subtotal"]
    limit = po.subtotal * (1 + rules.PO_VALUE_TOLERANCE_PCT / 100)
    after = round(before_value + this_value, 2)
    earlier = [inv for inv in consumed["invoices"] if inv["status"] == "approved"]
    earlier_txt = f"{len(earlier)} earlier invoice(s) ({', '.join(e['invoice_number'] for e in earlier)})" if earlier \
        else "no earlier invoices"
    ctx.po_after = {"po_value": po.subtotal, "approved_before": before_value, "this_invoice": this_value,
                    "after": after, "pending": consumed["pending_subtotal"], "used_pct": round(after / po.subtotal * 100, 1)}
    if after > limit and after - po.subtotal <= price_impact + rules.ARITHMETIC_SLACK:
        ctx.add(s, "PO value", "warn", f"{inr(after)} against a PO of {inr(po.subtotal)}; the {inr(after - po.subtotal)} "
                "excess is the price variance already flagged above, not extra quantity.", evidence=ctx.po_after)
    elif after > limit:
        ctx.fail(s, "PO value", f"{po.po_number} is worth {inr(po.subtotal)} (pre-tax). Already approved: "
                 f"{inr(before_value)} from {earlier_txt}. This invoice adds {inr(this_value)}, making {inr(after)}, which is "
                 f"{inr(after - po.subtotal)} over the PO.", "review", "PO value exceeded across invoices",
                 route="po_owner", evidence=ctx.po_after)
    else:
        ctx.add(s, "PO value", "pass", f"After this invoice {inr(after)} of {inr(po.subtotal)} is used "
                f"({ctx.po_after['used_pct']:g}%), counting {earlier_txt}.")

    if over_qty:
        ctx.fail(s, "Quantities", "Billed beyond the ordered quantity: " + "; ".join(over_qty) + ".", "review",
                 "quantity beyond PO", route="po_owner")
    elif rows:
        ctx.add(s, "Quantities", "pass", "Billed quantities, including earlier invoices, stay within the PO.")

    # GST rate vs PO
    if ctx.subtotal_ex_tax and ctx.tax_total:
        effective = round(ctx.tax_total / ctx.subtotal_ex_tax * 100, 2)
        expected = po.lines[0].tax_rate
        if abs(effective - expected) > 0.5:
            ctx.fail(s, "GST rate", f"Invoice charges {effective:g}% GST; the PO items carry {expected:g}%.", "review",
                     "GST rate differs from PO", route="po_owner")
        else:
            ctx.add(s, "GST rate", "pass", f"GST works out to {effective:g}%, as on the PO.")

    bad = sum(1 for row in rows if row["status"] == "fail")
    return (f"{len(rows) - bad} of {len(rows)} line(s) clean, PO {ctx.po_after['used_pct']:g}% used after this"), {
        "rows": rows, "po_after": ctx.po_after}


def stage_decide(ctx: Context) -> tuple[str, dict]:
    fails = [c for c in ctx.checks if c.status == "fail"]
    rejects = [c for c in fails if c.severity == "reject"]
    reviews = [c for c in fails if c.severity == "review"]
    warns = [c for c in ctx.checks if c.status == "warn"]
    ext, vendor, po = ctx.ext, ctx.vendor, ctx.po
    master = load_master()

    if rejects:
        decision = "rejected"
        headline = "Rejected - " + "; ".join(c.short for c in rejects[:2])
    elif reviews:
        decision = "review"
        headline = "Needs review - " + "; ".join(c.short for c in reviews[:2])
    else:
        decision = "approved"
        lines = len(ctx.line_rows)
        headline = f"Approved - matches {po.po_number} on all {lines} line(s)" + (
            f", {len(warns)} note(s) within tolerance" if warns else "")

    # Who acts next
    owners = {"po_owner": po.owner + " (PO owner)" if po else "PO owner", "ap_lead": "AP lead",
              "vendor": "Vendor", "onboarding": "Vendor onboarding team"}
    actions = []
    for check in rejects + reviews:
        who = owners.get(check.route or "", "AP team")
        actions.append({"who": who, "what": check.detail, "label": check.label, "severity": check.severity})

    if decision == "approved":
        terms = vendor.payment_terms_days if vendor else 30
        due = ext.due_date or (_day(ext.invoice_date) + timedelta(days=terms)).isoformat()
        next_step = f"Post to ERP against {po.po_number} and schedule {inr(ext.total)} for payment on {due}."
    elif decision == "review":
        people = sorted({a["who"] for a in actions})
        next_step = f"Hold. Routed to {', '.join(people)}. Payment is blocked until a reviewer approves."
    else:
        next_step = "Do not pay. Return to the vendor with the reasons below."

    message = None
    asks = [c.vendor_ask for c in rejects + reviews if c.vendor_ask]
    if asks and ext:
        to = vendor.contact_email if vendor else "(vendor contact not on file)"
        name = vendor.legal_name if vendor else (ext.vendor_name or "Vendor")
        verb = "cannot be processed" if decision == "rejected" else "is on hold"
        body = [f"Dear {name} team,", "",
                f"Your invoice {ext.invoice_number or '(no number)'}"
                + (f" dated {ext.invoice_date}" if ext.invoice_date else "")
                + (f" for {inr(ext.total)}" if ext.total is not None else "") + f" {verb}:", ""]
        body += [f"  - {ask}" for ask in asks]
        body += ["", "Thank you,", f"Accounts Payable, {master.company['name']}"]
        message = {"to": to, "subject": f"Invoice {ext.invoice_number or ''} - {'returned' if decision == 'rejected' else 'action needed'}",
                   "body": "\n".join(body),
                   "note": "Addressed to the email in our vendor master, not the one printed on the invoice."
                   if ctx.identity_mismatch else None}

    ctx.result = {
        "decision": decision, "headline": headline, "next_step": next_step, "actions": actions,
        "vendor_message": message, "checks": [asdict(c) for c in ctx.checks], "lines": ctx.line_rows,
        "po_after": ctx.po_after, "po_number": po.po_number if po else None, "po_method": ctx.po_method,
        "vendor_id": vendor.vendor_id if vendor else None, "counts": {
            "pass": sum(1 for c in ctx.checks if c.status == "pass"), "warn": len(warns),
            "review": len(reviews), "reject": len(rejects)},
    }
    return headline, {"decision": decision, "next_step": next_step}


def stage_record(ctx: Context) -> tuple[str, dict]:
    ext, vendor = ctx.ext, ctx.vendor
    decision = ctx.result["decision"]
    if not (ext and vendor and ext.invoice_number):
        return "Not added to the ledger (no known vendor or invoice number)", {"recorded": False}
    status = {"approved": "approved", "review": "pending", "rejected": "rejected"}[decision]
    lines = [{"po_line_no": r["po_line_no"], "quantity": r["quantity"] or 0, "amount": r["amount_ex_tax"]}
             for r in ctx.line_rows if "po_line_no" in r]
    db.record_in_ledger(ctx.run_id, {
        "vendor_id": vendor.vendor_id, "invoice_number": ext.invoice_number,
        "po_number": ctx.po.po_number if ctx.po else None, "invoice_date": ext.invoice_date,
        "subtotal": ctx.subtotal_ex_tax, "tax": ctx.tax_total, "total": ext.total,
        "file_hash": ctx.file_hash, "status": status, "note": ctx.result["headline"],
    }, lines)
    effect = {"approved": "counts against the PO balance", "pending": "held; counts once a reviewer approves",
              "rejected": "kept so a re-send is caught"}[status]
    return f"Saved as {status}; {effect}", {"recorded": True, "status": status}


STAGES = [
    ("intake", "Intake", stage_intake),
    ("extract", "Read document", stage_extract),
    ("validate", "Validate document", stage_validate),
    ("vendor", "Vendor check", stage_vendor),
    ("duplicates", "Duplicate check", stage_duplicates),
    ("po_match", "PO match", stage_po_match),
    ("lines", "Line & amount match", stage_lines),
    ("decide", "Decision", stage_decide),
    ("record", "Record", stage_record),
]
LEDGER_STAGES = {"duplicates", "po_match", "lines", "decide", "record"}


def run_pipeline(run_id: int, pdf: bytes, file_hash: str, mode: str) -> None:
    ctx = Context(run_id=run_id, pdf=pdf, file_hash=file_hash, mode=mode)
    started = time.perf_counter()
    locked = False
    try:
        for key, label, stage in STAGES:
            if key in LEDGER_STAGES and not locked:
                _ledger_lock.acquire()
                locked = True
            running = f"Reading the PDF ({MODES.get(mode, mode)})..." if key == "extract" else f"{label}..."
            db.add_event(run_id, key, "running", running)
            t0 = time.perf_counter()
            try:
                message, data = stage(ctx)
                own = [c for c in ctx.checks if c.stage == key]
                state = "fail" if any(c.severity == "reject" for c in own) else \
                    "warn" if any(c.status in ("fail", "warn") for c in own) else "pass"
                if key == "decide":
                    state = {"approved": "pass", "review": "warn", "rejected": "fail"}[ctx.result["decision"]]
            except Skip as reason:
                message, data, state, own = f"Skipped: {reason}", {}, "skipped", []
            data["checks"] = [asdict(c) for c in own]
            db.add_event(run_id, key, state, message, data, int((time.perf_counter() - t0) * 1000))
        result = ctx.result
        db.update_run(run_id, status="completed", decision=result["decision"], headline=result["headline"],
                      vendor_id=result["vendor_id"], po_number=result["po_number"],
                      result_json=json.dumps(result), finished_at=db.now_iso(),
                      duration_ms=int((time.perf_counter() - started) * 1000))
    except Exception as exc:  # a bug, not a business outcome: show it plainly
        logger.error("run %s crashed: %s", run_id, traceback.format_exc())
        db.add_event(run_id, "error", "error", f"Process error: {exc}")
        db.update_run(run_id, status="error", error=str(exc), finished_at=db.now_iso(),
                      duration_ms=int((time.perf_counter() - started) * 1000))
    finally:
        if locked:
            _ledger_lock.release()


def review_run(run_id: int, action: str, note: str) -> dict:
    """A person approves or rejects a held invoice. The ledger follows the human decision."""
    run = db.get_run(run_id)
    if run is None:
        raise LookupError("run not found")
    if run["decision"] != "review" or run["reviewer_action"]:
        raise ValueError("only invoices waiting for review can be decided")
    with _ledger_lock:
        db.set_ledger_status_for_run(run_id, "approved" if action == "approve" else "rejected")
        db.update_run(run_id, reviewer_action=action, reviewer_note=note, reviewed_at=db.now_iso())
        db.add_event(run_id, "review", "pass" if action == "approve" else "fail",
                     f"Reviewer {'approved' if action == 'approve' else 'rejected'}: {note or 'no note'}")
    return db.get_run(run_id)
