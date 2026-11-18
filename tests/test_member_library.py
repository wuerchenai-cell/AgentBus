"""Codex, 2026-10-05: isolated public-card storage acceptance."""
import sys
from pathlib import Path
_ROOT_DIR = Path(__file__).resolve().parents[1]
if str(_ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(_ROOT_DIR))
if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))

import copy
import hashlib
import json
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace

from agentbus.member_library import MemberLibrary, store_project_card
from agentbus.lease import LeaseManager


def card():
    return dict(schema_version=1, member_id="sample-agent", display_name="Sample",
                host_version="unknown", model_id="unknown", route="desktop-file",
                tools=["read"], specialties=["review"], limitations=["write unverified"],
                samples=[], source="bounded public sample", updated_at="2026-10-05T20:40:00+08:00")


def sample():
    return dict(project_id="project-a", task_id="m-sample", result_id="m-result",
                capability_family="file-read", outcome="done", verified=True,
                artifact_sha256="a"*64, transport="desktop-transcription",
                observed_at="2026-10-05T20:40:00+08:00", version=None,
                model=None, route="desktop-file")


class PublicMemberLibraryTests(unittest.TestCase):
    def test_round_trip_and_no_mutation(self):
        with tempfile.TemporaryDirectory() as td:
            library = MemberLibrary(Path(td)/"public")
            value=card(); before=copy.deepcopy(value)
            result=library.save(value,"MISSING")
            self.assertEqual(library.get(value["member_id"]),value)
            self.assertEqual(value,before)
            self.assertEqual(hashlib.sha256(Path(result["path"]).read_bytes()).hexdigest(),result["sha256"])

    def test_cas_refuses_overwrite(self):
        with tempfile.TemporaryDirectory() as td:
            library=MemberLibrary(Path(td)); first=library.save(card(),"MISSING")
            with self.assertRaisesRegex(ValueError,"CAS_MISMATCH"):
                library.save(card(),"MISSING")
            library.save(card(),first["sha256"])

    def test_private_and_traversal_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            for name in ("../escape", "CON", ".workbuddy", "x/y"):
                value=card();value["member_id"]=name
                with self.assertRaisesRegex(ValueError,"INVALID_CARD"):
                    MemberLibrary(Path(td)).save(value,"MISSING")
            with self.assertRaisesRegex(ValueError,"UNSAFE_PATH"):
                MemberLibrary(Path(td)/".workbuddy"/"library")

    def test_unknown_and_permission_fields_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            for field in ("token","permission_allowed","online","command"):
                value=card();value[field]="do not inherit"
                with self.assertRaisesRegex(ValueError,"INVALID_CARD"):
                    MemberLibrary(Path(td)).save(value,"MISSING")

    def test_two_real_projects_only_import_public_card(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td); library=MemberLibrary(root/"library")
            library.save(card(),"MISSING")
            for name in ("project-a","project-b"):
                cfg=SimpleNamespace(workspace=root/name,data={"workers":{"sample-agent":{"enabled":True},"codex":{"enabled":True}}})
                result=store_project_card(cfg,"codex",library.get("sample-agent"),"MISSING")
                self.assertTrue(Path(result["path"]).is_file())
                self.assertFalse((cfg.workspace/".agentbus"/"sessions").exists())

    def test_unknown_member_not_imported(self):
        with tempfile.TemporaryDirectory() as td:
            cfg=SimpleNamespace(workspace=Path(td),data={"workers":{"codex":{"enabled":True}}})
            with self.assertRaisesRegex(ValueError,"UNKNOWN_MEMBER"):
                store_project_card(cfg,"codex",card(),"MISSING")

    def test_oversize_rejected_before_write(self):
        with tempfile.TemporaryDirectory() as td:
            value=card();value["source"]="x"*65536
            with self.assertRaisesRegex(ValueError,"INVALID_CARD"):
                MemberLibrary(Path(td)).save(value,"MISSING")

    def test_schema_must_be_integer_one_not_boolean_or_float(self):
        with tempfile.TemporaryDirectory() as td:
            for version in (True, 1.0, None, "1"):
                value=card(); value["schema_version"]=version
                with self.subTest(version=version), self.assertRaisesRegex(ValueError,"INVALID_CARD"):
                    MemberLibrary(Path(td)).save(value,"MISSING")

    def test_explicit_unknown_version_and_model_can_be_none(self):
        with tempfile.TemporaryDirectory() as td:
            value=card(); value["host_version"]=None; value["model_id"]=None
            library=MemberLibrary(Path(td)); library.save(value,"MISSING")
            self.assertEqual(library.get(value["member_id"]),value)

    def test_actual_zcode_normalized_sample_fields_are_preserved(self):
        with tempfile.TemporaryDirectory() as td:
            value=card(); value["samples"]=[sample()]
            library=MemberLibrary(Path(td)); library.save(value,"MISSING")
            self.assertEqual(library.get(value["member_id"])["samples"],[sample()])

    def test_legacy_sample_host_fields_are_not_silently_kept(self):
        with tempfile.TemporaryDirectory() as td:
            item=sample(); item.pop("version"); item.pop("model")
            value=card(); value["samples"]=[dict(item,host_version="wrong-contract")]
            with self.assertRaisesRegex(ValueError,"INVALID_CARD"):
                MemberLibrary(Path(td)).save(value,"MISSING")

    def test_invalid_sample_types_and_missing_identity_are_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            mutations=(dict(outcome="success"),dict(project_id=42),dict(task_id=[]),
                       dict(result_id=True),dict(verified=1),dict(artifact_sha256="bad"),
                       dict(transport=7),dict(observed_at="not-a-time"),dict(version=3),
                       dict(model={}),dict(route=[]))
            for update in mutations:
                item=sample(); item.pop("version"); item.pop("model")
                value=card(); value["samples"]=[dict(item,**update)]
                with self.subTest(update=update),self.assertRaisesRegex(ValueError,"INVALID_CARD"):
                    MemberLibrary(Path(td)).save(value,"MISSING")
            for field in ("project_id","task_id","capability_family","outcome","verified","observed_at"):
                value=card(); item=sample(); item.pop("version"); item.pop("model"); item.pop(field); value["samples"]=[item]
                with self.subTest(missing=field),self.assertRaisesRegex(ValueError,"INVALID_CARD"):
                    MemberLibrary(Path(td)).save(value,"MISSING")

    def test_unknown_and_private_sample_fields_are_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            for field in ("online","auth","permission_allowed","credentials","owner","command"):
                value=card(); value["samples"]=[dict(sample(),**{field:True})]
                with self.subTest(field=field),self.assertRaisesRegex(ValueError,"INVALID_CARD"):
                    MemberLibrary(Path(td)).save(value,"MISSING")

    def test_disabled_member_and_disabled_caller_cannot_import(self):
        with tempfile.TemporaryDirectory() as td:
            for subject in ("sample-agent","codex"):
                workers={"sample-agent":{"enabled":True},"codex":{"enabled":True}}
                workers[subject]["enabled"]=False
                cfg=SimpleNamespace(workspace=Path(td),data={"workers":workers})
                with self.subTest(subject=subject),self.assertRaisesRegex(ValueError,"UNKNOWN_MEMBER"):
                    store_project_card(cfg,"codex",card(),"MISSING")

    def test_enabled_flag_requires_explicit_boolean_true(self):
        with tempfile.TemporaryDirectory() as td:
            for enabled in (None,1,"true"):
                cfg=SimpleNamespace(workspace=Path(td),data={"workers":{
                    "sample-agent":{"enabled":enabled},"codex":{"enabled":True}}})
                with self.subTest(enabled=enabled),self.assertRaisesRegex(ValueError,"UNKNOWN_MEMBER"):
                    store_project_card(cfg,"codex",card(),"MISSING")

    def test_foreign_real_lease_prevents_write(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td); target=root/"sample-agent.json"
            manager=LeaseManager(root/".agentbus")
            held=manager.acquire(str(target),"other-member",ttl_s=60)
            self.assertIsNotNone(held)
            try:
                with self.assertRaisesRegex(ValueError,"LEASE_BUSY"):
                    MemberLibrary(root).save(card(),"MISSING")
                self.assertFalse(target.exists())
            finally:
                self.assertTrue(manager.release(held))

    def test_two_threads_cannot_both_commit_same_missing_digest(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td); barrier=threading.Barrier(2); results=[]
            def write(index):
                value=card(); value["source"]="writer "+str(index)
                barrier.wait()
                try: results.append(("saved",MemberLibrary(root).save(value,"MISSING")))
                except ValueError as exc: results.append(("rejected",str(exc)))
            threads=[threading.Thread(target=write,args=(i,)) for i in range(2)]
            for thread in threads: thread.start()
            for thread in threads: thread.join(5); self.assertFalse(thread.is_alive())
            self.assertEqual([item[0] for item in results].count("saved"),1)
            rejection=next(item[1] for item in results if item[0]=="rejected")
            self.assertTrue(rejection.startswith(("LEASE_BUSY:","CAS_MISMATCH:")))
            self.assertIn(MemberLibrary(root).get("sample-agent")["source"],("writer 0","writer 1"))

    def test_duplicate_json_keys_in_stored_card_are_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            library=MemberLibrary(Path(td)); result=library.save(card(),"MISSING")
            path=Path(result["path"]); raw=path.read_text(encoding="utf-8")
            path.write_text(raw.replace('"schema_version": 1','"schema_version": true, "schema_version": 1'),encoding="utf-8")
            with self.assertRaisesRegex(ValueError,"INVALID_CARD"):
                library.get("sample-agent")

    def test_reparse_point_is_rejected(self):
        import os
        import subprocess
        with tempfile.TemporaryDirectory() as td:
            root=Path(td); real=root/"real"; real.mkdir(); alias=root/"alias"
            if os.name=="nt":
                made=subprocess.run(["cmd","/c","mklink","/J",str(alias),str(real)],capture_output=True)
                if made.returncode:
                    self.skipTest("junction creation unavailable")
            else:
                alias.symlink_to(real,target_is_directory=True)
            try:
                with self.assertRaisesRegex(ValueError,"UNSAFE_PATH"):
                    MemberLibrary(alias).save(card(),"MISSING")
                self.assertEqual(list(real.iterdir()),[])
            finally:
                if os.name=="nt": alias.rmdir()
                else: alias.unlink()

    def test_other_host_private_roots_are_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            for name in (".codex",".codebuddy",".zcode",".opencode",".trae"):
                with self.subTest(name=name),self.assertRaisesRegex(ValueError,"UNSAFE_PATH"):
                    MemberLibrary(Path(td)/name/"member-library")

    def test_directory_target_is_rejected_as_unsafe(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td); (root/"sample-agent.json").mkdir()
            with self.assertRaisesRegex(ValueError,"UNSAFE_PATH"):
                MemberLibrary(root).get("sample-agent")

    def test_linked_lease_directory_is_rejected_before_any_external_write(self):
        import os
        import subprocess
        with tempfile.TemporaryDirectory() as td:
            root=Path(td)/"public"; root.mkdir(); (root/".agentbus").mkdir()
            outside=Path(td)/"outside"; outside.mkdir(); alias=root/".agentbus"/"leases"
            if os.name=="nt":
                made=subprocess.run(["cmd","/c","mklink","/J",str(alias),str(outside)],capture_output=True)
                if made.returncode: self.skipTest("junction creation unavailable")
            else: alias.symlink_to(outside,target_is_directory=True)
            try:
                with self.assertRaisesRegex(ValueError,"UNSAFE_PATH"):
                    MemberLibrary(root).save(card(),"MISSING")
                self.assertEqual(list(outside.iterdir()),[])
            finally:
                if os.name=="nt": alias.rmdir()
                else: alias.unlink()

    def test_64kib_exact_utf8_boundary_is_accepted_and_next_byte_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            value=card()
            size=len((json.dumps(value,ensure_ascii=False,sort_keys=True,indent=2)+"\n").encode("utf-8"))
            value["source"]+="x"*(65536-size)
            library=MemberLibrary(Path(td)); result=library.save(value,"MISSING")
            self.assertEqual(Path(result["path"]).stat().st_size,65536)
            value["source"]+="x"
            with self.assertRaisesRegex(ValueError,"INVALID_CARD"):
                library.save(value,result["sha256"])
            self.assertEqual(Path(result["path"]).stat().st_size,65536)

    def test_corrupt_and_oversize_stored_json_preserved(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td); path=root/"sample-agent.json"
            for raw in (b"invalid json",b"\xff",b"x"*65537):
                path.write_bytes(raw)
                with self.subTest(raw_size=len(raw)),self.assertRaisesRegex(ValueError,"INVALID_CARD"):
                    MemberLibrary(root).get("sample-agent")
                self.assertEqual(path.read_bytes(),raw)

    def test_library_root_must_be_directory(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td)/"ordinary-file"; root.write_bytes(b"keep")
            with self.assertRaisesRegex(ValueError,"UNSAFE_PATH"):
                MemberLibrary(root)
            self.assertEqual(root.read_bytes(),b"keep")

    def test_invalid_caller_types_fail_with_registration_error(self):
        with tempfile.TemporaryDirectory() as td:
            cfg=SimpleNamespace(workspace=Path(td),data={"workers":{
                "sample-agent":{"enabled":True},"codex":{"enabled":True}}})
            for caller in (None, [], {}, 1):
                with self.subTest(caller=caller),self.assertRaisesRegex(ValueError,"UNKNOWN_MEMBER"):
                    store_project_card(cfg,caller,card(),"MISSING")


if __name__ == '__main__':
    unittest.main()
