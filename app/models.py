"""The one schema every invoice is read into, whatever the vendor's layout."""

from typing import Literal

from pydantic import BaseModel, Field


class LineItem(BaseModel):
    description: str
    quantity: float | None = None
    unit_price: float | None = Field(None, description="Price per unit exactly as printed on the invoice")
    amount: float | None = Field(None, description="Line amount exactly as printed")


class TaxLine(BaseModel):
    tax_type: Literal["CGST", "SGST", "IGST", "GST", "OTHER"]
    rate_percent: float | None = None
    amount: float | None = None


class InvoiceExtraction(BaseModel):
    vendor_name: str | None = None
    vendor_gstin: str | None = None
    vendor_address: str | None = None
    buyer_name: str | None = None
    buyer_gstin: str | None = None
    invoice_number: str | None = None
    invoice_date: str | None = Field(None, description="ISO date YYYY-MM-DD")
    due_date: str | None = Field(None, description="ISO date YYYY-MM-DD")
    po_reference: str | None = Field(None, description="Purchase order number exactly as printed, or null if absent")
    currency: str | None = Field(None, description="ISO currency code, e.g. INR")
    tax_inclusive: bool = Field(False, description="True if line prices already include GST")
    line_items: list[LineItem] = Field(default_factory=list)
    subtotal: float | None = Field(None, description="Amount before tax; null if the invoice does not show one")
    tax_lines: list[TaxLine] = Field(default_factory=list)
    total: float | None = Field(None, description="Grand total payable")
    bank_account_number: str | None = None
    bank_ifsc: str | None = None
    unreadable_notes: list[str] = Field(
        default_factory=list, description="Anything that could not be read with confidence, in plain words"
    )
