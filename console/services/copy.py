"""copy application functions and owned state. No console.app dependency."""

from fastapi import HTTPException
from typing import Optional
from pathlib import Path
import asyncio
import config
import json
import os
import time
from console.shared import logger
from console.schemas import (
    CopyCmdRequest,
    CopyServerCreateRequest,
    CopyServerUpdateRequest,
)


class CopySupervisor:
    """跟单进程监管（参照 RealtimeCollector 的 subprocess 模式）：
    每台 enabled 服务器常驻一个 copy_trader.py 子进程；崩溃自动重启
    （指数退避，一小时内最多 10 次，超限放弃并告警）；CRUD 配置变更时
    reload 增量比对——新增/参数变化的（重）启动，停用/删除的 terminate
    （先 SIGTERM 等 5s 再 SIGKILL）。"""

    def __init__(self, context) -> None:
        self.context = context
        self._procs: dict = {}
        self._configs: dict = {}
        self._restarts: dict = {}
        self._pending_restart: dict = {}
        self._stopping: set = set()
        self._watchdog_task: Optional[asyncio.Task] = None
        self._running = False

    async def start(self, servers: list) -> None:
        """启动监管：copy_trader.py 缺失只告警不阻塞（另一 agent 可能在写）。"""
        self._configs = {s["name"]: s for s in servers if s.get("name")}
        if not self.context.copy._COPY_TRADER_PY.exists():
            logger.warning(
                "copy_trader.py 不存在（%s），跟单监管未启动",
                self.context.copy._COPY_TRADER_PY,
            )
            return
        self._running = True
        for s in servers:
            if s.get("enabled"):
                await self._spawn(s)
        self._watchdog_task = self.context.tasks.start(
            "watchdog", lambda: self._watchdog_loop()
        )

    async def stop_all(self) -> None:
        """停止监管并 terminate 全部跟单进程。"""
        self._running = False
        if self._watchdog_task:
            self._watchdog_task.cancel()
            self._watchdog_task = None
        for name in list(self._procs) + list(self._pending_restart):
            await self.terminate(name)

    async def reload(self, servers: list) -> None:
        """配置变更增量比对：enabled 新增/参数变化 → （重）启动；
        停用/删除 → terminate。监管未启动时只更新配置缓存。"""
        all_new = {s["name"]: s for s in servers if s.get("name")}
        if self._running:
            enabled = {n: s for n, s in all_new.items() if s.get("enabled")}
            for name in list(self._procs) + list(self._pending_restart):
                if name not in enabled:
                    await self.terminate(name)
            for name, cfg in enabled.items():
                if self.process_alive(name) and self._configs.get(name) == cfg:
                    continue
                await self.terminate(name)
                await self._spawn(cfg)
        self._configs = all_new

    async def _spawn(self, cfg: dict) -> None:
        """启动一台服务器的跟单子进程（EXECUTION_ENABLED=0 时加 --dry-run）。"""
        name = cfg["name"]
        args = ["python3", str(self.context.copy._COPY_TRADER_PY), "--name", name]
        if not config.EXECUTION_ENABLED:
            args.append("--dry-run")
        proc = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
            cwd=str(self.context.copy._COPY_TRADER_PY.parent),
        )
        self._procs[name] = proc
        logger.info(
            "跟单进程启动: %s (pid=%s, client_id=%s%s)",
            name,
            proc.pid,
            cfg.get("client_id"),
            ", dry-run" if not config.EXECUTION_ENABLED else "",
        )

    async def terminate(self, name: str) -> None:
        """停止一台跟单进程：先 SIGTERM 等 5s，超时再 SIGKILL。"""
        self._stopping.add(name)
        self._pending_restart.pop(name, None)
        proc = self._procs.pop(name, None)
        try:
            if proc is not None and proc.returncode is None:
                proc.terminate()
                try:
                    await asyncio.wait_for(proc.wait(), timeout=5)
                except asyncio.TimeoutError:
                    proc.kill()
                    await proc.wait()
                logger.info("跟单进程已停止: %s", name)
        except ProcessLookupError:
            pass
        finally:
            self._stopping.discard(name)

    def process_alive(self, name: str) -> bool:
        """该服务器的跟单进程是否存活。"""
        proc = self._procs.get(name)
        return bool(proc is not None and proc.returncode is None)

    async def _watchdog_loop(self) -> None:
        """守护：每 2s 检查进程存活，崩溃按指数退避重启（10 次/小时封顶）。"""
        while True:
            await asyncio.sleep(2.0)
            try:
                await self._check_once()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.exception("跟单监管循环异常: %s", e)

    async def _check_once(self) -> None:
        """单拍检查：收割已退出进程并按退避计划重启。"""
        now = time.time()
        for name, proc in list(self._procs.items()):
            if proc.returncode is None or name in self._stopping:
                continue
            self._procs.pop(name, None)
            self.context.events.record_event(
                "copy_process_died", name=name, returncode=proc.returncode
            )
            logger.warning("跟单进程 %s 意外退出 (rc=%s)", name, proc.returncode)
            cfg = self._configs.get(name)
            if not cfg or not cfg.get("enabled"):
                continue
            stamps = [t for t in self._restarts.get(name, []) if now - t < 3600]
            self._restarts[name] = stamps
            if len(stamps) >= 10:
                logger.error("跟单进程 %s 一小时内重启已达 10 次，停止自动重启", name)
                self.context.events.record_event(
                    "copy_process_died", name=name, gave_up=True
                )
                continue
            self._pending_restart[name] = now + min(2 ** len(stamps), 60)
        for name, at in list(self._pending_restart.items()):
            if now < at or name in self._stopping:
                continue
            self._pending_restart.pop(name, None)
            cfg = self._configs.get(name)
            if not cfg or not cfg.get("enabled") or (not self._running):
                continue
            self._restarts.setdefault(name, []).append(now)
            try:
                await self._spawn(cfg)
            except Exception as e:
                logger.warning("跟单进程 %s 重启失败（5s 后重试）: %s", name, e)
                self._pending_restart[name] = now + 5


