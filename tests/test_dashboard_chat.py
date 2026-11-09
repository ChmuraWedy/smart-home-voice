import asyncio
import json

import httpx
from fastapi.testclient import TestClient

import app as voice
from chat_session import Conversation, EmbedSession, Sessions

TICKET = "t" * 43
DELEGATED = "shc_" + "s" * 43
HEADERS = {"X-Voice-Command": "1"}


def test_ticket_auth_and_full_conversation_per_panel(monkeypatch):
    monkeypatch.setenv("VOICE_DASHBOARD_ORIGIN", "http://dashboard.test:8082")
    monkeypatch.setattr(voice, "embedded_conversations", Sessions())
    monkeypatch.setattr(voice, "secret", lambda name: "model-test-key" if name == "OPENAI_API_KEY" else None)
    unused = {TICKET, "u" * 43}
    requests = []
    conversations = []
    active = True

    def handle(request):
        requests.append((request.url.path, request.headers.get("X-API-Key")))
        if request.url.path == "/api/v1/assistant/session":
            ticket = json.loads(request.content)["ticket"]
            if ticket not in unused:
                return httpx.Response(401, json={})
            unused.remove(ticket)
            return httpx.Response(200, json={"key": DELEGATED})
        if request.url.path == "/api/v1/devices":
            assert request.headers["X-API-Key"] == DELEGATED
            return httpx.Response(200 if active else 401, json={})
        raise AssertionError(request.url.path)

    def dashboard_api(key=None):
        return httpx.AsyncClient(
            base_url="http://dashboard.test:8082/api/v1/",
            transport=httpx.MockTransport(handle),
            headers={"X-API-Key": key} if key else {},
        )

    async def catalog(*args, **kwargs):
        assert kwargs["delegated"]
        return {"1": {"path": "devices/1"}}

    async def interpret(text, key, catalog, previous, history, *, choice=None):
        conversations.append((text, list(history)))
        return []

    monkeypatch.setattr(voice, "dashboard_api", dashboard_api)
    monkeypatch.setattr(voice, "load_catalog", catalog)
    monkeypatch.setattr(voice, "interpret", interpret)
    with TestClient(voice.app) as client:
        page = client.get("/embed")
        assert page.status_code == 200 and "Type instead" in page.text
        assert page.headers["Content-Security-Policy"] == "frame-ancestors http://dashboard.test:8082"
        assert client.post("/embed/command", headers=HEADERS, json={"text": "hello"}).status_code == 401

        def sign_in(ticket):
            result = client.post("/embed/session", headers=HEADERS, json={"ticket": ticket})
            assert result.status_code == 200
            assert DELEGATED not in result.text
            return {**HEADERS, "Authorization": "Bearer " + result.json()["session"]}

        headers = sign_in(TICKET)
        assert client.post("/embed/session", headers=HEADERS, json={"ticket": TICKET}).status_code == 401
        for text in ("First", "Second", "Third"):
            result = client.post("/embed/command", headers=headers, json={"text": text})
            assert result.status_code == 200, result.text
        assert [text for text, _ in conversations] == ["First", "Second", "Third"]
        assert conversations[0][1] == []
        assert conversations[2][1] == [
            {"role": "user", "content": "First"},
            {"role": "assistant", "content": result.json()["message"]},
            {"role": "user", "content": "Second"},
            {"role": "assistant", "content": result.json()["message"]},
        ]
        assert client.post("/embed/session/close", headers=headers).status_code == 204
        assert client.post("/embed/command", headers=headers, json={"text": "Old"}).status_code == 401
        headers = sign_in("u" * 43)
        assert client.post("/embed/command", headers=headers, json={"text": "New"}).status_code == 200
        assert conversations[-1] == ("New", [])
        active = False
        assert client.post("/embed/command", headers=headers, json={"text": "Revoked"}).status_code == 401
        assert conversations[-1] == ("New", [])
    assert all(key == DELEGATED for path, key in requests if path.endswith("/devices"))


