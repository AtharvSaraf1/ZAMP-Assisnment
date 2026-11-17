"""Build the demo invoice PDFs in samples/invoices/.

Each vendor gets its own layout (as in real life), and one invoice is turned into an
image-only "scan" with no text layer. Run:  .venv/Scripts/python scripts/make_samples.py
"""

import io
import random
import sys
from pathlib import Path

import fitz  # PyMuPDF
from PIL import Image, ImageFilter
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.pdfgen import canvas

OUT = Path(__file__).resolve().parent.parent / "samples" / "invoices"
BUYER = ("Nirmaan Retail Pvt Ltd", "Plot 14, Hinjewadi Phase 2, Pune 411057, Maharashtra", "27AAFCN5521E1ZO")
W, H = A4


def inr(value: float) -> str:
    """Indian digit grouping: 138532.0 -> 1,38,532.00"""
    whole, frac = f"{value:.2f}".split(".")
    if len(whole) > 3:
        head, tail = whole[:-3], whole[-3:]
        groups = []
        while len(head) > 2:
            groups.insert(0, head[-2:])
            head = head[:-2]
        if head:
            groups.insert(0, head)
        whole = ",".join(groups + [tail])
    return f"{whole}.{frac}"


# ---------------------------------------------------------------- layouts

def layout_classic(c: canvas.Canvas, inv: dict) -> None:
    """Dark header band, boxed meta, gridded table. (Sharma Office Supplies)"""
    accent = colors.HexColor(inv.get("accent", "#1f3a5f"))
    c.setFillColor(accent)
    c.rect(0, H - 38 * mm, W, 38 * mm, fill=1, stroke=0)
    c.setFillColor(colors.white)
    c.setFont("Helvetica-Bold", 18)
    c.drawString(18 * mm, H - 17 * mm, inv["vendor"])
    c.setFont("Helvetica", 8.5)
    c.drawString(18 * mm, H - 23 * mm, inv["vendor_address"])
    c.drawString(18 * mm, H - 28 * mm, f"GSTIN: {inv['gstin']}   |   {inv['vendor_email']}")
    c.setFont("Helvetica-Bold", 14)
    c.drawRightString(W - 18 * mm, H - 17 * mm, "TAX INVOICE")

    c.setFillColor(colors.black)
    y = H - 50 * mm
    c.setFont("Helvetica-Bold", 9)
    c.drawString(18 * mm, y, "Bill To")
    c.setFont("Helvetica", 9)
    c.drawString(18 * mm, y - 5 * mm, BUYER[0])
    c.drawString(18 * mm, y - 10 * mm, BUYER[1])
    c.drawString(18 * mm, y - 15 * mm, f"GSTIN: {BUYER[2]}")
    meta = [("Invoice No.", inv["number"]), ("Invoice Date", inv["date"]), ("Due Date", inv["due"])]
    if inv.get("po"):
        meta.append(("PO Number", inv["po"]))
    for i, (label, value) in enumerate(meta):
        c.setFont("Helvetica-Bold", 9)
        c.drawString(125 * mm, y - i * 5 * mm, label)
        c.setFont("Helvetica", 9)
        c.drawString(152 * mm, y - i * 5 * mm, value)
    table(c, inv, top=H - 78 * mm, grid=True, header_fill=colors.HexColor("#e8edf3"))


def layout_minimal(c: canvas.Canvas, inv: dict) -> None:
    """Plain letterhead, rules instead of boxes, meta right-aligned. (Kaveri, Mehta)"""
    c.setFont("Times-Bold", 20)
    c.drawString(18 * mm, H - 22 * mm, inv["vendor"].upper())
    c.setFont("Times-Roman", 9)
    c.drawString(18 * mm, H - 28 * mm, inv["vendor_address"])
    c.drawString(18 * mm, H - 33 * mm, f"GSTIN {inv['gstin']}  -  {inv['vendor_email']}")
    c.setLineWidth(1.2)
    c.line(18 * mm, H - 37 * mm, W - 18 * mm, H - 37 * mm)
    c.setFont("Times-Bold", 12)
    c.drawString(18 * mm, H - 46 * mm, "Invoice")
    c.setFont("Times-Roman", 9.5)
    lines = [f"Invoice # {inv['number']}", f"Dated {inv['date']}", f"Payment due {inv['due']}"]
    if inv.get("po"):
        lines.append(f"{inv.get('po_label', 'PO No:')} {inv['po']}")
    for i, text in enumerate(lines):
        c.drawRightString(W - 18 * mm, H - 46 * mm - i * 5 * mm, text)
    c.drawString(18 * mm, H - 53 * mm, "Billed to:")
    c.drawString(18 * mm, H - 58 * mm, f"{BUYER[0]}, {BUYER[1]}")
    c.drawString(18 * mm, H - 63 * mm, f"GSTIN {BUYER[2]}")
    if inv.get("period"):
        c.drawString(18 * mm, H - 70 * mm, inv["period"])
    table(c, inv, top=H - 80 * mm, grid=False, font="Times-Roman", bold="Times-Bold")


