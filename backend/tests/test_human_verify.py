"""Human verification of an inconclusive Guardian result (Source of Truth §34).

The rules these tests protect:

* Guardian must have run — an unverified asset cannot be blessed by hand;
* a *positive* failure is never overridable, only an inconclusive verdict;
* the decision is explicit (an attestation flag) and owner-scoped;
* the Guardian rows survive, so the certificate still reports the machine
  verdict rather than the human decision replacing it;
* and an accepted asset becomes publishable.
"""

from __future__ import annotations


def _campaign_with_poster(client) -> tuple[int, int]:
    created = client.post(
        "/api/campaigns",
        json={
            "text": (
                "Sunrise Cafe is offering 20% off cold coffee this Saturday and Sunday "
                "from 4 PM to 8 PM for college students."
            )
        },
    )
    assert created.status_code == 201, created.text
    campaign = created.json()
    campaign_id = campaign["id"]
    sheet_id = campaign["factsheet"]["id"]

    assert client.post(f"/api/factsheets/{sheet_id}/lock").status_code == 200
    assert client.post(f"/api/campaigns/{campaign_id}/plan").status_code == 200
    posters = client.post(
        f"/api/campaigns/{campaign_id}/assets/posters", json={"variants": 1}
    )
    assert posters.status_code == 200, posters.text
    captions = client.post(f"/api/campaigns/{campaign_id}/assets/captions")
    assert captions.status_code == 200, captions.text
    return campaign_id, posters.json()[0]["id"]


def _verify(client, campaign_id: int) -> None:
    response = client.post(f"/api/campaigns/{campaign_id}/verify")
    assert response.status_code == 200, response.text


def _aggregate_verdict(asset_id: int) -> str:
    """Read the persisted aggregate verdict straight from the database."""
    from sqlalchemy import select

    from app.db import get_session_factory
    from app.models import VerificationResultRecord

    with get_session_factory()() as session:
        row = session.scalar(
            select(VerificationResultRecord).where(
                VerificationResultRecord.asset_id == asset_id,
                VerificationResultRecord.check_name == "aggregate",
            )
        )
        assert row is not None, "Guardian aggregate row missing"
        return row.verdict


def _force_aggregate_verdict(asset_id: int, verdict: str) -> None:
    """Simulate a hard failure so the refusal path can be tested directly."""
    from sqlalchemy import select

    from app.db import get_session_factory
    from app.models import VerificationResultRecord

    with get_session_factory()() as session:
        row = session.scalar(
            select(VerificationResultRecord)
            .where(
                VerificationResultRecord.asset_id == asset_id,
                VerificationResultRecord.check_name == "aggregate",
            )
            .order_by(VerificationResultRecord.id.desc())
        )
        assert row is not None
        row.verdict = verdict
        session.commit()


def test_human_verify_requires_guardian_to_have_run(client):
    _, asset_id = _campaign_with_poster(client)

    response = client.post(
        f"/api/assets/{asset_id}/human-verify", json={"attestation": True}
    )
    assert response.status_code == 409, response.text
    assert response.json()["error"]["code"] == "verification_required"


def test_human_verify_requires_explicit_attestation(client):
    campaign_id, asset_id = _campaign_with_poster(client)
    _verify(client, campaign_id)

    response = client.post(f"/api/assets/{asset_id}/human-verify", json={})
    assert response.status_code == 422, response.text
    assert response.json()["error"]["code"] == "attestation_required"

    still = client.get(f"/api/campaigns/{campaign_id}/assets").json()
    assert next(a for a in still if a["id"] == asset_id)["asset_status"] != "human_verified"


def test_human_verify_accepts_an_inconclusive_verdict(client):
    campaign_id, asset_id = _campaign_with_poster(client)
    _verify(client, campaign_id)

    # Mock mode cannot conclude the OCR/ASR checks, so the verdict is
    # NEEDS_REVIEW — the exact case a human decision exists for.
    assert _aggregate_verdict(asset_id) == "NEEDS_REVIEW"

    response = client.post(
        f"/api/assets/{asset_id}/human-verify",
        json={"attestation": True, "note": "Reviewed against the offer — correct."},
    )
    assert response.status_code == 200, response.text
    assert response.json()["asset_status"] == "human_verified"

    # The machine's findings are preserved, not replaced.
    stored = client.get(f"/api/campaigns/{campaign_id}/assets").json()
    asset = next(a for a in stored if a["id"] == asset_id)
    assert asset["asset_status"] == "human_verified"
    assert len(asset.get("verification", [])) > 0


def test_human_verify_refuses_a_hard_failure(client):
    campaign_id, asset_id = _campaign_with_poster(client)
    _verify(client, campaign_id)
    _force_aggregate_verdict(asset_id, "FAIL")

    response = client.post(
        f"/api/assets/{asset_id}/human-verify",
        json={"attestation": True, "note": "trying to override"},
    )
    assert response.status_code == 409, response.text
    assert response.json()["error"]["code"] == "human_verify_refused"

    stored = client.get(f"/api/campaigns/{campaign_id}/assets").json()
    assert next(a for a in stored if a["id"] == asset_id)["asset_status"] != "human_verified"


def test_latest_guardian_verdict_wins_over_an_earlier_failure(client):
    """A repaired asset must not stay blocked by a superseded FAIL row.

    Guardian appends one aggregate row per run, so a real FAIL before a repair
    and a later NEEDS_REVIEW must resolve to the later one.
    """
    campaign_id, asset_id = _campaign_with_poster(client)
    _verify(client, campaign_id)
    _force_aggregate_verdict(asset_id, "FAIL")

    refused = client.post(
        f"/api/assets/{asset_id}/human-verify", json={"attestation": True}
    )
    assert refused.status_code == 409, refused.text

    # A later, successful verification run appends a fresh aggregate row.
    _verify(client, campaign_id)

    accepted = client.post(
        f"/api/assets/{asset_id}/human-verify", json={"attestation": True}
    )
    assert accepted.status_code == 200, accepted.text
    assert accepted.json()["asset_status"] == "human_verified"


def test_human_verify_is_owner_scoped(client, other_account):
    campaign_id, asset_id = _campaign_with_poster(client)
    _verify(client, campaign_id)

    response = client.post(
        f"/api/assets/{asset_id}/human-verify",
        json={"attestation": True},
        headers=other_account,
    )
    assert response.status_code == 404, response.text


def test_a_human_verified_asset_can_be_prepared_for_publish(client):
    campaign_id, asset_id = _campaign_with_poster(client)
    _verify(client, campaign_id)

    blocked = client.post(
        f"/api/campaigns/{campaign_id}/publish/prepare",
        json={"asset_id": asset_id, "platform": "sandbox", "media_kind": "image"},
    )
    assert blocked.status_code == 409, blocked.text

    accepted = client.post(
        f"/api/assets/{asset_id}/human-verify", json={"attestation": True}
    )
    assert accepted.status_code == 200, accepted.text

    # The publication pairs the image with a caption, so every asset it binds
    # has to be acceptable first.
    for asset in client.get(f"/api/campaigns/{campaign_id}/assets").json():
        recorded = client.post(
            f"/api/assets/{asset['id']}/human-verify", json={"attestation": True}
        )
        assert recorded.status_code == 200, recorded.text

    prepared = client.post(
        f"/api/campaigns/{campaign_id}/publish/prepare",
        json={"asset_id": asset_id, "platform": "sandbox", "media_kind": "image"},
    )
    assert prepared.status_code == 201, prepared.text
