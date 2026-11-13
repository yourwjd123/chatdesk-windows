"""Three-column fiction workspace; canonical documents are separate from AI candidates."""
import copy
import difflib
from datetime import datetime
from PySide6.QtCore import Qt,QTimer
from PySide6.QtGui import QFont,QTextCursor,QKeySequence,QShortcut
from PySide6.QtWidgets import (QWidget,QVBoxLayout,QHBoxLayout,QGridLayout,QFormLayout,QSplitter,QLabel,QPushButton,
    QComboBox,QLineEdit,QPlainTextEdit,QListWidget,QListWidgetItem,QTreeWidget,QTreeWidgetItem,QTabWidget,
    QDialog,QDialogButtonBox,QMessageBox,QInputDialog,QSpinBox,QCheckBox,QFileDialog)
from app import MainWindow,button,error,choose_save
from core import atomic_write
from novel import project,chapter,uid,checkpoint,restore,context_sources,assemble,exclude_message,remove_message,undo_remove,branch,candidate_branch

CHAPTER_FIELDS=['title','volume','body','plan','summary']
BOOK_FIELDS=['title','premise','outline','style']
LORE_FIELDS=['title','text','kind','pinned']


class ContextDialog(QDialog):
    def __init__(self,parent,entries,budget,selected=None):
        super().__init__(parent); self.entries=entries; self.bundle=None
        self.setWindowTitle('本次上下文 · 仅发送已勾选资料'); self.resize(1000,700)
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
        self.items.itemChanged.connect(self.rebuild); self.budget.valueChanged.connect(self.rebuild); self.rebuild()
    def selection(self): return {self.items.item(i).data(Qt.ItemDataRole.UserRole) for i in range(self.items.count()) if self.items.item(i).checkState()==Qt.CheckState.Checked}
    def rebuild(self,*_):
        try:
            self.bundle=assemble(self.entries,self.budget.value(),self.selection()); self.preview.setPlainText(self.bundle['text'])
            self.note.setText('已选择%d项 · %d字符。未选：%s。预算只限制作品资料；发送前仍会估算完整请求。'%(len(self.bundle['selected']),self.bundle['characters'],'、'.join(self.bundle['omitted']) or '无'))
        except ValueError as exc: self.bundle=None; self.preview.clear(); self.note.setText(str(exc))
    def auto(self):
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
        self.ready=False; self.book=None; self.chapter=None; self.document=None; self.doc_kind=None
        self.shown_sessions=[]; self.force_full=False
        super().__init__(user)
        old=self.takeCentralWidget(); oldlayout=old.layout()
        oldlayout.itemAt(0).layout()  # Keep old controls owned until reparented; unused wrappers are hidden.
        oldsplit=oldlayout.itemAt(1).widget(); assistant=oldsplit.widget(1)
        assistant.setParent(None)
        old.setParent(self); old.hide()
        # Replace dense rows with compact, explicit controls in the assistant column.
        ai_layout=assistant.layout()
        while ai_layout.count():
            item=ai_layout.takeAt(0)
            if item.widget(): item.widget().setParent(None)
            if item.layout():
                inner=item.layout()
                while inner.count():
                    child=inner.takeAt(0)
                    if child.widget(): child.widget().setParent(old)
        root=QWidget(); root.setObjectName('workbench'); self.setCentralWidget(root); layout=QVBoxLayout(root); layout.setContentsMargins(18,14,18,12); layout.setSpacing(12)
        header=QHBoxLayout(); layout.addLayout(header)
        brand=QLabel('墨舟  /  Novel Studio'); brand.setObjectName('brand'); header.addWidget(brand)
        subtitle=QLabel('把灵感写成作品'); subtitle.setObjectName('muted'); header.addWidget(subtitle); header.addStretch()
        button('设置',self.settings,header); button('锁定 / 切换用户',self.lock,header); header.addWidget(self.identity)
        self.workspace=QSplitter(); layout.addWidget(self.workspace,1)
        navigation=QWidget(); navigation.setObjectName('sidebar'); nl=QVBoxLayout(navigation); nl.setSpacing(10); self.workspace.addWidget(navigation)
        label=QLabel('作品书架'); label.setObjectName('section'); nl.addWidget(label)
        self.books=QComboBox(); nl.addWidget(self.books); self.books.currentIndexChanged.connect(self.switch_book)
        book_actions=QHBoxLayout(); nl.addLayout(book_actions)
        button('＋ 新建作品',self.create_book,book_actions); button('删除作品',self.delete_book,book_actions)
        self.nav=QTreeWidget(); self.nav.setHeaderHidden(True); self.nav.itemSelectionChanged.connect(self.select_nav); nl.addWidget(self.nav,1)
        row=QHBoxLayout(); nl.addLayout(row); button('＋ 章节',self.add_chapter,row); button('＋ 资料',self.add_lore,row)
        row=QHBoxLayout(); nl.addLayout(row); button('上移',lambda:self.move_chapter(-1),row); button('下移',lambda:self.move_chapter(1),row)
        button('删除当前章节 / 资料',self.delete_document,nl)
        editor_panel=QWidget(); editor_panel.setObjectName('paper'); el=QVBoxLayout(editor_panel); el.setContentsMargins(20,16,20,16); el.setSpacing(10); self.workspace.addWidget(editor_panel)
        self.breadcrumb=QLabel('正文工作区'); self.breadcrumb.setObjectName('muted'); el.addWidget(self.breadcrumb)
        self.doc_title=QLineEdit(); self.doc_title.setObjectName('documentTitle'); self.doc_title.setPlaceholderText('标题'); el.addWidget(self.doc_title)
        meta=QHBoxLayout(); el.addLayout(meta)
        self.volume=QLineEdit(); self.volume.setPlaceholderText('卷名，例如 第一卷'); meta.addWidget(self.volume)
        self.pinned=QCheckBox('固定加入上下文'); meta.addWidget(self.pinned)
        self.editor_tabs=QTabWidget(); el.addWidget(self.editor_tabs,1)
        self.editor=QPlainTextEdit(); self.editor.setPlaceholderText('在这里写下你的故事……'); self.editor_tabs.addTab(self.editor,'正文')
        self.plan_editor=QPlainTextEdit(); self.plan_editor.setPlaceholderText('本章目标、冲突、节奏与结尾……'); self.editor_tabs.addTab(self.plan_editor,'章节计划')
        self.summary_editor=QPlainTextEdit(); self.summary_editor.setPlaceholderText('人工确认的剧情摘要；AI不会自动写入。'); self.editor_tabs.addTab(self.summary_editor,'已确认摘要')
        row=QHBoxLayout(); el.addLayout(row); button('保存版本',self.save_version,row); button('版本 / 对比',self.versions,row); button('导出正文',self.export_manuscript,row); button('专注写作',self.focus_mode,row)
        self.count=QLabel('0 字符'); self.count.setObjectName('muted'); el.addWidget(self.count)
        ai_layout.setContentsMargins(14,12,14,12); ai_layout.setSpacing(8)
        label=QLabel('创作助手'); label.setObjectName('section'); ai_layout.addWidget(label)
        ai_layout.addWidget(self.sessions); self.sessions.setMaximumHeight(58)
        row=QHBoxLayout(); ai_layout.addLayout(row); button('新讨论',self.new,row); button('改名',self.rename,row); button('删除讨论',self.delete,row)
        ai_layout.addWidget(self.profile)
        row=QHBoxLayout(); ai_layout.addLayout(row); button('背景提示词 / 参数',self.chat_parameters,row); button('检查上下文',self.preview_context,row)
        self.prompt.setParent(old)
        for w in self.params.values(): w.setParent(old)
        self.style.setParent(old)
        tasks=QGridLayout(); ai_layout.addLayout(tasks)
        for i,name in enumerate(['续写','润色选段','扩写选段','压缩选段','总结本章','检查矛盾']):
            b=QPushButton(name); b.clicked.connect(lambda checked=False,n=name:self.task(n)); tasks.addWidget(b,i//3,i%3)
        ai_layout.addWidget(self.chat,1)
        self.reply_list=QListWidget(); self.reply_list.setMaximumHeight(65); ai_layout.addWidget(self.reply_list)
        history_actions=QGridLayout(); ai_layout.addLayout(history_actions)
        for i,(label,fn) in enumerate([('排除 / 恢复',self.exclude_reply),('删除回复',self.delete_reply),('撤销删除',self.undo_reply),('从此处分支',self.branch_here),('另一候选',self.regenerate),('采纳候选',self.adopt)]):
            b=QPushButton(label); b.clicked.connect(fn); history_actions.addWidget(b,i//3,i%3)
        ai_layout.addWidget(self.attachment_list); self.attachment_list.setMaximumHeight(65)
        row=QHBoxLayout(); ai_layout.addLayout(row); button('附件',self.attach,row); button('预览',self.preview_attachment,row); button('附件库',self.manage_attachments,row)
        ai_layout.addWidget(self.input); self.input.setMaximumHeight(90); self.input.setPlaceholderText('讨论剧情，或选中文本后使用上方创作操作……')
        row=QHBoxLayout(); ai_layout.addLayout(row); send=button('发送  Ctrl+Enter',self.send,row); send.setObjectName('primary'); button('停止',self.stop,row)
        row=QHBoxLayout(); ai_layout.addLayout(row); button('导出回答',lambda:self.export(True),row); button('导出讨论',lambda:self.export(False),row)
        assistant.setObjectName('assistant'); self.workspace.addWidget(assistant); self.workspace.setSizes([210,660,440])
        layout.addWidget(self.status)
        self.resize(1450,960); self.setMinimumSize(1180,800)
        self.autosave=QTimer(self); self.autosave.setSingleShot(True); self.autosave.timeout.connect(self.autosave_document)
        for w in [self.editor,self.plan_editor,self.summary_editor]: w.textChanged.connect(self.schedule_save)
        self.doc_title.textChanged.connect(self.schedule_save); self.volume.textChanged.connect(self.schedule_save); self.pinned.toggled.connect(self.schedule_save)
        self.save_shortcut=QShortcut(QKeySequence('Ctrl+S'),self); self.save_shortcut.activated.connect(self.save_version)
        if not self.user.state['projects']: self.user.state['projects'].append(project('我的第一部小说'))
        self.ready=True; self.refresh_books(); self.books.setCurrentIndex(0); self.switch_book(0); self.apply_appearance(); self.update_title()

    def update_title(self):
        super().update_title()
        self.setWindowTitle('墨舟 Novel Studio 0.3 · '+self.user.name)
    def refresh_books(self):
        active=self.book['id'] if self.book else None
        self.books.blockSignals(True); self.books.clear()
        for p in self.user.state['projects']: self.books.addItem(self.user.redact(p['title']),p['id'])
        if any(not s.get('project_id') for s in self.user.state['sessions']): self.books.addItem('未归档对话',None)
        idx=self.books.findData(active)
        if idx>=0: self.books.setCurrentIndex(idx)
        self.books.blockSignals(False)
    def switch_book(self,index):
        if not self.ready or self.busy or index<0: return
        if not self.save_current(): return
        pid=self.books.itemData(index); self.book=next((p for p in self.user.state['projects'] if p['id']==pid),None)
        self.chapter=self.book['chapters'][0] if self.book and self.book['chapters'] else None
        self.document=None; self.refresh_nav()
        if self.chapter: self.show_document('chapter',self.chapter)
        elif self.book: self.show_document('book',self.book)
        else: self.show_document(None,None)
        self.refresh_sessions()
        if self.shown_sessions: self.load(self.shown_sessions[0])
        else: self.current=None; self.new()
        self.persist()
    def refresh_sessions(self):
        if not getattr(self,'ready',False): return super().refresh_sessions()
        self.shown_sessions=[s for s in self.user.state['sessions'] if s.get('project_id')==(self.book['id'] if self.book else None)]
        self.sessions.blockSignals(True); self.sessions.clear()
        for s in self.shown_sessions: self.sessions.addItem(self.user.redact(s['title']))
        self.sessions.blockSignals(False)
    def switch(self,index):
        if not getattr(self,'ready',False): return super().switch(index)
        if self.busy or index<0 or index>=len(self.shown_sessions) or not self.save_current(): return
        self.load(self.shown_sessions[index])
    def new(self):
        if self.busy: return
        if not getattr(self,'ready',False): return super().new()
        if not self.save_current(): return
        from storage import default_session
        s=default_session(self.user.state['defaults']); s.update(project_id=self.book['id'] if self.book else None,chapter_id=self.chapter['id'] if self.chapter else None,profile=self.profile.currentText())
        if self.chapter: s['title']=self.chapter['title']+' · 讨论'
        self.user.state['sessions'].append(s); self.current=s; self.persist(); self.refresh_sessions(); self.load(s)
    def load(self,session):
        if getattr(self,'ready',False) and self.book and session.get('chapter_id'):
            linked=next((c for c in self.book['chapters'] if c['id']==session['chapter_id']),None)
            if linked and (not self.chapter or linked['id']!=self.chapter['id']):
                self.chapter=linked
                self.show_document('chapter',linked)
        super().load(session)

    def delete(self):
        if self.busy or not self.current: return
        if QMessageBox.question(self,'删除讨论','删除当前讨论？正文、章节摘要和作品资料不受影响。')!=QMessageBox.StandardButton.Yes: return
        self.user.state['sessions'].remove(self.current); self.current=None; self.refresh_sessions()
        if self.shown_sessions: self.load(self.shown_sessions[0])
        else: self.new()
        self.persist()
    def save_current(self):
        if getattr(self,'ready',False):
            if not self.capture_document(): return False
        return super().save_current()
    def create_book(self):
        if self.busy: return
        name,ok=QInputDialog.getText(self,'新建作品','作品名称：')
        if not ok or not name.strip() or not self.save_current(): return
        p=project(self.user.redact(name[:100])); self.user.state['projects'].append(p); self.persist(); self.refresh_books(); self.books.setCurrentIndex(self.books.findData(p['id']))
    def delete_book(self):
        if self.busy or not self.book: return
        if QMessageBox.question(self,'删除作品','删除这部作品的全部章节、资料、版本和讨论？附件库保留。此操作不能撤销，请先备份。')!=QMessageBox.StandardButton.Yes: return
        identity=self.book['id']
        self.user.state['projects']=[p for p in self.user.state['projects'] if p['id']!=identity]
        self.user.state['sessions']=[s for s in self.user.state['sessions'] if s.get('project_id')!=identity]
        self.current=None; self.book=None; self.chapter=None; self.document=None
        if not self.user.state['projects']: self.user.state['projects'].append(project('新作品'))
        self.persist(); self.refresh_books(); self.books.setCurrentIndex(0); self.switch_book(0)

    def refresh_nav(self):
        self.nav.blockSignals(True); self.nav.clear()
        if self.book:
            item=QTreeWidgetItem(['作品总纲与风格']); item.setData(0,Qt.ItemDataRole.UserRole,('book',self.book['id'])); self.nav.addTopLevelItem(item)
            volumes={}
            for c in self.book['chapters']:
                v=c.get('volume','') or '未分卷'
                if v not in volumes: volumes[v]=QTreeWidgetItem([v]); self.nav.addTopLevelItem(volumes[v])
                item=QTreeWidgetItem([self.user.redact(c['title'])]); item.setData(0,Qt.ItemDataRole.UserRole,('chapter',c['id'])); volumes[v].addChild(item)
            groups={}
            for record in self.book['lore']:
                k=record['kind']
                if k not in groups: groups[k]=QTreeWidgetItem([k]); self.nav.addTopLevelItem(groups[k])
                item=QTreeWidgetItem([self.user.redact(record['title'])]); item.setData(0,Qt.ItemDataRole.UserRole,('lore',record['id'])); groups[k].addChild(item)
            self.nav.expandAll()
        self.nav.blockSignals(False)
    def select_nav(self):
        if not self.ready or self.busy: return
        items=self.nav.selectedItems()
        if not items: return
        data=items[0].data(0,Qt.ItemDataRole.UserRole)
        if not data or not self.capture_document(): return
        kind,identity=data
        doc=self.book if kind=='book' else next((d for d in self.book['chapters' if kind=='chapter' else 'lore'] if d['id']==identity),None)
        changed=kind=='chapter' and (not self.chapter or self.chapter['id']!=doc['id'])
        if changed and not self.save_current(): return
        self.show_document(kind,doc)
        if changed:
            relevant=next((s for s in self.shown_sessions if s.get('chapter_id')==doc['id']),None)
            if relevant: self.load(relevant)
            else: self.new()
    def show_document(self,kind,doc):
        self.autosave.stop(); self.document=doc; self.doc_kind=kind
        for w in [self.doc_title,self.volume,self.editor,self.plan_editor,self.summary_editor,self.pinned]: w.blockSignals(True)
        self.doc_title.setText(doc['title'] if doc else '')
        self.volume.setVisible(kind=='chapter'); self.pinned.setVisible(kind=='lore')
        self.volume.setText(doc.get('volume','') if doc else ''); self.pinned.setChecked(doc.get('pinned',False) if doc else False)
        self.editor.setPlainText(doc.get('body' if kind=='chapter' else 'outline' if kind=='book' else 'text','') if doc else '')
        self.plan_editor.setPlainText(doc.get('plan' if kind=='chapter' else 'premise','') if doc else '')
        self.summary_editor.setPlainText(doc.get('summary' if kind=='chapter' else 'style','') if doc else '')
        for i,title in enumerate(['正文','章节计划','已确认摘要'] if kind=='chapter' else ['作品总纲','作品简介','风格与约束']): self.editor_tabs.setTabText(i,title)
        self.editor_tabs.setTabVisible(1,kind!='lore'); self.editor_tabs.setTabVisible(2,kind!='lore')
        self.editor_tabs.setCurrentIndex(0)
        if kind=='chapter': self.chapter=doc
        self.breadcrumb.setText((self.book['title']+'  /  ' if self.book else '')+('章节正文' if kind=='chapter' else '作者确认的作品资料' if doc else '选择作品开始创作'))
        for w in [self.doc_title,self.volume,self.editor,self.plan_editor,self.summary_editor,self.pinned]: w.blockSignals(False); w.setEnabled(bool(doc))
        self.update_count()
    def capture_document(self):
        if not self.document: return True
        d=self.document; title=self.user.redact(self.doc_title.text().strip())
        d['title']=title or '未命名'
        if self.doc_kind=='chapter': d.update(volume=self.volume.text().strip(),body=self.user.redact(self.editor.toPlainText()),plan=self.user.redact(self.plan_editor.toPlainText()),summary=self.user.redact(self.summary_editor.toPlainText()))
        elif self.doc_kind=='book': d.update(outline=self.user.redact(self.editor.toPlainText()),premise=self.user.redact(self.plan_editor.toPlainText()),style=self.user.redact(self.summary_editor.toPlainText()))
        else: d.update(text=self.user.redact(self.editor.toPlainText()),pinned=self.pinned.isChecked())
        return True
    def schedule_save(self,*_):
        if self.ready: self.update_count(); self.autosave.start(1500)
    def update_count(self): self.count.setText('%s · %d 字符 · Ctrl+S 保存版本'%('正文' if self.doc_kind=='chapter' else '资料',len(self.editor.toPlainText())))
    def autosave_document(self):
        if not self.ready or self.pending_lock: return
        self.capture_document()
        if self.persist(): self.status.setText('已加密自动保存草稿 · 正式版本请点击“保存版本”')
    def fields(self): return CHAPTER_FIELDS if self.doc_kind=='chapter' else BOOK_FIELDS if self.doc_kind=='book' else LORE_FIELDS
    def save_version(self):
        if not self.ready or not self.document: return
        self.autosave.stop(); self.capture_document(); checkpoint(self.document,self.fields())
        if self.persist(): self.refresh_nav(); self.refresh_books(); self.status.setText('版本已保存 · 保留最近50个手动版本')
    def add_chapter(self):
        if self.busy or not self.book: return
        self.capture_document(); c=chapter('第%d章'%(len(self.book['chapters'])+1)); self.book['chapters'].append(c)
        self.persist(); self.refresh_nav(); self.show_document('chapter',c); self.new()
    def add_lore(self):
        if self.busy or not self.book: return
        kind,ok=QInputDialog.getItem(self,'新增作品资料','资料类型：',['人物','世界观','关系与地点','时间线','剧情状态','伏笔'],0,False)
        if not ok: return
        self.capture_document(); item=dict(id=uid(),title='新'+kind,kind=kind,text='',pinned=False,versions=[])
        item['checkpoint']={k:item[k] for k in LORE_FIELDS}
        self.book['lore'].append(item); self.persist(); self.refresh_nav(); self.show_document('lore',item)
    def move_chapter(self,delta):
        if self.busy or self.doc_kind!='chapter': return
        self.capture_document(); chapters=self.book['chapters']; i=chapters.index(self.document); j=i+delta
        if 0<=j<len(chapters): chapters[i],chapters[j]=chapters[j],chapters[i]; self.persist(); self.refresh_nav()
    def delete_document(self):
        if self.busy or not self.document or self.doc_kind=='book': return
        if QMessageBox.question(self,'删除章节或资料','删除当前文档及其版本？该操作不能撤销，建议先备份。')!=QMessageBox.StandardButton.Yes: return
        collection=self.book['chapters' if self.doc_kind=='chapter' else 'lore']; collection.remove(self.document)
        self.chapter=self.book['chapters'][0] if self.book['chapters'] else None
        self.document=None; self.persist(); self.refresh_nav(); self.show_document('chapter',self.chapter) if self.chapter else self.show_document('book',self.book)
    def versions(self):
        if not self.document: return
        self.capture_document(); dialog=QDialog(self); dialog.setWindowTitle('文档版本 · 恢复前保留当前内容'); dialog.resize(900,600); layout=QVBoxLayout(dialog)
        items=QListWidget(); layout.addWidget(items)
        for v in self.document.get('versions',[]): items.addItem(datetime.fromisoformat(v['at']).astimezone().strftime('%Y-%m-%d %H:%M:%S')+' · '+v['data'].get('title','旧版本'))
        diff=QPlainTextEdit(); diff.setReadOnly(True); layout.addWidget(diff,1)
        def compare(index):
            if index<0: return
            old=self.document['versions'][index]['data']; current={k:self.document.get(k,'') for k in self.fields()}
            before='\n'.join(str(old.get(k,'')) for k in self.fields()); after='\n'.join(str(current.get(k,'')) for k in self.fields())
            diff.setPlainText(''.join(difflib.unified_diff(before.splitlines(True),after.splitlines(True),fromfile='历史版本',tofile='当前草稿')))
        items.currentRowChanged.connect(compare)
        def recover():
            if items.currentRow()<0: return
            restore(self.document,items.currentRow(),self.fields()); self.persist(); self.show_document(self.doc_kind,self.document); self.refresh_nav(); dialog.accept()
        button('恢复选中版本',recover,layout); dialog.exec()
    def focus_mode(self):
        focused=self.workspace.widget(0).isVisible()
        self.workspace.widget(0).setVisible(not focused); self.workspace.widget(2).setVisible(not focused)
    def export_manuscript(self):
        if not self.book or not self.save_current(): return
        try:
            path=choose_save(self,'另存本作品全部章节','Markdown (*.md);;文本 (*.txt)')
            if not path: return
            if path.suffix.lower() not in ['.md','.txt']: raise ValueError('请选择TXT或MD。')
            md=path.suffix.lower()=='.md'; text=('# ' if md else '')+self.book['title']+'\n\n'
            for c in self.book['chapters']: text+=('## ' if md else '')+c.get('volume','')+' · '+c['title']+'\n\n'+c['body']+'\n\n'
            atomic_write(path,self.user.redact(text).encode('utf-8')); self.status.setText('正文已另存为：'+str(path))
        except ValueError as exc: error(self,str(exc))
        except Exception: error(self)
    def settings(self):
        if self.busy: return
        bid=self.book['id'] if self.book else None
        cid=self.chapter['id'] if self.chapter else None
        kind=self.doc_kind; did=self.document['id'] if self.document else None
        super().settings()
        if self.user.closed: return
        self.book=next((b for b in self.user.state['projects'] if b['id']==bid),None)
        self.chapter=next((c for c in self.book['chapters'] if c['id']==cid),None) if self.book else None
        doc=self.book if kind=='book' else next((d for d in self.book['chapters' if kind=='chapter' else 'lore'] if d['id']==did),None) if self.book else None
        self.show_document(kind,doc); self.refresh_books(); self.refresh_nav(); self.apply_appearance()

    def chat_parameters(self):
        if self.busy: return
        dialog=QDialog(self); dialog.setWindowTitle('本讨论的背景提示词与生成参数'); layout=QFormLayout(dialog); layout.addRow('背景提示词',self.prompt)
        for k,label in [('temperature','温度'),('tokens','最大输出token'),('chars','期望字符（0不限）'),('rounds','保留历史轮数')]: layout.addRow(label,self.params[k])
        layout.addRow('遵循预设',self.style); button('完成',dialog.accept,layout)
        for w in [self.prompt,self.style,*self.params.values()]: w.show()
        dialog.exec()
        for w in [self.prompt,self.style,*self.params.values()]: w.setParent(self); w.hide()
        self.save_current()
    def context_dialog(self):
        if not self.book: return None
        self.capture_document(); entries=context_sources(self.book,self.chapter,self.input.toPlainText())
        selected=self.current.get('context_selected')
        if self.force_full and self.chapter:
            try: selected=assemble(entries,self.user.state.get('context_budget',32000))['selected']
            except ValueError: selected={e['id'] for e in entries if e['required']}
            selected=set(selected); selected.discard('tail:'+self.chapter['id']); selected.add('body:'+self.chapter['id'])
        return ContextDialog(self,entries,self.user.state.get('context_budget',32000),selected)
    def preview_context(self):
        if self.busy: return
        dialog=self.context_dialog()
        if not dialog: error(self,'请先选择小说作品。'); return
        if dialog.exec()==QDialog.DialogCode.Accepted:
            self.current['context_selected']=list(dialog.bundle['selected']); self.user.state['context_budget']=dialog.budget.value(); self.persist()
        dialog.entries=[]; dialog.bundle=None
    def send(self):
        if self.busy: return
        if not self.input.toPlainText().strip() and not self.draft: return
        if self.book:
            linked=self.current.get('chapter_id')
            if linked and not any(c['id']==linked for c in self.book['chapters']):
                error(self,'当前讨论关联的章节已删除，请为现有章节新建讨论。'); return
            dialog=self.context_dialog()
            if dialog.exec()!=QDialog.DialogCode.Accepted: self.force_full=False; return
            self.current['context_bundle']=self.user.redact(dialog.bundle['text']); self.current['context_selected']=list(dialog.bundle['selected']); self.user.state['context_budget']=dialog.budget.value()
            dialog.entries=[]; dialog.bundle=None
        else: self.current.pop('context_bundle',None)
        self.force_full=False; super().send(); self.busy_controls()
    def task(self,name):
        if self.busy or not self.chapter: return
        self.capture_document(); selected=self.editor.textCursor().selectedText().replace('\u2029','\n') if self.doc_kind=='chapter' else ''
        if name in ['润色选段','扩写选段','压缩选段'] and not selected: error(self,'请先在章节正文中选中文本。'); return
        prompts={'续写':'按照本章计划与正文末尾继续写作，保持人物、视角与时间线一致。只输出候选正文，不改变正式设定。',
        '润色选段':'润色以下选段，保留事实与叙事视角，仅返回候选文本：\n'+selected,
        '扩写选段':'扩写以下选段，不自行新增关键剧情事实，仅返回候选文本：\n'+selected,
        '压缩选段':'压缩以下选段，保留关键事件，仅返回候选文本：\n'+selected,
        '总结本章':'根据当前章节完整正文，生成章节摘要，并另列人物状态变化、时间线与未回收伏笔。全部为待作者确认的建议，不自动成为正式资料。',
        '检查矛盾':'检查当前章节完整正文与已提供总纲、人物、摘要之间的矛盾。逐条引用资料名称及对应原文；区分确定冲突与待确认疑点，不自行修正文稿。'}
        self.input.setPlainText(prompts[name]); self.force_full=name in ['总结本章','检查矛盾']; self.send()
    def selected_reply(self):
        item=self.reply_list.currentItem()
        return item.data(Qt.ItemDataRole.UserRole) if item else None
    def render(self):
        super().render()
        if not getattr(self,'ready',False) or not hasattr(self,'reply_list'): return
        selected=self.selected_reply(); self.reply_list.clear()
        for i,m in enumerate(self.current['messages']):
            label=('你' if m['role']=='user' else '助手')+(' · 已排除' if m.get('excluded') else '')+(' · 未完成' if m.get('status','complete')!='complete' else '')
            item=QListWidgetItem('%d  %s  %s'%(i+1,label,self.user.redact(m['content'][:35]).replace('\n',' '))); item.setData(Qt.ItemDataRole.UserRole,i); self.reply_list.addItem(item)
        self.reply_list.setVisible(self.reply_list.count()>0)
        if self.reply_list.count(): self.reply_list.setCurrentRow(selected if selected is not None and selected<self.reply_list.count() else self.reply_list.count()-1)
    def exclude_reply(self):
        index=self.selected_reply()
        if self.busy or index is None: return
        exclude_message(self.current,index); self.current.pop('context_bundle',None); self.persist(); self.render()
        self.status.setText('已更新上下文范围；后续已有回答不会因此重写，需要纠错时请创建分支。')
    def delete_reply(self):
        index=self.selected_reply()
        if self.busy or index is None: return
        remove_message(self.current,index); self.current.pop('context_bundle',None); self.persist(); self.render()
    def undo_reply(self):
        if self.busy: return
        undo_remove(self.current); self.persist(); self.render()
    def open_branch(self,s):
        self.user.state['sessions'].append(s); self.persist(); self.refresh_sessions(); self.load(s)
    def branch_here(self):
        index=self.selected_reply()
        if not self.busy and index is not None and self.save_current(): self.open_branch(branch(self.current,index))
    def regenerate(self):
        index=self.selected_reply()
        if self.busy or index is None: return
        if not self.save_current(): return
        try: self.open_branch(candidate_branch(self.current,index)); self.status.setText('已创建独立候选分支；可修改问题后发送。原分支保留。')
        except ValueError as exc: error(self,str(exc))
    def adopt(self):
        index=self.selected_reply()
        if self.busy or index is None or not self.book: return
        message=self.current['messages'][index]
        if message['role']!='assistant' or message.get('status','complete')!='complete': error(self,'请选择一条已完成的助手回复。'); return
        dialog=QDialog(self); dialog.setWindowTitle('采纳候选 · 确认后才写入作品'); dialog.resize(760,620); layout=QVBoxLayout(dialog)
        target=QComboBox(); target.addItems(['插入章节正文','替换选中的正文','替换整章正文','存为本章摘要','新增剧情状态','新增伏笔','新增人物资料']); layout.addWidget(target)
        text=QPlainTextEdit(message['content']); layout.addWidget(text,1)
        title=QLineEdit('待命名资料'); layout.addWidget(title)
        buttons=QDialogButtonBox(QDialogButtonBox.StandardButton.Ok|QDialogButtonBox.StandardButton.Cancel); layout.addWidget(buttons); buttons.accepted.connect(dialog.accept); buttons.rejected.connect(dialog.reject)
        if dialog.exec()!=QDialog.DialogCode.Accepted: text.clear(); return
        content=self.user.redact(text.toPlainText()); text.clear(); choice=target.currentIndex()
        self.save_version()
        if choice<=3:
            if not self.chapter: error(self,'请先新建章节。'); return
            if choice==1 and (self.doc_kind!='chapter' or not self.editor.textCursor().hasSelection()): error(self,'请先在正文中选中要替换的文本。'); return
            c=self.chapter
            if choice<=1:
                if self.doc_kind!='chapter': self.show_document('chapter',c)
                self.editor.textCursor().insertText(content); self.capture_document()
            elif choice==2: c['body']=content
            else: c['summary']=content
            checkpoint(c,CHAPTER_FIELDS); self.show_document('chapter',c)
        else:
            record=dict(id=uid(),title=self.user.redact(title.text().strip()) or '新资料',kind={4:'剧情状态',5:'伏笔',6:'人物'}[choice],text=content,pinned=False,versions=[])
            record['checkpoint']={k:record[k] for k in LORE_FIELDS}; self.book['lore'].append(record); self.show_document('lore',record)
        self.persist(); self.refresh_nav(); self.status.setText('候选已由作者确认写入作品；未自动修改其他资料。')
    def busy_controls(self):
        if not self.ready: return
        self.nav.setEnabled(not self.busy); self.books.setEnabled(not self.busy)
        for w in [self.editor,self.plan_editor,self.summary_editor]: w.setReadOnly(self.busy)
    def worker_finished(self):
        super().worker_finished()
        if not self.user.closed: self.busy_controls()
    def apply_appearance(self):
        super().apply_appearance()
        if not getattr(self,'ready',False): return
        a=self.user.state['appearance']; dark=a['theme']=='深色'
        bg='#111923' if dark else '#eef1f5'; paper='#1b2736' if dark else '#ffffff'; ink='#e6edf5' if dark else '#203047'; muted='#91a3b9' if dark else '#748398'; border='#304156' if dark else '#dce3eb'
        self.setStyleSheet('''QWidget {background: %s; color: %s; font-family: "Microsoft YaHei"; font-size: 12px;}
        QWidget#paper,QWidget#assistant,QWidget#sidebar {background: %s; border-radius: 12px;}
        QLabel {background: transparent;} QLabel#brand {font-size: 21px; font-weight: 700; letter-spacing: 1px;}
        QLabel#muted {color: %s;} QLabel#section {font-size: 15px; font-weight: 600;}
        QLineEdit,QPlainTextEdit,QListWidget,QTreeWidget,QComboBox,QSpinBox,QDoubleSpinBox {background: %s; border: 1px solid %s; border-radius: 6px; padding: 5px; selection-background-color: #d5e6fa; selection-color: #18375f;}
        QPlainTextEdit {padding: 14px; line-height: 1.8;} QLineEdit#documentTitle {font-size: 24px; font-weight: 600; border: none; padding: 6px 0;}
        QPushButton {background: %s; border: 1px solid %s; border-radius: 6px; padding: 7px 9px;} QPushButton:hover {border-color: #7196c6; background: %s;} QPushButton:disabled {color: %s;}
        QPushButton#primary {background: #386caa; color: white; border-color: #386caa; font-weight: 600;}
        QTabWidget::pane {border: none;} QTabBar::tab {padding: 9px 12px; border-bottom: 2px solid transparent;} QTabBar::tab:selected {color: #5289c5; border-bottom-color: #5289c5;}
        QSplitter::handle {background: transparent; width: 10px;} QTreeWidget::item {padding: 7px 2px;} QScrollBar:vertical {width: 8px; background: transparent;} QScrollBar::handle:vertical {background: %s; border-radius: 4px; min-height: 24px;}
        '''%(bg,ink,paper,muted,paper,border,paper,border,bg,muted,border))
        font=QFont(a['family'],a['size'])
        for w in [self.editor,self.plan_editor,self.summary_editor]: w.setFont(font); w.setStyleSheet('font-family: "%s"; font-size: %dpt;'%(a['family'].replace('"',''),a['size']))
        self.chat.setFont(font)
        rule='font-family: "%s"; font-size: %dpt;'%(a['family'].replace('"',''),a['size'])
        self.chat.setStyleSheet(self.chat.styleSheet()+' QTextBrowser {'+rule+'}')
        for w in [self.input,self.prompt]: w.setStyleSheet('QPlainTextEdit {'+rule+'}')
    def lock(self):
        if self.ready:
            self.autosave.stop()
            if self.busy:
                self.capture_document()
                if not self.persist(): return
        super().lock()
        if self.pending_lock:
            self.ready=False
            for w in self.findChildren(QPlainTextEdit): w.clear()
    def closeEvent(self,event):
        if self.ready and not self.busy:
            self.autosave.stop(); self.capture_document()
        super().closeEvent(event)
        if self.user.closed:
            self.ready=False; self.book=None; self.chapter=None; self.document=None; self.shown_sessions=[]
            self.nav.clear(); self.books.clear()
            for w in self.findChildren(QPlainTextEdit): w.clear()