def layout_compact(c: canvas.Canvas, inv: dict) -> None:
    """Two-column header with a coloured left rule. (Iyer, Deccan)"""
    accent = colors.HexColor(inv.get("accent", "#7a3e1d"))
    c.setFillColor(accent)
    c.rect(18 * mm, H - 42 * mm, 2.2 * mm, 26 * mm, fill=1, stroke=0)
    c.setFillColor(colors.black)
    c.setFont("Helvetica-Bold", 15)
    c.drawString(24 * mm, H - 22 * mm, inv["vendor"])
    c.setFont("Helvetica", 8.5)
    c.drawString(24 * mm, H - 28 * mm, inv["vendor_address"])
    c.drawString(24 * mm, H - 33 * mm, f"GSTIN/UIN: {inv['gstin']}")
    c.drawString(24 * mm, H - 38 * mm, inv["vendor_email"])
    c.setFont("Helvetica-Bold", 11)
    c.drawRightString(W - 18 * mm, H - 22 * mm, inv.get("title", "TAX INVOICE"))
    c.setFont("Helvetica", 9)
    rows = [f"No: {inv['number']}", f"Date: {inv['date']}", f"Due: {inv['due']}"]
    if inv.get("po"):
        rows.append(f"Order Ref: {inv['po']}")
    if inv.get("ref_note"):
        rows.append(inv["ref_note"])
    for i, text in enumerate(rows):
        c.drawRightString(W - 18 * mm, H - 28 * mm - i * 5 * mm, text)
    c.setFont("Helvetica-Bold", 9)
    c.drawString(18 * mm, H - 54 * mm, "Buyer")
    c.setFont("Helvetica", 9)
    c.drawString(18 * mm, H - 59 * mm, BUYER[0])
    c.drawString(18 * mm, H - 64 * mm, BUYER[1])
    c.drawString(18 * mm, H - 69 * mm, f"GSTIN/UIN: {BUYER[2]}")
    table(c, inv, top=H - 80 * mm, grid=True, header_fill=colors.HexColor("#f3ece6"))


def table(c, inv, top, grid, header_fill=None, font="Helvetica", bold="Helvetica-Bold"):
    cols = [18 * mm, 28 * mm, 118 * mm, 138 * mm, 170 * mm, W - 18 * mm]  # #, desc, qty, rate, amount, end
    rate_head = "Rate (incl. GST)" if inv.get("inclusive") else "Rate"
    heads = ["#", "Description", "Qty", rate_head, "Amount"]
    row_h = 7 * mm
    if header_fill is not None:
        c.setFillColor(header_fill)
        c.rect(cols[0], top - row_h, cols[-1] - cols[0], row_h, fill=1, stroke=0)
        c.setFillColor(colors.black)
    c.setFont(bold, 9)
    y = top - 5 * mm
    for i, head in enumerate(heads):
        if i >= 2:
            c.drawRightString(cols[i + 1] - 2 * mm, y, head)
        else:
            c.drawString(cols[i] + 1.5 * mm, y, head)
    c.setFont(font, 9)
    for n, (desc, qty, rate) in enumerate(inv["lines"], start=1):
        y -= row_h
        amount = round(qty * rate, 2)
        c.drawString(cols[0] + 1.5 * mm, y, str(n))
        c.drawString(cols[1] + 1.5 * mm, y, desc)
        c.drawRightString(cols[3] - 2 * mm, y, f"{qty:g}")
        c.drawRightString(cols[4] - 2 * mm, y, inr(rate))
        c.drawRightString(cols[5] - 2 * mm, y, inr(amount))
    bottom = y - 3 * mm
    if grid:
        c.setLineWidth(0.4)
        for x in cols:
            c.line(x, top, x, bottom)
        c.line(cols[0], top, cols[-1], top)
        c.line(cols[0], top - row_h, cols[-1], top - row_h)
        c.line(cols[0], bottom, cols[-1], bottom)
    else:
        c.setLineWidth(0.6)
        c.line(cols[0], top - row_h, cols[-1], top - row_h)
        c.line(cols[0], bottom, cols[-1], bottom)

    lines_total = round(sum(round(q * r, 2) for _, q, r in inv["lines"]), 2)
    y = bottom - 7 * mm
    summary = []
    if inv.get("inclusive"):
        summary.append(("Total (inclusive of GST)", lines_total, True))
        summary.append((f"GST @{inv['rate']}% included in above", inv["included_tax"], False))
    else:
        summary.append(("Sub-total", lines_total, False))
        for label, amount in tax_rows(inv, lines_total):
            summary.append((label, amount, False))
        summary.append(("Total Payable (INR)", round(lines_total + sum(a for _, a in tax_rows(inv, lines_total)), 2), True))
    for label, amount, strong in summary:
        c.setFont(bold if strong else font, 10 if strong else 9)
        c.drawRightString(cols[4] - 2 * mm, y, label)
        c.drawRightString(cols[5] - 2 * mm, y, inr(amount))
        y -= 6 * mm

    y -= 6 * mm
    c.setFont(bold, 9)
    c.drawString(18 * mm, y, "Bank details for payment")
    c.setFont(font, 9)
    c.drawString(18 * mm, y - 5 * mm, f"A/c No: {inv['bank'][0]}    IFSC: {inv['bank'][1]}    {inv['bank'][2]}")
    if inv.get("bank_note"):
        c.setFont(bold, 9)
        c.drawString(18 * mm, y - 11 * mm, inv["bank_note"])
    c.setFont(font, 8)
    c.drawString(18 * mm, 22 * mm, inv.get("footer", "This is a computer generated invoice."))
    c.drawRightString(W - 18 * mm, 22 * mm, f"For {inv['vendor']}")


