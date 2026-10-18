"""Deterministic Fact Engine tests: canonical JSON, hashes, HMAC seal, tokens."""

from __future__ import annotations

import copy
import json

import pytest

from app.errors import NormalizationError, SealUnavailableError, TokenError
from app.services.fact_engine import (
    compile_tokens,
    compute_fact_hash,
    compute_seal,
    canonical_json,
    canonical_payload,
    lock_blockers,
    normalize_fact_data,
    recompute_missing,
    substitute_tokens,
    validate_template,
    verify_fact_hash,
    verify_locked_payload,
    verify_seal,
)

SECRET = "unit-test-secret"


def make_sheet(**offer_overrides):
    data = {
        "business": {"name": "Demo Cafe", "location": None},
        "offer": {
            "product": ["Cold coffee"],
            "discount_percent": 20,
            "days": ["Saturday", "Sunday"],
            "start_time": "4 PM",
            "end_time": "8 PM",
            "audience": ["college students"],
            "conditions": ["for students only"],
        },
        "languages": ["English"],
    }
    data["offer"].update(offer_overrides)
    return normalize_fact_data(data)


# ── schema validation & missing ───────────────────────────────────────
def test_normalize_fact_data_rejects_unknown_fields():
    with pytest.raises(NormalizationError):
        normalize_fact_data({"offer": {"product": ["Tea"], "bogus": 1}})


def test_normalize_fact_data_rejects_bad_confidence():
    with pytest.raises(NormalizationError):
        normalize_fact_data(
            {"offer": {"product": ["Tea"]}, "extraction_confidence": {"offer.product": 1.4}}
        )


def test_normalize_fact_data_normalizes_values():
    sheet = normalize_fact_data(
        {
            "business": {"name": "  Demo   Cafe "},
            "offer": {
                "product": [" Cold coffee ", "cold coffee"],
                "discount_percent": "twenty",
                "days": ["sat", "sun"],
                "start_time": "4 PM",
            },
            "languages": ["kn"],
        }
    )
    assert sheet.business.name == "Demo Cafe"
    assert sheet.offer.product == ["Cold coffee"]
    assert sheet.offer.discount_percent == 20
    assert sheet.offer.days == ["Saturday", "Sunday"]
    assert sheet.offer.start_time == "16:00"
    assert sheet.languages == ["Kannada"]


def test_recompute_missing_reports_hard_and_soft_gaps():
    empty = normalize_fact_data({})
    missing = recompute_missing(empty)
    assert "offer.product" in missing
    assert "offer.discount_percent" in missing
    assert "languages" in missing
    assert "business.name" in missing

    complete = make_sheet()
    # product/discount/days/window/audience/languages/business name present
    assert "offer.product" not in recompute_missing(complete)
    assert "offer.discount_percent" not in recompute_missing(complete)
    # location is genuinely absent → still reported
    assert "offer.location" in recompute_missing(complete)


def test_partial_window_flagged():
    sheet = make_sheet(end_time=None)
    assert "offer.end_time" in recompute_missing(sheet)


def test_lock_blockers_require_product_and_a_price_signal():
    empty = normalize_fact_data({})
    blockers = lock_blockers(empty)
    assert "offer.product" in blockers
    assert "offer.discount_percent" in blockers
    assert lock_blockers(make_sheet()) == []
    assert lock_blockers(make_sheet(discount_percent=None, price=499)) == []


# ── canonical serialization ───────────────────────────────────────────
def test_canonical_json_stable_key_order():
    first = {"b": 1, "a": {"y": 2, "x": 1}}
    second = {"a": {"x": 1, "y": 2}, "b": 1}
    assert canonical_json(first) == canonical_json(second)


def test_canonical_payload_is_order_independent():
    a = make_sheet(product=["Tea", "Cake"], audience=["Students", "Families"])
    b = make_sheet(product=["Cake", "Tea"], audience=["Families", "Students"])
    assert canonical_json(canonical_payload(a)) == canonical_json(canonical_payload(b))


def test_canonical_payload_normalizes_days_order():
    a = make_sheet(days=["Sunday", "Saturday"])
    b = make_sheet(days=["Saturday", "Sunday"])
    assert canonical_payload(a)["offer"]["days"] == ["Saturday", "Sunday"]
    assert canonical_json(canonical_payload(a)) == canonical_json(canonical_payload(b))


def test_canonical_payload_excludes_extraction_metadata():
    base = make_sheet()
    decorated = base.model_copy(
        update={
            "extraction_confidence": {"offer.product": 0.1},
            "inferred": ["offer.days"],
            "ambiguities": [{"field": "offer.price", "note": "unclear", "candidates": []}],
        }
    )
    assert canonical_json(canonical_payload(base)) == canonical_json(
        canonical_payload(decorated)
    )


def test_canonical_payload_covers_only_authoritative_fields():
    payload = canonical_payload(make_sheet())
    assert set(payload) == {"business", "offer", "languages"}
    assert "extraction_confidence" not in json.dumps(payload)
    assert "inferred" not in json.dumps(payload)


# ── hashes ────────────────────────────────────────────────────────────
def test_fact_hash_deterministic():
    sheet = make_sheet()
    first = compute_fact_hash(canonical_json(canonical_payload(sheet)))
    second = compute_fact_hash(canonical_json(canonical_payload(sheet)))
    assert first == second
    assert first.startswith("sha256:")
    assert len(first) == len("sha256:") + 64


