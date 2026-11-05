"""Read-only access to an explicit set of project documentation files."""

from pathlib import Path

from fastapi import HTTPException
from fastapi.responses import FileResponse

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DOCUMENTS = {
    "opening-breakout": ("docs/OPENING_BREAKOUT_RULES.md", "text/plain"),
    "night-test": ("docs/NIGHT_PAPER_TEST.md", "text/plain"),
    "architecture.svg": ("docs/diagrams/system-architecture.svg", "image/svg+xml"),
    "architecture.png": ("docs/diagrams/system-architecture.png", "image/png"),
    "architecture": ("ARCHITECTURE.md", "text/plain"),
    "terminology": ("docs/TERMINOLOGY.md", "text/plain"),
    "pine-contract": ("docs/PINE_INDICATOR_CONTRACT.md", "text/plain"),
    "pine-prompt": ("docs/PINE_INDICATOR_DEVELOPMENT_PROMPT.md", "text/plain"),
}


def documentation_file(document: str, *, download: bool = False) -> FileResponse:
    entry = DOCUMENTS.get(document)
    if entry is None:
        raise HTTPException(404, "未找到该项目文档")
    relative_path, media_type = entry
    path = PROJECT_ROOT / relative_path
    if not path.is_file():
        raise HTTPException(404, "项目文档暂不可用")
    return FileResponse(
        path,
        media_type=media_type,
        filename=path.name if download else None,
        headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"},
    )
