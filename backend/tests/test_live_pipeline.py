"""Validation, Campaign Director, TTS, image and AI-video pipeline tests.

Everything here is hermetic. "Live mode" tests switch `TITAN_MODE=live` and
replace the HTTP layer (or the client factory) with a fake, so they prove the
live code path and the documented request shapes without touching a network —
they are NOT evidence that the real providers were reached.
"""

from __future__ import annotations

import asyncio
import base64
import io
import json
import wave

import httpx
import pytest

from app.errors import ProviderUnavailableError, STTUnavailableError
from app.services.agnes import LLMResult

BRIEF = "20% off cold coffee this Saturday and Sunday, 4 PM to 8 PM at our cafe for college students."
FAKE_AUDIO_B64 = base64.b64encode(b"\x1aE\xdf\xa3fake-webm-audio").decode()

LIVE_ENV = {
    "TITAN_MODE": "live",
    "TITAN_AGNES_API_BASE": "https://agnes.test/v1",
    "TITAN_AGNES_API_KEY": "agnes-test-key",
    "TITAN_SARVAM_API_KEY": "sarvam-test-key",
    "TITAN_PROVIDER_MAX_RETRIES": "0",
}


def _wav_bytes(seconds: float = 0.2) -> bytes:
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as writer:
        writer.setnchannels(1)
        writer.setsampwidth(2)
        writer.setframerate(8000)
        writer.writeframes(b"\x00\x01" * int(8000 * seconds))
    return buffer.getvalue()


def _png_bytes(size: int = 512) -> bytes:
    from PIL import Image

    buffer = io.BytesIO()
    Image.new("RGB", (size, size), (200, 120, 40)).save(buffer, format="PNG")
    return buffer.getvalue()


def _create(client, text: str = BRIEF) -> dict:
    response = client.post("/api/campaigns", json={"text": text})
    assert response.status_code == 201, response.text
    return response.json()


def _locked(client) -> tuple[int, int]:
    campaign = _create(client)
    sheet_id = campaign["factsheet"]["id"]
    client.patch(f"/api/factsheets/{sheet_id}", json={"languages": ["English", "Hindi"]})
    assert client.post(f"/api/factsheets/{sheet_id}/lock").status_code == 200
    return campaign["id"], sheet_id


def _planned(client) -> int:
    campaign_id, _ = _locked(client)
    assert client.post(f"/api/campaigns/{campaign_id}/plan").status_code == 200
    return campaign_id


class FakeLLM:
    """Scripted Agnes stand-in: returns/raises the queued items in order."""

    name = "agnes"

    def __init__(self, *outputs):
        self.outputs = list(outputs)
        self.calls: list[tuple[str, str]] = []

    async def complete_json(self, system, user, *, max_tokens=2000, temperature=0.4):
        self.calls.append((system, user))
        item = self.outputs.pop(0) if len(self.outputs) > 1 else self.outputs[0]
        if isinstance(item, Exception):
            raise item
        text = item if isinstance(item, str) else json.dumps(item)
        return LLMResult(text=text, provider="agnes", model="agnes-3.0-flash", is_mock=False)


_REAL_ASYNC_CLIENT = httpx.AsyncClient


