import copy
import os
os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch
from PySide6.QtWidgets import QApplication,QDialog,QMessageBox,QInputDialog
from novel import project,chapter,uid,checkpoint,restore,context_sources,assemble,remove_message,undo_remove,exclude_message,branch,candidate_branch
from core import messages_for
from storage import Accounts,default_session
from workshop import NovelWindow,ContextDialog,CHAPTER_FIELDS


class CanonTests(unittest.TestCase):
    def test_context_retrieval_budget_and_explicit_selection(self):
        book=project('星海'); book.update(outline='主角寻找遗失的星图',premise='冒险',style='第三人称')
        c1=book['chapters'][0]; c1.update(body='第一章结尾',summary='主角遇到林舟')
        c2=chapter('第二章'); c2.update(plan='林舟调查星图',body='本章正在写')
        book['chapters'].append(c2)
        lore=dict(id=uid(),kind='人物',title='林舟',text='林舟怕水',pinned=False)
        other=dict(id=uid(),kind='地点',title='远方城堡',text='城堡建于千年前',pinned=False)
        book['lore']=[lore,other]
        entries=context_sources(book,c2,'林舟会游泳吗')
        result=assemble(entries,2000)
        self.assertIn('主角寻找遗失的星图',result['text']); self.assertIn('林舟怕水',result['text'])
        self.assertNotIn('城堡建于千年前',result['text'])
        self.assertIn('第一章结尾',result['text']); self.assertIn('主角遇到林舟',result['text'])
        manual=assemble(entries,2000,{'canon'})
        self.assertNotIn('林舟怕水',manual['text']); self.assertIn(c2['plan'],manual['text'])
        with self.assertRaises(ValueError): assemble(entries,20)
        self.assertNotIn('body:'+c2['id'],result['selected'])

    def test_exclusion_delete_undo_and_branches(self):
        s=default_session(); s['messages']=[dict(role='user',content='question'),dict(role='assistant',content='bad answer'),dict(role='user',content='follow up'),dict(role='assistant',content='descendant')]
        exclude_message(s,1); self.assertNotIn('bad answer',str(messages_for(s)))
        self.assertIn('descendant',str(messages_for(s)))
        original=copy.deepcopy(s); remove_message(s,1)
        self.assertNotIn('bad answer',str(messages_for(s))); self.assertTrue(undo_remove(s))
        self.assertEqual(s['messages'],original['messages'])
        b=branch(s,0); self.assertEqual(len(b['messages']),1); self.assertNotEqual(b['id'],s['id'])
        b['messages'][0]['content']='changed'; self.assertEqual(s['messages'][0]['content'],'question')
        fresh=candidate_branch(s,1); self.assertEqual(fresh['messages'],[]); self.assertEqual(fresh['draft_text'],'question')
        self.assertNotIn('bad answer',str(fresh['messages']))

    def test_revisions_restore_and_bound(self):
        c=chapter(); c['body']='first'; checkpoint(c,CHAPTER_FIELDS)
        c['body']='second'; checkpoint(c,CHAPTER_FIELDS)
        self.assertEqual(c['versions'][-1]['data']['body'],'first')
        restore(c,len(c['versions'])-1,CHAPTER_FIELDS)
        self.assertEqual(c['body'],'first'); self.assertEqual(c['versions'][-1]['data']['body'],'second')
        for i in range(60): c['body']=str(i); checkpoint(c,CHAPTER_FIELDS)
        self.assertEqual(len(c['versions']),50)
        book=project('Original title'); book['outline']='new outline'
        checkpoint(book,['title','premise','outline','style']); restore(book,0,['title','premise','outline','style'])
        self.assertEqual(book['outline'],'')


class WorkshopTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls): cls.app=QApplication.instance() or QApplication([])
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(); self.accounts=Accounts(Path(self.temp.name)/'data'); self.password='novel test password long'
        self.user=self.accounts.create('Author',self.password); self.window=NovelWindow(self.user)
    def tearDown(self):
        if not self.user.closed: self.window.close()
        self.app.processEvents(); self.temp.cleanup()

    def test_novel_drafts_versions_and_locked_reopen(self):
        self.window.editor.setPlainText('private novel body'); self.window.plan_editor.setPlainText('private chapter outline')
        self.window.save_version(); self.window.editor.setPlainText('private revised body'); self.window.save_version()
        self.assertEqual(self.window.chapter['versions'][-1]['data']['body'],'private novel body')
        self.window.input.setPlainText('private unsent request')
        prompt=self.window.prompt.toPlainText(); uid=self.user.uid
        self.window.lock(); self.app.processEvents()
        self.assertTrue(self.user.closed); self.assertEqual(self.window.editor.toPlainText(),'')
        reopened=self.accounts.unlock(uid,self.password)
        self.assertEqual(reopened.state['projects'][0]['chapters'][0]['body'],'private revised body')
        sessions=[s for s in reopened.state['sessions'] if s.get('project_id')]
        self.assertEqual(sessions[0]['draft_text'],'private unsent request'); self.assertEqual(sessions[0]['system'],prompt)
        for p in self.accounts.root.rglob('*'):
            if p.is_file(): self.assertNotIn(b'private revised body',p.read_bytes())
        reopened.close()

    def test_delete_exclude_and_regenerate_in_ui(self):
        self.window.current['messages']=[dict(role='user',content='q'),dict(role='assistant',content='unwanted')]
        self.window.render(); self.window.reply_list.setCurrentRow(1); self.window.exclude_reply()
        self.assertNotIn('unwanted',str(messages_for(self.window.current)))
        self.window.delete_reply(); self.assertEqual(len(self.window.current['messages']),1)
        self.window.undo_reply(); self.assertEqual(len(self.window.current['messages']),2)
        self.window.reply_list.setCurrentRow(1); old=self.window.current; self.window.regenerate()
        self.assertIsNot(self.window.current,old); self.assertEqual(self.window.input.toPlainText(),'q')
        self.assertEqual(self.window.current['messages'],[])

    def test_context_dialog_no_silent_truncation(self):
        self.window.book['outline']='canonical outline'
        dialog=self.window.context_dialog()
        self.assertIn('canonical outline',dialog.bundle['text'])
        dialog.reject(); self.assertIsNone(dialog.bundle); self.assertEqual(dialog.entries,[])

    def test_mocked_ai_stays_candidate_until_adopted(self):
        self.window.user.state['profiles'][0]['key']='EXAMPLE_TEST_KEY'
        self.window.book['outline']='confirmed outline'; self.window.chapter['body']='confirmed body'; self.window.show_document('chapter',self.window.chapter)
        self.window.input.setPlainText('continue the story')
        captured=[]
        def fake(profile,messages,params,stop,emit):
            captured.extend(messages); emit('unconfirmed new event'); return True
        with patch('workshop.ContextDialog.exec',return_value=QDialog.DialogCode.Accepted),patch('app.QMessageBox.question',return_value=QMessageBox.StandardButton.Yes),patch('app.stream_chat',side_effect=fake):
            self.window.send(); limit=time.monotonic()+5
            while self.window.busy and time.monotonic()<limit: self.app.processEvents(); time.sleep(0.005)
        self.assertFalse(self.window.busy); self.assertIn('confirmed outline',captured[0]['content'])
        self.assertEqual(self.window.chapter['body'],'confirmed body'); self.assertEqual(self.window.chapter['summary'],'')
        self.assertEqual(self.window.book['lore'],[])
        self.assertIn('unconfirmed new event',self.window.current['messages'][-1]['content'])
        # Confirm the editable adoption dialog: only explicit acceptance updates canon.
        with patch('workshop.QDialog.exec',return_value=QDialog.DialogCode.Accepted): self.window.adopt()
        self.assertIn('unconfirmed new event',self.window.chapter['body'])
        self.assertTrue(self.window.chapter['versions'])

    def test_project_switch_and_chapter_order(self):
        self.window.editor.setPlainText('first project text'); first=self.window.book['id']
        with patch('workshop.QInputDialog.getText',return_value=('Second book',True)): self.window.create_book()
        self.assertNotEqual(self.window.book['id'],first); self.assertEqual(self.window.editor.toPlainText(),'')
        self.window.add_chapter(); second_chapter=self.window.chapter['id']; self.window.move_chapter(-1)
        self.assertEqual(self.window.book['chapters'][0]['id'],second_chapter)
        self.window.books.setCurrentIndex(self.window.books.findData(first))
        self.assertEqual(self.window.editor.toPlainText(),'first project text')
        self.assertTrue(all(s.get('project_id')==first for s in self.window.shown_sessions))


if __name__=='__main__': unittest.main()
