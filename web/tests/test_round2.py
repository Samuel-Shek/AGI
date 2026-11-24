"""Synthetic regressions for round 2; never invokes a real Core or listener."""
import copy
import json
from unittest.mock import patch
from test_sync_center import Base, ROOT
from psi_sync.state import category, mapped, version, walk
from psi_sync.core_reader import normalize_snapshot
from psi_sync.presentation import recommendation_excerpt, short_title
from psi_sync.storage import LocalStore
from psi_sync.state import State


def closure(**changes):
    return dict(id='synthetic-closure', kind='closure', state='decision',
                requires_decision=True, title='合成服务需要修复',
                recommendation='助手检查服务', **changes)


class Round2Tests(Base):
    def test_technical_decision_moves_to_automatic(self):
        row = mapped(closure())
        self.assertEqual(row['status'], 'automatic')
        self.assertEqual(row['reason'], '系统/助手侧事项，无需你决定')

    def test_explicit_human_actions_survive_technical_context(self):
        for recommendation in ('由本人完成签名', '由本人完成最终资金动作', '请本人确认授权范围', '需要本人选择保留范围'):
            with self.subTest(recommendation=recommendation):
                item = closure()
                item['recommendation'] = recommendation
                self.assertEqual(category(item), 'attention')
                self.assertTrue(mapped(item)['question'])

    def test_negated_conditional_and_record_mentions_are_not_human_actions(self):
        for recommendation in ('助手检查签名格式', '无需本人签名，由助手检查服务',
                               '如果以后需要本人签名，再讨论', '禁止由本人签名',
                               '不允许本人亲自操作', '助手读取本人选择记录'):
            item = closure()
            item['recommendation'] = recommendation
            self.assertEqual(category(item), 'automatic', recommendation)

    def test_activity_uses_exact_source_question_and_choices(self):
        row = mapped({'id': 'synthetic-activity', 'kind': 'activity', 'requires_decision': True,
                      'title': '内部标题', 'card': {'display_title': '合成活动标题',
                      'display_question': '这次你希望每周回顾，还是每月回顾？',
                      'answer_options': ['每周回顾', {'label': '每月回顾'}]}})
        self.assertEqual(row['title'], '合成活动标题')
        self.assertEqual(row['question'], '这次你希望每周回顾，还是每月回顾？')
        self.assertEqual([x['label'] for x in row['answer_options']], ['每周回顾', '每月回顾'])

    def test_source_labels_and_actor_identifiers(self):
        for kind, label in {'activity':'活动记录', 'closure':'闭环事项', 'memory':'记忆',
                            'system':'系统', 'reference':'资料', 'unexpected':'PSI'}.items():
            row = mapped({'id':'synthetic-id', 'kind':kind,
                          'summary':'待你决定 · 负责人：<agent:12345678-0000-4000-8000-000000000003>',
                          'title':'负责人 codex:12345678-0000-4000-8000-000000000003'})
            self.assertEqual(row['source_label'], label)
            self.assertNotIn('12345678', row['summary'] + row['title'])
            self.assertNotIn('agent:', row['summary'])

    def test_fixture_personal_first_and_counts(self):
        listing = self.state.listing()
        self.assertEqual(listing['counts'], {'attention':5,'automatic':2,'waiting':2,'closed':2,'all':11})
        kinds = [x['kind'] for x in listing['items']]
        self.assertEqual(kinds, ['closure', 'closure', 'activity', 'activity', 'activity'])
        self.assertEqual(listing['items'][0]['id'], 'demo-signature')
        self.assertEqual(self.state.view()['metrics']['needs'], 5)

    def test_option_confirmation_records_locally_and_hides(self):
        row = self.state.detail('demo-event')
        option = row['answer_options'][0]
        before = copy.deepcopy(self.state.items)
        payload = self.payload('answer_choice', choice=option['value'], content=option['label'])
        with patch.object(self.reader, '_raw', side_effect=AssertionError('No Core calls')):
            result = self.state.write('demo-event', 'action', payload)
            self.state.write('demo-event', 'action', payload)
        self.assertEqual(result['message'], '已在本机记录，待同步')
        self.assertEqual(self.state.items, before)
        self.assertEqual(len(self.store.read()['actions']), 1)
        self.assertEqual(self.store.read()['actions'][0]['choice'], option['value'])
        self.assertNotIn('demo-event', [x['id'] for x in self.state.listing()['items']])
        self.assertEqual(self.state.listing()['counts']['attention'], 4)
        self.state.refresh()
        self.assertEqual(self.state.listing()['counts']['closed'], 3)

    def test_forged_or_stale_choices_rejected(self):
        for extra in ({'choice':'forged'}, {'choice':'forged','revision':'old'}):
            with self.assertRaises(ValueError):
                self.state.write('demo-event', 'action', {**self.payload('answer_choice'), **extra})
        self.assertFalse(self.store.read()['actions'])

    def test_closure_question_fallback_does_not_invent(self):
        item = closure()
        item.update(title='合成材料需要本人确认用途，完整范围仍需确认', recommendation='由本人确认用途')
        self.assertEqual(mapped(item)['question_subject'], item['title'])
        self.assertEqual(mapped(item)['question'], '这件事需要你怎么定？')
        item.update(title='合成签名由本人完成', recommendation='先准备合成材料')
        self.assertEqual(mapped(item)['question'], '签名如何安排？')

    def test_narrow_chat_layout_keeps_header_and_anchored_composer(self):
        css = (ROOT/'static/app.css').read_text().split('@media (max-width: 1179px)')[-1]
        self.assertIn('.conversation-header', css)
        self.assertIn('display: flex', css)
        self.assertIn('.conversation-messages', css)
        self.assertIn('overflow-y: auto', css)
        self.assertIn('.composer-wrap', css)

    def test_reused_punctuation_rules_preserve_quoted_clauses_and_fallback(self):
        for raw, expected in (
            ('已定位1枚AGP和3枚AEP：需要本人选择', '已定位1枚AGP和3枚AEP'),
            ('合成设备 "AEP-7 (rev.2, B型)" 已到货；待本人确认', '合成设备 "AEP-7 (rev.2, B型)" 已到货'),
            ('合成材料需要本人确认的；完整背景仍在原文', '合成材料需要本人确认的；完整背景仍在原文')):
            self.assertEqual(short_title(raw), expected)
        for raw in ('先保留用于', '如果完成核验，再决定', '先核对（合成材料', '先核对“将材料运往”', '合成材料'*30):
            self.assertEqual(recommendation_excerpt(raw), '')

    def test_recommendation_button_keeps_full_conditions_in_confirmation(self):
        row = self.state.detail('demo-signature')
        option = row['answer_options'][0]
        self.assertEqual(option['label'], '由本人完成签名')
        self.assertIn('签名前需本人核对内容', option['full'])
        payload = self.payload('answer_choice', 'demo-signature', choice=option['value'], content=option['full'])
        self.state.write('demo-signature', 'action', payload)
        fresh = State(self.reader, LocalStore(self.directory, self.reader.cache_identity))
        fresh.refresh()
        self.assertEqual(fresh.detail('demo-signature')['local_answer']['content'], option['full'])
        self.assertNotIn('demo-signature', [r['id'] for r in fresh.listing()['items']])
        with self.assertRaises(ValueError):
            self.state.write('demo-signature', 'action', {**payload, 'content':option['label'], 'request_id':'shortened'})

    def test_material_choices_and_unknown_closures_follow_accepted_rules(self):
        item = closure()
        item.update(title='原始录音是否保留？', recommendation='先保留，仅用于个人回顾')
        self.assertEqual(category(item), 'attention')
        self.assertEqual(mapped(item)['question'], item['title'])
        item.update(title='合成待核对事项', recommendation='暂无建议')
        self.assertEqual(category(item), 'waiting')
        self.assertEqual(mapped(item)['answer_options'], [])

    def test_no_generic_or_old_background_question_is_invented(self):
        item = closure()
        item.update(title='合成材料用途待确认', background='旧背景：签名由本人完成。', recommendation='先核对材料')
        self.assertEqual(mapped(item)['question_subject'], item['title'])
        self.assertEqual(mapped(item)['question'], '这件事需要你怎么定？')
        item.update(title='合成签名样例需要本人确认', recommendation='确认签名样例')
        self.assertEqual(mapped(item)['question_subject'], item['title'])
        self.assertEqual(mapped(item)['question'], '这件事需要你怎么定？')

    def test_overview_sanitizes_actor_and_explains_technical_routing(self):
        row = next(r for r in self.state.view()['automatic'] if r['id']=='demo-technical')
        self.assertEqual(row['explanation'], '系统/助手侧事项，无需你决定')
        for key in ('title', 'summary', 'background', 'reason', 'recommendation'):
            self.assertNotRegex(row[key], r'agent:|codex:|11111111')
        self.assertIn('负责人：助手', row['summary'])

    def test_all_attention_rows_have_questions_and_new_revision_reopens_choice(self):
        self.assertTrue(all(row['question'] for row in self.state.listing()['items']))
        row = self.state.detail('demo-history-pending')
        option = row['answer_options'][0]
        self.state.write(row['id'], 'action', self.payload('answer_choice', row['id'], choice=option['value'], content=option['full']))
        historical = next(r for r in self.state.view()['history'][0]['items'] if r['id']==row['id'])
        self.assertEqual(historical['status'], 'closed')
        self.state.items[row['id']]['revision'] = 'synthetic-next'
        self.assertIn(row['id'], [r['id'] for r in self.state.listing()['items']])

    def test_supplied_snapshot_shape_reclassified_using_synthetic_text_only(self):
        items = []
        for i in range(42):
            item = closure()
            item.update(id=f'synthetic-closure-{i}', summary='待你决定 · 负责人：<agent:11111111-2222-3333-4444-555555555555>')
            if i < 6:
                item['recommendation'] = '由本人完成签名，仅限合成材料'
            item['background'] = item['title']
            items.append(item)
        for state, count in [('verification',374), ('blocked',333), ('complete',88)]:
            for i in range(count):
                item = closure()
                item.update(id=f'synthetic-{state}-{i}', state=state, requires_decision=False)
                items.append(item)
        children = [dict(id=f'synthetic-activity-{i}', kind='activity', requires_decision=True,
                         card={'display_title':'合成活动', 'display_question':'你参加了吗？', 'answer_options':['参加了','没有参加']}) for i in range(8)]
        raw = dict(interface='attention-center', contract=1, status='ok', decision_count=50,
                   closures=items, decisions=[], systems=[], resources=[dict(id='synthetic-history',kind='historical_questions',children=children)])
        self.state.snapshot = normalize_snapshot(raw)
        self.state.items = {row['id']:row for col in ('closures','resources') for row in walk(self.state.snapshot[col])}
        listing = self.state.listing()
        self.assertEqual(listing['counts'], {'attention':14,'automatic':410,'waiting':333,'closed':88,'all':845})
        self.assertEqual([row['kind'] for row in listing['items']], ['closure']*6+['activity']*8)
        self.assertEqual(self.state.view()['metrics']['needs'], 14)
