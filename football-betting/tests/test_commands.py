"""Issue-based owner commands: authorization and strict validation."""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fbs.commands import CommandError, authorize, parse


def body(obj: str) -> str:
    return "Sent from the app.\n\n```json\n" + obj + "\n```\n"


class AuthTests(unittest.TestCase):
    def test_owner_in_private_repo_only(self):
        authorize("true", "youbro934", "youbro934", "OWNER")
        for args in (("false", "youbro934", "youbro934", "OWNER"),        # public repo
                     ("true", "someone", "youbro934", "NONE"),            # stranger
                     ("true", "someone", "youbro934", "COLLABORATOR"),
                     ("true", "youbro934", "youbro934", "CONTRIBUTOR"),   # association must be OWNER
                     ("", "", "youbro934", "")):
            with self.assertRaises(CommandError, msg=args):
                authorize(*args)


class ParseTests(unittest.TestCase):
    def test_valid_commands(self):
        self.assertEqual(parse(body('{"command":"stop"}')), ("stop", {}))
        cmd, p = parse(body('{"command":"confirm","id":"OPP-20261107-1","stake":"10","odds":1.36,"reference":"AB-12"}'))
        self.assertEqual((cmd, p["stake"], p["odds"], p["reference"], p["bookmaker"]), ("confirm", 10.0, 1.36, "AB-12", "Bet365"))
        self.assertEqual(parse(body('{"command":"continue","confirm":"CONTINUE"}'))[1], {"confirm": "CONTINUE"})

    def test_rejects_bad_input(self):
        bad = ['{"command":"rm -rf /"}', '{"command":"confirm","id":"OPP-1; echo x","stake":1,"odds":1.3}',
               '{"command":"confirm","id":"OPP-20261107-1","stake":-1,"odds":1.3}',
               '{"command":"confirm","id":"OPP-20261107-1","stake":1,"odds":1.0}',
               '{"command":"confirm","id":"OPP-20261107-1","stake":1,"odds":1.3,"reference":"$(curl x)"}',
               '{"command":"continue","confirm":"continue"}', '{"command":"settle-bet","bet_id":1,"result":"maybe"}',
               '[1,2]', 'not json']
        for b in bad:
            with self.assertRaises(CommandError, msg=b):
                parse(body(b))
        with self.assertRaises(CommandError):
            parse("no block here")


if __name__ == "__main__":
    unittest.main()


class IssueChannelTests(unittest.TestCase):
    def _run(self, env, d):
        import tempfile
        from unittest import mock
        from fbs.__main__ import _dispatch
        from fbs.config import Settings
        from fbs.engine import Engine
        res = os.path.join(d, "result.txt")
        with mock.patch.dict(os.environ, env | {"FBS_RESULT_FILE": res}):
            eng = Engine(Settings(), d)
            _dispatch(eng, "issue-command", [])
            enabled = eng.ai_enabled
            eng.close()
        return enabled, open(res).read()

    def test_only_owner_in_private_repo_changes_anything(self):
        import tempfile
        d = tempfile.mkdtemp()
        stop = body('{"command":"stop"}')
        base = {"REPO_OWNER": "youbro934", "ISSUE_BODY": stop}
        enabled, msg = self._run(base | {"REPO_PRIVATE": "true", "ISSUE_AUTHOR": "intruder",
                                         "ISSUE_ASSOCIATION": "NONE"}, d)
        self.assertTrue(enabled)
        self.assertIn("Not executed", msg)
        enabled, msg = self._run(base | {"REPO_PRIVATE": "false", "ISSUE_AUTHOR": "youbro934",
                                         "ISSUE_ASSOCIATION": "OWNER"}, d)
        self.assertTrue(enabled)
        self.assertIn("public", msg)
        enabled, msg = self._run(base | {"REPO_PRIVATE": "true", "ISSUE_AUTHOR": "youbro934",
                                         "ISSUE_ASSOCIATION": "OWNER"}, d)
        self.assertFalse(enabled)
        self.assertIn("stop", msg)
