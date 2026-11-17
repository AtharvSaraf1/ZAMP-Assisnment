"""Fallback reader for PDFs that have a text layer, used when the AI is unavailable or switched off.

It rebuilds visual lines from word positions and applies label-based patterns
("Invoice No", "PO Number", "CGST @9%" ...). It cannot read scanned images; those
need the AI path, and the pipeline routes them to a person if the AI fails.
"""

import re
from datetime import datetime

import fitz

from app.identifiers import GSTIN_PATTERN, clean_gstin
from app.models import InvoiceExtraction, LineItem, TaxLine

MONEY = r"(\d[\d,]*\.\d{2})"
DATE = r"(\d{1,2}[/.-]\d{1,2}[/.-]\d{4})"


def _num(text: str | None) -> float | None:
    return float(text.replace(",", "")) if text else None


def _date(text: str | None) -> str | None:
    if not text:
        return None
    for fmt in ("%d/%m/%Y", "%d-%m-%Y", "%d.%m.%Y"):
        try:
            return datetime.strptime(text, fmt).date().isoformat()
        except ValueError:
            continue
    return None


def _visual_lines(page: fitz.Page) -> list[str]:
    rows: dict[int, list] = {}
    for word in page.get_text("words"):
        rows.setdefault(round(word[3] / 3), []).append(word)
    return [" ".join(w[4] for w in sorted(row, key=lambda w: w[0])) for _, row in sorted(rows.items())]


def _largest_text(page: fitz.Page) -> str | None:
    """The vendor name is usually the biggest text on the page that is not the word 'invoice'."""
    best: tuple[float, str] | None = None
    for block in page.get_text("dict")["blocks"]:
        for line in block.get("lines", []):
            for span in line["spans"]:
                text = span["text"].strip()
                if len(text) < 3 or "INVOICE" in text.upper():
                    continue
                if best is None or span["size"] > best[0]:
                    best = (span["size"], text)
    return best[1] if best else None


def _find(pattern: str, text: str) -> str | None:
    match = re.search(pattern, text, flags=re.IGNORECASE)
    return match.group(1).strip() if match else None


def text_layer_chars(document: fitz.Document) -> int:
    return sum(len(page.get_text().strip()) for page in document)


def parse_pdf(pdf_bytes: bytes, buyer_gstin: str) -> InvoiceExtraction:
    document = fitz.open(stream=pdf_bytes, filetype="pdf")
    page = document[0]
    lines = [line for page_ in document for line in _visual_lines(page_)]
    text = "\n".join(lines)

    gstins = [clean_gstin(g) for g in re.findall(r"\b\d{2}[A-Z0-9]{13}\b", text)]
    gstins = [g for g in gstins if GSTIN_PATTERN.match(g)]
    vendor_gstin = next((g for g in gstins if g != buyer_gstin), None)

    items: list[LineItem] = []
    row = re.compile(rf"^\d{{1,3}}\s+(.+?)\s+(\d[\d,]*(?:\.\d+)?)\s+{MONEY}\s+{MONEY}$")
    for line in lines:
        match = row.match(line)
        if match:
            items.append(LineItem(description=match.group(1), quantity=_num(match.group(2)),
                                  unit_price=_num(match.group(3)), amount=_num(match.group(4))))

    taxes = [
        TaxLine(tax_type=kind.upper(), rate_percent=float(rate), amount=_num(amount))
        for kind, rate, amount in re.findall(rf"\b(CGST|SGST|IGST)\s*@?\s*([\d.]+)\s*%\s+{MONEY}", text, re.IGNORECASE)
    ]

    return InvoiceExtraction(
        vendor_name=_largest_text(page),
        vendor_gstin=vendor_gstin,
        buyer_gstin=buyer_gstin if buyer_gstin in gstins else None,
        invoice_number=_find(r"Invoice\s*(?:No\.?|Number|#)\s*[:#]?\s*([A-Z0-9][A-Z0-9/\-]+)", text)
        or _find(r"(?<!PO )(?<!A/c )\bNo:\s*([A-Z0-9][A-Z0-9/\-]+)", text),
        invoice_date=_date(_find(rf"(?:Invoice\s*Date|Dated|(?<!Due )\bDate)\s*:?\s*{DATE}", text)),
        due_date=_date(_find(rf"(?:Due\s*Date|Payment\s*due|\bDue)\s*:?\s*{DATE}", text)),
        po_reference=_find(r"(?:PO\s*(?:Number|No\.?|#)|P\.O\.\s*(?:Ref|No)\.?|Order\s*Ref|Purchase\s*Order(?:\s*No\.?)?)"
                           r"\s*:?\s*([A-Z0-9][A-Z0-9/\-]*\d)", text),
        currency="INR",
        tax_inclusive=bool(re.search(r"inclusive of GST|incl\.?\s*GST", text, re.IGNORECASE)),
        line_items=items,
        subtotal=_num(_find(rf"(?:Sub-?\s*total|Taxable\s*(?:Value|Amount))\s*:?\s*{MONEY}", text)),
        tax_lines=taxes,
        total=_num(_find(rf"(?:Total\s*Payable|Grand\s*Total|Total\s*Amount|Total\s*\(inclusive of GST\)|Amount\s*Due)"
                         rf"[^\d\n]*{MONEY}", text)),
        bank_account_number=_find(r"A/c\s*No\.?\s*:?\s*(\d{6,18})", text),
        bank_ifsc=_find(r"IFSC\s*:?\s*([A-Z]{4}0[A-Z0-9]{6})", text),
    )
