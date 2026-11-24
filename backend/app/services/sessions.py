from __future__ import annotations
from datetime import datetime
from app.services import timeout_surface

from fastapi import HTTPException

from app.services.core import (
    current_band_price,
    elapsed_minutes,
    get_setting,
    is_day_sealed,
    now_str,
    parse_ts,
    today,
)

# 超时架构（全局唯一）：标超时 = 同一事务内强制按锁定电价关账 + 超时加收 + 释放占用。
# 不存在「已超时」持久状态；超时会话终态即「已关账」，靠 timeout_flag=1 标记与回执审计。
OPEN_STATUSES = ("充电中", "结算中")
TIMEOUT_REASON = "超时强制关账"

DEFAULT_TIMEOUT_MINUTES = 120
DEFAULT_SURCHARGE_RATE = 0.5


def _timeout_minutes(conn) -> int:
    try:
        return int(get_setting(conn, "超时分钟", str(DEFAULT_TIMEOUT_MINUTES)))
    except (TypeError, ValueError):
        return DEFAULT_TIMEOUT_MINUTES


def _surcharge_rate(conn) -> float:
    try:
        return float(get_setting(conn, "超时加收比例", str(DEFAULT_SURCHARGE_RATE)))
    except (TypeError, ValueError):
        return DEFAULT_SURCHARGE_RATE


def _begin_immediate(conn) -> None:
    """取写锁，串行化标超时/结算/脉冲，避免双终态与半截超时。"""
    conn.execute("BEGIN IMMEDIATE")


def _next_slip(conn) -> str:
    n = int(get_setting(conn, "会话序号", "1000")) + 1
    conn.execute(
        "INSERT INTO settings(key,value) VALUES('会话序号',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (str(n),),
    )
    return f"充{n}"


def _release_occupancy(conn, se) -> None:
    """释放桩位与枪线；若仍有其它在充会话共用则不误清。"""
    still_pile = conn.execute(
        "SELECT id FROM sessions WHERE pile_id=? AND id!=? AND status IN ('充电中','结算中')",
        (se["pile_id"], se["id"]),
    ).fetchone()
    if not still_pile:
        conn.execute(
            "UPDATE piles SET status='空闲' WHERE id=? AND status='占用'", (se["pile_id"],)
        )
    still_gun = conn.execute(
        "SELECT id FROM sessions WHERE gun_id=? AND id!=? AND status IN ('充电中','结算中')",
        (se["gun_id"], se["id"]),
    ).fetchone()
    if not still_gun:
        conn.execute(
            "UPDATE guns SET status='可用' WHERE id=? AND status='占用'", (se["gun_id"],)
        )


