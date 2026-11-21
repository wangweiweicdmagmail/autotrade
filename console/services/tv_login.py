"""First-run login coordinator; leaves market daemon stopped until credentials exist."""

import asyncio
import time

import config
from controller.tv_credentials import credentials_present, save_credentials
from controller.tv_credentials import read_browser_credentials, open_login_page
from console.shared import logger


class TvLoginService:
    def __init__(self, context, credentials_path):
        self.context = context
        self.credentials_path = credentials_path
        self.state = 'idle'
        self.detail = ''
        self.market_started_at = None
        self._task = None
        self._lock = asyncio.Lock()
        self._login_opened = False

    @property
    def waiting(self):
        return self.state in {'login_required', 'browser_wait', 'save_error', 'market_error'}

    def snapshot(self):
        return {'state': self.state, 'required': self.waiting, 'detail': self.detail}

    async def start(self):
        if self.state != 'idle':
            return
        if credentials_present(self.credentials_path):
            self.market_started_at = time.time()
            await self.context.collector.start()
            self.state = 'configured'
            self.detail = '已加载本机 TradingView 凭据'
            return
        self.state = 'login_required'
        self.detail = '请在运行 AutoTrade 的电脑上，使用专用 Chrome 登录自己的 TradingView；登录后自动继续。'
        logger.info('TradingView 尚未配置登录，等待专用 Chrome 登录；暂不启动行情进程')
        self._task = self.context.tasks.start('tv_login.wait', self._wait_for_login)

    async def _step(self):
        async with self._lock:
            if not self.waiting:
                return True
            if not credentials_present(self.credentials_path):
                try:
                    credentials = await read_browser_credentials(config.CDP_HTTP_ENDPOINT)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    self.state = 'browser_wait'
                    self.detail = '正在等待专用 Chrome；请在该浏览器中登录自己的 TradingView，登录后自动继续。'
                    return False
                if not credentials:
                    self.state = 'login_required'
                    self.detail = '等待 TradingView 登录完成；验证码或双重验证请在专用 Chrome 中完成。'
                    if (not self._login_opened
                            and self.context.lifecycle._startup_state.get('browser_ready')
                            and self.context.desktop._CONSOLE_AUTO_OPEN_BROWSER):
                        try:
                            await open_login_page(config.CDP_HTTP_ENDPOINT)
                            self._login_opened = True
                        except asyncio.CancelledError:
                            raise
                        except Exception:
                            self.detail = '请在专用 Chrome 中打开 TradingView 并登录；也可点击下方按钮打开登录页。'
                    return False
                try:
                    save_credentials(self.credentials_path, credentials)
                except (OSError, ValueError):
                    self.state = 'save_error'
                    self.detail = '登录已检测到，但凭据保存失败；请检查项目目录写入权限，系统会自动重试。'
                    return False
                logger.info('已从专用 Chrome 保存 TradingView 登录会话，准备启动行情')
            try:
                if self.market_started_at is None:
                    self.market_started_at = time.time()
                await self.context.collector.start()
            except asyncio.CancelledError:
                raise
            except Exception:
                self.state = 'market_error'
                self.detail = '登录凭据已保存，但行情进程启动失败；请检查 Node 运行环境，系统会自动重试。'
                return False
            self.state = 'configured'
            self.detail = '登录会话已保存，正在订阅行情并计算指标'
            return True

    async def _wait_for_login(self):
        while self.waiting:
            if await self._step():
                return
            await asyncio.sleep(3)

    async def open_login(self):
        async with self._lock:
            return await self._open_login()

    async def _open_login(self):
        if not self.waiting:
            return {'ok': True, 'state': self.state}
        if not await self.context.desktop._cdp_alive():
            error = await self.context.desktop._workspace_ensure_browser()
            if error:
                return {'ok': False, 'detail': '专用 Chrome 未能启动，请检查 Chrome 安装与启动状态。'}
        try:
            await open_login_page(config.CDP_HTTP_ENDPOINT)
            self._login_opened = True
            return {'ok': True, 'state': self.state}
        except asyncio.CancelledError:
            raise
        except Exception:
            return {'ok': False, 'detail': '暂时无法打开专用 Chrome 登录页，请稍后重试。'}

    async def stop(self):
        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None
