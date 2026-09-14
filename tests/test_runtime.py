import contextlib, hashlib, importlib.util, io, json, os, subprocess, tempfile, unittest
from datetime import datetime, timezone
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('office_runtime', ROOT/'scripts/office_runtime.py')
rt=importlib.util.module_from_spec(spec); spec.loader.exec_module(rt)

def cand(name, money=1, quota=1, reward=0, state='proven', caps=('builder',), floor=True, remaining=80, advisory=True, model_id='m', effort='high'):
    return {'harness':name,'harness_version':'1','model_id':model_id,'effort':effort,'adapter_state':state,'capabilities':list(caps),'absolute_floor_pass':floor,'supported_playbooks':['Change'],'advisory_pass':advisory,'local_reward':reward,'quota':{'status':'ok','tightest_remaining_percent':remaining,'projected_burn_percent':quota},'cost':{'money_estimate':money,'quota_burn':quota,'wall_clock_seconds':10}}

class RuntimeTests(unittest.TestCase):
    def test_floor_before_cost(self):
        bad=cand('cheap',money=.01,floor=False); good=cand('good',money=2)
        r=rt.route({'role':'executor','playbook':'Change','candidates':[bad,good]})
        self.assertTrue(r['selected'].startswith('good@'))
        self.assertTrue(any(x['stage']==4 for x in r['rejected']))
    def test_unverified_denied_for_executor(self):
        r=rt.route({'role':'executor','playbook':'Change','candidates':[cand('x',state='valid-unverified')]})
        self.assertIsNone(r['selected']); self.assertEqual(r['status'],'no_qualifying_candidate')
    def test_quota_reserve_changes_route(self):
        low=cand('low',money=.5,remaining=21,quota=3); safe=cand('safe',money=.6,remaining=80,quota=10)
        r=rt.route({'role':'executor','playbook':'Change','candidates':[low,safe]})
        self.assertTrue(r['selected'].startswith('safe@'))
    def test_balanced_prefers_quota_within_money_band(self):
        a=cand('a',money=10,quota=10); b=cand('b',money=11,quota=2)
        r=rt.route({'role':'executor','playbook':'Change','candidates':[a,b]})
        self.assertTrue(r['selected'].startswith('b@'))
    def test_local_reward_tiebreak(self):
        a=cand('a',reward=1); b=cand('b',reward=5)
        r=rt.route({'role':'executor','playbook':'Change','candidates':[a,b]})
        self.assertTrue(r['selected'].startswith('b@'))
    def test_preferred_seed_picks_first_choice_even_if_pricier(self):
        first=cand('agy',model_id='gemini-3.8-flash',effort='medium',money=5)
        first['invocation_model_id']='gemini-3.8-flash-preview'
        second=cand('claude',model_id='claude-sonnet-5',effort='high',money=1)
        seed=[{'harness':'agy','model_id':'gemini-3.8-flash','effort':'medium'},
              {'harness':'claude','model_id':'claude-sonnet-5','effort':'high'}]
        r=rt.route({'role':'executor','playbook':'Change','preferred_seed':seed,'candidates':[second,first]})
        self.assertTrue(r['selected'].startswith('agy@'))
        disclosure=r['selection_disclosure']
        self.assertEqual(disclosure['role'],'executor')
        self.assertEqual(disclosure['model_id'],'gemini-3.8-flash')
        self.assertEqual(disclosure['invocation_model_id'],'gemini-3.8-flash-preview')
        self.assertIn('preferred seed #1',disclosure['reason'])
    def test_preferred_seed_falls_back_when_first_choice_excluded(self):
        first=cand('agy',model_id='gemini-3.8-flash',effort='medium',floor=False)
        second=cand('claude',model_id='claude-sonnet-5',effort='high')
        seed=[{'harness':'agy','model_id':'gemini-3.8-flash','effort':'medium'},
              {'harness':'claude','model_id':'claude-sonnet-5','effort':'high'}]
        r=rt.route({'role':'executor','playbook':'Change','preferred_seed':seed,'candidates':[first,second]})
        self.assertTrue(r['selected'].startswith('claude@'))
    def test_preferred_seed_ignores_unmatched_candidates_when_a_match_exists(self):
        matched=cand('agy',model_id='gemini-3.8-flash',effort='medium',money=5)
        unmatched=cand('other',model_id='other-model',effort='high',money=.01)
        seed=[{'harness':'agy','model_id':'gemini-3.8-flash','effort':'medium'}]
        r=rt.route({'role':'executor','playbook':'Change','preferred_seed':seed,'candidates':[unmatched,matched]})
        self.assertTrue(r['selected'].startswith('agy@'))
    def test_disclosure_flags_unverified_invocation_slug(self):
        only=cand('codex',model_id='luna')
        r=rt.route({'role':'executor','playbook':'Change','candidates':[only]})
        d=r['selection_disclosure']
        self.assertEqual(d['invocation_model_id'],'luna')
        self.assertEqual(d['invocation_model_id_source'],'fallback:model_id')
        self.assertIn('unverified',d['reason'])
    def test_disclosure_marks_catalog_slug_verified(self):
        only=cand('codex',model_id='luna'); only['invocation_model_id']='gpt-5.6-luna'
        d=rt.route({'role':'executor','playbook':'Change','candidates':[only]})['selection_disclosure']
        self.assertEqual(d['invocation_model_id'],'gpt-5.6-luna')
        self.assertEqual(d['invocation_model_id_source'],'catalog')
        self.assertNotIn('unverified',d['reason'])

