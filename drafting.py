"""Bound AI tasks and explicit, revision-checked application to author documents."""
import copy
import hashlib
import json
from datetime import datetime, timezone
from novel import uid, chapter

FIELDS={'book':['title','premise','outline','style'],'chapter':['title','volume','plan','summary','body'],
        'lore':['title','kind','text','pinned']}
PROMPTS={
 '创意构思':'根据现有想法，提出三个有区别的故事方向，说明核心冲突、主角动机和发展潜力。信息不足时明确假设，必要时先问少量关键问题。候选由作者选择，不自动成为设定。',
 '起草背景':'为这部作品起草可供讨论的背景与世界观，包括时代、规则、地点和主要矛盾。没有完整大纲也可以开始；明确标注假设，避免把假设说成已确认事实。',
 '完善大纲':'根据作者现有资料起草或完善故事总纲，包含开端、主线推进、关键转折与结局方向。保留已确认事实，未确定部分给出候选。输出完整可编辑大纲。',
 '人物候选':'起草人物候选，包括姓名、动机、缺点、关系、成长线和剧情作用。已有目标人物时保留其姓名与身份，以原档案为基础改进；没有人物时提出新候选。与现有设定保持一致，明确哪些是新建议。',
 '文风方案':'结合作品题材和作者偏好起草写作风格要求，包括叙述视角、语言节奏和应避免的问题。明确候选可修改，不凭空改写正式剧情。',
 '完善资料':'根据已有资料完善条理、补充待讨论方向，保留已确认事实，明确假设和新建议。只输出可编辑的资料候选。',
 '起草本章':'根据本章计划、总纲及已确认资料起草这一章，只输出候选正文，不自动写入作品。',
 '按意见修稿':'依据作者提供的审稿意见和本章完整原文修稿，保持未要求修改的事实，只输出完整修订正文。',
 '章节规划':'根据总纲提出分卷和章节计划，至多30章。只返回JSON对象 {"chapters":[{"title":"章名","volume":"卷名","plan":"目标、冲突、转折、伏笔"}]}。不写正文，结果等待作者确认。',
 '光标续写':'从标记的插入位置续写，衔接位置前后的文字，不重复原文。只输出应插入的候选正文。',
 '续写':'依据本章计划和衔接段落续写，只输出候选正文。保持视角、人物与时间线一致。',
 '润色选段':'润色下面的选段，保留事实、叙事视角和含义，只输出用于替换选段的正文。',
 '扩写选段':'扩写下面的选段，保留关键事实，不自行增加决定性的剧情事件，只输出用于替换选段的正文。',
 '压缩选段':'压缩下面的选段，保留关键事件与含义，只输出用于替换选段的正文。',
 '总结本章':'依据本章完整正文生成简洁、准确的章节摘要，不加入推测。只输出摘要正文，状态与伏笔建议可在后续讨论中另列。',
 '检查矛盾':'检查本章完整正文与已确认资料的矛盾，引用来源与对应原文，区分确定冲突和疑点。只提供审稿建议，不自动修改原稿。',
}


def digest(text): return hashlib.sha256(text.encode('utf-8')).hexdigest()


def qt_index(text,position):
    """QTextCursor offsets count UTF-16 units; Python string slices count code points."""
    return len(text.encode('utf-16-le')[:max(0,position)*2].decode('utf-16-le',errors='ignore'))


def find_book(state,identity):
    book=next((b for b in state['projects'] if b['id']==identity),None)
    if book is None: raise ValueError('这份候选所属的作品已不存在。')
    return book


def find_document(book,kind,identity):
    if kind=='book' and identity==book['id']: return book
    if kind not in ['chapter','lore']: raise ValueError('候选的目标类型无效。')
    doc=next((d for d in book['chapters' if kind=='chapter' else 'lore'] if d['id']==identity),None)
    if doc is None: raise ValueError('候选关联的原文已删除。可以复制候选或另存为资料。')
    return doc


def chapter_stamp(book):
    data=[{k:c.get(k,'') for k in ['id','title','volume','plan','summary','body']} for c in book['chapters']]
    return digest(json.dumps(data,ensure_ascii=False,sort_keys=True))


def make_task(book,name,doc=None,kind=None,field=None,start=None,end=None):
    if name not in PROMPTS: raise ValueError('未知写作任务。')
    task=dict(id=uid(),book_id=book['id'],name=name,prompt=PROMPTS[name],target='none',label=name)
    if name in ['创意构思','完善大纲','文风方案']:

        kind='book'; doc=book; field={'创意构思':'premise','完善大纲':'outline','文风方案':'style'}[name]
    if name=='章节规划':
        task.update(target='chapters',source_hash=chapter_stamp(book),label='分卷与章节规划'); return task
    if name in ['起草背景','人物候选']:
        expected='世界观' if name=='起草背景' else '人物'
        if kind=='lore' and doc and doc.get('kind')==expected: field='text'
        else:
            task.update(target='new_lore',lore_kind=expected,label='新增'+expected); return task
    if name in ['起草本章','按意见修稿','续写','光标续写','润色选段','扩写选段','压缩选段','总结本章','检查矛盾']:
        if kind!='chapter' or doc is None: raise ValueError('请先选择一章正文。')
        field='summary' if name=='总结本章' else 'body'
        task['chapter_id']=doc['id']
        task['full_chapter']=name in ['总结本章','检查矛盾','按意见修稿']
        if name=='总结本章': task['body_hash']=digest(doc.get('body',''))
    if doc is None or kind not in FIELDS or field not in FIELDS[kind]: raise ValueError('请先选择需要处理的文档。')
    source=doc.get(field,'')
    task.update(kind=kind,doc_id=doc['id'],field=field,source_hash=digest(source),source_excerpt=source[:8000],
                source_truncated=len(source)>8000,label=doc['title']+' / '+{'body':'正文','summary':'摘要','premise':'创意与简介','outline':'大纲','text':'资料'}.get(field,field))
    if name in ['润色选段','扩写选段','压缩选段']:
        if start is None or end is None or not 0<=start<end<=len(source): raise ValueError('请先在正文中选中要修改的文本。')
        task.update(target='selection',start=start,end=end,source_excerpt=source[start:end],source_truncated=False)
    elif name in ['续写','光标续写']:
        point=len(source) if start is None else start
        if not 0<=point<=len(source): raise ValueError('插入位置已失效，请重新选择。')
        task.update(target='insert',start=point,end=point)
        task['label']+=' · '+('章末续写' if point==len(source) else '第%d字符处插入'%(point+1))
    elif name=='检查矛盾': task['target']='review'
    else: task['target']='field'
    return task


