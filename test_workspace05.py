"""Stage navigation, adaptive workspace and confirmed manuscript regression checks."""
import os
os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from PySide6.QtWidgets import QApplication,QMessageBox
from PySide6.QtCore import QPoint
from storage import Accounts
from workshop import NovelWindow
from novel import chapter,context_sources,summary_current
from drafting import make_task,apply_proposal


class Workspace05Tests(unittest.TestCase):
    @classmethod
    def setUpClass(cls): cls.app=QApplication.instance() or QApplication([])
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.accounts=Accounts(Path(self.temp.name)/'data')
        self.user=self.accounts.create('Writer','a long test password')
        self.window=NovelWindow(self.user)
    def tearDown(self):
        if not self.user.closed: self.window.close()
        self.app.processEvents(); self.temp.cleanup()

    def test_stages_save_text_without_changing_discussion(self):
        w=self.window; session=w.current['id']
        w.set_stage('idea'); w.plan_editor.setPlainText('A letter from tomorrow')
        w.set_stage('plan')
        self.assertEqual(w.book['premise'],'A letter from tomorrow')
        self.assertEqual(w.stage,'plan')
        w.editor.setPlainText('Find the missing sender')
        w.set_stage('write')
        self.assertEqual(w.book['outline'],'Find the missing sender')
        self.assertEqual(w.doc_kind,'chapter'); self.assertEqual(w.current['id'],session)
        self.assertTrue(w.nav_items[('book','outline')].isHidden())
        self.assertFalse(w.nav_items[('chapter',w.document['id'])].isHidden())

    def test_small_workspace_has_usable_modes_and_visible_composer(self):
        w=self.window; w.show()
        for width,height in [(1440,900),(1280,720),(960,640),(800,600),(720,500)]:
            w.resize(width,height); w.set_mode('write',persist=False); self.app.processEvents()
            self.assertLessEqual(w.minimumSizeHint().width(),width)
            self.assertLessEqual(w.width(),width); self.assertLessEqual(w.height(),height)
            if width<900:
                self.assertTrue(w.assistant.isHidden()); self.assertFalse(w.paper.isHidden())
            w.set_mode('discuss',persist=False); self.app.processEvents()
            self.assertFalse(w.assistant.isHidden()); self.assertTrue(w.paper.isHidden())
            self.assertGreaterEqual(w.chat.height(),230)
            bottom=w.input.mapTo(w,QPoint(w.input.width(),w.input.height()))
            self.assertLessEqual(bottom.y(),w.height()); self.assertLessEqual(bottom.x(),w.width())

    def test_finalized_snapshot_survives_edit_and_unlock(self):
        w=self.window; w.set_stage('write'); w.editor.setPlainText('Confirmed first manuscript')
        with patch('workshop.QMessageBox.question',return_value=QMessageBox.StandardButton.Yes): w.finalize_chapter()
        w.editor.setPlainText('Work in progress'); w.save_current()
        self.assertEqual(w.document['finalized']['body'],'Confirmed first manuscript')
        self.assertIn('修改中',w.chapter_status.text())
        identity=self.user.uid; w.lock()
        reopened=self.accounts.unlock(identity,'a long test password')
        doc=reopened.state['projects'][0]['chapters'][0]
        self.assertEqual(doc['body'],'Work in progress')
        self.assertEqual(doc['finalized']['body'],'Confirmed first manuscript'); reopened.close()

    def test_manual_body_change_excludes_stale_summary(self):
        w=self.window; w.set_stage('write')
        w.editor.setPlainText('The key is in the well'); w.summary_editor.setPlainText('Key in well'); w.save_current()
        first=w.document; self.assertTrue(summary_current(first))
        w.editor.setPlainText('The key is in the attic'); w.save_current()
        later=chapter('Later'); w.book['chapters'].append(later)
        entries=context_sources(w.book,later,'key')
        self.assertNotIn('summary:'+first['id'],[e['id'] for e in entries])
        self.assertEqual(first['summary'],'Key in well')
        w.summary_editor.setPlainText('Key in attic'); w.save_current(); self.assertTrue(summary_current(first))

    def test_ai_revision_invalidates_summary_and_new_summary_restores_it(self):
        w=self.window; w.set_stage('write'); d=w.document
        d.update(body='Before',summary='Before summary')
        task=make_task(w.book,'按意见修稿',d,'chapter')
        apply_proposal(self.user.state,task,'After')
        self.assertFalse(summary_current(d))
        task=make_task(w.book,'总结本章',d,'chapter')
        apply_proposal(self.user.state,task,'After summary')
        self.assertTrue(summary_current(d))


if __name__=='__main__': unittest.main()