def test_numbered_light_follow_up_uses_recent_matches(monkeypatch):
    lights = {
        "1": "WorkRoomLamp1",
        "2": "WorkRoomLamp2",
        "3": "WorkRoom Lighting 2",
        "4": "Kitchen Light 2",
    }
    catalog = {
        ident: {"name": name, "aliases": [], "channels": {}, "path": f"devices/{ident}"}
        for ident, name in lights.items()
    }
    first = "set brightness 20% for all ligth in work room"
    follow_up = "check light 2 again"
    command_calls = []
    model_requests = []

    def dashboard_api(key):
        return httpx.AsyncClient(
            base_url="http://dashboard.test/api/v1/",
            transport=httpx.MockTransport(lambda request: httpx.Response(200)),
        )

    async def load_catalog(*args, **kwargs):
        return catalog

    async def interpret(text, key, available, previous, history, *, choice=None):
        model_requests.append(text)
        if text != first:
            return []
        return [{
            "name": "set_device_control",
            "arguments": json.dumps({"device_ids": ["1", "2", "3"], "capability": "brightness"}),
        }]

    async def execute(backend, available, call, *, selected):
        args = json.loads(call["arguments"])
        command_calls.append((call["name"], args["device_ids"]))
        selected.extend((available[ident]["path"], None) for ident in args["device_ids"])
        return [
            voice.Answer(available[ident]["name"], "Test result.", "offline" if ident == "3" else "state")
            for ident in args["device_ids"]
        ]

    monkeypatch.setattr(voice, "dashboard_api", dashboard_api)
    monkeypatch.setattr(voice, "load_catalog", load_catalog)
    monkeypatch.setattr(voice, "interpret", interpret)
    monkeypatch.setattr(voice, "execute", execute)

    async def run():
        conversation = Conversation()
        await voice.answer_command(first, "model-key", conversation, DELEGATED)
        reply = await voice.answer_command(follow_up, "model-key", conversation, DELEGATED)
        assert reply["cards"][0]["status"] == "clarify"
        assert "WorkRoomLamp2, WorkRoom Lighting 2" in reply["message"]
        assert "Kitchen Light 2" not in reply["message"]
        assert command_calls == [("set_device_control", ["1", "2", "3"])]

        choice = reply["cards"][0]["options"][0]["id"]
        reply = await voice.run_command("", "model-key", EmbedSession(DELEGATED, conversation), choice_id=choice)
        assert reply["transcript"] == "Selected: WorkRoomLamp2"
        assert reply["cards"][0]["title"] == "WorkRoomLamp2"
        assert command_calls[-1] == ("get_device_status", ["2"])
        assert reply["message"].startswith("Status check only.")

        mixed_request = "check light 2 again and turn it off"
        await voice.answer_command(mixed_request, "model-key", conversation, DELEGATED)
        assert model_requests == [first, mixed_request]
        assert command_calls == [
            ("set_device_control", ["1", "2", "3"]),
            ("get_device_status", ["2"]),
        ]

    asyncio.run(run())


def test_model_clarification_lists_permitted_options_before_any_command(monkeypatch):
    def device(name, room, home, path, channels=None):
        return {
            "name": name, "room": room, "home": home, "path": path,
            "room_key": (home, room), "channels": channels or {}, "capabilities": {},
        }

    catalog = {
        "1": device("Desk lamp", "Study", "Home", "devices/1"),
        "2": device("Desk lamp", "Bedroom", "Home", "devices/2"),
        "3": device("Wall switch", "Study", "Annex", "devices/3", {"switch_1": "Desk"}),
    }
    assert any(tool["name"] == "ask_clarification" for tool in voice.TOOLS)

    def backend_request(request):
        assert request.method == "GET" and request.url.path == "/api/v1/devices"
        return httpx.Response(200)

    def dashboard_api(key):
        return httpx.AsyncClient(
            base_url="http://dashboard.test/api/v1/",
            transport=httpx.MockTransport(backend_request),
        )

    async def load_catalog(*args, **kwargs):
        return catalog

    async def interpret(text, key, available, previous, history, *, choice=None):
        return [
            {
                "name": "set_device_control",
                "arguments": json.dumps({"device_ids": ["1"], "capability": "power", "value": False, "format": "value"}),
            },
            {
                "name": "ask_clarification",
                "arguments": json.dumps({
                    "scope": "device",
                    "question": "Which lamp did you mean?",
                    "candidates": [
                        {"device_id": ident, "channel": None} for ident in ("1", "2", "unknown", "2")
                    ],
                }),
            },
        ]

    monkeypatch.setattr(voice, "dashboard_api", dashboard_api)
    monkeypatch.setattr(voice, "load_catalog", load_catalog)
    monkeypatch.setattr(voice, "interpret", interpret)

    async def run():
        conversation = Conversation()
        reply = await voice.answer_command("turn off the desk lamp", "model-key", conversation, DELEGATED)
        card, = reply["cards"]
        assert card["title"] == "Home Voice"
        assert card["detail"] == "Which lamp did you mean?"
        assert card["status"] == "clarify"
        assert [option["label"] for option in card["options"]] == [
            "Desk lamp (Study · Home)", "Desk lamp (Bedroom · Home)",
        ]
        assert "Desk lamp (Bedroom · Home)" in reply["message"]
        assert conversation.targets == ()

        rooms = await voice.execute(None, catalog, {
            "name": "room_lights", "arguments": '{"room":"Study","home":null,"on":false}',
        })
        assert [option.label for option in rooms[0].options] == ["Study · Home", "Study · Annex"]

        channels = voice.clarification(catalog, "Which channel?", "channel", [
            {"device_id": "3", "channel": "switch_1"},
            {"device_id": "3", "channel": "switch_9"},
        ])
        assert [option.label for option in channels.options] == ["Wall switch · Desk (Study · Annex)"]

    asyncio.run(run())


