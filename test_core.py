import base64
import copy
import io
import json
import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from core import endpoint, messages_for, estimate, export_text, stream_chat, allowed_path, atomic_write, safe_error
from storage import Accounts, default_session, derive, valid_id
from attachments import parse_file


class StorageTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(); self.root=Path(self.temp.name)
        self.accounts=Accounts(self.root/'data')
        self.password='test password long enough'
        self.user=self.accounts.create('Alice',self.password)
    def tearDown(self): self.temp.cleanup()
    def text_file(self,text='private attachment sentence'):
        path=self.root/'sample.txt'; path.write_text(text,encoding='utf-8'); return path

    def test_account_isolation_encryption_and_tampering(self):
        self.user.state['profiles'][0]['key']='EXAMPLE_KEY_DO_NOT_USE'
        self.user.state['sessions']=[default_session()]
        self.user.state['sessions'][0]['messages']=[dict(role='user',content='private conversation sentence')]
        aid=self.user.add_attachment(self.text_file())
        other=self.accounts.create('Bob',self.password)
        self.assertEqual(other.state['sessions'],[])
        with self.assertRaises(ValueError): other.read_blob(aid)
        for path in (self.root/'data').rglob('*'):
            if path.is_file():
                raw=path.read_bytes()
                for secret in [b'EXAMPLE_KEY_DO_NOT_USE',b'private attachment sentence',b'private conversation sentence',self.password.encode()]:
                    self.assertNotIn(secret,raw)
        reopened=self.accounts.unlock(self.user.uid,self.password)
        self.assertEqual(reopened.read_blob(aid),b'private attachment sentence')
        with self.assertRaises(Exception): self.accounts.unlock(self.user.uid,'wrong')
        path=self.user.folder/'state.enc'; data=bytearray(path.read_bytes()); data[-1]^=1; path.write_bytes(data)
        with self.assertRaises(Exception): self.accounts.unlock(self.user.uid,self.password)

    def test_rename_password_change_and_delete(self):
        uid=self.user.uid; folder=self.user.folder
        self.accounts.rename(uid,'Alice Changed')
        self.assertEqual(folder,self.user.folder)
        self.assertEqual(self.user.name,'Alice Changed')
        self.user.change_password(self.password,'replacement password long')
        with self.assertRaises(Exception): self.accounts.unlock(uid,self.password)
        self.assertEqual(self.accounts.unlock(uid,'replacement password long').name,'Alice Changed')
        with self.assertRaises(Exception): self.accounts.delete(uid,'wrong')
        self.accounts.delete(uid,'replacement password long')
        self.assertFalse(folder.exists()); self.assertEqual(self.accounts.users,[])

    def test_duplicate_names_and_invalid_ids(self):
        with self.assertRaises(ValueError): self.accounts.create('ALICE',self.password)
        with self.assertRaises(ValueError): self.accounts.create('Ｂｏｂ','short')
        for value in ['../escape','A'*32,'a/b','']:
            with self.assertRaises(ValueError): valid_id(value)

    def test_backup_restore_and_wrong_password_no_side_effect(self):
        aid=self.user.add_attachment(self.text_file())
        self.user.state['profiles'][0]['key']='EXAMPLE_KEY_DO_NOT_USE'
        self.user.save()
        path=self.root/'saved.cdbak'; self.user.backup(path,'backup password long')
        before=len(self.accounts.users)
        with self.assertRaises(Exception): self.accounts.restore(path,'wrong','Restored',self.password)
        self.assertEqual(len(self.accounts.users),before)
        restored=self.accounts.restore(path,'backup password long','Restored',self.password)
        self.assertNotEqual(restored.uid,self.user.uid)
        self.assertEqual(restored.read_blob(aid),self.user.read_blob(aid))
        self.assertEqual(restored.state['profiles'][0]['key'],'EXAMPLE_KEY_DO_NOT_USE')
        self.assertNotEqual(restored.blob_path(aid).read_bytes(),self.user.blob_path(aid).read_bytes())
        self.assertNotIn(b'private attachment',path.read_bytes())

    def test_attachment_deletion_clears_all_references(self):
        aid=self.user.add_attachment(self.text_file()); s=default_session()
        s['messages']=[dict(role='user',content='q',attachments=[aid])]; s['disabled_attachments']=[aid]
        self.user.state['sessions']=[s]; self.user.save()
        self.user.delete_attachment(aid)
        self.assertEqual(s['messages'][0]['attachments'],[])
        self.assertFalse(self.user.blob_path(aid).exists())
        self.assertNotIn(aid,self.accounts.unlock(self.user.uid,self.password).state['attachments'])

    def test_close_clears_account_and_blocks_use(self):
        self.user.close()
        self.assertIsNone(self.user.key); self.assertEqual(self.user.state,{})
        with self.assertRaises(ValueError): self.user.save()

    def test_legacy_import(self):
        old=self.root/'old'; old.mkdir(); salt=os.urandom(16); nonce=os.urandom(12)
        encrypted=AESGCM(derive(self.password,salt)).encrypt(nonce,json.dumps({'DeepSeek':'EXAMPLE_KEY'}).encode(),b'ChatDesk-v1')
        (old/'vault.enc').write_text(json.dumps(dict(salt=base64.b64encode(salt).decode(),nonce=base64.b64encode(nonce).decode(),data=base64.b64encode(encrypted).decode())))
        session=default_session(); session['messages']=[dict(role='user',content='old question')]
        (old/'conversations.json').write_text(json.dumps(dict(profiles=[dict(name='DeepSeek',url='https://api.deepseek.com/v1',model='deepseek-chat')],sessions=[session])))
        self.user.import_v1(old,self.password)
        self.assertEqual(self.user.state['profiles'][0]['key'],'EXAMPLE_KEY')
        self.assertEqual(self.user.state['sessions'][0]['messages'][0]['content'],'old question')
        self.assertTrue((old/'conversations.json').exists())

    def test_write_failure_has_no_fallback(self):
        path=self.root/'no-folder'/'file.txt'
        with self.assertRaises(OSError): atomic_write(path,b'private')
        self.assertFalse(path.exists())


