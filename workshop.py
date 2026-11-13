"""Writing/discussion workspace with explicit approval required for publication."""
import copy
import difflib
import html
import json
import re
import time
from datetime import datetime
from PySide6.QtCore import Qt,QTimer,QUrl
from PySide6.QtGui import QFont,QTextCursor,QTextDocument,QKeySequence,QShortcut,QPixmap,QCursor
from PySide6.QtWidgets import (QMainWindow,QApplication,QWidget,QVBoxLayout,QHBoxLayout,QGridLayout,QFormLayout,
    QSplitter,QLabel,QPushButton,QToolButton,QComboBox,QLineEdit,QPlainTextEdit,QListWidget,QListWidgetItem,
    QTreeWidget,QTreeWidgetItem,QTabWidget,QStackedWidget,QDialog,QDialogButtonBox,QMessageBox,QInputDialog,
    QSpinBox,QDoubleSpinBox,QCheckBox,QFileDialog,QMenu,QSizePolicy,QPlainTextDocumentLayout)
from app import MainWindow,ChatView,Request,Settings,button,error,choose_save
from core import atomic_write,messages_for,estimate,export_text
from storage import default_session
from novel import project,chapter,uid,checkpoint,restore,context_sources,assemble,exclude_message,remove_message,undo_remove,branch,candidate_branch
from drafting import FIELDS,PROMPTS,make_task,task_input,qt_index,find_book,find_document,check_target,apply_proposal
from workspace_dialogs import CandidateDialog

CHAPTER_FIELDS=FIELDS['chapter']
BOOK_FIELDS=FIELDS['book']
LORE_FIELDS=FIELDS['lore']


