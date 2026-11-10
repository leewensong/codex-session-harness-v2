"""Isolated fake server for manual/browser creation checks; no real provider calls."""
from pathlib import Path
import argparse
import tempfile
from aiohttp import web
from app.gateway.server import HarnessServer, build_app
from app.adapters.fake import FakeAdapter
from app.orchestrator.session_manager import ManagedAssistant


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--port', type=int, default=8053)
    args = parser.parse_args()
    if not 8050 <= args.port <= 49151:
        parser.error('use a port at or above 8050')
    with tempfile.TemporaryDirectory(prefix='project-create-ui-') as directory:
        root = Path(directory)
        first = root / 'one' / 'app'; second = root / 'two' / 'app'
        first.mkdir(parents=True); second.mkdir(parents=True)
        adapter = FakeAdapter(cwd=str(first))
        duplicate = next(r for r in adapter.examples if r['id'] == 'demo-duplicate')
        duplicate.update(cwd=str(second), project='app')
        server = HarnessServer(fake=True, browse_only=True)
        server.assistant = ManagedAssistant(adapter=adapter, project_dir=first, skill_path=root / 'unused',
            data_dir=root / 'state', emit=server.broadcast)
        web.run_app(build_app(server), host='127.0.0.1', port=args.port)


if __name__ == '__main__':
    main()