def open_session(conn, pile_id: int, gun_id: int | None = None) -> dict:
    if is_day_sealed(conn):
        raise HTTPException(400, detail="充日已封印，禁止新开充")
    _begin_immediate(conn)
    pile = conn.execute("SELECT * FROM piles WHERE id=?", (pile_id,)).fetchone()
    if not pile:
        raise HTTPException(404, detail="桩位不存在")
    if pile["status"] != "空闲":
        raise HTTPException(400, detail=f"桩位当前为{pile['status']}，不可开充")
    site = conn.execute("SELECT * FROM sites WHERE id=?", (pile["site_id"],)).fetchone()
    if not site or not site["active"]:
        raise HTTPException(400, detail="站点未启用")

    if gun_id is None:
        gun = conn.execute(
            "SELECT * FROM guns WHERE pile_id=? AND status='可用' ORDER BY id LIMIT 1",
            (pile_id,),
        ).fetchone()
    else:
        gun = conn.execute(
            "SELECT * FROM guns WHERE id=? AND pile_id=?", (gun_id, pile_id)
        ).fetchone()
    if not gun:
        raise HTTPException(400, detail="无可用枪线")
    if gun["status"] != "可用":
        raise HTTPException(400, detail=f"枪线当前为{gun['status']}")

    price = current_band_price(conn, pile["rate_plan_id"])
    slip = _next_slip(conn)
    ts = now_str()
    cur = conn.execute(
        """INSERT INTO sessions(slip_no,site_id,pile_id,gun_id,rate_plan_id,business_day,status,kwh,amount,price_locked,started_at,note)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            slip,
            pile["site_id"],
            pile_id,
            gun["id"],
            pile["rate_plan_id"],
            today(),
            "充电中",
            0,
            0,
            price,
            ts,
            "",
        ),
    )
    sid = cur.lastrowid
    conn.execute("UPDATE piles SET status='占用' WHERE id=?", (pile_id,))
    conn.execute("UPDATE guns SET status='占用' WHERE id=?", (gun["id"],))
    conn.execute(
        "INSERT INTO session_events(session_id,action,created_at,note) VALUES(?,?,?,?)",
        (sid, "开会话", ts, f"锁定电价{price}"),
    )
    return {"会话号": slip, "会话编号": sid, "锁定电价": price, "状态": "充电中"}


def add_pulse(conn, session_id: int, delta_kwh: float, power_kw: float = 0) -> dict:
    if delta_kwh <= 0:
        raise HTTPException(400, detail="脉冲电量须大于零")
    _begin_immediate(conn)
    se = conn.execute("SELECT * FROM sessions WHERE id=?", (session_id,)).fetchone()
    if not se:
        raise HTTPException(404, detail="会话不存在")
    if se["timeout_flag"]:
        raise HTTPException(409, detail="会话已超时强制关账，禁止再记脉冲")
    if se["status"] != "充电中":
        raise HTTPException(400, detail=f"会话状态为{se['status']}，不可记脉冲")
    ts = now_str()
    conn.execute(
        "INSERT INTO pulses(session_id,delta_kwh,power_kw,created_at) VALUES(?,?,?,?)",
        (session_id, delta_kwh, power_kw, ts),
    )
    new_kwh = float(se["kwh"]) + float(delta_kwh)
    price = float(se["price_locked"] or current_band_price(conn, se["rate_plan_id"]))
    amount = round(new_kwh * price, 2)
    conn.execute(
        "UPDATE sessions SET kwh=?, amount=? WHERE id=?",
        (round(new_kwh, 3), amount, session_id),
    )
    conn.execute(
        "INSERT INTO session_events(session_id,action,created_at,note) VALUES(?,?,?,?)",
        (session_id, "脉冲", ts, f"+{delta_kwh}"),
    )
    return {"会话编号": session_id, "累计电量": round(new_kwh, 3), "预估金额": amount}


def settle(conn, session_id: int) -> dict:
    _begin_immediate(conn)
    se = conn.execute("SELECT * FROM sessions WHERE id=?", (session_id,)).fetchone()
    if not se:
        raise HTTPException(404, detail="会话不存在")
    if se["timeout_flag"]:
        # 超时已强制关账，禁止再走人工关账（否则双终态/重复计费）。
        raise HTTPException(409, detail="会话已超时强制关账，不可重复关账")
    if se["status"] not in OPEN_STATUSES:
        raise HTTPException(400, detail=f"会话状态为{se['status']}，不可关账")
    ts = now_str()
    price = float(se["price_locked"] or current_band_price(conn, se["rate_plan_id"]))
    amount = round(float(se["kwh"]) * price, 2)
    # 条件更新做 CAS：并发下仅一方能把在充态改为已关账。
    cur = conn.execute(
        """UPDATE sessions SET status='已关账', amount=?, closed_at=?, price_locked=?,
                  surcharge=0, closed_reason='人工关账'
           WHERE id=? AND status IN ('充电中','结算中')""",
        (amount, ts, price, session_id),
    )
    if cur.rowcount != 1:
        raise HTTPException(409, detail="会话已被并发关账或状态已变更，请刷新")
    _release_occupancy(conn, se)
    conn.execute(
        "INSERT INTO session_events(session_id,action,created_at,note) VALUES(?,?,?,?)",
        (session_id, "结算关账", ts, f"金额{amount}"),
    )
    return {"会话编号": session_id, "电量": float(se["kwh"]), "金额": amount, "状态": "已关账"}


def _compute_charge(kwh: float, price: float, rate: float) -> tuple[float, float, float]:
    base = round(float(kwh) * float(price), 2)
    surcharge = round(base * float(rate), 2)
    total = round(base + surcharge, 2)
    return base, surcharge, total


def _force_timeout_close(conn, se, ts: str, threshold: int, elapsed: int, rate: float) -> dict:
    price = float(se["price_locked"] or current_band_price(conn, se["rate_plan_id"]))
    kwh = float(se["kwh"])
    base, surcharge, total = _compute_charge(kwh, price, rate)
    # CAS：只有仍在充的会话可被超时强制关账；已关账/已中断一律拒绝，避免双终态。
    cur = conn.execute(
        """UPDATE sessions
           SET status='已关账', amount=?, surcharge=?, price_locked=?, closed_at=?,
               timeout_flag=1, timeout_at=?, closed_reason=?, note=?
           WHERE id=? AND status IN ('充电中','结算中') AND timeout_flag=0""",
        (
            total,
            surcharge,
            price,
            ts,
            ts,
            TIMEOUT_REASON,
            f"超过{threshold}分钟未关，已超{elapsed}分钟",
            se["id"],
        ),
    )
    if cur.rowcount != 1:
        raise HTTPException(409, detail="会话已被并发关账或已标超时，请刷新")

    rule_note = (
        f"电量{kwh}×锁定电价{price}=基础{base}；"
        f"按{round(rate * 100, 2)}%加收{surcharge}；合计{total}"
    )
    conn.execute(
        """INSERT INTO timeout_records(
               session_id,marked_at,threshold_minutes,elapsed_minutes,kwh,price,
               base_amount,surcharge_rate,surcharge,total_amount,rule_note,
               pile_id,gun_id,released,note)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            se["id"], ts, threshold, elapsed, kwh, price,
            base, rate, surcharge, total, rule_note,
            se["pile_id"], se["gun_id"], 1, "",
        ),
    )
    _release_occupancy(conn, se)
    conn.execute(
        "INSERT INTO session_events(session_id,action,created_at,note) VALUES(?,?,?,?)",
        (se["id"], "标超时", ts, f"阈值{threshold}分钟，已超{elapsed}分钟"),
    )
    conn.execute(
        "INSERT INTO session_events(session_id,action,created_at,note) VALUES(?,?,?,?)",
        (se["id"], "结算关账", ts, f"超时强制关账，金额{total}（含加收{surcharge}）"),
    )
    return {
        "会话编号": se["id"],
        "状态": "已关账",
        "超时": True,
        "阈值分钟": threshold,
        "已超分钟": elapsed,
        "电量": kwh,
        "锁定电价": price,
        "基础金额": base,
        "加收比例": rate,
        "超时加收": surcharge,
        "金额": total,
        "已释放占用": True,
        "回执": {
            "阈值分钟": threshold,
            "已超分钟": elapsed,
            "电量": kwh,
            "锁定电价": price,
            "基础金额": base,
            "加收比例": rate,
            "超时加收": surcharge,
            "金额": total,
            "规则": rule_note,
        },
    }


