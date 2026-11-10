"""Read-only runtime probe; does not make model calls."""
import argparse,asyncio,json
from .config import load_registry

def main():
    parser=argparse.ArgumentParser(); parser.add_argument('--config',required=True); args=parser.parse_args()
    async def run():
        registry=load_registry(args.config)
        try: print(json.dumps(await registry.probe(),ensure_ascii=False,indent=2))
        finally: await registry.close()
    asyncio.run(run())
if __name__=='__main__': main()
