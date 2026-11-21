"""Local-only TradingView session capture. Never return cookies to HTTP clients."""

import asyncio
import json
import math
import os
from pathlib import Path
import tempfile
import time
from urllib.parse import urlsplit

LOGIN_URL = 'https://www.tradingview.com/accounts/signin/'
TV_HOSTS = {'tradingview.com', 'www.tradingview.com', 'cn.tradingview.com'}
COOKIE_NAMES = ('sessionid', 'sessionid_sign')


def valid_credentials(value):
    return isinstance(value, dict) and all(
        isinstance(value.get(name), str) and 0 < len(value[name]) <= 4096
        and all(32 < ord(char) < 127 for char in value[name]) for name in COOKIE_NAMES
    )


def credentials_present(path):
    try:
        with Path(path).open(encoding='utf-8') as source:
            return valid_credentials(json.loads(source.read(16385)))
    except (OSError, ValueError, UnicodeError):
        return False


def save_credentials(path, value):
    if not valid_credentials(value):
        raise ValueError('TradingView 登录会话不完整')
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(prefix='.tv-session-', dir=path.parent)
    try:
        # mkstemp starts at 0600, including before any secret is written.
        with os.fdopen(descriptor, 'w', encoding='utf-8') as target:
            os.fchmod(target.fileno(), 0o600)
            json.dump({key: value[key] for key in COOKIE_NAMES}, target)
            target.flush()
            os.fsync(target.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def credentials_from_cookies(cookies, *, now=None):
    now = time.time() if now is None else now
    if not isinstance(cookies, list):
        return None
    scopes = {}
    for cookie in cookies:
        if not isinstance(cookie, dict):
            continue
        domain = str(cookie.get('domain', '')).lstrip('.').lower()
        expires = cookie.get('expires', -1)
        if (domain not in TV_HOSTS or cookie.get('name') not in COOKIE_NAMES
                or cookie.get('path') != '/' or cookie.get('partitionKey')
                or type(expires) not in (int, float) or not math.isfinite(expires)
                or (expires != -1 and expires <= now)):
            continue
        pair = scopes.setdefault(domain, {})
        name = cookie['name']
        if name in pair and pair[name] != cookie.get('value'):
            return None
        pair[name] = cookie.get('value')
    complete = [pair for pair in scopes.values() if valid_credentials(pair)]
    unique = {tuple(pair[key] for key in COOKIE_NAMES) for pair in complete}
    if len(unique) != 1:
        return None
    return dict(zip(COOKIE_NAMES, next(iter(unique))))


def _local_endpoint(endpoint):
    address = urlsplit(endpoint)
    if (address.scheme != 'http' or address.hostname not in {'localhost', '127.0.0.1', '::1'}
            or address.username or address.password or address.query or address.fragment
            or address.path not in ('', '/')):
        raise ValueError('登录引导仅支持本机专用 Chrome')


def _tv_page(page):
    address = urlsplit(page.get('url', ''))
    return address.scheme == 'https' and address.hostname in TV_HOSTS


async def read_browser_credentials(endpoint):
    from controller.window_chart import _pages
    from controller.tv_cdp import TradingViewCDPController

    _local_endpoint(endpoint)
    async with asyncio.timeout(6):
        pages = await asyncio.to_thread(_pages, endpoint)
        page = next((page for page in pages if _tv_page(page)), None)
        if page is None:
            return None
        async with TradingViewCDPController(endpoint, target_id=page['id']) as chrome:
            response = await chrome._send_cdp('Network.getCookies', {
                'urls': ['https://www.tradingview.com/', 'https://cn.tradingview.com/'],
            })
        return credentials_from_cookies(response.get('cookies', []))


async def open_login_page(endpoint):
    from controller.window_chart import _pages
    from controller.tv_cdp import TradingViewCDPController

    _local_endpoint(endpoint)
    async with asyncio.timeout(6):
        pages = await asyncio.to_thread(_pages, endpoint)
        if not pages:
            raise RuntimeError('专用 Chrome 尚未就绪')
        page = next((page for page in pages if _tv_page(page)
                     and urlsplit(page['url']).path.startswith('/accounts/signin')), None)
        async with TradingViewCDPController(endpoint, target_id=(page or pages[0])['id']) as chrome:
            if page:
                await chrome._send_cdp('Target.activateTarget', {'targetId': page['id']})
            else:
                await chrome._send_cdp('Target.createTarget', {'url': LOGIN_URL})
