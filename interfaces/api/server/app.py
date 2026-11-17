"""FastAPI application entry point."""

import logging
import os
import threading
from contextlib import asynccontextmanager

from fastapi import FastAPI

from core.cloud_autonomy_outbox import CloudSupervisorEventOutbox
from core.cloud_autonomy_supervisor import CloudAutonomyWorkerSupervisor
from core.cloud_autonomy_worker import (
    report_supervisor_event,
    report_supervisor_outbox_heartbeat,
    run_worker,
)
from core.system_state import SystemState
from interfaces.api.routes.router import router

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(application: FastAPI):
    """Start and stop the optional, bounded Cloud specialist dispatcher."""
    enabled = os.getenv("AMIKI_CLOUD_AUTONOMY_WORKER_ENABLED", "false").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }
    stop_event = threading.Event()
    outbox_stop_event = threading.Event()
    supervisor = None
    supervisor_thread = None
    outbox = None
    outbox_supervisor = None
    outbox_thread = None
    application.state.cloud_autonomy_worker_enabled = enabled
    application.state.cloud_autonomy_worker_running = False
    application.state.cloud_autonomy_worker_thread = None
    application.state.cloud_autonomy_worker_supervisor = None
    application.state.cloud_autonomy_supervisor_event_outbox = None
    application.state.cloud_autonomy_supervisor_event_outbox_available = False
    application.state.cloud_autonomy_supervisor_event_outbox_supervisor = None
    application.state.cloud_autonomy_supervisor_event_outbox_thread = None
    application.state.cloud_autonomy_supervisor_event_outbox_stop_event = outbox_stop_event

    if enabled:
        try:
            outbox = CloudSupervisorEventOutbox()
        except Exception:
            logger.error("amiki_cloud_supervisor_event_outbox_unavailable")
        else:
            def run_outbox_delivery(worker_stop: threading.Event) -> None:
                outbox.run(
                    worker_stop,
                    report_supervisor_event,
                    heartbeat=report_supervisor_outbox_heartbeat,
                )

            outbox_supervisor = CloudAutonomyWorkerSupervisor(
                run_outbox_delivery,
                event_reporter=outbox.enqueue,
                component="supervisor_outbox_delivery",
            )
            application.state.cloud_autonomy_supervisor_event_outbox = outbox
            application.state.cloud_autonomy_supervisor_event_outbox_available = True
            application.state.cloud_autonomy_supervisor_event_outbox_supervisor = outbox_supervisor
            if outbox_supervisor.start(
                outbox_stop_event,
                thread_name="amiki-cloud-supervisor-event-outbox-supervisor",
            ):
                outbox_thread = outbox_supervisor.supervisor_thread
                application.state.cloud_autonomy_supervisor_event_outbox_thread = outbox_thread
            else:
                logger.error("amiki_cloud_supervisor_event_outbox_supervisor_start_failed")

        supervisor = CloudAutonomyWorkerSupervisor(
            run_worker,
            event_reporter=outbox.enqueue if outbox is not None else None,
        )
        supervisor_thread = threading.Thread(
            target=supervisor.run,
            args=(stop_event,),
            name="amiki-cloud-autonomy-supervisor",
            daemon=True,
        )
        application.state.cloud_autonomy_worker_supervisor = supervisor
        application.state.cloud_autonomy_worker_thread = supervisor_thread
        supervisor_thread.start()
        application.state.cloud_autonomy_worker_running = supervisor.snapshot()[
            "worker_running"
        ]

    try:
        yield
    finally:
        stop_event.set()
        if supervisor_thread is not None:
            supervisor_thread.join(timeout=6.0)
        if supervisor is not None:
            application.state.cloud_autonomy_worker_running = supervisor.snapshot()[
                "worker_running"
            ]
        # Stop delivery only after the supervisor has had a chance to enqueue its
        # shutdown event. Any remaining rows stay durable for the next Core start.
        outbox_stop_event.set()
        outbox_thread = getattr(
            application.state,
            "cloud_autonomy_supervisor_event_outbox_thread",
            outbox_thread,
        )
        if outbox_thread is not None:
            outbox_thread.join(timeout=7.0)


app = FastAPI(title="Amiki API", version="1.0.0", lifespan=lifespan)

app.state.system_state = SystemState.from_runtime()

app.include_router(router)