def tax_rows(inv: dict, base: float) -> list[tuple[str, float]]:
    rate = inv["rate"]
    if inv["tax"] == "IGST":
        return [(f"IGST @{rate}%", round(base * rate / 100, 2))]
    half = rate / 2
    return [(f"CGST @{half:g}%", round(base * half / 100, 2)), (f"SGST @{half:g}%", round(base * half / 100, 2))]


LAYOUTS = {"classic": layout_classic, "minimal": layout_minimal, "compact": layout_compact}

# ---------------------------------------------------------------- the demo invoices

INVOICES = {
    "01_happy_sharma_office_supplies.pdf": dict(
        layout="classic", vendor="Sharma Office Supplies Pvt Ltd",
        vendor_address="Shop 6, Laxmi Road, Pune 411030, Maharashtra",
        vendor_email="accounts@sharmaoffice.example", gstin="27AAKCS4417M1ZW",
        number="SOS/2026/1184", date="10/11/2026", due="10/12/2026", po="PO-2026-0101",
        lines=[("A4 copier paper 75 GSM (ream of 500)", 200, 260.00),
               ("Laser toner cartridge HP 88A compatible", 12, 4850.00),
               ("Whiteboard markers (box of 10)", 40, 180.00)],
        tax="CGST+SGST", rate=18, bank=("001205004471", "ICIC0000012", "ICICI Bank, Laxmi Road"),
    ),
    "02_edge_split_po_overbilled_kaveri.pdf": dict(
        layout="minimal", vendor="Kaveri Logistics LLP",
        vendor_address="No. 41, Peenya Industrial Area, Bengaluru 560058, Karnataka",
        vendor_email="billing@kaverilogistics.example", gstin="29AAFFK8823R1ZF",
        number="KL/2026/0987", date="14/11/2026", due="29/12/2026", po="2026/0102", po_label="PO No:",
        period="Freight for trips completed 01/11/2026 to 14/11/2026 (part 2)",
        lines=[("FTL trip Pune to Bengaluru (32 ft MXL)", 18, 10000.00)],
        tax="IGST", rate=18, bank=("50200031184420", "HDFC0001753", "HDFC Bank, Peenya"),
    ),
    "03_edge_duplicate_resend_iyer.pdf": dict(
        layout="compact", accent="#245c4a", vendor="Iyer Facility Services",
        vendor_address="12, Ambattur Industrial Estate, Chennai 600058, Tamil Nadu",
        vendor_email="finance@iyerfacility.example", gstin="33AAIFI6604K1ZC",
        number="IFS/26-27/0457", date="12/11/2026", due="27/11/2026", po="PO-2026-0105",
        lines=[("Housekeeping staff - October (6 persons)", 1, 78000.00),
               ("Housekeeping consumables - October", 1, 24000.00)],
        tax="IGST", rate=18, bank=("7701458820", "UTIB0000288", "Axis Bank, Ambattur"),
    ),
    "04_edge_scanned_no_po_deccan.pdf": dict(
        layout="compact", accent="#7a3e1d", vendor="Deccan Packaging Industries",
        vendor_address="Gat No. 211, Chakan MIDC, Pune 410501, Maharashtra",
        vendor_email="ar@deccanpack.example", gstin="27AADFD2290P1Z3",
        number="DPI-2611-0319", date="07/11/2026", due="07/12/2026", po=None,
        ref_note="Ref: Your mail dt. 18/10/2026",
        lines=[("Corrugated box 3-ply 18x12x10 inch", 2000, 21.83),
               ("BOPP tape 48mm x 65m", 300, 49.56),
               ("Stretch film roll 18 inch 23 micron", 50, 755.20)],
        inclusive=True, rate=18, included_tax=14688.00, tax="CGST+SGST",
        bank=("39811020005512", "SBIN0004012", "State Bank of India, Chakan"),
        footer="Rates are inclusive of GST. Goods once sold will not be taken back.",
        scan=True,
    ),
    "05_edge_vendor_impersonation_sharma.pdf": dict(
        layout="classic", accent="#2b4a6f", vendor="Sharma Office Supplies Pvt. Ltd.",
        vendor_address="Office 3, Market Yard Road, Pune 411037, Maharashtra",
        vendor_email="sharmaoffice.billing@example.net", gstin="27AAGCS9081H1ZX",
        number="SOS/2026/1201", date="13/11/2026", due="20/11/2026", po="PO-2026-0108",
        lines=[("Ergonomic mesh chair with lumbar support", 15, 8900.00)],
        tax="CGST+SGST", rate=18, bank=("50200077123456", "HDFC0000145", "HDFC Bank, Market Yard"),
        bank_note="Please note: our bank account has changed. Kindly pay to the account above.",
    ),
    "06_edge_price_creep_mehta.pdf": dict(
        layout="minimal", vendor="Mehta Electricals & Co",
        vendor_address="22, GIDC Estate, Naroda, Ahmedabad 382330, Gujarat",
        vendor_email="accounts@mehtaelectricals.example", gstin="24AAHFM3315L1ZC",
        number="MEC-2611-042", date="11/11/2026", due="11/12/2026", po="PO-2026-0103", po_label="P.O. Ref:",
        lines=[("LED panel light 2x2 ft 36W", 60, 2279.00),
               ("MCB 32A double pole", 40, 420.00),
               ("FR copper cable 2.5 sq mm (90 m roll)", 10, 3600.00)],
        tax="IGST", rate=18, bank=("912020044518873", "KKBK0002581", "Kotak Mahindra Bank, Naroda"),
    ),
}


