"""precheck application functions and owned state. No console.app dependency."""

from fastapi import HTTPException
import asyncio
import config
import json
import os
import time
from console.shared import logger


class PrecheckService:
    def __init__(self, context):
        self.context = context

    def _pc_item(self, key: str, name: str, status: str, detail: str) -> dict:
        return {"key": key, "name": name, "status": status, "detail": detail}

    async def run_precheck(self) -> dict:
        """盘前检查单（纯读现有状态，无副作用）。
        每项 {key, name, status: ok/bad/neutral/skip, detail}；
        overall = 有红则 bad，否则 ok（neutral/skip 不拖红）。"""
        from zoneinfo import ZoneInfo
        import datetime as _dt

        et = ZoneInfo("America/New_York")
        now = _dt.datetime.now(et)
        hm = now.hour * 60 + now.minute
        items = []
        market = self.context.collector.latest() or {}
        n_syms, n_watch = (len(market), len(config.WATCHLIST))
        missing = sorted(set(config.WATCHLIST) - set(market))
        bt = self.context.collector.latest_bar_time or 0
        age = time.time() - bt if bt else None
        fresh = age is not None and age < 600
        data_ready = not missing and fresh
        waiting_for_market = not data_ready and getattr(self.context.collector, "starting_up", False)
        detail = (
            f"标的 {n_syms}/{n_watch}"
            + (
                f"，缺 {', '.join((m[:12] for m in missing[:5]))}"
                + ("…" if len(missing) > 5 else "")
                if missing
                else ""
            )
            + (
                "；最新 bar "
                + (
                    "无数据"
                    if age is None
                    else f"{int(age // 60)} 分钟前"
                    + ("（新鲜）" if fresh else "（>10 分钟，陈旧）")
                )
            )
        )
        items.append(
            self.context.precheck._pc_item(
                "data_flow",
                "数据流",
                "ok" if data_ready else "neutral" if waiting_for_market else "bad",
                f"启动加载中；{detail}" if waiting_for_market else detail,
            )
        )
        n_attack = sum(
            (
                1
                for d in market.values()
                if (d.get("attack_c") or {}).get("pmh") is not None
            )
        )
        if hm < 570:
            items.append(
                self.context.precheck._pc_item(
                    "attack_zone",
                    "盘前区间注入",
                    "neutral",
                    f"待注入（09:30 锁定后注入）：当前 {n_attack}/{n_syms} 已有 attack_c 值",
                )
            )
        else:
            items.append(
                self.context.precheck._pc_item(
                    "attack_zone",
                    "盘前区间注入",
                    "ok" if data_ready and n_attack >= n_syms else "neutral",
                    f"{n_attack}/{n_syms} 标的已注入盘前区间"
                    if data_ready
                    else "等待数据流就绪",
                )
            )
        status = await self.context.accounts.get_status_cached()
        connected = bool(self.context.gateway._connected)
        equity = status.get("equity")
        ok3 = connected and equity is not None
        items.append(
            self.context.precheck._pc_item(
                "ibkr",
                "IBKR 连接",
                "ok" if ok3 else "bad",
                ("已连接" if connected else "未连接")
                + (
                    f"，净值 ${equity:,.0f}"
                    if equity is not None
                    else "，净值取不到（日盈亏熔断 fail-open，风控不设防）"
                ),
            )
        )
        exp = self.context.order_mgr.risk_exposure()["total"]
        budget = config.RISK_MAX_TOTAL_RISK_USD
        if budget <= 0:
            items.append(
                self.context.precheck._pc_item(
                    "risk_budget",
                    "风险预算",
                    "neutral",
                    f"未设总敞口预算（0=不限制）；当前已用 ${exp:,.0f}",
                )
            )
        else:
            over = exp > budget
            items.append(
                self.context.precheck._pc_item(
                    "risk_budget",
                    "风险预算",
                    "bad" if over else "ok",
                    f"预算 ${budget:,.0f} / 已用 ${exp:,.0f} / 余量 ${budget - exp:,.0f}"
                    + ("（超预算！）" if over else ""),
                )
            )
        servers = [
            s for s in self.context.copy._load_copy_servers() if s.get("enabled")
        ]
        if not servers:
            items.append(
                self.context.precheck._pc_item(
                    "copy", "跟单进程", "skip", "无启用的跟单服务器"
                )
            )
        else:
            bad5 = []
            for s in servers:
                name = s.get("name", "?")
                alive = self.context.copy_supervisor.process_alive(name)
                st = self.context.copy._copy_status(name) or {}
                if not alive:
                    bad5.append(f"{name} 进程不在")
                elif st and st.get("master_ws") is False:
                    bad5.append(f"{name} 未连上 master")
                elif st and st.get("last_error"):
                    bad5.append(f"{name} 报错: {str(st['last_error'])[:30]}")
            items.append(
                self.context.precheck._pc_item(
                    "copy",
                    "跟单进程",
                    "bad" if bad5 else "ok",
                    "；".join(bad5)
                    if bad5
                    else f"{len(servers)} 台 enabled 服务器进程均在运行",
                )
            )
        if not connected:
            items.append(
                self.context.precheck._pc_item(
                    "clock", "时钟", "skip", "IBKR 未连接，服务器时钟比对跳过"
                )
            )
        else:
            ibkr_t = self.context.gateway.ibkr_time or 0
            drift = abs(time.time() - ibkr_t) if ibkr_t else None
            ok6 = drift is not None and drift < 30
            items.append(
                self.context.precheck._pc_item(
                    "clock",
                    "时钟",
                    "ok" if ok6 else "bad",
                    (
                        f"IBKR 时钟偏差 {drift:.1f}s"
                        if drift is not None
                        else "IBKR 时钟未取得"
                    )
                    + (f"；数据时钟 {int(age)}s 前" if age is not None else ""),
                )
            )
        yday = (now - _dt.timedelta(days=1)).strftime("%Y%m%d")
        ev_path = self.context.precheck._PRECHECK_DIR / f"events-{yday}.jsonl"
        if not ev_path.exists():
            items.append(
                self.context.precheck._pc_item(
                    "leftover", "昨日遗留告警", "neutral", f"无昨日事件日志（{yday}）"
                )
            )
        else:
            alerts: dict = {}
            try:
                for line in ev_path.read_text(encoding="utf-8").splitlines():
                    try:
                        rec = json.loads(line)
                    except ValueError:
                        continue
                    if rec.get("type") == "alert" and rec.get("level") in (
                        "warning",
                        "critical",
                    ):
                        alerts[rec.get("key")] = (
                            rec.get("level"),
                            rec.get("title"),
                            rec.get("msg"),
                        )
                    elif rec.get("type") == "alert_recovered":
                        alerts.pop(rec.get("key"), None)
            except OSError:
                alerts = {}
            # TWS is checked against the current connection above.
            alerts.pop("tws_down", None)
            crits = {k: v for k, v in alerts.items() if v[0] == "critical"}
            if crits:
                items.append(
                    self.context.precheck._pc_item(
                        "leftover",
                        "昨日遗留告警",
                        "bad",
                        "；".join((f"{v[1]}" for v in list(crits.values())[:3]))
                        + (f" 等 {len(crits)} 条" if len(crits) > 3 else "")
                        + "（昨日未恢复）",
                    )
                )
            elif alerts:
                items.append(
                    self.context.precheck._pc_item(
                        "leftover",
                        "昨日遗留告警",
                        "neutral",
                        f"{len(alerts)} 条 warning 级昨日未恢复",
                    )
                )
            else:
                items.append(
                    self.context.precheck._pc_item(
                        "leftover", "昨日遗留告警", "ok", "昨日无未恢复告警"
                    )
                )
        overall = "bad" if any((i["status"] == "bad" for i in items)) else "ok"
        return {
            "ts": time.time(),
            "day": now.strftime("%Y-%m-%d"),
            "overall": overall,
            "waiting_for_market": waiting_for_market,
            "items": items,
        }

    async def _precheck_check_once(self, now, done_days: set) -> bool:
        """调度单拍（可测）：ET 工作日且时间 ≥09:25 且当天未跑 → 跑检查单并落盘
        （logs/precheck-YYYYMMDD.json）+ 事件 + 有红项走 notifier（critical：
        声音 + 前端 + iMessage 摘要列出红项）。返回本拍是否执行。"""
        if now.weekday() >= 5:
            return False
        hm = now.hour * 60 + now.minute
        day_key = now.strftime("%Y-%m-%d")
        if hm < 9 * 60 + 25 or day_key in done_days:
            return False
        report = await self.context.precheck.run_precheck()
        if report.get("waiting_for_market"):
            return False
        path = (
            self.context.precheck._PRECHECK_DIR
            / f"precheck-{day_key.replace('-', '')}.json"
        )
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(
            json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8"
        )
        os.replace(tmp, path)
        bads = [i for i in report["items"] if i["status"] == "bad"]
        self.context.events.record_event(
            "precheck",
            day=day_key,
            overall=report["overall"],
            bad=[i["key"] for i in bads],
            path=str(path),
        )
        if bads:
            await self.context.notifier.notify(
                f"precheck:{day_key}",
                "critical",
                "盘前检查有红项",
                "；".join((f"{i['name']}（{i['detail']}）" for i in bads)),
            )
        logger.info(
            "盘前检查单已生成: %s（%s，红项 %d）", path, report["overall"], len(bads)
        )
        done_days.add(day_key)
        return True

    async def _precheck_loop(self) -> None:
        """盘前检查调度（每 60s 检查；ET 工作日 09:25 自动跑一次；
        单拍见 _precheck_check_once，周末不跑）。"""
        from zoneinfo import ZoneInfo
        import datetime as _dt

        et = ZoneInfo("America/New_York")
        done_days: set = set()
        while True:
            await asyncio.sleep(60.0)
            try:
                await self.context.precheck._precheck_check_once(
                    _dt.datetime.now(et), done_days
                )
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.exception("盘前检查调度异常: %s", e)

    async def api_precheck(self) -> dict:
        """盘前检查单：手动随时跑（纯读状态，无副作用；非交易日如实报红）。"""
        return await self.context.precheck.run_precheck()

    async def api_precheck_last(self) -> dict:
        """最近一次落盘的盘前检查单（logs/precheck-*.json 最新一份）。"""
        files = sorted(self.context.precheck._PRECHECK_DIR.glob("precheck-*.json"))
        if not files:
            raise HTTPException(404, "暂无盘前检查记录")
        try:
            return json.loads(files[-1].read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raise HTTPException(500, "最近一份盘前检查读取失败")
