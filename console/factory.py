"""Application factory; production resources are created only during startup."""

from contextlib import asynccontextmanager
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from console.shared import CONSOLE_FILE
import logging

logger = logging.getLogger(__name__)


def create_app(services=None, *, paths=None):
    @asynccontextmanager
    async def lifespan(app):
        context = app.state.services
        if context is None:
            from console.runtime.bootstrap import build_services

            context = build_services(paths)
            app.state.services = context
            context.app = app
        logging.basicConfig(
            level=getattr(logging, context.config.LOG_LEVEL, logging.INFO),
            format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        )
        logging.getLogger("ib_async").setLevel(logging.WARNING)
        try:
            await context.lifecycle.on_startup()
            await context.news._news_startup()
            yield
        finally:
            try:
                for cleanup in (
                    context.tasks.stop_all,
                    context.news._news_shutdown,
                    context.lifecycle.on_shutdown,
                ):
                    try:
                        await cleanup()
                    except Exception:
                        logger.exception("应用退出资源清理失败: %s", cleanup.__name__)
            finally:
                if context.execution_writer_acquired:
                    from execution.writer_lock import release_writer_lock

                    release_writer_lock(owner=context)
                    context.execution_writer_acquired = False
                context.close_log_handler()

    app = FastAPI(title="autotrade 下单控制台", version="0.1.0", lifespan=lifespan)
    app.state.services = services
    if services is not None:
        services.app = app

    from console.routes import (
        system,
        clients,
        copy,
        settings,
        desktop,
        snapshot,
        events,
        signals,
        research,
        precheck,
        automatic,
        night_test,
        trading,
        accounts,
        lifecycle,
        news,
        reports,
    )

    for module in (
        system,
        clients,
        copy,
        settings,
        desktop,
        snapshot,
        events,
        signals,
        research,
        precheck,
        automatic,
        night_test,
        trading,
        accounts,
        lifecycle,
        news,
        reports,
    ):
        app.include_router(module.router)
    app.mount(
        "/static",
        StaticFiles(directory=str(CONSOLE_FILE.parent / "static")),
        name="static",
    )

    @app.middleware("http")
    async def request_guard(request: Request, call_next):
        context = app.state.services
        if context is None:
            return JSONResponse({"detail": "交易服务尚未初始化"}, status_code=503)
        return await context.system._no_cache_static(request, call_next)

    return app
