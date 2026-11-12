"""Run the acceptance sets against an empty database and print the counts.

    uv run python scripts/accept.py [orders|receivables|repairs ...]
"""
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from sqlalchemy import create_engine

from oneflow import acceptance, base, db

flows = sys.argv[1:] or ["orders", "receivables", "repairs"]
with tempfile.TemporaryDirectory() as tmp:
    db.set_engine(create_engine(db.start_embedded(tmp)))
    session = db.new_session()
    user = base.add_user(session, "accept@example.test", "驗收", "admin", "not used")
    for flow in flows:
        print(f"== {flow}")
        print(json.dumps(getattr(acceptance, f"run_{flow}")(session, user), ensure_ascii=False, indent=1, default=str))
    session.close()
    db.get_engine().dispose()
