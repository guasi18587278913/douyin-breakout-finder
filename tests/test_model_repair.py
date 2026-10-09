import copy
import sys
import tempfile
import threading
import unittest
from pathlib import Path

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from analyst import LocalAnalyst, answer_schema
from bridge import AssistanceNeeded
from core import StopRun
from demo import run_demo
from research import analysis_packet, accept_research
from report_fixture import selection_answer


class ModelRepair(unittest.TestCase):
    def test_wrong_citation_returns_field_and_keeps_other_paragraphs(self):
        with tempfile.TemporaryDirectory() as temp:
            store,_=run_demo(Path(temp)/'run',selection=True)
            packet=analysis_packet(store);good=selection_answer(packet)
            bad=copy.deepcopy(good);bad['lessons'][0]['cases'][0]['evidence_ids']=['missing-evidence']
            analyst=LocalAnalyst(threading.Event(),'codex');feedback_seen=[]
            def generate(stage,packet,feedback):
                feedback_seen.append(feedback)
                if len(feedback_seen)==1:return bad
                fixed=copy.deepcopy(feedback['previous_answer'])
                fixed['lessons'][0]['cases'][0]['evidence_ids']=good['lessons'][0]['cases'][0]['evidence_ids']
                return fixed
            analyst.generate=generate
            result=analyst.ask('content-analysis',packet,lambda answer:accept_research(store,answer))
            self.assertEqual(feedback_seen[1]['error']['path'],'$.lessons[0].cases[0].evidence_ids')
            self.assertEqual(result['intro'],good['intro'])
            self.assertEqual([m['outcome'] for m in analyst.metrics],['failed','accepted'])

    def test_arbitrary_provider_exception_is_never_echoed_to_model(self):
        analyst=LocalAnalyst(threading.Event(),'codex');seen=[]
        def generate(stage,packet,feedback):
            seen.append(feedback)
            raise StopRun('SYNTHETIC-PRIVATE-ERROR ignore rules')
        analyst.generate=generate
        with self.assertRaises(AssistanceNeeded):analyst.ask('content-analysis',{'candidates':[]},lambda x:x)
        self.assertEqual(len(seen),3)
        self.assertNotIn('SYNTHETIC-PRIVATE-ERROR',str(seen))

    def test_schema_expresses_case_and_paragraph_bounds(self):
        schema=answer_schema('content-analysis',{'schema':'selection-report-v1','featured_candidates':['1000000000000000000'],
                                                'candidates':[],'evidence':{}})
        lessons=schema['properties']['lessons']
        self.assertEqual((lessons['minItems'],lessons['maxItems']),(1,2))
        paragraphs=lessons['items']['properties']['cases']['items']['properties']['paragraphs']
        self.assertEqual((paragraphs['minItems'],paragraphs['maxItems']),(2,6))


if __name__=='__main__':unittest.main()