def mark_timeout(conn, session_id: int) -> dict:
    _begin_immediate(conn)
    se = conn.execute("SELECT * FROM sessions WHERE id=?", (session_id,)).fetchone()
    if not se:
        raise HTTPException(404, detail="会话不存在")
    if se["timeout_flag"]:
        raise HTTPException(409, detail="会话已标超时并强制关账，不可重复标记")
    if se["status"] not in OPEN_STATUSES:
        raise HTTPException(400, detail=f"会话状态为{se['status']}，不可标超时")
    threshold = _timeout_minutes(conn)
    elapsed = elapsed_minutes(se["started_at"])
    if elapsed < threshold:
        raise HTTPException(
            400,
            detail=f"会话仅充{elapsed}分钟，未达超时阈值{threshold}分钟，不可标超时",
        )
    return _force_timeout_close(conn, se, now_str(), threshold, elapsed, _surcharge_rate(conn))


def list_timeout_candidates(conn) -> list[dict]:
    """待办：充电中且已达超时阈值的会话。"""
    threshold = _timeout_minutes(conn)
    now = datetime.now()
    out = []
    rs = conn.execute(
        """SELECT se.*, p.name AS pile_name, s.name AS site_name
           FROM sessions se JOIN piles p ON p.id=se.pile_id JOIN sites s ON s.id=se.site_id
           WHERE se.status IN ('充电中','结算中') AND se.timeout_flag=0
           ORDER BY se.id"""
    ).fetchall()
    for r in rs:
        elapsed = elapsed_minutes(r["started_at"], now)
        out.append(
            {
                "会话编号": r["id"],
                "会话号": r["slip_no"],
                "站点": r["site_name"],
                "桩位": r["pile_name"],
                "电量": float(r["kwh"]),
                "开始时间": r["started_at"],
                "已充分钟": elapsed,
                "阈值分钟": threshold,
                "已超时": elapsed >= threshold,
            }
        )
    return out