def _transport(monkeypatch, module, handler) -> list[httpx.Request]:
    """Route a service module's AsyncClient through an in-memory transport."""
    seen: list[httpx.Request] = []
    real = _REAL_ASYNC_CLIENT  # not the current attribute: helpers may stack

    def _wrapped(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    def _factory(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(_wrapped)
        return real(*args, **kwargs)

    monkeypatch.setattr(module.httpx, "AsyncClient", _factory)
    return seen


# ── fact validation ───────────────────────────────────────────────────
def test_extraction_is_followed_by_both_validation_layers(client):
    sheet = _create(client)["factsheet"]
    validation = sheet["validation"]
    assert validation["status"] in ("passed", "warnings")
    assert validation["stale"] is False
    assert validation["deterministic"]["findings"] == []
    semantic = validation["semantic"]
    assert semantic["status"] == "ok"
    assert semantic["is_mock"] is True  # offline stand-in is labelled


def test_deterministic_errors_are_field_level_and_block_the_lock(client):
    sheet = _create(client)["factsheet"]
    response = client.patch(
        f"/api/factsheets/{sheet['id']}",
        json={"offer": {"date_start": "2026-11-10", "date_end": "2026-11-01"}},
    )
    assert response.status_code == 200, response.text
    validation = response.json()["validation"]
    assert validation["status"] == "failed"
    finding = next(f for f in validation["deterministic"]["findings"] if f["code"] == "date_range_inverted")
    assert finding["field"] == "offer.date_end"
    assert finding["severity"] == "error"
    # The earlier semantic report describes the old facts.
    assert validation["semantic"]["stale"] is True

    lock = client.post(f"/api/factsheets/{sheet['id']}/lock")
    assert lock.status_code == 422
    assert lock.json()["error"]["code"] == "facts_invalid"


def test_missing_required_fact_is_reported_not_invented(client):
    sheet = _create(client, "Come visit our shop this weekend.")["factsheet"]
    codes = {(f["field"], f["code"]) for f in sheet["validation"]["deterministic"]["findings"]}
    assert ("offer.product", "missing_required") in codes
    assert sheet["facts"]["offer"]["product"] == []


def test_validate_endpoint_refreshes_a_stale_report(client):
    sheet = _create(client)["factsheet"]
    client.patch(f"/api/factsheets/{sheet['id']}", json={"offer": {"product": ["masala chai"]}})
    response = client.post(f"/api/factsheets/{sheet['id']}/validate")
    assert response.status_code == 200, response.text
    validation = response.json()["validation"]
    assert validation["stale"] is False
    assert validation["semantic"]["stale"] is False
    # The mock evidence check notices the product is not in the transcript.
    assert any(
        f["field"] == "offer.product" and f["code"] == "unsupported_claim"
        for f in validation["semantic"]["findings"]
    )


def test_live_semantic_validation_is_schema_checked(client, env_override, monkeypatch):
    from app.services import fact_validation

    sheet = _create(client)["factsheet"]
    env_override(**LIVE_ENV)
    fake = FakeLLM(
        {
            "findings": [
                {"field": "offer.discount_percent", "severity": "error", "issue": "contradiction",
                 "explanation": "The transcript says 20% but the facts say 25%.", "suggestion": "Use 20."},
                {"field": "not.a.field", "severity": "warning", "issue": "other",
                 "explanation": "Something general."},
            ],
            "summary": "One contradiction.",
        }
    )
    monkeypatch.setattr(fact_validation, "get_llm_client", lambda settings=None: fake)

    response = client.post(f"/api/factsheets/{sheet['id']}/validate")
    assert response.status_code == 200, response.text
    validation = response.json()["validation"]
    semantic = validation["semantic"]
    assert semantic["status"] == "ok" and semantic["is_mock"] is False
    assert semantic["provider"] == "agnes"
    assert [f["field"] for f in semantic["findings"]] == ["offer.discount_percent", "general"]
    assert all(f["source"] == "agnes" for f in semantic["findings"])
    assert validation["status"] == "failed"
    # The model saw the transcript and the canonical facts.
    envelope = json.loads(fake.calls[0][1])
    assert envelope["TRANSCRIPT"] == BRIEF
    assert envelope["FACTS"]["offer"]["discount_percent"] == 20


def test_live_semantic_failure_is_reported_as_unavailable(client, env_override, monkeypatch):
    from app.services import fact_validation

    sheet = _create(client)["factsheet"]
    env_override(**LIVE_ENV)
    monkeypatch.setattr(
        fact_validation,
        "get_llm_client",
        lambda settings=None: FakeLLM(
            ProviderUnavailableError("Agnes rejected the API key.", code="provider_auth_error")
        ),
    )
    validation = client.post(f"/api/factsheets/{sheet['id']}/validate").json()["validation"]
    assert validation["semantic"]["status"] == "unavailable"
    assert validation["semantic"]["code"] == "provider_auth_error"
    assert validation["semantic"]["findings"] == []
    # Deterministic validation still ran and still decides lockability.
    assert validation["deterministic"]["status"] == "passed"


def test_live_semantic_gives_up_after_bounded_malformed_answers(client, env_override, monkeypatch):
    from app.services import fact_validation

    sheet = _create(client)["factsheet"]
    env_override(**LIVE_ENV)
    fake = FakeLLM("I think the facts look fine!")
    monkeypatch.setattr(fact_validation, "get_llm_client", lambda settings=None: fake)
    semantic = client.post(f"/api/factsheets/{sheet['id']}/validate").json()["validation"]["semantic"]
    assert semantic["status"] == "unavailable"
    assert len(fake.calls) == fact_validation.SEMANTIC_MAX_ATTEMPTS


# ── live gating: no silent mocks ──────────────────────────────────────
def test_live_stt_without_a_key_reports_configuration_and_invents_nothing(client, env_override):
    env_override(TITAN_MODE="live", TITAN_STT_PROVIDER="sarvam")
    response = client.post(
        "/api/campaigns", json={"audio_b64": FAKE_AUDIO_B64, "audio_mime": "audio/webm"}
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["transcript"] is None
    assert body["stt"]["status"] == "unavailable"
    assert body["stt"]["code"] == "provider_not_configured"
    assert "TITAN_SARVAM_API_KEY" in body["stt"]["message"]
    assert body["factsheet"] is not None  # manual entry stays possible


def test_live_auto_stt_never_selects_another_engine(client, env_override):
    from app.config import get_settings
    from app.services.stt import SarvamSaarasSTTProvider, get_stt_provider

    env_override(TITAN_MODE="live", TITAN_STT_PROVIDER="auto")
    assert isinstance(get_stt_provider(get_settings()), SarvamSaarasSTTProvider)


def test_sarvam_stt_request_matches_the_documented_contract(client, env_override, monkeypatch):
    from app.config import get_settings
    from app.services import sarvam

    env_override(**LIVE_ENV)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"request_id": "r-1", "transcript": " बीस प्रतिशत छूट ", "language_code": "hi-IN"},
        )

    seen = _transport(monkeypatch, sarvam, handler)
    result = asyncio.run(
        sarvam.get_stt_sarvam(get_settings()).transcribe(
            b"audio-bytes", mime="audio/webm;codecs=opus", language_hint="hi"
        )
    )
    assert result.transcript == "बीस प्रतिशत छूट"
    assert result.is_mock is False and result.language_code == "hi-IN"

    request = seen[0]
    assert str(request.url) == "https://api.sarvam.ai/speech-to-text"
    assert request.headers["api-subscription-key"] == "sarvam-test-key"
    body = request.content
    assert b'name="model"' in body and b"saaras:v4" in body
    assert b'name="language_code"' in body and b"hi-IN" in body
    assert b'filename="audio.webm"' in body and b"Content-Type: audio/webm\r\n" in body


@pytest.mark.parametrize(
    ("status", "payload", "code"),
    [
        (403, {"error": {"message": "bad key", "code": "invalid_api_key_error"}}, "provider_auth_error"),
        (429, {"error": {"message": "slow down", "code": "rate_limit_exceeded_error"}}, "provider_rate_limited"),
        (429, {"error": {"message": "no credits", "code": "insufficient_quota_error"}}, "provider_quota_exceeded"),
        (400, {"error": {"message": "unsupported audio", "code": "invalid_request_error"}}, "invalid_audio"),
        (500, {"error": {"message": "oops", "code": "internal_server_error"}}, "provider_server_error"),
    ],
)
def test_sarvam_stt_errors_are_normalized(client, env_override, monkeypatch, status, payload, code):
    from app.config import get_settings
    from app.services import sarvam

    env_override(**LIVE_ENV)
    _transport(monkeypatch, sarvam, lambda request: httpx.Response(status, json=payload))
    with pytest.raises(STTUnavailableError) as excinfo:
        asyncio.run(
            sarvam.get_stt_sarvam(get_settings()).transcribe(b"x", mime="audio/wav", language_hint=None)
        )
    assert excinfo.value.code == code


def test_sarvam_moves_to_the_next_key_when_one_is_refused(client, env_override, monkeypatch):
    from app.config import get_settings
    from app.services import sarvam

    env_override(**{
        **LIVE_ENV,
        "TITAN_SARVAM_API_KEY": "key-one",
        "TITAN_SARVAM_API_KEY_2": "key-two",
        "TITAN_SARVAM_API_KEY_3": "key-three",
    })
    monkeypatch.setattr(sarvam, "_last_good_key", None)
    refusals = {
        "key-one": (403, {"error": {"message": "bad key", "code": "invalid_api_key_error"}}),
        "key-two": (429, {"error": {"message": "no credits", "code": "insufficient_quota_error"}}),
    }

    def handler(request: httpx.Request) -> httpx.Response:
        refusal = refusals.get(request.headers["api-subscription-key"])
        if refusal:
            return httpx.Response(refusal[0], json=refusal[1])
        return httpx.Response(200, json={"request_id": "r-3", "transcript": "hello"})

    seen = _transport(monkeypatch, sarvam, handler)
    stt = sarvam.get_stt_sarvam(get_settings())
    result = asyncio.run(stt.transcribe(b"x", mime="audio/wav", language_hint=None))
    assert result.transcript == "hello"
    assert [r.headers["api-subscription-key"] for r in seen] == ["key-one", "key-two", "key-three"]

    # The next call starts with the key that worked.
    asyncio.run(stt.transcribe(b"x", mime="audio/wav", language_hint=None))
    assert seen[-1].headers["api-subscription-key"] == "key-three" and len(seen) == 4


def test_sarvam_does_not_switch_keys_for_a_bad_request(client, env_override, monkeypatch):
    from app.config import get_settings
    from app.services import sarvam

    env_override(**{**LIVE_ENV, "TITAN_SARVAM_API_KEY": "key-one", "TITAN_SARVAM_API_KEY_2": "key-two"})
    monkeypatch.setattr(sarvam, "_last_good_key", None)
    seen = _transport(
        monkeypatch,
        sarvam,
        lambda request: httpx.Response(400, json={"error": {"message": "bad audio", "code": "invalid_request_error"}}),
    )
    with pytest.raises(STTUnavailableError) as excinfo:
        asyncio.run(
            sarvam.get_stt_sarvam(get_settings()).transcribe(b"x", mime="audio/wav", language_hint=None)
        )
    assert excinfo.value.code == "invalid_audio"
    assert len(seen) == 1


# ── Campaign Director ─────────────────────────────────────────────────
LOCALIZED = {
    "Hindi": {
        "instagram": "{{PRODUCT}} का समय! {{AUDIENCE}} के लिए {{DISCOUNT}} की छूट।",
        "voice_script": "{{DAYS}} को {{PRODUCT}} पर {{DISCOUNT}} की छूट पाइए।",
    }
}


def _valid_plan(languages=("English", "Hindi")) -> dict:
    templates = {
        "instagram": "{{PRODUCT}} time! {{DISCOUNT}} off for {{AUDIENCE}}.",
        "whatsapp": "Hello! {{DISCOUNT}} off {{PRODUCT}} on {{DAYS}}, {{WINDOW}}. Reply to know more!",
        "poster_headline": "{{DISCOUNT}} off {{PRODUCT}}",
        "poster_subline": "{{DAYS}} {{WINDOW}}",
        "voice_script": "Enjoy {{DISCOUNT}} off {{PRODUCT}} on {{DAYS}}.",
    }
    return {
        "strategy": {"angle": "Weekend treat", "rationale": "Students want an affordable break."},
        "copy_templates": templates,
        "poster_briefs": [{"art_prompt": "Iced drink on a cafe counter, golden light.", "overlay_layout": "bottom"}],
        "video_brief": {"concept": "Pour shot", "prompt": "Slow pour of a cold drink over ice."},
        "localization": [
            {"language": language, "region": "Local", "audience": "students", "tone": ["friendly"],
             "copy_templates": LOCALIZED.get(language) or {
                 "instagram": templates["instagram"], "voice_script": templates["voice_script"]}}
            for language in languages
        ],
        "missing_information": ["A photo of the shop front would help."],
    }


def test_mock_plan_reports_gaps_and_carries_media_briefs(client):
    campaign = _create(client, "20% off cold coffee at our cafe.")
    sheet_id = campaign["factsheet"]["id"]
    client.post(f"/api/factsheets/{sheet_id}/lock")
    plan = client.post(
        f"/api/campaigns/{campaign['id']}/plan",
        json={"objective": "footfall", "tone": "playful"},
    ).json()
    assert plan["is_mock"] is True
    assert plan["brief"] == {"objective": "footfall", "tone": "playful"}
    assert plan["video_brief"]["prompt"]
    # Days and audience were never given: reported, not invented.
    joined = " ".join(plan["missing_information"]).lower()
    assert "days" in joined and "audience" in joined


def test_plan_rejects_an_unknown_objective(client):
    campaign_id, _ = _locked(client)
    response = client.post(f"/api/campaigns/{campaign_id}/plan", json={"objective": "world-domination"})
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_objective"


def test_live_director_repairs_an_ungrounded_plan(client, env_override, monkeypatch):
    from app.services import campaign_brain

    campaign_id, _ = _locked(client)
    env_override(**LIVE_ENV)
    bad = _valid_plan()
    bad["copy_templates"]["instagram"] = "Flat 50% off cold coffee, today only!"  # invented number
    fake = FakeLLM(bad, _valid_plan())
    monkeypatch.setattr(campaign_brain, "get_llm_client", lambda settings=None: fake)

    response = client.post(f"/api/campaigns/{campaign_id}/plan")
    assert response.status_code == 200, response.text
    plan = response.json()
    assert plan["is_mock"] is False and plan["provider"] == "agnes"
    assert len(fake.calls) == 2
    assert "literal number" in fake.calls[1][1]  # the rejection reason went back to the model
    # The Director is given the validated facts as a token map and told to
    # reference them by token.
    envelope = json.loads(fake.calls[0][1])
    assert envelope["locked_fact_tokens"]["DISCOUNT"] == "20%"
    assert "{{DISCOUNT}}" in fake.calls[0][0]

    read = client.get(f"/api/campaigns/{campaign_id}/plan").json()
    assert read["substituted"]["master"]["poster_headline"] == "20% off cold coffee"
    assert "50%" not in json.dumps(read)


def test_live_director_never_falls_back_to_the_mock_template(client, env_override, monkeypatch):
    from app.services import campaign_brain

    campaign_id, _ = _locked(client)
    env_override(**LIVE_ENV)
    bad = _valid_plan()
    bad["copy_templates"]["whatsapp"] = "Buy now at {{SECRET_PRICE}}"
    fake = FakeLLM(bad)
    monkeypatch.setattr(campaign_brain, "get_llm_client", lambda settings=None: fake)

    response = client.post(f"/api/campaigns/{campaign_id}/plan")
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "plan_invalid"
    assert len(fake.calls) == campaign_brain.PLAN_MAX_ATTEMPTS
    assert client.get(f"/api/campaigns/{campaign_id}/plan").status_code == 404  # nothing stored

    monkeypatch.setattr(
        campaign_brain,
        "get_llm_client",
        lambda settings=None: FakeLLM(
            ProviderUnavailableError("Agnes rate limit reached.", code="provider_rate_limited")
        ),
    )
    response = client.post(f"/api/campaigns/{campaign_id}/plan")
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "provider_rate_limited"


def test_regenerate_stores_a_new_plan_version(client):
    campaign_id = _planned(client)
    again = client.post(f"/api/campaigns/{campaign_id}/plan").json()
    fresh = client.post(f"/api/campaigns/{campaign_id}/plan", json={"regenerate": True}).json()
    assert again["version"] == 1 and fresh["version"] == 2


def test_agnes_chat_request_matches_the_documented_contract(client, env_override, monkeypatch):
    from app.config import get_settings
    from app.services import agnes

    env_override(**LIVE_ENV)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={"id": "c-1", "choices": [{"message": {"content": '{"ok": true}'}}], "usage": {}}
        )

    seen = _transport(monkeypatch, agnes, handler)
    result = asyncio.run(agnes.get_llm_client(get_settings()).complete_json("sys", "user"))
    assert agnes.parse_llm_json(result) == {"ok": True}
    request = seen[0]
    assert str(request.url) == "https://agnes.test/v1/chat/completions"
    assert request.headers["authorization"] == "Bearer agnes-test-key"
    body = json.loads(request.content)
    assert body["model"] == "agnes-3.0-flash"
    assert "response_format" not in body  # not documented for this model


