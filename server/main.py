"""
Cloud Run telemetry ingestion service.

Accepts JSON telemetry events via POST and stores them in Firestore.
Events with valid HMAC signatures go into the verified collection;
events with missing or invalid signatures go into a separate unverified
collection (for from-source / dev builds).

Provides a backup endpoint that exports events to JSONL in Cloud Storage.

Rate-limits requests per IP to prevent abuse from running up Cloud Run costs.
"""

import hashlib
import hmac
import json
import logging
import os
import threading
import time

from fastapi import FastAPI, HTTPException, Header, Request
from fastapi.responses import JSONResponse
from google.cloud import firestore, storage
from pydantic import BaseModel

logger = logging.getLogger(__name__)

SHARED_SECRET: str = os.environ["SKELLYPINGS_SECRET"]
VERIFIED_COLLECTION: str = os.environ.get("FIRESTORE_COLLECTION", "telemetry_events")
UNVERIFIED_COLLECTION: str = os.environ.get("FIRESTORE_COLLECTION_UNVERIFIED", "telemetry_events_unverified")
BACKUP_BUCKET: str = os.environ["BACKUP_BUCKET"]

# Rate limit: max requests per IP within the window. These are intentionally
# generous for legitimate use but will block sustained flooding.
RATE_LIMIT_MAX_REQUESTS: int = int(os.environ.get("RATE_LIMIT_MAX_REQUESTS", "60"))
RATE_LIMIT_WINDOW_SECONDS: float = float(os.environ.get("RATE_LIMIT_WINDOW_SECONDS", "60"))

app = FastAPI()
db = firestore.Client()
gcs = storage.Client()


# ---------------------------------------------------------------------------
# In-memory per-IP rate limiter (sliding window counter)
#
# Since Cloud Run runs a single container (max_instances=1), in-memory state
# is sufficient. If the container restarts, the rate limit state resets —
# this is fine because a restart also interrupts any ongoing attack.
# ---------------------------------------------------------------------------

class RateLimiter:
    """Thread-safe sliding-window rate limiter keyed by IP address.

    Tracks IPs that get rate-limited so they can be periodically flushed
    to Firestore for a persistent audit trail.
    """

    def __init__(self, max_requests: int, window_seconds: float) -> None:
        self._max_requests: int = max_requests
        self._window_seconds: float = window_seconds
        self._requests: dict[str, list[float]] = {}
        self._lock: threading.Lock = threading.Lock()

        # Track rate-limit events to flush to Firestore. Each entry maps
        # an IP to {"first_seen": float, "last_seen": float, "count": int}.
        self._flagged: dict[str, dict[str, float | int]] = {}
        self._flagged_lock: threading.Lock = threading.Lock()

    def is_allowed(self, ip: str) -> bool:
        """Return True if the IP is within the rate limit, False otherwise."""
        now = time.monotonic()
        cutoff = now - self._window_seconds

        with self._lock:
            timestamps = self._requests.get(ip, [])
            timestamps = [t for t in timestamps if t > cutoff]

            if len(timestamps) >= self._max_requests:
                self._requests[ip] = timestamps
                self._record_flagged(ip=ip)
                return False

            timestamps.append(now)
            self._requests[ip] = timestamps
            return True

    def _record_flagged(self, ip: str) -> None:
        """Record that an IP was rate-limited."""
        now = time.time()
        with self._flagged_lock:
            if ip in self._flagged:
                self._flagged[ip]["last_seen"] = now
                self._flagged[ip]["count"] = int(self._flagged[ip]["count"]) + 1
            else:
                self._flagged[ip] = {"first_seen": now, "last_seen": now, "count": 1}

    def drain_flagged(self) -> dict[str, dict[str, float | int]]:
        """Return and clear all accumulated rate-limit events."""
        with self._flagged_lock:
            flagged = self._flagged.copy()
            self._flagged.clear()
        return flagged

    def cleanup(self) -> None:
        """Remove stale entries. Call periodically to prevent memory growth."""
        now = time.monotonic()
        cutoff = now - self._window_seconds

        with self._lock:
            stale_ips = [
                ip for ip, timestamps in self._requests.items()
                if not timestamps or timestamps[-1] <= cutoff
            ]
            for ip in stale_ips:
                del self._requests[ip]


