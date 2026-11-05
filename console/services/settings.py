"""settings application functions and owned state. No console.app dependency."""

from fastapi import HTTPException
import config
import json
import os
from console.shared import logger
from console.schemas import SettingsRequest


class SettingsService:
    def __init__(self, context):
        self.context = context

    def _load_settings(self) -> dict:
        """启动加载 settings.json；缺文件/损坏返回空 dict（各键回退 config 默认）。"""
        try:
            payload = json.loads(
                self.context.settings._SETTINGS_PATH.read_text(encoding="utf-8")
            )
            if isinstance(payload, dict):
                return payload
        except FileNotFoundError:
            pass
        except (OSError, ValueError, TypeError) as e:
            logger.warning("settings.json 加载失败: %s", e)
        return {}

    def _save_settings(self) -> None:
        """原子写落盘（tmp + os.replace，与 clients.json 落盘模式一致）。"""
        try:
            self.context.settings._SETTINGS_PATH.parent.mkdir(
                parents=True, exist_ok=True
            )
            tmp = self.context.settings._SETTINGS_PATH.with_suffix(".json.tmp")
            tmp.write_text(
                json.dumps(
                    self.context.settings._settings, ensure_ascii=False, indent=2
                ),
                encoding="utf-8",
            )
            os.replace(tmp, self.context.settings._SETTINGS_PATH)
        except OSError:
            logger.exception("settings.json 落盘失败")

    def _stop_atr_mult(self) -> float:
        """当前全局止损 ATR 倍数（settings.json 优先，缺省回退 config.STOP_ATR_MULT）。"""
        try:
            v = float(self.context.settings._settings.get("stop_atr_mult"))
            if (
                self.context.settings._STOP_ATR_MULT_MIN
                <= v
                <= self.context.settings._STOP_ATR_MULT_MAX
            ):
                return v
        except (TypeError, ValueError):
            pass
        return config.STOP_ATR_MULT

    def _pyramid_auto_breakeven(self) -> bool:
        return bool(
            self.context.settings._settings.get(
                "pyramid_auto_breakeven", config.PYRAMID_AUTO_BREAKEVEN
            )
        )

    def _pyramid_r_setting(self, key: str, default: float) -> float:
        try:
            value = float(self.context.settings._settings.get(key))
            if (
                self.context.settings._PYRAMID_R_MIN
                <= value
                <= self.context.settings._PYRAMID_R_MAX
            ):
                return value
        except (TypeError, ValueError):
            pass
        return default

    def _pyramid_breakeven_r(self) -> float:
        return self.context.settings._pyramid_r_setting(
            "pyramid_breakeven_r", config.PYRAMID_BREAKEVEN_R
        )

    def _pyramid_add_unlock_r(self) -> float:
        return self.context.settings._pyramid_r_setting(
            "pyramid_add_unlock_r", config.PYRAMID_ADD_UNLOCK_R
        )

    def _screen_premarket_filter(self) -> bool:
        """选股盘前区间过滤开关（默认关：缺键/假值都不过滤）。"""
        return bool(self.context.settings._settings.get("screen_premarket_filter"))

    def _screen_opening_range_filter(self) -> bool:
        return bool(self.context.settings._settings.get("screen_opening_range_filter"))

    def _fvg_premarket_filter(self) -> bool:
        """FVG 监控盘前区间过滤开关（默认开，保留用户显式关闭）。"""
        return bool(self.context.settings._settings.get("fvg_premarket_filter", True))

    def _settings_payload(self) -> dict:
        """全局设置当前值（GET /api/settings 与 POST 响应共用）。"""
        return {
            "stop_atr_mult": self.context.settings._stop_atr_mult(),
            "screen_premarket_filter": self.context.settings._screen_premarket_filter(),
            "screen_opening_range_filter": self.context.settings._screen_opening_range_filter(),
            "fvg_premarket_filter": self.context.settings._fvg_premarket_filter(),
            "imessage_recipient": str(
                self.context.settings._settings.get("imessage_recipient") or ""
            ),
            "pyramid_auto_breakeven": self.context.settings._pyramid_auto_breakeven(),
            "pyramid_breakeven_r": self.context.settings._pyramid_breakeven_r(),
            "pyramid_add_unlock_r": self.context.settings._pyramid_add_unlock_r(),
        }

    async def api_get_settings(self) -> dict:
        """全局设置（所有客户端共享，快照 status 同步下发）。"""
        return self.context.settings._settings_payload()

    async def api_update_settings(
        self, req: SettingsRequest, trader: str = None
    ) -> dict:
        """修改全局设置并落盘 settings.json（重启后保持）。
        只更新显式传入的字段（None = 不动），至少传一个。"""
        changed = {}
        if req.stop_atr_mult is not None:
            if (
                not self.context.settings._STOP_ATR_MULT_MIN
                <= req.stop_atr_mult
                <= self.context.settings._STOP_ATR_MULT_MAX
            ):
                raise HTTPException(
                    400,
                    f"stop_atr_mult 超出允许范围（{self.context.settings._STOP_ATR_MULT_MIN}~{self.context.settings._STOP_ATR_MULT_MAX}）",
                )
            self.context.settings._settings["stop_atr_mult"] = round(
                float(req.stop_atr_mult), 2
            )
            changed["stop_atr_mult"] = self.context.settings._settings["stop_atr_mult"]
        for key in (
            "screen_premarket_filter",
            "screen_opening_range_filter",
            "fvg_premarket_filter",
            "pyramid_auto_breakeven",
        ):
            val = getattr(req, key)
            if val is not None:
                self.context.settings._settings[key] = bool(val)
                changed[key] = self.context.settings._settings[key]
        for key in ("pyramid_breakeven_r", "pyramid_add_unlock_r"):
            value = getattr(req, key)
            if value is not None:
                if (
                    not self.context.settings._PYRAMID_R_MIN
                    <= value
                    <= self.context.settings._PYRAMID_R_MAX
                ):
                    raise HTTPException(
                        400,
                        f"{key} 超出允许范围（{self.context.settings._PYRAMID_R_MIN}~{self.context.settings._PYRAMID_R_MAX}）",
                    )
                self.context.settings._settings[key] = round(float(value), 2)
                changed[key] = self.context.settings._settings[key]
        if req.imessage_recipient is not None:
            v = req.imessage_recipient.strip()
            if v:
                self.context.settings._settings["imessage_recipient"] = v
            else:
                self.context.settings._settings.pop("imessage_recipient", None)
            changed["imessage_recipient"] = v
        if not changed:
            raise HTTPException(400, "未传入任何设置字段")
        self.context.auto_engine.breakeven_enabled = (
            self.context.settings._pyramid_auto_breakeven()
        )
        self.context.auto_engine.breakeven_r = (
            self.context.settings._pyramid_breakeven_r()
        )
        self.context.auto_engine.add_unlock_r = (
            self.context.settings._pyramid_add_unlock_r()
        )
        self.context.settings._save_settings()
        self.context.events.record_event("settings_updated", trader=trader, **changed)
        logger.info("全局设置已更新 %s（by %s）", changed, trader)
        return {"ok": True, **self.context.settings._settings_payload()}
