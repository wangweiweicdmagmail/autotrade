# -*- coding: utf-8 -*-
"""
LLM 客户端 (agent_llm.py)
==========================

交易 Agent 的模型接入层，移植自 nautilus_ibkr_helloworld
tv_bridge/ai_reader/llm.py（实战验证过的封装）：

- OpenAI 兼容 /chat/completions，urllib 裸调（零新增依赖，阻塞调用由
  上层 asyncio.to_thread 池化）；
- `LLM_BACKEND` 选择后端：deepseek（主力，成本可忽略）/ kimi（对照）；
- `response_format={"type":"json_object"}` API 级 JSON 约束；
  不传 temperature（kimi-k2/k3 系只允许 1）；
- 两级重试：网络错 sleep 2s 重试 1 次；空输出/非 JSON → 追加提示重试 1 次；
- `_extract_json()` 容错解析（容忍 markdown 围栏与杂文本）；
- 返回 dict 附 `_usage / _latency_s / _model / _reasoning` 元数据
  （token 用量 → 费用对账；思维链 → 决策审查）。

配置（项目根 .env，见 .env.example；手写 dotenv 加载，不覆盖已有环境变量）：
    KIMI_API_KEY / KIMI_BASE_URL / KIMI_MODEL
    DEEPSEEK_API_KEY / DEEPSEEK_BASE_URL / DEEPSEEK_MODEL
    LLM_BACKEND=deepseek|kimi
"""

import json
import logging
import os
import re
import time
import urllib.request
import urllib.error
from pathlib import Path

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parent.parent


class LLMUnavailable(RuntimeError):
    """LLM 后端不可用（无 key / 配置缺失）。"""


def load_dotenv(path: Path = None) -> None:
    """手写 dotenv 加载：不覆盖已存在的环境变量。"""
    path = path or (PROJECT_ROOT / ".env")
    if not path.exists():
        return
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            key, val = key.strip(), val.strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = val
    except OSError as e:
        logger.warning(".env 加载失败: %s", e)


load_dotenv()

_BACKENDS = {
    "kimi": {
        "key_env": "KIMI_API_KEY",
        "base_env": "KIMI_BASE_URL",
        "model_env": "KIMI_MODEL",
        "default_base": "https://api.moonshot.cn/v1",
        "default_model": "kimi-k2.7-code",
    },
    "deepseek": {
        "key_env": "DEEPSEEK_API_KEY",
        "base_env": "DEEPSEEK_BASE_URL",
        "model_env": "DEEPSEEK_MODEL",
        "default_base": "https://api.deepseek.com/v1",
        "default_model": "deepseek-v4-flash",
    },
}

MAX_TOKENS = 2000            # 输出契约很小（{action, confidence, reason}）
TIMEOUT_S = 30


def _backend_config() -> dict:
    name = os.environ.get("LLM_BACKEND", "deepseek").strip().lower()
    cfg = _BACKENDS.get(name)
    if cfg is None:
        raise LLMUnavailable(f"未知 LLM_BACKEND: {name}")
    key = os.environ.get(cfg["key_env"], "").strip()
    if not key:
        raise LLMUnavailable(f"未配置 {cfg['key_env']}（.env）")
    return {
        "name": name,
        "key": key,
        "base": os.environ.get(cfg["base_env"], cfg["default_base"]).rstrip("/"),
        "model": os.environ.get(cfg["model_env"], cfg["default_model"]),
    }


def _extract_json(text: str) -> dict:
    """容错解析：容忍 markdown 围栏与杂文本，取第一个 {...} 块。"""
    if not text:
        raise ValueError("空输出")
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if not m:
        raise ValueError(f"输出中无 JSON 块: {text[:120]}")
    return json.loads(m.group(0))


def _post(cfg: dict, system: str, user: str, max_tokens: int,
          json_mode: bool = True) -> dict:
    body = {
        "model": cfg["model"],
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "max_tokens": max_tokens,
        # 不传 temperature：kimi-k2/k3 系只允许 1
    }
    if json_mode:
        body["response_format"] = {"type": "json_object"}
    req = urllib.request.Request(
        cfg["base"] + "/chat/completions",
        data=json.dumps(body).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {cfg['key']}",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=TIMEOUT_S) as resp:
        return json.loads(resp.read().decode("utf-8"))


def call_llm(system: str, user: str) -> dict:
    """调 LLM 并解析 JSON 输出。失败抛 LLMUnavailable / RuntimeError。
    返回 dict 附 _usage/_latency_s/_model/_reasoning 元数据。"""
    cfg = _backend_config()
    t0 = time.monotonic()
    last_err = None
    for attempt, extra in ((0, ""), (1, ""), (1, "\n\n上次输出为空或不是合法 JSON，"
                                               "请只输出一个 JSON 对象。")):
        try:
            raw = _post(cfg, system, user + extra, MAX_TOKENS)
            choice = (raw.get("choices") or [{}])[0]
            msg = choice.get("message") or {}
            content = msg.get("content") or ""
            out = _extract_json(content)
            out["_usage"] = raw.get("usage") or {}
            out["_latency_s"] = round(time.monotonic() - t0, 2)
            out["_model"] = f"{cfg['name']}/{cfg['model']}"
            reasoning = msg.get("reasoning_content")
            if reasoning:
                out["_reasoning"] = reasoning[-2000:]   # 思维链尾部，供审查
            return out
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            last_err = e
            logger.warning("LLM 网络错误（第 %d 次）: %s", attempt + 1, e)
            time.sleep(2)
        except (ValueError, KeyError, json.JSONDecodeError) as e:
            last_err = e
            logger.warning("LLM 输出解析失败（第 %d 次）: %s", attempt + 1, e)
            if attempt == 0:
                time.sleep(1)
    raise RuntimeError(f"LLM 调用失败: {last_err}")


MAX_TEXT_TOKENS = 4000         # 纯文本输出（盘前报告 markdown 等长文）


def call_llm_text(system: str, user: str,
                  max_tokens: int = MAX_TEXT_TOKENS) -> dict:
    """纯文本输出调用（盘前报告 markdown 等）：不加 JSON 约束、不做 JSON 解析。
    网络错重试 1 次；空输出重试 1 次。失败抛 LLMUnavailable / RuntimeError。
    返回 {"text": ..., "_usage", "_latency_s", "_model"}。"""
    cfg = _backend_config()
    t0 = time.monotonic()
    last_err = None
    for attempt in (0, 1):
        try:
            raw = _post(cfg, system, user, max_tokens, json_mode=False)
            choice = (raw.get("choices") or [{}])[0]
            msg = choice.get("message") or {}
            text = msg.get("content") or ""
            if not text.strip():
                raise ValueError("空输出")
            return {"text": text, "_usage": raw.get("usage") or {},
                    "_latency_s": round(time.monotonic() - t0, 2),
                    "_model": f"{cfg['name']}/{cfg['model']}"}
        except (urllib.error.URLError, TimeoutError, OSError, ValueError) as e:
            last_err = e
            logger.warning("LLM 文本调用失败（第 %d 次）: %s", attempt + 1, e)
            time.sleep(2)
    raise RuntimeError(f"LLM 文本调用失败: {last_err}")
