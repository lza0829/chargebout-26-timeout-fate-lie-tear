"""超时命运（架构 A：超时即强制关账加收）联动与对撞回归。"""
from __future__ import annotations

import threading

import pytest
from conftest import (
    age_session,
    open_and_pulse,
    raw_count,
    raw_pile,
    raw_session,
)

from app.db import db
from app.services import sessions as svc


def _events(sid):
    with db() as conn:
        return [
            r["action"]
            for r in conn.execute(
                "SELECT action FROM session_events WHERE session_id=? ORDER BY id", (sid,)
            ).fetchall()
        ]


def test_timeout_force_closes_settles_and_releases(fresh):
    sid = open_and_pulse(fresh, pile_id=1, kwh=10.0)
    locked = raw_session(sid)["price_locked"]
    age_session(sid, 200)

    r = fresh.post(f"/api/会话/{sid}/超时")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["状态"] == "已关账"
    assert body["已释放占用"] is True
    assert body["超时加收"] > 0
    # 金额=基础+加收，且基础=电量×锁定电价。
    assert body["基础金额"] == round(10.0 * locked, 2)
    assert body["金额"] == round(body["基础金额"] + body["超时加收"], 2)

    se = raw_session(sid)
    assert se["status"] == "已关账"
    assert se["timeout_flag"] == 1
    assert se["closed_reason"] == "超时强制关账"
    assert abs(float(se["amount"]) - body["金额"]) < 1e-6
    assert abs(float(se["surcharge"]) - body["超时加收"]) < 1e-6
    # 占用同刷释放。
    assert raw_pile(sid) == {"pile_status": "空闲", "gun_status": "可用"}
    # 审计：标超时 + 强制结算关账两条事件。
    actions = _events(sid)
    assert "标超时" in actions and "结算关账" in actions


def test_no_pulse_after_timeout(fresh):
    sid = open_and_pulse(fresh, pile_id=1, kwh=5.0)
    age_session(sid, 200)
    assert fresh.post(f"/api/会话/{sid}/超时").status_code == 200
    before = raw_count("pulses", sid)
    p = fresh.post(f"/api/会话/{sid}/脉冲", json={"电量": 1.0, "功率": 7})
    assert p.status_code == 409
    assert raw_count("pulses", sid) == before
    # 电量/金额不得因拒记而变动。
    assert float(raw_session(sid)["amount"]) > 0


def test_settle_after_timeout_is_rejected(fresh):
    sid = open_and_pulse(fresh, pile_id=1, kwh=8.0)
    age_session(sid, 200)
    assert fresh.post(f"/api/会话/{sid}/超时").status_code == 200
    amount_before = float(raw_session(sid)["amount"])
    r = fresh.post(f"/api/会话/{sid}/结算")
    assert r.status_code == 409
    assert float(raw_session(sid)["amount"]) == amount_before  # 金额不被二次改写


def test_timeout_below_threshold_rejected(fresh):
    sid = open_and_pulse(fresh, pile_id=1, kwh=3.0)  # 刚开，0 分钟
    r = fresh.post(f"/api/会话/{sid}/超时")
    assert r.status_code == 400
    assert raw_session(sid)["status"] == "充电中"
    assert raw_session(sid)["timeout_flag"] == 0


def test_overview_timeout_bucket_and_hanging_residue(fresh):
    sid = open_and_pulse(fresh, pile_id=1, kwh=12.0)
    age_session(sid, 200)
    fresh.post(f"/api/会话/{sid}/超时")
    o = fresh.get("/api/总览").json()
    assert o["已超时"] == 1
    assert o["超时占用残留"] == 0
    assert o["桩位占用"] == 0
    assert o["本日已超时"] == 1
    assert o["本日超时加收"] > 0
    assert o["结算中"] == 0  # 超时不落入任何在充桶


def test_session_ledger_and_detail_linkage(fresh):
    sid = open_and_pulse(fresh, pile_id=1, kwh=6.0)
    age_session(sid, 200)
    fresh.post(f"/api/会话/{sid}/超时")

    rows = fresh.get("/api/会话", params={"超时": "true"}).json()
    assert [r["id"] for r in rows] == [sid]
    assert rows[0]["status"] == "已关账"
    assert rows[0]["timeout_flag"] == 1

    d = fresh.get(f"/api/会话/{sid}").json()
    assert d["status"] == "已关账"
    assert d["结算可点"] is False
    assert d["占用中"] is False
    assert d["超时回执"]["session_id"] == sid
    assert d["超时回执"]["total_amount"] == pytest.approx(d["amount"])


@pytest.mark.parametrize("settle_first", [True, False])
def test_timeout_vs_manual_settle_collision(fresh, settle_first):
    """超时与人工关账对撞：无论谁先，只允许一个终态，绝不双关。"""
    sid = open_and_pulse(fresh, pile_id=1, kwh=9.0)
    age_session(sid, 200)
    if settle_first:
        assert fresh.post(f"/api/会话/{sid}/结算").status_code == 200
        r = fresh.post(f"/api/会话/{sid}/超时")
        assert r.status_code == 400  # 已关账，不可标超时
    else:
        assert fresh.post(f"/api/会话/{sid}/超时").status_code == 200
        r = fresh.post(f"/api/会话/{sid}/结算")
        assert r.status_code == 409  # 已超时强制关账
    se = raw_session(sid)
    assert se["status"] == "已关账"
    # 只有一条结算关账审计，不出现双终态。
    assert _events(sid).count("结算关账") == 1


