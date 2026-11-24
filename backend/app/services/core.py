from __future__ import annotations
from datetime import datetime

from app.db import db, row_dict, rows

TS_FMT = "%Y-%m-%d %H:%M:%S"


def now_str() -> str:
    return datetime.now().strftime(TS_FMT)


def today() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def parse_ts(value: str) -> datetime:
    return datetime.strptime(value, TS_FMT)


def elapsed_minutes(started_at: str, at: datetime | None = None) -> int:
    """自开始时间起的整分钟数（向下取整，最小 0）。"""
    at = at or datetime.now()
    mins = int((at - parse_ts(started_at)).total_seconds() // 60)
    return max(mins, 0)


def get_setting(conn, key: str, default: str = "") -> str:
    r = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return r["value"] if r else default


def set_setting(conn, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, value),
    )


def current_band_price(conn, rate_plan_id: int) -> float:
    plan = conn.execute("SELECT * FROM rate_plans WHERE id=?", (rate_plan_id,)).fetchone()
    if not plan:
        return 0.0
    hour = datetime.now().hour
    if 8 <= hour < 11 or 18 <= hour < 21:
        return float(plan["peak_price"])
    if 23 <= hour or hour < 7:
        return float(plan["valley_price"])
    return float(plan["flat_price"])


def is_day_sealed(conn, day: str | None = None) -> bool:
    d = day or today()
    r = conn.execute("SELECT sealed FROM day_seals WHERE business_day=?", (d,)).fetchone()
    return bool(r and r["sealed"])


def overview(conn) -> dict:
    piles = rows(conn.execute("SELECT status, COUNT(*) AS n FROM piles GROUP BY status").fetchall())
    sess = rows(conn.execute("SELECT status, COUNT(*) AS n FROM sessions GROUP BY status").fetchall())
    pile_map = {p["status"]: p["n"] for p in piles}
    sess_map = {s["status"]: s["n"] for s in sess}
    open_kwh = conn.execute(
        "SELECT COALESCE(SUM(kwh),0) AS k FROM sessions WHERE status IN ('充电中','结算中')"
    ).fetchone()["k"]
    today_amount = conn.execute(
        "SELECT COALESCE(SUM(amount),0) AS a FROM sessions WHERE business_day=? AND status='已关账'",
        (today(),),
    ).fetchone()["a"]
    # 超时架构 A：超时即强制关账，故「已超时」是交叉桶（timeout_flag=1 且已关账）。
    timed_out_all = conn.execute(
        "SELECT COUNT(*) AS n FROM sessions WHERE timeout_flag=1 AND status='已关账'"
    ).fetchone()["n"]
    timed_out_today = conn.execute(
        "SELECT COUNT(*) AS n FROM sessions WHERE timeout_flag=1 AND status='已关账' AND business_day=?",
        (today(),),
    ).fetchone()["n"]
    surcharge_today = conn.execute(
        "SELECT COALESCE(SUM(surcharge),0) AS a FROM sessions WHERE timeout_flag=1 AND status='已关账' AND business_day=?",
        (today(),),
    ).fetchone()["a"]
    # 不变量：标过超时的会话不得仍占桩（A 口径下必须为 0）。
    timeout_hanging = conn.execute(
        "SELECT COUNT(*) AS n FROM sessions WHERE timeout_flag=1 AND status!='已关账'"
    ).fetchone()["n"]
    return {
        "桩位空闲": pile_map.get("空闲", 0),
        "桩位占用": pile_map.get("占用", 0),
        "桩位故障": pile_map.get("故障", 0),
        "桩位维护": pile_map.get("维护", 0),
        "充电中": sess_map.get("充电中", 0),
        "结算中": sess_map.get("结算中", 0),
        "已关账": sess_map.get("已关账", 0),
        "已超时": timed_out_all,
        "已中断": sess_map.get("已中断", 0),
        "在充电量": round(float(open_kwh), 3),
        "本日关账金额": round(float(today_amount), 2),
        "本日已超时": timed_out_today,
        "本日超时加收": round(float(surcharge_today), 2),
        "超时占用残留": timeout_hanging,
        "超时口径": "超时即强制关账加收",
        "营业日": today(),
        "充日已封印": is_day_sealed(conn),
    }


