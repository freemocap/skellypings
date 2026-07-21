"""
Quick telemetry stats straight from Firestore — an ad-hoc maintainer view
(not the automated public-stats pipeline).

Run:  uv sync  &&  uv run stats.py

Requires GCP application-default credentials for the telemetry project:
    gcloud config set project freemocap-user-pings
    gcloud auth application-default login

Note: this streams every event into memory — fine for now, not built for millions of rows.
"""
import os
from collections import Counter
from datetime import datetime, timezone

from google.cloud import firestore

VERIFIED_COLLECTION = os.environ.get("FIRESTORE_COLLECTION", "telemetry_events")
UNVERIFIED_COLLECTION = os.environ.get("FIRESTORE_COLLECTION_UNVERIFIED", "telemetry_events_unverified")

db = firestore.Client()


def _load(collection: str) -> list[dict]:
    return [doc.to_dict() for doc in db.collection(collection).stream()]


def _section(title: str) -> None:
    print(f"\n{title}\n" + "-" * len(title))


verified = _load(VERIFIED_COLLECTION)
unverified = _load(UNVERIFIED_COLLECTION)
events = verified + unverified
total = len(events)

print(f"Skellypings telemetry stats  (project: {db.project})")
print("=" * 48)
print(f"Total pings:   {total}")
print(f"  verified:    {len(verified)}")
print(f"  unverified:  {len(unverified)}")

_section("By app")
for app, n in Counter(e.get("app_name", "(unknown)") for e in events).most_common():
    print(f"  {app:<20} {n}")

_section("By event type")
for et, n in Counter(e.get("event_type", "(unknown)") for e in events).most_common():
    print(f"  {et:<20} {n}")

_section("By app version")
for ver, n in Counter(e.get("app_version", "(unknown)") for e in events).most_common(10):
    print(f"  {ver:<20} {n}")

_section("By OS platform")
for osp, n in Counter(e.get("os_platform", "(unknown)") for e in events).most_common(10):
    print(f"  {osp:<28} {n}")

_section("Users")
users = Counter(e.get("user_id") for e in events if e.get("user_id"))
print(f"  unique users:     {len(users)}")
if users:
    print(f"  mean pings/user:  {total / len(users):.1f}")
    print("  top 5 by pings:")
    for uid, n in users.most_common(5):
        print(f"    {str(uid)[:18]:<20} {n}")

_section("Recent 20 pings")
recent = sorted(events, key=lambda e: e.get("timestamp", 0) or 0, reverse=True)[:20]
for e in recent:
    ts = e.get("timestamp", 0) or 0
    when = datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M") if ts else "(no ts)"
    flag = "  ok " if e.get("verified") else "unver"
    print(f"  {when}  {flag}  {e.get('app_name', '?'):<10} {e.get('event_type', '?'):<14} "
          f"{e.get('app_version', '?'):<12} {str(e.get('user_id', ''))[:12]}")
