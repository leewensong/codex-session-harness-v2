import asyncio
import tempfile
import unittest
from pathlib import Path

from agent_adaptors.claude import ClaudeCodeAdaptor
from agent_adaptors.contracts import AdaptorError


class LeaseTests(unittest.TestCase):
    def test_second_adaptor_cannot_share_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            cwd = Path(tmp)
            first = ClaudeCodeAdaptor(cwd=str(cwd), state_dir=str(cwd / "state"), binary="missing-claude")
            try:
                with self.assertRaises(AdaptorError) as caught:
                    ClaudeCodeAdaptor(cwd=str(cwd), state_dir=str(cwd / "state"), binary="missing-claude")
                self.assertEqual(caught.exception.code, "state_in_use")
            finally:
                asyncio.run(first.close())


if __name__ == "__main__":
    unittest.main()
