"""Pine 模型接入与执行实例管理；不依赖 FastAPI 或 console.app。"""

import math

from engine.pine_execution import PineExecutionEngine
from engine.pine_models import PineModelRegistry


class AutoServiceError(ValueError):
    def __init__(self, status, detail):
        super().__init__(detail)
        self.status = status
        self.detail = detail


class AutoService:
    def __init__(
        self,
        broker,
        collector,
        order_manager,
        *,
        registry=None,
        default_engine=None,
        default_engine_provider=None,
        watchlist=(),
        default_risk=200,
        max_instances=5,
        emit=None,
        record_event=None,
    ):
        self.broker = broker
        self.collector = collector
        self.order_manager = order_manager
        self.registry = registry or PineModelRegistry.load()
        self.watchlist = watchlist
        self.default_risk = default_risk
        self.max_instances = max_instances
        self.emit = emit
        self.record_event = record_event or (lambda *_a, **_k: None)
        self._engines = {}
        self._default_engine_provider = default_engine_provider
        if default_engine is not None:
            self._engines[("ema9_pyramid", "default")] = default_engine
        self.enabled = False
        self.strategy_name = ""

    def engines(self):
        if self._default_engine_provider:
            self._engines[("ema9_pyramid", "default")] = self._default_engine_provider()
        return list(self._engines.values())

    def engine(self, strategy="ema9_pyramid", instance_id="default"):
        try:
            model = self.registry.get(strategy)
        except ValueError as error:
            raise AutoServiceError(400, str(error)) from error
        if not instance_id or not isinstance(instance_id, str) or len(instance_id) > 64:
            raise AutoServiceError(400, "策略实例标识无效")
        self.engines()
        key = (strategy, instance_id)
        if key not in self._engines:
            self._engines[key] = PineExecutionEngine(
                self.broker,
                emit=self.emit,
                max_instances=self.max_instances,
                model=model,
                instance_id=instance_id,
            )
        return self._engines[key]

    async def restore(self, session_date):
        """Rehydrate the current session from the authoritative execution ledger."""
        rows = self.order_manager.list_lots()
        snapshots = self.order_manager.pyramid_states()
        latest = {}
        owners = set()
        submitted = set()
        for row in rows:
            meta = row.get("signal_snapshot") or {}
            strategy = meta.get("strategy_id")
            if strategy:
                owners.add((strategy, meta.get("strategy_instance_id") or "default"))
            if meta.get("group_id"):
                submitted.add(meta["group_id"])
        for group_id, snapshot in snapshots.items():
            if not isinstance(snapshot, dict) or snapshot.get("group_id") != group_id:
                continue
            owner = (snapshot.get("strategy_id", "ema9_pyramid"),
                     snapshot.get("strategy_instance_id", "default"))
            symbol = snapshot.get("symbol")
            if not symbol:
                continue
            owners.add(owner)
            key = (*owner, symbol)
            if key not in latest or snapshot.get("created_at", 0) > latest[key].get("created_at", 0):
                latest[key] = snapshot
        for owner in owners:
            try:
                self.engine(*owner)
            except AutoServiceError as error:
                self.record_event("auto_restore_skipped", owner=owner, reason=str(error))
        resumable = {snapshot["group_id"] for snapshot in latest.values()}
        for engine in self.engines():
            await engine.adopt_manual_lots(
                rows, session_date=session_date, resumable_groups=resumable,
            )
        for (strategy, instance_id, _), snapshot in latest.items():
            engine = self._engines.get((strategy, instance_id))
            if engine is not None and snapshot["group_id"] not in submitted:
                engine.restore_watch(snapshot, session_date)
        running = [engine for engine in self.engines() if engine.enabled]
        self.enabled = bool(running)
        self.strategy_name = "、".join(dict.fromkeys(engine.model.name for engine in running))

    def symbol(self, symbol):
        symbol = symbol.strip().upper()
        return (
            next((s for s in self.watchlist if s.split(":")[-1] == symbol), symbol)
            if ":" not in symbol
            else symbol
        )

    def instances(self):
        return [row for engine in self.engines() for row in engine.instances()]

    def start(self, strategy, symbols, params, instance_id="default", *,
              preview=False, confirmed_directions=None):
        try:
            model = self.registry.get(strategy)
        except ValueError as error:
            raise AutoServiceError(400, str(error)) from error
        symbols = list(
            dict.fromkeys(
                self.symbol(s) for s in (symbols or self.watchlist) if s.strip()
            )
        )
        try:
            risk = float(params.get("initial_risk_usd") or self.default_risk)
        except (TypeError, ValueError):
            raise AutoServiceError(400, "首仓风险预算格式无效") from None
        if not math.isfinite(risk) or risk <= 0:
            raise AutoServiceError(400, "首仓风险预算必须大于 0")
        market = self.collector.latest() or {}
        missing = [
            s for s in symbols if not model.from_market(market.get(s) or {}).entry_price
        ]
        if missing:
            label = "EMA9 v7" if strategy == "ema9_pyramid" else model.name
            raise AutoServiceError(409, label + " 指标未就绪: " + ",".join(missing))
        directions = {
            s: model.from_market(market.get(s) or {}).direction for s in symbols
        }
        if any(not direction for direction in directions.values()):
            raise AutoServiceError(
                409,
                "启动时方向不明确，无法锁定策略方向: "
                + ",".join(s for s in symbols if not directions[s]),
            )
        if preview or confirmed_directions is not None:
            qqq = "NASDAQ:QQQ"
            qqq_direction = model.from_market(market.get(qqq) or {}).direction
            if qqq_direction not in (-1, 1):
                raise AutoServiceError(409, f"{model.name} 的 QQQ 当前方向未就绪")
            checked = {**directions, qqq: qqq_direction}
            mismatched = [s for s, direction in directions.items() if direction != qqq_direction]
            if mismatched:
                labels = {1: "多", -1: "空"}
                details = "、".join(f"{s} {labels[directions[s]]}" for s in mismatched)
                raise AutoServiceError(409, f"{model.name} 方向不一致：QQQ {labels[qqq_direction]}，{details}")
            if preview:
                return {"model": model.name, "directions": checked, "symbols": symbols, "risk": risk}
            if confirmed_directions != checked:
                raise AutoServiceError(409, "启动方向尚未确认或已变化，请重新检查并确认")
        engine = self.engine(strategy, instance_id)
        active = sum(row["state"] != "DONE" for row in self.instances())
        candidates = []
        for symbol in symbols:
            current = engine.get_instance(symbol)
            if current is not None and current.state != "DONE":
                candidates.append(symbol)
            elif active < self.max_instances:
                candidates.append(symbol)
                active += 1
        started = engine.start(candidates, risk, directions)
        self.enabled = True
        self.strategy_name = strategy
        profile = engine.profile
        details = {
            "initial_risk_usd": risk,
            "start_directions": directions,
            "risk_ratios": list(profile.risk_ratios),
            "max_entries": len(profile.risk_ratios),
            "pyramid_auto_breakeven": engine.breakeven_enabled,
            "pyramid_breakeven_r": engine.breakeven_r,
            "pyramid_add_unlock_r": engine.add_unlock_r,
            "execution_profile": profile.name,
            "instance_id": instance_id,
        }
        self.record_event(
            "auto_start", strategy=strategy, symbols=started, params=details
        )
        reasons = {
            i["symbol"]: i["recovery_block_reason"] or "退出处理中，不能重新启用"
            for i in engine.instances()
            if i["symbol"] in symbols
            and i["symbol"] not in started
            and (i.get("recovery_block_reason") or i.get("exit_requested_reason"))
        }
        return {
            "ok": True,
            "started": started,
            "params": details,
            "skipped": [s for s in symbols if s not in started],
            "skipped_reasons": reasons,
        }

    async def stop(self, close_positions=False):
        self.enabled = False
        stopped = []
        market = self.collector.latest() or {}
        for engine in self.engines():
            engine.enabled = False
            for row in engine.instances():
                if row["state"] == "DONE":
                    continue
                symbol = row["symbol"]
                engine.stop_symbol(symbol, close_position=close_positions)
                if close_positions:
                    ref = ((market.get(symbol) or {}).get("bar") or {}).get(
                        "close"
                    ) or 0
                    await engine.close_symbol(symbol, ref)
                stopped.append(symbol)
        self.record_event("auto_stop", symbols=stopped, close_positions=close_positions)
        return {"ok": True, "stopped": stopped, "close_positions": close_positions}

    async def stop_instance(
        self,
        symbol,
        close_positions=False,
        strategy="ema9_pyramid",
        instance_id="default",
    ):
        engine = self.engine(strategy, instance_id)
        symbol = self.symbol(symbol)
        if engine.get_instance(symbol) is None:
            raise AutoServiceError(404, "策略实例不存在")
        engine.stop_symbol(symbol, close_position=close_positions)
        if close_positions:
            data = (self.collector.latest() or {}).get(symbol) or {}
            ref = (data.get("bar") or {}).get("close") or 0
            await engine.close_symbol(symbol, ref)
        self.record_event(
            "auto_instance_stop",
            symbol=symbol,
            close_positions=close_positions,
            strategy_id=strategy,
            strategy_instance_id=instance_id,
        )
        return {"ok": True, "symbol": symbol, "close_positions": close_positions}

    async def breakeven_instance(self, symbol, group_id, qty, avg_cost,
                                strategy="ema9_pyramid", instance_id="default"):
        engine = self.engine(strategy, instance_id)
        try:
            return await engine.manual_breakeven(self.symbol(symbol), group_id, qty, avg_cost)
        except ValueError as error:
            raise AutoServiceError(409, str(error)) from error

    async def adopt(
        self, symbol, lot_id, risk=0, strategy="ema9_pyramid", instance_id="default"
    ):
        engine = self.engine(strategy, instance_id)
        symbol = self.symbol(symbol)
        lot = next(
            (
                row
                for row in self.order_manager.list_lots(active_only=True)
                if row["lot_id"] == lot_id and row["symbol"] == symbol
            ),
            None,
        )
        if lot is None:
            raise AutoServiceError(404, "未找到该股票的 ACTIVE 手动仓")
        if any(
            lot_id in row["lot_ids"]
            for other in self.engines()
            if other is not engine
            for row in other.instances()
        ):
            raise AutoServiceError(409, "该订单已由其他策略实例管理")
        old = engine.get_instance(symbol)
        risk = risk or (old.initial_risk_usd if old else 0) or self.default_risk
        raw = ((self.collector.latest() or {}).get(symbol) or {}).get(
            engine.model.realtime_key
        ) or {}
        try:
            inst = await engine.adopt_seed(symbol, lot, risk, raw, enabled=True)
        except ValueError as error:
            raise AutoServiceError(409, str(error)) from error
        self.enabled = True
        self.strategy_name = strategy
        self.record_event(
            "auto_manual_adopted", symbol=symbol, lot_id=lot_id, initial_risk_usd=risk
        )
        return {"ok": True, "instance": inst.view()}
