"""Backend-shaped journal fixtures for doctor and screen tests."""

from datetime import datetime, timezone

from charmer.platform.observation import LogRecord, LogWindow


def log_window(text: str = "", *, gateway_instance: str | None = "fixture-gateway") -> LogWindow:
    """Wrap synthetic log lines in a bounded window with source metadata.

    The fixtures spell envelope times in local time. Normalize those times
    to UTC, as a source adapter does, without interpreting message content.
    Malformed envelopes keep an unknown timestamp; an explicit None instance
    lets tests exercise conservative joins. Cursors are optional, so repeated
    lines remain distinct observations.
    """

    records = []
    for line in text.splitlines():
        try:
            timestamp = datetime.strptime(line[:21], "[%Y-%m-%d %H:%M:%S]").astimezone(timezone.utc)
        except ValueError:
            timestamp = None
        records.append(LogRecord(line, timestamp=timestamp, gateway_instance=gateway_instance))
    return LogWindow(tuple(records), "bounded")
