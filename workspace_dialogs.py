"""Focused candidate review and optional context inspection; no automatic publication."""
import json
from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QDialog,QVBoxLayout,QHBoxLayout,QSplitter,QPlainTextEdit,QLabel,QLineEdit,
    QPushButton,QDialogButtonBox,QTableWidget,QTableWidgetItem,QCheckBox,QHeaderView,QInputDialog)
from app import error
from drafting import check_target,parse_chapters


class CandidateDialog(QDialog):
    def __init__(self,parent,task,text):
        super().__init__(parent); self.user=parent.user; self.task=task; self.result_text=None; self.result_title=''; self.reuse_empty=False
        self.setWindowTitle('审阅候选 · '+task['label']); self.resize(min(920,max(560,parent.width()-40)),min(660,max(400,parent.height()-40)))
        layout=QVBoxLayout(self); self.notice=QLabel('目标：'+task['label']+'。确认前不会改变作品。'); self.notice.setWordWrap(True); layout.addWidget(self.notice)
        self.title=QLineEdit('新'+task.get('lore_kind','资料')); self.title.setPlaceholderText('资料名称'); self.title.setVisible(task['target']=='new_lore'); layout.addWidget(self.title)
        self.table=None; self.editor=None; self.original=None
        self.buttons=QDialogButtonBox(QDialogButtonBox.StandardButton.Ok|QDialogButtonBox.StandardButton.Cancel)
        labels={'field':'采用此修改','selection':'替换原选段','insert':'插入原位置','new_lore':'保存为作品资料','chapters':'确认章节规划','review':'根据意见准备修稿'}
        self.buttons.button(QDialogButtonBox.StandardButton.Ok).setText(labels[task['target']])
        self.valid=True
        try: book,doc=check_target(self.user.state,task)
        except ValueError as exc:
            self.notice.setText(str(exc)); self.valid=False; book=None; doc=None
        if task['target']=='chapters':
            try:
                rows=parse_chapters(text); self.table=QTableWidget(len(rows),4); self.table.setHorizontalHeaderLabels(['采用','卷','章名','本章安排'])
                self.table.horizontalHeader().setSectionResizeMode(3,QHeaderView.ResizeMode.Stretch)
                self.table.setColumnWidth(0,42); self.table.setColumnWidth(1,90); self.table.setColumnWidth(2,160)
                for i,row in enumerate(rows):
                    checked=QTableWidgetItem(); checked.setFlags(Qt.ItemFlag.ItemIsEnabled|Qt.ItemFlag.ItemIsUserCheckable); checked.setCheckState(Qt.CheckState.Checked); self.table.setItem(i,0,checked)
                    for j,key in enumerate(['volume','title','plan'],1): self.table.setItem(i,j,QTableWidgetItem(row[key]))
                    self.table.setRowHeight(i,60)
                self.table.cellDoubleClicked.connect(self.edit_plan)
                for i in range(self.table.rowCount()): self.table.item(i,3).setToolTip(self.table.item(i,3).text())
                layout.addWidget(self.table,1)
            except ValueError as exc:
                self.valid=False; self.notice.setText(str(exc)); self.editor=QPlainTextEdit(text); layout.addWidget(self.editor,1)
            self.reuse=QCheckBox('将首条计划写入现有空白占位章，其余章节追加（保留已有正文）')
            can_reuse=book and len(book['chapters'])==1 and not any(book['chapters'][0].get(k,'').strip() for k in ['body','plan','summary'])
            self.reuse.setEnabled(bool(can_reuse)); self.reuse.setChecked(bool(can_reuse)); layout.addWidget(self.reuse)
        else:
            split=QSplitter(); layout.addWidget(split,1)
            if task['target'] not in ['new_lore','review']:
                self.original=QPlainTextEdit(); self.original.setReadOnly(True)
                original=task.get('source_excerpt','')
                if doc is not None:
                    source=doc.get(task['field'],'')
                    original=source[task['start']:task['end']] if task['target']=='selection' else source
                self.original.setPlainText(self.user.redact(original)); split.addWidget(self.original)
                self.original.setPlaceholderText('原文（空白表示尚未填写）')
            self.editor=QPlainTextEdit(self.user.redact(text)); split.addWidget(self.editor)
            self.editor.setPlaceholderText('可在这里修改候选，再决定是否采用')
        layout.addWidget(self.buttons); self.buttons.accepted.connect(self.accept); self.buttons.rejected.connect(self.reject)
        self.buttons.button(QDialogButtonBox.StandardButton.Ok).setEnabled(self.valid)
        copy_button=QPushButton('复制候选'); self.buttons.addButton(copy_button,QDialogButtonBox.ButtonRole.ActionRole); copy_button.clicked.connect(self.copy_candidate)
    def edit_plan(self,row,column):
        if column!=3: return
        value,ok=QInputDialog.getMultiLineText(self,'编辑章节安排',self.table.item(row,2).text(),self.table.item(row,3).text())
        if ok: self.table.item(row,3).setText(value); self.table.item(row,3).setToolTip(value)

    def content(self):
        if self.table is not None:
            rows=[]
            for i in range(self.table.rowCount()):
                if self.table.item(i,0).checkState()!=Qt.CheckState.Checked: continue
                rows.append({key:self.table.item(i,j).text() for j,key in enumerate(['volume','title','plan'],1)})
            text=json.dumps({'chapters':rows},ensure_ascii=False); parse_chapters(text); return text
        return self.editor.toPlainText()
    def copy_candidate(self):
        from PySide6.QtWidgets import QApplication
        try: QApplication.clipboard().setText(self.user.redact(self.content()))
        except ValueError as exc: error(self,str(exc))
    def accept(self):
        try:
            check_target(self.user.state,self.task); self.result_text=self.user.redact(self.content())
            if not self.result_text.strip(): raise ValueError('候选内容为空。')
            self.result_title=self.user.redact(self.title.text().strip()); self.reuse_empty=bool(self.table is not None and self.reuse.isChecked())
            super().accept()
        except ValueError as exc: error(self,str(exc))
    def done(self,result):
        if result!=QDialog.DialogCode.Accepted: self.result_text=None
        for edit in self.findChildren(QPlainTextEdit): edit.clear()
        self.title.clear(); self.task=None
        super().done(result)
