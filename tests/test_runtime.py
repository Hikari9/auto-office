import importlib.util, json, tempfile, unittest
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
