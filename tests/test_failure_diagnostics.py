import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from core import APIError,Store,default_config,now
from tikhub import TikHubClient


class FailureDiagnostics(unittest.TestCase):
    def check_failure(self,key,body):
        with tempfile.TemporaryDirectory() as temp:
            config=default_config('synthetic',['synthetic'],'0.50')
            config['pricing']={'source_url':'https://tikhub.io/pricing','checked_at':now(),
                               'usd_per_request':{'search':'0.01','posts':'0.001','stats':'0.001','comments':'0.001'}}
            store=Store.create(Path(temp)/'run',config)
            client=TikHubClient(store,True,api_key=key,request_interval=0)
            with patch('transport.request',return_value=(400,json.dumps(body))):
                with self.assertRaises(APIError):client.call('posts',{'sec_user_id':'synthetic-author','max_cursor':0})
            journal=(store.root/'requests.jsonl').read_text()
            self.assertNotIn(key,journal);self.assertNotIn('DO-NOT-SAVE-BODY',journal)
            self.assertEqual(len(store.reservations()),1)
            return next(e['diagnostic'] for e in store.events if e['event']=='failed')

    def test_only_safe_provider_identifiers_are_saved(self):
        d=self.check_failure('SYNTHETIC-PRIVATE-KEY',{'code':400,'request_id':'synthetic-request-123',
                                                  'message':'DO-NOT-SAVE-BODY SYNTHETIC-PRIVATE-KEY'})
        self.assertEqual(d,{'http_status':400,'provider_code':400,'provider_request_id':'synthetic-request-123'})

    def test_a_numeric_key_cannot_be_retained_in_code_or_request_id(self):
        d=self.check_failure('12345678901234567890',{'code':12345678901234567890,'request_id':'12345678901234567890'})
        self.assertEqual(d,{'http_status':400})


if __name__=='__main__':unittest.main()