# ── image generation ──────────────────────────────────────────────────
def test_live_poster_uses_grounded_model_art(client, env_override, monkeypatch):
    from app.services import agnes

    campaign_id = _planned(client)
    env_override(**LIVE_ENV)
    png = _png_bytes()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={"created": 1, "data": [{"url": None, "b64_json": base64.b64encode(png).decode()}]}
        )

    seen = _transport(monkeypatch, agnes, handler)
    response = client.post(f"/api/campaigns/{campaign_id}/assets/posters", json={"variants": 1})
    assert response.status_code == 200, response.text
    poster = response.json()[0]
    assert poster["is_mock"] is False and poster["used_fallback"] is False
    assert poster["provider"] == "agnes_image"

    # The Brag Director's storyboard request may overlap the image request,
    # so the image call is picked out by its endpoint rather than by order.
    image_request = next(r for r in seen if str(r.url) == "https://agnes.test/v1/images/generations")
    body = json.loads(image_request.content)
    assert body["model"] == "agnes-image-2.5-flash"
    assert body["size"] == "1K" and body["ratio"] == "3:4"
    assert body["extra_body"] == {"response_format": "url"} and "response_format" not in body
    assert "cold coffee" in body["prompt"] and "NO TEXT" in body["prompt"]
    assert "20%" not in body["prompt"]  # numbers are drawn by the compositor only