def test_click_resumes_original_command_once_for_selected_channel(monkeypatch):
    session_store = Sessions()
    monkeypatch.setattr(voice, "embedded_conversations", session_store)
    monkeypatch.setattr(voice, "secret", lambda name: "model-test-key" if name == "OPENAI_API_KEY" else None)
    key = session_store.open(DELEGATED)
    headers = {**HEADERS, "Authorization": "Bearer " + key}
    other_headers = {**HEADERS, "Authorization": "Bearer " + session_store.open(DELEGATED)}
    original = "turn off the switch"
    writes = []
    catalog = {
        "1": {
            "name": "Wall switch", "path": "devices/42", "cloud": False,
            "channels": {"switch_1": "Desk", "switch_2": "Ceiling"},
            "capabilities": {
                code: {"type": "boolean", "writable": True, "readable": True}
                for code in ("switch_1", "switch_2")
            },
        },
    }

    def backend_request(request):
        assert request.headers["X-API-Key"] == DELEGATED
        if request.url.path == "/api/v1/devices" and request.method == "GET":
            return httpx.Response(200)
        assert request.url.path == "/api/v1/devices/42/commands/capabilities" and request.method == "POST"
        writes.append(json.loads(request.content))
        return httpx.Response(200, json={"acknowledged": True})

    def dashboard_api(key):
        return httpx.AsyncClient(
            base_url="http://dashboard.test/api/v1/",
            transport=httpx.MockTransport(backend_request),
            headers={"X-API-Key": key},
        )

    async def load_catalog(*args, **kwargs):
        return catalog

    async def interpret(text, key, available, previous, history, *, choice=None):
        assert text == original
        if choice:
            assert choice == {"label": "Wall switch · Ceiling", "channel": "switch_2"}
            assert set(available) == {"1"}
            return [{
                "name": "set_device_control",
                "arguments": json.dumps({"device_ids": ["1"], "capability": "power", "value": False, "format": "value"}),
            }]
        return [{
            "name": "ask_clarification",
            "arguments": json.dumps({
                "scope": "channel", "question": "Which channel should I turn off?",
                "candidates": [{"device_id": "1", "channel": code} for code in ("switch_1", "switch_2")],
            }),
        }]

    monkeypatch.setattr(voice, "dashboard_api", dashboard_api)
    monkeypatch.setattr(voice, "load_catalog", load_catalog)
    monkeypatch.setattr(voice, "interpret", interpret)
    path = "/embed/command"
    with TestClient(voice.app) as client:
        ask = client.post(path, headers=headers, json={"text": original})
        assert ask.status_code == 200
        first, second = ask.json()["cards"][0]["options"]
        assert writes == []
        assert client.post(path, headers=other_headers, json={"choice": second["id"]}).status_code == 409
        assert client.post(path, headers=headers, json={"choice": second["id"], "text": "turn everything on"}).status_code == 400
        result = client.post(path, headers=headers, json={"choice": second["id"]})
        assert result.status_code == 200, result.text
        assert result.json()["transcript"] == "Selected: Wall switch · Ceiling"
        assert result.json()["cards"][0]["status"] == "accepted"
        assert writes == [{"values": {"switch_2": False}}]
        assert client.post(path, headers=headers, json={"choice": second["id"]}).status_code == 409
        assert client.post(path, headers=headers, json={"choice": first["id"]}).status_code == 409

        ask = client.post(path, headers=headers, json={"text": original})
        stale = ask.json()["cards"][0]["options"][1]["id"]
        client.post(path, headers=headers, json={"text": original})
        assert client.post(path, headers=headers, json={"choice": stale}).status_code == 409
        assert writes == [{"values": {"switch_2": False}}]
