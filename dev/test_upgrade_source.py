import json
import os
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'manager'))
import upgrade_build as source


class UpgradeSourceTests(unittest.TestCase):
    def test_default_commit_lookup_uses_fork(self):
        with patch.dict(os.environ, {}, clear=True), patch.object(source, 'download', return_value=json.dumps({'sha': 'a' * 40}).encode()) as download:
            self.assertEqual(source.latest_commit(), 'a' * 40)
            self.assertEqual(download.call_args.args[0], 'https://api.github.com/repos/zy95-12/FlyBot/commits/main')

    def test_operator_can_select_repository_and_ref(self):
        with patch.dict(os.environ, {'FLYBOT_SOURCE_REPOSITORY': 'team/repo', 'FLYBOT_SOURCE_REF': 'demo/baseline'}), patch.object(source, 'download', return_value=json.dumps({'sha': 'b' * 40}).encode()) as download:
            source.latest_commit()
            self.assertEqual(download.call_args.args[0], 'https://api.github.com/repos/team/repo/commits/demo%2Fbaseline')

    def test_invalid_source_is_rejected_before_network(self):
        for repository in ('https://example.com/repo', '../repo', 'owner/repo?query', 'owner/repo/extra'):
            with self.subTest(repository=repository), patch.dict(os.environ, {'FLYBOT_SOURCE_REPOSITORY': repository}), patch.object(source, 'download', return_value=json.dumps({'sha': 'a' * 40}).encode()) as download:
                with self.assertRaises(source.UpgradeError):
                    source.latest_commit()
                download.assert_not_called()

    def test_invalid_commit_is_not_accepted(self):
        with patch.object(source, 'download', return_value=b'{"sha":"main"}'):
            with self.assertRaises(source.UpgradeError):
                source.latest_commit()


if __name__ == '__main__':
    unittest.main()
