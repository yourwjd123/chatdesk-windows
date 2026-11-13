"""HTTPS chat protocol and user-selected filesystem writes. No request or error logging."""
import base64
import json
import math
import os
import ssl
import tempfile
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlsplit


def allowed_path(path):
    p=Path(path).resolve()
    if os.name=='nt':
        system=os.environ.get('SystemDrive','C:').casefold()
        if not p.drive or p.drive.casefold()==system or str(p).startswith('\\\\'):
            raise ValueError('请选择非系统盘的本地目录。')
    return p


def atomic_write(path,data):
    path=allowed_path(path)
    fd,name=tempfile.mkstemp(prefix='.writing-',dir=path.parent)
    try:
        with os.fdopen(fd,'wb') as f:
            f.write(data); f.flush(); os.fsync(f.fileno())
        os.replace(name,path)
    finally:
        if os.path.exists(name): os.unlink(name)


def endpoint(url):
    p=urlsplit(url.strip())
    if p.scheme!='https' or not p.hostname or p.username or p.password or p.query or p.fragment:
        raise ValueError('接口须为HTTPS地址，不可含认证信息、查询参数或片段。')
    url=url.strip().rstrip('/')
    return url if url.endswith('/chat/completions') else url+'/chat/completions'


def messages_for(session,attachments=None,read_blob=None,vision=False):
    attachments=attachments or {}
    system=session['system']
    if session['chars']: system+='\n期望本次回答不超过%s个字符，请保持完整、简洁。'%session['chars']
    system+={'严格':'\n严格遵循任务约束；信息不足时明确说明，不自行补充事实。','平衡':'','自由':'\n在满足任务要求的前提下，可以提出多种创意和视角。'}[session['style']]
    history=[m for m in session['messages'] if m.get('status','complete')=='complete'][-(session['rounds']*2+1):]
    while history and history[0]['role']!='user': history.pop(0)
    result=[dict(role='system',content=system)]
    for m in history:
        text=m['content']; images=[]
        for aid in m.get('attachments',[]):
            if aid in session.get('disabled_attachments',[]): continue
            meta=attachments.get(aid)
            if meta is None: raise ValueError('会话附件不存在，请移除引用。')
            if meta['kind']=='text':
                text+='\n\n[附件：'+meta['name']+'；以下为用户提供的文档内容]\n'+meta['text']+'\n[附件结束]'
            else:
                if not vision: raise ValueError('本次上下文包含图片，请选择支持视觉的模型并勾选视觉能力，或取消图片参与发送。')
                raw=read_blob(aid)
                images.append(dict(type='image_url',image_url=dict(url='data:'+meta['mime']+';base64,'+base64.b64encode(raw).decode())))
        content=[dict(type='text',text=text)]+images if images else text
        result.append(dict(role=m['role'],content=content))
    return result


def estimate(messages):
    # Conservative display estimate, not model tokenizer or billing data.
    chars=sum(len(m['content']) if isinstance(m['content'],str) else sum(len(p.get('text','')) for p in m['content']) for m in messages)
    images=sum(sum(p['type']=='image_url' for p in m['content']) for m in messages if isinstance(m['content'],list))
    return math.ceil(chars/2)+images*1500


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self,*_): return None


def stream_chat(profile,messages,params,stop,emit):
    payload=dict(model=profile['model'],messages=messages,temperature=params['temperature'],max_tokens=params['tokens'],stream=True)
    req=urllib.request.Request(endpoint(profile['url']),data=json.dumps(payload).encode(),
        headers={'Content-Type':'application/json','Authorization':'Bearer '+profile['key']})
    client=urllib.request.build_opener(urllib.request.ProxyHandler({}),NoRedirect(),urllib.request.HTTPSHandler(context=ssl.create_default_context()))
    done=False
    with client.open(req,timeout=30) as response:
        for raw in response:
            if stop.is_set(): return False
            line=raw.decode('utf-8').strip()
            if not line.startswith('data:'): continue
            body=line[5:].strip()
            if body=='[DONE]': done=True; break
            chunk=json.loads(body)
            if 'error' in chunk: raise ValueError('服务返回错误。')
            for choice in chunk.get('choices',[]):
                content=choice.get('delta',{}).get('content')
                if content: emit(content)
                if choice.get('finish_reason')=='length': emit('\n\n[已达到输出token上限，回答可能不完整。]')
    if not done and not stop.is_set(): raise ValueError('响应未完整结束。')
    return not stop.is_set()


def safe_error(error):
    if isinstance(error,urllib.error.HTTPError):
        return {400:'服务拒绝了请求，请检查模型、参数和附件支持。',401:'认证失败，请检查密钥。',402:'服务账户余额不足。',403:'服务拒绝访问。',404:'接口或模型不存在。',429:'请求过于频繁或配额不足。'}.get(error.code,'服务请求失败（HTTP %d）。'%error.code)
    return '请求失败：请检查网络、接口及模型设置。未保存原始错误。'


def export_text(session,attachments,answer_only=False,markdown=False,include_system=False):
    if answer_only:
        answers=[m['content'] for m in session['messages'] if m['role']=='assistant']
        if not answers: raise ValueError('当前会话没有回答。')
        return answers[-1]
    sections=[('背景提示词',session['system'])] if include_system else []
    for m in session['messages']:
        label='用户' if m['role']=='user' else '助手'
        if m.get('status','complete')!='complete': label+='（未完成）'
        content=m['content']
        for aid in m.get('attachments',[]):
            content+='\n[附件：'+attachments.get(aid,{}).get('name','已删除')+']'
        sections.append((label,content))
    return '\n\n'.join(('## ' if markdown else '')+label+'\n\n'+content for label,content in sections)+'\n'
