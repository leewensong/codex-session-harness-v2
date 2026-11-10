import asyncio
import json

from app.adapters.codex.adapter import CodexSessionAdapter


async def main() -> None:
    adapter = CodexSessionAdapter(cwd=".")
    try:
        print(json.dumps(await adapter.runtime_status(), ensure_ascii=False, indent=2))
    finally:
        await adapter.close()


if __name__ == "__main__":
    asyncio.run(main())
