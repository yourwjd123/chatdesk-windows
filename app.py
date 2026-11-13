import os
import sys
sys.dont_write_bytecode = True
import json
import queue
import threading
import uuid
from pathlib import Path
import tkinter as tk
from tkinter import ttk, filedialog, messagebox, simpledialog
from core import Vault, Store, allowed_path, atomic_write, endpoint, stream_chat, export_text


class App:
    def __init__(self, root, folder, vault):
        self.root, self.folder, self.vault = root, folder, vault
        self.store = Store(folder, vault)
        self.current = None
        self.running = False
        self.events = queue.Queue()
        self.stop_event = threading.Event()
        root.title('ChatDesk 验证版 0.1 · 本地多轮对话')
        root.geometry('1120x820')
        root.minsize(900, 650)
        outer = ttk.Panedwindow(root, orient='horizontal')
        outer.pack(fill='both', expand=True, padx=10, pady=10)
        left = ttk.Frame(outer, width=190)
        outer.add(left, weight=1)
        ttk.Label(left, text='会话').pack(anchor='w')
        self.sessions = tk.Listbox(left, exportselection=False, width=24)
        self.sessions.pack(fill='both', expand=True)
        self.sessions.bind('<<ListboxSelect>>', self.switch)
        for label, command in [('新建会话', self.new), ('重命名', self.rename), ('删除会话', self.delete), ('API 配置 / 密钥', self.configure)]:
            ttk.Button(left, text=label, command=command).pack(fill='x', pady=3)
        right = ttk.Frame(outer)
        outer.add(right, weight=5)
        self.profile = tk.StringVar()
        ttk.Label(right, text='API 配置（仅支持 OpenAI 兼容聊天接口）').pack(anchor='w')
        self.profiles = ttk.Combobox(right, textvariable=self.profile, state='readonly')
        self.profiles.pack(fill='x')
        self.refresh_profiles()
        ttk.Label(right, text='本会话背景提示词').pack(anchor='w', pady=(6,0))
        self.system = tk.Text(right, height=3, wrap='word')
        self.system.pack(fill='x')
        params = ttk.Frame(right)
        params.pack(fill='x', pady=5)
        self.params = {}
        for name, label, default, width in [('temperature','温度',0.7,5), ('tokens','最大 token',1024,7), ('chars','期望字符（0不限）',0,7), ('rounds','保留最近轮数',10,5)]:
            ttk.Label(params, text=label).pack(side='left', padx=(4,2))
            var = tk.StringVar(value=str(default))
            ttk.Entry(params, textvariable=var, width=width).pack(side='left')
            self.params[name] = var
        self.style = tk.StringVar(value='平衡')
        ttk.Combobox(right, textvariable=self.style, values=['严格','平衡','自由'], state='readonly', width=12).pack(anchor='w')
        ttk.Label(right, text='遵循预设是提示词引导；期望字符数非硬限制。较早历史超过保留轮数后不发送。').pack(anchor='w')
        frame = ttk.Frame(right)
        frame.pack(fill='both', expand=True, pady=5)
        self.chat = tk.Text(frame, wrap='word', state='disabled')
        scroll = ttk.Scrollbar(frame, command=self.chat.yview)
        self.chat.configure(yscrollcommand=scroll.set)
        scroll.pack(side='right', fill='y')
        self.chat.pack(fill='both', expand=True)
        self.input = tk.Text(right, height=4, wrap='word')
        self.input.pack(fill='x')
        self.input.bind('<Control-Return>', lambda _: self.send())
        buttons = ttk.Frame(right)
        buttons.pack(fill='x', pady=5)
        for label, command in [('发送 Ctrl+Enter',self.send), ('停止生成',self.stop), ('保存设置',self.save_current), ('另存最近回答',lambda:self.export(True)), ('另存完整对话',lambda:self.export(False))]:
            ttk.Button(buttons, text=label, command=command).pack(side='left', padx=2)
        self.status = tk.StringVar(value='就绪 · 数据目录：' + str(folder))
        ttk.Label(root, textvariable=self.status).pack(fill='x', padx=10, pady=(0,8))
        self.refresh_sessions()
        if self.store.data['sessions']:
            self.load(self.store.data['sessions'][0])
        else:
            self.new()
        root.protocol('WM_DELETE_WINDOW', self.close)
        root.after(80, self.poll)

    def refresh_profiles(self):
        names = [p['name'] for p in self.store.data['profiles']]
        self.profiles['values'] = names
        if self.profile.get() not in names:
            self.profile.set(names[0] if names else '')

    def refresh_sessions(self):
        self.sessions.delete(0,'end')
        for s in self.store.data['sessions']:
            self.sessions.insert('end',s['title'])

    def new(self):
        if self.running: return
        if self.current and not self.save_current(): return
        s = dict(id=uuid.uuid4().hex, title='新会话', system='你是一位严谨、友好的助手。',
                 temperature=0.7, tokens=1024, chars=0, rounds=10, style='平衡', messages=[], profile=self.profile.get())
        self.store.data['sessions'].append(s)
        self.current = s
        self.persist()
        self.refresh_sessions()
        self.load(s)

    def load(self,s):
        self.current = s
        self.system.delete('1.0','end'); self.system.insert('1.0',s['system'])
        for k,v in self.params.items(): v.set(str(s[k]))
        self.style.set(s['style'])
        self.profile.set(s.get('profile',self.store.data['profiles'][0]['name']))
        self.render()

    def switch(self,_):
        if self.running:
            self.status.set('生成期间请先停止，再切换会话。'); return
        selected = self.sessions.curselection()
        if selected and self.save_current(): self.load(self.store.data['sessions'][selected[0]])

    def persist(self):
        try:
            self.store.save()
            return True
        except Exception:
            messagebox.showerror('保存失败','指定数据目录不可写或空间不足。未改用其他目录，请恢复该目录后重试。')
            return False

    def save_current(self):
        if not self.current: return True
        if self.running:
            self.status.set('生成期间不能修改本次请求设置。'); return False
        try:
            values = dict(temperature=float(self.params['temperature'].get()), tokens=int(self.params['tokens'].get()),
                          chars=int(self.params['chars'].get()), rounds=int(self.params['rounds'].get()))
            if not (0 <= values['temperature'] <= 2 and 1 <= values['tokens'] <= 131072 and 0 <= values['chars'] <= 1000000 and 1 <= values['rounds'] <= 500): raise ValueError()
        except ValueError:
            messagebox.showerror('参数错误','温度 0–2；token 1–131072；期望字符 0–1000000；轮数 1–500。实际支持范围由模型决定。')
            return False
        self.current.update(values, system=self.vault.redact(self.system.get('1.0','end-1c')), style=self.style.get(), profile=self.profile.get())
        return self.persist()

    def render(self):
        self.chat.configure(state='normal')
        self.chat.delete('1.0','end')
        for m in self.current['messages']:
            label = '你' if m['role']=='user' else '助手'
            if m.get('status','complete')!='complete': label += '（未完成，不纳入后续上下文）'
            self.chat.insert('end',label+'\n'+self.vault.redact(m['content'])+'\n\n')
        self.chat.configure(state='disabled')
        self.chat.see('end')

    def rename(self):
        if self.running: return
        name = simpledialog.askstring('重命名','会话名称：',parent=self.root)
        if name:
            self.current['title'] = self.vault.redact(name[:100]); self.persist(); self.refresh_sessions()

    def delete(self):
        if self.running: return
        if messagebox.askyesno('删除','确定删除当前会话？'):
            self.store.data['sessions'].remove(self.current)
            self.current = None
            if self.store.data['sessions']: self.load(self.store.data['sessions'][0])
            else: self.new()
            self.persist(); self.refresh_sessions()

    def configure(self):
        if self.running: return
        win = tk.Toplevel(self.root); win.title('API 配置'); win.geometry('560x340'); win.grab_set()
        ttk.Label(win,text='名称相同则更新；新增名称则创建独立配置。密钥只存加密文件。').pack(pady=10)
        existing = next((p for p in self.store.data['profiles'] if p['name']==self.profile.get()),self.store.data['profiles'][0])
        fields = {}
        for key,label,default in [('name','配置名称',existing['name']),('url','接口基础地址（HTTPS）',existing['url']),('model','模型名称',existing['model']),('key','API 密钥（留空保留原密钥）','')]:
            ttk.Label(win,text=label).pack(anchor='w',padx=12)
            var = tk.StringVar(value=default)
            ttk.Entry(win,textvariable=var,show='•' if key=='key' else '').pack(fill='x',padx=12,pady=3)
            fields[key] = var
        def save():
            p = {k: fields[k].get().strip() for k in ['name','url','model']}
            secret = fields['key'].get().strip()
            try:
                endpoint(p['url'])
                if not p['name'] or not p['model']: raise ValueError()
                if secret and (len(secret)>4096 or any(c.isspace() for c in secret)): raise ValueError()
                # Prevent accidentally embedding an API key in public profile fields.
                if secret and any(secret in v for v in p.values()): raise ValueError()
                if secret:
                    old = self.vault.values.get(p['name'])
                    self.vault.values[p['name']] = secret
                    try: self.vault.save()
                    except Exception:
                        if old is None: self.vault.values.pop(p['name'],None)
                        else: self.vault.values[p['name']] = old
                        raise
                profiles = self.store.data['profiles']
                index = next((i for i,x in enumerate(profiles) if x['name']==p['name']),None)
                if index is None: profiles.append(p)
                else: profiles[index] = p
                if not self.persist(): return
                fields['key'].set('')
                self.refresh_profiles(); self.profile.set(p['name']); win.destroy()
            except Exception:
                messagebox.showerror('配置失败','请检查 HTTPS 地址、配置字段及指定目录写入权限。密钥未显示。',parent=win)
        ttk.Button(win,text='保存配置',command=save).pack(pady=12)

    def send(self):
        if self.running or not self.save_current(): return
        text = self.input.get('1.0','end-1c').strip()
        if not text: return
        profile = next((p for p in self.store.data['profiles'] if p['name']==self.profile.get()),None)
        secret = self.vault.values.get(self.profile.get(),'')
        if not profile or not secret:
            messagebox.showinfo('需要配置','请先通过“API 配置 / 密钥”保存密钥。'); return
        s = self.current
        s['messages'].append(dict(role='user',content=self.vault.redact(text)))
        if not self.persist():
            s['messages'].pop(); return
        self.input.delete('1.0','end')
        # Snapshot before adding the streaming placeholder.
        snapshot = json.loads(json.dumps(s))
        s['messages'].append(dict(role='assistant',content='',status='incomplete'))
        self.running = True; self.stop_event.clear(); self.render()
        self.status.set('生成中 · 停止请求最迟在网络读取超时（30秒）后生效。')
        def worker():
            try:
                complete = stream_chat(profile, secret, snapshot, self.stop_event, lambda t:self.events.put(('text',t)))
                self.events.put(('done','complete' if complete else 'stopped'))
            except Exception:
                # Deliberately never stringify exceptions or response bodies.
                self.events.put(('done','error'))
        threading.Thread(target=worker,daemon=True).start()

    def stop(self):
        self.stop_event.set()
        if self.running: self.status.set('已请求停止，等待网络读取结束（最长约30秒）。')

    def poll(self):
        changed = False
        try:
            while True:
                kind,value = self.events.get_nowait()
                if kind=='text':
                    self.current['messages'][-1]['content'] += value
                    changed = True
                else:
                    m = self.current['messages'][-1]
                    m['content'] = self.vault.redact(m['content'])
                    m['status'] = 'complete' if value=='complete' and m['content'] else 'incomplete'
                    self.running = False
                    if value=='error':
                        self.status.set('请求失败：请检查网络、地址、密钥、模型及参数。未记录服务端原始错误。')
                    else:
                        self.status.set(('已完成' if value=='complete' else '已停止')+' · 回答字符数：'+str(len(m['content'])))
                    self.persist(); changed = True
        except queue.Empty: pass
        if changed: self.render()
        self.root.after(80,self.poll)

    def export(self,answer_only):
        if self.running:
            messagebox.showinfo('请稍候','请先停止或等待本次回答完成，再导出。'); return
        if not self.save_current(): return
        include = False if answer_only else messagebox.askyesno('导出选项','是否包含背景提示词？')
        name = filedialog.asksaveasfilename(title='另存为（请选择非系统盘）',initialdir=str(self.folder),
               defaultextension='.md',filetypes=[('Markdown 文档','*.md'),('文本文件','*.txt')])
        if not name: return
        try:
            path = allowed_path(name)
            if path.suffix.lower() not in ['.txt','.md']: raise ValueError('请选择 TXT 或 MD 格式。')
            text = export_text(self.current,answer_only,path.suffix.lower()=='.md',include)
            atomic_write(path,self.vault.redact(text).encode('utf-8'))
            self.status.set('已另存为：'+str(path))
        except ValueError as e: messagebox.showerror('无法导出',str(e))
        except Exception: messagebox.showerror('无法导出','指定目录无法写入，未改用其他位置。')

    def close(self):
        if self.running:
            self.stop(); messagebox.showinfo('正在停止','请在停止完成后关闭窗口。'); return
        if self.save_current(): self.root.destroy()


