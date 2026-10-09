import concurrent.futures
import http.client
import json
import os
import stat
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from credentials import CredentialStore,CredentialError,NAME,windows_crypt
from wizard import Session,make_server
from core import StopRun,now

KEY='SYNTHETIC-REMEMBER-KEY-ONLY'


class PrivateStorage(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)/'private';self.store=CredentialStore(self.root)
    def tearDown(self):self.temp.cleanup()

    def test_missing_store_does_not_create_files(self):
        self.assertIsNone(self.store.load());self.assertFalse(self.store.exists());self.store.forget();self.assertFalse(self.root.exists())

    def test_save_restart_load_and_forget_only_remove_key(self):
        self.store.save(KEY);self.assertEqual(CredentialStore(self.root).load(),KEY)
        (self.root/'keep.txt').write_text('unrelated');self.store.forget();self.assertIsNone(self.store.load())
        self.assertEqual((self.root/'keep.txt').read_text(),'unrelated')
        if os.name!='nt':self.assertEqual(stat.S_IMODE(self.root.stat().st_mode),0o700)

    @unittest.skipIf(os.name=='nt','POSIX mode checks')
    def test_owner_only_file_and_reject_broad_permissions(self):
        self.store.save(KEY);path=self.root/NAME
        self.assertEqual(stat.S_IMODE(path.stat().st_mode),0o600)
        path.chmod(0o644)
        with self.assertRaises(CredentialError):self.store.load()
        self.store.forget();self.assertFalse(path.exists())
        self.root.chmod(0o755)
        with self.assertRaises(CredentialError):self.store.save(KEY)

    def test_source_tree_storage_is_disabled(self):
        forbidden=Path(self.temp.name)/'source';store=CredentialStore(forbidden/'private',forbidden_roots=[forbidden])
        self.assertFalse(store.available);self.assertIsNone(store.load());store.forget()
        with self.assertRaises(CredentialError):store.save(KEY)
        self.assertFalse(forbidden.exists())

    @unittest.skipIf(os.name=='nt','Windows symlink creation may need privileges')
    def test_symlink_directory_cannot_redirect_saves(self):
        target=Path(self.temp.name)/'target';target.mkdir();self.root.symlink_to(target,target_is_directory=True)
        with self.assertRaises(CredentialError):self.store.save(KEY)
        self.assertEqual(list(target.iterdir()),[])

    @unittest.skipIf(os.name=='nt','Windows symlink creation may need privileges')
    def test_symlink_file_cannot_be_read_and_forget_does_not_follow_it(self):
        self.root.mkdir(mode=0o700);target=Path(self.temp.name)/'outside';target.write_text('unrelated')
        (self.root/NAME).symlink_to(target)
        with self.assertRaises(CredentialError):self.store.load()
        self.store.forget();self.assertEqual(target.read_text(),'unrelated');self.assertFalse((self.root/NAME).exists())

    @unittest.skipIf(os.name=='nt','POSIX link test')
    def test_hardlinked_secret_is_rejected(self):
        self.store.save(KEY);os.link(self.root/NAME,Path(self.temp.name)/'copy')
        with self.assertRaises(CredentialError):self.store.load()

    def test_failed_atomic_replace_keeps_old_key_and_cleans_temporary_file(self):
        self.store.save(KEY)
        with patch('credentials.os.replace',side_effect=OSError('SYNTHETIC-NEW-KEY')),self.assertRaises(CredentialError) as error:self.store.save('SYNTHETIC-NEW-KEY')
        self.assertNotIn('SYNTHETIC-NEW-KEY',str(error.exception));self.assertEqual(self.store.load(),KEY)
        self.assertEqual([p.name for p in self.root.iterdir()],[NAME])

    def test_concurrent_saves_are_complete_records(self):
        values=[KEY+str(i) for i in range(8)]
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:list(pool.map(self.store.save,values))
        self.assertIn(self.store.load(),values);self.assertEqual(len(list(self.root.iterdir())),1)

    def test_bad_data_and_invalid_key_do_not_echo_secrets(self):
        for key in ('',None,'a\nb','x'*2049):
            with self.assertRaises(CredentialError):self.store.save(key)
        self.assertFalse(self.root.exists())
        self.store.save(KEY);path=self.root/NAME
        for data in (KEY,'{"value":"'+KEY+'"}','x'*17000):
            path.write_text(data)
            with self.assertRaises(CredentialError) as error:self.store.load()
            self.assertNotIn(KEY,str(error.exception))

    @unittest.skipUnless(os.name=='nt','Requires real Windows DPAPI')
    def test_windows_roundtrip_does_not_store_plaintext(self):
        self.store.save(KEY);self.assertNotIn(KEY.encode(),(self.root/NAME).read_bytes());self.assertEqual(self.store.load(),KEY)
        with self.assertRaises(CredentialError):windows_crypt(b'invalid protected blob',decrypt=True)


