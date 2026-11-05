"""Documentation is served from an allowlist without starting trading services."""

import pytest
from fastapi.testclient import TestClient


@pytest.mark.parametrize(
    "name,content_type,expected",
    [
        ("architecture.svg", "image/svg+xml", "<svg"),
        ("architecture.png", "image/png", None),
        ("architecture", "text/plain", "公共执行模型"),
        ("opening-breakout", "text/plain", "10:30"),
        ("night-test", "text/plain", "夜盘模拟测试"),
        ("terminology", "text/plain", "策略模型"),
        ("pine-contract", "text/plain", "统一导出属性"),
        ("pine-prompt", "text/plain", "Entry_Signal"),
    ],
)
def test_documentation_files_are_available(console_app, name, content_type, expected):
    response = TestClient(console_app.app).get(f"/system-docs/{name}")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith(content_type)
    assert response.headers["cache-control"] == "no-store"
    if expected:
        assert expected in response.text
    else:
        assert response.content.startswith(b"\x89PNG\r\n\x1a\n")


def test_documentation_download_and_allowlist(console_app):
    client = TestClient(console_app.app)
    response = client.get("/system-docs/architecture.png?download=true")
    assert response.status_code == 200
    assert 'attachment; filename="system-architecture.png"' == response.headers[
        "content-disposition"
    ]
    for name in (".env", "settings.json", "AGENTS.md", "pine_strategies.json"):
        assert client.get(f"/system-docs/{name}").status_code == 404
    assert client.get("/system-docs/%2e%2e%2f.env").status_code == 404


def test_architecture_entry_and_standalone_page(console_app):
    client = TestClient(console_app.app)
    index = client.get("/static/index.html")
    assert 'href="/static/architecture.html" target="_blank"' in index.text
    page = client.get("/static/architecture.html")
    assert page.status_code == 200
    assert "策略输出规范" in page.text
    assert "下载 PNG" in page.text
    for asset in ("architecture.js", "architecture.css"):
        assert client.get(f"/static/{asset}").status_code == 200
