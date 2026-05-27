from __future__ import annotations

from hio.core import http
from keri import help
from keri.app import indirecting

from kfboot.app import Context, create_app
from kfboot.config import Config
from kfboot.sweeping import CleanupDoer

logger = help.ogler.getLogger(__name__)


def setup(config: Config | None = None, *, temp: bool = False):
    """Build the Falcon app, service context, and root HIO doers."""

    app, ctx = create_app(config=config, temp=temp)
    return app, ctx, build_doers(app, ctx)


def build_doers(app, ctx: Context) -> list:
    """Return the HIO doers that run the service."""

    server = indirecting.createHttpServer(
        host=ctx.config.host,
        port=ctx.config.port,
        app=app,
    )
    service_doers = [http.ServerDoer(server=server)]

    if ctx.cleanup.expected_running:
        service_doers.append(
            CleanupDoer(
                expirer=ctx.exchanger.expirer,
                interval=ctx.config.cleanup_interval_seconds,
                batch_size=ctx.config.cleanup_batch_size,
                time_budget_seconds=ctx.config.cleanup_time_budget_seconds,
                state=ctx.cleanup,
            )
        )
    elif not ctx.config.cleanup_runner_enabled:
        logger.info("Periodic cleanup sweeper disabled because cleanup_runner_enabled is false")
    else:
        logger.info("Periodic cleanup sweeper disabled because cleanup_interval_seconds <= 0")

    return service_doers
