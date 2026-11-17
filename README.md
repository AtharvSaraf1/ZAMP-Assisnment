# Ledgerline

Ledgerline is an accounts-payable invoice desk. You give it a vendor invoice (PDF). It reads the invoice, checks it against the company's vendors, purchase orders and past invoices, and decides one of three things:

- **Approved**: everything matches, ready to pay.
- **Needs review**: a person must look. The app says who should look and why.
- **Rejected**: must not be paid, for example a duplicate or a fake vendor.

Every step is shown live in the browser while it runs. Every decision comes with the reasons behind it.

## How to start

You need Python 3.11.

```bash
git clone https://github.com/AtharvSaraf1/ZAMP-Assisnment.git
cd ZAMP-Assisnment
python -m venv .venv
.venv\Scripts\activate            # Windows
# source .venv/bin/activate       # macOS / Linux
pip install -r requirements.txt
```

**Optional: turn on the AI reader.** Create a file named `.env` in the project folder with this line:

```
GEMINI_API_KEY=your-key-here
```

Without a key the app still runs, using its built-in text reader. That reader cannot read scanned (image-only) invoices; those go to "Needs review".

**Start the app:**

```bash
uvicorn app.main:app --port 8000
```

Then open http://localhost:8000.

**Use it:**

1. On **Process invoice**, pick one of the sample invoices (or drop your own PDF) and press **Process invoice**.
2. Watch the stages run. The decision is stamped on the invoice, with the reasons and next steps below it.
3. **Runs** lists every invoice processed so far. Click a row to open it. Held invoices can be approved or rejected there.
4. **Purchase orders** shows how much of each PO has been billed.
5. **Reset demo data** clears all runs and restores the starting data. The app remembers past invoices to catch duplicates, so reset before running the same samples again.

## What the code does

An invoice goes through nine steps. They are in `app/pipeline.py`.

| Step | What happens |
|---|---|
| Intake | Opens the PDF and checks whether it has text or is a scanned image |
| Read document | Gemini AI reads the PDF into fixed fields (vendor, GSTIN, invoice number, dates, PO number, lines, tax, total, bank details). If the AI is not available, a local text reader is used |
| Validate document | Checks required fields and that the invoice is addressed to our company. Also checks the GSTIN check digit, that lines + tax add up to the total, the date, and the GST type (CGST+SGST inside the state, IGST from another state) |
| Vendor check | Finds the vendor by GSTIN in the approved vendor list. Flags blocked vendors, a known name with a different GSTIN, and bank details that differ from our records |
| Duplicate check | Catches the same invoice number even when written differently (`INV/0457` = `INV-457`), the same file, or the same amount within 10 days |
| PO match | Finds the purchase order. If the invoice has no PO number, it suggests the best matching open PO but never approves on a guess |
| Line & amount match | Compares each line with the PO: price (2% tolerance), quantity, and total value. Quantity and total value count invoices already billed on the same PO |
| Decision | A rule that must block payment → Rejected; a rule that needs a person → Needs review; nothing raised → Approved. Also drafts an email to the vendor when something is needed from them |
| Record | Saves the result in the invoice ledger so later invoices are checked against it |

The AI only reads the document. The decision is always made by the fixed rules in the code, so the same invoice always gets the same answer. The rule limits are in `app/rules.py`.

## Project structure

```
app/
  main.py            web server and API (FastAPI)
  pipeline.py        the nine steps and the decision rules
  rules.py           tolerance limits and policy text
  identifiers.py     GSTIN check digit, cleaning of invoice/PO numbers and names
  extract/gemini.py  AI reader (Gemini) with a cache per file
  extract/local_parser.py  text reader used when AI is off
  db.py              SQLite storage: runs, live step events, invoice ledger
  master.py          loads vendors and purchase orders
  models.py          the invoice fields every reader fills
web/                 the browser interface (HTML, CSS, JavaScript)
data/master/         sample company data: vendors, purchase orders, past invoices
samples/invoices/    six sample invoice PDFs
scripts/make_samples.py  creates the sample PDFs
tests/               automated tests
```

## Sample invoices

| File | What it shows | Result |
|---|---|---|
| 01 Sharma Office Supplies | Clean invoice that matches its PO exactly | Approved |
| 02 Kaveri Logistics | Second invoice on a PO; together with the first it bills more than the PO | Needs review |
| 03 Iyer Facility | An already-paid invoice sent again with a new date | Rejected |
| 04 Deccan Packaging | Scanned image, prices include GST, no PO number; the right PO is suggested | Needs review |
| 05 "Sharma Office Supplies" | Known vendor name but a different GSTIN and new bank account | Rejected |
| 06 Mehta Electricals | One line priced 6% above the PO | Needs review |

## Tests

```bash
pytest
```

The tests run every sample through the API and check each rule. They also cover the live step stream, reviewer approval and reset.

## Limits

- Built for Indian invoices (INR, GST). Matches invoice against PO only (no goods-receipt check).
- One invoice per PDF; invoices are uploaded, not read from an email inbox.
- Data is stored in a local SQLite file (`data/app.db`).
