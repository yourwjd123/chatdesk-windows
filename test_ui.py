"""Offscreen Qt integration checks, no external API calls or real credentials."""
import os
os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from PySide6.QtWidgets import QApplication,QDialog,QMessageBox,QLineEdit
from PySide6.QtGui import QFont
from storage import Accounts
from app import MainWindow,Settings,Login


class QtChecks(unittest.TestCase):
    @classmethod
    def setUpClass(cls): cls.app=QApplication.instance() or QApplication([])
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(); self.accounts=Accounts(Path(self.temp.name)/'data')
        self.user=self.accounts.create('Alice','test password long enough'); self.window=MainWindow(self.user)
    def tearDown(self):
        if not self.user.closed: self.window.close()
        self.app.processEvents(); self.temp.cleanup()

    def test_settings_password_theme_fonts_and_defaults(self):
        settings=Settings(self.window)
        settings.username.setText('Alice Renamed')
        settings.new.setText('replacement password long')
        settings.old.setText('test password long enough'); settings.confirm.setText('replacement password long')
        with patch('app.QMessageBox.information'):
            settings.rename_user(); settings.change_password()
        settings.size.setValue(18); settings.theme.setCurrentText('深色'); settings.default_prompt.setPlainText('new default background')
        settings.api['key'].setText('EXAMPLE_KEY_DO_NOT_USE')
        settings.apply()
        self.assertEqual(settings.result(),QDialog.DialogCode.Accepted)
        self.assertEqual(self.user.name,'Alice Renamed'); self.assertEqual(self.user.state['appearance']['size'],18)
        self.assertEqual(self.user.state['profiles'][0]['key'],'EXAMPLE_KEY_DO_NOT_USE')
        self.assertEqual(settings.values,{})
        self.assertTrue(all(not w.text() for w in settings.findChildren(QLineEdit)))
        self.assertEqual(self.window.current['system'],'你是一位严谨、友好的助手。')
        self.window.new(); self.assertEqual(self.window.current['system'],'new default background')
        with self.assertRaises(Exception): self.accounts.unlock(self.user.uid,'test password long enough')
        reopened=self.accounts.unlock(self.user.uid,'replacement password long')
        self.assertEqual(reopened.state['appearance']['theme'],'深色'); reopened.close()

    def test_encrypted_background_and_attachment_toggle(self):
        from PIL import Image
        path=Path(self.temp.name)/'bg.png'; Image.new('RGB',(80,60),(30,100,170)).save(path)
        aid=self.user.add_attachment(path,True)
        settings=Settings(self.window); settings.bg=aid; settings.shade.setValue(0); settings.apply()
        self.assertFalse(self.window.chat.picture.isNull())
        self.window.draft=[aid]; self.window.refresh_attachments()
        item=self.window.attachment_list.item(0)
        from PySide6.QtCore import Qt
        item.setCheckState(Qt.CheckState.Unchecked)
        self.assertIn(aid,self.window.current['disabled_attachments'])
        self.window.show(); self.app.processEvents()
        self.assertFalse(self.window.grab().isNull())
        self.assertEqual(self.window.chat.viewport().grab().toImage().pixelColor(150,100).name(),'#1e64aa')

    def test_lock_discards_credentials_and_user_state(self):
        self.user.state['profiles'][0]['key']='EXAMPLE_KEY_DO_NOT_USE'
        settings=Settings(self.window); settings.show(); self.window.show(); self.app.processEvents()
        self.window.input.setPlainText('unsent private input')
        self.window.lock(); self.app.processEvents()
        self.assertTrue(self.user.closed); self.assertIsNone(self.window.current)
        self.assertEqual(self.window.input.toPlainText(),''); self.assertEqual(self.window.chat.toPlainText(),'')
        self.assertEqual(settings.values,{})
        self.assertFalse(self.window.exit_app)
        reopened=self.accounts.unlock(self.user.uid,'test password long enough')
        self.assertEqual(reopened.state['sessions'][0]['draft_text'],'unsent private input')
        reopened.close()

    def test_switch_separate_account_and_login(self):
        bob=self.accounts.create('Bob','second password long enough'); uid=bob.uid; bob.close()
        self.window.lock(); self.app.processEvents()
        login=Login(self.accounts); login.names.setCurrentIndex(1); login.password.setText('second password long enough'); login.login()
        self.assertEqual(login.user.uid,uid); self.assertEqual(login.password.text(),'')
        self.assertEqual(login.user.state['sessions'],[]); login.user.close(); login.close()

    def test_mocked_stream_does_not_log_or_keep_plaintext_key(self):
        self.user.state['profiles'][0]['key']='EXAMPLE_KEY_DO_NOT_USE'
        self.window.input.setPlainText('test question')
        def fake(profile,messages,params,stop,emit):
            emit('test reply EXAMPLE_KEY_DO_NOT_USE'); return True
        with patch('app.QMessageBox.question',return_value=QMessageBox.StandardButton.Yes),patch('app.stream_chat',side_effect=fake):
            self.window.send()
            import time
            limit=time.monotonic()+5
            while self.window.busy and time.monotonic()<limit:
                self.app.processEvents(); time.sleep(0.005)
            self.assertFalse(self.window.busy)
        self.assertEqual(self.window.current['messages'][-1]['status'],'complete')
        self.assertIn('[已隐藏密钥]',self.window.current['messages'][-1]['content'])
        self.assertNotIn('EXAMPLE_KEY_DO_NOT_USE',self.window.chat.toPlainText())
        self.assertIsNone(self.window.worker)


if __name__=='__main__': unittest.main()
