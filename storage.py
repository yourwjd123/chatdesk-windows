"""Account-scoped authenticated encryption. No logging, plaintext files, or password recovery."""
import base64
import copy
import json
import os
import re
import shutil
import unicodedata
import uuid
from pathlib import Path
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt
from core import allowed_path, atomic_write

MAX_ATTACHMENT = 20 * 1024 * 1024
MAX_BACKUP = 256 * 1024 * 1024


def encode(value):
    return json.dumps(value, ensure_ascii=False, separators=(',', ':')).encode('utf-8')


def derive(password, salt):
    return Scrypt(salt=salt, length=32, n=2**15, r=8, p=1).derive(password.encode('utf-8'))


def encrypt(key, raw, aad):
    nonce = os.urandom(12)
    return nonce + AESGCM(key).encrypt(nonce, raw, aad)


def decrypt(key, raw, aad):
    return AESGCM(key).decrypt(raw[:12], raw[12:], aad)


def wrap(key, password, aad):
    salt = os.urandom(16)
    return encode({'version': 2, 'salt': base64.b64encode(salt).decode(),
                   'wrapped': base64.b64encode(encrypt(derive(password, salt), key, aad)).decode()})


def unwrap(raw, password, aad):
    value = json.loads(raw)
    if value['version'] != 2:
        raise ValueError('不支持的文件版本。')
    return decrypt(derive(password, base64.b64decode(value['salt'])), base64.b64decode(value['wrapped']), aad)


def check_password(password):
    if len(password) < 12 or len(password) > 1024:
        raise ValueError('密码须为12至1024个字符；遗忘后无法恢复数据。')


def valid_id(value):
    if not re.fullmatch('[0-9a-f]{32}', value):
        raise ValueError('无效的数据标识。')
    return value


def default_session(defaults=None):
    result = dict(id=uuid.uuid4().hex, title='新会话', system='你是一位严谨、友好的助手。',
                  temperature=0.7, tokens=1024, chars=0, rounds=10, style='平衡', profile='DeepSeek',
                  messages=[], disabled_attachments=[], draft_text='', draft_attachments=[])
    result.update(copy.deepcopy(defaults or {}))
    return result


def defaults():
    return dict(profiles=[dict(name='DeepSeek',url='https://api.deepseek.com/v1',model='deepseek-chat',key='',vision=False)],
        sessions=[], attachments={}, appearance=dict(family='Microsoft YaHei',code_family='Consolas',size=12,
        theme='浅色',background='',mode='填充',shade=80), idle_minutes=10, defaults={})


class Accounts:
    def __init__(self, root):
        self.root = allowed_path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.index_path = self.root / 'users.json'
        self.users = json.loads(self.index_path.read_bytes()) if self.index_path.exists() else []
        for user in self.users:
            valid_id(user['id'])

    def save(self):
        atomic_write(self.index_path, encode(self.users))

    def name(self, name, excluding=None):
        name = unicodedata.normalize('NFKC', name.strip())
        if not name or len(name) > 48 or any(ord(c)<32 for c in name):
            raise ValueError('用户名须为1至48个可见字符。')
        if any(u['name'].casefold()==name.casefold() and u['id']!=excluding for u in self.users):
            raise ValueError('用户名已存在。')
        return name

    def folder(self, uid):
        base = allowed_path(self.root/'users'/valid_id(uid))
        if not base.is_relative_to(self.root):
            raise ValueError('数据目录越界。')
        return base

    def create(self, name, password):
        name = self.name(name)
        check_password(password)
        uid = uuid.uuid4().hex
        folder = self.folder(uid)
        folder.mkdir(parents=True)
        key = os.urandom(32)
        try:
            atomic_write(folder/'account.json', wrap(key,password,('account:'+uid).encode()))
            session = User(self,uid,key,defaults())
            session.save()
            self.users.append(dict(id=uid,name=name))
            try:
                self.save()
            except Exception:
                self.users.pop()
                raise
            return session
        except Exception:
            shutil.rmtree(folder)
            raise

    def unlock(self, uid, password):
        if uid not in [u['id'] for u in self.users]:
            raise ValueError('用户不存在。')
        folder = self.folder(uid)
        key = unwrap((folder/'account.json').read_bytes(), password, ('account:'+uid).encode())
        state = json.loads(decrypt(key,(folder/'state.enc').read_bytes(),('state:'+uid).encode()))
        return User(self,uid,key,state)

    def rename(self, uid, name):
        name = self.name(name,uid)
        user = next(u for u in self.users if u['id']==uid)
        old = user['name']; user['name'] = name
        try: self.save()
        except Exception:
            user['name'] = old
            raise

    def delete(self, uid, password):
        user = self.unlock(uid,password)
        user.close()
        # Detach before deletion so partial filesystem errors cannot revive an account.
        old = self.users[:]
        self.users = [u for u in self.users if u['id']!=uid]
        try: self.save()
        except Exception:
            self.users = old
            raise
        shutil.rmtree(self.folder(uid))

    def restore(self, path, backup_password, name, password):
        path = Path(path)
        if path.stat().st_size > MAX_BACKUP:
            raise ValueError('备份文件超过256 MiB。')
        raw = unwrap(path.read_bytes(), backup_password, b'ChatDesk-backup-v2')
        backup = json.loads(raw)
        state, blobs = backup['state'], backup['blobs']
        # Validate every ID and authenticate all blobs BEFORE creating an account.
        for aid in state['attachments']:
            valid_id(aid)
            if aid not in blobs: raise ValueError('备份缺少附件。')
        data = {aid:base64.b64decode(blobs[aid],validate=True) for aid in state['attachments']}
        if any(len(v)>MAX_ATTACHMENT for v in data.values()): raise ValueError('附件过大。')
        restored = self.create(name,password)
        try:
            restored.state = state
            restored.state.setdefault('projects',[])
            for aid,raw in data.items(): restored.write_blob(aid,raw)
            restored.save()
            return restored
        except Exception:
            restored.close()
            self.delete(restored.uid,password)
            raise