class ContextDialog(QDialog):
    def __init__(self,parent,entries,budget,selected=None):
        super().__init__(parent); self.entries=entries; self.bundle=None; self.manual=selected is not None
        self.setWindowTitle('本次上下文 · 仅发送已勾选资料'); self.resize(min(1000,max(600,parent.width()-40)),min(700,max(420,parent.height()-40)))
        layout=QVBoxLayout(self); top=QHBoxLayout(); layout.addLayout(top)
        top.addWidget(QLabel('资料字符预算（不含会话历史与附件）'))
        self.budget=QSpinBox(); self.budget.setRange(2000,2000000); self.budget.setValue(budget); top.addWidget(self.budget)
        button('自动选取相关资料',self.auto,top)
        split=QSplitter(); layout.addWidget(split,1); self.items=QListWidget(); split.addWidget(self.items)
        self.preview=QPlainTextEdit(); self.preview.setReadOnly(True); split.addWidget(self.preview); split.setSizes([360,640])
        self.note=QLabel(); self.note.setWordWrap(True); layout.addWidget(self.note)
        buttons=QDialogButtonBox(QDialogButtonBox.StandardButton.Ok|QDialogButtonBox.StandardButton.Cancel)
        layout.addWidget(buttons); buttons.accepted.connect(self.accept); buttons.rejected.connect(self.reject)
        if selected is None:
            try: selected=assemble(entries,budget)['selected']
            except ValueError: selected={e['id'] for e in entries if e['required']}
        for e in entries:
            item=QListWidgetItem(e['label']+' · '+str(len(e['text']))+'字\n'+e['reason']); item.setData(Qt.ItemDataRole.UserRole,e['id'])
            item.setFlags(item.flags()|Qt.ItemFlag.ItemIsUserCheckable)
            if e['required']: item.setFlags(item.flags()&~Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(Qt.CheckState.Checked if e['required'] or e['id'] in selected else Qt.CheckState.Unchecked)
            self.items.addItem(item)
        self.items.itemChanged.connect(self.selection_changed); self.budget.valueChanged.connect(self.rebuild); self.rebuild()
    def selection_changed(self,*_):
        self.manual=True; self.rebuild()
    def selection(self): return {self.items.item(i).data(Qt.ItemDataRole.UserRole) for i in range(self.items.count()) if self.items.item(i).checkState()==Qt.CheckState.Checked}
    def rebuild(self,*_):
        try:
            self.bundle=assemble(self.entries,self.budget.value(),self.selection()); self.preview.setPlainText(self.bundle['text'])
            self.note.setText('已选择%d项 · %d字符 · 未选择%d项。列表中的复选框表示实际发送范围，预算仅限制作品资料。'%(len(self.bundle['selected']),self.bundle['characters'],len(self.bundle['omitted'])))
        except ValueError as exc: self.bundle=None; self.preview.clear(); self.note.setText(str(exc))
    def auto(self):
        self.manual=False
        try: selected=assemble(self.entries,self.budget.value())['selected']
        except ValueError as exc: error(self,str(exc)); return
        self.items.blockSignals(True)
        for i in range(self.items.count()):
            item=self.items.item(i); item.setCheckState(Qt.CheckState.Checked if item.data(Qt.ItemDataRole.UserRole) in selected else Qt.CheckState.Unchecked)
        self.items.blockSignals(False); self.rebuild()
    def accept(self):
        if self.bundle: super().accept()
        else: error(self,'请先调整资料选择或字符预算。')
    def done(self,result):
        self.preview.clear()
        if result!=QDialog.DialogCode.Accepted: self.entries=[]; self.bundle=None
        super().done(result)


class NovelWindow(MainWindow):
    def __init__(self,user):
        QMainWindow.__init__(self)
        self.user=user; self.current=None; self.book=None; self.chapter=None; self.document=None; self.doc_kind=None
        self.ready=False; self.busy=False; self.worker=None; self.run=None; self.pending_lock=False; self.exit_app=False; self.close_requested=False
        self.draft=[]; self.pending_task=None; self.selected_index=None; self.last_activity=time.monotonic()
        self.doc_cache={}; self.html_cache={}; self.shown_sessions=[]; self.showing_document=False; self.focused=False
        self.ui=user.state.setdefault('workspace',dict(mode='write',navigation=True,assistant=True)); self.last_result=self.ui.get('last_result')
        self.render_timer=QTimer(self); self.render_timer.setSingleShot(True); self.render_timer.timeout.connect(self.render)
        self.autosave=QTimer(self); self.autosave.setSingleShot(True); self.autosave.timeout.connect(self.autosave_document)
        self.context_timer=QTimer(self); self.context_timer.setSingleShot(True); self.context_timer.timeout.connect(self.update_context_label)
        # Familiar API configuration widgets are reused in the parameter dialog only.
        self.prompt=QPlainTextEdit(self); self.prompt.hide(); self.params={}
        for key,lo,hi in [('temperature',0,2),('tokens',1,131072),('chars',0,1000000),('rounds',1,500)]:
            w=QDoubleSpinBox(self) if key=='temperature' else QSpinBox(self); w.setRange(lo,hi); w.hide(); self.params[key]=w
        self.style=QComboBox(self); self.style.addItems(['严格','平衡','自由']); self.style.hide()
        self.profile=QComboBox(self); self.profile.hide(); self.refresh_profiles()
        self.profile.currentIndexChanged.connect(self.profile_changed)
        self.attachment_list=QListWidget(self); self.attachment_list.hide(); self.attachment_list.itemChanged.connect(self.toggle_attachment)
        root=QWidget(); root.setObjectName('workbench'); self.setCentralWidget(root)
        layout=QVBoxLayout(root); layout.setContentsMargins(12,10,12,8); layout.setSpacing(8)
        top=QHBoxLayout(); top.setSpacing(8); layout.addLayout(top)
        self.nav_button=button('☰',self.toggle_navigation,top); self.nav_button.setToolTip('收起或展开作品导航')
        brand=QLabel('墨舟'); brand.setObjectName('brand'); top.addWidget(brand)
        self.books=QComboBox(); self.books.setMinimumContentsLength(10); self.books.setMaximumWidth(230)
        self.books.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        top.addWidget(self.books); self.books.currentIndexChanged.connect(self.switch_book)
        self.book_menu=QToolButton(); self.book_menu.setText('作品 ▾'); self.book_menu.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        menu=QMenu(self.book_menu)
        for text,fn in [('新建作品',self.create_book),('导出正文',self.export_manuscript),('数据与备份',self.settings)]: menu.addAction(text,fn)
        menu.addSeparator(); menu.addAction('删除作品',self.delete_book); self.book_menu.setMenu(menu); top.addWidget(self.book_menu)
        top.addStretch()
        self.write_mode=button('写作',lambda:self.set_mode('write'),top); self.write_mode.setCheckable(True)
        self.discuss_mode=button('AI讨论',lambda:self.set_mode('discuss'),top); self.discuss_mode.setCheckable(True)
        self.focus_button=button('专注',self.focus_mode,top)
        button('设置',self.settings,top); button('锁定',self.lock,top)
        self.identity=QLabel(user.name); self.identity.setTextFormat(Qt.TextFormat.PlainText); self.identity.setObjectName('muted'); self.identity.setMaximumWidth(100); self.identity.setMinimumWidth(0); self.identity.setSizePolicy(QSizePolicy.Policy.Ignored,QSizePolicy.Policy.Preferred); top.addWidget(self.identity)
        self.workspace=QSplitter(); self.workspace.setChildrenCollapsible(False); layout.addWidget(self.workspace,1)
        self.navigation=QWidget(); self.navigation.setObjectName('sidebar'); nl=QVBoxLayout(self.navigation); nl.setContentsMargins(8,12,8,8); nl.setSpacing(8)
        self.nav=QTreeWidget(); self.nav.setHeaderHidden(True); self.nav.setMinimumWidth(145); self.nav.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.nav.itemSelectionChanged.connect(self.select_nav); self.nav.customContextMenuRequested.connect(self.navigation_menu); nl.addWidget(self.nav,1)
        add=QToolButton(); add.setText('＋ 新增'); add.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup); addmenu=QMenu(add)
        addmenu.addAction('章节',self.add_chapter); addmenu.addAction('世界观 / 背景',lambda:self.add_lore('世界观')); addmenu.addAction('人物',lambda:self.add_lore('人物'))
        addmenu.addAction('其他资料',lambda:self.add_lore()); add.setMenu(addmenu); nl.addWidget(add)
        self.workspace.addWidget(self.navigation)
        self.content_split=QSplitter(); self.content_split.setChildrenCollapsible(False); self.workspace.addWidget(self.content_split)
        self.paper=QWidget(); self.paper.setObjectName('paper'); el=QVBoxLayout(self.paper); el.setContentsMargins(16,12,16,10); el.setSpacing(8)
        self.breadcrumb=QLabel(); self.breadcrumb.setTextFormat(Qt.TextFormat.PlainText); self.breadcrumb.setObjectName('muted'); self.breadcrumb.setWordWrap(True); el.addWidget(self.breadcrumb)
        self.doc_title=QLineEdit(); self.doc_title.setObjectName('documentTitle'); self.doc_title.setPlaceholderText('文档标题'); el.addWidget(self.doc_title)
        meta=QHBoxLayout(); el.addLayout(meta)
        self.volume=QLineEdit(); self.volume.setPlaceholderText('卷名'); meta.addWidget(self.volume)
        self.pinned=QCheckBox('固定加入上下文'); meta.addWidget(self.pinned)
        actions=QHBoxLayout(); el.addLayout(actions)
        self.primary_ai=button('AI构思',self.primary_task,actions); self.primary_ai.setObjectName('primary')
        button('直接编写',self.manual_edit,actions); button('与AI讨论',self.discuss_document,actions)
        more=QToolButton(); more.setText('更多 ▾'); more.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup); actions.addWidget(more)
        self.document_menu=QMenu(more); more.setMenu(self.document_menu)
        self.editor_stack=QStackedWidget(); el.addWidget(self.editor_stack,1)
        self.editor_tabs=QTabWidget(); self.editor_stack.addWidget(self.editor_tabs)
        self.editor=QPlainTextEdit(); self.plan_editor=QPlainTextEdit(); self.summary_editor=QPlainTextEdit()
        self.editor_tabs.addTab(self.editor,'正文'); self.editor_tabs.addTab(self.plan_editor,'章节计划'); self.editor_tabs.addTab(self.summary_editor,'确认摘要')
        self.editor_tabs.currentChanged.connect(self.editor_tab_changed)
        self.collection_page=QWidget(); self.collection_page.setObjectName('collection'); cl=QVBoxLayout(self.collection_page); cl.setContentsMargins(10,18,10,12)
        self.collection_title=QLabel(); self.collection_title.setObjectName('emptyTitle'); cl.addWidget(self.collection_title)
        self.collection_note=QLabel(); self.collection_note.setWordWrap(True); cl.addWidget(self.collection_note)
        self.collection_items=QListWidget(); self.collection_items.itemDoubleClicked.connect(self.open_collection_item); cl.addWidget(self.collection_items,1)
        self.editor_stack.addWidget(self.collection_page)
        self.selection_tools=QWidget(); sr=QHBoxLayout(self.selection_tools); sr.setContentsMargins(0,0,0,0)
        for name in ['润色选段','扩写选段','压缩选段']: button('AI '+name,lambda checked=False,n=name:self.task(n),sr)
        sr.addStretch(); el.addWidget(self.selection_tools); self.selection_tools.hide()
        self.count=QLabel(); self.count.setObjectName('muted'); el.addWidget(self.count)
        self.content_split.addWidget(self.paper)
        self.assistant=QWidget(); self.assistant.setObjectName('assistant'); al=QVBoxLayout(self.assistant); al.setContentsMargins(10,10,10,8); al.setSpacing(6)
        head=QHBoxLayout(); al.addLayout(head)
        self.discussions=QComboBox(); self.discussions.setMinimumContentsLength(8); self.discussions.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.discussions.currentIndexChanged.connect(self.switch); head.addWidget(self.discussions,1)
        self.new_discussion=button('＋',self.new,head); self.new_discussion.setToolTip('新建讨论（不改变正在编辑的文档）')
        chatmore=QToolButton(); chatmore.setText('⋯'); chatmore.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        cm=QMenu(chatmore)
        for text,fn in [('重命名讨论',self.rename),('参数与背景提示词',self.chat_parameters),('导出讨论',lambda:self.export(False)),('导出最近回答',lambda:self.export(True)),('撤销删除消息',self.undo_reply)]: cm.addAction(text,fn)
        cm.addSeparator(); cm.addAction('删除讨论',self.delete); chatmore.setMenu(cm); head.addWidget(chatmore)
        reference=QHBoxLayout(); al.addLayout(reference)
        self.reference=QComboBox(); self.reference.currentIndexChanged.connect(self.reference_changed); reference.addWidget(self.reference,1)
        self.use_current=button('引用当前章',self.reference_current,reference)
        self.context_button=button('本次引用 · 查看',self.preview_context,al); self.context_button.setObjectName('contextLink')
        self.chat=ChatView(); self.chat.setMinimumHeight(230); self.chat.anchorClicked.connect(self.message_action); al.addWidget(self.chat,1)
        self.task_row=QWidget(); tr=QHBoxLayout(self.task_row); tr.setContentsMargins(0,0,0,0)
        self.task_label=QLabel(); self.task_label.setTextFormat(Qt.TextFormat.PlainText); self.task_label.setWordWrap(True); tr.addWidget(self.task_label,1); button('取消任务',self.clear_task,tr); al.addWidget(self.task_row)
        self.input=QPlainTextEdit(); self.input.setFixedHeight(82); self.input.setPlaceholderText('讨论创意、背景或剧情……'); al.addWidget(self.input)
        bottom=QHBoxLayout(); al.addLayout(bottom)
        self.attach_button=QToolButton(); self.attach_button.setText('＋ 附件'); self.attach_button.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        am=QMenu(self.attach_button); am.addAction('上传文件',self.attach); am.addAction('本讨论的附件选择',self.choose_attachments); am.addAction('附件库',self.manage_attachments); self.attach_button.setMenu(am); bottom.addWidget(self.attach_button)
        self.parameter_button=button('模型 / 参数',self.chat_parameters,bottom); bottom.addStretch()
        self.stop_button=button('停止',self.stop,bottom); self.send_button=button('发送',self.send,bottom); self.send_button.setObjectName('primary'); self.send_button.setToolTip('Ctrl+Enter 发送；AI任务可以不填写补充要求')
        self.content_split.addWidget(self.assistant)
        self.workspace.setStretchFactor(0,0); self.workspace.setStretchFactor(1,1); self.workspace.setSizes([180,1100]); self.content_split.setSizes([620,420])
        footer=QHBoxLayout(); layout.addLayout(footer)
        self.status=QLabel('就绪'); self.status.setTextFormat(Qt.TextFormat.PlainText); self.status.setObjectName('muted'); self.status.setWordWrap(True); footer.addWidget(self.status,1)
        self.run_button=button('查看生成',self.open_run,footer); self.run_button.hide()
        self.resize(1360,820); self.setMinimumSize(900,560)
        for edit in [self.editor,self.plan_editor,self.summary_editor]: edit.textChanged.connect(self.schedule_save); edit.selectionChanged.connect(self.update_selection_actions)
        self.doc_title.textChanged.connect(self.schedule_save); self.volume.textChanged.connect(self.schedule_save); self.pinned.toggled.connect(self.schedule_save)
        self.input.textChanged.connect(self.schedule_context)
        self.shortcut=QShortcut(QKeySequence('Ctrl+Return'),self); self.shortcut.activated.connect(self.send)
        self.save_shortcut=QShortcut(QKeySequence('Ctrl+S'),self); self.save_shortcut.activated.connect(self.save_draft)
        self.timer=QTimer(self); self.timer.timeout.connect(self.check_idle); self.timer.start(1000); QApplication.instance().installEventFilter(self)
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.DefaultContextMenu)
        self.editor.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu); self.editor.customContextMenuRequested.connect(self.selection_menu)
        if not user.state['projects']: user.state['projects'].append(project('我的第一部小说'))
        self.ready=True; self.refresh_books()
        last=self.ui.get('book_id'); index=self.books.findData(last); self.switch_book(index if index>=0 else 0)
        self.set_mode(self.ui.get('mode','write'),persist=False); self.apply_appearance(); self.update_title(); self.busy_controls()

    def update_title(self):
        self.setWindowTitle('墨舟 Novel Studio 0.4.0 · 验证版 · '+self.user.name)
        if hasattr(self,'identity'):
            self.identity.setText(self.identity.fontMetrics().elidedText(self.user.name,Qt.TextElideMode.ElideRight,100)); self.identity.setToolTip(self.user.name)

    def refresh_profiles(self):
        MainWindow.refresh_profiles(self)
        if getattr(self,'ready',False): self.profile_changed()

    def profile_changed(self,*_):
        if not self.ready: return
        full=(self.profile.currentText() or '选择模型')+' ▾'
        self.parameter_button.setText(self.parameter_button.fontMetrics().elidedText(full,Qt.TextElideMode.ElideRight,145)); self.parameter_button.setToolTip(full)
        self.schedule_context()

    def set_mode(self,mode,persist=True):
        mode='discuss' if mode=='discuss' else 'write'
        if self.ui.get('mode')=='write' and all(v>0 for v in self.content_split.sizes()): self.ui['split']=self.content_split.sizes()
        self.ui['mode']=mode; self.focused=False
        self.paper.setVisible(mode=='write'); self.assistant.setVisible(True)
        if mode=='write': self.content_split.setSizes(self.ui.get('split',[620,420]))
        self.write_mode.setChecked(mode=='write'); self.discuss_mode.setChecked(mode=='discuss')
        self.navigation.setVisible(self.ui.get('navigation',True)); self.focus_button.setText('专注')
        if persist and self.ready: self.remember_view(); self.persist()

    def toggle_navigation(self):
        self.ui['navigation']=not self.navigation.isVisible(); self.navigation.setVisible(self.ui['navigation']); self.persist()

    def focus_mode(self):
        self.focused=not self.focused
        if self.focused:
            self.paper.show(); self.assistant.hide(); self.navigation.hide(); self.focus_button.setText('退出专注')
        else: self.set_mode(self.ui.get('mode','write'),persist=False)

    def refresh_books(self):
        active=self.book['id'] if self.book else None; self.books.blockSignals(True); self.books.clear()
        for p in self.user.state['projects']: self.books.addItem(self.user.redact(p['title']),p['id'])
        if any(not s.get('project_id') for s in self.user.state['sessions']): self.books.addItem('未归档对话',None)
        index=self.books.findData(active)
        if index>=0: self.books.setCurrentIndex(index)
        self.books.blockSignals(False)

    def switch_book(self,index):
        if not self.ready or index<0: return
        if not self.save_current(): return
        identity=self.books.itemData(index); self.book=next((b for b in self.user.state['projects'] if b['id']==identity),None)
        self.chapter=None; self.document=None; self.doc_kind=None; self.current=None; self.pending_task=None
        self.books.blockSignals(True); self.books.setCurrentIndex(index); self.books.blockSignals(False)
        self.ui['book_id']=identity; self.refresh_nav(); view=self.book.get('view',{}) if self.book else {}
        doc=None; kind=view.get('kind','book'); did=view.get('doc_id')
        if self.book:
            if kind=='book': doc=self.book
            elif kind in ['chapter','lore']: doc=next((d for d in self.book['chapters' if kind=='chapter' else 'lore'] if d['id']==did),None)
            if kind=='collection': self.show_collection(view.get('collection','世界观'))
            elif doc: self.show_document(kind,doc,view.get('tab',1 if kind=='book' else 0))
            else: self.show_document('book',self.book,1)
        else: self.show_collection('未归档对话')
        self.refresh_sessions(); wanted=view.get('discussion_id'); chosen=next((s for s in self.shown_sessions if s['id']==wanted),None)
        if chosen or self.shown_sessions: self.load(chosen or self.shown_sessions[0])
        else: self.new()
        self.persist()

    def refresh_sessions(self):
        if not self.ready: return
        identity=self.book['id'] if self.book else None
        self.shown_sessions=[s for s in self.user.state['sessions'] if s.get('project_id')==identity]
        self.discussions.blockSignals(True); self.discussions.clear()
        for s in self.shown_sessions:
            active=' · 生成中' if self.run and self.run['session']['id']==s['id'] else ''
            self.discussions.addItem(self.user.redact(s['title'])+active,s['id'])
        index=self.discussions.findData(self.current['id']) if self.current else -1
        self.discussions.setCurrentIndex(index); self.discussions.blockSignals(False)

    def switch(self,index):
        if not self.ready or index<0 or index>=len(self.shown_sessions): return
        if self.save_current(): self.load(self.shown_sessions[index])

    def load(self,session):
        self.current=session; self.current.pop('context_bundle',None); self.draft=session.get('draft_attachments',[])[:]
        self.pending_task=copy.deepcopy(session.get('draft_task')); self.selected_index=None
        self.input.blockSignals(True); self.input.setPlainText(session.get('draft_text','')); self.input.blockSignals(False)
        self.prompt.setPlainText(session['system'])
        for key,w in self.params.items(): w.setValue(session[key])
        self.style.setCurrentText(session['style']); index=self.profile.findText(session['profile']); self.profile.setCurrentIndex(max(0,index))
        self.refresh_reference(); self.refresh_sessions(); self.refresh_attachments(); self.profile_changed(); self.update_task_hint(); self.render(); self.update_context_label(); self.busy_controls()
        view=session.get('view',{}); cursor=self.input.textCursor(); cursor.setPosition(min(view.get('input_cursor',0),self.input.document().characterCount()-1)); self.input.setTextCursor(cursor)
        if 'scroll' in view: QTimer.singleShot(0,lambda s=session,v=view['scroll']:self.restore_chat_scroll(s['id'],v))

    def restore_chat_scroll(self,identity,value):
        if self.current and self.current['id']==identity: self.chat.verticalScrollBar().setValue(value)

    def save_current(self):
        if not self.ready: return True
        self.capture_document(); self.remember_view()
        if self.current:
            self.current.update(system=self.user.redact(self.prompt.toPlainText()),profile=self.profile.currentText(),style=self.style.currentText(),
                draft_text=self.user.redact(self.input.toPlainText()),draft_attachments=self.draft[:],**{k:w.value() for k,w in self.params.items()})
            if self.pending_task: self.current['draft_task']=copy.deepcopy(self.pending_task)
            else: self.current.pop('draft_task',None)
            self.current['view']=dict(input_cursor=self.input.textCursor().position(),scroll=self.chat.verticalScrollBar().value())
        return self.persist()

    def remember_view(self):
        if not self.book: return
        self.save_document_position()
        self.book['view']=dict(kind=self.doc_kind,doc_id=self.document['id'] if self.document else None,
            collection=getattr(self,'collection_kind','世界观'),tab=self.editor_tabs.currentIndex(),discussion_id=self.current['id'] if self.current else None)
        sizes=self.content_split.sizes()
        if self.ui.get('mode')=='write' and all(v>0 for v in sizes): self.ui['split']=sizes

    def new(self):
        if not self.ready or not self.save_current(): return
        s=default_session(self.user.state['defaults']); s.update(project_id=self.book['id'] if self.book else None,reference_chapter_id=None)
        s['profile']=self.profile.currentText(); s['title']='新讨论'
        self.user.state['sessions'].append(s); self.load(s); self.persist()

    def rename(self):
        if not self.current: return
        name,ok=QInputDialog.getText(self,'重命名讨论','讨论名称：',text=self.current['title'])
        if ok and name.strip(): self.current['title']=self.user.redact(name[:100]); self.persist(); self.refresh_sessions()

    def delete(self):
        if not self.current: return
        if self.run and self.run['session']['id']==self.current['id']: error(self,'请先停止这份讨论的生成。'); return
        if QMessageBox.question(self,'删除讨论','删除这份讨论？正文、设定与章节不会改变。')!=QMessageBox.StandardButton.Yes: return
        self.user.state['sessions'].remove(self.current); self.current=None; self.refresh_sessions()
        if self.shown_sessions: self.load(self.shown_sessions[0])
        else: self.new()
        self.persist()

    def refresh_reference(self):
        self.reference.blockSignals(True); self.reference.clear(); self.reference.addItem('仅引用作品资料',None)
        if self.book:
            for c in self.book['chapters']: self.reference.addItem('引用：'+c['title'],c['id'])
        identity=self.current.get('reference_chapter_id',self.current.get('chapter_id')) if self.current else None
        index=self.reference.findData(identity); self.reference.setCurrentIndex(max(0,index)); self.reference.blockSignals(False)
        self.use_current.setEnabled(self.doc_kind=='chapter')

    def reference_changed(self,*_):
        if not self.ready or not self.current: return
        self.current['reference_chapter_id']=self.reference.currentData(); self.schedule_context(); self.persist()

    def reference_current(self):
        if self.doc_kind=='chapter': self.reference.setCurrentIndex(self.reference.findData(self.document['id']))

    def reference_document(self,task=None):
        if not self.book: return None
        identity=task.get('chapter_id') if task else self.reference.currentData()
        return next((c for c in self.book['chapters'] if c['id']==identity),None)
    def refresh_nav(self):
        self.nav.blockSignals(True); self.nav.clear(); self.nav_items={}
        if self.book:
            for label,data in [('创意与设定',('book','premise')),('故事大纲',('book','outline')),('背景设定',('collection','世界观')),('人物',('collection','人物')),('章节',('collection','chapters')),('资料与伏笔',('collection','other'))]:
                item=QTreeWidgetItem([label]); item.setData(0,Qt.ItemDataRole.UserRole,data); self.nav.addTopLevelItem(item)
                self.nav_items[data]=item
            chapter_root=self.nav_items[('collection','chapters')]; volumes={}
            for c in self.book['chapters']:
                volume=c.get('volume','') or '未分卷'
                if volume not in volumes: volumes[volume]=QTreeWidgetItem([volume]); chapter_root.addChild(volumes[volume])
                item=QTreeWidgetItem([self.user.redact(c['title'])]); item.setData(0,Qt.ItemDataRole.UserRole,('chapter',c['id'])); volumes[volume].addChild(item); self.nav_items[('chapter',c['id'])]=item
            for record in self.book['lore']:
                group=record['kind'] if record['kind'] in ['世界观','人物'] else 'other'
                item=QTreeWidgetItem([self.user.redact(record['title'])]); item.setData(0,Qt.ItemDataRole.UserRole,('lore',record['id'])); self.nav_items[('collection',group)].addChild(item); self.nav_items[('lore',record['id'])]=item
            self.nav.expandAll()
            if self.document:
                data=(self.doc_kind,self.document['id']) if self.doc_kind!='book' else ('book','premise' if self.editor_tabs.currentIndex()==1 else 'outline')
                if data in self.nav_items: self.nav.setCurrentItem(self.nav_items[data])
        self.nav.blockSignals(False)

    def select_nav(self):
        if not self.ready: return
        items=self.nav.selectedItems()
        if not items: return
        data=items[0].data(0,Qt.ItemDataRole.UserRole)
        if not data or not self.save_current(): return
        kind,identity=data
        if kind=='collection': self.show_collection(identity)
        elif kind=='book': self.show_document('book',self.book,1 if identity=='premise' else 0)
        else:
            doc=next((d for d in self.book['chapters' if kind=='chapter' else 'lore'] if d['id']==identity),None)
            if doc: self.show_document(kind,doc)
        # Selecting a document never changes the active conversation or its references.
        self.remember_view(); self.persist()

    def navigation_menu(self,point):
        item=self.nav.itemAt(point)
        if not item: return
        data=item.data(0,Qt.ItemDataRole.UserRole)
        if not data or data[0] not in ['chapter','lore']: return
        self.nav.setCurrentItem(item); menu=QMenu(self.nav)
        if data[0]=='chapter': menu.addAction('上移',lambda:self.move_chapter(-1)); menu.addAction('下移',lambda:self.move_chapter(1)); menu.addSeparator()
        menu.addAction('删除',self.delete_document); menu.exec(self.nav.viewport().mapToGlobal(point))

    def save_document_position(self):
        if not self.book or not self.document: return
        self.book.setdefault('positions',{})[self.doc_kind+':'+self.document['id']]=dict(tab=self.editor_tabs.currentIndex(),
            cursors=[dict(position=w.textCursor().position(),anchor=w.textCursor().anchor(),scroll=w.verticalScrollBar().value()) for w in [self.editor,self.plan_editor,self.summary_editor]])

    def bind_editor(self,widget,field,doc,kind):
        key=(kind,doc['id'],field)
        cached=self.doc_cache.get(key)
        if cached is None:
            cached=QTextDocument(self); cached.setDocumentLayout(QPlainTextDocumentLayout(cached)); self.doc_cache[key]=cached
        appearance=self.user.state['appearance']; cached.setDefaultFont(QFont(appearance['family'],appearance['size']))
        text=str(doc.get(field,''))
        if cached.toPlainText()!=text: cached.setPlainText(text)
        widget.setDocument(cached)

    def show_document(self,kind,doc,tab=None):
        if self.document: self.capture_document(); self.save_document_position()
        if doc is None: self.show_collection('世界观'); return
        self.showing_document=True; self.autosave.stop(); self.document=doc; self.doc_kind=kind; self.chapter=doc if kind=='chapter' else None
        self.doc_title.show(); self.doc_title.setText(doc['title']); self.volume.setVisible(kind=='chapter'); self.volume.setText(doc.get('volume',''))
        self.pinned.setVisible(kind=='lore'); self.pinned.setChecked(doc.get('pinned',False))
        fields=['body','plan','summary'] if kind=='chapter' else ['outline','premise','style'] if kind=='book' else ['text']
        for i,widget in enumerate([self.editor,self.plan_editor,self.summary_editor]):
            self.editor_tabs.setTabVisible(i,i<len(fields))
            if i<len(fields): self.bind_editor(widget,fields[i],doc,kind)
        titles=['正文','章节计划','确认摘要'] if kind=='chapter' else ['故事大纲','创意与简介','文风与要求'] if kind=='book' else ['资料原文']
        for i,title in enumerate(titles): self.editor_tabs.setTabText(i,title)
        position=self.book.get('positions',{}).get(kind+':'+doc['id'],{}) if self.book else {}
        for widget,cursor_data in zip([self.editor,self.plan_editor,self.summary_editor],position.get('cursors',[])):
            cursor=widget.textCursor(); maximum=widget.document().characterCount()-1
            cursor.setPosition(min(cursor_data['anchor'],maximum)); cursor.setPosition(min(cursor_data['position'],maximum),QTextCursor.MoveMode.KeepAnchor); widget.setTextCursor(cursor); widget.verticalScrollBar().setValue(cursor_data['scroll'])
        self.editor_tabs.setCurrentIndex(tab if tab is not None else position.get('tab',0))
        self.editor_stack.setCurrentIndex(0); self.breadcrumb.setText((self.book['title']+' / ' if self.book else '')+('章节' if kind=='chapter' else '作品资料'))
        self.showing_document=False; self.refresh_ai_actions(); self.update_count(); self.update_selection_actions()
        if self.current: self.use_current.setEnabled(kind=='chapter')

    def show_collection(self,kind):
        if self.document: self.capture_document(); self.save_document_position()
        self.autosave.stop(); self.document=None; self.chapter=None; self.doc_kind='collection'; self.collection_kind=kind
        self.doc_title.hide(); self.volume.hide(); self.pinned.hide(); self.editor_stack.setCurrentIndex(1)
        title={'世界观':'背景与世界观','人物':'人物资料','chapters':'章节规划','other':'资料与伏笔','未归档对话':'未归档对话'}.get(kind,kind)
        self.collection_title.setText(title); self.breadcrumb.setText((self.book['title']+' / ' if self.book else '')+title)
        self.collection_note.setText('可以直接自己编写，也可以从AI候选开始，或先讨论再整理。AI内容经你采纳后才写入作品。')
        self.collection_items.clear()
        entries=(self.book['chapters'] if kind=='chapters' else [r for r in self.book['lore'] if (r['kind']==kind if kind in ['世界观','人物'] else r['kind'] not in ['世界观','人物'])]) if self.book else []
        for record in entries:
            item=QListWidgetItem(self.user.redact(record['title'])); item.setData(Qt.ItemDataRole.UserRole,record['id']); self.collection_items.addItem(item)
        self.refresh_ai_actions(); self.update_count(); self.use_current.setEnabled(False)

    def open_collection_item(self,item):
        identity=item.data(Qt.ItemDataRole.UserRole); kind='chapter' if self.collection_kind=='chapters' else 'lore'
        doc=next((d for d in self.book['chapters' if kind=='chapter' else 'lore'] if d['id']==identity),None)
        if doc: self.show_document(kind,doc)

    def capture_document(self):
        if self.showing_document or not self.document: return True
        d=self.document; d['title']=self.user.redact(self.doc_title.text().strip()) or '未命名'
        if self.doc_kind=='chapter': d.update(volume=self.user.redact(self.volume.text().strip()),body=self.user.redact(self.editor.toPlainText()),plan=self.user.redact(self.plan_editor.toPlainText()),summary=self.user.redact(self.summary_editor.toPlainText()))
        elif self.doc_kind=='book': d.update(outline=self.user.redact(self.editor.toPlainText()),premise=self.user.redact(self.plan_editor.toPlainText()),style=self.user.redact(self.summary_editor.toPlainText()))
        else: d.update(text=self.user.redact(self.editor.toPlainText()),pinned=self.pinned.isChecked())
        return True

    def editor_tab_changed(self,*_):
        if self.ready and not self.showing_document: self.refresh_ai_actions(); self.update_selection_actions(); self.remember_view()

    def schedule_save(self,*_):
        if not self.ready or self.showing_document: return
        self.update_count(); self.autosave.start(1500); self.schedule_context()

    def schedule_context(self,*_):
        if self.ready and not self.pending_lock: self.context_timer.start(450)

    def update_count(self):
        if not self.document: self.count.setText('支持手写、AI起草和共同修改'); return
        current=[self.editor,self.plan_editor,self.summary_editor][self.editor_tabs.currentIndex()]
        self.count.setText('%d 字符 · 自动保存草稿 · Ctrl+S 保存'%len(current.toPlainText()))

    def autosave_document(self):
        if not self.ready or self.pending_lock: return
        if self.save_current(): self.status.setText('已加密自动保存草稿')

    def save_draft(self):
        self.autosave.stop()
        if self.save_current(): self.status.setText('草稿已保存；应用AI修改时会自动保留原版本')

    def save_version(self):
        if not self.document: return
        self.capture_document(); checkpoint(self.document,self.fields())
        if self.persist(): self.refresh_nav(); self.refresh_books(); self.status.setText('已创建版本快照 · 最近50个版本可恢复')

    def fields(self): return FIELDS[self.doc_kind]

    def refresh_ai_actions(self):
        if not self.ready: return
        if self.doc_kind=='book': primary=['完善大纲','创意构思','文风方案'][self.editor_tabs.currentIndex()]
        elif self.doc_kind=='chapter': primary='续写' if self.editor.toPlainText().strip() else '起草本章'
        elif self.doc_kind=='lore': primary='人物候选' if self.document['kind']=='人物' else '起草背景' if self.document['kind']=='世界观' else '完善资料'
        else: primary={'世界观':'起草背景','人物':'人物候选','chapters':'章节规划'}.get(getattr(self,'collection_kind',''),'创意构思')
        self.primary_name=primary; label='完善人物' if primary=='人物候选' and self.doc_kind=='lore' else primary
        self.primary_ai.setText('AI '+label); self.primary_ai.setEnabled(self.book is not None)
        self.document_menu.clear()
        choices=['创意构思','完善大纲','起草背景','人物候选','文风方案','章节规划']
        if self.doc_kind=='chapter': choices=['起草本章','续写','光标续写','润色选段','扩写选段','压缩选段','总结本章','检查矛盾','按意见修稿']
        for name in choices: self.document_menu.addAction('AI '+name,lambda checked=False,n=name:self.task(n))
        self.document_menu.addSeparator(); self.document_menu.addAction('创建版本快照',self.save_version); self.document_menu.addAction('版本 / 对比',self.versions)
        if self.doc_kind in ['chapter','lore']: self.document_menu.addAction('删除当前文档',self.delete_document)

    def primary_task(self): self.task(self.primary_name)

    def manual_edit(self):
        if not self.book: return
        if self.doc_kind=='collection':
            if self.collection_kind=='chapters': self.add_chapter()
            else: self.add_lore(self.collection_kind if self.collection_kind in ['世界观','人物'] else None)
        self.editor.setFocus()

    def discuss_document(self):
        if not self.current: return
        title=self.document['title'] if self.document else getattr(self,'collection_kind','这部作品')
        self.set_mode('discuss')
        if not self.input.toPlainText().strip(): self.input.setPlainText('我们先讨论“'+title+'”。请帮我梳理方向，信息不足时可以先提问。')
        self.input.setFocus()

    def update_selection_actions(self):
        if not getattr(self,'ready',False): return
        if hasattr(self,'selection_tools'):
            self.selection_tools.setVisible(self.doc_kind=='chapter' and self.editor_tabs.currentIndex()==0 and self.editor.textCursor().hasSelection())

    def selection_menu(self,point):
        menu=self.editor.createStandardContextMenu(); menu.addSeparator()
        for name in ['润色选段','扩写选段','压缩选段']:
            action=menu.addAction('AI '+name,lambda checked=False,n=name:self.task(n)); action.setEnabled(self.doc_kind=='chapter' and self.editor.textCursor().hasSelection())
        menu.exec(self.editor.viewport().mapToGlobal(point))

    def task(self,name):
        if not self.book or not self.current: return
        self.capture_document(); self.save_document_position()
        try:
            kind=self.doc_kind; doc=self.document; field=None; start=end=None
            if kind=='lore': field='text'
            if kind=='chapter':
                field='body'; cursor=self.editor.textCursor(); source=self.editor.toPlainText()
                start=qt_index(source,cursor.selectionStart()); end=qt_index(source,cursor.selectionEnd())
                if name=='续写': start=end=len(source)
            task=make_task(self.book,name,doc,kind,field,start,end)
            if name=='起草本章' and doc and doc.get('body','').strip(): task['full_chapter']=True
            self.pending_task=task; self.current['draft_task']=copy.deepcopy(task); self.update_task_hint(); self.schedule_context(); self.persist()
            self.assistant.show(); self.input.setFocus(); self.status.setText('任务已准备；补充要求可留空，点击发送才会调用AI。')
        except ValueError as exc: error(self,str(exc))

    def update_task_hint(self):
        self.reference.setEnabled(not bool(self.pending_task)); self.use_current.setEnabled(not self.pending_task and self.doc_kind=='chapter')
        self.task_row.setVisible(bool(self.pending_task))
        if self.pending_task:
            self.task_label.setText(self.pending_task['name']+' → '+self.pending_task['label'])
            self.input.setPlaceholderText('可选：补充本次要求。留空也可以直接发送，生成结果先作为候选。')
        else: self.input.setPlaceholderText('讨论创意、背景或剧情……')

    def clear_task(self):
        self.pending_task=None
        if self.current: self.current.pop('draft_task',None)
        self.update_task_hint(); self.schedule_context(); self.persist()
    def task_context_entries(self,task,query):
        ref=self.reference_document(task); entries=context_sources(self.book,ref,query)
        if task and task.get('full_chapter') and ref:
            for entry in entries:
                if entry['id']=='body:'+ref['id']: entry['required']=True
        if task and task.get('kind')=='lore':
            doc=find_document(self.book,'lore',task['doc_id']); text=doc.get(task['field'],'')
            if text.strip(): entries.append(dict(id='task-source:'+doc['id'],label='本次处理的资料 · '+doc['title'],text=text,reason='本次操作绑定的原资料',required=True,priority=100))
        if task and task['target']=='insert':
            doc=find_document(self.book,'chapter',task['doc_id']); body=doc['body']; point=task['start']
            text=body[max(0,point-2500):point]+'【在此插入续写】'+body[point:point+1200]
            entries.append(dict(id='insertion:'+doc['id'],label='续写位置附近正文',text=text,reason='保留插入点前后衔接',required=True,priority=100))
        target=(task.get('doc_id') or task.get('target',''))+'/'+task.get('field','') if task else 'free'
        scope=self.book['id']+'/'+(ref['id'] if ref else '-')+'/'+str(bool(task and task.get('full_chapter')))+'/'+target
        return entries,ref,scope

    def context_bundle(self,task=None,query=''):
        if not self.book: return dict(text='',selected=set(),omitted=[],characters=0),[],'none'
        entries,ref,scope=self.task_context_entries(task,query)
        manual=self.current.get('context_policy')=='manual' and self.current.get('context_scope')==scope
        selected=self.current.get('context_selected') if manual else None
        if task and task.get('full_chapter') and ref:
            selected=set(selected) if selected is not None else assemble(entries,self.user.state.get('context_budget',32000))['selected']
            selected.discard('tail:'+ref['id']); selected.add('body:'+ref['id'])
        return assemble(entries,self.user.state.get('context_budget',32000),selected),entries,scope

    def user_text(self):
        guidance=self.input.toPlainText().strip()
        return self.user.redact(task_input(self.pending_task,guidance) if self.pending_task else guidance)

    def prepared_messages(self,preview=False):
        if not self.current: raise ValueError('请先新建讨论。')
        profile=next((p for p in self.user.state['profiles'] if p['name']==self.profile.currentText()),None)
        if profile is None: raise ValueError('请先选择API配置。')
        bundle,entries,scope=self.context_bundle(self.pending_task,self.user_text())
        snapshot=copy.deepcopy(self.current)
        snapshot.update(system=self.user.redact(self.prompt.toPlainText()),context_bundle=self.user.redact(bundle['text']),style=self.style.currentText(),**{k:w.value() for k,w in self.params.items()})
        enabled=[a for a in self.draft if a not in self.current.get('disabled_attachments',[])]
        snapshot['messages'].append(dict(role='user',content=self.user_text(),attachments=enabled))
        read=(lambda _:b'') if preview else self.user.read_blob
        messages=messages_for(snapshot,self.user.state['attachments'],read,profile.get('vision',False))
        for message in messages:
            if isinstance(message['content'],str): message['content']=self.user.redact(message['content'])
            else:
                for part in message['content']:
                    if part['type']=='text': part['text']=self.user.redact(part['text'])
        return profile,messages,bundle,entries,scope

    def update_context_label(self):
        if not self.ready or self.pending_lock or not self.current: return
        self.capture_document()
        try:
            profile,messages,bundle,entries,scope=self.prepared_messages(preview=True)
            prefix='任务引用' if self.pending_task else '本次引用'
            self.context_button.setText('%s：%d项 · 约%d token · 查看'%(prefix,len(bundle['selected']),estimate(messages)))
            labels=[e['label'] for e in entries if e['id'] in bundle['selected']]
            self.context_button.setToolTip('估算包含会话与附件，图片误差较大；不是实际计费量。\n'+'\n'.join(labels))
        except ValueError as exc:
            self.context_button.setText('引用需要调整 · 查看'); self.context_button.setToolTip(str(exc))
        except Exception: self.context_button.setText('引用暂不可读 · 查看')

    def context_dialog(self):
        if not self.book: return None
        self.capture_document(); entries,ref,scope=self.task_context_entries(self.pending_task,self.user_text())
        selected=self.current.get('context_selected') if self.current.get('context_policy')=='manual' and self.current.get('context_scope')==scope else None
        if self.pending_task and self.pending_task.get('full_chapter') and ref:
            try: selected=assemble(entries,self.user.state.get('context_budget',32000),selected)['selected']
            except ValueError: selected={e['id'] for e in entries if e['required']}
            selected=set(selected); selected.discard('tail:'+ref['id']); selected.add('body:'+ref['id'])
        dialog=ContextDialog(self,entries,self.user.state.get('context_budget',32000),selected); dialog.scope=scope
        return dialog

    def preview_context(self):
        dialog=self.context_dialog()
        if not dialog: self.status.setText('未归档对话不引用小说资料。'); return
        if dialog.exec()==QDialog.DialogCode.Accepted and not self.user.closed:
            self.current.update(context_selected=list(dialog.bundle['selected']),context_scope=dialog.scope,context_policy='manual' if dialog.manual else 'auto')
            self.user.state['context_budget']=dialog.budget.value(); self.persist(); self.update_context_label()
        dialog.entries=[]; dialog.bundle=None; dialog.deleteLater()

    def send(self):
        if self.busy: self.status.setText('当前任务正在生成，可继续编写其他草稿；结束后再发送。'); return
        if not self.current or not (self.input.toPlainText().strip() or self.draft or self.pending_task): return
        if not self.save_current(): return
        try:
            profile,messages,bundle,entries,scope=self.prepared_messages()
            if not profile.get('key'): self.status.setText('请在“设置 → API与模型”保存自己的密钥，再发送。'); return
            task=copy.deepcopy(self.pending_task)
            if task: check_target(self.user.state,task)
        except ValueError as exc: self.status.setText(self.user.redact(str(exc))); return
        except Exception: self.status.setText('准备请求失败，请检查引用、附件和模型配置。'); return
        session=self.current; before=copy.deepcopy(session)
        enabled=[a for a in self.draft if a not in session.get('disabled_attachments',[])]
        question=dict(id=uid(),role='user',content=self.user_text(),attachments=enabled,guidance=self.user.redact(self.input.toPlainText()))
        if task: question['task']=copy.deepcopy(task)
        answer=dict(id=uid(),role='assistant',content='',status='incomplete')
        if task: answer['task']=task
        session['messages'].extend([question,answer]); session['draft_text']=''; session['draft_attachments']=[]; session.pop('draft_task',None)
        if not self.persist():
            session.clear(); session.update(before); return
        self.input.blockSignals(True); self.input.clear(); self.input.blockSignals(False); self.draft=[]; self.pending_task=None
        self.run=dict(session=session,message=answer,book_id=self.book['id'] if self.book else None,task=task)
        self.busy=True; self.selected_index=len(session['messages'])-1; self.update_task_hint(); self.refresh_attachments(); self.render(); self.refresh_sessions()
        self.worker=Request(copy.deepcopy(profile),messages,{k:session[k] for k in ['temperature','tokens']})
        self.worker.chunk.connect(self.on_chunk); self.worker.result.connect(self.on_result); self.worker.finished.connect(self.worker_finished)
        self.worker.start(); self.busy_controls(); self.status.setText('正在生成 · 可继续查看其他章节和资料，结果只进入原讨论。')

    def on_chunk(self,text):
        if not self.run: return
        self.run['message']['content']+=text
        if not self.pending_lock and self.current and self.current['id']==self.run['session']['id'] and not self.render_timer.isActive(): self.render_timer.start(80)

    def on_result(self,result):
        self.render_timer.stop()
        if not self.run: return
        self.run['finished']=True
        message=self.run['message']; message['content']=self.user.redact(message['content'])
        message['status']='complete' if result=='done' and message['content'] else 'incomplete'
        self.last_result=dict(session_id=self.run['session']['id'],book_id=self.run['book_id']); self.ui['last_result']=self.last_result
        try: self.user.save()
        except Exception:
            self.status.setText('生成内容仍在内存，暂时无法保存；请恢复指定目录后保存。'); return
        self.status.setText(('生成完成，候选尚未写入作品' if result=='done' else '已停止，已保留不完整候选' if result=='stopped' else result)+' · %d字符'%len(message['content']))
        if not self.pending_lock and self.current and self.current['id']==self.run['session']['id']: self.render()

    def worker_finished(self):
        identity=self.run['session']['id'] if self.run else None
        self.busy=False
        if self.worker: self.worker.deleteLater(); self.worker=None
        self.run=None; self.refresh_sessions()
        if self.pending_lock: self.close(); return
        if self.close_requested:
            self.close_requested=False; self.centralWidget().setEnabled(True); self.close(); return
        self.busy_controls(); self.schedule_context()
        if self.current and self.current['id']==identity: self.render()

    def stop(self):
        if self.worker: self.worker.stop.set(); self.status.setText('正在停止，等待本次网络读取结束（最长约30秒）。')

    def busy_controls(self):
        if not self.ready: return
        self.send_button.setEnabled(not self.busy); self.stop_button.setVisible(self.busy)
        target=self.run['session']['id'] if self.run else self.last_result.get('session_id') if self.last_result else None
        self.run_button.setText('查看生成' if self.busy else '查看结果')
        self.run_button.setVisible(bool(target and self.current and target!=self.current['id']))

    def open_run(self):
        if not self.save_current(): return
        identity=self.run['session']['id'] if self.run else self.last_result.get('session_id') if self.last_result else None
        session=next((s for s in self.user.state['sessions'] if s['id']==identity),None)
        if session is None: self.status.setText('对应讨论已不存在。'); return
        book_id=session.get('project_id'); index=self.books.findData(book_id)
        if (self.book['id'] if self.book else None)!=book_id and index>=0: self.switch_book(index)
        self.load(session); self.set_mode('discuss')

    def render(self):
        if not self.ready or not self.current: return
        scrollbar=self.chat.verticalScrollBar(); old_scroll=scrollbar.value(); at_end=old_scroll>=scrollbar.maximum()-24
        cursor=self.chat.textCursor(); selection=(cursor.anchor(),cursor.position()) if cursor.hasSelection() else None
        dark=self.user.state['appearance']['theme']=='深色'; foreground='#e8edf5' if dark else '#253448'; muted='#9facbd' if dark else '#64748b'; user_bg='#2b3e55' if dark else '#edf2f8'
        parts=[]; cache={}
        for index,message in enumerate(self.current['messages']):
            identity=message.setdefault('id',uid()); content=self.user.redact(message['content']); key=(identity,content)
            body=self.html_cache.get(key)
            if body is None:
                doc=QTextDocument(); doc.setDefaultFont(self.chat.font()); doc.setMarkdown(content)
                source=doc.toHtml(); body=source.split('<body',1)[-1].split('>',1)[-1].rsplit('</body>',1)[0]
                body=re.sub(r'\s+href="[^"]*"','',body,flags=re.I)
            cache[key]=body
            title='你' if message['role']=='user' else 'AI'; badges=[]
            if message.get('excluded'): badges.append('已排除上下文')
            if message.get('status','complete')!='complete': badges.append('生成中' if self.run and not self.run.get('finished') and self.run['message'] is message else '未完成')
            task=message.get('task')
            if task: badges.append(task['name'])
            label=html.escape(title+' · '+' · '.join(badges) if badges else title)
            actions=[]
            if message['role']=='assistant' and message.get('status','complete')=='complete':
                if task and task['target']!='none': actions.append(('apply','查看审稿' if task['target']=='review' else '对比 / 采用'))
                else: actions.append(('save','写入作品资料'))
            if message['role']=='assistant': actions.append(('retry','再试一版'))
            actions.append(('more','更多'))
            links='　'.join('<a href="mzw-action:/%s/%d" style="color:%s">%s</a>'%(action,index,muted,text) for action,text in actions)
            background=' bgcolor="'+user_bg+'"' if message['role']=='user' else ''
            parts.append('<table width="100%" cellpadding="9" cellspacing="0"'+background+'><tr><td><p style="color:'+muted+'">'+label+'</p>'+body+'<p>'+links+'</p></td></tr></table><p style="font-size:4px">&nbsp;</p>')
        self.html_cache=cache
        placeholder='<p style="color:'+muted+'">从一个想法开始。可以讨论背景、大纲或人物，也可以选中文本让AI共同修改。未采纳的回复不会自动进入作品资料。</p>'
        self.chat.setHtml('<div style="color:'+foreground+'">'+(''.join(parts) if parts else placeholder)+'</div>')
        if selection:
            current=self.chat.textCursor(); maximum=self.chat.document().characterCount()-1
            current.setPosition(min(selection[0],maximum)); current.setPosition(min(selection[1],maximum),QTextCursor.MoveMode.KeepAnchor); self.chat.setTextCursor(current)
        scrollbar.setValue(scrollbar.maximum() if at_end else old_scroll)

    def message_action(self,url):
        if url.scheme()!='mzw-action': return
        try: action,index=url.path().strip('/').split('/'); index=int(index)
        except (ValueError,TypeError): return
        if not self.current or not 0<=index<len(self.current['messages']): return
        self.select_message(index)
        if action=='apply': self.adopt()
        elif action=='retry': self.regenerate()
        elif action=='save': self.save_message_material()
        elif action=='more':
            menu=QMenu(self.chat)
            for title,fn in [('排除 / 恢复上下文',self.exclude_reply),('删除消息',self.delete_reply),('从这里创建分支',self.branch_here),('复制消息',self.copy_message),('另存为TXT / MD',self.export_message)]: menu.addAction(title,fn)
            menu.exec(QCursor.pos())

    def select_message(self,index):
        self.selected_index=index

    def selected_reply(self):
        return self.selected_index if self.current and self.selected_index is not None and 0<=self.selected_index<len(self.current['messages']) else None

    def can_change_messages(self):
        if self.run and self.current and self.run['session']['id']==self.current['id']:
            self.status.setText('请先停止当前讨论的生成，再修改其历史消息。'); return False
        return True

    def exclude_reply(self):
        index=self.selected_reply()
        if index is None or not self.can_change_messages(): return
        exclude_message(self.current,index); self.persist(); self.render(); self.schedule_context()
        self.status.setText('已更新发送范围；已有的后续回答不会重写，纠错可从出错前创建分支。')

    def delete_reply(self):
        index=self.selected_reply()
        if index is None or not self.can_change_messages(): return
        remove_message(self.current,index); self.selected_index=None; self.persist(); self.render(); self.schedule_context()
        self.status.setText('消息已删除，不再发送。可在讨论菜单中撤销最近20次删除。')

    def undo_reply(self):
        if self.current and self.can_change_messages(): undo_remove(self.current); self.persist(); self.render(); self.schedule_context()

    def open_branch(self,session):
        if not self.save_current(): return
        self.user.state['sessions'].append(session); self.load(session); self.persist()

    def branch_here(self):
        index=self.selected_reply()
        if index is not None and self.can_change_messages() and self.save_current(): self.open_branch(branch(self.current,index))

    def regenerate(self):
        index=self.selected_reply()
        if index is None or not self.can_change_messages() or not self.save_current(): return
        try:
            old=self.current['messages'][index]; fresh=candidate_branch(self.current,index)
            if old.get('task'):
                task=copy.deepcopy(old['task'])
                # Refresh the source binding deliberately from current author text.
                self.capture_document(); book=find_book(self.user.state,task['book_id'])
                if task['target'] in ['selection','insert']: check_target(self.user.state,task)
                else:
                    doc=find_document(book,task['kind'],task['doc_id']) if task.get('kind') else None
                    task=make_task(book,task['name'],doc,task.get('kind'),task.get('field'))
                fresh['draft_task']=task
                start=index-1
                while start>=0 and self.current['messages'][start]['role']!='user': start-=1
                guidance=self.current['messages'][start].get('guidance','') if start>=0 else ''
                fresh['draft_text']=guidance+'\n请提供不同的候选方案，保留已确认事实。'
            self.open_branch(fresh); self.status.setText('已准备独立候选分支。原讨论保留，点击发送生成新方案。')
        except ValueError as exc: error(self,str(exc))

    def copy_message(self):
        index=self.selected_reply()
        if index is not None: QApplication.clipboard().setText(self.user.redact(self.current['messages'][index]['content']))
    def adopt(self):
        index=self.selected_reply()
        if index is None: return
        message=self.current['messages'][index]
        if message['role']!='assistant' or message.get('status','complete')!='complete': self.status.setText('请等待候选完整生成；不完整内容可以复制保留。'); return
        task=message.get('task')
        if not task: self.save_message_material(); return
        self.capture_document(); dialog=CandidateDialog(self,copy.deepcopy(task),message['content'])
        result=dialog.exec()
        if result!=QDialog.DialogCode.Accepted or self.user.closed: dialog.deleteLater(); return
        text=dialog.result_text; title=dialog.result_title; reuse=dialog.reuse_empty; dialog.result_text=None; dialog.deleteLater()
        if task['target']=='review':
            try:
                book=find_book(self.user.state,task['book_id']); doc=find_document(book,'chapter',task['doc_id'])
                check_target(self.user.state,task)
                self.pending_task=make_task(book,'按意见修稿',doc,'chapter','body')
                self.input.setPlainText('请处理以下已确认的审稿意见：\n'+text); self.current['draft_task']=copy.deepcopy(self.pending_task)
                self.update_task_hint(); self.schedule_context(); self.persist(); self.status.setText('修稿任务已准备，点击发送生成修稿候选。')
            except ValueError as exc: error(self,str(exc))
            return
        self.apply_candidate(task,text,title,reuse)

    def apply_candidate(self,task,text,title='',reuse_empty=False):
        self.capture_document(); self.save_document_position()
        before=copy.deepcopy(self.user.state)
        pointers=dict(book=self.book['id'] if self.book else None,document=self.document['id'] if self.document else None,kind=self.doc_kind,
                      discussion=self.current['id'] if self.current else None,tab=self.editor_tabs.currentIndex())
        try:
            outcome=apply_proposal(self.user.state,task,self.user.redact(text),self.user.redact(title),reuse_empty)
            self.user.save()
        except ValueError as exc:
            error(self,str(exc)); return False
        except Exception:
            self.user.state.clear(); self.user.state.update(before); self.rebind_state(pointers)
            error(self,'候选未能保存，已恢复原数据；没有改用其他目录。'); return False
        self.refresh_nav(); self.refresh_reference(); self.schedule_context()
        if self.document and outcome['doc_id']==self.document['id']:
            tab=self.editor_tabs.currentIndex(); doc=self.document; kind=self.doc_kind
            # Model changes are loaded deliberately; their exact old contents are in versions.
            self.document=None; self.show_document(kind,doc,tab)
        elif self.doc_kind=='collection' and self.book and outcome['book_id']==self.book['id']:
            self.show_collection(self.collection_kind)
        self.status.setText('已采用到 '+task['label']+'；原内容已保留版本，当前编辑位置未自动跳转。')
        return True

    def rebind_state(self,pointers):
        self.ui=self.user.state.setdefault('workspace',{})
        self.book=next((b for b in self.user.state['projects'] if b['id']==pointers['book']),None)
        self.current=next((s for s in self.user.state['sessions'] if s['id']==pointers['discussion']),None)
        if self.run:
            sid=self.run['session']['id']; mid=self.run['message']['id']
            self.run['session']=next(s for s in self.user.state['sessions'] if s['id']==sid)
            self.run['message']=next(m for m in self.run['session']['messages'] if m['id']==mid)
        self.document=None
        if self.book and pointers['kind'] in FIELDS:
            try: doc=find_document(self.book,pointers['kind'],pointers['document']); self.show_document(pointers['kind'],doc,pointers['tab'])
            except ValueError: self.show_document('book',self.book,1)
        self.refresh_books(); self.refresh_sessions(); self.refresh_nav(); self.render()

    def save_message_material(self):
        index=self.selected_reply()
        if index is None or not self.book: return
        text=self.current['messages'][index]['content']
        task=dict(target='new_lore',book_id=self.book['id'],name='保存讨论资料',label='新增参考资料',lore_kind='参考资料')
        dialog=CandidateDialog(self,task,text)
        if dialog.exec()==QDialog.DialogCode.Accepted and not self.user.closed:
            content=dialog.result_text; title=dialog.result_title; dialog.result_text=None; self.apply_candidate(task,content,title)
        dialog.deleteLater()

    def versions(self):
        if not self.document: self.status.setText('请先打开具体文档。'); return
        self.capture_document(); document=self.document; kind=self.doc_kind; fields=self.fields()
        dialog=QDialog(self); dialog.setWindowTitle('版本历史 · 恢复前保留当前内容'); dialog.resize(880,580); layout=QVBoxLayout(dialog)
        items=QListWidget(); layout.addWidget(items)
        for version in document.get('versions',[]):
            items.addItem(datetime.fromisoformat(version['at']).astimezone().strftime('%Y-%m-%d %H:%M:%S')+' · '+version['data'].get('title','文档'))
        diff=QPlainTextEdit(); diff.setReadOnly(True); layout.addWidget(diff,1)
        def compare(index):
            if index<0: return
            old=document['versions'][index]['data']; left='\n'.join(str(old.get(k,'')) for k in fields); right='\n'.join(str(document.get(k,'')) for k in fields)
            diff.setPlainText(''.join(difflib.unified_diff(left.splitlines(True),right.splitlines(True),fromfile='历史版本',tofile='当前草稿')))
        items.currentRowChanged.connect(compare)
        def recover():
            index=items.currentRow()
            if index<0: return
            before=copy.deepcopy(document); restore(document,index,fields)
            try: self.user.save()
            except Exception: document.clear(); document.update(before); error(dialog,'版本恢复未能保存，原文未改变。'); return
            self.document=None; self.show_document(kind,document); self.refresh_nav(); self.schedule_context(); dialog.accept()
        button('恢复选中版本',recover,layout); dialog.exec(); diff.clear(); dialog.deleteLater()

    def create_book(self):
        name,ok=QInputDialog.getText(self,'新建作品','作品名称：')
        if not ok or not name.strip() or not self.save_current(): return
        book=project(self.user.redact(name[:100])); self.user.state['projects'].append(book)
        if self.persist(): self.refresh_books(); self.switch_book(self.books.findData(book['id']))

    def delete_book(self):
        if not self.book: return
        if self.run and self.run['book_id']==self.book['id']: error(self,'请先停止这部作品的生成。'); return
        if QMessageBox.question(self,'删除作品','删除当前作品的章节、资料、版本和讨论？附件库保留。建议先创建加密备份。')!=QMessageBox.StandardButton.Yes: return
        identity=self.book['id']; self.user.state['projects']=[b for b in self.user.state['projects'] if b['id']!=identity]
        self.user.state['sessions']=[s for s in self.user.state['sessions'] if s.get('project_id')!=identity]
        self.current=None; self.book=None; self.document=None; self.chapter=None
        if not self.user.state['projects']: self.user.state['projects'].append(project('新作品'))
        self.persist(); self.refresh_books(); self.switch_book(0)

    def add_chapter(self):
        if not self.book or not self.save_current(): return
        doc=chapter('第%d章'%(len(self.book['chapters'])+1)); self.book['chapters'].append(doc)
        if self.persist(): self.refresh_nav(); self.show_document('chapter',doc); self.refresh_reference()

    def add_lore(self,kind=None):
        if not self.book or not self.save_current(): return
        if not kind:
            kind,ok=QInputDialog.getItem(self,'新增资料','资料类型：',['世界观','人物','关系与地点','时间线','剧情状态','伏笔','参考资料'],0,False)
            if not ok: return
        doc=dict(id=uid(),title='新'+kind,kind=kind,text='',pinned=False,versions=[])
        doc['checkpoint']={k:doc[k] for k in LORE_FIELDS}; self.book['lore'].append(doc)
        if self.persist(): self.refresh_nav(); self.show_document('lore',doc)

    def move_chapter(self,delta):
        if self.doc_kind!='chapter': return
        self.capture_document(); chapters=self.book['chapters']; i=chapters.index(self.document); j=i+delta
        if 0<=j<len(chapters): chapters[i],chapters[j]=chapters[j],chapters[i]; self.persist(); self.refresh_nav(); self.schedule_context()

    def delete_document(self):
        if self.doc_kind not in ['chapter','lore'] or not self.document: return
        if self.run and self.run.get('task') and self.run['task'].get('doc_id')==self.document['id']: error(self,'请先停止这个文档的任务。'); return
        if QMessageBox.question(self,'删除文档','删除当前文档和版本历史？关联的讨论仍保留。')!=QMessageBox.StandardButton.Yes: return
        kind=self.doc_kind; identity=self.document['id']; collection=self.book['chapters' if kind=='chapter' else 'lore']
        collection.remove(self.document); self.document=None; self.chapter=None
        for key,doc in list(self.doc_cache.items()):
            if key[1]==identity: self.doc_cache.pop(key)
        self.persist(); self.refresh_nav(); self.refresh_reference(); self.show_collection('chapters' if kind=='chapter' else 'other')

    def export_manuscript(self):
        if not self.book or not self.save_current(): return
        try:
            path=choose_save(self,'另存作品正文','Markdown (*.md);;文本 (*.txt)')
            if not path: return
            if path.suffix.lower() not in ['.md','.txt']: raise ValueError('请选择TXT或MD格式。')
            md=path.suffix.lower()=='.md'; text=('# ' if md else '')+self.book['title']+'\n\n'
            for c in self.book['chapters']: text+=('## ' if md else '')+c.get('volume','')+' · '+c['title']+'\n\n'+c['body']+'\n\n'
            atomic_write(path,self.user.redact(text).encode('utf-8')); self.status.setText('正文已另存为：'+str(path))
        except ValueError as exc: error(self,str(exc))
        except Exception: error(self,'导出未完成，请检查指定目录。')

    def export(self,answer_only):
        if not self.current or not self.save_current(): return
        include=False if answer_only else QMessageBox.question(self,'导出选项','是否包含背景提示词？')==QMessageBox.StandardButton.Yes
        try:
            path=choose_save(self,'另存讨论','Markdown (*.md);;文本 (*.txt)')
            if not path: return
            if path.suffix.lower() not in ['.md','.txt']: raise ValueError('请选择TXT或MD。')
            text=export_text(self.current,self.user.state['attachments'],answer_only,path.suffix.lower()=='.md',include)
            atomic_write(path,self.user.redact(text).encode('utf-8')); self.status.setText('已导出：'+str(path))
        except ValueError as exc: error(self,str(exc))
        except Exception: error(self,'导出未完成，请检查指定目录。')

    def export_message(self):
        index=self.selected_reply()
        if index is None: return
        try:
            path=choose_save(self,'另存这条消息','Markdown (*.md);;文本 (*.txt)')
            if not path: return
            if path.suffix.lower() not in ['.md','.txt']: raise ValueError('请选择TXT或MD。')
            atomic_write(path,self.user.redact(self.current['messages'][index]['content']).encode('utf-8'))
        except ValueError as exc: error(self,str(exc))
        except Exception: error(self,'消息未能导出。')

    def refresh_attachments(self):
        if not self.current: return
        MainWindow.refresh_attachments(self); self.attachment_list.hide()
        count=len([a for a in self.draft if a not in self.current.get('disabled_attachments',[])])
        self.attach_button.setText('＋ 附件'+(' '+str(count) if count else ''))
        self.schedule_context()

    def attach(self):
        names,_=QFileDialog.getOpenFileNames(self,'上传附件','','附件 (*.txt *.md *.csv *.pdf *.docx *.png *.jpg *.jpeg *.webp)')
        if self.user.closed: return
        for name in names:
            try: self.draft.append(self.user.add_attachment(name))
            except ValueError as exc: error(self,str(exc))
            except Exception: error(self,'附件解析或加密保存失败。')
        self.refresh_attachments(); self.save_current()

    def choose_attachments(self):
        dialog=QDialog(self); dialog.setWindowTitle('本讨论的附件 · 勾选后参与发送'); dialog.resize(660,380)
        layout=QVBoxLayout(dialog); layout.addWidget(self.attachment_list); self.attachment_list.show()
        row=QHBoxLayout(); layout.addLayout(row); button('预览',self.preview_attachment,row); button('完成',dialog.accept,row)
        dialog.exec(); self.attachment_list.setParent(self); self.attachment_list.hide(); dialog.deleteLater(); self.refresh_attachments()

    def toggle_attachment(self,item):
        if not self.current: return
        identity=item.data(Qt.ItemDataRole.UserRole); disabled=self.current.setdefault('disabled_attachments',[])
        if item.checkState()==Qt.CheckState.Unchecked and identity not in disabled: disabled.append(identity)
        elif item.checkState()==Qt.CheckState.Checked and identity in disabled: disabled.remove(identity)
        self.persist(); self.schedule_context()

    def chat_parameters(self):
        if not self.current: return
        dialog=QDialog(self); dialog.setWindowTitle('本讨论的模型、背景提示词与生成参数'); dialog.resize(620,470); layout=QFormLayout(dialog)
        layout.addRow('API配置',self.profile); layout.addRow('背景提示词',self.prompt)
        for key,title in [('temperature','温度'),('tokens','最大输出token'),('chars','期望字符（0不限）'),('rounds','保留历史轮数')]: layout.addRow(title,self.params[key])
        layout.addRow('遵循预设',self.style)
        for w in [self.profile,self.prompt,self.style,*self.params.values()]: w.show()
        button('完成',dialog.accept,layout); dialog.exec()
        for w in [self.profile,self.prompt,self.style,*self.params.values()]: w.setParent(self); w.hide()
        dialog.deleteLater()
        if not self.user.closed: self.save_current(); self.profile_changed(); self.update_context_label()

    def settings(self):
        if self.busy: self.status.setText('请先停止生成，再修改账户或API设置。'); return
        if not self.save_current(): return
        pointers=dict(book=self.book['id'] if self.book else None,document=self.document['id'] if self.document else None,kind=self.doc_kind,
                      discussion=self.current['id'] if self.current else None,tab=self.editor_tabs.currentIndex())
        Settings(self).exec()
        if self.user.closed: return
        self.rebind_state(pointers); self.refresh_reference(); self.apply_appearance()
    def apply_appearance(self):
        if not self.ready: return
        a=self.user.state['appearance']; dark=a['theme']=='深色'
        bg='#151b25' if dark else '#eef0ed'; paper='#202a38' if dark else '#fffefa'; ink='#e6edf5' if dark else '#263345'
        muted='#adbacb' if dark else '#627185'; border='#3b4b60' if dark else '#d9dfe6'; selected='#344c6b' if dark else '#e7eef8'
        ui_size=a.get('ui_size',10)
        self.setStyleSheet('''QWidget {background: %s; color: %s; font-family: "Microsoft YaHei"; font-size: %dpt;}
        QWidget#paper,QWidget#assistant,QWidget#sidebar {background: %s; border-radius: 9px;}
        QWidget#collection {background: transparent;} QLabel {background: transparent;} QLabel#brand {font-size: 17pt; font-weight: 600;} QLabel#muted {color: %s;} QLabel#emptyTitle {font-size: 18pt;}
        QLineEdit,QPlainTextEdit,QListWidget,QTreeWidget,QComboBox,QSpinBox,QDoubleSpinBox,QTextBrowser {background: %s; border: 1px solid %s; border-radius: 5px; padding: 4px; selection-background-color: %s; selection-color: %s;}
        QLineEdit#documentTitle {font-size: 17pt; border: none; padding: 2px;}
        QPushButton,QToolButton {background: %s; border: 1px solid %s; border-radius: 5px; padding: 5px 8px;}
        QPushButton:hover,QToolButton:hover {border-color: #7197c4;} QPushButton:checked,QPushButton#primary {background: #365b8c; color: white; border-color: #365b8c;}
        QPushButton:disabled {color: %s;} QPushButton#contextLink {border: none; text-align:left; color: %s; padding: 3px;}
        QTreeWidget {border: none;} QTreeWidget::item {padding: 7px 2px;} QTreeWidget::item:selected {background: %s; color: %s;}
        QTabWidget::pane {border: none;} QTabBar::tab {padding: 7px 9px; border-bottom: 2px solid transparent;} QTabBar::tab:selected {border-bottom-color: #7197c4;}
        QSplitter::handle {background: transparent; width: 8px;} QMenu {border: 1px solid %s;} QMenu::item {padding: 7px 16px;} QMenu::item:selected {background: %s;}
        QScrollBar:vertical {width:8px; background:transparent;} QScrollBar::handle:vertical {background:%s; min-height:24px; border-radius:4px;} QScrollBar::add-line:vertical,QScrollBar::sub-line:vertical {height:0px;} QScrollBar::add-page:vertical,QScrollBar::sub-page:vertical {background:transparent;}
        '''%(bg,ink,ui_size,paper,muted,paper,border,selected,ink,paper,border,muted,muted,selected,ink,border,selected,border))
        body_font=QFont(a['family'],a['size']); chat_font=QFont(a.get('chat_family',a['family']),a.get('chat_size',a['size']))
        for w in [self.editor,self.plan_editor,self.summary_editor]:
            w.setFont(body_font); w.setStyleSheet('QPlainTextEdit {font-family:"%s";font-size:%dpt;padding:12px;}'%(a['family'].replace('"',''),a['size']))
        for doc in self.doc_cache.values(): doc.setDefaultFont(body_font)
        rule='font-family:"%s";font-size:%dpt;'%(a.get('chat_family',a['family']).replace('"',''),a.get('chat_size',a['size']))
        self.chat.setFont(chat_font); self.input.setFont(chat_font); self.prompt.setFont(chat_font)
        self.input.setStyleSheet('QPlainTextEdit {'+rule+'padding:8px;}'); self.prompt.setStyleSheet('QPlainTextEdit {'+rule+'}')
        self.chat.document().setDefaultStyleSheet('code, pre {font-family:"%s";}'%a['code_family'].replace('"',''))
        self.chat.picture=QPixmap(); self.chat.dark=dark; self.chat.mode=a['mode']; self.chat.shade=a['shade']
        if a['background']:
            try: self.chat.picture.loadFromData(self.user.read_blob(a['background']))
            except Exception: self.status.setText('背景图暂不可读，使用纯色背景。')
        self.chat.setStyleSheet('QTextBrowser {'+rule+'background:'+('transparent' if not self.chat.picture.isNull() else paper)+';padding:8px;}')
        self.html_cache.clear(); self.render()

    def lock(self):
        if self.pending_lock: return
        if not self.save_current(): return
        self.pending_lock=True; self.ready=False; self.autosave.stop(); self.context_timer.stop(); self.render_timer.stop()
        for dialog in self.findChildren(QDialog): dialog.reject()
        self.clear_sensitive_views(); self.centralWidget().hide()
        if self.busy: self.stop()
        else: self.close()

    def clear_sensitive_views(self):
        for edit in self.findChildren(QLineEdit): edit.clear()
        for edit in self.findChildren(QPlainTextEdit):
            # Clear detached cached documents too, not just currently visible editors.
            edit.clear()
        for doc in self.findChildren(QTextDocument): doc.clear()
        self.doc_cache.clear(); self.html_cache.clear(); self.chat.clear(); self.chat.picture=QPixmap()
        self.pending_task=None; self.document=None; self.chapter=None; self.last_result=None

    def closeEvent(self,event):
        if self.busy:
            if not self.pending_lock and not self.save_current(): event.ignore(); return
            self.close_requested=True; self.stop(); self.centralWidget().setEnabled(False); event.ignore(); return
        if not self.pending_lock:
            if not self.save_current(): event.ignore(); return
            self.exit_app=True
        self.ready=False; self.timer.stop(); self.autosave.stop(); self.context_timer.stop(); self.render_timer.stop(); QApplication.instance().removeEventFilter(self)
        for dialog in self.findChildren(QDialog): dialog.reject()
        self.clear_sensitive_views(); self.book=None; self.current=None; self.shown_sessions=[]; self.draft=[]
        self.nav.clear(); self.books.clear(); self.discussions.clear(); self.reference.clear(); self.user.close(); self.finished.emit(); event.accept()