def render_text_pdf(inv: dict) -> bytes:
    buffer = io.BytesIO()
    c = canvas.Canvas(buffer, pagesize=A4)
    c.setTitle(f"Invoice {inv['number']}")
    LAYOUTS[inv["layout"]](c, inv)
    c.showPage()
    c.save()
    return buffer.getvalue()


def to_scanned_pdf(pdf_bytes: bytes, seed: int = 7) -> bytes:
    """Rasterise, tilt, add grain and JPEG artefacts, then wrap as an image-only PDF (no text layer)."""
    rng = random.Random(seed)
    page = fitz.open(stream=pdf_bytes, filetype="pdf")[0]
    pix = page.get_pixmap(dpi=140, colorspace=fitz.csGRAY)
    image = Image.frombytes("L", (pix.width, pix.height), pix.samples)
    image = image.rotate(-0.9, resample=Image.BICUBIC, expand=False, fillcolor=246)
    pixels = image.load()
    for _ in range(image.width * image.height // 60):
        x, y = rng.randrange(image.width), rng.randrange(image.height)
        pixels[x, y] = max(0, min(255, pixels[x, y] + rng.randint(-70, 40)))
    image = image.filter(ImageFilter.GaussianBlur(0.55)).point(lambda v: min(255, int(v * 0.96 + 8)))
    jpeg = io.BytesIO()
    image.save(jpeg, format="JPEG", quality=58)
    out = fitz.open()
    new_page = out.new_page(width=page.rect.width, height=page.rect.height)
    new_page.insert_image(new_page.rect, stream=jpeg.getvalue())
    return out.tobytes(deflate=True)


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    for name, inv in INVOICES.items():
        data = render_text_pdf(inv)
        if inv.get("scan"):
            data = to_scanned_pdf(data)
        (OUT / name).write_bytes(data)
        print(f"wrote {name} ({len(data) // 1024} KB)")


if __name__ == "__main__":
    sys.exit(main())