_rate_limiter = RateLimiter(
    max_requests=RATE_LIMIT_MAX_REQUESTS,
    window_seconds=RATE_LIMIT_WINDOW_SECONDS,
)

# Flush interval for writing rate-limit events to Firestore (seconds).
# Batching prevents an attacker from generating a Firestore write per blocked request.
_RATE_LIMIT_FLUSH_INTERVAL: float = 30.0
_flush_stop_event = threading.Event()


def _flush_flagged_loop() -> None:
    """Background thread: periodically write accumulated rate-limit events to Firestore."""
    while not _flush_stop_event.wait(timeout=_RATE_LIMIT_FLUSH_INTERVAL):
        _flush_flagged_to_firestore()
    # Final flush on shutdown
    _flush_flagged_to_firestore()


def _flush_flagged_to_firestore() -> None:
    """Drain flagged IPs and write them to Firestore."""
    flagged = _rate_limiter.drain_flagged()
    if not flagged:
        return

    collection = db.collection(VERIFIED_COLLECTION)
    fs_batch = db.batch()
    for ip, info in flagged.items():
        doc_ref = collection.document()
        fs_batch.set(doc_ref, {
            "event_type": "rate_limited",
            "ip": ip,
            "first_seen": info["first_seen"],
            "last_seen": info["last_seen"],
            "blocked_count": info["count"],
            "ingested_at": time.time(),
            # These fields match the TelemetryEvent shape so they appear in
            # the same collection and flow through the backup pipeline.
            "app_version": "server",
            "os_platform": "cloud_run",
            "user_id": "system",
            "timestamp": info["first_seen"],
            "payload": {
                "ip": ip,
                "blocked_count": info["count"],
                "window_start": info["first_seen"],
                "window_end": info["last_seen"],
            },
        })
    try:
        fs_batch.commit()
        logger.info("Flushed %d rate_limited events to Firestore", len(flagged))
    except Exception:
        logger.error("Failed to flush rate_limited events to Firestore", exc_info=True)


_flush_thread: threading.Thread | None = None


@app.on_event("startup")
def _start_flush_thread() -> None:
    global _flush_thread
    _flush_thread = threading.Thread(
        target=_flush_flagged_loop,
        daemon=True,
        name="rate-limit-flush",
    )
    _flush_thread.start()


@app.on_event("shutdown")
def _stop_flush_thread() -> None:
    _flush_stop_event.set()
    if _flush_thread is not None:
        _flush_thread.join(timeout=5.0)


@app.middleware("http")
async def rate_limit_middleware(request: Request, call_next):
    """Reject requests from IPs that exceed the rate limit.

    This runs BEFORE any route logic, so flooding with junk requests
    consumes minimal CPU — we check the IP and return 429 immediately
    without parsing the body or touching Firestore.
    """
    # Cloud Run sets X-Forwarded-For with the real client IP
    forwarded_for = request.headers.get("x-forwarded-for", "")
    client_ip = forwarded_for.split(",")[0].strip() if forwarded_for else (request.client.host if request.client else "unknown")

    if not _rate_limiter.is_allowed(ip=client_ip):
        logger.warning("RATE_LIMITED ip=%s path=%s", client_ip, request.url.path)
        return JSONResponse(
            status_code=429,
            content={"detail": "Rate limit exceeded"},
        )

    # Periodically clean up stale entries (cheap, doesn't need to happen every request)
    if hash(client_ip) % 100 == 0:
        _rate_limiter.cleanup()

    return await call_next(request)


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------