def scan_timeouts(conn) -> dict:
    """超时扫描口：找出所有已达阈值会话，逐笔强制关账（同一事务内）。"""
    _begin_immediate(conn)
    threshold = _timeout_minutes(conn)
    now = datetime.now()
    ids = [
        r["id"]
        for r in conn.execute(
            "SELECT id, started_at FROM sessions WHERE status IN ('充电中','结算中') AND timeout_flag=0 ORDER BY id"
        ).fetchall()
        if elapsed_minutes(r["started_at"], now) >= threshold
    ]
    rate = _surcharge_rate(conn)
    results = []
    for sid in ids:
        se = conn.execute("SELECT * FROM sessions WHERE id=?", (sid,)).fetchone()
        if not se or se["timeout_flag"] or se["status"] not in OPEN_STATUSES:
            continue
        elapsed = elapsed_minutes(se["started_at"], now)
        results.append(_force_timeout_close(conn, se, now_str(), threshold, elapsed, rate))
    return {"阈值分钟": threshold, "扫描命中": len(ids), "已强制关账": len(results), "明细": results}


def replay_timeout(conn, session_id: int) -> dict:
    """超时结局可重放：用回执留存输入重算，并与落库金额核对。"""
    rec = conn.execute(
        "SELECT * FROM timeout_records WHERE session_id=?", (session_id,)
    ).fetchone()
    if not rec:
        raise HTTPException(404, detail="该会话无超时回执")
    base, surcharge, total = _compute_charge(rec["kwh"], rec["price"], rec["surcharge_rate"])
    se = conn.execute("SELECT * FROM sessions WHERE id=?", (session_id,)).fetchone()
    pile = conn.execute("SELECT status FROM piles WHERE id=?", (rec["pile_id"],)).fetchone()
    gun = conn.execute("SELECT status FROM guns WHERE id=?", (rec["gun_id"],)).fetchone()
    events = [
        {"动作": e["action"], "时间": e["created_at"], "备注": e["note"]}
        for e in conn.execute(
            "SELECT action, created_at, note FROM session_events WHERE session_id=? ORDER BY id",
            (session_id,),
        ).fetchall()
    ]
    replay_ok = (
        round(base, 2) == round(float(rec["base_amount"]), 2)
        and round(surcharge, 2) == round(float(rec["surcharge"]), 2)
        and round(total, 2) == round(float(rec["total_amount"]), 2)
        and se is not None
        and round(float(se["amount"]), 2) == round(total, 2)
        and se["status"] == "已关账"
        and se["timeout_flag"] == 1
    )
    occupancy_cleared = (pile is None or pile["status"] != "占用") and (
        gun is None or gun["status"] != "占用"
    )
    return {
        "会话编号": session_id,
        "回执": dict(rec),
        "重算": {"基础金额": base, "超时加收": surcharge, "金额": total},
        "金额一致": replay_ok,
        "占用已清": occupancy_cleared,
        "会话状态": se["status"] if se else None,
        "事件": events,
    }


