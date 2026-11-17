"""Policy numbers in one place. Shown in the UI so a reviewer can see exactly what the machine applies."""

PRICE_TOLERANCE_PCT = 2.0        # unit price may be up to 2% above PO price (rounding, small freight)
PO_VALUE_TOLERANCE_PCT = 1.0     # cumulative billing may exceed PO value by at most 1%
ARITHMETIC_SLACK = 1.0           # Rs: rounding slack when re-adding lines, taxes and totals
MAX_INVOICE_AGE_DAYS = 180       # GST input credit window guard; older invoices go to a person
NEAR_DUPLICATE_DAYS = 10         # same vendor + same total within this many days = possible re-send
VENDOR_NAME_MATCH = 0.85         # name similarity needed to treat two names as the same company
LINE_MATCH = 0.55                # description similarity needed to pair an invoice line with a PO line
INFER_PO_MIN_SCORE = 0.67        # share of invoice lines that must match an open PO to suggest it

POLICY = [
    ("Unit price variance", f"Up to {PRICE_TOLERANCE_PCT:g}% above PO price passes with a note; more goes to the PO owner."),
    ("PO value", f"All approved invoices on a PO may total at most {PO_VALUE_TOLERANCE_PCT:g}% over the PO value."),
    ("Quantity", "Billed quantity on each PO line (all invoices together) may not exceed the ordered quantity."),
    ("Duplicates", "Same vendor + same invoice number (ignoring separators and leading zeros) is rejected."),
    ("Near duplicates", f"Same vendor + same total within {NEAR_DUPLICATE_DAYS} days under a new number goes to review."),
    ("Vendor identity", "Vendor is identified by GSTIN. A known name with an unknown GSTIN is rejected."),
    ("Bank details", "Bank account different from the vendor master is held for a call-back check."),
    ("Missing PO", "A PO is suggested from the vendor's open POs but never auto-approved."),
    ("GST type", "Same-state vendor must charge CGST + SGST; other-state vendor must charge IGST."),
    ("Invoice age", f"Invoices dated in the future or more than {MAX_INVOICE_AGE_DAYS} days ago go to review."),
]
