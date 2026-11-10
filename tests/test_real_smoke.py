import asyncio
import json
import uuid

from aiohttp import ClientSession, WSMsgType


async def receive_json(ws, timeout: float = 10) -> dict:
    message = await ws.receive(timeout=timeout)
    if message.type != WSMsgType.TEXT:
        raise RuntimeError(f"unexpected websocket message: {message.type}")
    return json.loads(message.data)


async def main() -> None:
    async with ClientSession() as client:
        async with client.ws_connect("http://127.0.0.1:8050/ws") as ws:
            snapshot = await receive_json(ws)
            assert snapshot["type"] == "harness.snapshot" and snapshot["payload"]["status"] == "ready", snapshot
            ready = await receive_json(ws)
            assert ready["type"] == "assistant.session.ready", ready

            await ws.send_json({"type": "sessions.list"})
            sessions = await receive_json(ws)
            assert sessions["type"] == "sessions.snapshot", sessions
            managed = [item for item in sessions["payload"]["sessions"] if item.get("managed")]
            assert len(managed) == 1, managed

            client_id = str(uuid.uuid4())
            await ws.send_json({
                "type": "chat.send",
                "clientMessageId": client_id,
                "text": "这是 Harness v2 的端到端联调消息。请只回复：真实 Codex 文本链路正常。",
            })
            events = []
            for _ in range(100):
                event = await receive_json(ws, timeout=30)
                if event.get("payload", {}).get("clientMessageId") == client_id:
                    events.append(event)
                if event.get("type") in {"chat.completed", "chat.error"} and event.get("payload", {}).get("clientMessageId") == client_id:
                    break
            completed = next((event for event in events if event["type"] == "chat.completed"), None)
            assert completed, events
            reply = completed["payload"].get("reply") or ""
            assert "真实 Codex 文本链路正常" in reply, reply
            print(json.dumps({"ok": True, "managed": managed[0]["idShort"], "reply": reply}, ensure_ascii=False))


if __name__ == "__main__":
    asyncio.run(main())
