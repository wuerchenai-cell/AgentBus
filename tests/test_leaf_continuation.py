from pathlib import Path
import sys
_SOURCE = Path(__file__).resolve().parents[1]
for _entry in (str(_SOURCE), str(_SOURCE / "tests")):
    if _entry not in sys.path:
        sys.path.insert(0, _entry)
import importlib.util,json,tempfile,unittest
from unittest.mock import patch
from agentbus import jobs
from agentbus.bus import BusError,LateResultRejected
from agentbus.claim_fencing import captured_ticket
import test_retirement as retire_fixtures

class LeafContinuationTests(unittest.TestCase):
    def api(self):
        p=Path(__file__).resolve().parents[1]/'agentbus/leaf_continuation.py'
        self.assertTrue(p.is_file(),'task-tree continuation entry is missing')
        spec=importlib.util.spec_from_file_location('agentbus.leaf_continuation_candidate',p);api=importlib.util.module_from_spec(spec)
        import sys
        retire=retire_fixtures.RetirementTests().api()
        with patch.dict(sys.modules,{'agentbus.retirement':retire}):spec.loader.exec_module(api)
        return api

    def fixture(self,temp):
        f=retire_fixtures.RetirementTests();control,forest,task=f.fixture(Path(temp));api=f.api()
        with patch.object(api,'_now',return_value=task.deadline_ts+1):f.call(api,forest,task)
        return control,forest,task

    def test_retired_leaf_gets_distinct_claimable_successor_with_preserved_history(self):
        api=self.api()
        with tempfile.TemporaryDirectory() as temp:
            control,forest,old=self.fixture(temp);job=control/'jobs'/(old.job_id+'.json');oldfile=control/'agents/worker/blocked'/(old.id+'.json');before=(job.read_bytes(),oldfile.read_bytes())
            result=api.continue_leaf(forest,'A',caller='codex',expected_task_id=old.id,expected_claim_epoch=old.claim_epoch,goal='Explicit new unit: finish remaining output',request_id='continue-a')
            newleaf=result['leaf'];self.assertNotEqual(newleaf['id'],'A');self.assertNotEqual(newleaf['job_id'],old.job_id)
            self.assertEqual(forest.resolve_leaf('A')['task_id'],old.id)
            self.assertEqual(before,(job.read_bytes(),oldfile.read_bytes()))
            fresh=forest.claim_leaf(newleaf['id'],'codex');self.assertGreater(fresh.deadline_ts,old.deadline_ts)
            with self.assertRaises((BusError,LateResultRejected)):forest.bus.commit_result('codex',fresh,captured_ticket(old),body='old executor cannot submit new work')
            self.assertEqual(jobs._load(forest.cfg,newleaf['job_id'])['parent_job_id'],old.job_id)

    def test_same_request_is_idempotent_and_changed_goal_cannot_reuse_it(self):
        api=self.api()
        with tempfile.TemporaryDirectory() as temp:
            control,forest,old=self.fixture(temp);kwargs=dict(caller='codex',expected_task_id=old.id,expected_claim_epoch=old.claim_epoch,goal='Explicit continuation',request_id='same')
            a=api.continue_leaf(forest,'A',**kwargs);b=api.continue_leaf(forest,'A',**kwargs)
            self.assertEqual(a['leaf']['id'],b['leaf']['id']);self.assertEqual(len(forest.snapshot()['leaves']),2)
            with self.assertRaises((ValueError,BusError)):api.continue_leaf(forest,'A',**{**kwargs,'goal':'different work'})

    def test_wrong_task_or_epoch_does_not_create_a_new_job(self):
        api=self.api()
        with tempfile.TemporaryDirectory() as temp:
            control,forest,old=self.fixture(temp);before=set((control/'jobs').glob('*.json'));tree=(control/'task-tree.json').read_bytes()
            for task,epoch in [('m-wrong',old.claim_epoch),(old.id,old.claim_epoch+1)]:
                with self.subTest(task=task,epoch=epoch),self.assertRaises((ValueError,BusError)):
                    api.continue_leaf(forest,'A',caller='codex',expected_task_id=task,expected_claim_epoch=epoch,goal='explicit',request_id='wrong')
            self.assertEqual(set((control/'jobs').glob('*.json')),before);self.assertEqual((control/'task-tree.json').read_bytes(),tree)

    def test_cli_binds_the_new_leaf_to_a_distinct_linked_job(self):
        import sys,io,json
        from contextlib import redirect_stdout,redirect_stderr
        api=self.api();path=Path(__file__).resolve().parents[1]/'agentbus/cli.py';spec=importlib.util.spec_from_file_location('agentbus.cli_tree_continue',path);cli=importlib.util.module_from_spec(spec);spec.loader.exec_module(cli)
        with tempfile.TemporaryDirectory() as temp:
            control,forest,old=self.fixture(temp);args=['tree','continue','--workspace',temp,'--leaf','A','--agent','codex','--id',old.id,'--claim-epoch',str(old.claim_epoch),'--goal','Explicitly complete remaining work','--request-id','cli-retry']
            with patch.dict(sys.modules,{'agentbus.leaf_continuation':api}):
                output=io.StringIO()
                with redirect_stdout(output),redirect_stderr(io.StringIO()):self.assertEqual(cli.main(args),0)
                result=json.loads(output.getvalue());self.assertFalse(result['old_task_delivered']);self.assertNotEqual(result['leaf']['id'],'A')

    def test_stop_or_live_claim_cannot_create_continuation(self):
        api=self.api()
        for stop in (False,True):
            with self.subTest(stop=stop),tempfile.TemporaryDirectory() as temp:
                if stop:
                    control,forest,old=self.fixture(temp);(control/'STOP').write_text('stop')
                else:control,forest,old=retire_fixtures.RetirementTests().fixture(Path(temp))
                before=(control/'task-tree.json').read_bytes();jobs_before=set((control/'jobs').glob('*.json'))
                with self.assertRaises((ValueError,BusError)):api.continue_leaf(forest,'A',caller='codex',expected_task_id=old.id,expected_claim_epoch=old.claim_epoch,goal='Explicit new work',request_id='reject')
                self.assertEqual(before,(control/'task-tree.json').read_bytes());self.assertEqual(jobs_before,set((control/'jobs').glob('*.json')))

    def test_invalid_successor_tree_never_allocates_orphan_job(self):
        api=self.api()
        for kind in ('title','capacity'):
            with self.subTest(kind=kind),tempfile.TemporaryDirectory() as temp:
                control,forest,old=self.fixture(temp);tree=forest._structure()
                if kind=='title':tree['leaves'][0]['title']='x'*100000
                else:
                    for i in range(999):tree['leaves'].append({'id':'FILL-'+str(i),'parent_id':'B','title':'filler'})
                forest._write(tree);before=(control/'task-tree.json').read_bytes();before_jobs=set((control/'jobs').glob('*.json'))
                for _ in range(2):
                    with self.assertRaises((ValueError,BusError)):api.continue_leaf(forest,'A',caller='codex',expected_task_id=old.id,expected_claim_epoch=old.claim_epoch,goal='Explicit new work',request_id='bounds')
                self.assertEqual(set((control/'jobs').glob('*.json')),before_jobs);self.assertEqual((control/'task-tree.json').read_bytes(),before)

    def test_source_changed_between_observation_and_locked_read_is_rejected(self):
        api=self.api()
        with tempfile.TemporaryDirectory() as temp:
            control,forest,old=self.fixture(temp);read=api._read
            def changed(public,path):
                value=read(public,path);value.claim_epoch+=1;return value
            before=set((control/'jobs').glob('*.json'))
            with patch.object(api,'_read',side_effect=changed),self.assertRaises((ValueError,BusError)):
                api.continue_leaf(forest,'A',caller='codex',expected_task_id=old.id,expected_claim_epoch=old.claim_epoch,goal='Explicit work',request_id='stale-snapshot')
            self.assertEqual(set((control/'jobs').glob('*.json')),before)

    def test_exact_bounded_lookup_avoids_global_message_index(self):
        api=self.api()
        with tempfile.TemporaryDirectory() as temp:
            control,forest,old=self.fixture(temp)
            with patch.object(forest,'_message',side_effect=AssertionError('unbounded lookup')):
                result=api.continue_leaf(forest,'A',caller='codex',expected_task_id=old.id,expected_claim_epoch=old.claim_epoch,goal='Explicit work',request_id='bounded')
            self.assertFalse(result['old_task_delivered'])

    def test_duplicate_source_or_oversized_record_is_refused_without_new_job(self):
        api=self.api()
        for kind in ('duplicate','oversized'):
            with self.subTest(kind=kind),tempfile.TemporaryDirectory() as temp:
                control,forest,old=self.fixture(temp);source=control/'agents/worker/blocked'/(old.id+'.json')
                if kind=='duplicate':
                    duplicate=control/'agents/worker/done'/source.name;duplicate.write_bytes(source.read_bytes())
                else:source.write_bytes(b' '* (4*1024*1024+1))
                before=set((control/'jobs').glob('*.json'))
                with self.assertRaises((ValueError,BusError)):
                    api.continue_leaf(forest,'A',caller='codex',expected_task_id=old.id,expected_claim_epoch=old.claim_epoch,goal='Explicit work',request_id='bad-source')
                self.assertEqual(set((control/'jobs').glob('*.json')),before)
