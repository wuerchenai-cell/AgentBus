"""Windows final-path spellings must not masquerade as workspace escapes."""
from pathlib import Path
import os,sys,tempfile,unittest
from unittest.mock import patch

SOURCE=Path(__file__).resolve().parents[1]
for candidate in (SOURCE,SOURCE/'tests'):
    if str(candidate) not in sys.path:sys.path.insert(0,str(candidate))
from agentbus import project_plan
from agentbus.config import load

@unittest.skipUnless(os.name=='nt','Windows final-path spelling regression')
class PlanPathSpellingTests(unittest.TestCase):
    def test_equivalent_extended_target_or_root_is_not_a_workspace_escape(self):
        for extended in ('target','root','both'):
            with self.subTest(extended=extended),tempfile.TemporaryDirectory() as tmp:
                cfg=load(Path(tmp),create=True);target=cfg.workspace/'.agentbus/leases/.mutation.lock'
                real=Path.resolve
                def spelling(path,*args,**kwargs):
                    resolved=real(path,*args,**kwargs)
                    if (path==target and extended in ('target','both')) or (path==cfg.workspace and extended in ('root','both')):
                        return Path('\\\\?\\'+str(resolved))
                    return resolved
                with patch.object(Path,'resolve',spelling):
                    self.assertEqual(project_plan._checked(cfg,'.agentbus/leases/.mutation.lock'),target)

    def test_different_resolved_directory_is_still_refused(self):
        with tempfile.TemporaryDirectory() as tmp,tempfile.TemporaryDirectory() as outside:
            cfg=load(Path(tmp),create=True);target=cfg.workspace/'.agentbus/leases/.mutation.lock';real=Path.resolve
            def escape(path,*args,**kwargs):
                return Path('\\\\?\\'+str(Path(outside)/'file')) if path==target else real(path,*args,**kwargs)
            with patch.object(Path,'resolve',escape),self.assertRaises(project_plan.ProjectPlanError):
                project_plan._checked(cfg,'.agentbus/leases/.mutation.lock')

    def test_concurrent_proposal_accepts_equivalent_lock_path_spelling(self):
        from concurrent.futures import ThreadPoolExecutor
        from agentbus.bus import Bus
        with tempfile.TemporaryDirectory() as tmp:
            cfg=load(Path(tmp),create=True);Bus(cfg.workspace/'.agentbus').init();real=Path.resolve
            def spelling(path,*args,**kwargs):
                resolved=real(path,*args,**kwargs)
                return Path('\\\\?\\'+str(resolved)) if path.name=='.mutation.lock' else resolved
            with patch.object(Path,'resolve',spelling),ThreadPoolExecutor(max_workers=3) as pool:
                rows=list(pool.map(lambda _:project_plan.propose_plan(cfg,'codex','# Explicit plan','Race spelling regression',['workbuddy'],None,'same'),range(3)))
            self.assertEqual(len({row['id'] for row in rows}),1)
            self.assertEqual(len(list((cfg.workspace/'.agentbus/project-plan/proposals').glob('*.json'))),1)

    def test_unc_drive_and_device_namespace_boundaries(self):
        base=Path(r'I:\project')
        unc=Path(r'\\server\share\project')
        for path,root,allowed in [
            (r'\\?\I:\project\file',base,True),
            (r'\\?\J:\project\file',base,False),
            (r'\\?\I:\project-other\file',base,False),
            (r'\\?\UNC\server\share\project\file',unc,True),
            (r'\\?\UNC\server\other\project\file',unc,False),
            (r'\\?\UNC\server\share\project-other\file',unc,False),
            (r'\\?\GLOBALROOT\Device\HarddiskVolume1\project\file',base,False),
            (r'\\.\I:\project\file',base,False),
        ]:
            with self.subTest(path=path):
                self.assertEqual(project_plan._comparison_path(Path(path)).is_relative_to(root),allowed)

if __name__=='__main__':unittest.main()
