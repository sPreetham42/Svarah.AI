"""Voice-only flow: spoken review, Magic Hour key rotation, plan headline repair."""

from __future__ import annotations

import asyncio
import base64
from dataclasses import replace

import httpx
import pytest

from app.api.voice_turn import quick_intent
from app.config import get_settings
from app.errors import ProviderUnavailableError
from app.services import magichour
from app.services.campaign_brain import _repair_headlines, validate_plan_raw


@pytest.mark.parametrize(
    "text",
    ["Yes.", "Yes, looks right.", "okay go ahead", "That's correct", "Haan, sahi hai", "हाँ, ठीक है"],
)
def test_plain_yes_is_confirm(text):
    assert quick_intent(text) == "confirm"


@pytest.mark.parametrize(
    "text",
    ["Yes but make it 25 percent", "Change the discount to 25%.", "No, that is wrong", "The shop is Sharma Cafe"],
)
def test_anything_with_a_change_is_not_a_quick_confirm(text):
    assert quick_intent(text) is None


def test_start_over_is_restart():
    assert quick_intent("Let's start over please") == "restart"


def test_voice_turn_in_mock_mode_changes_nothing(client):
    created = client.post("/api/campaigns", json={"text": "20% off cold coffee on Saturday."}).json()
    before = created["factsheet"]["facts"]
    response = client.post(
        f"/api/campaigns/{created['id']}/voice-turn",
        json={"audio_b64": base64.b64encode(b"not-real-audio").decode(), "audio_mime": "audio/webm"},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    # The mock transcript is an offer, not a yes: nothing is confirmed or edited.
    assert body["intent"] == "unclear"
    assert body["campaign"]["factsheet"]["facts"] == before
    assert body["campaign"]["factsheet"]["status"] != "locked"


def test_voice_turn_is_owner_scoped(client, other_account):
    created = client.post("/api/campaigns", json={"text": "20% off cold coffee on Saturday."}).json()
    response = client.post(
        f"/api/campaigns/{created['id']}/voice-turn",
        json={"audio_b64": base64.b64encode(b"x").decode()},
        headers=other_account,
    )
    assert response.status_code == 404


def _magichour_client(monkeypatch, handler, keys=("key-a", "key-b")):
    settings = replace(get_settings(), magichour_api_keys=keys, titan_mode="live")
    client = magichour.MagicHourVideoClient(settings)
    seen: list[tuple[str, str, str]] = []

    async def fake_call(method, path, key, **kwargs):
        seen.append((method, path, key))
        return handler(method, path, key)

    monkeypatch.setattr(client, "_call", fake_call)
    return client, seen


def test_magichour_moves_to_the_next_key_when_one_is_out_of_credits(monkeypatch):
    def handler(method, path, key):
        if key == "key-a":
            return httpx.Response(402, json={"code": "insufficient_credits", "message": "Buy credits"})
        return httpx.Response(200, json={"id": "proj123", "credits_charged": 150})

    client, seen = _magichour_client(monkeypatch, handler)
    task = asyncio.run(client.create("a cafe", seconds=5, size="720P", aspect_ratio="9:16"))
    assert task.video_id == "mh1:proj123"
    assert [key for _, _, key in seen] == ["key-a", "key-b"]

    # The poll must use the key that created the project.
    def poll(method, path, key):
        assert key == "key-b" and path.endswith("/proj123")
        return httpx.Response(200, json={"status": "complete", "downloads": [{"url": "https://cdn.example/v.mp4"}]})

    client, _ = _magichour_client(monkeypatch, poll)
    done = asyncio.run(client.retrieve(task.video_id))
    assert done.status == "completed" and done.url == "https://cdn.example/v.mp4"


def test_magichour_reports_the_refusal_when_every_key_is_refused(monkeypatch):
    client, seen = _magichour_client(
        monkeypatch, lambda *_: httpx.Response(402, json={"code": "insufficient_credits"})
    )
    with pytest.raises(ProviderUnavailableError) as raised:
        asyncio.run(client.create("a cafe", seconds=5, size="720P", aspect_ratio="9:16"))
    assert raised.value.code == "provider_quota_exceeded"
    assert len(seen) == 2


def test_magichour_bad_request_is_not_retried_on_other_keys(monkeypatch):
    client, seen = _magichour_client(
        monkeypatch, lambda *_: httpx.Response(422, json={"message": "end_seconds too long"})
    )
    with pytest.raises(ProviderUnavailableError) as raised:
        asyncio.run(client.create("a cafe", seconds=5, size="720P", aspect_ratio="9:16"))
    assert raised.value.code == "provider_bad_request"
    assert len(seen) == 1


def test_tokenless_poster_headline_is_replaced_by_the_offer_line():
    tokens = {"PRODUCT": "cold coffee", "DISCOUNT": "25%"}
    raw = {
        "strategy": {"angle": "weekend"},
        "copy_templates": {
            "instagram": "{{PRODUCT}} at {{DISCOUNT}} off",
            "whatsapp": "{{DISCOUNT}} off {{PRODUCT}}",
            "poster_headline": "Chill out this weekend",
            "voice_script": "Enjoy {{DISCOUNT}} off {{PRODUCT}}",
        },
        "poster_briefs": [{"art_prompt": "a cafe counter, no text"}],
        "video_brief": {"prompt": "push-in on a cafe counter"},
    }
    assert any("poster_headline" in issue for issue in validate_plan_raw(raw, tokens, []))
    _repair_headlines(raw, tokens)
    assert raw["copy_templates"]["poster_headline"] == "{{DISCOUNT}} off {{PRODUCT}}"
    assert not any("poster_headline" in issue for issue in validate_plan_raw(raw, tokens, []))


# ── Brag Director storyboard: creative only, never a fact ─────────────
def _board(**overrides):
    from app.services.brag_director import default_storyboard

    board = default_storyboard()
    for path, value in overrides.items():
        group, key = path.split("__")
        board[group][key] = value
    return board


def test_default_storyboard_is_valid():
    from app.services.brag_director import validate_storyboard

    assert validate_storyboard(_board(), {"PRODUCT": "cold coffee"}) == []


@pytest.mark.parametrize("line", ["Only 20 today", "Half price fun", "Weekend treat", "See you Saturday"])
def test_storyboard_copy_may_not_state_a_fact(line):
    from app.services.brag_director import validate_storyboard

    issues = validate_storyboard(_board(video__hook=line), {"PRODUCT": "cold coffee"})
    assert any("video.hook" in issue for issue in issues)


def test_storyboard_copy_may_use_known_tokens_only():
    from app.services.brag_director import validate_storyboard

    tokens = {"PRODUCT": "cold coffee"}
    assert validate_storyboard(_board(video__hook="{{PRODUCT}} fans, listen"), tokens) == []
    assert validate_storyboard(_board(video__hook="{{PRICE}} fans, listen"), tokens)


def test_salvage_keeps_valid_fields_and_replaces_only_the_bad_ones():
    from app.services.brag_director import DEFAULT_STORYBOARD, salvage

    raw = _board(video__hook="Weekend treat", poster__kicker="Fresh from the oven")
    raw["style"] = "elegant"
    raw["palette"] = {"bg": "#3B1F10", "accent": "#FFD9A0"}
    board, replaced = salvage(raw, {"PRODUCT": "pizza"})
    assert replaced == ["video.hook"]
    assert board["video"]["hook"] == DEFAULT_STORYBOARD["video"]["hook"]
    assert board["poster"]["kicker"] == "Fresh from the oven"
    assert board["style"] == "elegant" and board["palette"]["bg"] == "#3b1f10"


def test_render_spec_draws_facts_from_tokens_and_fixes_unreadable_colours():
    from app.services.brag_renderer import build_spec

    board = _board(video__hook="{{PRODUCT}} fans, listen")
    board["palette"] = {"bg": "#101010", "accent": "#141414"}  # accent invisible on bg
    tokens = {"PRODUCT": "cold coffee", "DISCOUNT": "25%", "DAYS": "Saturday & Sunday", "WINDOW": "4 PM–8 PM"}
    spec = build_spec(board, tokens, mode="video", business_name="Sharma Cafe", headline="25% off cold coffee", art=None, voice_seconds=17.0)
    assert spec["facts"] == tokens
    assert spec["video"]["hook"] == "cold coffee fans, listen"
    assert spec["discountNeedsOff"] is True
    assert spec["accent"] != "#141414"
    names = [name for name, _ in spec["video"]["timeline"]]
    assert names == ["hook", "reveal", "offer", "when", "outro"]
    # Stretched so the 17 s voice-over finishes before the video does.
    assert 17.9 <= sum(seconds for _, seconds in spec["video"]["timeline"]) <= 22.0

    flat = build_spec(board, {"PRODUCT": "school bags", "DISCOUNT": "100 off"}, mode="video", business_name="Shop", headline="100 off school bags", art=None)
    assert flat["discountNeedsOff"] is False
    assert "when" not in [name for name, _ in flat["video"]["timeline"]]  # no days or times to show
