"""Isolated applicability checks; no production task/data changes."""
import os
import json
import hashlib
import tempfile
import unittest
from pathlib import Path
from activity_mvp.task_closure import Store, digest


class ApplicabilityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()
        self.path = self.root / 'article.md'
        self.path.write_text('修复这个词只是文章引用。')
        self.store = Store(self.root / 'activity.sqlite')
        self.payload = dict(platform='codex', session='isolated', turn='one',
                            objective='长文与调研', scope_root=str(self.root), complex=True,
                            closure_mode='light', artifact_paths=[str(self.path)])

    def tearDown(self):
        self.store.conn.close()
        self.tmp.cleanup()

    def begin(self, **extra):
        return self.store.begin({**self.payload, **extra})['task_id']

    def issue(self, task, key='one'):
        return self.store.register(dict(task_id=task, category='main', key=key, summary='真实未完执行',
            target_root=str(self.root), evidence=[{'path':str(self.path)}],
            authority=dict(source='isolated authorized fixture', scope={'files':[str(self.path)]},
                reversible=True, cost=False, new_permission=False, external=False, personal_choice=False),
            recommendation='核验', acceptance_rule='当前证据'))['issue']

    def test_five_platforms_light_is_not_an_audit_or_delivery_pass(self):
        for platform in ('codex','claude','hermes','workbuddy','openclaw'):
            task = self.begin(platform=platform)
            status = self.store.task_status(task)
            self.assertFalse(status['closure_required'])
            self.assertEqual(status['stop_decision']['decision'],'not_applicable')
            for field in ('audit_valid','all_complete','independent','wording_valid','delivery_ready'):
                self.assertFalse(status[field],field)
            self.assertTrue(self.store.check_final_response(task,'article')['not_applicable'])
        self.assertEqual(self.store.conn.execute('SELECT count(*) FROM closure_audits').fetchone()[0],0)
        self.assertEqual(self.store.conn.execute('SELECT count(*) FROM closure_issues').fetchone()[0],0)

    def test_declared_save_before_and_after_registration_stays_light(self):
        identifier=digest(['codex','isolated','one'])[:32]
        self.store.observe_closure_operation(identifier,[str(self.path)])
        task=self.begin()
        self.store.observe_closure_operation(task,[str(self.path)])
        self.assertFalse(self.store.task_status(task)['closure_required'])

    def test_unregistered_plain_chat_can_have_empty_artifacts(self):
        task=self.begin(artifact_paths=[])
        self.assertFalse(self.store.task_status(task)['closure_required'])

    def test_light_audit_request_does_not_create_empty_review(self):
        task=self.begin()
        self.assertEqual(self.store.audit({'task_id':task})['stop_decision']['decision'],'not_applicable')
        self.assertEqual(self.store.conn.execute('SELECT count(*) FROM closure_audits').fetchone()[0],0)

    def test_mixed_effect_upgrades_and_cannot_downgrade(self):
        task=self.begin()
        self.store.observe_closure_operation(task,[str(self.root/'runtime.py')])
        self.begin()
        self.assertEqual(self.store.task_status(task)['closure_mode'],'full')

    def test_opaque_or_publish_before_begin_cannot_be_hidden(self):
        for i,paths,operational in ((0,[],False),(1,[str(self.path)],True)):
            turn=str(i); identifier=digest(['codex','isolated',turn])[:32]
            self.store.observe_closure_operation(identifier,paths,operational=operational)
            self.assertTrue(self.store.task_status(self.begin(turn=turn))['closure_required'])

    def test_registered_and_linked_issues_force_full_even_after_cancel(self):
        first=self.begin(); item=self.issue(first)
        second=self.begin(turn='two'); self.issue(second)
        self.store.transition(item['id'],'canceled','fixture cancel',1)
        for task in (first,second): self.assertTrue(self.store.task_status(task)['closure_required'])

    def test_real_choice_preserved_when_light_registration_upgrades(self):
        task=self.begin(); item=self.issue(task)
        self.store.transition(item['id'],'blocked','fixture wait',1,'actual condition')
        self.assertTrue(self.store.task_status(task)['closure_required'])
        self.assertEqual(self.store.get(item['id'])['state'],'blocked')

    def test_directory_and_escaping_path_rejected(self):
        for path in (str(self.root),str(self.root.parent/'outside.md'),str(self.root)+'/./article.md'):
            with self.subTest(path=path),self.assertRaises(ValueError): self.begin(artifact_paths=[path])

    def test_rule_markdown_and_code_cannot_be_content(self):
        for name in ('AGENTS.md','CLAUDE.md','SKILL.md','指令要求.md','.codex/story.md','work/runtime.py','config.yaml','.zshrc','app.mjs','app.cjs','page.tsx'):
            with self.subTest(name=name),self.assertRaises(ValueError): self.begin(artifact_paths=[str(self.root/name)])

    def test_symlink_parent_and_file_rejected(self):
        link=self.root/'link.md'; link.symlink_to(self.path)
        folder=self.root/'alias'; folder.symlink_to(self.root,target_is_directory=True)
        for path in (link,folder/'article.md'):
            with self.assertRaises(ValueError): self.begin(artifact_paths=[str(path)])

    def test_executable_bit_is_not_an_ordinary_draft(self):
        self.path.chmod(0o755)
        with self.assertRaises(ValueError): self.begin()

    def test_many_saves_of_64_declared_files_do_not_upgrade(self):
        paths=[str(self.root/('draft-'+str(i)+'.md')) for i in range(64)]
        task=self.begin(artifact_paths=paths)
        self.store.observe_closure_operation(task,paths)
        for _ in range(70): self.store.observe_closure_operation(task,[paths[0]])
        self.assertFalse(self.store.task_status(task)['closure_required'])

    def test_hardlink_after_declaration_invalidates_and_sticks(self):
        task=self.begin(); alias=self.root/'other.md'; os.link(self.path,alias)
        self.assertTrue(self.store.task_status(task)['closure_required'])
        alias.unlink()
        self.assertEqual(self.store.task_status(task)['closure_mode'],'full')

    def test_rename_destination_not_declared_upgrades(self):
        task=self.begin(); moved=self.root/'moved.md'; self.path.rename(moved)
        self.store.observe_closure_operation(task,[str(self.path),str(moved)])
        self.assertTrue(self.store.task_status(task)['closure_required'])

    def test_artifacts_frozen_and_identity_not_reused(self):
        self.begin()
        with self.assertRaises(ValueError): self.begin(artifact_paths=[str(self.root/'extra.md')])
        with self.assertRaises(ValueError): self.begin(objective='new request')

    def test_legacy_complex_and_simple_keep_their_routing(self):
        payload={k:v for k,v in self.payload.items() if k not in {'closure_mode','artifact_paths'}}
        task=self.store.begin(payload)['task_id']
        self.assertTrue(self.store.task_status(task)['closure_required'])
        with self.assertRaises(ValueError): self.begin()
        simple=self.store.begin({**payload,'turn':'simple','complex':False})['task_id']
        self.assertFalse(self.store.task_status(simple)['closure_required'])

    def test_full_rebegin_does_not_reset_existing_guard(self):
        task=self.begin(closure_mode='full')
        self.store.conn.execute('INSERT INTO closure_guards VALUES(?,1)',(task,))
        self.begin()
        self.assertEqual(self.store.task_status(task)['closure_mode'],'full')
        self.assertEqual(self.store.conn.execute('SELECT attempts FROM closure_guards WHERE guard_key=?',(task,)).fetchone()[0],1)

    def test_readonly_query_changes_no_database_bytes(self):
        task=self.begin(); self.store.conn.close()
        before=(self.root/'attention-center/closure.sqlite').read_bytes()
        with Store(self.root/'activity.sqlite',readonly=True) as store:
            self.assertFalse(store.task_status(task)['closure_required'])
        self.assertEqual(before,(self.root/'attention-center/closure.sqlite').read_bytes())
        self.store=Store(self.root/'activity.sqlite')

    def test_simple_full_binds_whole_response_without_fabricating_independence(self):
        task=self.begin(closure_mode='full',complex=False)
        response=self.root/'final.md'; response.write_text('本次执行全部完成。')
        sha=hashlib.sha256(response.read_bytes()).hexdigest()
        artifact=self.root/'wording.json'; artifact.write_text(json.dumps(dict(final_response_sha256=sha,wording_approved=True)))
        status=self.store.audit(dict(task_id=task,evidence=[{'path':str(self.path)}],independent=False,
            reviewer='executor content check',wording_approved=True,wording_artifact={'path':str(artifact)},
            review={key:'checked' for key in ('main','related','discovered','failed_steps','verification_gaps','downstream')},
            final_response=dict(path=str(response),sha256=sha,completion='complete',scope='本次执行',remaining=[],
                user_action=dict(required=False,scope='本次执行',reason='已验收'))))
        self.assertTrue(status['wording_valid']); self.assertFalse(status['independent'])
        self.assertTrue(self.store.check_final_response(task,response.read_text())['valid'])
        self.assertFalse(self.store.check_final_response(task,response.read_text()+'\n')['valid'])
        artifact.write_text('{}')
        self.assertFalse(self.store.task_status(task)['wording_valid'])

