import pytest
from sqlalchemy import create_engine, text

from oneflow import base, db
from oneflow.models import Base

PASSWORD = "correct horse battery"
_HASH = base.hash_password(PASSWORD)          # scrypt is slow on purpose; hash once


def wipe(engine):
    with engine.begin() as conn:
        conn.execute(text("TRUNCATE " + ", ".join(t.name for t in Base.metadata.sorted_tables) + " RESTART IDENTITY CASCADE"))


def add_users(session):
    from oneflow.models import User
    for email, name, role in (("admin@example.test", "管理者", "admin"), ("sales@example.test", "業務助理", "sales"),
                              ("acct@example.test", "會計", "accounting"), ("dispatch@example.test", "調度", "dispatch")):
        session.add(User(email=email, name=name, role=role, password_hash=_HASH))
    session.commit()
    return session.get(User, 1)


@pytest.fixture(scope="session")
def engine(tmp_path_factory):
    """A real PostgreSQL for the test run, started from the pgserver package."""
    engine = create_engine(db.start_embedded(tmp_path_factory.mktemp("pg")))
    db.set_engine(engine)
    return engine


@pytest.fixture
def session(engine):
    wipe(engine)
    s = db.new_session()
    yield s
    s.close()


@pytest.fixture
def admin(session):
    return add_users(session)


@pytest.fixture
def client(session, admin):
    from fastapi.testclient import TestClient

    from oneflow.app import app
    with TestClient(app, base_url="http://testserver") as c:
        r = c.post("/login", data={"email": "admin@example.test", "password": PASSWORD}, follow_redirects=False)
        assert r.status_code == 303, r.text
        yield c
