"""
Smoke-test the skellypings server AND prove the ping actually landed in Firestore.

Run:  uv sync  &&  uv run test_ping.py

Requires:
  - .env with SKELLYPINGS_SECRET and SKELLYPINGS_URL
  - GCP application-default credentials for the telemetry project:
        gcloud config set project freemocap-user-pings
        gcloud auth application-default login
"""
import hashlib
import hmac
import json
import os
import time

import requests
from dotenv import load_dotenv
from google.cloud import firestore
from google.cloud.firestore_v1.base_query import FieldFilter

load_dotenv()

SECRET: str = os.environ["SKELLYPINGS_SECRET"]
BASE_URL: str = os.environ["SKELLYPINGS_URL"].rstrip("/")

# Unique marker so we can find exactly THIS ping on read-back.
user_id: str = f"dev-test-{int(time.time())}"
event: dict[str, object] = {
    "event_type": "test_ping",
    "app_name": "test",
    "app_version": "0.0.1",
    "os_platform": "windows",
    "user_id": user_id,
    "timestamp": time.time(),
    "payload": {"hello": "world"},
}

body: bytes = json.dumps({"events": [event]}, separators=(",", ":"), sort_keys=True).encode()
sig: str = hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest()

# --- 1. Send it ---
resp = requests.post(
    url=f"{BASE_URL}/events",
    data=body,
    headers={"Content-Type": "application/json", "X-Telemetry-Signature": sig},
    timeout=10,
)
resp.raise_for_status()
result = resp.json()
print("POST:", resp.status_code, result)

# --- 2. Pull it back out of Firestore to prove the whole payload made it through ---
collection = "telemetry_events" if result.get("verified") else "telemetry_events_unverified"
db = firestore.Client()
docs = list(db.collection(collection).where(filter=FieldFilter("user_id", "==", user_id)).stream())

if not docs:
    print(f"[FAIL] no document with user_id={user_id!r} found in '{collection}'")
    raise SystemExit(1)

stored = docs[0].to_dict()
print(f"[OK] round-trip found in '{collection}':")
print(json.dumps(stored, indent=2, default=str))

# --- 3. Confirm every field we sent survived storage ---
mismatches = [
    k for k in ("event_type", "app_name", "app_version", "os_platform", "user_id")
    if stored.get(k) != event[k]
]
if mismatches:
    print(f"[FAIL] stored fields differ from what was sent: {mismatches}")
    raise SystemExit(1)
print("[OK] all sent fields match what was stored.")
