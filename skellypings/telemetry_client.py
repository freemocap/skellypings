"""
Telemetry client for desktop apps.

Collects events in memory and flushes them to the telemetry server
in batches on a background thread. Telemetry failures never crash the host app.

On flush failure, events are returned to the buffer for retry. If the server
returns a persistent auth error (401/403), the client backs off exponentially
and caps the buffer to avoid unbounded memory growth.
"""

import atexit
import hashlib
import hmac
import json
import logging
import platform
import threading
import time
import uuid
from pathlib import Path

import requests

logger = logging.getLogger(__name__)

# After this many consecutive failures, stop retrying and discard events.
_MAX_CONSECUTIVE_FAILURES: int = 5

# Never let the retry buffer grow beyond this many events.
_MAX_BUFFER_SIZE: int = 500


def _get_or_create_user_id(user_id_file: Path) -> str:
    """Read or generate a persistent anonymous user ID."""
    user_id_file.parent.mkdir(parents=True, exist_ok=True)
    if user_id_file.exists():
        uid = user_id_file.read_text().strip()
        if uid:
            return uid
    uid = uuid.uuid4().hex
    user_id_file.write_text(uid)
    return uid


def _sign(body: bytes, secret: str) -> str:
    """Compute HMAC-SHA256 signature."""
    return hmac.new(
        key=secret.encode(),
        msg=body,
        digestmod=hashlib.sha256,
    ).hexdigest()


class TelemetryClient:
    """
    Batched, async telemetry client.

    Events accumulate in memory and are flushed to the server
    every `flush_interval_seconds` or when `flush_batch_size` is reached.

    On transient failures, events are returned to the buffer for retry.
    On persistent auth failures (401/403), the client backs off exponentially
    and eventually discards events to prevent memory growth.
    """

    def __init__(
        self,
        server_url: str,
        secret: str,
        app_version: str,
        user_id_file: Path,
        flush_interval_seconds: float = 60.0,
        flush_batch_size: int = 50,
    ) -> None:
        self._server_url: str = server_url.rstrip("/")
        self._secret: str = secret
        self._app_version: str = app_version
        self._flush_interval: float = flush_interval_seconds
        self._flush_batch_size: int = flush_batch_size

        self._user_id: str = _get_or_create_user_id(user_id_file=user_id_file)
        self._os_platform: str = f"{platform.system()} {platform.release()}"

        self._buffer: list[dict[str, object]] = []
        self._lock: threading.Lock = threading.Lock()
        self._stop_event: threading.Event = threading.Event()
        self._consecutive_failures: int = 0
        self._disabled: bool = False

        self._flush_thread: threading.Thread = threading.Thread(
            target=self._flush_loop,
            daemon=True,
            name="telemetry-flush",
        )
        self._flush_thread.start()

        atexit.register(self.shutdown)

    @property
    def user_id(self) -> str:
        return self._user_id

    def track(self, event_type: str, payload: dict[str, object] | None = None) -> None:
        """Queue a telemetry event. No-op if telemetry has been disabled due to persistent errors."""
        if self._disabled:
            return

        event: dict[str, object] = {
            "event_type": event_type,
            "app_version": self._app_version,
            "os_platform": self._os_platform,
            "user_id": self._user_id,
            "timestamp": time.time(),
            "payload": payload or {},
        }
        with self._lock:
            self._buffer.append(event)
            should_flush = len(self._buffer) >= self._flush_batch_size

        if should_flush:
            self._flush()

    def shutdown(self) -> None:
        """Flush remaining events and stop the background thread."""
        self._stop_event.set()
        self._flush()
        self._flush_thread.join(timeout=5.0)

    def _flush_loop(self) -> None:
        """Background loop that periodically flushes the buffer, with backoff on repeated failures."""
        while not self._stop_event.is_set():
            # Exponential backoff: base interval * 2^failures, capped at 15 minutes
            backoff: float = min(self._flush_interval * (2 ** self._consecutive_failures), 900.0)
            if self._stop_event.wait(timeout=backoff):
                break
            self._flush()

    def _flush(self) -> None:
        """Send all buffered events to the server. On failure, return events to the buffer."""
        if self._disabled:
            return

        with self._lock:
            if not self._buffer:
                return
            events: list[dict[str, object]] = self._buffer.copy()
            self._buffer.clear()

        body: bytes = json.dumps({"events": events}, separators=(",", ":"), sort_keys=True).encode()
        signature: str = _sign(body=body, secret=self._secret)

        try:
            response: requests.Response = requests.post(
                url=f"{self._server_url}/events",
                data=body,
                headers={
                    "Content-Type": "application/json",
                    "X-Telemetry-Signature": signature,
                },
                timeout=10,
            )
            response.raise_for_status()
            self._consecutive_failures = 0
        except requests.exceptions.HTTPError as exc:
            status_code: int = exc.response.status_code if exc.response is not None else 0
            self._consecutive_failures += 1

            if status_code in (401, 403):
                # Auth error — server is rejecting us. Back off aggressively.
                if self._consecutive_failures >= _MAX_CONSECUTIVE_FAILURES:
                    logger.warning(
                        "Telemetry disabled: server returned %d on %d consecutive attempts. "
                        "Discarding %d buffered events.",
                        status_code,
                        self._consecutive_failures,
                        len(events),
                    )
                    self._disabled = True
                    return
                else:
                    logger.warning(
                        "Telemetry flush got %d (attempt %d/%d), will retry with backoff",
                        status_code,
                        self._consecutive_failures,
                        _MAX_CONSECUTIVE_FAILURES,
                    )
            else:
                logger.warning("Telemetry flush failed with HTTP %d", status_code, exc_info=True)

            # Return events to the buffer for retry, respecting the max buffer size
            self._return_events_to_buffer(events=events)

        except Exception:
            self._consecutive_failures += 1
            logger.warning("Telemetry flush failed (attempt %d)", self._consecutive_failures, exc_info=True)

            if self._consecutive_failures >= _MAX_CONSECUTIVE_FAILURES:
                logger.warning(
                    "Telemetry disabled after %d consecutive failures. Discarding %d events.",
                    self._consecutive_failures,
                    len(events),
                )
                self._disabled = True
                return

            self._return_events_to_buffer(events=events)

    def _return_events_to_buffer(self, events: list[dict[str, object]]) -> None:
        """Put failed events back into the buffer, dropping oldest if over the cap."""
        with self._lock:
            # Prepend the failed events (they're older), then trim to max size
            self._buffer = events + self._buffer
            if len(self._buffer) > _MAX_BUFFER_SIZE:
                dropped: int = len(self._buffer) - _MAX_BUFFER_SIZE
                self._buffer = self._buffer[-_MAX_BUFFER_SIZE:]
                logger.warning("Telemetry buffer overflow, dropped %d oldest events", dropped)