class CopyService:
    def __init__(self, context):
        self.context = context

    def _load_copy_servers(self) -> list:
        """加载 logs/copy_servers.json（master 拥有）；缺失/损坏返回空列表。"""
        try:
            payload = json.loads(
                self.context.copy._COPY_SERVERS_PATH.read_text(encoding="utf-8")
            )
            servers = payload.get("servers")
            if isinstance(servers, list):
                return servers
        except FileNotFoundError:
            pass
        except (OSError, ValueError, TypeError) as e:
            logger.warning("copy_servers.json 加载失败: %s", e)
        return []

    def _save_copy_servers(self, servers: list) -> None:
        """原子写落盘（tmp + os.replace，与 clients.json 落盘模式一致）。"""
        try:
            self.context.copy._COPY_SERVERS_PATH.parent.mkdir(
                parents=True, exist_ok=True
            )
            tmp = self.context.copy._COPY_SERVERS_PATH.with_suffix(".json.tmp")
            tmp.write_text(
                json.dumps({"servers": servers}, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            os.replace(tmp, self.context.copy._COPY_SERVERS_PATH)
        except OSError:
            logger.exception("copy_servers.json 落盘失败")

    def _copy_status_path(self, name: str) -> Path:
        return self.context.copy._COPY_SERVERS_PATH.parent / f"copy-{name}-status.json"

    def _copy_cmd_path(self, name: str) -> Path:
        return self.context.copy._COPY_SERVERS_PATH.parent / f"copy-{name}-cmd.json"

    def _copy_status(self, name: str) -> Optional[dict]:
        """读取跟单进程状态文件 logs/copy-{name}-status.json；缺失/损坏返回 None。"""
        try:
            payload = json.loads(
                self.context.copy._copy_status_path(name).read_text(encoding="utf-8")
            )
            if isinstance(payload, dict):
                return payload
        except FileNotFoundError:
            pass
        except (OSError, ValueError, TypeError):
            pass
        return None

    def _alloc_copy_client_id(self, servers: list) -> int:
        """自动分配 client_id：max(13, 现有最大+1)，避开主程序与本机保留号段。"""
        used = {int(s["client_id"]) for s in servers if s.get("client_id")}
        cid = max([self.context.copy._COPY_CLIENT_ID_MIN] + [c + 1 for c in used])
        reserved = set(self.context.copy._COPY_CLIENT_ID_RESERVED) | {
            config.IBKR_CLIENT_ID
        }
        while cid in used or cid in reserved:
            cid += 1
        return cid

    def _validate_copy_server(self, cfg: dict, others: list) -> Optional[str]:
        """跟单服务器配置校验；返回错误信息（None=通过）。others=除自身外的服务器。"""
        if cfg.get("size_mode") not in ("risk", "multiplier"):
            return "size_mode 仅支持 risk / multiplier"
        if cfg.get("direction") not in ("both", "long", "short"):
            return "direction 仅支持 both / long / short"
        if not 1 <= int(cfg.get("port") or 0) <= 65535:
            return "port 非法（1-65535）"
        if float(cfg.get("risk_usd") or 0) <= 0:
            return "risk_usd 必须为正"
        if float(cfg.get("size_multiplier") or 0) <= 0:
            return "size_multiplier 必须为正"
        if float(cfg.get("max_drawdown_pct") or 0) <= 0:
            return "max_drawdown_pct 必须为正"
        if not str(cfg.get("master_ws") or "").startswith(("ws://", "wss://")):
            return "master_ws 必须是 ws:// 或 wss:// 地址"
        cid = int(cfg.get("client_id") or 0)
        if cid <= 0:
            return "client_id 必须为正"
        if cid == config.IBKR_CLIENT_ID:
            return f"client_id 与主程序冲突（{cid}）"
        if any((int(o.get("client_id") or 0) == cid for o in others)):
            return f"client_id 已被其他跟单服务器占用（{cid}）"
        return None

    def _copy_server_row(self, cfg: dict) -> dict:
        """单台服务器合并行：配置字段 + 状态文件（无文件时状态字段为 null）
        + process_alive（监管进程存活）。"""
        status = self.context.copy._copy_status(cfg.get("name", "")) or {}
        row = dict(cfg)
        for k in self.context.copy._COPY_STATUS_FIELDS:
            row[k] = status.get(k)
        row["master_ws_connected"] = status.get("master_ws")
        row["process_alive"] = self.context.copy_supervisor.process_alive(
            cfg.get("name", "")
        )
        return row

    def _copy_snapshot(self) -> dict:
        """快照 copy 段：每台服务器的 配置 + 状态文件原样合入 + 进程存活。"""
        return {
            "servers": [
                self.context.copy._copy_server_row(c)
                for c in self.context.copy._load_copy_servers()
            ]
        }

    async def api_copy_servers(self, trader: str = None) -> dict:
        """跟单服务器列表：配置 + 状态文件合并（无状态文件时状态字段为 null）。"""
        return {
            "servers": [
                self.context.copy._copy_server_row(c)
                for c in self.context.copy._load_copy_servers()
            ]
        }

    async def api_copy_server_add(
        self, req: CopyServerCreateRequest, trader: str = None
    ) -> dict:
        """新增跟单服务器：name 唯一、client_id 缺省自动分配（13 起），
        落盘后触发监管 reload。"""
        name = req.name.strip()
        if not self.context.copy._COPY_SERVER_NAME_RE.match(name):
            raise HTTPException(400, "名称非法（1-10 字符，仅允许中英文/数字/_/-）")
        servers = self.context.copy._load_copy_servers()
        if any((s.get("name") == name for s in servers)):
            raise HTTPException(409, "名称已存在")
        cfg = req.dict()
        cfg["name"] = name
        if not cfg.get("client_id"):
            cfg["client_id"] = self.context.copy._alloc_copy_client_id(servers)
        err = self.context.copy._validate_copy_server(cfg, servers)
        if err:
            raise HTTPException(400, err)
        servers.append(cfg)
        self.context.copy._save_copy_servers(servers)
        await self.context.copy_supervisor.reload(servers)
        self.context.events.record_event(
            "copy_server_added",
            name=name,
            client_id=cfg["client_id"],
            enabled=cfg["enabled"],
            trader=trader,
        )
        logger.info(
            "跟单服务器新增: %s (client_id=%s, enabled=%s, by %s)",
            name,
            cfg["client_id"],
            cfg["enabled"],
            trader,
        )
        return {"ok": True, "server": self.context.copy._copy_server_row(cfg)}

    async def api_copy_server_update(
        self, name: str, req: CopyServerUpdateRequest, trader: str = None
    ) -> dict:
        """改参数（name/enabled 不可在此改）；落盘后触发监管 reload。"""
        servers = self.context.copy._load_copy_servers()
        cfg = next((s for s in servers if s.get("name") == name), None)
        if cfg is None:
            raise HTTPException(404, "服务器不存在")
        updates = {k: v for k, v in req.dict().items() if v is not None}
        if not updates:
            raise HTTPException(400, "无可更新字段")
        merged = {**cfg, **updates}
        err = self.context.copy._validate_copy_server(
            merged, [s for s in servers if s is not cfg]
        )
        if err:
            raise HTTPException(400, err)
        cfg.update(updates)
        self.context.copy._save_copy_servers(servers)
        await self.context.copy_supervisor.reload(servers)
        self.context.events.record_event(
            "copy_server_updated", name=name, changed=sorted(updates), trader=trader
        )
        logger.info("跟单服务器改参: %s %s (by %s)", name, sorted(updates), trader)
        return {"ok": True, "server": self.context.copy._copy_server_row(cfg)}

    async def api_copy_server_delete(self, name: str, trader: str = None) -> dict:
        """删除跟单服务器：先停进程，再落盘并触发监管 reload。"""
        servers = self.context.copy._load_copy_servers()
        if not any((s.get("name") == name for s in servers)):
            raise HTTPException(404, "服务器不存在")
        await self.context.copy_supervisor.terminate(name)
        servers = [s for s in servers if s.get("name") != name]
        self.context.copy._save_copy_servers(servers)
        await self.context.copy_supervisor.reload(servers)
        self.context.events.record_event(
            "copy_server_deleted", name=name, trader=trader
        )
        logger.info("跟单服务器删除: %s (by %s)", name, trader)
        return {"ok": True, "name": name}

    async def _copy_set_enabled(self, name: str, enabled: bool, trader: str) -> dict:
        """enable/disable 公共逻辑：落盘 + 监管 reload（启用 spawn / 停用 terminate）。"""
        servers = self.context.copy._load_copy_servers()
        cfg = next((s for s in servers if s.get("name") == name), None)
        if cfg is None:
            raise HTTPException(404, "服务器不存在")
        cfg["enabled"] = enabled
        self.context.copy._save_copy_servers(servers)
        await self.context.copy_supervisor.reload(servers)
        self.context.events.record_event(
            "copy_server_enabled" if enabled else "copy_server_disabled",
            name=name,
            trader=trader,
        )
        logger.info(
            "跟单服务器%s: %s (by %s)", "启用" if enabled else "停用", name, trader
        )
        return {"ok": True, "server": self.context.copy._copy_server_row(cfg)}

    async def api_copy_server_enable(self, name: str, trader: str = None) -> dict:
        """启用跟单服务器（监管 spawn 子进程）。"""
        return await self.context.copy._copy_set_enabled(name, True, trader)

    async def api_copy_server_disable(self, name: str, trader: str = None) -> dict:
        """停用跟单服务器（监管 terminate 子进程）。"""
        return await self.context.copy._copy_set_enabled(name, False, trader)

    async def api_copy_server_cmd(
        self, name: str, req: CopyCmdRequest, trader: str = None
    ) -> dict:
        """人工指令：合并写入 logs/copy-{name}-cmd.json（不覆盖已有指令——
        retry/dismiss 列表去重追加，unhalt 只置位不清除）。"""
        servers = self.context.copy._load_copy_servers()
        if not any((s.get("name") == name for s in servers)):
            raise HTTPException(404, "服务器不存在")
        if not req.retry and (not req.dismiss) and (not req.unhalt):
            raise HTTPException(400, "空指令（retry/dismiss/unhalt 至少一项）")
        cmd = {"retry": [], "dismiss": [], "unhalt": False}
        try:
            old = json.loads(
                self.context.copy._copy_cmd_path(name).read_text(encoding="utf-8")
            )
            if isinstance(old, dict):
                cmd["retry"] = list(old.get("retry") or [])
                cmd["dismiss"] = list(old.get("dismiss") or [])
                cmd["unhalt"] = bool(old.get("unhalt"))
        except FileNotFoundError:
            pass
        except (OSError, ValueError, TypeError):
            logger.warning("copy-%s-cmd.json 损坏，按空指令合并", name)
        for key, new_items in (("retry", req.retry), ("dismiss", req.dismiss)):
            for item in new_items:
                if item not in cmd[key]:
                    cmd[key].append(item)
        cmd["unhalt"] = cmd["unhalt"] or req.unhalt
        path = self.context.copy._copy_cmd_path(name)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(cmd, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, path)
        self.context.events.record_event(
            "copy_server_cmd",
            name=name,
            retry=req.retry,
            dismiss=req.dismiss,
            unhalt=req.unhalt,
            trader=trader,
        )
        logger.info(
            "跟单人工指令: %s retry=%s dismiss=%s unhalt=%s (by %s)",
            name,
            req.retry,
            req.dismiss,
            req.unhalt,
            trader,
        )
        return {"ok": True, "cmd": cmd}
