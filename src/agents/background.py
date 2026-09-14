from __future__ import annotations

import logging
import os
from pathlib import Path
from threading import Lock

from apscheduler.jobstores.sqlalchemy import SQLAlchemyJobStore
from apscheduler.schedulers.background import BackgroundScheduler

from src.agents.recoverly_agent import enabled, run_cycle
from src.config import PROJECT_ROOT

log = logging.getLogger("recoverly.agents.background")

_scheduler: BackgroundScheduler | None = None
_lock = Lock()


def _interval_minutes() -> int:
    try:
        return max(1, int(os.getenv("AUTONOMOUS_AGENT_INTERVAL_MINUTES", "15")))
    except ValueError:
        return 15


def _database_url() -> str:
    configured = os.getenv("AUTONOMOUS_AGENT_JOBSTORE_URL", "").strip()
    if configured:
        return configured
    path = Path(PROJECT_ROOT) / "data" / "recoverly_jobs.sqlite"
    path.parent.mkdir(parents=True, exist_ok=True)
    return f"sqlite:///{path.as_posix()}"


def start_background_agent() -> BackgroundScheduler | None:
    global _scheduler
    if not enabled():
        log.info("autonomous Recoverly agent is disabled")
        return None
    with _lock:
        if _scheduler and _scheduler.running:
            return _scheduler
        scheduler = BackgroundScheduler(
            jobstores={"default": SQLAlchemyJobStore(url=_database_url())},
            timezone="UTC",
            daemon=True,
        )
        scheduler.add_job(
            run_cycle,
            "interval",
            minutes=_interval_minutes(),
            id="recoverly-autonomous-cycle",
            replace_existing=True,
            coalesce=True,
            max_instances=1,
        )
        scheduler.start()
        _scheduler = scheduler
        log.info("autonomous Recoverly agent scheduled every %d minutes", _interval_minutes())
        return scheduler
