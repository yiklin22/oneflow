"""Fill the local database with the three synthetic test sets and four demo users.

    ONEFLOW_DEMO_PASSWORD=... uv run python scripts/seed_demo.py

Refuses to run on a database that already has users. All data is invented.
"""
import json
import os
import secrets
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from oneflow import acceptance, base, db, orders, receivables, repairs  # noqa: E402
from oneflow.models import User  # noqa: E402

session = db.new_session()
if session.query(User).count():
    sys.exit("這個資料庫已經有使用者，不重複建立示範資料。")
password = os.environ.get("ONEFLOW_DEMO_PASSWORD") or secrets.token_urlsafe(9)
users = [base.add_user(session, f"{who}@example.test", name, role, password)
         for who, name, role in (("admin", "管理者", "admin"), ("sales", "業務助理", "sales"), ("acct", "會計", "accounting"), ("dispatch", "調度", "dispatch"))]
admin = users[0]
root = acceptance.FIXTURES

orders.import_products(session, admin, (root / "orders/products.csv").read_bytes())
orders.import_customer_parts(session, admin, (root / "orders/customer_parts.csv").read_bytes())
for path in sorted((root / "orders/files").iterdir()):
    orders.ingest(session, admin, path.name, path.read_bytes())

receivables.import_export(session, admin, "ar-2026-11-30.csv", (root / "receivables/ar-2026-11-30.csv").read_bytes())
for path in sorted((root / "receivables/mails").iterdir()):
    receivables.import_mail(session, admin, path.name, path.read_bytes())

for entry in json.loads((root / "repairs/enquiries.json").read_text())["enquiries"]:
    at = datetime.fromisoformat(entry["at"])
    answers = {k: list(v) if isinstance(v, list) else [v] for k, v in entry["answers"].items()}
    _, replies = repairs.receive(session, "line", entry["user"], entry["opening"], at=at, event_id=f"seed-{entry['id']}-0")
    for turn in range(1, 12):
        asked = next((f for f, q in repairs.QUESTIONS.items() if replies and q in replies[-1]), None)
        if not asked or not answers.get(asked):
            break
        at += timedelta(minutes=2)
        _, replies = repairs.receive(session, "line", entry["user"], answers[asked].pop(0), at=at, event_id=f"seed-{entry['id']}-{turn}")

print("示範帳號（密碼相同）：", ", ".join(u.email for u in users))
print("密碼：", password if not os.environ.get("ONEFLOW_DEMO_PASSWORD") else "（ONEFLOW_DEMO_PASSWORD）")
print("應收帳款的基準日請選 2026-11-30。")
