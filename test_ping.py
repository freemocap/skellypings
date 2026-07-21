import hashlib
import hmac
import json
import os

import requests
from dotenv import load_dotenv

load_dotenv()

SECRET: str = os.environ["SKELLYPINGS_SECRET"]
BASE_URL: str = os.environ["SKELLYPINGS_URL"].rstrip("/")

body: bytes = json.dumps({
    "events": [{
        "event_type": "test_ping",
        "app_name": "test",
        "app_version": "0.0.1",
        "os_platform": "windows",
        "user_id": "dev-test",
        "timestamp": 1700000000.0,
        "payload": {"hello": "world"},
    }]
}, separators=(",", ":"), sort_keys=True).encode()

sig: str = hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest()

resp = requests.post(
    url=f"{BASE_URL}/events",
    data=body,
    headers={"Content-Type": "application/json", "X-Telemetry-Signature": sig},
    timeout=10,
)
resp.raise_for_status()
print(resp.status_code, resp.json())
