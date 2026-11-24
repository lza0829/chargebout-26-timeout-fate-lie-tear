from __future__ import annotations
import os
import sys
import tempfile
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))

# 必须在导入 app 之前指定独立库文件，避免污染仓库 data/。
_TMP = tempfile.mkdtemp(prefix="chargebout-test-")
os.environ["CHARGEBOUT_DB"] = str(Path(_TMP) / "test.db")

import pytest  # noqa: E402

from app.db import db  # noqa: E402
from app.main import app  # noqa: E402
from app.services.core import set_setting  # noqa: E402

# 每次用例前清空的业务表（保留站点/桩/枪/费率底座）。
WIPE = ("timeout_records", "session_events", "pulses", "sessions")


@pytest.fixture(scope="session")
def client():
    from fastapi.testclient import TestClient

    return TestClient(app)


@pytest.fixture()
def fresh(client):
    """复位到干净底座：清会话类数据、恢复桩/枪空闲、重置阈值与加收。"""
    with db() as conn:
        for t in WIPE:
            conn.execute(f"DELETE FROM {t}")
        conn.execute("UPDATE piles SET status='空闲'")
        conn.execute("UPDATE piles SET status='维护' WHERE name='河甲四号'")
        conn.execute("UPDATE guns SET status='可用'")
        conn.execute("UPDATE day_seals SET sealed=0, sealed_at=NULL")
        set_setting(conn, "超时分钟", "120")
        set_setting(conn, "超时加收比例", "0.5")
    return client


def open_and_pulse(client, pile_id=1, kwh=10.0):
    r = client.post("/api/开会话", json={"桩位编号": pile_id})
    assert r.status_code == 200, r.text
    sid = r.json()["会话编号"]
    if kwh:
        p = client.post(f"/api/会话/{sid}/脉冲", json={"电量": kwh, "功率": 7})
        assert p.status_code == 200, p.text
    return sid


def age_session(session_id, minutes):
    """把开始时间拨回 minutes 分钟前，用于复现超时；不改动其它字段。"""
    from datetime import datetime, timedelta

    started = (datetime.now() - timedelta(minutes=minutes)).strftime("%Y-%m-%d %H:%M:%S")
    with db() as conn:
        conn.execute("UPDATE sessions SET started_at=? WHERE id=?", (started, session_id))


def raw_session(session_id):
    with db() as conn:
        return dict(conn.execute("SELECT * FROM sessions WHERE id=?", (session_id,)).fetchone())


def raw_count(table, session_id):
    with db() as conn:
        return conn.execute(
            f"SELECT COUNT(*) AS n FROM {table} WHERE session_id=?", (session_id,)
        ).fetchone()["n"]


def raw_pile(session_id):
    with db() as conn:
        return dict(
            conn.execute(
                """SELECT p.status AS pile_status, g.status AS gun_status
                   FROM sessions se JOIN piles p ON p.id=se.pile_id JOIN guns g ON g.id=se.gun_id
                   WHERE se.id=?""",
                (session_id,),
            ).fetchone()
        )
