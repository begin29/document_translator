from __future__ import annotations

import threading
import time

from app.config import get_settings, worker_identity
from app.core.errors import InjectedCrash
from app.core.service import process_available, reap, sync_gate
from app.db.session import session_factory
from app.observability.logging import configure_logging, get_logger
from app.observability.metrics import HEARTBEAT
from app.observability.tracing import configure_agent_tracing, configure_tracing, flush_tracing

log = get_logger()


def loop(worker_id: str, stop: threading.Event) -> None:
    factory = session_factory()
    while not stop.is_set():
        HEARTBEAT.labels(worker_id=worker_id).set(time.time())
        worked = False
        try:
            with factory() as session:
                reap(session)
                sync_gate(session)
                session.commit()
                worked = process_available(session, worker_id)
        except InjectedCrash:
            log.bind(worker_id=worker_id, stage="translate").info("injected_crash")
            worked = True
        except Exception as exc:
            log.bind(worker_id=worker_id, stage="worker").error("worker_error", error=str(exc))
            time.sleep(1)
        if not worked:
            time.sleep(0.3)


def main() -> None:
    configure_logging()
    configure_tracing()
    configure_agent_tracing()
    settings = get_settings()
    worker_id = worker_identity()
    threads = max(2, min(8, settings.provider_max_concurrency))
    stop = threading.Event()
    pool = [threading.Thread(target=loop, args=(f"{worker_id}-{index}", stop), daemon=True) for index in range(threads)]
    for thread in pool:
        thread.start()
    log.bind(worker_id=worker_id, stage="worker").info("worker_started", threads=threads)
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        stop.set()
        flush_tracing()


if __name__ == "__main__":
    main()
