import asyncio
import json
import uuid

from aiohttp import ClientSession


async def main() -> None:
    async with ClientSession() as client:
        async with client.ws_connect("http://127.0.0.1:8050/ws") as ws:
            events = []
            for _ in range(2):
                msg = await ws.receive(timeout=5)
                if msg.type.name != "TEXT":
                    continue
                event = json.loads(msg.data)
                events.append(event)
            assert any(event.get("type") == "harness.snapshot" for event in events)
            assert any(event.get("type") == "assistant.session.ready" for event in events)
            await ws.send_json({"type": "sessions.list"})
            sessions = json.loads((await ws.receive(timeout=5)).data)
            assert sessions["type"] == "sessions.snapshot"
            managed = next(item for item in sessions["payload"]["sessions"] if item["managed"])
            client_id = str(uuid.uuid4())
            await ws.send_json({"type": "chat.send", "clientMessageId": client_id, "text": "你好，返回一句确认"})
            seen = []
            for _ in range(10):
                event = json.loads((await ws.receive(timeout=5)).data)
                seen.append(event)
                if event.get("type") in {"chat.completed", "chat.error"}:
                    break
            assert any(event.get("type") == "chat.completed" for event in seen), seen
            await ws.send_json({"type": "chat.send", "clientMessageId": client_id, "text": "你好，返回一句确认"})
            duplicate = []
            try:
                for _ in range(4):
                    event = json.loads((await ws.receive(timeout=0.5)).data)
                    duplicate.append(event)
                    if event.get("type") in {"chat.completed", "chat.error"}:
                        break
            except asyncio.TimeoutError:
                pass
            assert any(event.get("type") == "chat.completed" and event.get("payload", {}).get("duplicate") for event in duplicate), duplicate
            print("fake smoke ok", managed["id"])


if __name__ == "__main__":
    asyncio.run(main())
