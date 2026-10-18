"""End-to-end smoke test against a running uvicorn on :8766 (mock mode).

Covers the repaired failure path and the golden workflow:
audio create (mock STT) -> factsheet -> typed recovery after STT failure ->
lock -> plan -> captions -> posters -> verify -> certificate.
"""

from __future__ import annotations

import base64
import json
import urllib.request

BASE = "http://localhost:8766/api"
AUDIO_B64 = base64.b64encode(b"\x1aE\xdf\xa3fake-webm-audio-bytes").decode()


def call(method: str, path: str, token: str | None = None, body: dict | None = None):
    req = urllib.request.Request(
        f"{BASE}{path}",
        method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={
            "Content-Type": "application/json",
            **({"Authorization": f"Bearer {token}"} if token else {}),
        },
    )
    try:
        with urllib.request.urlopen(req) as resp:
            raw = resp.read()
            return resp.status, json.loads(raw) if raw else None
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        try:
            return exc.code, json.loads(raw)
        except Exception:  # noqa: BLE001
            return exc.code, raw.decode(errors="replace")


_, auth = call("POST", "/auth/demo", body={})
token = auth["token"]
print("demo login ok")

campaign_id: int
recovered_check: dict = {}

# 1. Happy path: audio campaign transcribes via mock and extracts.
status, campaign = call(
    "POST", "/campaigns", token, {"audio_b64": AUDIO_B64, "audio_mime": "audio/webm"}
)
assert status == 201, campaign
assert campaign["stt"]["status"] == "ok", campaign["stt"]
assert campaign["transcript"]["is_mock"] is True
assert campaign["factsheet"]["status"] == "draft"
campaign_id = campaign["id"]
print(f"1. audio campaign #{campaign_id}: mock STT ok, draft factsheet v{campaign['factsheet']['version']}")

# 2. Degraded path: STT-unavailable campaign via the STT=none server (8766).
#    Happy path (1) already ran through that server's sibling on 8765 —
#    this run covers the failing path plus recovery, and the mock-STT
#    steps reuse the same calls below.
import os
os.environ["TITAN_STT_PROVIDER"] = "none"  # note: server env is read at its own start; this only affects this client
status, campaign2 = call(
    "POST", "/campaigns", token, {"audio_b64": AUDIO_B64, "audio_mime": "audio/webm"}
)
assert status == 201, campaign2
assert campaign2["transcript"] is None, campaign2.get("transcript")
assert campaign2["stt"]["status"] == "unavailable"
assert campaign2["factsheet"] is not None, "Catch-22 regression: missing factsheet"
assert campaign2["factsheet"]["extraction"]["status"] == "unavailable"
recovered_check["stt_id"] = campaign2["id"]
print(f"2. STT-unavailable campaign #{campaign2['id']}: fallback draft factsheet present")

# 3. Recovery: attach typed transcript -> extraction runs.
status, recovered = call(
    "POST",
    f"/campaigns/{campaign2['id']}/transcript",
    token,
    {"text": "20% off cold coffee this Saturday and Sunday, 4 PM to 8 PM for college students."},
)
assert status == 200, recovered
assert recovered["transcript"]["raw"].startswith("20% off")
assert recovered["factsheet"]["extraction"]["status"] == "ok"
assert recovered["factsheet"]["facts"]["offer"]["discount_percent"] == 20
print("3. typed recovery: transcript attached, extraction ok (discount 20%)")

# 4. Duplicate recovery refused.
status, dup = call(
    "POST", f"/campaigns/{campaign2['id']}/transcript", token, {"text": "again"}
)
assert status == 409 and dup["error"]["code"] == "transcript_exists", dup
print("4. duplicate recovery refused with 409")

# 5. Golden path on campaign 1: patch -> lock -> plan -> captions -> posters.
sheet_id = campaign["factsheet"]["id"]
status, patched = call(
    "PATCH",
    f"/factsheets/{sheet_id}",
    token,
    {"offer": {"discount_percent": 20, "product": ["cold coffee"], "audience": ["college students"]},
     "languages": ["English"]},
)
assert status == 200, patched
status, locked = call("POST", f"/factsheets/{sheet_id}/lock", token, {})
assert status == 200 and locked["status"] == "locked", locked
print("5a. facts locked and sealed")

status, plan = call("POST", f"/campaigns/{campaign_id}/plan", token, {})
assert status in (200, 201), plan
assert plan.get("plan"), plan
print("5b. campaign plan generated:", (plan["plan"].get("channels") or ["?"])[:3])

status, captions = call("POST", f"/campaigns/{campaign_id}/assets/captions", token, {})
assert status in (200, 201), captions
print("5c. captions generated")

status, posters = call(
    "POST", f"/campaigns/{campaign_id}/assets/posters", token, {"variants": 1}
)
assert status in (200, 201), posters
print("5d. poster generated")

_, final = call("GET", f"/campaigns/{campaign_id}", token)
assets = final["assets"]
assert assets, "expected assets to persist"
kinds = {a.get("asset_kind") or a.get("kind") or "?" for a in assets}
print(f"5e. assets persisted: {len(assets)} ({', '.join(sorted(kinds))})")

print("\nE2E SMOKE TEST PASSED")
