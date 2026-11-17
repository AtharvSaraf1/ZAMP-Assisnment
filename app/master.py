"""Procurement master data: our company, approved vendors, purchase orders and PO lines (CSV files)."""

import csv
from dataclasses import dataclass, field
from functools import lru_cache

from app.config import get_settings


@dataclass
class Vendor:
    vendor_id: str
    legal_name: str
    gstin: str
    state_code: str
    bank_account: str
    bank_ifsc: str
    status: str
    payment_terms_days: int
    contact_email: str


@dataclass
class PoLine:
    line_no: int
    description: str
    quantity: float
    unit: str
    unit_price: float
    tax_rate: float

    @property
    def amount(self) -> float:
        return round(self.quantity * self.unit_price, 2)


@dataclass
class PurchaseOrder:
    po_number: str
    vendor_id: str
    po_date: str
    status: str
    description: str
    owner: str
    lines: list[PoLine] = field(default_factory=list)

    @property
    def subtotal(self) -> float:
        return round(sum(line.amount for line in self.lines), 2)


@dataclass
class MasterData:
    company: dict
    vendors: dict[str, Vendor]
    purchase_orders: dict[str, PurchaseOrder]


def _read(name: str) -> list[dict]:
    with open(get_settings().master_dir / name, newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


@lru_cache
def load_master() -> MasterData:
    company = _read("company.csv")[0]
    vendors = {
        row["vendor_id"]: Vendor(**{**row, "payment_terms_days": int(row["payment_terms_days"])})
        for row in _read("vendors.csv")
    }
    pos = {row["po_number"]: PurchaseOrder(**row) for row in _read("purchase_orders.csv")}
    for row in _read("po_lines.csv"):
        pos[row["po_number"]].lines.append(
            PoLine(
                line_no=int(row["line_no"]),
                description=row["description"],
                quantity=float(row["quantity"]),
                unit=row["unit"],
                unit_price=float(row["unit_price"]),
                tax_rate=float(row["tax_rate"]),
            )
        )
    return MasterData(company=company, vendors=vendors, purchase_orders=pos)
