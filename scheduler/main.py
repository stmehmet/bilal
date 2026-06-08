"""Entry point for the Adhan scheduler service."""

import logging
import os
import signal
import sys
import time

from config import load_config, save_config
from geolocation import detect_location
from adhan_scheduler import AdhanSchedulerService

LOG_FORMAT = os.getenv(
    "LOG_FORMAT",
    "%(asctime)s [%(name)s] %(levelname)s: %(message)s",
)
logging.basicConfig(
    level=getattr(logging, os.getenv("LOG_LEVEL", "INFO").upper(), logging.INFO),
    format=LOG_FORMAT,
)
logger = logging.getLogger("bilal")


def auto_setup() -> None:
    """Attempt automatic location detection on first boot."""
    config = load_config()
    if config.get("latitude") is not None:
        return

    logger.info("No location configured – running auto-detection...")
    loc = detect_location()
    if loc:
        config.update(loc)
        try:
            save_config(config)
            logger.info("Auto-detected location: %s, %s", loc["city"], loc["country"])
        except OSError as exc:
            # A save failure (e.g. full disk) must NOT crash the scheduler — doing
            # so just spins a restart loop that re-hits the geo provider until it
            # rate-limits us.  Carry on; the location re-detects next boot and the
            # web layer surfaces the underlying disk problem.
            logger.error(
                "Detected %s, %s but could not persist it: %s",
                loc["city"], loc["country"], exc,
            )
    else:
        logger.warning("Auto-detection failed; user must configure manually")


def main() -> None:
    auto_setup()
    service = AdhanSchedulerService()

    def _shutdown(signum, frame):
        logger.info("Shutting down scheduler...")
        service.stop()
        sys.exit(0)

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)

    service.start()
    logger.info("Bilal Adhan Scheduler is running")

    # Keep the main thread alive
    while True:
        time.sleep(60)


if __name__ == "__main__":
    main()
