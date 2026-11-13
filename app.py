"""ChatDesk 0.2: encrypted multi-user Qt desktop client."""
import os
import sys
sys.dont_write_bytecode=True
import copy
import json
import threading
import time
from pathlib import Path
from core import allowed_path, atomic_write, endpoint, messages_for, estimate, stream_chat, safe_error, export_text
from storage import Accounts, default_session
from PySide6.QtCore import Qt, QTimer, QThread, Signal, QEvent, QEventLoop, QLockFile, QUrl
from PySide6.QtGui import QFont, QFontDatabase, QPixmap, QPainter, QColor, QShortcut, QKeySequence
from PySide6.QtWidgets import (QApplication,QMainWindow,QDialog,QVBoxLayout,QHBoxLayout,QFormLayout,QWidget,
    QLabel,QLineEdit,QPlainTextEdit,QTextBrowser,QPushButton,QComboBox,QSpinBox,QDoubleSpinBox,QCheckBox,
    QListWidget,QListWidgetItem,QFileDialog,QMessageBox,QInputDialog,QTabWidget,QFontComboBox,QSplitter,QDialogButtonBox)


def error(parent,text='操作未完成，请检查密码、文件完整性及指定目录的写入权限。'):
    owner=parent
    while owner is not None:
        user=getattr(owner,'user',None)
        if user is not None and not user.closed:
            text=user.redact(text); break
        owner=owner.parentWidget()
    QMessageBox.warning(parent,'操作未完成',text)


def ask_password(parent,title='身份确认',label='输入本地用户密码：'):
    value,ok=QInputDialog.getText(parent,title,label,QLineEdit.EchoMode.Password)
    return value if ok else None


def choose_save(parent,title,filters):
    name,_=QFileDialog.getSaveFileName(parent,title,str(parent.user.accounts.root),filters)
    return allowed_path(name) if name else None


def line(value='',password=False):
    w=QLineEdit(str(value))
    if password: w.setEchoMode(QLineEdit.EchoMode.Password)
    return w


def button(text,fn,layout):
    b=QPushButton(text); b.clicked.connect(fn); layout.addWidget(b); return b


class Login(QDialog):
    def __init__(self,accounts):
        super().__init__(); self.accounts=accounts; self.user=None
        self.setWindowTitle('ChatDesk 0.2 · 本地用户'); self.resize(480,320)
        layout=QVBoxLayout(self)
        layout.addWidget(QLabel('选择用户并解锁。每个用户的数据和附件独立加密保存。'))
        self.names=QComboBox(); layout.addWidget(self.names); self.refresh()
        self.password=line(password=True); self.password.setPlaceholderText('本地用户密码'); layout.addWidget(self.password)
        self.password.returnPressed.connect(self.login)
        button('解锁登录',self.login,layout)
        row=QHBoxLayout(); layout.addLayout(row)
        button('新建用户',self.create,row); button('恢复加密备份',self.restore,row)
        button('删除用户',self.delete,row)
        text=QLabel('数据根目录：'+str(accounts.root)+'\n忘记密码无法恢复数据；本地账户不能抵御已控制电脑的恶意程序。')
        text.setWordWrap(True); layout.addWidget(text)
        button('取消',self.reject,layout)

    def refresh(self):
        self.names.clear()
        for u in self.accounts.users: self.names.addItem(u['name'],u['id'])

    def login(self):
        try:
            self.user=self.accounts.unlock(self.names.currentData(),self.password.text())
            self.password.clear(); self.accept()
        except Exception: error(self,'密码不正确、用户未选择或数据文件无法读取。')

    def credentials(self,title):
        name,ok=QInputDialog.getText(self,title,'用户名：')
        if not ok: return None
        password=ask_password(self,title,'设置本地密码（至少12位；忘记后无法恢复）：')
        if password is None: return None
        confirm=ask_password(self,title,'再次输入密码：')
        if password!=confirm:
            error(self,'两次密码不一致。'); return None
        return name,password

    def create(self):
        values=self.credentials('新建用户')
        if not values: return
        try:
            self.user=self.accounts.create(*values); self.accept()
        except ValueError as e: error(self,str(e))
        except Exception: error(self)

    def restore(self):
        path,_=QFileDialog.getOpenFileName(self,'选择加密备份',str(self.accounts.root),'ChatDesk备份 (*.cdbak)')
        if not path: return
        password=ask_password(self,'恢复备份','备份密码：')
        if password is None: return
        values=self.credentials('为恢复的数据新建用户')
        if not values: return
        try:
            self.user=self.accounts.restore(path,password,*values); self.accept()
        except Exception: error(self,'备份密码不正确、文件损坏、用户名不可用或目录无法写入。')

    def delete(self):
        uid=self.names.currentData()
        if not uid: return
        answer=QMessageBox.question(self,'删除用户','将删除该用户的配置、会话和所有附件。请先登录并备份。确定继续？')
        if answer!=QMessageBox.StandardButton.Yes: return
        password=ask_password(self)
        if password is None: return
        try: self.accounts.delete(uid,password); self.refresh(); self.password.clear()
        except Exception: error(self)