def test_live_poster_failure_is_an_error_unless_fallback_is_requested(client, env_override, monkeypatch):
    from app.services import agnes

    campaign_id = _planned(client)
    env_override(**LIVE_ENV)
    _transport(monkeypatch, agnes, lambda request: httpx.Response(200, json={"data": [{"url": None}]}))

    response = client.post(f"/api/campaigns/{campaign_id}/assets/posters", json={"variants": 1})
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "image_bad_response"
    assert client.get(f"/api/campaigns/{campaign_id}/assets").json() == []

    response = client.post(
        f"/api/campaigns/{campaign_id}/assets/posters",
        json={"variants": 1, "allow_fallback_art": True},
    )
    assert response.status_code == 200, response.text
    poster = response.json()[0]
    assert poster["used_fallback"] is True
    assert "no image" in poster["provenance"]["art_error"]


def test_non_image_bytes_from_the_provider_are_rejected(client, env_override, monkeypatch):
    from app.services import agnes

    campaign_id = _planned(client)
    env_override(**LIVE_ENV)
    garbage = base64.b64encode(b"<html>not an image</html>").decode()
    _transport(monkeypatch, agnes, lambda request: httpx.Response(200, json={"data": [{"b64_json": garbage}]}))
    response = client.post(f"/api/campaigns/{campaign_id}/assets/posters", json={"variants": 1})
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "image_bad_response"