class User:
    def __init__(self, accounts, uid, key, state):
        self.accounts, self.uid, self.key, self.state = accounts, uid, key, state
        self.folder = accounts.folder(uid)
        self.closed = False
        self.state.setdefault('projects',[])

    @property
    def name(self):
        return next(u['name'] for u in self.accounts.users if u['id']==self.uid)

    def redact(self, text):
        for secret in sorted({p.get('key','') for p in self.state['profiles']},key=len,reverse=True):
            if secret: text = text.replace(secret,'[已隐藏密钥]')
        return text

    def save(self):
        if self.closed: raise ValueError('用户已锁定。')
        # Entire state, including API keys, is encrypted. Nothing is written as plaintext.
        atomic_write(self.folder/'state.enc',encrypt(self.key,encode(self.state),('state:'+self.uid).encode()))

    def change_password(self, old, new):
        check_password(new)
        verified = self.accounts.unlock(self.uid,old)
        verified.close()
        atomic_write(self.folder/'account.json',wrap(self.key,new,('account:'+self.uid).encode()))

    def blob_path(self, aid):
        folder = allowed_path(self.folder/'attachments')
        if not folder.is_relative_to(self.folder): raise ValueError('附件目录越界。')
        folder.mkdir(exist_ok=True)
        return folder/(valid_id(aid)+'.enc')

    def write_blob(self, aid, raw):
        if self.closed: raise ValueError('用户已锁定。')
        atomic_write(self.blob_path(aid),encrypt(self.key,raw,('attachment:'+self.uid+':'+aid).encode()))

    def read_blob(self, aid):
        if self.closed: raise ValueError('用户已锁定。')
        if aid not in self.state['attachments']: raise ValueError('附件不存在。')
        return decrypt(self.key,self.blob_path(aid).read_bytes(),('attachment:'+self.uid+':'+aid).encode())

    def add_attachment(self, path, background=False):
        from attachments import parse_file
        raw, meta = parse_file(path,background)
        aid = uuid.uuid4().hex
        meta.update(id=aid)
        self.write_blob(aid,raw)
        self.state['attachments'][aid] = meta
        try: self.save()
        except Exception:
            del self.state['attachments'][aid]
            self.blob_path(aid).unlink(missing_ok=True)
            raise
        return aid

    def delete_attachment(self, aid):
        if self.state['appearance']['background']==aid:
            raise ValueError('请先在外观中移除该背景图片。')
        meta = self.state['attachments'].pop(aid)
        old = copy.deepcopy(self.state['sessions'])
        for s in self.state['sessions']:
            for m in s['messages']+[d['message'] for d in s.get('deleted',[])]:
                m['attachments'] = [a for a in m.get('attachments',[]) if a!=aid]
            s['disabled_attachments'] = [a for a in s.get('disabled_attachments',[]) if a!=aid]
            s['draft_attachments'] = [a for a in s.get('draft_attachments',[]) if a!=aid]
        try: self.save()
        except Exception:
            self.state['attachments'][aid] = meta
            for current, original in zip(self.state['sessions'],old):
                current.clear(); current.update(original)
            raise
        self.blob_path(aid).unlink(missing_ok=True)

    def backup(self, path, password):
        check_password(password)
        body = dict(state=self.state,blobs={aid:base64.b64encode(self.read_blob(aid)).decode() for aid in self.state['attachments']})
        raw = encode(body)
        if len(raw)>180*1024*1024: raise ValueError('备份过大，请先清理附件。')
        # Independent password; plaintext is held in memory only, never in a ZIP/temp file.
        atomic_write(path,wrap(raw,password,b'ChatDesk-backup-v2'))

    def import_v1(self, root, password):
        root = Path(root)
        old = json.loads((root/'conversations.json').read_bytes())
        secrets = {}
        if (root/'vault.enc').exists():
            e=json.loads((root/'vault.enc').read_bytes())
            key=derive(password,base64.b64decode(e['salt']))
            secrets=json.loads(AESGCM(key).decrypt(base64.b64decode(e['nonce']),base64.b64decode(e['data']),b'ChatDesk-v1'))
        previous = self.state
        candidate = dict(self.state)
        candidate['profiles'] = copy.deepcopy(self.state['profiles'])
        candidate['sessions'] = list(self.state['sessions'])
        for profile in old['profiles']:
            p=copy.deepcopy(profile); p.update(key=secrets.get(p['name'],''),vision=False)
            p['name'] = self.redact(p['name'])
            matching=next((x for x in candidate['profiles'] if x['name']==p['name']),None)
            if matching: matching.update(p)
            else: candidate['profiles'].append(p)
        for session in old['sessions']:
            s=default_session(); s.update(copy.deepcopy(session)); s['id']=uuid.uuid4().hex
            s.setdefault('disabled_attachments',[])
            candidate['sessions'].append(s)
        self.state = candidate
        try: self.save()
        except Exception:
            self.state = previous
            raise

    def close(self):
        self.state.clear()
        self.key = None
        self.closed = True