def task_input(task,guidance=''):
    text=task['prompt']
    if task['target']=='selection': text+='\n\n原选段：\n'+task['source_excerpt']
    if guidance.strip(): text+='\n\n作者补充要求：\n'+guidance.strip()
    return text


def parse_chapters(text):
    text=text.strip()
    if text.startswith('```'):
        lines=text.splitlines(); text='\n'.join(lines[1:-1]) if lines[-1].strip()=='```' else text
    try:
        data=json.loads(text)
        rows=data.get('chapters') if isinstance(data,dict) else data
        if not isinstance(rows,list) or not 1<=len(rows)<=100: raise ValueError()
        result=[]
        for row in rows:
            if not isinstance(row,dict): raise ValueError()
            item={k:row.get(k,'') for k in ['title','volume','plan']}
            if any(not isinstance(v,str) or len(v)>20000 for v in item.values()) or not item['title'].strip(): raise ValueError()
            item['title']=item['title'].strip(); item['volume']=item['volume'].strip() or '第一卷'; result.append(item)
        return result
    except (ValueError,TypeError,KeyError):
        raise ValueError('未能识别章节条目。可继续让AI按章节规划整理，或将候选另存为资料。') from None


def snapshot(document,kind):
    old={k:copy.deepcopy(document.get(k,'')) for k in FIELDS[kind]}
    versions=document.setdefault('versions',[])
    if not versions or versions[-1]['data']!=old:
        versions.append(dict(at=datetime.now(timezone.utc).isoformat(),data=old))
        document['versions']=versions[-50:]


def check_target(state,task):
    book=find_book(state,task['book_id'])
    if task['target']=='new_lore': return book,None
    if task['target']=='chapters':
        if chapter_stamp(book)!=task['source_hash']: raise ValueError('章节结构或正文已变化，请重新生成规划，避免覆盖新的工作。')
        return book,None
    doc=find_document(book,task['kind'],task['doc_id'])
    if task.get('body_hash') and digest(doc.get('body',''))!=task['body_hash']:
        raise ValueError('本章正文已变化，旧摘要候选暂不写入，请重新总结。')
    if digest(doc.get(task['field'],''))!=task['source_hash']:
        raise ValueError('原文已发生变化。这份候选保留供参考，请重新选段生成，或复制后手动编辑。')
    return book,doc


def apply_proposal(state,task,text,title='',reuse_empty=False):
    """All validation precedes mutation. Persistence/rollback is the caller's responsibility."""
    if not text.strip(): raise ValueError('候选内容为空。')
    book,doc=check_target(state,task)
    target=task['target']
    if target=='review': raise ValueError('审稿意见不会直接替换正文，请按意见继续修稿。')
    if target=='new_lore':
        doc=dict(id=uid(),title=title.strip() or ('新'+task['lore_kind']),kind=task['lore_kind'],text=text,pinned=False,versions=[])
        doc['checkpoint']={k:doc[k] for k in FIELDS['lore']}; book['lore'].append(doc)
        return dict(kind='lore',doc_id=doc['id'],book_id=book['id'])
    if target=='chapters':
        rows=parse_chapters(text); reuse=None
        if reuse_empty and len(book['chapters'])==1:
            first=book['chapters'][0]
            if not any(first.get(k,'').strip() for k in ['body','plan','summary']): reuse=first
        for index,row in enumerate(rows):
            if index==0 and reuse is not None:
                c=reuse; snapshot(c,'chapter'); c.update(row)
            else:
                c=chapter(row['title']); c.update(row); book['chapters'].append(c)
            c['checkpoint']={k:c[k] for k in FIELDS['chapter']}
        return dict(kind='chapter',doc_id=(reuse or book['chapters'][-len(rows)])['id'],book_id=book['id'])
    old=doc.get(task['field'],'')
    if target in ['selection','insert']:
        start,end=task['start'],task['end']
        if not 0<=start<=end<=len(old): raise ValueError('原选区位置已失效。')
        value=old[:start]+text+old[end:]
    elif target=='field': value=text
    else: raise ValueError('无法直接应用这类候选。')
    snapshot(doc,task['kind']); doc[task['field']]=value
    doc['checkpoint']={k:doc.get(k,'') for k in FIELDS[task['kind']]}
    return dict(kind=task['kind'],doc_id=doc['id'],book_id=book['id'])