class SavedCredentialWeb(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.server=make_server(self.temp.name,assistant_mode='host')
        threading.Thread(target=self.server.serve_forever,daemon=True).start()
        self.host='127.0.0.1:'+str(self.server.server_port)
        self.auth={'Cookie':'research_session='+self.server.session.token,'Origin':'http://'+self.host,'X-Wizard':'1'}
    def tearDown(self):self.server.shutdown();self.server.server_close();self.temp.cleanup()
    def request(self,path,body=None,headers=None):
        conn=http.client.HTTPConnection('127.0.0.1',self.server.server_port,timeout=3)
        headers={'Content-Type':'application/json',**(self.auth if headers is None else headers)}
        conn.request('GET' if body is None else 'POST',path,None if body is None else json.dumps(body),headers)
        response=conn.getresponse();status=response.status;raw=response.read();conn.close();return status,raw

    def test_remember_is_opt_in_and_secret_never_returned(self):
        status,raw=self.request('/api/setup',{'mode':'live','key':KEY,'remember':True})
        self.assertEqual(status,200);self.assertNotIn(KEY.encode(),raw)
        status,raw=self.request('/api/state');self.assertNotIn(KEY.encode(),raw);self.assertTrue(json.loads(raw)['key_saved'])
        self.assertEqual(self.request('/report/../private/tikhub.json')[0],404)
        self.assertEqual(self.request('/private/tikhub.json')[0],404)
        fresh=Session(self.temp.name);self.assertTrue(fresh.snapshot()['key_ready']);self.assertIsNone(fresh.key)
        fresh.key=None;self.assertTrue(fresh.snapshot()['key_ready'])

    def test_default_legacy_setup_stays_in_memory(self):
        self.assertEqual(self.request('/api/setup',{'mode':'live','key':KEY})[0],200)
        self.assertFalse(self.server.session.credentials.exists())
        self.assertFalse(Session(self.temp.name).snapshot()['key_ready'])

    def test_next_start_loads_saved_key_without_new_input(self):
        self.request('/api/setup',{'mode':'live','key':KEY,'remember':True})
        fresh=Session(self.temp.name)
        quote={'source_url':'https://tikhub.io/pricing','checked_at':now(),'usd_per_request':{x:'0.001' for x in ('search','posts','stats','comments')}}
        with patch.object(fresh,'select_assistant',return_value=Mock()),patch('wizard.fetch_prices',return_value=quote),patch('wizard.threading.Thread'):
            fresh.start({'description':'AI 自媒体','consent':True})
        self.assertEqual(fresh.key,KEY)

    def test_forget_requires_auth_and_clears_memory_and_file(self):
        self.request('/api/setup',{'mode':'live','key':KEY,'remember':True})
        self.assertEqual(self.request('/api/forget-key',{},headers={})[0],403)
        self.assertEqual(self.request('/api/forget-key',{},headers={'Origin':'http://'+self.host,'X-Wizard':'1'})[0],401)
        headers=dict(self.auth,Origin='https://evil.example')
        self.assertEqual(self.request('/api/forget-key',{},headers=headers)[0],403)
        self.server.session.status='running'
        self.assertEqual(self.request('/api/forget-key',{})[0],400);self.assertTrue(self.server.session.credentials.exists())
        self.server.session.status='ready';status,raw=self.request('/api/forget-key',{})
        self.assertEqual(status,200);self.assertNotIn(KEY.encode(),raw);self.assertIsNone(self.server.session.key)
        self.assertFalse(Session(self.temp.name).snapshot()['key_ready'])

    def test_unchecking_remember_removes_previous_saved_key(self):
        self.request('/api/setup',{'mode':'live','key':KEY,'remember':True})
        status,_=self.request('/api/setup',{'mode':'live','key':'SYNTHETIC-SESSION-ONLY','remember':False})
        self.assertEqual(status,200);self.assertFalse(self.server.session.credentials.exists())
        self.assertEqual(self.server.session.key,'SYNTHETIC-SESSION-ONLY')

    def test_save_failure_does_not_claim_success_or_replace_session_key(self):
        with patch.object(self.server.session.credentials,'save',side_effect=CredentialError('无法保存')):
            status,raw=self.request('/api/setup',{'mode':'live','key':KEY,'remember':True})
        self.assertEqual(status,400);self.assertNotIn(KEY.encode(),raw);self.assertIsNone(self.server.session.key)
        self.assertFalse(self.server.session.key_remembered)

    def test_non_boolean_remember_does_not_save(self):
        self.assertEqual(self.request('/api/setup',{'mode':'live','key':KEY,'remember':'false'})[0],400)
        self.assertFalse(self.server.session.credentials.exists())


if __name__=='__main__':unittest.main()