class ProtocolTests(unittest.TestCase):
    @unittest.skipUnless(os.name=='nt','Windows-only system drive validation')
    def test_windows_system_drive_rejected(self):
        with self.assertRaises(ValueError): allowed_path(Path(os.environ['SystemRoot']))
        with self.assertRaises(ValueError): allowed_path(Path(os.environ['SystemRoot'])/'Temp'/'chatdesk-test.txt')

    def test_urls_and_error_redaction(self):
        self.assertEqual(endpoint('https://api.deepseek.com/v1'),'https://api.deepseek.com/v1/chat/completions')
        for url in ['http://example.org','https://user:key@example.org','https://example.org?key=secret','https://example.org/#secret']:
            with self.assertRaises(ValueError): endpoint(url)
        self.assertNotIn('PRIVATE',safe_error(ValueError('PRIVATE')))

    def test_context_text_images_and_exclusions(self):
        s=default_session(); s['rounds']=1
        s['messages']=[dict(role='user',content='old'),dict(role='assistant',content='old answer'),
           dict(role='user',content='recent',attachments=['text']),dict(role='assistant',content='partial',status='incomplete'),dict(role='user',content='current',attachments=['image'])]
        meta={'text':dict(name='t.txt',kind='text',text='document content'), 'image':dict(name='i.png',kind='image',mime='image/png')}
        with self.assertRaises(ValueError): messages_for(s,meta,lambda _:b'bytes',False)
        messages=messages_for(s,meta,lambda _:b'bytes',True)
        self.assertNotIn('old answer',str(messages)); self.assertNotIn('partial',str(messages))
        self.assertIn('document content',messages[1]['content'])
        self.assertIn('data:image/png;base64,',messages[-1]['content'][1]['image_url']['url'])
        self.assertGreater(estimate(messages),1500)
        s['disabled_attachments']=['text','image']; messages=messages_for(s,meta,None,False)
        self.assertNotIn('document content',str(messages)); self.assertIsInstance(messages[-1]['content'],str)
        self.assertIn('## 背景提示词',export_text(s,meta,False,True,True))
        self.assertEqual(export_text(s,meta,True),'partial')

    def test_stream(self):
        class Response:
            def __enter__(self): return self
            def __exit__(self,*_): pass
            def __iter__(self): return iter([b'data: {"choices":[{"delta":{"content":"hello"}}]}\n',b'data: [DONE]\n'])
        class Client:
            def open(self,request,timeout): return Response()
        output=[]
        with patch('urllib.request.build_opener',return_value=Client()):
            self.assertTrue(stream_chat(dict(model='m',url='https://example.org',key='test'),[dict(role='user',content='q')],dict(temperature=0.7,tokens=100),threading.Event(),output.append))
        self.assertEqual(output,['hello'])
        stopped=threading.Event(); stopped.set()
        with patch('urllib.request.build_opener',return_value=Client()):
            self.assertFalse(stream_chat(dict(model='m',url='https://example.org',key='test'),[],dict(temperature=0.7,tokens=100),stopped,output.append))


class AttachmentTests(unittest.TestCase):
    def test_text_docx_pdf_image_and_limit(self):
        from docx import Document
        from PIL import Image
        from pypdf import PdfWriter
        with tempfile.TemporaryDirectory() as root:
            root=Path(root); text=root/'file.txt'; text.write_text('你好',encoding='utf-8')
            self.assertEqual(parse_file(text)[1]['text'],'你好')
            text.write_text('x'*200001); self.assertTrue(parse_file(text)[1]['truncated'])
            doc=Document(); doc.add_paragraph('document paragraph'); table=doc.add_table(rows=1,cols=1); table.cell(0,0).text='table cell'; path=root/'f.docx'; doc.save(path)
            self.assertIn('table cell',parse_file(path)[1]['text'])
            path=root/'i.png'; Image.new('RGB',(12,12)).save(path)
            self.assertEqual(parse_file(path,True)[1]['kind'],'image')
            with self.assertRaises(ValueError): parse_file(text,True)
            writer=PdfWriter(); writer.add_blank_page(width=100,height=100); path=root/'f.pdf'; writer.write(path)
            with self.assertRaises(ValueError): parse_file(path)
            from pypdf.generic import DictionaryObject,NameObject,DecodedStreamObject
            page=writer.pages[0]
            font=DictionaryObject({NameObject('/Type'):NameObject('/Font'),NameObject('/Subtype'):NameObject('/Type1'),NameObject('/BaseFont'):NameObject('/Helvetica')})
            page[NameObject('/Resources')]=DictionaryObject({NameObject('/Font'):DictionaryObject({NameObject('/F1'):font})})
            content=DecodedStreamObject(); content.set_data(b'BT /F1 12 Tf 10 50 Td (PDF readable text) Tj ET')
            page[NameObject('/Contents')]=content; writer.write(path)
            self.assertIn('PDF readable text',parse_file(path)[1]['text'])
            path=root/'large.txt'
            with open(path,'wb') as f: f.truncate(20*1024*1024+1)
            with self.assertRaises(ValueError): parse_file(path)


if __name__=='__main__': unittest.main()