# ── Sarvam TTS ────────────────────────────────────────────────────────
def test_mock_tts_creates_a_labelled_playable_voice_asset(client):
    campaign_id = _planned(client)
    options = client.get("/api/voice/options").json()
    assert options["model"] == "bulbul:v3" and "shubh" in options["speakers"]

    response = client.post(f"/api/campaigns/{campaign_id}/tts", json={"language": "Hindi"})
    assert response.status_code == 201, response.text
    asset = response.json()
    assert asset["kind"] == "voice" and asset["locale"] == "hi-IN"
    assert asset["is_mock"] is True and asset["provider"] == "mock_tts"
    assert asset["provenance"]["engine"] == "tts" and asset["provenance"]["speaker"] == "shubh"
    assert "20%" in asset["text_content"]  # the spoken text is the grounded copy

    audio = client.get(f"/api/campaigns/assets/{asset['id']}/file")
    assert audio.status_code == 200 and audio.headers["content-type"] == "audio/wav"
    with wave.open(io.BytesIO(audio.content), "rb") as reader:
        assert reader.getnframes() > 0


def test_tts_validates_language_speaker_channel_and_plan(client):
    campaign_id, _ = _locked(client)
    no_plan = client.post(f"/api/campaigns/{campaign_id}/tts", json={"language": "English"})
    assert no_plan.status_code == 409 and no_plan.json()["error"]["code"] == "plan_missing"

    client.post(f"/api/campaigns/{campaign_id}/plan")
    for body, code in (
        ({"language": "Klingon"}, "language_unsupported"),
        ({"language": "English", "speaker": "darth"}, "speaker_unsupported"),
        ({"language": "English", "channel": "poster_headline"}, "channel_unsupported"),
    ):
        response = client.post(f"/api/campaigns/{campaign_id}/tts", json=body)
        assert response.status_code == 422, response.text
        assert response.json()["error"]["code"] == code


def test_tts_budget_is_enforced(client, env_override):
    campaign_id = _planned(client)
    env_override(TITAN_MAX_VOICE_VARIANTS="1")
    assert client.post(f"/api/campaigns/{campaign_id}/tts", json={"language": "English"}).status_code == 201
    second = client.post(f"/api/campaigns/{campaign_id}/tts", json={"language": "Hindi"})
    assert second.status_code == 409 and second.json()["error"]["code"] == "budget_exceeded"


