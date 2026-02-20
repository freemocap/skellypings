"""
Telemetry client for the desktop app's Python backend.

Collects events in memory and flushes them to the telemetry server
in batches on a background thread. Telemetry failures never crash the host app.

Usage:
    from telemetry_client import TelemetryClient

    telemetry = TelemetryClient(
        server_url="https://your-cloud-run-url.run.app",
        secret="your-shared-secret",
        app_version="1.2.3",
    )

    telemetry.track("feature_used", payload={"feature": "export_csv"})
    telemetry.track("app_launched")
    telemetry.track("error", payload={"error": "ValueError", "message": "bad input"})

    # On app shutdown (also registered with atexit automatically):
    telemetry.shutdown()
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

# Persistent anonymous user ID, stored alongside the app.
# Change "your_app_name" to your actual app name.
_USER_ID_FILE: Path = Path.home() / ".config" / "your_app_name" / "telemetry_uid"


def _get_or_create_user_id() -> str:
    """Read or generate a persistent anonymous user ID."""
    _USER_ID_FILE.parent.mkdir(parents=True, exist_ok=True)
    if _USER_ID_FILE.exists():
        uid = _USER_ID_FILE.read_text().strip()
        if uid:
            return uid
    uid = uuid.uuid4().hex
    _USER_ID_FILE.write_text(uid)
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
    """

    def __init__(
        self,
        server_url: str,
        secret: str,
        app_version: str,
        flush_interval_seconds: float = 60.0,
        flush_batch_size: int = 50,
    ) -> None:
        self._server_url: str = server_url.rstrip("/")
        self._secret: str = secret
        self._app_version: str = app_version
        self._flush_interval: float = flush_interval_seconds
        self._flush_batch_size: int = flush_batch_size

        self._user_id: str = _get_or_create_user_id()
        self._os_platform: str = f"{platform.system()} {platform.release()}"

        self._buffer: list[dict[str, object]] = []
        self._lock: threading.Lock = threading.Lock()
        self._stop_event: threading.Event = threading.Event()

        self._flush_thread: threading.Thread = threading.Thread(
            target=self._flush_loop,
            daemon=True,
            name="telemetry-flush",
        )
        self._flush_thread.start()

        atexit.register(self.shutdown)

    def track(self, event_type: str, payload: dict[str, object] | None = None) -> None:
        """Queue a telemetry event."""
        event = {
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
        """Background loop that periodically flushes the buffer."""
        while not self._stop_event.wait(timeout=self._flush_interval):
            self._flush()

    def _flush(self) -> None:
        """Send all buffered events to the server."""
        with self._lock:
            if not self._buffer:
                return
            events = self._buffer.copy()
            self._buffer.clear()

        body = json.dumps({"events": events}, separators=(",", ":"), sort_keys=True).encode()
        signature = _sign(body=body, secret=self._secret)

        try:
            response = requests.post(
                url=f"{self._server_url}/events",
                data=body,
                headers={
                    "Content-Type": "application/json",
                    "X-Telemetry-Signature": signature,
                },
                timeout=10,
            )
            response.raise_for_status()
        except Exception:
            # Telemetry must never crash the host app. Log and move on.
            logger.warning("Failed to flush telemetry events", exc_info=True)
