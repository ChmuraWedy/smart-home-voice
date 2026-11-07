"""Relay one microphone turn to the OpenAI Realtime transcription API."""

import asyncio
import base64
import json

from fastapi import WebSocket, WebSocketDisconnect
from websockets.asyncio.client import connect

REALTIME_URL = "wss://api.openai.com/v1/realtime?intent=transcription"
MAX_AUDIO_BYTES = 24_000 * 2 * 16


class StreamError(Exception):
    pass


async def upload_audio(browser, upstream):
    total = 0
    while True:
        frame = await browser.receive()
        if frame["type"] == "websocket.disconnect":
            raise WebSocketDisconnect(frame["code"])
        if frame.get("text") == "commit":
            if total < 4800:
                raise StreamError("No speech detected. Tap to try again.")
            await upstream.send('{"type":"input_audio_buffer.commit"}')
            return
        audio = frame.get("bytes")
        if not audio or len(audio) % 2 or len(audio) > 24_000:
            raise StreamError("Invalid microphone audio. Tap to try again.")
        total += len(audio)
        if total > MAX_AUDIO_BYTES:
            raise StreamError("The recording exceeded the time limit.")
        await upstream.send(json.dumps({
            "type": "input_audio_buffer.append",
            "audio": base64.b64encode(audio).decode("ascii"),
        }))


async def read_transcript(browser, upstream):
    async for raw in upstream:
        event = json.loads(raw)
        match event["type"]:
            case "conversation.item.input_audio_transcription.delta":
                await browser.send_json({"type": "delta", "text": event["delta"]})
            case "conversation.item.input_audio_transcription.completed":
                return event["transcript"].strip()
            case "error" | "conversation.item.input_audio_transcription.failed":
                raise StreamError("Live transcription failed. Tap to try again.")
    raise StreamError("The transcription connection closed before it finished.")


async def stream_transcript(browser: WebSocket, key: str):
    async with asyncio.timeout(45), connect(
        REALTIME_URL, additional_headers={"Authorization": f"Bearer {key}"},
        open_timeout=8, close_timeout=1,
    ) as upstream:
        await upstream.send(json.dumps({
            "type": "session.update",
            "session": {
                "type": "transcription",
                "audio": {"input": {
                    "format": {"type": "audio/pcm", "rate": 24000},
                    "transcription": {"model": "gpt-live-transcribe", "delay": "low"},
                    "turn_detection": None,
                }},
            },
        }))
        async with asyncio.timeout(8):
            while True:
                event = json.loads(await upstream.recv())
                if event["type"] in ("session.updated", "transcription_session.updated"):
                    break
                if event["type"] == "error":
                    raise StreamError("OpenAI could not start live transcription.")
        await browser.send_json({"type": "ready"})
        upload = asyncio.create_task(upload_audio(browser, upstream))
        transcript = asyncio.create_task(read_transcript(browser, upstream))
        try:
            _, final = await asyncio.gather(upload, transcript)
            return final
        finally:
            upload.cancel()
            transcript.cancel()
            await asyncio.gather(upload, transcript, return_exceptions=True)