def test_live_tts_request_matches_the_documented_contract(client, env_override, monkeypatch):
    from app.services import sarvam

    campaign_id = _planned(client)
    env_override(**LIVE_ENV)
    wav = _wav_bytes()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"request_id": "tts-1", "audios": [base64.b64encode(wav).decode()]})

    seen = _transport(monkeypatch, sarvam, handler)
    response = client.post(
        f"/api/campaigns/{campaign_id}/tts", json={"language": "English", "speaker": "priya"}
    )
    assert response.status_code == 201, response.text
    asset = response.json()
    assert asset["is_mock"] is False and asset["provider"] == "sarvam_bulbul"

    request = seen[0]
    assert str(request.url) == "https://api.sarvam.ai/text-to-speech"
    assert request.headers["api-subscription-key"] == "sarvam-test-key"
    body = json.loads(request.content)
    assert body == {
        "text": asset["text_content"],
        "language_code": "en-IN",
        "speaker": "priya",
        "model": "bulbul:v3",
        "output_audio_codec": "wav",
    }
    stored = client.get(f"/api/campaigns/assets/{asset['id']}/file").content
    assert stored == wav


def test_live_tts_failures_store_nothing(client, env_override, monkeypatch):
    from app.services import sarvam

    campaign_id = _planned(client)
    env_override(**{**LIVE_ENV, "TITAN_SARVAM_API_KEY": ""})
    missing = client.post(f"/api/campaigns/{campaign_id}/tts", json={"language": "English"})
    assert missing.status_code == 503
    assert missing.json()["error"]["code"] == "provider_not_configured"

    env_override(**LIVE_ENV)
    _transport(
        monkeypatch,
        sarvam,
        lambda request: httpx.Response(403, json={"error": {"message": "bad key", "code": "invalid_api_key_error"}}),
    )
    denied = client.post(f"/api/campaigns/{campaign_id}/tts", json={"language": "English"})
    assert denied.status_code == 503 and denied.json()["error"]["code"] == "provider_auth_error"

    _transport(monkeypatch, sarvam, lambda request: httpx.Response(200, json={"audios": ["bm90LWEtd2F2"]}))
    garbage = client.post(f"/api/campaigns/{campaign_id}/tts", json={"language": "English"})
    assert garbage.status_code == 503 and garbage.json()["error"]["code"] == "tts_bad_audio"

    assets = client.get(f"/api/campaigns/{campaign_id}/assets").json()
    assert [a for a in assets if a["kind"] == "voice"] == []


# ── AI video jobs ─────────────────────────────────────────────────────
@pytest.fixture
def fast_polls(monkeypatch):
    from app.services import video_service

    monkeypatch.setattr(video_service, "AI_VIDEO_POLL_INTERVAL", 0.0)
    return video_service


def test_mock_ai_video_job_runs_through_every_state(client, fast_polls):
    campaign_id = _planned(client)
    submitted = client.post(f"/api/campaigns/{campaign_id}/videos/generate", json={"seconds": 6})
    assert submitted.status_code == 202, submitted.text
    job = submitted.json()
    assert job["kind"] == "ai_video" and job["status"] == "running"
    assert job["provider_status"] == "queued" and job["is_mock"] is True

    # A repeat while active returns the same job, not a second task.
    again = client.post(f"/api/campaigns/{campaign_id}/videos/generate").json()
    assert again["id"] == job["id"]

    first = client.get(f"/api/campaigns/{campaign_id}/videos/jobs").json()[0]
    assert first["status"] == "running" and first["provider_status"] == "in_progress"
    assert first["progress"] == 50 and first["asset_id"] is None

    done = client.get(f"/api/campaigns/{campaign_id}/videos/jobs").json()[0]
    assert done["status"] == "completed" and done["progress"] == 100
    asset = next(
        a for a in client.get(f"/api/campaigns/{campaign_id}/assets").json() if a["id"] == done["asset_id"]
    )
    assert asset["kind"] == "video" and asset["is_mock"] is True
    assert asset["provenance"]["playable"] is False  # mock container, honestly labelled
    assert "cold coffee" in asset["provenance"]["prompt"] and "20%" not in asset["provenance"]["prompt"]
    served = client.get(f"/api/campaigns/assets/{asset['id']}/file")
    assert served.headers["content-type"] == "video/mp4"

    # Terminal jobs are not polled again and stay completed.
    assert client.get(f"/api/campaigns/{campaign_id}/videos/jobs").json()[0]["status"] == "completed"


def test_polling_is_throttled(client):
    campaign_id = _planned(client)
    client.post(f"/api/campaigns/{campaign_id}/videos/generate")
    for _ in range(3):  # well inside the poll interval: no provider call, no progress
        job = client.get(f"/api/campaigns/{campaign_id}/videos/jobs").json()[0]
        assert job["provider_status"] == "queued"