def list_sites(conn):
    return rows(conn.execute("SELECT * FROM sites ORDER BY id").fetchall())


def list_piles(conn, site_id: int | None = None):
    if site_id:
        rs = conn.execute(
            """SELECT p.*, s.name AS site_name, r.name AS rate_name
               FROM piles p JOIN sites s ON s.id=p.site_id
               JOIN rate_plans r ON r.id=p.rate_plan_id
               WHERE p.site_id=? ORDER BY p.id""",
            (site_id,),
        ).fetchall()
    else:
        rs = conn.execute(
            """SELECT p.*, s.name AS site_name, r.name AS rate_name
               FROM piles p JOIN sites s ON s.id=p.site_id
               JOIN rate_plans r ON r.id=p.rate_plan_id
               ORDER BY p.id"""
        ).fetchall()
    return rows(rs)


def list_guns(conn, pile_id: int | None = None):
    if pile_id:
        return rows(
            conn.execute(
                "SELECT g.*, p.name AS pile_name FROM guns g JOIN piles p ON p.id=g.pile_id WHERE g.pile_id=? ORDER BY g.id",
                (pile_id,),
            ).fetchall()
        )
    return rows(
        conn.execute(
            "SELECT g.*, p.name AS pile_name FROM guns g JOIN piles p ON p.id=g.pile_id ORDER BY g.id"
        ).fetchall()
    )


def list_rates(conn):
    return rows(conn.execute("SELECT * FROM rate_plans ORDER BY id").fetchall())


def _session_select(conn, where: str, params: tuple, limit: int | None = None):
    sql = f"""SELECT se.*, p.name AS pile_name, p.status AS pile_status,
                     g.name AS gun_name, g.status AS gun_status, s.name AS site_name
              FROM sessions se
              JOIN piles p ON p.id=se.pile_id
              JOIN guns g ON g.id=se.gun_id
              JOIN sites s ON s.id=se.site_id
              {where} ORDER BY se.id DESC"""
    if limit:
        sql += f" LIMIT {limit}"
    return rows(conn.execute(sql, params).fetchall())


def list_sessions(conn, status: str | None = None, timeout_only: bool = False):
    clauses = []
    params: list = []
    if status:
        clauses.append("se.status=?")
        params.append(status)
    if timeout_only:
        clauses.append("se.timeout_flag=1")
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    return _session_select(conn, where, tuple(params), limit=200 if not status and not timeout_only else None)


def timeout_record(conn, session_id: int) -> dict | None:
    r = conn.execute(
        "SELECT * FROM timeout_records WHERE session_id=?", (session_id,)
    ).fetchone()
    return row_dict(r)


def list_timeout_records(conn, limit: int = 100):
    return rows(
        conn.execute(
            "SELECT * FROM timeout_records ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
    )


def session_detail(conn, session_id: int):
    se = conn.execute(
        """SELECT se.*, p.name AS pile_name, p.status AS pile_status,
                  g.name AS gun_name, g.status AS gun_status,
                  s.name AS site_name, r.name AS rate_name
           FROM sessions se
           JOIN piles p ON p.id=se.pile_id
           JOIN guns g ON g.id=se.gun_id
           JOIN sites s ON s.id=se.site_id
           JOIN rate_plans r ON r.id=se.rate_plan_id
           WHERE se.id=?""",
        (session_id,),
    ).fetchone()
    if not se:
        return None
    pulses = rows(
        conn.execute(
            "SELECT * FROM pulses WHERE session_id=? ORDER BY id", (session_id,)
        ).fetchall()
    )
    events = rows(
        conn.execute(
            "SELECT * FROM session_events WHERE session_id=? ORDER BY id", (session_id,)
        ).fetchall()
    )
    d = row_dict(se)
    d["脉冲"] = pulses
    d["事件"] = events
    d["超时回执"] = timeout_record(conn, session_id)
    d["结算可点"] = se["status"] in ("充电中", "结算中")
    d["占用中"] = se["status"] in ("充电中", "结算中")
    return d

