import unittest
from unittest.mock import patch
from check_versions import inspect


class VersionEvidence(unittest.TestCase):
    def test_remote_mismatch_is_not_hidden_by_matching_local_and_server(self):
        current='a'*40;old='b'*40
        answers=[current,'main','',old+'\trefs/heads/main',
                 '/opt/douyin-breakout/releases/'+current+'\n/opt/douyin-breakout/releases/'+current]
        with patch('check_versions.command',side_effect=answers):result=inspect(server='synthetic-server')
        self.assertFalse(result['local_matches_github']);self.assertFalse(result['synchronized'])
        self.assertEqual(result['running_commit'],current)

    def test_current_symlink_does_not_prove_running_process_was_updated(self):
        current='a'*40;old='b'*40
        answers=[current,'main','',current+'\trefs/heads/main',
                 '/opt/douyin-breakout/releases/'+current+'\n/opt/douyin-breakout/releases/'+old]
        with patch('check_versions.command',side_effect=answers):result=inspect(server='synthetic-server')
        self.assertTrue(result['local_matches_github']);self.assertFalse(result['synchronized'])

    def test_without_server_evidence_never_claims_three_way_sync(self):
        current='a'*40
        with patch('check_versions.command',side_effect=[current,'main','',current+'\trefs/heads/main']):
            result=inspect()
        self.assertFalse(result['server_checked']);self.assertFalse(result['synchronized'])


if __name__=='__main__':unittest.main()
