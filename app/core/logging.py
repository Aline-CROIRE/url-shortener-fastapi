"""Logging setup for the app."""

import logging


def setup_logging(debug: bool) -> None:
    """Send our "app.*" log messages to the console."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    # More detail for our own code in debug mode, without noisy library logs
    logging.getLogger("app").setLevel(logging.DEBUG if debug else logging.INFO)
