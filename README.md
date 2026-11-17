# Ledgerline: AP invoice desk (Zamp case study, PS-1)

Ledgerline takes a vendor invoice PDF and returns a decision: **Approved**, **Needs review** or **Rejected**.
Every reason behind it is shown, and every stage is visible as it runs.

AI reads the document. Fixed rules make the decision, so every decision can be explained and repeated.

See `PLAN.txt` for the process map, assumptions and edge-case design.

## Run it

```bash
python -m venv .venv
.venv/Scripts/pip install -r requirements.txt        # (Linux/macOS: .venv/bin/pip)
# optional AI reader: put GEMINI_API_KEY=... in .env
.venv/Scripts/python -m uvicorn app.main:app --port 8000
```

Open http://localhost:8000.

- Without a key, the app runs in local-parser mode: text PDFs work, scanned PDFs go to review.
- `python scripts/make_samples.py` rebuilds the demo PDFs.
- `pip install -r requirements-dev.txt && pytest` runs the 40 tests.
- Docker: `docker build -t ledgerline . && docker run -p 8000:8000 -e GEMINI_API_KEY=... ledgerline`

## What happens to an invoice

| Stage | What it does |
|---|---|
| Intake | Opens the PDF, hashes it, and detects whether it is a scanned image with no text layer |
| Read document | Gemini reads the PDF, scans included, into one fixed schema. If the AI fails, a local parser takes over (text PDFs only) |
| Validate document | Checks required fields and that the invoice is billed to our GSTIN. Verifies the GSTIN check digit, re-adds lines + tax = total, checks the date window, and checks GST type (CGST+SGST in-state, IGST out of state) |
| Vendor check | Identifies the vendor by GSTIN (then by name), checks it is not blocked, and compares the bank account to the vendor master |
| Duplicate check | Same invoice number however it is written (`IFS/26-27/0457` = `IFS-26-27-457`), same file, or same amount within 10 days |
| PO match | Cleans the PO number (`2026/0102` → `PO-2026-0102`). With no PO printed, it scores the vendor's open POs and *suggests* one |
| Line & amount match | Pairs invoice lines with PO lines; checks price (2% tolerance) and quantity, both cumulative across earlier invoices; checks PO value (1%) and GST rate |
| Decision | Any reject-level failure → Rejected; else any review-level failure → Needs review; else Approved. Routes the invoice to the PO owner, AP lead or vendor, and drafts the email to the vendor |
| Record | Writes the result to the ledger. Approved invoices use up PO balance. Held invoices count once a reviewer approves them |

## Demo inputs (`samples/invoices/`)

| File | What it tests | Result |
|---|---|---|
| 01 happy path | Clean invoice, exact PO match | Approved |
| 02 split PO | Part 2 of a PO; with part 1 (already approved) it is ₹30,000 / 3 trips over | Needs review |
| 03 duplicate re-send | Paid invoice re-sent with a new date and the number written differently | Rejected |
| 04 scanned, no PO | Image-only scan, prices include GST, no PO printed. AI reads it and PO-2026-0104 is suggested (3 of 3 lines) | Needs review |
| 05 impersonation | Known vendor name with someone else's GSTIN and a "changed" bank account | Rejected |
| 06 price creep (bonus) | One line 6% above PO price | Needs review |

Suggested demo order: 01 → 02 → 03 → 04 → 05.

- Then approve the held run 02 from the run page and watch PO-2026-0102 move on the Purchase orders tab.
- Run 01 again: it is now rejected as a duplicate.
- **Reset demo data** restores the starting ledger.

Reader modes:

- **AI (cached)**: reuses the AI's earlier reading of the same file. It is instant and gives the same answer every time.
- **AI (fresh call)**: calls Gemini live, about 15–20 s.
- **Local parser only**: shows the no-AI fallback.

## Design choices

- **AI reads, rules decide.** The model returns data in a strict schema and is told never to invent or infer values (for example, a PO number). The checks are plain Python, so the same input always gets the same decision with the same reasons.
- **History matters.** Split billing and duplicates can only be caught against a ledger, so the ledger starts seeded with invoices billed before the system existed (`data/master/seed_ledger.csv`).
- **Suggest, don't assume.** An inferred PO is shown with its evidence but always goes to a person.
- **Fraud signals are not the vendor's word.** Identity is by GSTIN. The rejection email goes to the address in our vendor master, not the one printed on the invoice.
- **Live view is real.** Each stage writes an event to SQLite and the browser receives it by Server-Sent Events. There are no fake timers.

## Assumptions and limits

- India, INR, GST. Two-way match (invoice vs PO); goods receipt (three-way match) is not modelled.
- One invoice per PDF. The first page is used for the preview image.
- Intake is by upload. Email inbox intake, ERP posting and payment runs are out of scope.
- The local parser handles common label layouts only. Anything it misses fails a check and goes to review rather than being approved.
- Runs are processed one at a time through the ledger-dependent stages, so two invoices on the same PO cannot both use the same balance.
