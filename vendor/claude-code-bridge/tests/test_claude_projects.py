import json
import tempfile
import unittest
from pathlib import Path

from agent_adaptors.history import History, project_dir_name


class ClaudeProjectDiscoveryTests(unittest.TestCase):
    def test_lists_and_reads_sessions_across_all_project_folders(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            projects = root / "projects"
            cwd_a = root / "SmallDesk"
            cwd_b = root / "UE5.8test"
            cwd_a.mkdir()
            cwd_b.mkdir()
            sid_a = "123e4567-e89b-12d3-a456-426614174000"
            sid_b = "123e4567-e89b-12d3-a456-426614174001"
            for cwd, sid, title in ((cwd_a, sid_a, "SmallDesk win build"), (cwd_b, sid_b, "VR180 render")):
                directory = projects / project_dir_name(str(cwd))
                directory.mkdir(parents=True)
                rows = [
                    {"type": "user", "sessionId": sid, "cwd": str(cwd), "message": {"content": "hello"}},
                    {"type": "custom-title", "sessionId": sid, "customTitle": title},
                ]
                (directory / f"{sid}.jsonl").write_text("\n".join(json.dumps(row) for row in rows), encoding="utf-8")

            history = History(cwd_a, projects)
            sessions = history.list()
            self.assertEqual({row["project"] for row in sessions}, {"SmallDesk", "UE5.8test"})
            self.assertEqual(history.read(sid_b)["name"], "VR180 render")
            discovered = {row["name"]: row["sessionCount"] for row in history.list_projects()}
            self.assertEqual(discovered, {"SmallDesk": 1, "UE5.8test": 1})


if __name__ == "__main__":
    unittest.main()
