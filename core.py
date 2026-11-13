"""Local storage and OpenAI-compatible HTTPS client. Never logs request objects."""
import base64
import json
import os
import re
import ssl
import tempfile
import urllib.request
import urllib.error
from pathlib import Path
from urllib.parse import urlsplit
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt


def allowed_path(path):
    p = Path(path).resolve()
    if os.name == 'nt':
        system = os.environ.get('SystemDrive', 'C:').casefold()
        if not p.drive or p.drive.casefold() == system or str(p).startswith('\\\\'):
            raise ValueError('请选择非系统盘的本地目录。')
    return p


def atomic_write(path, data):
    path = allowed_path(path)
    # The temporary file is always beside the destination, never in system TEMP.
    fd, name = tempfile.mkstemp(prefix='.writing-', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


class Vault:
    def __init__(self, root, password):
        self.path = allowed_path(root) / 'vault.enc'
        self.password = password
        if self.path.exists():
            envelope = json.loads(self.path.read_text('utf-8'))
            self.salt = base64.b64decode(envelope['salt'])
            self.key = self.derive(password, self.salt)
            self.values = json.loads(AESGCM(self.key).decrypt(
                base64.b64decode(envelope['nonce']),
                base64.b64decode(envelope['data']), b'ChatDesk-v1'))
        else:
            self.salt = os.urandom(16)
            self.key = self.derive(password, self.salt)
            self.values = {}
            self.save()
        self.password = None

    @staticmethod
    def derive(password, salt):
        return Scrypt(salt=salt, length=32, n=2**15, r=8, p=1).derive(password.encode('utf-8'))

    def save(self):
        nonce = os.urandom(12)
        data = AESGCM(self.key).encrypt(nonce, json.dumps(self.values).encode(), b'ChatDesk-v1')
        atomic_write(self.path, json.dumps({
            'version': 1, 'salt': base64.b64encode(self.salt).decode(),
            'nonce': base64.b64encode(nonce).decode(), 'data': base64.b64encode(data).decode()
        }).encode())

    def redact(self, text):
        for secret in sorted(set(self.values.values()), key=len, reverse=True):
            if secret:
                text = text.replace(secret, '[已隐藏密钥]')
        return text


DEFAULT = {'profiles': [{'name': 'DeepSeek', 'url': 'https://api.deepseek.com/v1',
                         'model': 'deepseek-chat'}], 'sessions': []}


class Store:
    def __init__(self, root, vault):
        self.path = allowed_path(root) / 'conversations.json'
        self.vault = vault
        self.data = json.loads(self.path.read_text('utf-8')) if self.path.exists() else json.loads(json.dumps(DEFAULT))

    def save(self):
        # Defense in depth: known secrets cannot enter persisted conversations/config.
        def scrub(value):
            if isinstance(value, str):
                return self.vault.redact(value)
            if isinstance(value, list):
                return [scrub(item) for item in value]
            if isinstance(value, dict):
                return {self.vault.redact(str(k)): scrub(v) for k, v in value.items()}
            return value
        atomic_write(self.path, json.dumps(scrub(self.data), ensure_ascii=False, indent=2).encode('utf-8'))


def endpoint(url):
    p = urlsplit(url.strip())
    if p.scheme != 'https' or not p.hostname or p.username or p.password or p.query or p.fragment:
        raise ValueError('接口必须为 HTTPS 地址，不可含用户名、密码、查询参数或片段。')
    url = url.rstrip('/')
    return url if url.endswith('/chat/completions') else url + '/chat/completions'


def messages_for(session):
    system = session['system']
    if session['chars']:
        system += '\n本次回答期望不超过 %s 个字符。请尽量完整、简洁；该长度为期望值。' % session['chars']
    presets = {'严格': '\n严格遵循任务约束；信息不足时明确说明，不自行补充事实。',
               '平衡': '', '自由': '\n在满足任务要求的前提下，可以提出多种创意和视角。'}
    system += presets[session['style']]
    history = [m for m in session['messages'] if m.get('status', 'complete') == 'complete']
    keep = session['rounds'] * 2 + 1
    history = history[-keep:]
    while history and history[0]['role'] != 'user':
        history.pop(0)
    return [{'role': 'system', 'content': system}] + [dict(role=m['role'], content=m['content']) for m in history]


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None  # Never forward Authorization to a redirect destination.


def stream_chat(profile, secret, session, stop, emit):
    payload = {'model': profile['model'], 'messages': messages_for(session),
               'temperature': session['temperature'], 'max_tokens': session['tokens'], 'stream': True}
    request = urllib.request.Request(endpoint(profile['url']),
        data=json.dumps(payload).encode('utf-8'),
        headers={'Content-Type': 'application/json', 'Authorization': 'Bearer ' + secret})
    # Ignore environment proxies: users explicitly choose the destination.
    client = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect(),
                                        urllib.request.HTTPSHandler(context=ssl.create_default_context()))
    done = False
    with client.open(request, timeout=30) as response:
        for raw in response:
            if stop.is_set():
                return False
            line = raw.decode('utf-8').strip()
            if not line.startswith('data:'):
                continue
            body = line[5:].strip()
            if body == '[DONE]':
                done = True
                break
            chunk = json.loads(body)
            if 'error' in chunk:
                raise ValueError('remote error')
            for choice in chunk.get('choices', []):
                text = choice.get('delta', {}).get('content')
                if text:
                    emit(text)
                if choice.get('finish_reason') == 'length':
                    emit('\n\n[已达到输出 token 上限，回答可能不完整。]')
    if not done and not stop.is_set():
        raise ValueError('incomplete response')
    return not stop.is_set()


def export_text(session, answer_only=False, markdown=False, include_system=False):
    if answer_only:
        answers = [m['content'] for m in session['messages'] if m['role'] == 'assistant']
        if not answers:
            raise ValueError('当前会话没有回答。')
        return answers[-1]
    sections = [('背景提示词', session['system'])] if include_system else []
    for m in session['messages']:
        label = '用户' if m['role'] == 'user' else '助手'
        if m.get('status', 'complete') != 'complete':
            label += '（未完成）'
        sections.append((label, m['content']))
    return '\n\n'.join((('## ' if markdown else '') + title + '\n\n' + content) for title, content in sections) + '\n'
