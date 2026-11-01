"""HTTP dependencies resolve the current app's services, never module globals."""

from fastapi import Depends, HTTPException
from starlette.requests import HTTPConnection


def get_services(connection: HTTPConnection):
    services = getattr(connection.app.state, "services", None)
    if services is None:
        raise HTTPException(503, "交易服务尚未初始化")
    return services


async def require_trader(connection: HTTPConnection, services=Depends(get_services)):
    return await services.clients.require_trader(connection)
