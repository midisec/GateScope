from __future__ import annotations

import os


def main() -> None:
    if os.environ.get("GATESCOPE_DATABASE_URL"):
        import uvicorn

        from apps.api.formal_app import create_formal_app

        uvicorn.run(
            create_formal_app(),
            host=os.environ.get("HOST", "127.0.0.1"),
            port=int(os.environ.get("PORT", "8777")),
            # OAuth authorize URLs contain state and a PKCE challenge. Nginx
            # supplies path-specific access logging; never log query strings here.
            access_log=False,
        )
        return
    from app import main as legacy_main

    legacy_main()

__all__ = ["main"]