def test_timeout_vs_pulse_halfway(fresh):
    """超时与脉冲半截：标超时后并发脉冲必须被拒，电量不被事后改写。"""
    sid = open_and_pulse(fresh, pile_id=1, kwh=4.0)
    age_session(sid, 200)
    assert fresh.post(f"/api/会话/{sid}/超时").status_code == 200
    frozen_kwh = float(raw_session(sid)["kwh"])
    for _ in range(3):
        assert fresh.post(f"/api/会话/{sid}/脉冲", json={"电量": 2.0, "功率": 7}).status_code == 409
    assert float(raw_session(sid)["kwh"]) == frozen_kwh


def test_frozen_cannot_be_misclosed(fresh):
    """冻结/已关账态不得被误关成第二种终态（中断也不行）。"""
    sid = open_and_pulse(fresh, pile_id=1, kwh=5.0)
    age_session(sid, 200)
    assert fresh.post(f"/api/会话/{sid}/超时").status_code == 200
    for path in (f"/api/会话/{sid}/结算", f"/api/会话/{sid}/中断"):
        r = fresh.post(path, json={})
        assert r.status_code in (400, 409)
    assert raw_session(sid)["status"] == "已关账"


def test_replay_timeout_outcome(fresh):
    """超时结局可重放：回执重算与落库一致，占用已清。"""
    sid = open_and_pulse(fresh, pile_id=1, kwh=11.0)
    age_session(sid, 250)
    assert fresh.post(f"/api/会话/{sid}/超时").status_code == 200
    r = fresh.get(f"/api/会话/{sid}/超时/重放")
    assert r.status_code == 200
    d = r.json()
    assert d["金额一致"] is True
    assert d["占用已清"] is True
    assert d["会话状态"] == "已关账"
    assert [e["动作"] for e in d["事件"]].count("结算关账") == 1


def test_scan_endpoint_force_closes_all_due(fresh):
    a = open_and_pulse(fresh, pile_id=1, kwh=3.0)
    b = open_and_pulse(fresh, pile_id=2, kwh=3.0)
    c = open_and_pulse(fresh, pile_id=3, kwh=3.0)  # 保持在充，未到阈值
    age_session(a, 200)
    age_session(b, 200)
    r = fresh.post("/api/超时/扫描")
    assert r.status_code == 200
    d = r.json()
    assert d["扫描命中"] == 2
    assert d["已强制关账"] == 2
    assert raw_session(a)["status"] == "已关账"
    assert raw_session(b)["status"] == "已关账"
    assert raw_session(c)["status"] == "充电中"
    o = fresh.get("/api/总览").json()
    assert o["已超时"] == 2 and o["充电中"] == 1 and o["超时占用残留"] == 0


def test_scan_is_idempotent(fresh):
    sid = open_and_pulse(fresh, pile_id=1, kwh=3.0)
    age_session(sid, 200)
    first = fresh.post("/api/超时/扫描").json()
    second = fresh.post("/api/超时/扫描").json()
    assert first["已强制关账"] == 1
    assert second["已强制关账"] == 0  # 幂等，不重复关账
    assert _events(sid).count("结算关账") == 1


def test_true_concurrent_timeout_and_settle(fresh):
    """真并发：两独立连接同时标超时与关账，仅一方成功，终态唯一。"""
    sid = open_and_pulse(fresh, pile_id=1, kwh=7.0)
    age_session(sid, 200)
    barrier = threading.Barrier(2)
    results: dict[str, str] = {}

    def run(name, fn):
        conn = __import__("app.db", fromlist=["connect"]).connect()
        try:
            barrier.wait()
            try:
                fn(conn)
                conn.commit()
                results[name] = "ok"
            except Exception as e:  # noqa: BLE001 - 记录竞争方结果
                conn.rollback()
                results[name] = getattr(e, "status_code", None) or type(e).__name__
        finally:
            conn.close()

    t1 = threading.Thread(target=run, args=("timeout", lambda c: svc.mark_timeout(c, sid)))
    t2 = threading.Thread(target=run, args=("settle", lambda c: svc.settle(c, sid)))
    t1.start(); t2.start(); t1.join(); t2.join()

    assert sorted(str(v) for v in results.values()).count("ok") == 1
    se = raw_session(sid)
    assert se["status"] == "已关账"
    # 无论谁赢，最终只关一次账。
    assert _events(sid).count("结算关账") == 1
    assert raw_pile(sid) == {"pile_status": "空闲", "gun_status": "可用"}


def test_legacy_half_timeout_is_reconciled(fresh):
    """旧库半截超时（已超时态仍占桩）升级后须补齐为已关账并释放占用。"""
    sid = open_and_pulse(fresh, pile_id=1, kwh=6.0)
    age_session(sid, 300)
    # 模拟旧口径：直接写「已超时」但保留占用，无回执。
    with db() as conn:
        conn.execute("UPDATE sessions SET status='已超时' WHERE id=?", (sid,))
        assert conn.execute(
            "SELECT status FROM piles WHERE id=1"
        ).fetchone()["status"] == "占用"

    with db() as conn:
        fixed = svc.reconcile_legacy_timeouts(conn)
    assert fixed == 1
    se = raw_session(sid)
    assert se["status"] == "已关账" and se["timeout_flag"] == 1
    assert raw_pile(sid) == {"pile_status": "空闲", "gun_status": "可用"}
    o = fresh.get("/api/总览").json()
    assert o["超时占用残留"] == 0
    assert o["已超时"] == 1
    # 回执可重放。
    assert fresh.get(f"/api/会话/{sid}/超时/重放").json()["金额一致"] is True
