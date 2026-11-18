"""Codex 2026-10-05: regressions against the preserved ZCode candidate."""
import sys
from pathlib import Path
_ROOT_DIR = Path(__file__).resolve().parents[1]
if str(_ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(_ROOT_DIR))
if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))

import copy
import unittest
from datetime import datetime, timedelta

from agentbus.capability_card import assess_member, validate_card

NOW="2026-10-05T20:40:00+08:00"


def card():
    return dict(schema_version=1,member_id="alpha",display_name="Alpha",
                host_version="1.0",model_id="model-a",route="desktop-file",
                tools=["Read","Write"],specialties=["review"],limitations=[],
                source="observed bounded sample",updated_at=NOW,
                samples=[dict(project_id="project-a",task_id="m-task",result_id="m-result",
                              capability_family="file_write",outcome="done",verified=True,
                              artifact_sha256="a"*64,transport="desktop-transcription",
                              observed_at="2026-10-05T20:39:00+08:00",version="1.0",
                              model="model-a",route="desktop-file")])


def requirements(**updates):
    return dict(dict(tools=["Read","Write"],capability_family="file_write"),**updates)


def context(**updates):
    return dict(dict(permission_allowed=True,available_tools=["Read","Write"],
                     project_id="project-a",host_version="1.0",model_id="model-a",
                     route="desktop-file"),**updates)


