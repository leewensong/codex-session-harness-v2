import asyncio
import json

from app.adapters.codex.adapter import CodexSessionAdapter


async def main() -> None:
    adapter = CodexSessionAdapter(cwd=".")
    try:
        await adapter.runtime_status()
        raw = await adapter.client.list_sessions(limit=5)
        print(json.dumps(raw, ensure_ascii=False, indent=2)[:20000])
    finally:
        await adapter.close()


if __name__ == "__main__":
    asyncio.run(main())