@pytest.mark.parametrize(
    "override",
    [
        {"product": ["Iced tea"]},
        {"discount_percent": 25},
        {"days": ["Sunday"]},
        {"start_time": "5 PM"},
        {"audience": ["office workers"]},
    ],
)
def test_hash_changes_when_fact_changes(override):
    base = make_sheet()
    changed = make_sheet(**override)
    assert compute_fact_hash(canonical_json(canonical_payload(base))) != compute_fact_hash(
        canonical_json(canonical_payload(changed))
    )


def test_hash_changes_when_languages_change():
    base = make_sheet()
    changed = make_sheet()
    changed = changed.model_copy(update={"languages": ["Hindi"]})
    assert compute_fact_hash(canonical_json(canonical_payload(base))) != compute_fact_hash(
        canonical_json(canonical_payload(changed))
    )


def test_verify_fact_hash_roundtrip_and_tamper():
    canonical = canonical_json(canonical_payload(make_sheet()))
    digest = compute_fact_hash(canonical)
    assert verify_fact_hash(canonical, digest) is True
    assert verify_fact_hash(canonical + " ", digest) is False


# ── HMAC seal ─────────────────────────────────────────────────────────
def test_seal_roundtrip_and_tamper():
    canonical = canonical_json(canonical_payload(make_sheet()))
    seal = compute_seal(canonical, SECRET)
    assert seal.startswith("hmac-sha256:")
    assert verify_seal(canonical, seal, SECRET) is True
    assert verify_seal(canonical, seal, "wrong-secret") is False
    # Tampered payload fails.
    tampered = canonical.replace('"discount_percent":20', '"discount_percent":25')
    assert tampered != canonical
    assert verify_seal(tampered, seal, SECRET) is False


def test_seal_missing_secret_fails_safely():
    canonical = canonical_json(canonical_payload(make_sheet()))
    with pytest.raises(SealUnavailableError):
        compute_seal(canonical, None)
    with pytest.raises(SealUnavailableError):
        compute_seal(canonical, "   ")
    with pytest.raises(SealUnavailableError):
        verify_seal(canonical, "hmac-sha256:deadbeef", None)


def test_verify_locked_payload():
    payload = canonical_payload(make_sheet())
    canonical = canonical_json(payload)
    fact_hash = compute_fact_hash(canonical)
    seal = compute_seal(canonical, SECRET)

    assert verify_locked_payload(payload, fact_hash, seal, SECRET) is True
    assert verify_locked_payload(payload, fact_hash, "hmac-sha256:00", SECRET) is False
    tampered = copy.deepcopy(payload)
    tampered["offer"]["discount_percent"] = 25
    assert verify_locked_payload(tampered, fact_hash, seal, SECRET) is False
    assert verify_locked_payload(payload, fact_hash, seal, None) is None


# ── tokens ────────────────────────────────────────────────────────────
def test_compile_tokens_full_map():
    tokens = compile_tokens(canonical_payload(make_sheet()))
    assert tokens["PRODUCT"] == "Cold coffee"
    assert tokens["DISCOUNT"] == "20%"
    assert tokens["DAYS"] == "Saturday & Sunday"
    assert tokens["WINDOW"] == "4 PM–8 PM"
    assert tokens["AUDIENCE"] == "college students"
    assert tokens["CONDITIONS"] == "for students only"
    assert "PRICE" not in tokens
    assert "LOCATION" not in tokens


def test_compile_only_supported_tokens():
    empty = normalize_fact_data({})
    assert compile_tokens(canonical_payload(empty)) == {}


def test_compile_flat_discount_and_price():
    sheet = make_sheet(discount_percent=None, discount_flat=100, price=499)
    tokens = compile_tokens(canonical_payload(sheet))
    assert tokens["DISCOUNT"] == "100 off"
    assert tokens["PRICE"] == "499"


def test_compile_location_prefers_offer_then_business():
    sheet = make_sheet(location="Indiranagar branch")
    tokens = compile_tokens(canonical_payload(sheet))
    assert tokens["LOCATION"] == "Indiranagar branch"

    fallback = make_sheet()
    fallback = fallback.model_copy(
        update={"business": fallback.business.model_copy(update={"location": "Bengaluru"})}
    )
    assert compile_tokens(canonical_payload(fallback))["LOCATION"] == "Bengaluru"


def test_substitute_tokens_roundtrip():
    tokens = compile_tokens(canonical_payload(make_sheet()))
    template = "Students, {{DISCOUNT}} off {{PRODUCT}} — {{DAYS}}, {{WINDOW}}."
    assert (
        substitute_tokens(template, tokens)
        == "Students, 20% off Cold coffee — Saturday & Sunday, 4 PM–8 PM."
    )


def test_substitute_accepts_lowercase_and_spaces():
    tokens = {"DISCOUNT": "20%"}
    assert substitute_tokens("Get {{ discount }} today", tokens) == "Get 20% today"


def test_substitute_unknown_token_raises():
    with pytest.raises(TokenError):
        substitute_tokens("Hello {{MAGIC}}", {"PRODUCT": "Tea"})


def test_substitute_unresolved_known_token_raises():
    tokens = {"PRODUCT": "Tea"}  # PRICE is a known token but unresolved
    with pytest.raises(TokenError):
        substitute_tokens("Only {{PRICE}} for {{PRODUCT}}", tokens)


def test_validate_template_reports_issues():
    issues = validate_template("{{MAGIC}} and {{PRICE}}", {"PRODUCT": "Tea"})
    assert any("Unknown token" in issue for issue in issues)
    assert any("unresolved" in issue for issue in issues)
    assert validate_template("{{PRODUCT}}", {"PRODUCT": "Tea"}) == []
