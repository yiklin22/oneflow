"""Acceptance runs: feed a flow its test set and count what the brief asks to be counted.

Each function returns plain numbers. Time saved is not among them: that needs a
person doing the work both ways, and no person has used this yet.
"""
from __future__ import annotations

import json
from datetime import date
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import orders
from .models import Handoff, Order, User

FIXTURES = Path(__file__).parent.parent / "fixtures"


def run_orders(session: Session, user: User) -> dict:
    root = FIXTURES / "orders"
    orders.import_products(session, user, (root / "products.csv").read_bytes())
    orders.import_customer_parts(session, user, (root / "customer_parts.csv").read_bytes())
    manifest = json.loads((root / "manifest.json").read_text())["orders"]
    out = dict(files=len(manifest), lines=0, lines_missing=0, orders_clean=0, orders_with_flags=0,
               header_wrong=0, duplicates_expected=0, duplicates_caught=0, unreadable_expected=0, unreadable_handed_off=0)
    fields = {"part_no": "part", "qty": "qty", "due_date": "due"}
    for name in fields.values():
        out[name] = dict(right=0, right_flagged=0, left_to_person=0, wrong_flagged=0, wrong_silent=0, silent=[])
    for entry in manifest:
        order, _ = orders.ingest(session, user, entry["file"], (root / "files" / entry["file"]).read_bytes())
        if entry["expect"] == "handoff":
            out["unreadable_expected"] += 1
            out["unreadable_handed_off"] += order.status == "handoff" and bool(session.scalar(
                select(Handoff).where(Handoff.entity == "order", Handoff.entity_id == order.id)))
            continue
        if entry["expect"] == "duplicate":
            out["duplicates_expected"] += 1
            out["duplicates_caught"] += order.status == "duplicate"
        if (order.customer, order.po_no, order.order_date) != (entry["customer"], entry["po_no"], date.fromisoformat(entry["order_date"])):
            out["header_wrong"] += 1
        got = {l.line_no: l for l in order.lines}
        flagged = False
        for truth in entry["lines"]:
            out["lines"] += 1
            line = got.get(truth["line_no"])
            if line is None:
                out["lines_missing"] += 1
                continue
            flagged = flagged or bool(line.issues)
            for attr, name in fields.items():
                value = getattr(line, attr)
                value = int(value) if attr == "qty" and value is not None else value.isoformat() if isinstance(value, date) else value
                want = truth[{"due_date": "due"}.get(attr, attr)]
                issues = [i for i in line.issues if i["field"] == attr]
                stopped = any(i["blocking"] for i in issues)
                if want is None or value is None:
                    kind = "left_to_person" if stopped else "wrong_silent"
                elif value == want:
                    kind = "right_flagged" if issues else "right"
                else:
                    kind = "wrong_flagged" if issues else "wrong_silent"
                out[name][kind] += 1
                if kind == "wrong_silent":
                    out[name]["silent"].append(f"{entry['file']} line {truth['line_no']}: got {value}, want {want}")
        if entry["expect"] == "review":
            out["orders_with_flags" if flagged or order.issues else "orders_clean"] += 1
    return out
