# 充桩条

开会话 → 计量脉冲累加 → 结算关账 → 超时/异常命运。

- 前端 http://127.0.0.1:6500
- API http://127.0.0.1:11500

## 超时口径（全局唯一，架构 A：超时即强制关账加收）

标超时 = 同一事务内按锁定电价强制关账 + 超时加收 + 释放桩位/枪线，状态直接落「已关账」。
不存在持久化「已超时」状态；总览「已超时」桶 = `timeout_flag=1` 且已关账的交叉桶。

- 标超时前置条件：会话仍在充且已充分钟 ≥ `超时分钟` 设置，否则拒绝（防误标）。
- 超时后：禁止再记脉冲（409）、禁止重复关账/中断（409）；`结算可点=false`。
- 加收：`基础金额=电量×锁定电价`，`超时加收=基础×超时加收比例`，`合计=基础+加收`，写入 `timeout_records` 回执。
- 结局可重放：`GET /api/会话/{id}/超时/重放` 用回执输入重算并与落库金额、状态、占用核对。
- 扫描口：`POST /api/超时/扫描` 批量强制关账全部到期会话（幂等）；`GET /api/超时/候选` 列待办。
- 并发安全：标超时/结算/中断/脉冲均以 `BEGIN IMMEDIATE` + 条件更新做 CAS，超时与关账对撞只允许一个终态。
- 升级：启动时 `reconcile_legacy_timeouts` 把旧库残留的「已超时仍占桩」半截会话补齐为已关账并释放占用。

回归：`cd backend && .venv/bin/python -m pytest tests/ -q`（含超时/脉冲半截、超时/人工关账对撞、占用未清、冻结误关、重放、迁移等夹具）。

```powershell
# 本地 API
cd backend
py -3.11 -m venv .venv
.\.venv\Scripts\pip install -r requirements.txt
$env:PYTHONPATH = (Get-Location).Path
.\.venv\Scripts\uvicorn.exe app.main:app --host 127.0.0.1 --port 11500

# 本地前端（另开）
cd frontend
npm install
npm run dev
```

或 `docker compose up -d --build`。
