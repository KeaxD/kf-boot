from __future__ import annotations

import logging
import sys

from hio.base import doing
from keri import help

from kfboot.runtime import setup

logger = help.ogler.getLogger(__name__)


def configure_logging(level: int = logging.INFO) -> None:
    """Set up console logging for the CLI and KERI logger.

    This configures the root logger and also updates KERI's console formatter
    so that log output uses the same application format.
    """

    # Root formatter for all console output.
    formatter = logging.Formatter(
        "%(asctime)s: %(levelname)s from %(name)s \n%(message)s\n"
    )

    root_logger = logging.getLogger()
    root_logger.setLevel(level)

    if not root_logger.handlers:
        # No handlers yet: create one and attach the formatter.
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(formatter)
        handler.setLevel(level)
        root_logger.addHandler(handler)
    else:
        # Existing handlers may have no formatter or too-high levels.
        for handler in root_logger.handlers:
            handler.setLevel(level)
            if handler.formatter is None:
                handler.setFormatter(formatter)

    # Update the KERI logger helper so its console output uses the same formatter.
    if hasattr(help.ogler, "baseConsoleHandler"):
        help.ogler.baseConsoleHandler.setFormatter(formatter)
    if hasattr(help.ogler, "baseFormatter"):
        help.ogler.baseFormatter = formatter

    # Ensure any previously-created logger objects are not stuck at CRITICAL.
    for existing_logger in logging.Logger.manager.loggerDict.values():
        if isinstance(existing_logger, logging.Logger):
            if existing_logger.level > level:
                existing_logger.setLevel(level)

    # If the KERI logger helper exposes a level setter, apply it too.
    try:
        if hasattr(help.ogler, "basicConfig"):
            help.ogler.basicConfig(level=level)
        elif hasattr(help.ogler, "setLevel"):
            help.ogler.setLevel(level)
        elif hasattr(help.ogler, "setLogLevel"):
            help.ogler.setLogLevel(level)
    except Exception:
        pass


def main() -> None:
    configure_logging()
    _app, ctx, doers = setup()
    try:
        logger.info(
            f"Server starting on http://{ctx.config.host}:{ctx.config.port}\n"
            f"Onboarding surface: {ctx.config.onboarding_public_url}\n"
            f"Account surface: {ctx.config.account_public_url}"
        )
        doist = doing.Doist(name="kf-boot", real=True, tock=0.00125)
        doist.do(doers=doers)
    finally:
        ctx.close()
        logger.info(
            "Server stopped"
        )


if __name__ == "__main__":
    main()
