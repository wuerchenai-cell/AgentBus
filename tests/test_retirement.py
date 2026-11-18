from pathlib import Path
import sys
_SOURCE = Path(__file__).resolve().parents[1]
for _entry in (str(_SOURCE), str(_SOURCE / "tests")):
    if _entry not in sys.path:
        sys.path.insert(0, _entry)
import importlib.util, tempfile, unittest, sys
from unittest.mock import patch
import test_reset_safety_20261111 as fixtures
from agentbus.claim_fencing import captured_ticket
from agentbus.bus import BusError, LateResultRejected

class RetirementTests(unittest.TestCase):
    def api(self):
        path=Path(__file__).resolve().parents[1]/"agentbus/retirement.py"
        self.assertTrue(path.is_file(), "explicit stopped-claim retirement API missing")
        spec=importlib.util.spec_from_file_location("agentbus.retirement_candidate",path)
        api=importlib.util.module_from_spec(spec);spec.loader.exec_module(api)
        base=path.parent
        for attr,relative,name in [('exclusive_lock','exclusive_lock.py','agentbus.exclusive_lock_retirement'),('TriggerDeliveryService','trigger/service.py','agentbus.trigger.service_retirement')]:
            dep_spec=importlib.util.spec_from_file_location(name,base/relative)
            dep=importlib.util.module_from_spec(dep_spec);sys.modules[name]=dep;dep_spec.loader.exec_module(dep)
            setattr(api,attr,dep if attr=='exclusive_lock' else dep.TriggerDeliveryService)
        return api

    def fixture(self, root):
        return fixtures.ResetSafetyTests().active_fixture(root)

    def test_expired_claim_can_retire_without_new_budget_or_success(self):
        api=self.api()
        with tempfile.TemporaryDirectory() as temp:
            control,forest,task=self.fixture(Path(temp));ticket=captured_ticket(task)
            job=control/"jobs"/(task.job_id+".json");tree=control/"task-tree.json"
            before=(job.read_bytes(),tree.read_bytes())
            with patch.object(api,"_now",return_value=task.deadline_ts+1):
                closed=api.retire_stopped_claim(forest.bus,"worker",task.id,expected_claim_epoch=task.claim_epoch,owner_confirmed_inactive=True,reason="executor stopped; preserve checkpoint")
            self.assertEqual(closed.status,"blocked")
            self.assertEqual((closed.deadline_ts,closed.budget_s,closed.attempt),(task.deadline_ts,task.budget_s,task.attempt))
            self.assertFalse(closed.claim_id)
            self.assertEqual(before,(job.read_bytes(),tree.read_bytes()))
            self.assertFalse((control/"agents/worker/processing"/(task.id+".json")).exists())
            self.assertFalse(forest.bus.pending("worker"))
            with self.assertRaises((LateResultRejected,BusError)):
                forest.bus.commit_result("worker",task,ticket,body="late old response")

    def test_missing_confirmation_keeps_source(self):
        api=self.api()
        with tempfile.TemporaryDirectory() as temp:
            control,forest,task=self.fixture(Path(temp));source=control/"agents/worker/processing"/(task.id+".json");before=source.read_bytes()
            for confirmation in (False,None,1,"true"):
                with self.subTest(value=confirmation),self.assertRaises(BusError):
                    api.retire_stopped_claim(forest.bus,"worker",task.id,expected_claim_epoch=task.claim_epoch,owner_confirmed_inactive=confirmation,reason="test")
            self.assertEqual(source.read_bytes(),before)

    def test_unexpired_or_stale_confirmation_is_refused(self):
        api=self.api()
        with tempfile.TemporaryDirectory() as temp:
            control,forest,task=self.fixture(Path(temp));source=control/"agents/worker/processing"/(task.id+".json");before=source.read_bytes()
            with self.assertRaises(BusError):
                api.retire_stopped_claim(forest.bus,"worker",task.id,expected_claim_epoch=task.claim_epoch,owner_confirmed_inactive=True,reason="not expired")
            with patch.object(api,"_now",return_value=task.deadline_ts+1),self.assertRaises(BusError):
                api.retire_stopped_claim(forest.bus,"worker",task.id,expected_claim_epoch=task.claim_epoch+1,owner_confirmed_inactive=True,reason="stale")
            self.assertEqual(source.read_bytes(),before)

    def call(self,api,forest,task,reason="stopped"):
        return api.retire_stopped_claim(forest.bus,"worker",task.id,expected_claim_epoch=task.claim_epoch,
                                       owner_confirmed_inactive=True,reason=reason)

    def test_terminal_record_must_remain_within_its_own_read_limit(self):
        import json
        api=self.api()
        with tempfile.TemporaryDirectory() as temp:
            control,forest,task=self.fixture(Path(temp));source=control/'agents/worker/processing'/(task.id+'.json')
            task.result='x'*(4*1024*1024-len(json.dumps(task.to_dict(),ensure_ascii=False,indent=2).encode('utf8'))-4000)
            forest.bus._write(source,task);before=source.read_bytes();self.assertLess(len(before),4*1024*1024)
            with patch.object(api,'_now',return_value=task.deadline_ts+1),self.assertRaises(BusError):
                self.call(api,forest,task,reason='r'*10000)
            self.assertEqual(source.read_bytes(),before)
            self.assertFalse((control/'agents/worker/blocked'/source.name).exists())

    def test_stop_allows_history_retirement_but_creates_no_ready_task(self):
        api=self.api()
        with tempfile.TemporaryDirectory() as temp:
            control,forest,task=self.fixture(Path(temp));(control/"STOP").write_text("stop")
            with patch.object(api,"_now",return_value=task.deadline_ts+1):self.call(api,forest,task)
            self.assertFalse(forest.bus.pending("worker"))
            self.assertTrue((control/"STOP").exists())

    def test_interrupted_source_removal_has_exact_idempotent_recovery(self):
        api=self.api()
        with tempfile.TemporaryDirectory() as temp:
            control,forest,task=self.fixture(Path(temp));source=control/"agents/worker/processing"/(task.id+".json")
            real=Path.unlink
            def fail(path,*args,**kwargs):
                if path==source:raise PermissionError("controlled deletion failure")
                return real(path,*args,**kwargs)
            with patch.object(api,"_now",return_value=task.deadline_ts+1):
                with patch.object(Path,"unlink",fail),self.assertRaises(PermissionError):self.call(api,forest,task)
                self.assertTrue(source.exists())
                a=self.call(api,forest,task);b=self.call(api,forest,task)
            self.assertEqual(a.to_dict(),b.to_dict())
            self.assertFalse(source.exists())
            self.assertEqual(sum(e.get("event")=="claim_retired" for e in forest.bus.events()),1)

    def test_final_audit_failure_can_finish_without_repeating_retirement(self):
        api=self.api()
        with tempfile.TemporaryDirectory() as temp:
            control,forest,task=self.fixture(Path(temp));audit=forest.bus.audit
            def fail(**event):
                if event.get("event")=="claim_retired":raise PermissionError("controlled audit failure")
                return audit(**event)
            with patch.object(api,"_now",return_value=task.deadline_ts+1):
                with patch.object(forest.bus,"audit",side_effect=fail),self.assertRaises(PermissionError):self.call(api,forest,task)
                self.call(api,forest,task)
            self.assertEqual(sum(e.get("event")=="claim_retirement_intent" for e in forest.bus.events()),1)
            self.assertEqual(sum(e.get("event")=="claim_retired" for e in forest.bus.events()),1)

    def test_pending_audit_or_rejected_record_is_not_overwritten(self):
        api=self.api()
        for status in ("awaiting_audit","rejected"):
            with self.subTest(status=status),tempfile.TemporaryDirectory() as temp:
                control,forest,task=self.fixture(Path(temp));source=control/"agents/worker/processing"/(task.id+".json")
                task.status=status;forest.bus._write(source,task);before=source.read_bytes()
                with patch.object(api,"_now",return_value=task.deadline_ts+1),self.assertRaises(BusError):self.call(api,forest,task)
                self.assertEqual(source.read_bytes(),before)

    def test_wrong_reason_cannot_resume_published_retirement(self):
        api=self.api()
        with tempfile.TemporaryDirectory() as temp:
            control,forest,task=self.fixture(Path(temp))
            with patch.object(api,"_now",return_value=task.deadline_ts+1):self.call(api,forest,task)
            blocked=control/"agents/worker/blocked"/(task.id+".json");before=blocked.read_bytes()
            with self.assertRaises(BusError):self.call(api,forest,task,reason="different")
            self.assertEqual(before,blocked.read_bytes())

    def test_failed_terminal_write_preserves_active_record(self):
        api=self.api()
        with tempfile.TemporaryDirectory() as temp:
            control,forest,task=self.fixture(Path(temp));source=control/"agents/worker/processing"/(task.id+".json");before=source.read_bytes()
            with patch.object(api,"_now",return_value=task.deadline_ts+1),patch.object(forest.bus,"_write",side_effect=OSError("full disk")),self.assertRaises(OSError):self.call(api,forest,task)
            self.assertEqual(before,source.read_bytes())
            self.assertFalse((control/"agents/worker/blocked"/(task.id+".json")).exists())

    def test_tampered_duplicate_after_publish_is_preserved(self):
        api=self.api()
        with tempfile.TemporaryDirectory() as temp:
            control,forest,task=self.fixture(Path(temp));source=control/"agents/worker/processing"/(task.id+".json")
            real=Path.unlink
            def fail(path,*args,**kwargs):
                if path==source:raise PermissionError("controlled")
                return real(path,*args,**kwargs)
            with patch.object(api,"_now",return_value=task.deadline_ts+1):
                with patch.object(Path,"unlink",fail),self.assertRaises(PermissionError):self.call(api,forest,task)
            current=forest.bus.read(source);current.result="changed after partial retirement";forest.bus._write(source,current);before=source.read_bytes()
            with self.assertRaises(BusError):self.call(api,forest,task)
            self.assertEqual(source.read_bytes(),before)

    def test_late_result_and_retirement_cannot_both_succeed(self):
        from concurrent.futures import ThreadPoolExecutor
        import threading
        api=self.api()
        for trial in range(3):
            with self.subTest(trial=trial),tempfile.TemporaryDirectory() as temp:
                control,forest,task=self.fixture(Path(temp));ticket=captured_ticket(task);barrier=threading.Barrier(2)
                def attempt(retiring):
                    barrier.wait(timeout=10)
                    try:
                        if retiring:self.call(api,forest,task)
                        else:forest.bus.commit_result("worker",task,ticket,body="real completed work")
                        return True
                    except (BusError,ValueError):return False
                with patch.object(api,"_now",return_value=task.deadline_ts+1),ThreadPoolExecutor(max_workers=2) as pool:
                    a=pool.submit(attempt,True);b=pool.submit(attempt,False);outcomes=[a.result(timeout=20),b.result(timeout=20)]
                self.assertEqual(sum(outcomes),1,outcomes)
                completed=(control/"agents/worker/done"/(task.id+".json")).exists()
                retired=(control/"agents/worker/blocked"/(task.id+".json")).exists()
                self.assertNotEqual(completed,retired)

    def test_retirement_enables_existing_linked_job_without_changing_parent(self):
        from agentbus import jobs
        api=self.api()
        with tempfile.TemporaryDirectory() as temp:
            control,forest,task=self.fixture(Path(temp));job=control/"jobs"/(task.job_id+".json");before=job.read_bytes()
            with patch.object(api,"_now",return_value=task.deadline_ts+1):self.call(api,forest,task)
            child=jobs.start_job(forest.cfg,"codex","Explicitly finish remaining work",request_id="retirement-child",parent_job_id=task.job_id)
            self.assertEqual(child["parent_job_id"],task.job_id)
            self.assertEqual(before,job.read_bytes())
            self.assertEqual(child["parent_snapshot"]["task_states"][0]["status"],"blocked")
            self.assertEqual(child["tasks"],[])
            self.assertGreater(child["remaining_s"],0)

    def test_two_process_retirement_and_late_result_cannot_both_win(self):
        import os,sys,json,subprocess,time
        worker = r"""
from pathlib import Path
import sys,json,time,importlib.util
from agentbus.bus import Bus,Message,BusError
from agentbus.config import load
ws=Path(sys.argv[1]);mode=sys.argv[2];spec=importlib.util.spec_from_file_location('agentbus.retire_race',sys.argv[3]);api=importlib.util.module_from_spec(spec);spec.loader.exec_module(api)
dep_spec=importlib.util.spec_from_file_location('agentbus.exclusive_lock_retire_child',Path(sys.argv[3]).parent/'exclusive_lock.py');dep=importlib.util.module_from_spec(dep_spec);dep_spec.loader.exec_module(dep);api.exclusive_lock=dep
service_spec=importlib.util.spec_from_file_location('agentbus.trigger.service_retire_child',Path(sys.argv[3]).parent/'trigger/service.py');service=importlib.util.module_from_spec(service_spec);sys.modules[service_spec.name]=service;service_spec.loader.exec_module(service);api.TriggerDeliveryService=service.TriggerDeliveryService
data=json.loads((ws/'race.json').read_text(encoding='utf8'));task=Message.from_dict(data['task'],strict=True);bus=Bus(ws/'.agentbus')
(ws/(mode+'.ready')).write_text('ready')
end=time.monotonic()+15
while not (ws/'go').exists():
 if time.monotonic()>end:raise SystemExit('barrier timeout')
 time.sleep(.005)
try:
 if mode=='retire':
  api._now=lambda:task.deadline_ts+1
  api.retire_stopped_claim(bus,'worker',task.id,expected_claim_epoch=task.claim_epoch,owner_confirmed_inactive=True,reason='stopped')
 else:bus.commit_result('worker',task,data['ticket'],body='real completed work')
 result={'ok':True}
except (ValueError,OSError,BusError) as exc:result={'ok':False,'error':type(exc).__name__}
print(json.dumps(result))
"""
        for trial in range(2):
            with self.subTest(trial=trial),tempfile.TemporaryDirectory() as temp:
                ws=Path(temp);control,forest,task=self.fixture(ws)
                (ws/'race.json').write_text(json.dumps({'task':task.to_dict(),'ticket':captured_ticket(task).to_dict()}),encoding='utf8')
                api_path=Path(__file__).resolve().parents[1]/'agentbus/retirement.py'
                children=[subprocess.Popen([sys.executable,'-B','-X','utf8','-c',worker,temp,mode,str(api_path)],stdout=subprocess.PIPE,stderr=subprocess.PIPE) for mode in ('retire','result')]
                try:
                    end=time.monotonic()+15
                    while not all((ws/(mode+'.ready')).exists() for mode in ('retire','result')):
                        if time.monotonic()>end:self.fail('child barrier timeout')
                        time.sleep(.01)
                    (ws/'go').write_text('go')
                    outcomes=[]
                    for child in children:
                        stdout,stderr=child.communicate(timeout=25)
                        self.assertEqual(child.returncode,0,stderr.decode('utf8',errors='replace'))
                        outcomes.append(json.loads(stdout)['ok'])
                    self.assertEqual(sum(outcomes),1,outcomes)
                finally:
                    for child in children:
                        if child.poll() is None:child.kill();child.wait(timeout=10)
                        if child.stdout:child.stdout.close()
                        if child.stderr:child.stderr.close()

    def test_mismatched_claim_owner_is_preserved(self):
        api=self.api()
        with tempfile.TemporaryDirectory() as temp:
            control,forest,task=self.fixture(Path(temp));source=control/"agents/worker/processing"/(task.id+".json")
            task.claim_agent="codex";forest.bus._write(source,task);before=source.read_bytes()
            with patch.object(api,"_now",return_value=task.deadline_ts+1),self.assertRaises(BusError):self.call(api,forest,task)
            self.assertEqual(before,source.read_bytes())

    def test_success_reconciles_member_busy_state(self):
        api=self.api()
        with tempfile.TemporaryDirectory() as temp:
            control,forest,task=self.fixture(Path(temp))
            with patch.object(api,"_now",return_value=task.deadline_ts+1),patch.object(api,"TriggerDeliveryService") as service:self.call(api,forest,task)
            service.return_value.on_work_state_changed.assert_called_once_with("worker",drain=False)

    def test_cli_requires_explicit_confirmation_and_uses_original_epoch(self):
        import sys,io,json
        from contextlib import redirect_stdout,redirect_stderr
        api=self.api();cli_path=Path(__file__).resolve().parents[1]/'agentbus/cli.py'
        spec=importlib.util.spec_from_file_location('agentbus.cli_retirement_candidate',cli_path);cli=importlib.util.module_from_spec(spec);spec.loader.exec_module(cli)
        with tempfile.TemporaryDirectory() as temp:
            control,forest,task=self.fixture(Path(temp));args=['retire','--workspace',temp,'--agent','worker','--id',task.id,'--claim-epoch',str(task.claim_epoch),'--reason','stopped']
            with patch.dict(sys.modules,{'agentbus.retirement':api}),patch.object(api,'_now',return_value=task.deadline_ts+1):
                with redirect_stdout(io.StringIO()),redirect_stderr(io.StringIO()):self.assertNotEqual(cli.main(args),0)
                self.assertTrue((control/'agents/worker/processing'/(task.id+'.json')).exists())
                output=io.StringIO()
                with redirect_stdout(output),redirect_stderr(io.StringIO()):self.assertEqual(cli.main(args+['--owner-confirmed-inactive']),0)
                result=json.loads(output.getvalue());self.assertTrue(result['retired']);self.assertFalse(result['delivered']);self.assertFalse(result['new_work_started'])

    def test_conflicting_same_id_in_another_mailbox_is_preserved(self):
        from agentbus.bus import Message
        api=self.api()
        with tempfile.TemporaryDirectory() as temp:
            control,forest,task=self.fixture(Path(temp));source=control/"agents/worker/processing"/(task.id+".json")
            duplicate=Message.from_dict(task.to_dict(),strict=True);duplicate.to='codex';target=forest.bus.dirs('codex')['processing']/(task.id+'.json');forest.bus._write(target,duplicate)
            before=(source.read_bytes(),target.read_bytes())
            with patch.object(api,'_now',return_value=task.deadline_ts+1),self.assertRaises(BusError):self.call(api,forest,task)
            self.assertEqual(before,(source.read_bytes(),target.read_bytes()))

    def test_committed_result_waiting_for_archive_is_not_retired(self):
        api=self.api()
        with tempfile.TemporaryDirectory() as temp:
            control,forest,task=self.fixture(Path(temp));ticket=captured_ticket(task)
            with patch.object(forest.bus,'_finish_receipt',side_effect=OSError('controlled archive failure')),self.assertRaises(OSError):
                forest.bus.commit_result('worker',task,ticket,body='real result already submitted')
            self.assertIsNotNone(forest.bus.find_result(task.id,sender='worker',recipient=task.result_to))
            source=control/'agents/worker/processing'/(task.id+'.json');before=source.read_bytes()
            with patch.object(api,'_now',return_value=task.deadline_ts+1),self.assertRaises(BusError):self.call(api,forest,task)
            self.assertEqual(before,source.read_bytes())

    def test_new_physical_lock_generation_is_not_removed(self):
        import json
        from agentbus.exclusive_lock import lock_record_path
        api=self.api()
        with tempfile.TemporaryDirectory() as temp:
            control,forest,task=self.fixture(Path(temp));source=control/'agents/worker/processing'/(task.id+'.json');before=source.read_bytes()
            lock=lock_record_path(control,task.id);record=json.loads(lock.read_text(encoding='utf8'));record['claim_epoch']=task.claim_epoch+1;record['claim_id']='b'*32;lock.write_text(json.dumps(record),encoding='utf8');before_lock=lock.read_bytes()
            with patch.object(api,'_now',return_value=task.deadline_ts+1),self.assertRaises((BusError,ValueError)):self.call(api,forest,task)
            self.assertEqual(before,source.read_bytes());self.assertEqual(before_lock,lock.read_bytes())

    def test_hardlinked_task_is_rejected_before_reading_payload(self):
        import os
        api=self.api()
        with tempfile.TemporaryDirectory() as temp:
            control,forest,task=self.fixture(Path(temp));source=control/'agents/worker/processing'/(task.id+'.json');alias=Path(temp)/'outside-copy.json';os.link(source,alias);before=source.read_bytes()
            with patch.object(api,'_now',return_value=task.deadline_ts+1),self.assertRaises((BusError,ValueError)):self.call(api,forest,task)
            self.assertEqual(before,source.read_bytes());self.assertEqual(before,alias.read_bytes())

    def test_audit_config_job_and_mutex_hardlinks_do_not_change_outside_file(self):
        import os,hashlib
        for kind in ('events','config','job','operation','physical'):
            with self.subTest(kind=kind),tempfile.TemporaryDirectory() as temp:
                api=self.api();control,forest,task=self.fixture(Path(temp))
                table={'events':control/'events.jsonl','config':control/'config.json','job':control/'jobs'/(task.job_id+'.json'),
                       'operation':control/'operation-locks'/(hashlib.sha256(('worker\0'+task.id).encode()).hexdigest()+'.lock'),
                       'physical':control/'exclusive-locks'/(task.id+'.lock.json')}
                source=table[kind];outside=Path(temp)/'outside.json';os.link(source,outside);before=outside.read_bytes()
                with patch.object(api,'_now',return_value=task.deadline_ts+1),self.assertRaises((BusError,ValueError)):self.call(api,forest,task)
                self.assertEqual(before,outside.read_bytes())
                self.assertTrue((control/'agents/worker/processing'/(task.id+'.json')).is_file())

    def test_tampered_allocated_deadline_or_body_cannot_authorize_retirement(self):
        for field,value in [('deadline_ts',1.0),('body','changed intent'),('budget_s',1)]:
            with self.subTest(field=field),tempfile.TemporaryDirectory() as temp:
                api=self.api();control,forest,task=self.fixture(Path(temp));source=control/'agents/worker/processing'/(task.id+'.json')
                changed=forest.bus.read(source);setattr(changed,field,value);forest.bus._write(source,changed);before=source.read_bytes()
                with patch.object(api,'_now',return_value=task.deadline_ts+1),self.assertRaises(BusError):self.call(api,forest,task)
                self.assertEqual(before,source.read_bytes())

    def test_new_lock_after_terminal_publish_is_never_released(self):
        import json
        from agentbus.exclusive_lock import lock_record_path
        api=self.api()
        with tempfile.TemporaryDirectory() as temp:
            control,forest,task=self.fixture(Path(temp));write=forest.bus._write;lock=lock_record_path(control,task.id);changed=[]
            def inject(path,message):
                write(path,message)
                if path.parent.name=='blocked':
                    info=json.loads(lock.read_text(encoding='utf8'));info['claim_epoch']=task.claim_epoch+1;info['claim_id']='c'*32;lock.write_text(json.dumps(info),encoding='utf8');changed.append(lock.read_bytes())
            with patch.object(api,'_now',return_value=task.deadline_ts+1),patch.object(forest.bus,'_write',side_effect=inject),self.assertRaises((BusError,ValueError)):self.call(api,forest,task)
            self.assertEqual(lock.read_bytes(),changed[0])
            with self.assertRaises((BusError,ValueError)):self.call(api,forest,task)
            self.assertEqual(lock.read_bytes(),changed[0])

    def test_non_draining_state_sync_does_not_submit_a_bound_contact(self):
        from types import SimpleNamespace
        api=self.api()
        with tempfile.TemporaryDirectory() as temp:
            control,forest,task=self.fixture(Path(temp))
            with patch.object(api,'_now',return_value=task.deadline_ts+1):self.call(api,forest,task)
            service=api.TriggerDeliveryService(Path(temp))
            with patch('agentbus.trigger.contacts.load_contact',return_value=SimpleNamespace(status='bound')),patch.object(service,'_drain_member') as drain:
                result=service.on_work_state_changed('worker',drain=False)
            self.assertEqual(result,{'status':'idle','drain_skipped':True});drain.assert_not_called()

    def test_oversized_task_is_refused_with_bounded_read(self):
        api=self.api()
        with tempfile.TemporaryDirectory() as temp:
            control,forest,task=self.fixture(Path(temp));source=control/'agents/worker/processing'/(task.id+'.json');source.write_bytes(b' '* (4*1024*1024+1))
            with patch.object(api,'_now',return_value=task.deadline_ts+1),self.assertRaises((BusError,ValueError)):self.call(api,forest,task)
            self.assertEqual(source.stat().st_size,4*1024*1024+1)