class ChatView(QTextBrowser):
    def __init__(self):
        super().__init__(); self.picture=QPixmap(); self.mode='填充'; self.shade=80; self.dark=False
        self.setOpenLinks(False); self.setOpenExternalLinks(False)
        self.viewport().setAutoFillBackground(False)

    def loadResource(self,kind,url):
        # Model Markdown must not load local files or external images/trackers.
        return None

    def paintEvent(self,event):
        if not self.picture.isNull():
            painter=QPainter(self.viewport()); rect=self.viewport().rect()
            painter.fillRect(rect,QColor('#17202b' if self.dark else '#ffffff'))
            if self.mode=='居中': pix=self.picture
            else:
                aspect=Qt.AspectRatioMode.KeepAspectRatioByExpanding if self.mode=='填充' else Qt.AspectRatioMode.KeepAspectRatio
                pix=self.picture.scaled(rect.size(),aspect,Qt.TransformationMode.SmoothTransformation)
            painter.drawPixmap((rect.width()-pix.width())//2,(rect.height()-pix.height())//2,pix)
            overlay=QColor('#17202b' if self.dark else '#ffffff'); overlay.setAlpha(round(self.shade*2.55))
            painter.fillRect(rect,overlay); painter.end()
        super().paintEvent(event)


class Request(QThread):
    chunk=Signal(str); result=Signal(str)
    def __init__(self,profile,messages,params):
        super().__init__(); self.profile=profile; self.messages=messages; self.params=params; self.stop=threading.Event()
    def run(self):
        try:
            complete=stream_chat(self.profile,self.messages,self.params,self.stop,self.chunk.emit)
            self.result.emit('done' if complete else 'stopped')
        except Exception as exc: self.result.emit(safe_error(exc))
        finally:
            self.profile.clear(); self.messages.clear(); self.params.clear()


class Settings(QDialog):
    def __init__(self,main):
        super().__init__(main); self.main=main; self.user=main.user
        self.setWindowTitle('设置'); self.resize(740,650)
        self.values=copy.deepcopy(self.user.state)
        layout=QVBoxLayout(self); tabs=QTabWidget(); layout.addWidget(tabs)
        account=QWidget(); form=QFormLayout(account); tabs.addTab(account,'用户与安全')
        self.username=line(self.user.name); form.addRow('用户名',self.username)
        button('修改用户名（立即生效）',self.rename_user,form)
        self.idle=QSpinBox(); self.idle.setRange(1,120); self.idle.setValue(self.values['idle_minutes']); form.addRow('空闲自动锁定（分钟）',self.idle)
        self.old=line(password=True); self.new=line(password=True); self.confirm=line(password=True)
        form.addRow('当前密码',self.old); form.addRow('新密码',self.new); form.addRow('确认新密码',self.confirm)
        button('修改密码（立即生效）',self.change_password,form)
        note=QLabel('用户名可修改，数据目录ID不变。切换用户和手动锁定在主界面。\n删除账户请在登录页面操作；建议先在“数据与附件”备份。'); note.setWordWrap(True); form.addRow(note)
        api=QWidget(); api_layout=QVBoxLayout(api); tabs.addTab(api,'API与模型')
        self.profile_index=-1; self.profile_picker=QComboBox(); api_layout.addWidget(self.profile_picker)
        self.api={}
        fields=QFormLayout(); api_layout.addLayout(fields)
        for key,title in [('name','配置名称'),('url','HTTPS接口基础地址'),('model','模型名称'),('key','API密钥')]:
            w=line(password=key=='key'); self.api[key]=w; fields.addRow(title,w)
        self.vision=QCheckBox('该服务和模型支持OpenAI格式图片输入（需自行确认）'); api_layout.addWidget(self.vision)
        row=QHBoxLayout(); api_layout.addLayout(row)
        button('保存本项编辑',self.commit_profile,row); button('新增配置',self.add_profile,row); button('删除配置',self.remove_profile,row)
        tip=QLabel('切换配置前请先“保存本项编辑”。所有更改须点击底部“应用并关闭”才持久保存。'); tip.setWordWrap(True); api_layout.addWidget(tip)
        self.profile_picker.currentIndexChanged.connect(self.load_profile)
        self.refresh_profiles()
        defaults_page=QWidget(); df=QFormLayout(defaults_page); tabs.addTab(defaults_page,'新会话默认值')
        current=default_session(self.values['defaults'])
        self.default_prompt=QPlainTextEdit(current['system']); df.addRow('背景提示词',self.default_prompt)
        self.default_numbers={}
        for key,title,lo,hi in [('temperature','温度',0,2),('tokens','最大输出token',1,131072),('chars','期望字符（0不限）',0,1000000),('rounds','保留历史轮数',1,500)]:
            w=QDoubleSpinBox() if key=='temperature' else QSpinBox(); w.setRange(lo,hi); w.setValue(current[key]); df.addRow(title,w); self.default_numbers[key]=w
        self.default_style=QComboBox(); self.default_style.addItems(['严格','平衡','自由']); self.default_style.setCurrentText(current['style']); df.addRow('遵循预设',self.default_style)
        df.addRow(QLabel('这些设置只影响以后新建的会话，不改动既有会话。'))
        appearance=QWidget(); af=QFormLayout(appearance); tabs.addTab(appearance,'外观')
        a=self.values['appearance']
        self.family=QFontComboBox(); self.family.setCurrentFont(QFont(a['family'])); af.addRow('正文字体',self.family)
        self.code_family=QFontComboBox(); self.code_family.setCurrentFont(QFont(a['code_family'])); af.addRow('代码字体',self.code_family)
        self.size=QSpinBox(); self.size.setRange(8,40); self.size.setValue(a['size']); af.addRow('字号',self.size)
        self.theme=QComboBox(); self.theme.addItems(['浅色','深色']); self.theme.setCurrentText(a['theme']); af.addRow('主题',self.theme)
        self.bg=a['background']; self.bglabel=QLabel('已选择背景图片' if self.bg else '纯色背景'); af.addRow('背景',self.bglabel)
        button('选择背景图片',self.choose_background,af); button('恢复纯色背景',self.clear_background,af)
        self.mode=QComboBox(); self.mode.addItems(['填充','适应','居中']); self.mode.setCurrentText(a['mode']); af.addRow('图片布局',self.mode)
        self.shade=QSpinBox(); self.shade.setRange(0,100); self.shade.setValue(a['shade']); af.addRow('遮罩强度（0–100）',self.shade)
        self.preview=QLabel('字体预览：你好，ChatDesk 0123456789'); af.addRow(self.preview)
        self.family.currentFontChanged.connect(self.preview_font); self.size.valueChanged.connect(self.preview_font); self.preview_font()
        button('恢复外观默认值',self.reset_appearance,af)
        data=QWidget(); dl=QVBoxLayout(data); tabs.addTab(data,'数据与附件')
        where=QLabel('当前用户目录：'+str(self.user.folder)+'\n附件、会话、密钥、背景均加密保存。\n数据根目录在启动时选择；移动时先关闭程序，完整复制数据根目录，再选择新位置。'); where.setWordWrap(True); dl.addWidget(where)
        button('管理本用户附件',main.manage_attachments,dl)
        button('创建本用户加密备份',self.backup,dl)
        button('从0.1版导入会话和API配置',self.import_legacy,dl)
        dl.addWidget(QLabel('恢复备份：在登录页选择“恢复加密备份”。\n备份包含已保存的数据；请先应用设置，再执行备份。\n原0.1版文件不会自动删除，请自行保管或清理。'))
        dl.addStretch()
        bottom=QHBoxLayout(); layout.addLayout(bottom)
        button('应用并关闭',self.apply,bottom); button('取消',self.reject,bottom)

    def rename_user(self):
        try:
            self.user.accounts.rename(self.user.uid,self.username.text()); self.username.setText(self.user.name); self.main.update_title()
            QMessageBox.information(self,'已修改','用户名已修改，取消设置不会撤销该操作。')
        except ValueError as exc: error(self,str(exc))
        except Exception: error(self)

    def change_password(self):
        try:
            if self.new.text()!=self.confirm.text(): raise ValueError('新密码两次输入不一致。')
            self.user.change_password(self.old.text(),self.new.text())
            self.old.clear(); self.new.clear(); self.confirm.clear()
            QMessageBox.information(self,'已修改','密码已修改，请使用新密码登录。取消设置不会撤销该操作。')
        except ValueError as exc: error(self,str(exc))
        except Exception: error(self,'当前密码不正确或密码文件无法更新。')

    def done(self,result):
        # Closed settings dialogs must not retain a copied credential dictionary.
        self.values.clear()
        for edit in self.findChildren(QLineEdit): edit.clear()
        super().done(result)

    def preview_font(self,*_): self.preview.setFont(QFont(self.family.currentFont().family(),self.size.value()))
    def refresh_profiles(self):
        self.profile_picker.blockSignals(True); self.profile_picker.clear()
        self.profile_picker.addItems([p['name'] for p in self.values['profiles']]); self.profile_picker.blockSignals(False)
        self.load_profile(0)
    def load_profile(self,index):
        if index<0: return
        self.profile_index=index; p=self.values['profiles'][index]
        for k,w in self.api.items(): w.setText(p[k])
        self.vision.setChecked(p['vision'])
    def commit_profile(self):
        try:
            p={k:w.text().strip() for k,w in self.api.items()}; endpoint(p['url'])
            if not p['name'] or not p['model']: raise ValueError('配置名称、模型不可为空。')
            if any(x['name']==p['name'] and i!=self.profile_index for i,x in enumerate(self.values['profiles'])): raise ValueError('配置名称重复。')
            if any(c.isspace() for c in p['key']): raise ValueError('密钥不可包含空白字符。')
            p['vision']=self.vision.isChecked(); self.values['profiles'][self.profile_index]=p
            self.profile_picker.setItemText(self.profile_index,p['name']); return True
        except ValueError as exc: error(self,str(exc)); return False
    def add_profile(self):
        name='新配置'+str(len(self.values['profiles'])+1)
        self.values['profiles'].append(dict(name=name,url='https://api.deepseek.com/v1',model='deepseek-chat',key='',vision=False))
        self.profile_picker.addItem(name); self.profile_picker.setCurrentIndex(len(self.values['profiles'])-1)
    def remove_profile(self):
        if len(self.values['profiles'])<2: error(self,'至少保留一个API配置。'); return
        self.values['profiles'].pop(self.profile_index); self.refresh_profiles()
    def choose_background(self):
        name,_=QFileDialog.getOpenFileName(self,'选择背景图片','','图片 (*.png *.jpg *.jpeg *.webp)')
        if not name: return
        try:
            self.bg=self.user.add_attachment(name,background=True)
            self.bglabel.setText('已选择：'+self.user.redact(Path(name).name))
        except ValueError as exc: error(self,str(exc))
        except Exception: error(self)
    def clear_background(self): self.bg=''; self.bglabel.setText('纯色背景')
    def reset_appearance(self):
        self.family.setCurrentFont(QFont('Microsoft YaHei')); self.code_family.setCurrentFont(QFont('Consolas')); self.size.setValue(12)
        self.theme.setCurrentText('浅色'); self.mode.setCurrentText('填充'); self.shade.setValue(80); self.clear_background()
    def backup(self):
        try:
            path=choose_save(self,'另存加密备份','ChatDesk备份 (*.cdbak)')
            if not path: return
            password=ask_password(self,'备份密码','设置独立备份密码（至少12位）：')
            if password is None: return
            confirm=ask_password(self,'备份密码','再次输入备份密码：')
            if password!=confirm: error(self,'两次密码不一致。'); return
            self.user.backup(path,password); QMessageBox.information(self,'完成','加密备份已保存。')
        except ValueError as exc: error(self,str(exc))
        except Exception: error(self)
    def import_legacy(self):
        folder=QFileDialog.getExistingDirectory(self,'选择0.1版数据目录',str(self.user.accounts.root))
        if not folder: return
        password=ask_password(self,'导入旧版','原密钥库主密码：')
        if password is None: return
        try:
            self.user.import_v1(folder,password)
            # Refresh settings snapshot so apply cannot overwrite imported profiles.
            self.values['profiles']=copy.deepcopy(self.user.state['profiles']); self.refresh_profiles()
            self.main.refresh_sessions(); QMessageBox.information(self,'完成','已导入；原文件保留在原位置。')
        except Exception: error(self,'旧版密码错误或数据无法读取，未删除原文件。')
    def apply(self):
        if not self.commit_profile(): return
        old=copy.deepcopy(self.user.state)
        try:
            if self.username.text()!=self.user.name:
                raise ValueError('请先点击“修改用户名”，或恢复原用户名。')
            if self.old.text() or self.new.text() or self.confirm.text():
                raise ValueError('请先点击“修改密码”，或清空密码栏。')
            if self.bg and self.bg not in self.user.state['attachments']:
                raise ValueError('所选背景已从附件库删除，请重新选择。')
            # Preserve live attachments/sessions changed by manager or legacy import.
            self.user.state.update(profiles=self.values['profiles'],idle_minutes=self.idle.value(),
                defaults=dict(system=self.default_prompt.toPlainText(),style=self.default_style.currentText(),**{k:w.value() for k,w in self.default_numbers.items()}),
                appearance=dict(family=self.family.currentFont().family(),code_family=self.code_family.currentFont().family(),size=self.size.value(),theme=self.theme.currentText(),background=self.bg,mode=self.mode.currentText(),shade=self.shade.value()))
            self.user.save(); self.main.apply_appearance(); self.main.refresh_profiles(); self.main.update_title(); self.accept()
        except ValueError as exc:
            self.restore_state(old); error(self,str(exc))
        except Exception:
            self.restore_state(old); error(self)

    def restore_state(self,old):
        uid=self.main.current['id'] if self.main.current else None
        self.user.state.clear(); self.user.state.update(old)
        self.main.current=next((s for s in self.user.state['sessions'] if s['id']==uid),None)


class MainWindow(QMainWindow):
    finished=Signal()
    def __init__(self,user):
        super().__init__(); self.user=user; self.current=None; self.draft=[]; self.worker=None
        self.busy=False; self.pending_lock=False; self.exit_app=False; self.last_activity=time.monotonic()
        self.resize(1180,850); self.setMinimumSize(1000,700); self.update_title()
        outer=QWidget(); self.setCentralWidget(outer); layout=QVBoxLayout(outer)
        toolbar=QHBoxLayout(); layout.addLayout(toolbar)
        button('设置',self.settings,toolbar); button('锁定 / 切换用户',self.lock,toolbar)
        button('字号 −',lambda:self.zoom(-1),toolbar); button('字号 ＋',lambda:self.zoom(1),toolbar)
        toolbar.addStretch(); self.identity=QLabel(user.name); toolbar.addWidget(self.identity)
        split=QSplitter(); layout.addWidget(split,1)
        left=QWidget(); ll=QVBoxLayout(left); split.addWidget(left)
        self.sessions=QListWidget(); ll.addWidget(self.sessions); self.sessions.currentRowChanged.connect(self.switch)
        row=QHBoxLayout(); ll.addLayout(row)
        button('新建',self.new,row); button('改名',self.rename,row); button('删除',self.delete,row)
        right=QWidget(); rl=QVBoxLayout(right); split.addWidget(right); split.setSizes([220,930])
        self.profile=QComboBox(); rl.addWidget(self.profile); self.refresh_profiles()
        self.prompt=QPlainTextEdit(); self.prompt.setMaximumHeight(80); self.prompt.setPlaceholderText('本会话背景提示词'); rl.addWidget(self.prompt)
        controls=QHBoxLayout(); rl.addLayout(controls); self.params={}
        for key,title,lo,hi in [('temperature','温度',0,2),('tokens','输出token',1,131072),('chars','期望字符',0,1000000),('rounds','历史轮数',1,500)]:
            controls.addWidget(QLabel(title)); w=QDoubleSpinBox() if key=='temperature' else QSpinBox(); w.setRange(lo,hi); controls.addWidget(w); self.params[key]=w
        self.style=QComboBox(); self.style.addItems(['严格','平衡','自由']); controls.addWidget(self.style)
        self.chat=ChatView(); rl.addWidget(self.chat,1)
        self.attachment_list=QListWidget(); self.attachment_list.setMaximumHeight(95); rl.addWidget(self.attachment_list)
        self.attachment_list.itemChanged.connect(self.toggle_attachment)
        attachrow=QHBoxLayout(); rl.addLayout(attachrow)
        button('上传附件',self.attach,attachrow); button('预览所选附件',self.preview_attachment,attachrow); button('管理附件',self.manage_attachments,attachrow)
        self.input=QPlainTextEdit(); self.input.setMaximumHeight(100); self.input.setPlaceholderText('输入消息；附件内容会发送给你选定的API服务。'); rl.addWidget(self.input)
        actions=QHBoxLayout(); rl.addLayout(actions)
        button('发送',self.send,actions); button('停止',self.stop,actions); button('保存会话设置',self.save_current,actions)
        button('另存最近回答',lambda:self.export(True),actions); button('另存完整会话',lambda:self.export(False),actions)
        self.shortcut=QShortcut(QKeySequence('Ctrl+Return'),self); self.shortcut.activated.connect(self.send)
        self.status=QLabel('就绪'); self.status.setWordWrap(True); layout.addWidget(self.status)
        self.refresh_sessions()
        if self.user.state['sessions']: self.load(self.user.state['sessions'][0])
        else: self.new()
        self.apply_appearance()
        self.timer=QTimer(self); self.timer.timeout.connect(self.check_idle); self.timer.start(1000)
        QApplication.instance().installEventFilter(self)
        self.chat.anchorClicked.connect(lambda _:QMessageBox.information(self,'链接','为保护隐私，不自动打开模型输出中的链接。'))

    def eventFilter(self,obj,event):
        if event.type() in [QEvent.Type.KeyPress,QEvent.Type.MouseButtonPress,QEvent.Type.Wheel,QEvent.Type.MouseMove]:
            self.last_activity=time.monotonic()
        return super().eventFilter(obj,event)
    def update_title(self):
        self.setWindowTitle('ChatDesk 0.2 · '+self.user.name)
        if hasattr(self,'identity'): self.identity.setText(self.user.name)
    def check_idle(self):
        if not self.pending_lock and time.monotonic()-self.last_activity>=self.user.state['idle_minutes']*60: self.lock()
    def persist(self):
        try: self.user.save(); return True
        except Exception: error(self,'指定用户目录无法写入；未回退到其他目录。'); return False
    def refresh_profiles(self):
        old=self.profile.currentText(); self.profile.clear(); self.profile.addItems([p['name'] for p in self.user.state['profiles']])
        index=self.profile.findText(old)
        if index>=0: self.profile.setCurrentIndex(index)
    def refresh_sessions(self):
        self.sessions.blockSignals(True); self.sessions.clear()
        for s in self.user.state['sessions']: self.sessions.addItem(self.user.redact(s['title']))
        self.sessions.blockSignals(False)
    def save_current(self):
        if not self.current or self.busy: return not self.busy
        self.current.update(system=self.user.redact(self.prompt.toPlainText()),profile=self.profile.currentText(),style=self.style.currentText(),draft_text=self.user.redact(self.input.toPlainText()),draft_attachments=self.draft[:],**{k:w.value() for k,w in self.params.items()})
        return self.persist()
    def load(self,session):
        self.current=session; self.draft=session.get('draft_attachments',[])[:]; self.input.setPlainText(session.get('draft_text','')); self.prompt.setPlainText(session['system'])
        for k,w in self.params.items(): w.setValue(session[k])
        self.style.setCurrentText(session['style'])
        i=self.profile.findText(session['profile']); self.profile.setCurrentIndex(max(0,i))
        self.render(); self.refresh_attachments()
    def new(self):
        if self.busy: return
        if self.current and not self.save_current(): return
        session=default_session(self.user.state['defaults']); session['profile']=self.profile.currentText()
        self.user.state['sessions'].append(session); self.persist(); self.refresh_sessions(); self.load(session)
    def switch(self,index):
        if self.busy or index<0: return
        if not self.save_current(): return
        self.load(self.user.state['sessions'][index])
    def rename(self):
        if self.busy: return
        name,ok=QInputDialog.getText(self,'会话改名','新名称：',text=self.current['title'])
        if ok and name.strip(): self.current['title']=self.user.redact(name[:100]); self.persist(); self.refresh_sessions()
    def delete(self):
        if self.busy: return
        if QMessageBox.question(self,'删除会话','确定删除当前会话？附件仍保留在本用户附件库。')!=QMessageBox.StandardButton.Yes: return
        self.user.state['sessions'].remove(self.current); self.current=None
        if self.user.state['sessions']: self.load(self.user.state['sessions'][0])
        else: self.new()
        self.persist(); self.refresh_sessions()
    def render(self):
        chunks=[]
        for m in self.current['messages']:
            title='你' if m['role']=='user' else '助手'
            if m.get('status','complete')!='complete': title+='（未完成）'
            chunks.append('## '+title+'\n\n'+self.user.redact(m['content']))
            for aid in m.get('attachments',[]): chunks.append('\n附件：'+self.user.redact(self.user.state['attachments'].get(aid,{}).get('name','已删除')))
        self.chat.setMarkdown('\n\n---\n\n'.join(chunks)); self.chat.verticalScrollBar().setValue(self.chat.verticalScrollBar().maximum())
    def refresh_attachments(self):
        self.attachment_list.blockSignals(True); self.attachment_list.clear()
        ids=list(dict.fromkeys([a for m in self.current['messages'] for a in m.get('attachments',[])]+self.draft))
        for aid in ids:
            meta=self.user.state['attachments'].get(aid)
            if not meta: continue
            name=('[待发送] ' if aid in self.draft else '')+meta['name']+' · '+('图片' if meta['kind']=='image' else str(len(meta['text']))+'字符')
            if meta.get('truncated'): name+='（已截取）'
            item=QListWidgetItem(self.user.redact(name)); item.setData(Qt.ItemDataRole.UserRole,aid)
            item.setFlags(item.flags()|Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(Qt.CheckState.Unchecked if aid in self.current.get('disabled_attachments',[]) else Qt.CheckState.Checked)
            self.attachment_list.addItem(item)
        self.attachment_list.blockSignals(False)
        self.attachment_list.setVisible(bool(ids))
    def toggle_attachment(self,item):
        if self.busy: self.refresh_attachments(); return
        aid=item.data(Qt.ItemDataRole.UserRole); disabled=self.current.setdefault('disabled_attachments',[])
        if item.checkState()==Qt.CheckState.Unchecked and aid not in disabled: disabled.append(aid)
        elif item.checkState()==Qt.CheckState.Checked and aid in disabled: disabled.remove(aid)
        self.persist()
    def attach(self):
        if self.busy: return
        names,_=QFileDialog.getOpenFileNames(self,'上传附件','','支持的附件 (*.txt *.md *.csv *.pdf *.docx *.png *.jpg *.jpeg *.webp)')
        for name in names:
            try: self.draft.append(self.user.add_attachment(name))
            except ValueError as exc: error(self,str(exc))
            except Exception: error(self,'附件解析或加密保存失败；未记录原始错误。')
        self.refresh_attachments()
    def preview_attachment(self):
        item=self.attachment_list.currentItem()
        if not item: return
        self.preview(item.data(Qt.ItemDataRole.UserRole))
    def preview(self,aid):
        try:
            meta=self.user.state['attachments'][aid]; dialog=QDialog(self); dialog.setWindowTitle('附件预览'); dialog.resize(680,500); layout=QVBoxLayout(dialog)
            title=QLabel(self.user.redact(meta['name'])+' · '+str(meta['size'])+' bytes'); layout.addWidget(title)
            if meta['kind']=='image':
                picture=QPixmap(); picture.loadFromData(self.user.read_blob(aid)); view=QLabel(); view.setPixmap(picture.scaled(640,400,Qt.AspectRatioMode.KeepAspectRatio)); layout.addWidget(view)
            else:
                view=QPlainTextEdit(self.user.redact(meta['text'])); view.setReadOnly(True); layout.addWidget(view)
            dialog.exec()
        except Exception: error(self)
    def manage_attachments(self):
        if self.busy: return
        dialog=QDialog(self); dialog.setWindowTitle('本用户附件库'); dialog.resize(650,430); layout=QVBoxLayout(dialog); items=QListWidget(); layout.addWidget(items)
        def refresh():
            items.clear()
            for aid,meta in self.user.state['attachments'].items():
                item=QListWidgetItem(self.user.redact(meta['name'])+' · '+str(meta['size'])+' bytes'); item.setData(Qt.ItemDataRole.UserRole,aid); items.addItem(item)
        def chosen(): return items.currentItem().data(Qt.ItemDataRole.UserRole) if items.currentItem() else None
        def add():
            aid=chosen()
            if aid and aid not in self.draft: self.draft.append(aid); self.refresh_attachments()
        def delete():
            aid=chosen()
            if not aid: return
            if QMessageBox.question(dialog,'删除附件','删除加密原文件并移除所有会话中的引用？')!=QMessageBox.StandardButton.Yes: return
            try:
                self.user.delete_attachment(aid); self.draft=[a for a in self.draft if a!=aid]; refresh(); self.refresh_attachments(); self.render()
            except ValueError as exc: error(dialog,str(exc))
            except Exception: error(dialog)
        refresh(); row=QHBoxLayout(); layout.addLayout(row)
        button('预览',lambda:self.preview(chosen()) if chosen() else None,row); button('加入当前输入',add,row); button('删除',delete,row)
        dialog.exec()
    def settings(self):
        if self.busy: return
        if self.save_current(): Settings(self).exec()
    def apply_appearance(self):
        a=self.user.state['appearance']; dark=a['theme']=='深色'
        self.chat.setFont(QFont(a['family'],a['size'])); self.input.setFont(QFont(a['family'],a['size'])); self.prompt.setFont(QFont(a['family'],a['size']))
        self.chat.document().setDefaultStyleSheet('code, pre {font-family: "%s";}'%a['code_family'].replace('"',''))
        base='QPushButton {padding: 6px 10px;} QLineEdit,QComboBox,QSpinBox,QDoubleSpinBox {padding: 3px;} QPlainTextEdit,QListWidget {border: 1px solid #b8c4d2; border-radius: 4px;}'
        self.setStyleSheet(base+('QWidget { background: #17202b; color: #e6edf3; } QLineEdit,QPlainTextEdit,QListWidget,QComboBox,QSpinBox,QDoubleSpinBox { background: #253244; }' if dark else 'QWidget {background: #f3f6fa; color: #172538;} QLineEdit,QPlainTextEdit,QListWidget,QComboBox,QSpinBox,QDoubleSpinBox {background: white;}'))
        self.chat.picture=QPixmap(); self.chat.dark=dark; self.chat.mode=a['mode']; self.chat.shade=a['shade']
        if a['background']:
            try: self.chat.picture.loadFromData(self.user.read_blob(a['background']))
            except Exception: error(self,'背景图片无法读取，使用纯色背景。')
        self.chat.setStyleSheet('QTextBrowser {background: transparent; border: 1px solid #b8c4d2; border-radius: 4px;}' if not self.chat.picture.isNull() else 'QTextBrowser {background: '+('#17202b' if dark else '#ffffff')+'; border: 1px solid #b8c4d2; border-radius: 4px;}')
        self.chat.viewport().update()
        if self.current: self.render()
    def zoom(self,delta):
        a=self.user.state['appearance']; a['size']=min(40,max(8,a['size']+delta)); self.persist(); self.apply_appearance()
    def send(self):
        if self.busy or not self.save_current(): return
        text=self.user.redact(self.input.toPlainText().strip())
        enabled=[aid for aid in self.draft if aid not in self.current.get('disabled_attachments',[])]
        if not text and not enabled: return
        profile=next((p for p in self.user.state['profiles'] if p['name']==self.profile.currentText()),None)
        if not profile or not profile['key']: error(self,'请在设置中保存API密钥。'); return
        message=dict(role='user',content=text,attachments=enabled)
        snapshot=copy.deepcopy(self.current); snapshot['messages'].append(message)
        try:
            messages=messages_for(snapshot,self.user.state['attachments'],self.user.read_blob,profile['vision'])
            tokens=estimate(messages)
            text_info='本次发送：%d条消息（含背景），估算约%d输入token；输出上限%d。\n近期轮数之外的历史不会发送，已勾选的附件内容可能发往所选服务。\n估算不是模型实际计费值，图片估算误差较大。'%(len(messages),tokens,self.current['tokens'])
            if QMessageBox.question(self,'发送前确认',text_info)!=QMessageBox.StandardButton.Yes: return
        except ValueError as exc: error(self,str(exc)); return
        except Exception: error(self,'准备附件失败，未发送请求。'); return
        self.current['messages'].append(message)
        old_draft=(self.current.get('draft_text',''),self.current.get('draft_attachments',[])[:])
        self.current['draft_text']=''; self.current['draft_attachments']=[]
        if not self.persist():
            self.current['messages'].pop(); self.current['draft_text'],self.current['draft_attachments']=old_draft
            return
        self.current['messages'].append(dict(role='assistant',content='',status='incomplete'))
        self.input.clear(); self.draft=[]; self.busy=True; self.render(); self.refresh_attachments()
        self.worker=Request(copy.deepcopy(profile),messages,{k:self.current[k] for k in ['temperature','tokens']})
        self.worker.chunk.connect(self.on_chunk); self.worker.result.connect(self.on_result); self.worker.finished.connect(self.worker_finished)
        self.worker.start(); self.status.setText('生成中 · 停止最迟约30秒生效。')
    def on_chunk(self,text):
        self.current['messages'][-1]['content']+=text
        if not self.pending_lock: self.render()
    def on_result(self,result):
        message=self.current['messages'][-1]; message['content']=self.user.redact(message['content'])
        message['status']='complete' if result=='done' and message['content'] else 'incomplete'
        self.persist()
        self.status.setText(('已完成' if result=='done' else '已停止' if result=='stopped' else result)+' · 回答字符数：'+str(len(message['content'])))
        if not self.pending_lock: self.render()
    def worker_finished(self):
        self.busy=False; self.worker.deleteLater(); self.worker=None
        if self.pending_lock: self.close()
    def stop(self):
        if self.worker: self.worker.stop.set(); self.status.setText('正在停止，等待网络读取结束（最长约30秒）。')
    def export(self,answer_only):
        if self.busy or not self.save_current(): return
        include=False if answer_only else QMessageBox.question(self,'导出选项','是否包含背景提示词？')==QMessageBox.StandardButton.Yes
        try:
            path=choose_save(self,'另存明文对话','Markdown (*.md);;文本 (*.txt)')
            if not path: return
            if path.suffix.lower() not in ['.md','.txt']: raise ValueError('请选择TXT或MD文件。')
            content=export_text(self.current,self.user.state['attachments'],answer_only,path.suffix.lower()=='.md',include)
            atomic_write(path,self.user.redact(content).encode('utf-8')); self.status.setText('已另存为：'+str(path))
        except ValueError as exc: error(self,str(exc))
        except Exception: error(self)
    def lock(self):
        if self.pending_lock: return
        if not self.busy and not self.save_current(): return
        self.pending_lock=True
        for dialog in self.findChildren(QDialog): dialog.reject()
        for edit in self.findChildren(QLineEdit): edit.clear()
        self.chat.clear(); self.input.clear(); self.prompt.clear(); self.chat.picture=QPixmap()
        self.centralWidget().hide()
        if self.busy: self.stop()
        else: self.close()
    def closeEvent(self,event):
        if self.busy:
            self.stop(); self.status.setText('请等待停止后再关闭。'); event.ignore(); return
        if not self.pending_lock:
            if not self.save_current(): event.ignore(); return
            self.exit_app=True
        self.timer.stop(); QApplication.instance().removeEventFilter(self)
        for dialog in self.findChildren(QDialog): dialog.reject()
        for edit in self.findChildren(QLineEdit): edit.clear()
        self.chat.clear(); self.input.clear(); self.prompt.clear(); self.chat.picture=QPixmap(); self.current=None; self.draft=[]
        self.user.close(); self.finished.emit(); event.accept()


def main():
    try:
        program=allowed_path(Path(sys.executable).parent if getattr(sys,'frozen',False) else Path(__file__).parent)
    except ValueError:
        if os.name=='nt':
            import ctypes
            ctypes.windll.user32.MessageBoxW(None,'请将整个软件文件夹移至非系统盘后启动。','ChatDesk',0)
        return
    runtime=allowed_path(program/'runtime'); runtime.mkdir(exist_ok=True)
    for key in ['TEMP','TMP','TMPDIR','XDG_CACHE_HOME','XDG_CONFIG_HOME','XDG_DATA_HOME']: os.environ[key]=str(runtime)
    import tempfile
    tempfile.tempdir=str(runtime)
    app=QApplication(sys.argv); app.setQuitOnLastWindowClosed(False); app.setApplicationName('ChatDesk')
    if '--self-test' in sys.argv:
        # Frozen executable smoke test: no API calls, no user data, no file chooser.
        import tempfile
        import pypdf, docx, PIL.Image
        with tempfile.TemporaryDirectory(dir=runtime) as test_root:
            test_accounts=Accounts(test_root)
            test_user=test_accounts.create('Build verification','build verification password')
            test_uid=test_user.uid
            window=MainWindow(test_user); window.show(); app.processEvents()
            assert not window.grab().isNull()
            window.close(); app.processEvents()
            reopened=test_accounts.unlock(test_uid,'build verification password'); reopened.close()
        return
    # We never instantiate QSettings, QtWebEngine or a telemetry/crash reporting SDK.
    def handle_exception(*_): error(app.activeWindow(),'发生内部错误，未保存异常堆栈或请求内容。')
    sys.excepthook=handle_exception
    folder=QFileDialog.getExistingDirectory(None,'选择非系统盘数据根目录',str(program))
    if not folder: return
    try: accounts=Accounts(folder)
    except Exception: error(None,'请选择可写入的非系统盘本地目录。'); return
    lock=QLockFile(str(accounts.root/'application.lock')); lock.setStaleLockTime(0)
    if not lock.tryLock(0): error(None,'此数据目录正在被另一个实例使用；请关闭旧实例后重试。'); return
    try:
        while True:
            login=Login(accounts)
            if login.exec()!=QDialog.DialogCode.Accepted: break
            user=login.user; login.user=None; login.password.clear(); login.deleteLater()
            window=MainWindow(user); loop=QEventLoop(); window.finished.connect(loop.quit); window.show(); loop.exec()
            exit_app=window.exit_app; window.deleteLater(); app.processEvents()
            if exit_app: break
    finally: lock.unlock()


if __name__=='__main__':
    try: main()
    except BaseException:
        if '--self-test' in sys.argv: sys.exit(1)
