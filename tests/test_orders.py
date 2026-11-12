import json

import pytest
from sqlalchemy import select

from oneflow import acceptance, base, orders
from oneflow.base import Refused
from oneflow.models import Change, Order, OrderLine

ROOT = acceptance.FIXTURES / "orders"
MANIFEST = json.loads((ROOT / "manifest.json").read_text())["orders"]


def load_tables(session, admin):
    orders.import_products(session, admin, (ROOT / "products.csv").read_bytes())
    orders.import_customer_parts(session, admin, (ROOT / "customer_parts.csv").read_bytes())


def ingest(session, admin, name):
    return orders.ingest(session, admin, name, (ROOT / "files" / name).read_bytes())[0]


def with_plant(kind):
    """(file, line number) of the first line carrying a planted difficulty."""
    return next((o["file"], l["line_no"]) for o in MANIFEST for l in o["lines"] if kind in l["planted"])


def test_acceptance_set_has_no_silent_errors(session, admin):
    result = acceptance.run_orders(session, admin)
    assert result["files"] == 50 and result["lines"] > 200 and result["lines_missing"] == 0
    assert result["part"]["wrong_silent"] == result["qty"]["wrong_silent"] == result["due"]["wrong_silent"] == 0
    assert result["part"]["wrong_flagged"] == 3            # a real part number of the wrong size: caught by the name
    assert result["duplicates_caught"] == result["duplicates_expected"] == 2
    assert result["unreadable_handed_off"] == result["unreadable_expected"] == 1
    assert result["header_wrong"] == 0 and result["orders_clean"] > 0


def test_same_file_twice_is_one_order(session, admin):
    load_tables(session, admin)
    first = ingest(session, admin, "order-01.pdf")
    again, is_new = orders.ingest(session, admin, "copy.pdf", (ROOT / "files" / "order-01.pdf").read_bytes())
    assert not is_new and again.id == first.id and session.query(Order).count() == 1


def test_real_order_without_consent_is_refused(session, admin):
    with pytest.raises(Refused):
        orders.ingest(session, admin, "x.pdf", (ROOT / "files" / "order-01.pdf").read_bytes(), data_class="real")
    assert session.query(Order).count() == 0


@pytest.mark.parametrize("kind, field, code, blocks", [
    ("typo", "part_no", "no_match", True), ("wrong_valid", "part_no", "name_differs", False),
    ("normalized", "part_no", "normalized", False), ("inactive", "part_no", "inactive", True),
    ("pack", "qty", "unit_converted", False), ("dozen", "qty", "unknown_unit", True), ("bad_qty", "qty", "bad_qty", True),
    ("no_due", "due_date", "bad_date", True), ("due_before", "due_date", "bad_date", True),
    ("bad_date", "due_date", "bad_date", True), ("price", "price", "price_differs", False),
])
def test_each_planted_difficulty_is_flagged(session, admin, kind, field, code, blocks):
    load_tables(session, admin)
    name, line_no = with_plant(kind)
    line = ingest(session, admin, name).lines[line_no - 1]
    assert {"field": field, "code": code, "blocking": blocks} in [{k: i[k] for k in ("field", "code", "blocking")} for i in line.issues]


def test_review_edit_confirm_export_once(session, admin):
    load_tables(session, admin)
    name, line_no = with_plant("typo")
    order = ingest(session, admin, name)
    orders.open_order(session, order)
    with pytest.raises(Refused):
        orders.confirm(session, admin, order.id)
    for line in order.lines:                      # a person resolves what the program would not
        for issue in [i for i in line.issues if i["blocking"]]:
            value = {"part_no": "NT-M4-ZN", "qty": "100", "due_date": "2026/12/31"}[issue["field"]]
            orders.edit_line(session, admin, line.id, issue["field"], value)
    line = order.lines[line_no - 1]
    with pytest.raises(Refused):
        orders.edit_line(session, admin, line.id, "part_no", "NO-SUCH-PART")
    with pytest.raises(Refused):
        orders.edit_line(session, admin, line.id, "due_date", "2020/01/01")
    with pytest.raises(Refused):
        orders.edit_line(session, admin, line.id, "qty", "1,20")
    orders.confirm(session, admin, order.id)
    assert order.confirmed_by == admin.id and order.opened_at <= order.confirmed_at
    changes = session.scalars(select(Change).where(Change.entity == "order_line", Change.entity_id == line.id)).all()
    assert changes and changes[0].user_id == admin.id and changes[0].new == "NT-M4-ZN"
    with pytest.raises(Refused):                  # confirmed means frozen
        orders.edit_line(session, admin, line.id, "qty", "5")

    batch = orders.export_confirmed(session, admin)
    rows = batch.csv.strip().split("\r\n")
    assert rows[0].split(",") == orders.EXPORT_HEADER and len(rows) == 1 + len(order.lines) and "NT-M4-ZN" in batch.csv
    assert session.get(Order, order.id).status == "exported"
    with pytest.raises(Refused):                  # nothing is exported twice
        orders.export_confirmed(session, admin)


def test_duplicate_order_number_cannot_be_confirmed_until_released(session, admin):
    load_tables(session, admin)
    dup = next(o for o in MANIFEST if o["expect"] == "duplicate")
    first = next(o for o in MANIFEST if o["po_no"] == dup["po_no"] and o["expect"] == "review")
    a, b = ingest(session, admin, first["file"]), ingest(session, admin, dup["file"])
    assert b.status == "duplicate" and b.duplicate_of == a.id
    with pytest.raises(Refused):
        orders.confirm(session, admin, b.id)
    with pytest.raises(Refused):
        orders.release_duplicate(session, admin, b.id, "")
    orders.release_duplicate(session, admin, b.id, "客戶確認是追加單")
    assert session.get(Order, b.id).status == "needs_review"


def test_unreadable_file_goes_to_a_person(session, admin):
    order, _ = orders.ingest(session, admin, "note.txt", b"please send 100 screws")
    assert order.status == "handoff" and not order.lines
    with pytest.raises(Refused):
        orders.confirm(session, admin, order.id)


def test_customer_part_numbers_only_match_for_that_customer(session, admin):
    load_tables(session, admin)
    catalog = orders.Catalog.load(session)
    (customer, theirs), ours = next(iter(catalog.aliases.items()))
    assert catalog.match(customer, theirs) == (ours, "customer_part", [])
    assert catalog.match("別家公司", theirs)[0] is None
    assert catalog.match(None, "sc m4012 ss")[:2] == ("SC-M4012-SS", "normalized")
    part, kind, candidates = catalog.match(None, "SC-M4013-SS")
    assert part is None and kind == "none" and candidates     # close ones are offered, never chosen