def main():
    root = tk.Tk(); root.withdraw()
    # These runtime writes occur only after validating the program directory.
    try:
        program = allowed_path(Path(sys.executable).parent if getattr(sys,'frozen',False) else Path(__file__).parent)
    except Exception:
        messagebox.showerror('存放位置不符合要求','请把软件整个文件夹移动到非系统盘后启动。'); root.destroy(); return
    folder = filedialog.askdirectory(title='选择数据目录（配置、加密密钥、历史均存此处；禁止系统盘）',initialdir=str(program))
    if not folder: root.destroy(); return
    try:
        folder = allowed_path(folder)
        runtime = folder/'runtime'; runtime.mkdir(exist_ok=True)
        for key in ['TEMP','TMP','TMPDIR']: os.environ[key] = str(runtime)
        import tempfile
        tempfile.tempdir = str(runtime)
    except Exception:
        messagebox.showerror('目录不可用','请选择可写入的非系统盘本地目录。'); root.destroy(); return
    new = not (folder/'vault.enc').exists()
    password = simpledialog.askstring('本地密钥库', '设置主密码（至少12位，忘记后无法恢复密钥）：' if new else '输入主密码解锁本地密钥库：',show='•',parent=root)
    if password is None: root.destroy(); return
    if new:
        if len(password)<12:
            messagebox.showerror('主密码过短','请重新启动并设置至少12位的主密码。'); root.destroy(); return
        confirm = simpledialog.askstring('确认主密码','再次输入主密码：',show='•',parent=root)
        if confirm != password:
            messagebox.showerror('不一致','两次密码不一致，未创建密钥库。'); root.destroy(); return
    try:
        vault = Vault(folder,password)
        password = None
        App(root,folder,vault)
    except Exception:
        messagebox.showerror('无法打开','主密码不正确、数据文件损坏或目录不可写。未生成异常日志。'); root.destroy(); return
    def safe_callback(*_):
        messagebox.showerror('操作未完成','发生内部错误；未保存堆栈、请求内容或崩溃转储。请检查指定数据目录。')
    root.report_callback_exception = safe_callback
    root.deiconify(); root.mainloop()


if __name__=='__main__':
    try: main()
    except BaseException:
        # No traceback or automatic crash report containing credentials.
        pass
