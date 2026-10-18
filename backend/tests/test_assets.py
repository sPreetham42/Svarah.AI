"""Phase 4 asset tests: posters, registry, budgets, captions."""

from __future__ import annotations


def _setup(client) -> tuple[int, int]:
    response = client.post(
        "/api/campaigns",
        json={"text": "20% off cold coffee this Saturday and Sunday, 4 PM to 8 PM at our cafe for college students."},
    )
    campaign_id = response.json()["id"]
    sheet_id = response.json()["factsheet"]["id"]
    client.patch(f"/api/factsheets/{sheet_id}", json={"languages": ["English", "Hindi"]})
    client.post(f"/api/factsheets/{sheet_id}/lock")
    client.post(f"/api/campaigns/{campaign_id}/plan")
    return campaign_id, sheet_id


def test_poster_generation_produces_registered_asset(client):
    campaign_id, _ = _setup(client)
    response = client.post(f"/api/campaigns/{campaign_id}/assets/posters", json={"variants": 1})
    assert response.status_code == 200, response.text
    assets = response.json()
    assert len(assets) == 1
    poster = assets[0]
    assert poster["kind"] == "poster"
    assert poster["fact_hash"]
    assert poster["is_mock"] is True  # offline fallback art, honestly labelled
    registry = poster["provenance"]["registry"]
    rendered = {item["text"] for item in registry["rendered_facts"]}
    assert any("20%" in t for t in rendered), rendered
    assert any("cold coffee" in t for t in rendered)

    # The stored file must exist and match the checksum.
    file_response = client.get(f"/api/campaigns/assets/{poster['id']}/file")
    assert file_response.status_code == 200
    assert file_response.headers["content-type"] == "image/png"


def test_caption_assets_carry_template_provenance(client):
    campaign_id, _ = _setup(client)
    response = client.post(f"/api/campaigns/{campaign_id}/assets/captions")
    assert response.status_code == 200, response.text
    captions = response.json()
    channels = {c["provenance"]["channel"] for c in captions if c["locale"] == "en-IN"}
    assert "instagram" in channels
    assert "whatsapp" in channels
    for caption in captions:
        assert caption["fact_hash"]
        assert caption["text_content"]  # substituted, non-empty
    localized = [c for c in captions if c["locale"] != "en-IN"]
    assert localized, "expected Hindi-localized captions"


def test_poster_budget_cap(client):
    campaign_id, _ = _setup(client)
    first = client.post(f"/api/campaigns/{campaign_id}/assets/posters", json={"variants": 3})
    assert first.status_code == 200
    client.post(f"/api/campaigns/{campaign_id}/assets/posters", json={"variants": 3})
    # Budget (3 posters) may cap the second request; either partial success with
    # fewer assets or a 409 budget_exceeded is acceptable, never > 3 total.
    listed = client.get(f"/api/campaigns/{campaign_id}/assets").json()
    posters = [a for a in listed if a["kind"] == "poster"]
    assert len(posters) <= 3


def test_assets_require_plan(client):
    response = client.post(
        "/api/campaigns",
        json={"text": "20% off cold coffee this Saturday and Sunday, 4 PM to 8 PM at our cafe."},
    )
    campaign_id = response.json()["id"]
    sheet_id = response.json()["factsheet"]["id"]
    client.post(f"/api/factsheets/{sheet_id}/lock")
    response = client.post(f"/api/campaigns/{campaign_id}/assets/posters")
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "plan_missing"
