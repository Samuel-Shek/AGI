"""Round 3 content regressions; all inputs and storage are synthetic."""
import copy
from test_sync_center import Base
from psi_sync.state import mapped


def item(**changes):
    return dict(dict(id='synthetic-round3', kind='closure', state='decision',
                     requires_decision=True, title='核对合成资产权益与可兑现证据',
                     recommendation='由本人确认用途'), **changes)


class Round3Tests(Base):
    def test_signature_in_later_recommendation_clause_keeps_title_subject(self):
        row = mapped(item(recommendation='先完成只读核验，之后由本人完成签名'))
        self.assertEqual(row['question_subject'], '核对合成资产权益与可兑现证据')
        self.assertEqual(row['question'], '签名如何安排？')
        self.assertEqual(row['answer_options'], [])

    def test_no_category_word_uses_full_title_and_only_fallback_question(self):
        title = '核对合成资产权益与可兑现证据，完整范围仍需确认'
        row = mapped(item(title=title, background='旧背景：由本人完成签名'))
        self.assertEqual(row['question_subject'], title)
        self.assertEqual(row['question'], '这件事需要你怎么定？')

    def test_question_title_is_preserved_without_another_generated_question(self):
        title = '合成资产是否保留？'
        row = mapped(item(title=title, recommendation='由本人完成签名'))
        self.assertEqual(row['question'], title)
        self.assertEqual(row['question_subject'], '')

    def test_generated_questions_require_explicit_category_words(self):
        for source, question in [('签名', '签名如何安排？'), ('签字', '签名如何安排？'),
                                 ('签署', '签名如何安排？'), ('最终资金动作', '资金动作如何安排？'),
                                 ('出售', '要不要出售？'), ('保留', '要不要保留？')]:
            for field in ('title', 'recommendation'):
                with self.subTest(source=source, field=field):
                    changes = {field: '由本人确认合成材料' + source}
                    row = mapped(item(**changes))
                    self.assertEqual(row['question'], question)
                    self.assertEqual(row['question_subject'], changes.get('title', item()['title']))

    def test_generated_subject_obeys_paired_punctuation_and_sixty_character_limit(self):
        title = '合成设备「甲，乙」的权益已确认；后续由本人决定'
        row = mapped(item(title=title, recommendation='由本人完成签名'))
        self.assertEqual(row['question_subject'], '合成设备「甲，乙」的权益已确认')
        for title in ('合成材料' * 16, '合成材料用于；完整原标题保留'):
            row = mapped(item(title=title, recommendation='由本人完成签名'))
            self.assertEqual(row['question_subject'], title)
            self.assertEqual(row['question'], '这件事需要你怎么定？')

    def test_negated_and_sample_signatures_do_not_generate_signature_question(self):
        for recommendation in ('无需本人签名，由本人确认用途', '由本人确认签名样例',
                               '由本人确认签字格式', '由本人确认签署记录'):
            row = mapped(item(title='由本人确认合成材料用途', recommendation=recommendation))
            self.assertEqual(row['question'], '这件事需要你怎么定？')

    def set_items(self, summary, recommendations, states=None):
        self.state.items = {str(i): item(id=str(i), summary=summary, recommendation=rec,
                                       state=states[i] if states else 'decision')
                            for i, rec in enumerate(recommendations)}

    def test_three_repeated_summaries_use_each_recommendation_first_clause(self):
        for summary in ('执行期间发现，持续跟踪到验收。', '待你决定 · 负责人：助手'):
            self.set_items(summary, ['由本人确认合成用途甲，核对后再决定',
                                     '由本人确认合成用途乙；限个人回顾', ''])
            before = copy.deepcopy(self.state.items)
            rows = self.state.listing('all')['items']
            self.assertEqual({r['id']: r['list_summary'] for r in rows},
                             {'0': '由本人确认合成用途甲', '1': '由本人确认合成用途乙', '2': ''})
            self.assertTrue(all(r['summary'] == summary for r in rows))
            self.assertEqual(self.state.items, before)

    def test_two_equal_summaries_remain_visible(self):
        summary = '执行期间发现，持续跟踪到验收。'
        self.set_items(summary, ['由本人确认用途'] * 2)
        self.assertTrue(all(r['list_summary'] == summary for r in self.state.listing()['items']))

    def test_template_count_is_scoped_to_current_list(self):
        summary = '待你决定 · 负责人：助手'
        self.set_items(summary, ['由本人确认用途'] * 3, ['decision', 'decision', 'complete'])
        self.assertTrue(all(r['list_summary'] == summary for r in self.state.listing()['items']))
        self.assertTrue(all(r['list_summary'] == '由本人确认用途' for r in self.state.listing('all')['items']))

    def test_template_summary_does_not_fall_back_to_reason_or_incomplete_excerpt(self):
        self.set_items('模板摘要', ['合成材料' * 16, '先保留用于', '先检查「甲，乙」材料，等本人确认'])
        for value in self.state.items.values():
            value['reason'] = '原始原因仍在详情显示'
        rows = {r['id']: r for r in self.state.listing('all')['items']}
        self.assertEqual(rows['0']['list_summary'], '')
        self.assertEqual(rows['1']['list_summary'], '')
        self.assertEqual(rows['2']['list_summary'], '先检查「甲，乙」材料')
        self.assertEqual(rows['0']['reason'], '原始原因仍在详情显示')

    def test_technical_prerequisites_are_not_choices_but_source_is_retained(self):
        for recommendation in ('先完成只读核验，之后由本人完成签名', '准备登录页', '先准备登录页',
                               '核验合成证据', '读取合成材料', '检查合成状态', '同步合成资料'):
            row = mapped(item(title='由本人确认合成用途', recommendation=recommendation))
            self.assertEqual(row['answer_options'], [], recommendation)
            self.assertEqual(row['recommendation'], recommendation)

    def test_concrete_personal_and_retention_choices_keep_full_source(self):
        for recommendation in ('由本人完成签名，仅限合成材料', '由本人签字', '由本人签署',
                               '先保留，仅用于个人回顾', '由本人确认授权范围',
                               '准备合成材料并由本人确认用途'):
            row = mapped(item(title='由本人确认合成材料用途', recommendation=recommendation))
            self.assertEqual(len(row['answer_options']), 1, recommendation)
            self.assertEqual(row['answer_options'][0]['full'], recommendation)

    def test_plain_status_or_question_is_not_a_selectable_recommendation(self):
        for recommendation in ('合成材料已经准备完成', '目前信息不足', '是否保留合成材料？'):
            self.assertEqual(mapped(item(title='由本人确认合成材料用途', recommendation=recommendation))['answer_options'], [])
