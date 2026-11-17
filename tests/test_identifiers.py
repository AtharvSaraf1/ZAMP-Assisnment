from app.identifiers import gstin_problem, invoice_number_key, name_key, po_number_key


def test_gstin_check_digit():
    assert gstin_problem("27AAPFU0939F1ZV") is None          # a published real-format example
    assert gstin_problem("27AAKCS4417M1ZW") is None
    assert "check digit" in gstin_problem("27AAKCS4417M1ZX")  # last character changed
    assert "15" in gstin_problem("27AAKCS4417M1Z")
    assert gstin_problem(None) == "GSTIN is missing"


def test_invoice_numbers_written_differently_are_the_same():
    assert invoice_number_key("IFS/26-27/0457") == invoice_number_key("IFS-26-27-457")
    assert invoice_number_key("inv 0042") == invoice_number_key("INV-42")
    assert invoice_number_key("INV-42") != invoice_number_key("INV-420")


def test_po_number_spellings():
    for spelling in ("PO-2026-0102", "2026/0102", "PO No 2026 102", "po-2026/102"):
        assert po_number_key(spelling) == "PO-2026-0102"
    assert po_number_key("see attached") is None


def test_company_name_ignores_legal_suffixes():
    assert name_key("Sharma Office Supplies Pvt. Ltd.") == name_key("SHARMA OFFICE SUPPLIES PRIVATE LIMITED")