class CapabilityIntegrationBoundaryTests(unittest.TestCase):
    def test_matching_verified_sample_is_recommended(self):
        out=assess_member(card(),requirements(),context(),NOW)
        self.assertTrue(out["eligible"])
        self.assertEqual(out["recommendation"],"recommended")

    def test_unverified_done_is_never_called_verified(self):
        value=card(); value["samples"][0]["verified"]=False
        out=assess_member(value,requirements(),context(),NOW)
        self.assertNotEqual(out["recommendation"],"recommended")
        self.assertNotIn("SAMPLE_VERIFIED",out["reasons"])
        self.assertTrue(out["eligible"])
        self.assertTrue(out["bounded_scope_required"])

    def test_future_observation_is_not_current_verification(self):
        value=card(); value["samples"][0]["observed_at"]="2026-10-06T00:00:00+08:00"
        out=assess_member(value,requirements(require_current_sample=True),context(),NOW)
        self.assertFalse(out["eligible"])
        self.assertNotEqual(out["recommendation"],"recommended")
        self.assertIn("FUTURE_OBSERVATION", " ".join(out["reasons"]))

    def test_explicit_age_policy_makes_old_sample_pending_retest(self):
        value=card(); value["samples"][0]["observed_at"]="2026-10-04T20:39:00+08:00"
        out=assess_member(value,requirements(max_evidence_age_s=3600),context(),NOW)
        self.assertNotEqual(out["recommendation"],"recommended")
        self.assertIn("file_write",out["pending_retest"])
        self.assertIn("EXPIRED", " ".join(out["reasons"]))
        required=assess_member(value,requirements(max_evidence_age_s=3600,require_current_sample=True),context(),NOW)
        self.assertFalse(required["eligible"])

    def test_no_fixed_age_policy_rejects_nothing_for_age_alone(self):
        value=card(); value["samples"][0]["observed_at"]="2020-01-01T00:00:00Z"
        out=assess_member(value,requirements(),context(),NOW)
        self.assertEqual(out["recommendation"],"recommended")
        self.assertIn("EVIDENCE_AGE_SECONDS", " ".join(out["reasons"]))

    def test_age_boundary_is_inclusive(self):
        value=card(); value["samples"][0]["observed_at"]=(datetime.fromisoformat(NOW)-timedelta(seconds=3600)).isoformat()
        out=assess_member(value,requirements(max_evidence_age_s=3600),context(),NOW)
        self.assertEqual(out["recommendation"],"recommended")

    def test_historical_other_project_is_reference_not_current_verification(self):
        out=assess_member(card(),requirements(),context(project_id="project-b"),NOW)
        self.assertTrue(out["eligible"])
        self.assertNotEqual(out["recommendation"],"recommended")
        self.assertNotIn("SAMPLE_VERIFIED",out["reasons"])
        self.assertIn("CROSS_PROJECT_HISTORY", " ".join(out["reasons"]))

    def test_route_drift_prevents_current_sample_requirement(self):
        out=assess_member(card(),requirements(require_current_sample=True),context(route="acp"),NOW)
        self.assertFalse(out["eligible"])
        self.assertIn("file_write",out["pending_retest"])

    def test_versions_models_and_routes_must_all_match_current_requirement(self):
        for update in (dict(host_version="2.0"),dict(model_id="different"),dict(route="acp")):
            with self.subTest(update=update):
                out=assess_member(card(),requirements(require_current_sample=True),context(**update),NOW)
                self.assertFalse(out["eligible"])
                self.assertNotEqual(out["recommendation"],"recommended")

    def test_unknown_sample_version_model_and_route_are_not_verified(self):
        for field in ("version","model","route"):
            for unknown in (None,"unknown"):
                value=card(); value["samples"][0][field]=unknown
                with self.subTest(field=field,unknown=unknown):
                    out=assess_member(value,requirements(require_current_sample=True),context(),NOW)
                    self.assertFalse(out["eligible"])
                    self.assertNotIn("SAMPLE_VERIFIED",out["reasons"])

    def test_unknown_current_version_model_and_route_are_not_verified(self):
        for field in ("host_version","model_id","route"):
            for unknown in (None,"unknown"):
                with self.subTest(field=field,unknown=unknown):
                    out=assess_member(card(),requirements(require_current_sample=True),context(**{field:unknown}),NOW)
                    self.assertFalse(out["eligible"])

    def test_context_must_match_card_software_and_route_for_current_verification(self):
        value=card(); value["host_version"]="2.0"
        out=assess_member(value,requirements(require_current_sample=True),context(),NOW)
        self.assertFalse(out["eligible"])

    def test_verified_sample_without_result_reference_is_not_current_proof(self):
        value=card(); value["samples"][0]["result_id"]=None
        out=assess_member(value,requirements(require_current_sample=True),context(),NOW)
        self.assertFalse(out["eligible"])

    def test_write_sample_without_artifact_digest_is_not_current_proof(self):
        value=card(); value["samples"][0]["artifact_sha256"]=None
        out=assess_member(value,requirements(require_current_sample=True),context(),NOW)
        self.assertFalse(out["eligible"])

    def test_readonly_analysis_sample_can_use_verified_result_without_file_digest(self):
        value=card(); value["samples"][0].update(artifact_sha256=None,capability_family="analysis")
        out=assess_member(value,requirements(tools=["Read"],capability_family="analysis"),context(),NOW)
        self.assertEqual(out["recommendation"],"recommended")

    def test_invalid_required_and_optional_sample_text_is_rejected(self):
        for field,invalid in (("version",3),("model",{}),("route",[]),("transport",False),
                              ("artifact_sha256","not-a-digest"),("task_id","")):
            value=card(); value["samples"][0][field]=invalid
            with self.subTest(field=field,invalid=invalid),self.assertRaises(ValueError): validate_card(value)

    def test_latest_sample_uses_actual_timestamp_not_lexicographic_offset(self):
        value=card(); failed=copy.deepcopy(value["samples"][0])
        failed.update(task_id="m-failed",result_id="m-failed-result",outcome="failed",
                      observed_at="2026-10-05T12:39:30Z")
        value["samples"].append(failed)
        out=assess_member(value,requirements(),context(),NOW)
        self.assertEqual(out["recommendation"],"probe_first")
        self.assertIn("RECENT_FAILURE", " ".join(out["reasons"]))

    def test_schema_version_requires_explicit_integer_one(self):
        for invalid in (True,1.0,None,"1"):
            value=card(); value["schema_version"]=invalid
            with self.subTest(invalid=invalid),self.assertRaises(ValueError):
                validate_card(value)
        value=card(); value.pop("schema_version")
        with self.assertRaises(ValueError): validate_card(value)

    def test_unknown_public_fields_are_not_silently_removed(self):
        for field in ("online","auth","owner","permission_allowed","command","made_up"):
            value=card(); value[field]=True
            with self.subTest(field=field),self.assertRaises(ValueError): validate_card(value)
            value=card(); value["samples"][0][field]=True
            with self.subTest(sample_field=field),self.assertRaises(ValueError): validate_card(value)

    def test_sample_identity_and_dates_are_typed(self):
        for field,invalid in (("project_id",[]),("task_id",7),("result_id",False),
                              ("capability_family",{}),("observed_at",123),
                              ("observed_at","tomorrow"),("observed_at","2026-10-05T20:40:00")):
            value=card(); value["samples"][0][field]=invalid
            with self.subTest(field=field,invalid=invalid),self.assertRaises(ValueError): validate_card(value)

    def test_member_id_cannot_be_path_or_windows_reserved_name(self):
        for identity in ("../secret","nul","CON","name/part"):
            value=card(); value["member_id"]=identity
            with self.subTest(identity=identity),self.assertRaises(ValueError): validate_card(value)

    def test_now_invalid_or_timezone_missing_fails_explicitly(self):
        for now in (None,1,"tomorrow","2026-10-05T20:40:00"):
            with self.subTest(now=now),self.assertRaises(ValueError):
                assess_member(card(),requirements(),context(),now)

    def test_age_policy_and_required_sample_flags_are_typed(self):
        for age in (True,"3600",-1,0,float("nan"),float("inf")):
            with self.subTest(age=age),self.assertRaises(ValueError):
                assess_member(card(),requirements(max_evidence_age_s=age),context(),NOW)
        with self.assertRaises(ValueError):
            assess_member(card(),requirements(require_current_sample="yes"),context(),NOW)

    def test_tools_and_permission_are_current_hard_limits(self):
        for ctx in (context(permission_allowed=False),context(available_tools=[])):
            out=assess_member(card(),requirements(),ctx,NOW)
            self.assertFalse(out["eligible"])
            self.assertEqual(out["recommendation"],"not_recommended")
        for tools in ([{}],[1],"Read"):
            with self.subTest(tools=tools),self.assertRaises(ValueError):
                assess_member(card(),requirements(),context(available_tools=tools),NOW)

    def test_unknown_capability_can_take_authorized_bounded_task(self):
        value=card(); value["samples"]=[]
        out=assess_member(value,requirements(),context(),NOW)
        self.assertTrue(out["eligible"])
        self.assertEqual(out["recommendation"],"probe_first")
        self.assertTrue(out["bounded_scope_required"])

    def test_brand_and_input_order_do_not_change_recommendation(self):
        a=card(); b=copy.deepcopy(a); b["member_id"]="another-brand"
        b["display_name"]="Another Brand"
        before=copy.deepcopy((a,b))
        left=assess_member(a,requirements(),context(),NOW)
        right=assess_member(b,requirements(),context(),NOW)
        self.assertEqual(left["recommendation"],right["recommendation"])
        self.assertEqual(left["reasons"],right["reasons"])
        self.assertEqual((a,b),before)


if __name__=="__main__": unittest.main()
