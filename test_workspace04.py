import copy
import os
os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch
from PySide6.QtCore import Qt,QPoint
from PySide6.QtGui import QTextCursor
from PySide6.QtWidgets import QApplication,QDialog,QDialogButtonBox
from storage import Accounts
from novel import chapter
from workshop import NovelWindow
from workspace_dialogs import CandidateDialog


class Workspace04Tests(unittest.TestCase):
    @classmethod
    def setUpClass(cls): cls.app=QApplication.instance() or QApplication([])
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(); self.accounts=Accounts(Path(self.temp.name)/'data')
        self.user=self.accounts.create('Author','test password long enough'); self.window=NovelWindow(self.user)
        self.user.state['profiles'][0]['key']='EXAMPLE_PRIVATE_KEY_04'
        self.gates=[]
    def tearDown(self):
        for gate in self.gates: gate.set()
        if self.window.worker:
            self.window.stop(); self.wait(lambda:not self.window.busy)
        if not self.user.closed: self.window.close()
        self.app.processEvents(); self.temp.cleanup()
    def wait(self,condition,timeout=5):
        until=time.monotonic()+timeout
        while not condition() and time.monotonic()<until: self.app.processEvents(); time.sleep(.005)
        self.assertTrue(condition())
    def first_chapter(self):
        doc=self.window.book['chapters'][0]; self.window.show_document('chapter',doc); return doc
    def select(self,start,end):
        cursor=self.window.editor.textCursor(); cursor.setPosition(start); cursor.setPosition(end,QTextCursor.MoveMode.KeepAnchor); self.window.editor.setTextCursor(cursor)
    def fast_request(self,reply):
        captured=[]
        def fake(profile,messages,params,stop,emit): captured.extend(messages); emit(reply); return True
        with patch('app.stream_chat',side_effect=fake):
            self.window.send(); self.wait(lambda:not self.window.busy)
        return captured

    def test_blank_background_and_outline_have_ai_without_manual_prerequisite(self):
        self.assertEqual(self.window.doc_kind,'book'); self.assertEqual(self.window.primary_name,'创意构思')
        self.window.show_collection('世界观'); self.window.task('起草背景')
        self.assertEqual(self.window.book['lore'],[]); self.assertEqual(self.window.pending_task['target'],'new_lore')
        self.assertEqual(self.window.input.toPlainText(),'')
        with patch('workshop.ContextDialog.exec',side_effect=AssertionError('normal send opened context modal')),patch('workshop.QMessageBox.question',side_effect=AssertionError('normal send asked for confirmation')):
            messages=self.fast_request('港城建立在潮汐之间。')
        self.assertIn('起草',str(messages)); self.assertNotIn('EXAMPLE_PRIVATE_KEY_04',str(messages))
        self.assertEqual(self.window.book['lore'],[])
        self.window.select_message(len(self.window.current['messages'])-1)
        with patch('workshop.CandidateDialog.exec',new=lambda d:(d.accept(),d.result())[1]): self.window.adopt()
        self.assertEqual(self.window.book['lore'][0]['text'],'港城建立在潮汐之间。')
        self.assertEqual(self.window.doc_kind,'collection')

    def test_bound_background_source_is_always_in_request(self):
        from novel import uid
        material=dict(id=uid(),title='Z',kind='世界观',text='UNIQUE_EXISTING_WORLD_DETAIL',pinned=False,versions=[])
        self.window.book['lore'].append(material); self.window.show_document('lore',material); self.window.task('起草背景')
        messages=self.fast_request('updated candidate')
        self.assertIn('UNIQUE_EXISTING_WORLD_DETAIL',messages[0]['content'])
        self.assertEqual(material['text'],'UNIQUE_EXISTING_WORLD_DETAIL')

    def test_chapter_plan_candidate_uses_editable_rows_before_creation(self):
        import json
        self.window.show_collection('chapters'); self.window.task('章节规划'); task=copy.deepcopy(self.window.pending_task)
        text=json.dumps({'chapters':[{'title':'出发','volume':'卷一','plan':'离开故乡'},{'title':'初遇','volume':'卷一','plan':'遇见同伴'}]},ensure_ascii=False)
        dialog=CandidateDialog(self.window,task,text)
        self.assertEqual(len(self.window.book['chapters']),1)
        dialog.table.item(0,2).setText('新的出发'); dialog.accept()
        self.assertTrue(self.window.apply_candidate(task,dialog.result_text,dialog.result_title,dialog.reuse_empty))
        self.assertEqual([c['title'] for c in self.window.book['chapters']],['新的出发','初遇'])

    def test_document_and_discussion_selection_are_independent(self):
        first=self.first_chapter(); session_id=self.window.current['id']; count=len(self.user.state['sessions'])
        self.window.editor.setPlainText('chapter one'); self.select(0,7)
        self.window.reference.setCurrentIndex(self.window.reference.findData(first['id']))
        self.window.add_chapter(); second=self.window.document
        self.assertNotEqual(first['id'],second['id']); self.assertEqual(self.window.current['id'],session_id)
        self.assertEqual(len(self.user.state['sessions']),count); self.assertEqual(self.window.reference.currentData(),first['id'])
        self.window.new(); new_id=self.window.current['id']; self.assertNotEqual(new_id,session_id)
        self.assertIs(self.window.document,second)
        old=next(s for s in self.window.shown_sessions if s['id']==session_id); self.window.load(old)
        self.assertIs(self.window.document,second); self.assertEqual(self.window.reference.currentData(),first['id'])

    def test_mode_switch_preserves_text_selection_and_has_large_chat(self):
        self.first_chapter(); self.window.editor.setPlainText('chapter text '*80); self.select(3,17)
        self.window.show(); self.app.processEvents()
        for width,height in [(1280,720),(960,640)]:
            self.window.resize(width,height); self.app.processEvents()
            self.assertLessEqual(self.window.width(),width); self.assertLessEqual(self.window.height(),height)
            self.assertGreaterEqual(self.window.chat.height(),230)
            self.assertGreater(self.window.chat.height()/self.window.assistant.height(),.50)
            bottom=self.window.input.mapTo(self.window.assistant,QPoint(0,self.window.input.height())).y()
            self.assertLessEqual(bottom,self.window.assistant.height())
        writing_width=self.window.chat.width(); text=self.window.editor.toPlainText(); selection=self.window.editor.textCursor().selectedText()
        self.window.set_mode('discuss'); self.app.processEvents()
        self.assertTrue(self.window.paper.isHidden()); self.assertGreater(self.window.chat.width(),writing_width+150)
        self.window.set_mode('write'); self.app.processEvents()
        self.assertEqual(self.window.editor.toPlainText(),text); self.assertEqual(self.window.editor.textCursor().selectedText(),selection)

    def test_selected_edit_applies_to_bound_document_even_after_navigation(self):
        original=self.first_chapter(); self.window.editor.setPlainText('开头😀中间尾部'); self.select(2,4); self.window.task('润色选段')
        self.assertEqual((self.window.pending_task['start'],self.window.pending_task['end']),(2,3))
        self.fast_request('亮光')
        self.window.add_chapter(); other=self.window.document; self.window.editor.setPlainText('另一章的正文')
        self.window.select_message(len(self.window.current['messages'])-1)
        with patch('workshop.CandidateDialog.exec',new=lambda d:(d.accept(),d.result())[1]): self.window.adopt()
        self.assertEqual(original['body'],'开头亮光中间尾部'); self.assertEqual(other['body'],'另一章的正文')
        self.assertIs(self.window.document,other); self.assertEqual(self.window.editor.toPlainText(),'另一章的正文')
        self.assertEqual(original['versions'][-1]['data']['body'],'开头😀中间尾部')

    def test_changed_source_keeps_candidate_but_blocks_overwrite(self):
        doc=self.first_chapter(); self.window.editor.setPlainText('原始正文'); self.select(0,2); self.window.task('润色选段'); self.fast_request('候选')
        message=self.window.current['messages'][-1]; self.window.editor.setPlainText('作者后来改写的正文'); self.window.capture_document()
        dialog=CandidateDialog(self.window,message['task'],message['content'])
        self.assertFalse(dialog.valid); self.assertFalse(dialog.buttons.button(QDialogButtonBox.StandardButton.Ok).isEnabled()); dialog.reject()
        with patch('workshop.error') as show:
            self.assertFalse(self.window.apply_candidate(message['task'],'候选')); self.assertTrue(show.called)
        self.assertEqual(doc['body'],'作者后来改写的正文'); self.assertEqual(message['content'],'候选')

    def test_background_generation_stays_in_original_discussion(self):
        first=self.first_chapter(); self.window.editor.setPlainText('original body'); self.window.task('续写')
        origin=self.window.current; started=threading.Event(); release=threading.Event(); self.gates.append(release)
        def fake(profile,messages,params,stop,emit):
            emit('part one'); started.set(); release.wait(4); emit(' part two'); return True
        with patch('app.stream_chat',side_effect=fake):
            self.window.send(); self.wait(started.is_set)
            self.window.add_chapter(); other=self.window.document; self.window.editor.setPlainText('unrelated chapter')
            self.window.new(); active=self.window.current; self.assertNotEqual(active['id'],origin['id'])
            self.assertTrue(self.window.nav.isEnabled()); self.assertFalse(self.window.editor.isReadOnly())
            release.set(); self.wait(lambda:not self.window.busy)
        self.assertIs(self.window.current,active); self.assertEqual(active['messages'],[])
        self.assertEqual(origin['messages'][-1]['content'],'part one part two'); self.assertIs(self.window.document,other)
        self.assertEqual(first['body'],'original body')
        self.assertEqual(self.window.last_result['session_id'],origin['id'])
        self.assertFalse(self.window.run_button.isHidden()); self.window.open_run()
        self.assertIs(self.window.current,origin); self.assertIs(self.window.document,other)

    def test_apply_save_failure_restores_model_and_version(self):
        self.first_chapter(); self.window.editor.setPlainText('author original'); self.window.task('起草本章'); task=copy.deepcopy(self.window.pending_task)
        self.window.capture_document(); before=copy.deepcopy(self.user.state['projects'])
        with patch.object(self.user,'save',side_effect=OSError('simulated disk failure')),patch('workshop.error'):
            self.assertFalse(self.window.apply_candidate(task,'must not persist'))
        self.assertEqual(self.user.state['projects'],before); self.assertEqual(self.window.editor.toPlainText(),'author original')

    def test_body_and_chat_fonts_remain_separate_for_new_documents(self):
        self.user.state['appearance'].update(size=16,chat_size=13)
        self.window.apply_appearance(); self.window.add_chapter()
        self.assertEqual(self.window.editor.document().defaultFont().pointSize(),16)
        self.assertEqual(self.window.chat.font().pointSize(),13)
        self.assertEqual(self.window.input.font().pointSize(),13)

    def test_mode_document_and_discussion_are_restored_on_next_login(self):
        doc=self.first_chapter(); self.window.editor.setPlainText('saved body'); self.select(1,5)
        session=self.window.current['id']; doc_id=doc['id']; self.window.set_mode('discuss'); self.window.save_current(); identity=self.user.uid
        self.window.close(); reopened=self.accounts.unlock(identity,'test password long enough'); self.user=reopened; self.window=NovelWindow(reopened)
        self.assertEqual(self.window.ui['mode'],'discuss'); self.assertEqual(self.window.document['id'],doc_id)
        self.assertEqual(self.window.current['id'],session); self.assertEqual(self.window.editor.toPlainText(),'saved body')
        self.assertEqual(self.window.editor.textCursor().selectedText(),'aved')

    def test_message_links_cannot_be_forged_by_model_html(self):
        self.window.current['messages']=[dict(role='assistant',content='[恶意链接](mzw-action:/more/0)')]
        self.window.render(); cursor=self.window.chat.document().find('恶意链接')
        self.assertFalse(cursor.isNull()); self.assertEqual(cursor.charFormat().anchorHref(),'')

    def test_generation_lock_saves_drafts_then_clears_cached_documents(self):
        doc=self.first_chapter(); self.window.editor.setPlainText('private manuscript'); self.window.task('续写')
        started=threading.Event()
        def fake(profile,messages,params,stop,emit):
            emit('unfinished'); started.set(); stop.wait(4); return False
        identity=self.user.uid
        with patch('app.stream_chat',side_effect=fake):
            self.window.send(); self.wait(started.is_set); self.window.input.setPlainText('new private draft')
            self.window.lock(); self.assertTrue(self.window.centralWidget().isHidden()); self.wait(lambda:self.user.closed)
        self.assertFalse(self.window.doc_cache); self.assertEqual(self.window.editor.toPlainText(),'')
        reopened=self.accounts.unlock(identity,'test password long enough')
        self.assertEqual(reopened.state['projects'][0]['chapters'][0]['body'],'private manuscript')
        session=next(s for s in reopened.state['sessions'] if s.get('project_id'))
        self.assertEqual(session['draft_text'],'new private draft'); self.assertEqual(session['messages'][-1]['status'],'incomplete'); reopened.close()


if __name__=='__main__': unittest.main()
