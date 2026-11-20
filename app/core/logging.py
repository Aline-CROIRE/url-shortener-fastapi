"""Logging setup for the app, and helpers to log client input safely."""

import logging

# Longest piece of client input written into one log line
MAX_LOGGED_LENGTH = 100


def setup_logging(debug: bool) -> None:
    """Send our "app.*" log messages to the console."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    # More detail for our own code in debug mode, without noisy library logs
    logging.getLogger("app").setLevel(logging.DEBUG if debug else logging.INFO)


def clean_for_log(text: str, max_length: int = MAX_LOGGED_LENGTH) -> str:
    """Make client input safe to write into a log line.

    Every character that is not printable (line breaks, tabs, terminal escape
    codes) becomes "?", so a client cannot start a fake log line ("log
    injection"). Long input is cut, so a client cannot flood the log.
    """
    cleaned = "".join(char if char.isprintable() else "?" for char in text[:max_length])
    return cleaned + "..." if len(text) > max_length else cleaned


def mask_email(email: str) -> str:
    """Keep only the first letter and the domain: "aline@x.com" -> "a***@x.com".

    Enough to spot a pattern (many failures for one domain or address) without
    writing a full personal email address into the logs.
    """
    local, at, domain = email.strip().rpartition("@")
    if not at or not local:
        return "***"
    return f"{clean_for_log(local[:1])}***@{clean_for_log(domain, 60)}"