class RouteDefectTests(unittest.TestCase):
    class Args:
        def __init__(self, **kw): self.__dict__.update(kw)

    def _state(self, d):
        (Path(d)/'state.json').write_text('{"run_id":"r"}')
        return d

    def test_recorded_defect_blocks_until_resolved(self):
        with tempfile.TemporaryDirectory() as d:
            self._state(d)
            self.assertEqual(rt.cmd_check_route_defects(self.Args(state_dir=d)),0)
            rt.cmd_route_defect(self.Args(state_dir=d,kind='invalid-invocation-slug',
                attempted='luna',observed='unknown model',correction='gpt-5.6-luna',harness='codex'))
            self.assertEqual(rt.cmd_check_route_defects(self.Args(state_dir=d)),2)
            rows=rt.load_route_defects(d)
            self.assertEqual(rows[0]['correction'],'gpt-5.6-luna')
            rt.cmd_resolve_route_defect(self.Args(state_dir=d,id=rows[0]['id'],proposal_ref='branch/x'))
            self.assertEqual(rt.cmd_check_route_defects(self.Args(state_dir=d)),0)

    def test_defect_requires_run_state(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(rt.cmd_route_defect(self.Args(state_dir=d,kind='other',
                attempted='x',observed='y',correction=None,harness=None)),1)

class _MaturityTests(unittest.TestCase):
    def test_maturity_curve(self):
        self.assertAlmostEqual(rt.maturity_age(0),0)
        self.assertGreater(rt.maturity_age(60),60)
        self.assertLess(rt.maturity_age(100000),100)
    def test_privacy_lint(self):
        f=rt.privacy_findings('mail me at person@example.com and see https://private.example')
        self.assertTrue({x['kind'] for x in f} >= {'email','url'})
    def test_packet_schema(self):
        p={'base_sha':'abcd','task_scope':'x','observable_outcome':'works','blast_radius':'local','allowed_mutations':[],'protected_paths':[],'validation_commands':[],'known_bad_behavior_to_exclude':'old bug','self_review':'diff','rollback_or_restore_notes':'git restore'}
        self.assertEqual(rt.validate_with_schema(p,'execution-packet.schema.json'),[])

if __name__=='__main__': unittest.main()


class EffectiveConfigTests(unittest.TestCase):
    """The user tier is the only config tier outside the repo, so it is the one
    agents skip. These tests pin the merge that makes skipping it impossible."""

    def resolve(self, repo_root, user=None, overrides=None, sets=None):
        return rt.resolve_config_tiers(Path(repo_root), overrides, sets or [], user)

    def write(self, d, name, text):
        p = Path(d)/name; p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding='utf-8'); return str(p)

    def test_user_tier_adds_preferred_seed_absent_from_default(self):
        # The exact shape of ~/.config/auto-office/config.yaml: executor has no
        # preferred_seed in the plugin default, so a strict unknown-key filter
        # would silently delete the only thing the user tier is there to say.
        with tempfile.TemporaryDirectory() as d:
            u = self.write(d, 'user.yaml',
                'schema_version: 3\nroles:\n  executor:\n    preferred_seed:\n'
                '      - {harness: agy, model_id: gemini-3.8-flash, effort: medium}\n')
            cfg, tiers, warns = self.resolve(d, user=u)
            self.assertEqual(cfg['roles']['executor']['preferred_seed'][0]['model_id'], 'gemini-3.8-flash')
            # sibling keys at the same level survive the merge
            self.assertEqual(cfg['roles']['executor']['required_capabilities'], ['builder'])
            # omitted roles keep the plugin default
            self.assertEqual(cfg['roles']['planner']['preferred_seed'][0]['model_id'], 'opus')
            self.assertEqual(warns, [])
            self.assertTrue(next(t for t in tiers if t['tier'] == 'user')['present'])

    def test_repo_tier_outranks_user_tier(self):
        with tempfile.TemporaryDirectory() as d:
            u = self.write(d, 'user.yaml', 'quota: {reserve_percent: 30}\n')
            self.write(d, '.auto-office/config.yaml', 'quota: {reserve_percent: 45}\n')
            cfg, _, _ = self.resolve(d, user=u)
            self.assertEqual(cfg['quota']['reserve_percent'], 45)

    def test_cli_set_outranks_every_file_tier(self):
        with tempfile.TemporaryDirectory() as d:
            self.write(d, '.auto-office/config.yaml', 'quota: {reserve_percent: 45}\n')
            cfg, _, _ = self.resolve(d, sets=['quota.reserve_percent=5'])
            self.assertEqual(cfg['quota']['reserve_percent'], 5)

    def test_lists_replace_rather_than_concatenate(self):
        with tempfile.TemporaryDirectory() as d:
            u = self.write(d, 'user.yaml',
                'roles:\n  planner:\n    preferred_seed:\n      - {model_id: luna, effort: high}\n')
            cfg, _, _ = self.resolve(d, user=u)
            self.assertEqual(cfg['roles']['planner']['preferred_seed'],
                             [{'model_id': 'luna', 'effort': 'high'}])

    def test_hard_invariants_cannot_be_overridden(self):
        with tempfile.TemporaryDirectory() as d:
            u = self.write(d, 'user.yaml', 'hard_invariants: []\n')
            cfg, _, warns = self.resolve(d, user=u)
            self.assertIn('no_self_approval', cfg['hard_invariants'])
            self.assertEqual([w['reason'] for w in warns], ['not-configurable-ignored'])

    def test_unknown_top_level_key_warns_and_is_dropped(self):
        with tempfile.TemporaryDirectory() as d:
            u = self.write(d, 'user.yaml', 'notakey: 1\n')
            cfg, _, warns = self.resolve(d, user=u)
            self.assertNotIn('notakey', cfg)
            self.assertEqual(warns[0]['reason'], 'unknown-key-ignored')

    def test_type_mismatch_falls_back_to_lower_tier(self):
        with tempfile.TemporaryDirectory() as d:
            u = self.write(d, 'user.yaml', 'quota: {reserve_percent: "lots"}\n')
            cfg, _, warns = self.resolve(d, user=u)
            self.assertEqual(cfg['quota']['reserve_percent'], 20)
            self.assertEqual(warns[0]['reason'], 'type-mismatch-ignored')

    def test_matching_schema_version_is_not_a_warning(self):
        with tempfile.TemporaryDirectory() as d:
            u = self.write(d, 'user.yaml', 'schema_version: 3\nquota: {reserve_percent: 25}\n')
            _, _, warns = self.resolve(d, user=u)
            self.assertEqual(warns, [])

    def test_hash_is_deterministic_and_tier_sensitive(self):
        with tempfile.TemporaryDirectory() as d:
            u = self.write(d, 'user.yaml', 'quota: {reserve_percent: 33}\n')
            a, _, _ = self.resolve(d, user=u)
            b, _, _ = self.resolve(d, user=u)
            bare, _, _ = self.resolve(d)
            self.assertEqual(rt.sha256_obj(a), rt.sha256_obj(b))
            self.assertNotEqual(rt.sha256_obj(a), rt.sha256_obj(bare))

    def test_missing_user_file_is_not_an_error(self):
        with tempfile.TemporaryDirectory() as d:
            cfg, tiers, warns = self.resolve(d, user=str(Path(d)/'nope.yaml'))
            self.assertFalse(next(t for t in tiers if t['tier'] == 'user')['present'])
            self.assertEqual(warns, [])
            self.assertEqual(cfg['quota']['reserve_percent'], 20)


