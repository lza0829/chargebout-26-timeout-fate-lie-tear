import { useEffect, useState } from 'react'
import { api } from '../api'

export default function Timeout() {
  const [cands, setCands] = useState([])
  const [receipts, setReceipts] = useState([])
  const [msg, setMsg] = useState('')
  const [ok, setOk] = useState(false)
  const [replay, setReplay] = useState(null)

  const load = () =>
    Promise.all([api.超时候选(), api.超时回执()]).then(([c, r]) => {
      setCands(c)
      setReceipts(r)
    })
  useEffect(() => {
    load()
  }, [])

  const mark = async (id) => {
    setOk(false)
    try {
      const r = await api.超时(id)
      setMsg(
        `会话 ${r.会话编号} 已强制关账：金额 ${r.金额}（基础 ${r.基础金额} + 加收 ${r.超时加收}），占用已释放`,
      )
      setOk(true)
      load()
    } catch (e) {
      setMsg(e.message)
    }
  }
  const scan = async () => {
    setOk(false)
    try {
      const r = await api.超时扫描()
      setMsg(`扫描命中 ${r.扫描命中} 笔，已强制关账 ${r.已强制关账} 笔`)
      setOk(true)
      load()
    } catch (e) {
      setMsg(e.message)
    }
  }
  const doReplay = async (id) => {
    try {
      setReplay(await api.超时重放(id))
    } catch (e) {
      setMsg(e.message)
    }
  }

  return (
    <div className="page">
      <h2>超时强制关账</h2>
      <p className="lead">
        口径：超时即按锁定电价强制关账并加收、释放占用；超时后禁止再记脉冲、不可重复关账。
      </p>

      <div className="panel">
        <div className="row">
          <button type="button" className="primary" onClick={scan}>
            扫描到期超时
          </button>
          <button type="button" onClick={load}>
            刷新
          </button>
        </div>
        <p className={`msg ${ok ? 'ok' : ''}`}>{msg}</p>
      </div>

      <div className="panel">
        <h3>待办候选（充电中）</h3>
        <table>
          <thead>
            <tr>
              <th>会话号</th>
              <th>桩位</th>
              <th>电量</th>
              <th>已充 / 阈值</th>
              <th>到期</th>
              <th />
            </tr>
          </thead>
          <tbody>
            {cands.map((s) => (
              <tr key={s.会话编号}>
                <td>{s.会话号}</td>
                <td>
                  {s.站点} · {s.桩位}
                </td>
                <td>{s.电量}</td>
                <td>
                  {s.已充分钟} / {s.阈值分钟} 分
                </td>
                <td>
                  <span className={`tag ${s.已超时 ? '已超时' : '充电中'}`}>
                    {s.已超时 ? '已达阈值' : '未到期'}
                  </span>
                </td>
                <td>
                  <button
                    type="button"
                    className="danger"
                    disabled={!s.已超时}
                    onClick={() => mark(s.会话编号)}
                  >
                    标超时并强制关账
                  </button>
                </td>
              </tr>
            ))}
            {!cands.length && (
              <tr>
                <td colSpan={6} className="lead">
                  无充电中会话
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </div>

      <div className="panel">
        <h3>超时回执（审计）</h3>
        <table>
          <thead>
            <tr>
              <th>会话</th>
              <th>标记时间</th>
              <th>阈值/已超</th>
              <th>电量 × 电价</th>
              <th>基础</th>
              <th>加收</th>
              <th>合计</th>
              <th />
            </tr>
          </thead>
          <tbody>
            {receipts.map((r) => (
              <tr key={r.id}>
                <td>#{r.session_id}</td>
                <td>{r.marked_at}</td>
                <td>
                  {r.threshold_minutes} / {r.elapsed_minutes} 分
                </td>
                <td>
                  {r.kwh} × {r.price}
                </td>
                <td>{r.base_amount}</td>
                <td>{r.surcharge}</td>
                <td>{r.total_amount}</td>
                <td>
                  <button type="button" onClick={() => doReplay(r.session_id)}>
                    重放
                  </button>
                </td>
              </tr>
            ))}
            {!receipts.length && (
              <tr>
                <td colSpan={8} className="lead">
                  暂无超时回执
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </div>

      {replay && (
        <div className="panel">
          <h3>
            重放 #{replay.会话编号} ·{' '}
            <span className={`tag ${replay.金额一致 && replay.占用已清 ? '已关账' : '已中断'}`}>
              {replay.金额一致 && replay.占用已清 ? '一致' : '不一致'}
            </span>
          </h3>
          <p>
            会话状态 {replay.会话状态} · 金额一致 {String(replay.金额一致)} · 占用已清{' '}
            {String(replay.占用已清)}
          </p>
          <p className="lead">{replay.回执?.rule_note}</p>
          <table>
            <thead>
              <tr>
                <th>动作</th>
                <th>时间</th>
                <th>备注</th>
              </tr>
            </thead>
            <tbody>
              {replay.事件.map((e, i) => (
                <tr key={i}>
                  <td>{e.动作}</td>
                  <td>{e.时间}</td>
                  <td>{e.备注}</td>
                </tr>
              ))}
            </tbody>
          </table>
          <button type="button" onClick={() => setReplay(null)}>
            收起
          </button>
        </div>
      )}
    </div>
  )
}
