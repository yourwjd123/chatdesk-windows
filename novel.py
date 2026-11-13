"""Novel canon, bounded contextual retrieval and editable conversation branches."""
import copy
import re
import uuid
from datetime import datetime, timezone


def uid(): return uuid.uuid4().hex

def chapter(title='第一章'):
    value=dict(id=uid(),title=title,volume='第一卷',plan='',summary='',body='',versions=[])
    value['checkpoint']={k:value[k] for k in ['title','volume','plan','summary','body']}
    return value


def project(title='未命名作品'):
    value=dict(id=uid(),title=title,premise='',outline='',style='',chapters=[chapter()],lore=[],versions=[])
    value['checkpoint']={k:value[k] for k in ['title','premise','outline','style']}
    return value


def checkpoint(document,fields):
    now={k:document.get(k,'') for k in fields}
    old=document.get('checkpoint',{})
    if old!=now:
        document.setdefault('versions',[]).append(dict(at=datetime.now(timezone.utc).isoformat(),data=copy.deepcopy(old)))
        document['versions']=document['versions'][-50:]
        document['checkpoint']=copy.deepcopy(now)
        return True
    return False


def restore(document,index,fields):
    target=copy.deepcopy(document['versions'][index]['data'])
    current={k:document.get(k,'') for k in fields}
    document['versions'].append(dict(at=datetime.now(timezone.utc).isoformat(),data=current))
    document['versions']=document['versions'][-50:]
    for field in fields: document[field]=target.get(field,False if field=='pinned' else '')
    document['checkpoint']={k:document.get(k,'') for k in fields}


def keywords(text):
    result=set(re.findall(r'[A-Za-z0-9_]{2,}',text.lower()))
    for run in re.findall(r'[\u4e00-\u9fff]+',text):
        result.update(run[i:i+2] for i in range(len(run)-1))
    return result


def context_sources(book,current,query):
    entries=[]
    def add(key,label,text,reason,required=False,priority=0):
        if text.strip(): entries.append(dict(id=key,label=label,text=text,reason=reason,required=required,priority=priority))
    add('canon','作品总纲与风格','作品：'+book['title']+'\n简介：'+book['premise']+'\n总纲：'+book['outline']+'\n风格与约束：'+book['style'],'总纲始终参与；内容可在作品资料中修改',True,100)
    if current:
        add('plan:'+current['id'],'当前章节计划 · '+current['title'],current['plan'],'当前章节写作边界',True,95)
        add('current-summary:'+current['id'],'本章已确认摘要',current['summary'],'本章人工确认的剧情状态',False,88)
        add('tail:'+current['id'],'当前正文末尾（最多6000字符）',current['body'][-6000:],'本章衔接段落',False,90)
        add('body:'+current['id'],'当前章节完整正文',current['body'],'需要全章审阅时手动勾选',False,-100)
        index=next(i for i,c in enumerate(book['chapters']) if c['id']==current['id'])
        if index:
            previous=book['chapters'][index-1]
            add('previous:'+previous['id'],'上一章末尾 · '+previous['title'],previous['body'][-3000:],'上一章衔接，最多3000字符',False,85)
    else: index=len(book['chapters'])
    search=keywords(query+' '+(current['title']+' '+current['plan'] if current else ''))
    for i,c in enumerate(book['chapters']):
        if current and c['id']==current['id']: continue
        score=len(search & keywords(c['title']+' '+c['summary']))
        nearby=max(0,3-(index-i-1)) if i<index else 0
        # Future chapter summaries are available for manual selection, never auto-included.
        priority=70+nearby if nearby else 30+min(20,score) if score and i<index else -10
        add('summary:'+c['id'],'章节摘要 · '+c['title'],c['summary'],'近期摘要' if nearby else '关键词匹配' if score else '手动选择',False,priority)
    for item in book['lore']:
        score=len(search & keywords(item['title']+' '+item['text']))
        priority=80 if item.get('pinned') else 40+min(20,score) if score else -10
        add('lore:'+item['id'],item['kind']+' · '+item['title'],item['text'],'固定资料' if item.get('pinned') else '关键词匹配' if score else '手动选择',False,priority)
    return entries


def assemble(entries,budget=32000,selected=None):
    required={e['id'] for e in entries if e['required']}
    choice=set(selected) | required if selected is not None else set(required)
    def chunk(e): return '\n\n【'+e['label']+'】\n'+e['text']
    header='以下是作者确认或手动编辑的作品资料。AI候选回复不自动构成正式剧情。以这些资料为依据；如有冲突请指出来源，不自行改写事实。'
    used=len(header)+sum(len(chunk(e)) for e in entries if e['id'] in choice)
    if used>budget: raise ValueError('已选资料超过上下文字符预算，请精简总纲、取消部分资料或提高预算。')
    if selected is None:
        for e in sorted(entries,key=lambda x:x['priority'],reverse=True):
            if e['id'] in choice or e['priority']<0: continue
            size=len(chunk(e))
            if used+size<=budget:
                choice.add(e['id']); used+=size
    text=header+''.join(chunk(e) for e in entries if e['id'] in choice)
    omitted=[e['label'] for e in entries if e['id'] not in choice]
    return dict(text=text,selected=choice,omitted=omitted,characters=len(text))


def exclude_message(session,index):
    session['messages'][index]['excluded']=not session['messages'][index].get('excluded',False)


def remove_message(session,index):
    message=session['messages'].pop(index)
    session.setdefault('deleted',[]).append(dict(index=index,message=message))
    session['deleted']=session['deleted'][-20:]


def undo_remove(session):
    if not session.get('deleted'): return False
    old=session['deleted'].pop(); session['messages'].insert(min(old['index'],len(session['messages'])),old['message']); return True


def branch(session,index):
    result=copy.deepcopy(session); result['id']=uid(); result['title']=session['title']+' · 分支'
    result['messages']=copy.deepcopy(session['messages'][:index+1]); result['deleted']=[]
    result['draft_text']=''; result['draft_attachments']=[]; result.pop('context_bundle',None)
    return result


def candidate_branch(session,index):
    # Regenerate from the previous user message, excluding the unwanted answer and descendants.
    if session['messages'][index]['role']!='assistant': raise ValueError('请选择助手回复。')
    start=index-1
    while start>=0 and session['messages'][start]['role']!='user': start-=1
    if start<0: raise ValueError('该回复没有对应的用户输入。')
    result=branch(session,start-1)
    question=session['messages'][start]
    result['draft_text']=question['content']; result['draft_attachments']=question.get('attachments',[])[:]
    result['title']=session['title']+' · 候选版本'
    return result