def _invoke(func, **kwargs):
    class Args:
        def __init__(self, **kw): self.__dict__.update(kw)
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        code = func(Args(**kwargs))
    out = buf.getvalue().strip()
    return code, (json.loads(out) if out else None)


def _init_git_repo(path):
    repo = Path(path); repo.mkdir(parents=True, exist_ok=True)
    subprocess.run(['git', 'init', '-q'], cwd=repo, capture_output=True)
    subprocess.run(['git', 'config', 'user.email', 'a@b.c'], cwd=repo, capture_output=True)
    subprocess.run(['git', 'config', 'user.name', 'test'], cwd=repo, capture_output=True)
    subprocess.run(['git', 'commit', '--allow-empty', '-q', '-m', 'init'], cwd=repo, capture_output=True)
    return repo


def _start_defaults(**overrides):
    d = dict(goal='do the thing', playbook='Change', gear=None, repo=None,
              volume=False, interview=False, adversarial=False, irreversible=False)
    d.update(overrides)
    return d


class StartCommandTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = self._tmp.name
        self.repo = _init_git_repo(Path(self.tmp)/'repo')
        self.state_home = Path(self.tmp)/'xdg-state'
        self.state_home.mkdir(parents=True, exist_ok=True)
        self.state_home = self.state_home.resolve()
        self._old_xdg = os.environ.get('XDG_STATE_HOME')
        os.environ['XDG_STATE_HOME'] = str(self.state_home)

    def tearDown(self):
        if self._old_xdg is None: os.environ.pop('XDG_STATE_HOME', None)
        else: os.environ['XDG_STATE_HOME'] = self._old_xdg
        self._tmp.cleanup()

    def _start(self, **overrides):
        return _invoke(rt.cmd_start, **_start_defaults(repo=str(self.repo), **overrides))

    def test_creates_canonical_state_dir_outside_repo_honouring_xdg_state_home(self):
        code, out = self._start(gear='direct')
        self.assertEqual(code, 0)
        state_dir = Path(out['state_dir'])
        self.assertTrue(str(state_dir).startswith(str(self.state_home)))
        self.assertIn(str(self.state_home / 'auto-office' / 'runs'), str(state_dir))
        self.assertTrue((state_dir/'state.json').exists())
        self.assertTrue((state_dir/'envelope.json').exists())
        self.assertNotIn(str(self.repo), str(state_dir))

    def test_writes_pointer_file_inside_target_repo(self):
        code, out = self._start(gear='direct')
        self.assertEqual(code, 0)
        pointer = self.repo/'.office'/'runs'/f"{out['run_id']}.ref"
        self.assertTrue(pointer.exists())
        self.assertEqual(pointer.read_text(encoding='utf-8').strip(), out['state_dir'])

    def test_creates_gitignore_when_absent(self):
        self.assertFalse((self.repo/'.gitignore').exists())
        code, out = self._start(gear='direct')
        self.assertEqual(code, 0)
        self.assertIn('.office/', (self.repo/'.gitignore').read_text(encoding='utf-8').splitlines())

    def test_appends_gitignore_entry_when_missing(self):
        (self.repo/'.gitignore').write_text('node_modules/\n*.log\n', encoding='utf-8')
        code, out = self._start(gear='direct')
        self.assertEqual(code, 0)
        lines = (self.repo/'.gitignore').read_text(encoding='utf-8').splitlines()
        self.assertIn('node_modules/', lines)
        self.assertIn('*.log', lines)
        self.assertIn('.office/', lines)

    def test_does_not_duplicate_existing_gitignore_entry(self):
        (self.repo/'.gitignore').write_text('foo\n.office/\nbar\n', encoding='utf-8')
        code, out = self._start(gear='direct')
        self.assertEqual(code, 0)
        lines = (self.repo/'.gitignore').read_text(encoding='utf-8').splitlines()
        self.assertEqual(lines.count('.office/'), 1)
        self.assertEqual(lines, ['foo', '.office/', 'bar'])

    def test_phase_is_intake(self):
        code, out = self._start(gear='direct')
        state = json.loads((Path(out['state_dir'])/'state.json').read_text(encoding='utf-8'))
        self.assertEqual(state['phase'], 'intake')

    def test_envelope_validates_against_schema(self):
        code, out = self._start(gear='direct')
        self.assertEqual(code, 0)
        envelope = json.loads((Path(out['state_dir'])/'envelope.json').read_text(encoding='utf-8'))
        self.assertEqual(rt.validate_with_schema(envelope, 'run-envelope.schema.json'), [])

    def test_stdout_contains_required_keys(self):
        code, out = self._start(gear='direct')
        for key in ('state_dir', 'run_id', 'gear', 'pointer', 'kickoff'):
            self.assertIn(key, out)

    def test_fit_test_irreversible_selects_full(self):
        code, out = self._start(irreversible=True, volume=False, interview=False, adversarial=False)
        self.assertEqual(code, 0)
        self.assertEqual(out['gear'], 'full')

    def test_fit_test_two_of_three_selects_express(self):
        code, out = self._start(irreversible=False, volume=True, interview=True, adversarial=False)
        self.assertEqual(code, 0)
        self.assertEqual(out['gear'], 'express')

    def test_fit_test_at_most_one_of_three_selects_direct(self):
        code, out = self._start(irreversible=False, volume=True, interview=False, adversarial=False)
        self.assertEqual(code, 0)
        self.assertEqual(out['gear'], 'direct')

    def test_fit_test_none_selects_direct(self):
        code, out = self._start(irreversible=False, volume=False, interview=False, adversarial=False)
        self.assertEqual(code, 0)
        self.assertEqual(out['gear'], 'direct')

    def test_explicit_gear_overrides_fit_test(self):
        code, out = self._start(gear='full', irreversible=False, volume=False, interview=False, adversarial=False)
        self.assertEqual(code, 0)
        self.assertEqual(out['gear'], 'full')