class TelemetryEvent(BaseModel):
    event_type: str
    app_version: str
    os_platform: str
    user_id: str
    timestamp: float
    payload: dict[str, object]


class TelemetryBatch(BaseModel):
    events: list[TelemetryEvent]


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------

def _check_signature(body: bytes, signature: str | None) -> bool:
    """Verify HMAC-SHA256 signature of the request body.

    Returns True if the signature is present and valid, False otherwise.
    """
    if not signature:
        return False
    expected = hmac.new(
        key=SHARED_SECRET.encode(),
        msg=body,
        digestmod=hashlib.sha256,
    ).hexdigest()
    return hmac.compare_digest(expected, signature)


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.post("/events")
async def ingest_events(
    request: Request,
    x_telemetry_signature: str | None = Header(default=None),
) -> dict[str, object]:
    """Ingest a batch of telemetry events into Firestore.

    Events with a valid HMAC signature are stored in the verified collection.
    Events with a missing or invalid signature are stored in a separate
    unverified collection — this captures telemetry from users running
    from source or dev builds that don't have the production secret.
    """
    raw_body: bytes = await request.body()
    is_verified: bool = _check_signature(body=raw_body, signature=x_telemetry_signature)
    target_collection: str = VERIFIED_COLLECTION if is_verified else UNVERIFIED_COLLECTION

    batch_data = TelemetryBatch.model_validate_json(raw_body)
    fs_batch = db.batch()

    collection = db.collection(target_collection)
    for event in batch_data.events:
        doc_ref = collection.document()
        fs_batch.set(doc_ref, {
            **event.model_dump(),
            "ingested_at": time.time(),
            "verified": is_verified,
        })

    fs_batch.commit()

    if is_verified:
        logger.info("Stored %d verified events", len(batch_data.events))
    else:
        logger.info("Stored %d unverified events (missing or invalid signature)", len(batch_data.events))

    return {
        "stored": len(batch_data.events),
        "verified": is_verified,
    }


@app.post("/backup")
async def run_backup(
    x_telemetry_signature: str = Header(),
) -> dict[str, object]:
    """
    Export all events since the last backup to JSONL files in Cloud Storage.

    Backs up both the verified and unverified collections to separate files.
    Tracks the last backup timestamp in a Firestore document at _meta/last_backup.
    """
    raw_body = b"backup"
    if not _check_signature(body=raw_body, signature=x_telemetry_signature):
        raise HTTPException(status_code=401, detail="Invalid signature")

    meta_ref = db.collection("_meta").document("last_backup")
    meta_doc = meta_ref.get()
    last_backup_ts: float = meta_doc.to_dict().get("timestamp", 0.0) if meta_doc.exists else 0.0

    now = time.time()
    timestamp_str = time.strftime("%Y-%m-%d_%H%M%S", time.gmtime(now))

    results: dict[str, object] = {}

    for collection_name in [VERIFIED_COLLECTION, UNVERIFIED_COLLECTION]:
        query = (
            db.collection(collection_name)
            .where("ingested_at", ">=", last_backup_ts)
            .where("ingested_at", "<", now)
            .order_by("ingested_at")
        )

        docs = list(query.stream())
        if not docs:
            results[collection_name] = {"status": "no_new_events", "file": ""}
            continue

        lines: list[str] = []
        for doc in docs:
            data = doc.to_dict()
            data["_firestore_id"] = doc.id
            lines.append(json.dumps(data, default=str))

        jsonl_content = "\n".join(lines) + "\n"
        blob_name = f"backups/{timestamp_str}_{collection_name}.jsonl"

        bucket = gcs.bucket(BACKUP_BUCKET)
        blob = bucket.blob(blob_name)
        blob.upload_from_string(jsonl_content, content_type="application/jsonl")

        results[collection_name] = {
            "status": "ok",
            "file": blob_name,
            "events_exported": len(docs),
        }

    meta_ref.set({"timestamp": now})

    return results


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}
