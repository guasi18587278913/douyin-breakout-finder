import copy
import sys
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from core import StopRun, default_config, Store
from research import accept_review, review_packet, pending_reviews, add_evidence
from selection_report import eligible_ids


class IncrementalReview(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store.create(Path(self.temp.name) / 'run', default_config('AI短剧', ['AI短剧'], '0.50'))

    def tearDown(self):
        self.temp.cleanup()

    def add(self, start, end):
        for index in range(start, end):
            vid = str(1000000000000000000 + index)
            self.store.state['candidates'][vid] = {'id':vid, 'title':'AI短剧演示', 'author_id':'synthetic_author',
                'author_name':'合成作者', 'followers':300, 'likes':12, 'queries':['AI短剧'],
                'review':{'decision':'pending', 'reason':''}}

    def accept(self, packet, unsure=None):
        answer = {'reviews':{v['id']:{'decision':'unsure' if v['id']==unsure else 'include',
                                   'reason':'合成的相关性标签', 'priority':1} for v in packet['candidates']},
                  'next_keywords':[]}
        return accept_review(self.store, answer, expected=packet['review_fingerprints'])

    def test_twenty_ten_ten_uses_forty_reviews_instead_of_ninety(self):
        sizes = []
        for round_number, (start, end) in enumerate(((0,20),(20,30),(30,40))):
            self.add(start,end); packet=review_packet(self.store,round_number)
            sizes.append(len(packet['candidates'])); self.accept(packet)
        self.assertEqual(sizes,[20,10,10])
        self.assertEqual(pending_reviews(self.store),{})
        self.assertEqual(len(self.store.state['candidates']),40)

    def test_uncertain_and_changed_content_are_rechecked(self):
        self.add(0,3); vids=list(self.store.state['candidates'])
        self.accept(review_packet(self.store,0),unsure=vids[0])
        self.store.state['candidates'][vids[1]]['title']='标题已经改变'
        self.assertEqual(set(pending_reviews(self.store)),set(vids[:2]))

    def test_conditions_and_registered_evidence_invalidate_reuse(self):
        self.add(0,2); self.accept(review_packet(self.store,0)); vids=list(self.store.state['candidates'])
        self.store.config['scope']='新的受众和排除范围'
        self.assertEqual(len(pending_reviews(self.store)),2)
        self.accept(review_packet(self.store,1))
        source=Path(self.temp.name)/'note.txt';source.write_text('合成证据：新的内容说明此前标题的理解需要重新核对。这段文字仅用于测试审核依据变化。',encoding='utf-8')
        add_evidence(self.store,vids[0],source,'video-notes','合成测试')
        self.assertEqual(set(pending_reviews(self.store)),{vids[0]})

    def test_relevance_policy_change_invalidates_all_prior_decisions(self):
        self.add(0, 2)
        with patch('research.RELEVANCE_POLICY', 'Previous review instructions'):
            self.accept(review_packet(self.store, 0))
            self.assertEqual(pending_reviews(self.store), {})
        packet = review_packet(self.store, 1)
        self.assertEqual(len(packet['candidates']), 2)
        self.accept(packet)
        self.assertEqual(pending_reviews(self.store), {})

    def test_recheck_packet_omits_stale_judgments_and_rule_hints(self):
        self.add(0, 2)
        vids = list(self.store.state['candidates'])
        self.accept(review_packet(self.store, 0), unsure=vids[0])
        self.store.state['candidates'][vids[1]]['title'] = '新的标题使旧判断失效'
        packet = review_packet(self.store, 1)
        self.assertEqual(len(packet['candidates']), 2)
        self.assertTrue(all('review' not in row for row in packet['candidates']))
        self.assertTrue(all(row['title'] for row in packet['candidates']))
        self.assertEqual(len(packet['review_fingerprints']), 2)

    def test_omitted_or_extra_answers_do_not_change_existing_reviews(self):
        self.add(0,2);packet=review_packet(self.store,0)
        before=copy.deepcopy(self.store.state)
        with self.assertRaises(StopRun):accept_review(self.store,{'reviews':{},'next_keywords':[]},packet['review_fingerprints'])
        self.assertEqual(self.store.state,before)
        self.accept(packet)
        with self.assertRaises(StopRun):self.accept(packet)

    def test_input_change_during_model_generation_rejects_stale_answer(self):
        self.add(0,1);packet=review_packet(self.store,0)
        next(iter(self.store.state['candidates'].values()))['title']='生成期间改变'
        with self.assertRaises(StopRun):self.accept(packet)

    def test_old_qualified_result_cannot_be_displayed_after_title_change(self):
        self.add(0,1);self.accept(review_packet(self.store,0))
        vid=next(iter(self.store.state['candidates']))
        self.store.state['results'][vid]={'status':'qualified'}
        from workflow import verification_fingerprint
        self.store.state['results'][vid]['verification_fingerprint']=verification_fingerprint(
            self.store,self.store.state['candidates'][vid],self.store.state['results'][vid])
        self.assertEqual(eligible_ids(self.store),{vid})
        self.store.state['candidates'][vid]['title']='新标题'
        self.assertEqual(eligible_ids(self.store),set())


if __name__ == '__main__':unittest.main()