def _fake_video_client(monkeypatch, **overrides):
    from app.services import agnes

    class _Client(agnes.MockVideoClient):
        name = "agnes_video"

        async def retrieve(self, video_id):
            if "retrieve" in overrides:
                return overrides["retrieve"](self, video_id)
            return await super().retrieve(video_id)

        async def download(self, url, *, max_bytes):
            if "download" in overrides:
                return overrides["download"]()
            return await super().download(url, max_bytes=max_bytes)

    monkeypatch.setattr(agnes, "get_video_client", lambda settings=None: _Client())


def test_provider_failure_and_missing_url_fail_the_job(client, fast_polls, monkeypatch):
    from app.services import agnes

    campaign_id = _planned(client)

    def failed(self, video_id):
        return agnes.VideoTask(video_id, "failed", 0, None, "Invalid reference media", "agnes_video", "agnes-video-2.5", False)

    _fake_video_client(monkeypatch, retrieve=failed)
    client.post(f"/api/campaigns/{campaign_id}/videos/generate")
    job = client.get(f"/api/campaigns/{campaign_id}/videos/jobs").json()[0]
    assert job["status"] == "failed" and job["error"] == "Invalid reference media"
    assert job["error_code"] == "video_generation_failed"

    def no_url(self, video_id):
        return agnes.VideoTask(video_id, "completed", 100, None, None, "agnes_video", "agnes-video-2.5", False)

    _fake_video_client(monkeypatch, retrieve=no_url)
    client.post(f"/api/campaigns/{campaign_id}/videos/generate")
    job = client.get(f"/api/campaigns/{campaign_id}/videos/jobs").json()[0]
    assert job["status"] == "failed" and job["error_code"] == "video_url_missing"

    _fake_video_client(monkeypatch, download=lambda: b"<html>expired link</html>")
    client.post(f"/api/campaigns/{campaign_id}/videos/generate")
    client.get(f"/api/campaigns/{campaign_id}/videos/jobs")
    job = client.get(f"/api/campaigns/{campaign_id}/videos/jobs").json()[0]
    assert job["status"] == "failed" and job["error_code"] == "video_invalid"

    assets = client.get(f"/api/campaigns/{campaign_id}/assets").json()
    assert [a for a in assets if a["kind"] == "video"] == []


def test_transient_poll_errors_keep_the_job_running_until_the_deadline(client, fast_polls, monkeypatch):
    campaign_id = _planned(client)

    def flaky(self, video_id):
        raise ProviderUnavailableError("Agnes rate limit reached.", code="provider_rate_limited")

    _fake_video_client(monkeypatch, retrieve=flaky)
    client.post(f"/api/campaigns/{campaign_id}/videos/generate")
    job = client.get(f"/api/campaigns/{campaign_id}/videos/jobs").json()[0]
    assert job["status"] == "running"  # a rate limit is not a failure

    real_now = fast_polls._now
    monkeypatch.setattr(fast_polls, "_now", lambda: real_now() + fast_polls.AI_VIDEO_DEADLINE + 1)
    job = client.get(f"/api/campaigns/{campaign_id}/videos/jobs").json()[0]
    assert job["status"] == "failed" and job["error_code"] == "video_deadline_exceeded"


def test_a_job_can_be_cancelled_and_is_owner_scoped(client, other_account):
    campaign_id = _planned(client)
    job = client.post(f"/api/campaigns/{campaign_id}/videos/generate").json()
    assert client.post(f"/api/campaigns/videos/jobs/{job['id']}/cancel", headers=other_account).status_code == 404
    cancelled = client.post(f"/api/campaigns/videos/jobs/{job['id']}/cancel").json()
    assert cancelled["status"] == "cancelled" and cancelled["active"] is False
    assert client.post(f"/api/campaigns/videos/jobs/{job['id']}/cancel").status_code == 409


def test_ai_video_respects_the_feature_flag_and_budget(client, env_override, fast_polls):
    campaign_id = _planned(client)
    env_override(TITAN_ENABLE_AGNES_VIDEO="false")
    disabled = client.post(f"/api/campaigns/{campaign_id}/videos/generate")
    assert disabled.status_code == 503 and disabled.json()["error"]["code"] == "video_disabled"

    env_override(TITAN_ENABLE_AGNES_VIDEO="true", TITAN_MAX_VIDEO_VARIANTS="1")
    client.post(f"/api/campaigns/{campaign_id}/videos/generate")
    client.get(f"/api/campaigns/{campaign_id}/videos/jobs")
    assert client.get(f"/api/campaigns/{campaign_id}/videos/jobs").json()[0]["status"] == "completed"
    over = client.post(f"/api/campaigns/{campaign_id}/videos/generate")
    assert over.status_code == 409 and over.json()["error"]["code"] == "budget_exceeded"


