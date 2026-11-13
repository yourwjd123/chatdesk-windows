import copy
import json
import unittest
from drafting import make_task,task_input,apply_proposal,check_target,parse_chapters,qt_index
from novel import project,chapter


class DraftingTests(unittest.TestCase):
    def setUp(self):
        self.book=project('测试作品'); self.state={'projects':[self.book]}; self.c=self.book['chapters'][0]
    def test_blank_planning_supported_without_canon_mutation(self):
        for name in ['创意构思','完善大纲','起草背景','人物候选','文风方案','章节规划']:
            before=copy.deepcopy(self.book); task=make_task(self.book,name)
            self.assertTrue(task_input(task)); self.assertEqual(before,self.book)
        task=make_task(self.book,'起草背景'); result=apply_proposal(self.state,task,'海港背景','港城')
        self.assertEqual(self.book['lore'][0]['text'],'海港背景'); self.assertEqual(result['kind'],'lore')
    def test_field_apply_records_exact_old_draft(self):
        self.book['outline']='尚未手动建立版本的大纲'
        task=make_task(self.book,'完善大纲'); apply_proposal(self.state,task,'新版大纲')
        self.assertEqual(self.book['outline'],'新版大纲')
        self.assertEqual(self.book['versions'][-1]['data']['outline'],'尚未手动建立版本的大纲')
    def test_changed_field_and_deleted_doc_reject_without_mutation(self):
        self.c['body']='原文'; task=make_task(self.book,'润色选段',self.c,'chapter','body',0,2)
        self.c['body']='作者后来写的正文'; before=copy.deepcopy(self.state)
        with self.assertRaises(ValueError): apply_proposal(self.state,task,'候选')
        self.assertEqual(self.state,before)
        self.book['chapters'].clear()
        with self.assertRaises(ValueError): check_target(self.state,task)
    def test_selection_uses_original_range_not_current_cursor(self):
        self.c['body']='开头😀中间尾部'; start=qt_index(self.c['body'],2); end=qt_index(self.c['body'],4)
        self.assertEqual((start,end),(2,3))
        task=make_task(self.book,'润色选段',self.c,'chapter','body',start,end)
        apply_proposal(self.state,task,'亮光')
        self.assertEqual(self.c['body'],'开头亮光中间尾部')
    def test_summary_rejects_changed_body_even_when_summary_unchanged(self):
        self.c['body']='最初的剧情'; task=make_task(self.book,'总结本章',self.c,'chapter','summary')
        self.c['body']='已经改变的剧情'
        with self.assertRaises(ValueError): apply_proposal(self.state,task,'旧剧情摘要')
        self.assertEqual(self.c['summary'],'')
    def test_chapter_proposal_is_structured_and_author_confirmed(self):
        task=make_task(self.book,'章节规划')
        text=json.dumps({'chapters':[{'title':'出发','volume':'卷一','plan':'离开故乡'},{'title':'初遇','volume':'卷一','plan':'遇见同伴'}]},ensure_ascii=False)
        self.assertEqual(len(self.book['chapters']),1)
        first_id=self.c['id']; apply_proposal(self.state,task,text,reuse_empty=True)
        self.assertEqual(len(self.book['chapters']),2); self.assertEqual(self.book['chapters'][0]['id'],first_id)
        self.assertEqual(self.book['chapters'][1]['plan'],'遇见同伴')
        self.assertEqual(self.book['chapters'][0]['versions'][-1]['data']['plan'],'')
    def test_chapter_plans_reject_structure_changes_and_invalid_output(self):
        task=make_task(self.book,'章节规划'); self.book['chapters'].append(chapter('新章'))
        with self.assertRaises(ValueError): apply_proposal(self.state,task,'[{"title":"不会写入","volume":"卷一","plan":"计划"}]')
        before=copy.deepcopy(self.state)
        fresh=make_task(self.book,'章节规划')
        with self.assertRaises(ValueError): apply_proposal(self.state,fresh,'一段没有章节结构的自由文本')
        self.assertEqual(before,self.state)
        with self.assertRaises(ValueError): parse_chapters('{"chapters":[]}')
    def test_review_does_not_directly_replace_body(self):
        self.c['body']='作者正文'; task=make_task(self.book,'检查矛盾',self.c,'chapter','body')
        with self.assertRaises(ValueError): apply_proposal(self.state,task,'审稿意见')
        self.assertEqual(self.c['body'],'作者正文')


if __name__=='__main__': unittest.main()
