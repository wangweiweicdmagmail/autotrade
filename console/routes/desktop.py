"""HTTP adapters; application logic lives in services."""

import io
import json
import re
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

from fastapi import Depends, HTTPException, Request, Response
from console.schemas import TvShowRequest, TvViewRequest, TvGridRequest, TvMarkerRequest, TvBindRequest
from console.dependencies import get_services, require_trader
from fastapi import APIRouter

router = APIRouter()
EXTENSION_DIR = Path(__file__).resolve().parents[2] / "extensions" / "autotrade-tv"


@router.get('/api/tv/charts')
async def api_tv_charts(trader: str=Depends(require_trader), *, services=Depends(get_services)):
    return await services.desktop.api_tv_charts(trader)


@router.post('/api/tv/bind')
async def api_tv_bind(req: TvBindRequest, request: Request, trader: str=Depends(require_trader), *, services=Depends(get_services)):
    return await services.desktop.api_tv_bind(req,trader,origin=f'{request.url.scheme}://{request.url.netloc}')


@router.get("/api/tv/extension.zip", include_in_schema=False)
def download_tv_extension(request: Request) -> Response:
    # Serve only the packaged files and scope the detection marker to this server.
    url = request.url
    host = url.hostname or ""
    if not re.fullmatch(r"[A-Za-z0-9.-]+", host):
        raise HTTPException(400, "当前控制台地址无法生成 Chrome 扩展包")
    origin = f"{url.scheme}://{url.netloc}"
    manifest = json.loads((EXTENSION_DIR / "manifest.json").read_text(encoding="utf-8"))
    manifest["web_accessible_resources"][0]["matches"] = [f"{url.scheme}://{host}/*"]
    output = io.BytesIO()
    with ZipFile(output, "w", ZIP_DEFLATED) as archive:
        archive.writestr("autotrade-tv/manifest.json", json.dumps(manifest, ensure_ascii=False))
        archive.writestr("autotrade-tv/defaults.json", json.dumps({"origin": origin}))
        for name in ("console-bridge.js", "worker.js", "chart-grid.js", "popup.html", "popup.js", "presence.json", "README.md"):
            archive.write(EXTENSION_DIR / name, f"autotrade-tv/{name}")
    return Response(output.getvalue(), media_type="application/zip", headers={
        "Content-Disposition": 'attachment; filename="autotrade-tv.zip"',
        "Cache-Control": "no-store",
    })


@router.post("/api/tv/show")
async def api_tv_show(
    req: TvShowRequest,
    trader: str = Depends(require_trader),
    *,
    services=Depends(get_services),
) -> dict:
    return await services.desktop.api_tv_show(req=req, trader=trader)


@router.post("/api/tv/view")
async def api_tv_view(
    req: TvViewRequest,
    request: Request,
    trader: str = Depends(require_trader),
    *,
    services=Depends(get_services),
) -> dict:
    origin = f"{request.url.scheme}://{request.url.netloc}"
    return await services.desktop.api_tv_view(req=req, trader=trader, origin=origin)


@router.post("/api/tv/grid")
async def api_tv_grid(req: TvGridRequest, request: Request,
                      trader: str = Depends(require_trader), *, services=Depends(get_services)) -> dict:
    origin = f"{request.url.scheme}://{request.url.netloc}"
    return await services.desktop.api_tv_grid(req, trader, origin=origin)


@router.post("/api/tv/entry-markers")
async def api_tv_entry_markers(req: TvMarkerRequest, request: Request,
                              trader: str = Depends(require_trader), *, services=Depends(get_services)) -> dict:
    return await services.desktop.api_tv_entry_markers(req, trader,
        origin=f"{request.url.scheme}://{request.url.netloc}")