def _write_state(d, phase='planned', **extra):
    obj = {'run_id': 'r1', 'phase': phase, 'plan_version': 1,
           'spokes_loaded': {'planner': '2026-01-01T00:00:00+00:00'},
           'catalog_snapshot_hash': 'a' * 16, 'adapter_snapshot_hash': 'b' * 16,
           'policy_hash': 'c' * 16, 'effective_config_hash': 'd' * 16}
    obj.update(extra)
    state_path = Path(d)/'state.json'
    state_path.write_text(json.dumps(obj, indent=2) + '\n', encoding='utf-8')
    return obj


class ApprovePlanCommandTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.state_dir = self._tmp.name

    def tearDown(self):
        self._tmp.cleanup()

    def _read_state(self):
        return json.loads((Path(self.state_dir)/'state.json').read_text(encoding='utf-8'))

    def _approve(self, **overrides):
        kw = dict(state_dir=self.state_dir, approved_by='user', quote='ship it as-is', plan_path=None)
        kw.update(overrides)
        return _invoke(rt.cmd_approve_plan, **kw)

    def test_refuses_when_phase_is_not_planned(self):
        before = _write_state(self.state_dir, phase='intake')
        code, out = self._approve()
        self.assertEqual(code, 2)
        self.assertIsInstance(out, dict)
        self.assertEqual(self._read_state(), before)

    def test_refuses_empty_quote(self):
        _write_state(self.state_dir, phase='planned')
        code, out = self._approve(quote='   ')
        self.assertEqual(code, 2)
        self.assertIsInstance(out, dict)
        self.assertEqual(self._read_state()['phase'], 'planned')

    def test_success_sets_phase_approved_and_records_approval(self):
        _write_state(self.state_dir, phase='planned', plan_version=3)
        code, out = self._approve(approved_by='user', quote='looks correct, go ahead')
        self.assertEqual(code, 0)
        state = self._read_state()
        self.assertEqual(state['phase'], 'approved')
        approval = state['approval']
        self.assertEqual(approval['by'], 'user')
        self.assertEqual(approval['quote'], 'looks correct, go ahead')
        self.assertEqual(approval['plan_version'], 3)
        self.assertIsNone(approval['plan_sha'])
        # 'at' must be a parseable ISO8601 UTC timestamp
        datetime.fromisoformat(approval['at'].replace('Z', '+00:00'))

    def test_plan_sha_is_sha256_of_plan_path_contents(self):
        plan_path = Path(self.state_dir)/'plan.md'
        plan_path.write_text('the plan body', encoding='utf-8')
        expected = hashlib.sha256(b'the plan body').hexdigest()
        _write_state(self.state_dir, phase='planned', plan_version=1)
        code, out = self._approve(plan_path=str(plan_path))
        self.assertEqual(code, 0)
        self.assertEqual(self._read_state()['approval']['plan_sha'], 'sha256:'+expected)

    def test_reapproval_at_same_plan_version_is_idempotent_noop(self):
        _write_state(self.state_dir, phase='planned', plan_version=1)
        code1, _ = self._approve(quote='first approval')
        self.assertEqual(code1, 0)
        before = self._read_state()
        code2, out2 = self._approve(quote='first approval')
        self.assertEqual(code2, 0)
        after = self._read_state()
        self.assertEqual(before, after)

    def test_reapproval_at_different_plan_version_errors(self):
        _write_state(self.state_dir, phase='planned', plan_version=1)
        code1, _ = self._approve(quote='first approval')
        self.assertEqual(code1, 0)
        state = self._read_state()
        state['plan_version'] = 2
        (Path(self.state_dir)/'state.json').write_text(json.dumps(state, indent=2) + '\n', encoding='utf-8')
        code2, out2 = self._approve(quote='second approval')
        self.assertEqual(code2, 2)
        self.assertIsInstance(out2, dict)

    def test_preserves_pinned_fields_not_related_to_approval(self):
        before = _write_state(self.state_dir, phase='planned', plan_version=1)
        code, out = self._approve()
        self.assertEqual(code, 0)
        state = self._read_state()
        self.assertEqual(state['spokes_loaded'], before['spokes_loaded'])
        self.assertEqual(state['catalog_snapshot_hash'], before['catalog_snapshot_hash'])
        self.assertEqual(state['adapter_snapshot_hash'], before['adapter_snapshot_hash'])
        self.assertEqual(state['policy_hash'], before['policy_hash'])
        self.assertEqual(state['effective_config_hash'], before['effective_config_hash'])
