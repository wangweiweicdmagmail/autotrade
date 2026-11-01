"""Production ASGI entrypoint. Run: python -m uvicorn console.app:app.

Pine indicators own strategy signals; execution services own order lifecycles.
Importing this module does not load production account state or connect services.
"""

from console.factory import create_app

app = create_app()
