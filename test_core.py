import copy
import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch
from core import Vault, Store, DEFAULT, endpoint, export_text, messages_for, stream_chat

class Checks(unittest.TestCase):
    def test_vault(self):
        with tempfile.TemporaryDirectory() as d:
            v=Vault(d,'a strong test password')
            v.values['DeepSeek']='sk-test-private-123456'
            v.save()
            self.assertNotIn(b'sk-test-private',Path(d,'vault.enc').read_bytes())
            self.assertEqual(Vault(d,'a strong test password').values,v.values)
            with self.assertRaises(Exception): Vault(d,'wrong password')
            store=Store(d,v)
            store.data['sessions']=[{'content':'echo sk-test-private-123456'}]
            store.save()
            self.assertNotIn('sk-test-private',store.path.read_text())
            self.assertIn('[已隐藏密钥]',store.path.read_text())
            e=json.loads(v.path.read_text()); e['data']=e['data'][:-4]+'AAAA'
            v.path.write_text(json.dumps(e))
            with self.assertRaises(Exception): Vault(d,'a strong test password')

    def session(self):
        return dict(system='background',chars=100,style='严格',rounds=1,tokens=100,temperature=0.7,messages=[
            dict(role='user',content='old question'),dict(role='assistant',content='old answer'),
            dict(role='user',content='new question'),dict(role='assistant',content='partial',status='incomplete'),
            dict(role='user',content='current question')])

    def test_context_and_export(self):
        s=self.session(); m=messages_for(s)
        self.assertEqual(m[0]['role'],'system')
        self.assertIn('background',m[0]['content'])
        self.assertEqual(m[1]['content'],'new question')
        self.assertNotIn('partial',str(m))
        self.assertNotIn('old question',str(m))
        self.assertEqual(export_text(s,True),'partial')
        self.assertNotIn('background',export_text(s))
        self.assertIn('## 背景提示词',export_text(s,False,True,True))

    def test_urls(self):
        self.assertEqual(endpoint('https://api.deepseek.com/v1'),'https://api.deepseek.com/v1/chat/completions')
        for url in ['http://a.com','https://user:secret@a.com','https://a.com?key=secret','https://a.com/#secret']:
            with self.assertRaises(ValueError): endpoint(url)

    def test_stream_and_truncation(self):
        class Response:
            def __enter__(self): return self
            def __exit__(self,*_): pass
            def __iter__(self): return iter([
                b'data: {"choices":[{"delta":{"content":"hello"}}]}\n',
                b'data: {"choices":[{"delta":{},"finish_reason":"length"}]}\n',
                b'data: [DONE]\n'])
        class Client:
            def open(self,request,timeout):
                self.request=request
                return Response()
        client=Client(); chunks=[]
        with patch('urllib.request.build_opener',return_value=client):
            self.assertTrue(stream_chat(DEFAULT['profiles'][0],'secret',self.session(),threading.Event(),chunks.append))
        self.assertEqual(chunks[0],'hello')
        self.assertIn('token',chunks[1])
        self.assertEqual(client.request.get_header('Authorization'),'Bearer secret')
        self.assertNotIn('secret',client.request.data.decode())
        stopped=threading.Event(); stopped.set()
        with patch('urllib.request.build_opener',return_value=client):
            self.assertFalse(stream_chat(DEFAULT['profiles'][0],'secret',self.session(),stopped,lambda _:self.fail()))

if __name__=='__main__': unittest.main()
