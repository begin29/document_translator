from __future__ import annotations

import os

import uvicorn

from app.config import ensure_auth_secret


def main() -> None:
    ensure_auth_secret()
    uvicorn.run(
        "app.realtime.app:app",
        host="0.0.0.0",
        port=int(os.environ.get("REALTIME_PORT", "8002")),
        loop="uvloop",
        http="httptools",
    )


if __name__ == "__main__":
    main()