def test_agnes_video_requests_match_the_documented_contract(client, env_override, monkeypatch):
    from app.config import get_settings
    from app.services import agnes

    env_override(**LIVE_ENV)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(200, json={"id": "task_1", "video_id": "vid_1", "status": "queued", "progress": 0})
        return httpx.Response(
            200, json={"id": "vid_1", "status": "completed", "progress": 100, "url": "https://cdn.test/v.mp4", "error": None}
        )

    seen = _transport(monkeypatch, agnes, handler)
    video = agnes.get_video_client(get_settings())
    created = asyncio.run(video.create("a shop", seconds=5, size="720P", aspect_ratio="9:16"))
    assert created.video_id == "vid_1" and created.status == "queued"
    polled = asyncio.run(video.retrieve("vid_1"))
    assert polled.status == "completed" and polled.url == "https://cdn.test/v.mp4"

    create, poll = seen
    assert str(create.url) == "https://agnes.test/v1/videos"
    assert json.loads(create.content) == {
        "model": "agnes-video-2.5", "prompt": "a shop", "mode": "text",
        "seconds": "5", "size": "720P", "aspect_ratio": "9:16",
    }
    assert poll.url.path == "/agnesapi"  # beside /v1, as documented
    assert dict(poll.url.params) == {"video_id": "vid_1", "model_name": "agnes-video-2.5"}
    assert poll.headers["authorization"] == "Bearer agnes-test-key"


def test_live_video_submit_rejection_is_persisted_as_a_failed_job(client, env_override, monkeypatch):
    from app.services import agnes

    campaign_id = _planned(client)
    env_override(**LIVE_ENV)
    _transport(monkeypatch, agnes, lambda request: httpx.Response(402, json={"error": {"message": "top up"}}))
    job = client.post(f"/api/campaigns/{campaign_id}/videos/generate").json()
    assert job["status"] == "failed" and job["error_code"] == "provider_quota_exceeded"
    listed = client.get(f"/api/campaigns/{campaign_id}/videos/jobs").json()
    assert listed[0]["id"] == job["id"] and listed[0]["status"] == "failed"


# ── provider status ───────────────────────────────────────────────────
def test_modes_reports_live_providers_from_configuration_alone(client, env_override):
    env_override(**LIVE_ENV)
    body = client.get("/api/modes").json()
    providers = {p["name"]: p for p in body["providers"]}
    for name in ("agnes_llm", "agnes_image", "agnes_video", "voice"):
        assert providers[name]["available"] is True, name
    assert body["stt"]["available"] is True and "saaras:v4" in body["stt"]["detail"]
    assert body["extraction"]["available"] is True

    env_override(**{**LIVE_ENV, "TITAN_SARVAM_API_KEY": ""})
    body = client.get("/api/modes").json()
    assert body["stt"]["available"] is False
    assert "TITAN_SARVAM_API_KEY" in body["stt"]["detail"]


def test_flat_discount_copy_does_not_double_the_word_off(client):
    campaign = _create(client, "Flat 50 off haircut every Monday at our salon for students")
    client.post(f"/api/factsheets/{campaign['factsheet']['id']}/lock")
    client.post(f"/api/campaigns/{campaign['id']}/plan")
    master = client.get(f"/api/campaigns/{campaign['id']}/plan").json()["substituted"]["master"]
    assert master["poster_headline"] == "50 off on haircut"
    assert all("off off" not in text for text in master.values())


def test_poster_pixels_actually_contain_the_overlay_text():
    """The compositor must draw the facts onto the art, not just record them."""
    from PIL import Image, ImageChops

    from app.services.poster import _Row, _render_png

    art = _png_bytes(600)
    bare = Image.open(io.BytesIO(_render_png(art, "", [], []))).convert("RGB")
    drawn = []
    with_text = Image.open(
        io.BytesIO(_render_png(art, "Chai Point", [_Row("headline", "20% off cold coffee", 88)], drawn))
    ).convert("RGB")
    assert [fact.text for fact in drawn] == ["Chai Point", "20% off cold coffee"]
    difference = ImageChops.difference(bare, with_text).getbbox()
    assert difference is not None, "poster with text is pixel-identical to the bare art"
    # White glyph pixels exist inside the text band.
    band = with_text.crop((0, int(with_text.height * 0.45), with_text.width, with_text.height))
    assert any(pixel[0] > 240 and pixel[1] > 240 and pixel[2] > 240 for pixel in band.getdata())


def test_localized_copy_must_use_the_native_script(client):
    from app.services.campaign_brain import validate_plan_raw

    tokens = {"PRODUCT": "cold coffee", "DISCOUNT": "20%", "DAYS": "Saturday", "AUDIENCE": "students", "WINDOW": "4 PM–8 PM"}
    plan = _valid_plan()
    assert validate_plan_raw(plan, tokens, ["English", "Hindi"]) == []
    plan["localization"][1]["copy_templates"]["instagram"] = "{{PRODUCT}} ka time! {{DISCOUNT}} ki chhoot."
    issues = validate_plan_raw(plan, tokens, ["English", "Hindi"])
    assert any("Hindi script" in issue for issue in issues)


def test_agnes_exhausted_quota_is_reported_as_quota_not_auth(client, env_override, monkeypatch):
    from app.services import agnes

    campaign_id = _planned(client)
    env_override(**LIVE_ENV)
    _transport(
        monkeypatch,
        agnes,
        lambda request: httpx.Response(403, json={"error": {"message": "Insufficient user quota, remaining: 0"}}),
    )
    job = client.post(f"/api/campaigns/{campaign_id}/videos/generate").json()
    assert job["status"] == "failed" and job["error_code"] == "provider_quota_exceeded"
    assert "quota" in job["error"].lower()
