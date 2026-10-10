import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from demo import run_demo
from webapp.job_outcome import assessment,_assessment


class OutcomeCache(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);_assessment.cache_clear()

    def test_registered_evidence_change_withdraws_cached_readiness(self):
        store,_=run_demo(self.root/'run',selection=True)
        self.assertTrue(assessment(store.root)['report']['ready'])
        index=json.loads((store.root/'evidence-index.json').read_text())
        entry=next(iter(index.values()))[0]
        path=store.root/entry['file'];path.write_text(path.read_text()+'合成证据发生变化')
        self.assertFalse(assessment(store.root)['report']['ready'])

    def test_a_full_history_window_does_not_evict_itself(self):
        folders=[]
        for i in range(100):
            folder=self.root/str(i);folder.mkdir();(folder/'state.json').write_text('{}');folders.append(folder)
        with patch('webapp.job_outcome.audit_run',return_value={'completion':{'synthetic':True}}) as audit:
            for _ in range(2):
                for folder in folders:assessment(folder)
            self.assertEqual(audit.call_count,100)


if __name__=='__main__':unittest.main()
