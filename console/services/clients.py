"""clients application functions and owned state. No console.app dependency."""

from fastapi import HTTPException
from fastapi import Request
import config
import json
import os
import time
import uuid
from console.shared import logger
from console.schemas import ClientRegisterRequest


class ClientsService:
    def __init__(self, context):
        self.context = context

    def _load_clients(self) -> dict:
        """启动加载 clients.json（已审批 token 重启后保持有效）。"""
        try:
            payload = json.loads(
                self.context.clients._CLIENTS_PATH.read_text(encoding="utf-8")
            )
            tokens = payload.get("tokens")
            if isinstance(tokens, dict):
                return {"tokens": tokens}
        except FileNotFoundError:
            pass
        except (OSError, ValueError, TypeError) as e:
            logger.warning("clients.json 加载失败: %s", e)
        return {"tokens": {}}

    def _save_clients(self) -> None:
        """原子写落盘（tmp + os.replace，参照 lots_state.json 的落盘模式）。"""
        try:
            self.context.clients._CLIENTS_PATH.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.context.clients._CLIENTS_PATH.with_suffix(".json.tmp")
            tmp.write_text(
                json.dumps(self.context.clients._clients, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            os.replace(tmp, self.context.clients._CLIENTS_PATH)
        except OSError:
            logger.exception("clients.json 落盘失败")

    def _token_valid(self, info: dict) -> bool:
        """token 当前是否有效：已审批且未超过有效期（自审批时刻起算）。"""
        if not info or not info.get("approved"):
            return False
        return (
            time.time() - info.get("approved_at", 0)
            < self.context.clients._CLIENT_TOKEN_TTL_SECONDS
        )

    def _is_local(self, request: Request) -> bool:
        """是否本机请求（remote_addr 为回环地址）= master，免确认。"""
        client = request.client
        return bool(client) and client.host in self.context.clients._LOCAL_ADDRS

    async def require_trader(self, request: Request) -> str:
        """交易员闸门（写操作统一依赖注入）：
        本机放行（trader="master"）；非本机 token 有效（已审批且未过期）→ 放行
        （trader=名称）；已过期 → 403「登录已过期」；否则 403「等待 master 审批」。"""
        if self.context.clients._is_local(request):
            return "master"
        token = request.headers.get("X-Trader-Token", "")
        info = self.context.clients._clients.get("tokens", {}).get(token)
        if self.context.clients._token_valid(info):
            return info["name"]
        if info and info.get("approved"):
            raise HTTPException(
                403, "登录已过期（token 超过有效期），请重新登记并经 master 审批"
            )
        raise HTTPException(403, "客户端未确认，等待 master 审批")

    def _clients_snapshot(self) -> dict:
        """快照给前端的客户端列表（只给 token 前 6 位，不泄露完整 token；
        approved 列只含未过期的）。"""
        pending, approved = ([], [])
        for token, info in self.context.clients._clients.get("tokens", {}).items():
            row = {"name": info.get("name", ""), "token_prefix": token[:6]}
            if self.context.clients._token_valid(info):
                approved.append(row)
            elif not info.get("approved"):
                row["created_at"] = info.get("created_at", 0)
                pending.append(row)
        pending.sort(key=lambda r: r.get("created_at", 0))
        approved.sort(key=lambda r: r["name"])
        return {"pending": pending, "approved": approved}

    async def api_clients_self(self, request: Request) -> dict:
        """当前客户端身份：本机=master（免确认）；非本机按 X-Trader-Token 解析。"""
        if self.context.clients._is_local(request):
            return {"role": "master", "name": "master", "approved": True}
        token = request.headers.get("X-Trader-Token", "")
        info = self.context.clients._clients.get("tokens", {}).get(token)
        if info:
            expired = bool(info.get("approved")) and (
                not self.context.clients._token_valid(info)
            )
            return {
                "role": "trader",
                "name": info.get("name", ""),
                "approved": self.context.clients._token_valid(info),
                "expired": expired,
                "ttl_hours": config.CLIENT_TOKEN_TTL_HOURS,
            }
        return {"role": "guest", "approved": False}

    async def api_clients_register(
        self, req: ClientRegisterRequest, request: Request
    ) -> dict:
        """非本机客户端注册交易员名称 → 签发 token（pending，等 master 审批）。
        同名 pending 返回原 token（幂等）；名称已被已审批 token 占用 → 409。"""
        if self.context.clients._is_local(request):
            raise HTTPException(400, "本机即 master，无需注册")
        name = req.name.strip()
        if not name or not self.context.clients._CLIENT_NAME_RE.match(name):
            raise HTTPException(400, "名称非法（1-20 字符，仅允许中英文/数字/_/-）")
        if name.lower() == "master":
            raise HTTPException(400, "master 为保留名称")
        tokens = self.context.clients._clients.setdefault("tokens", {})
        for old_token, info in list(tokens.items()):
            if info.get("name") == name:
                if self.context.clients._token_valid(info):
                    raise HTTPException(409, "名称已被使用")
                if info.get("approved"):
                    del tokens[old_token]
                    logger.info("客户端 %s 旧 token 已过期，重新登记", name)
                    break
                return {"token": old_token, "name": name, "approved": False}
        token = uuid.uuid4().hex
        tokens[token] = {
            "name": name,
            "approved": False,
            "created_at": time.time(),
            "approved_at": 0,
        }
        self.context.clients._save_clients()
        self.context.events.record_event(
            "client_registered", name=name, token_prefix=token[:6]
        )
        logger.info("客户端注册: %s（token %s…），等待 master 审批", name, token[:6])
        return {"token": token, "name": name, "approved": False}

    def _resolve_client_token(self, token_or_prefix: str):
        """按完整 token 或前缀解析客户端 token（快照只暴露前 6 位，
        审批端点须同时支持两种入参）。返回 (token, info) 或 (None, None)。"""
        tokens = self.context.clients._clients.get("tokens", {})
        info = tokens.get(token_or_prefix)
        if info is not None:
            return (token_or_prefix, info)
        matches = [(t, i) for t, i in tokens.items() if t.startswith(token_or_prefix)]
        if len(matches) == 1:
            return matches[0]
        return (None, None)

    async def api_clients_approve(self, token: str, request: Request) -> dict:
        """master 审批客户端 token（仅本机可调用，支持 token 前缀）。"""
        if not self.context.clients._is_local(request):
            raise HTTPException(403, "仅本机（master）可审批客户端")
        full, info = self.context.clients._resolve_client_token(token)
        if info is None:
            raise HTTPException(404, "token 不存在或前缀不唯一")
        info["approved"] = True
        info["approved_at"] = time.time()
        self.context.clients._save_clients()
        self.context.events.record_event(
            "client_approved", name=info.get("name", ""), token_prefix=full[:6]
        )
        logger.info("客户端已审批: %s（token %s…）", info.get("name"), full[:6])
        return {"ok": True, "name": info.get("name", ""), "approved": True}

    async def api_clients_reject(self, token: str, request: Request) -> dict:
        """master 拒绝客户端 token：直接删除（仅本机可调用，支持 token 前缀）。"""
        if not self.context.clients._is_local(request):
            raise HTTPException(403, "仅本机（master）可拒绝客户端")
        full, info = self.context.clients._resolve_client_token(token)
        if info is None:
            raise HTTPException(404, "token 不存在或前缀不唯一")
        del self.context.clients._clients["tokens"][full]
        self.context.clients._save_clients()
        self.context.events.record_event(
            "client_rejected", name=info.get("name", ""), token_prefix=full[:6]
        )
        logger.info("客户端已拒绝并删除: %s（token %s…）", info.get("name"), full[:6])
        return {"ok": True, "name": info.get("name", ""), "approved": False}
