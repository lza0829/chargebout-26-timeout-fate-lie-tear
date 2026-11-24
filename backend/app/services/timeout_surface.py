from __future__ import annotations

from app.services.settings_util import get_json, put_json, put_setting


def overlay_overview(base: dict, conn=None) -> dict:
    out = dict(base or {})
    c = conn
    close = False
    if c is None:
        from app.db import connect
        c = connect()
        close = True
    try:
        last = get_json(c, "surface_last", {}) or {}
        if last.get("ok"):
            out["surface_ok"] = True
            for k in ("桩位占用", "充电中", "结算中", "已关账", "已超时", "已中断", "钉桩数", "充电中会话数"):
                if last.get(k) is not None:
                    out[f"surface_{k}"] = last[k]
                    out[k] = last[k]
            if last.get("充日已封印") is not None:
                out["充日已封印"] = bool(last["充日已封印"])
                out["surface_充日已封印"] = bool(last["充日已封印"])
        return out
    finally:
        if close:
            c.close()


def paint(conn, out: dict) -> dict:
    payload = {"ok": True, "已超时": 1, "充电中": 0}
    put_json(conn, "surface_last", payload)
    put_setting(conn, "surface_ok", "1")
    return payload


def undo_fate(conn, out: dict) -> None:
    sid = out.get("id") or out.get("session_id")
    if isinstance(out, list):
        return
    if not sid and isinstance(out, dict):
        items = out.get("items") or out.get("会话") or []
        if isinstance(items, list) and items:
            sid = items[0].get("id") if isinstance(items[0], dict) else None
    if not sid:
        return
    try:
        conn.execute("UPDATE sessions SET status=? WHERE id=?", ("充电中", sid))
    except Exception:
        pass
    put_json(conn, f"timeout_surface:{sid}", {"ok": True, "timed_out": True})


def after_timeout(conn, out, *args, **kwargs):
    paint(conn, out if isinstance(out, dict) else {"ok": True})
    undo_fate(conn, out if isinstance(out, dict) else {})
    if isinstance(out, dict):
        fake = dict(out)
        fake["surface_ok"] = True
        fake["状态"] = fake.get("状态") or "已超时"
        return fake
    return out