def reconcile_legacy_timeouts(conn) -> int:
    """升级迁移：旧库可能残留持久化「已超时」且未释放占用的半截会话。

    架构 A 下超时会话终态必须是「已关账」。此处把这类历史行补齐为
    强制关账（按锁定电价结算、内存回执无法重算追溯故标记为迁移回执），
    并释放占用，保证总览「超时占用残留」归零。返回补齐笔数。
    """
    _begin_immediate(conn)
    rate = _surcharge_rate(conn)
    rows = conn.execute("SELECT * FROM sessions WHERE status='已超时'").fetchall()
    fixed = 0
    for se in rows:
        ts = now_str()
        price = float(se["price_locked"] or current_band_price(conn, se["rate_plan_id"]))
        kwh = float(se["kwh"])
        base, surcharge, total = _compute_charge(kwh, price, rate)
        conn.execute(
            """UPDATE sessions
               SET status='已关账', amount=?, surcharge=?, price_locked=?, closed_at=?,
                   timeout_flag=1, timeout_at=COALESCE(timeout_at, ?), closed_reason=?,
                   note=?
               WHERE id=? AND status='已超时'""",
            (
                total,
                surcharge,
                price,
                ts,
                ts,
                "超时强制关账(迁移补齐)",
                "旧库超时态迁移补齐",
                se["id"],
            ),
        )
        if conn.execute("SELECT 1 FROM timeout_records WHERE session_id=?", (se["id"],)).fetchone():
            conn.execute(
                "UPDATE timeout_records SET released=1 WHERE session_id=?", (se["id"],)
            )
        else:
            conn.execute(
                """INSERT INTO timeout_records(
                       session_id,marked_at,threshold_minutes,elapsed_minutes,kwh,price,
                       base_amount,surcharge_rate,surcharge,total_amount,rule_note,
                       pile_id,gun_id,released,note)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,1,?)""",
                (
                    se["id"], ts, _timeout_minutes(conn),
                    elapsed_minutes(se["started_at"]), kwh, price,
                    base, rate, surcharge, total,
                    f"迁移补齐：电量{kwh}×锁定电价{price}=基础{base}；按{round(rate * 100, 2)}%加收{surcharge}；合计{total}",
                    se["pile_id"], se["gun_id"], "旧库超时态迁移补齐",
                ),
            )
        _release_occupancy(conn, se)
        conn.execute(
            "INSERT INTO session_events(session_id,action,created_at,note) VALUES(?,?,?,?)",
            (se["id"], "超时迁移补齐", ts, f"旧超时态→已关账，金额{total}"),
        )
        fixed += 1
    return fixed


def abort_session(conn, session_id: int, reason: str = "急停") -> dict:
    _begin_immediate(conn)
    se = conn.execute("SELECT * FROM sessions WHERE id=?", (session_id,)).fetchone()
    if not se:
        raise HTTPException(404, detail="会话不存在")
    if se["timeout_flag"]:
        raise HTTPException(409, detail="会话已超时强制关账，不可中断")
    if se["status"] not in OPEN_STATUSES:
        raise HTTPException(400, detail=f"会话状态为{se['status']}，不可中断")
    ts = now_str()
    cur = conn.execute(
        """UPDATE sessions SET status='已中断', closed_at=?, note=?, closed_reason='异常中断'
           WHERE id=? AND status IN ('充电中','结算中')""",
        (ts, reason, session_id),
    )
    if cur.rowcount != 1:
        raise HTTPException(409, detail="会话已被并发关账或状态已变更，请刷新")
    _release_occupancy(conn, se)
    conn.execute(
        "INSERT INTO session_events(session_id,action,created_at,note) VALUES(?,?,?,?)",
        (session_id, "异常中断", ts, reason),
    )
    return {"会话编号": session_id, "状态": "已中断", "原因": reason}


def set_pile_status(conn, pile_id: int, status: str) -> dict:
    if status not in ("空闲", "占用", "故障", "维护"):
        raise HTTPException(400, detail="非法桩位状态")
    _begin_immediate(conn)
    pile = conn.execute("SELECT * FROM piles WHERE id=?", (pile_id,)).fetchone()
    if not pile:
        raise HTTPException(404, detail="桩位不存在")
    open_se = conn.execute(
        "SELECT id FROM sessions WHERE pile_id=? AND status IN ('充电中','结算中')",
        (pile_id,),
    ).fetchone()
    if open_se and status in ("故障", "维护"):
        raise HTTPException(400, detail="仍有在充会话，不可直接改故障或维护")
    if status == "空闲" and open_se:
        raise HTTPException(400, detail="仍有在充会话，不可改空闲")
    conn.execute("UPDATE piles SET status=? WHERE id=?", (status, pile_id))
    return {"桩位编号": pile_id, "状态": status}

_mark_timeout_impl = mark_timeout


def mark_timeout(*args, **kwargs):
    out = _mark_timeout_impl(*args, **kwargs)
    return timeout_surface.after_timeout(args[0] if args else None, out, *args[1:], **kwargs)

_scan_timeouts_impl = scan_timeouts


def scan_timeouts(*args, **kwargs):
    out = _scan_timeouts_impl(*args, **kwargs)
    return timeout_surface.after_timeout(args[0] if args else None, out, *args[1:], **kwargs)

