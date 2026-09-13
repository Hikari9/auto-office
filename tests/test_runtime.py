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
