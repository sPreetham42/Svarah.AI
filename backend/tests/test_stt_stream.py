"""Live transcription preview (WebSocket).

The contract these tests protect:

* the socket refuses an unauthenticated or tampered credential;
* mock mode reports itself as streaming *and* as mock — it never pretends to be
  a live provider;
* partials arrive while the speaker is still talking, and the final text is the
  same deterministic transcript the offline provider would return, so a preview
  can never disagree with what gets saved.
"""

from __future__ import annotations


def _demo_token(client) -> str:
    response = client.post("/api/auth/demo")
    assert response.status_code == 200, response.text
    return response.json()["token"]


def test_preview_streams_partials_then_final(client):
    token = _demo_token(client)

    with client.websocket_connect("/api/stt/stream") as ws:
        ws.send_json({"type": "auth", "token": token})

        ready = ws.receive_json()
        assert ready["type"] == "ready"
        assert ready["streaming"] is True
        assert ready["is_mock"] is True
        assert ready["provider"] == "mock"

        # Words arrive while the speaker is still recording.
        partial = ws.receive_json()
        assert partial["type"] == "partial"
        assert partial["is_mock"] is True
        assert partial["text"].split()[0] == "Mock"

        ws.send_json({"type": "stop"})

        final = None
        for _ in range(400):
            message = ws.receive_json()
            if message["type"] == "final":
                final = message
                break

    assert final is not None, "the stream never produced a final transcript"
    # The preview and the authoritative transcript come from one source.
    from app.services.stt import MockSTTProvider

    assert final["text"] == MockSTTProvider.DEFAULT_TEXT
    assert final["is_mock"] is True
    assert final["provider"] == "mock"


def test_preview_rejects_a_tampered_token(client):
    with client.websocket_connect("/api/stt/stream") as ws:
        ws.send_json({"type": "auth", "token": "t1.aaaa.bbbb"})
        message = ws.receive_json()
    assert message["type"] == "error"
    assert message["code"] in {"token_invalid", "token_expired"}


def test_preview_requires_an_auth_message(client):
    with client.websocket_connect("/api/stt/stream") as ws:
        ws.send_json({"type": "start"})
        message = ws.receive_json()
    assert message["type"] == "error"
    assert message["code"] == "auth_required"
