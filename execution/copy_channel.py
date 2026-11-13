"""Configuration shared by the copy snapshot producer and follower."""

import os
from urllib.parse import urlsplit, urlunsplit


def copy_channel_token():
    token = os.environ.get("COPY_CHANNEL_TOKEN", "").strip()
    return token if len(token) >= 32 and token.isascii() and token.isalnum() else ""


def copy_channel_url(configured_url):
    url = urlsplit(configured_url)
    if (url.scheme not in {"ws", "wss"} or not url.hostname
            or url.username or url.password or url.query or url.fragment
            or url.path not in {"/ws", "/ws/copy"}):
        raise ValueError("跟单地址须使用 /ws/copy（兼容旧 /ws 地址）")
    if url.scheme == "ws" and url.hostname not in {"127.0.0.1", "::1", "localhost"}:
        raise ValueError("非本机跟单地址必须使用 wss")
    return urlunsplit(url._replace(path="/ws/copy"))
