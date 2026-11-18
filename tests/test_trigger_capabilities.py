#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""test_trigger_capabilities.py: 能力推导、三态证据体系与对外等级评估测试。"""

import unittest

from agentbus.trigger.capabilities import (
    CapabilityTier,
    CapabilityEvidence,
    EffectiveCapabilities,
    derive_capabilities,
)


class TriggerCapabilitiesTests(unittest.TestCase):
    def test_declared_only_stays_unverified(self):
        declared = {
            "can_identify_target": True,
            "can_check_project": True,
            "can_submit_existing": True,
            "can_route_exact_id": True,
            "can_detect_busy": True,
            "host_queue_verified": True,
            "atomic_target_submit": True,
            "can_report_native_start": True,
            "can_report_native_finish": True,
            "can_reconcile": True,
        }
        # 1. No evidence at all -> file_only
        eff1 = derive_capabilities(
            declared=declared,
            runtime_host_version="26.10.1",
            connector_version="1.0.0",
            evidence_list=[],
        )
        self.assertEqual(eff1.overall_tier, CapabilityTier.FILE_ONLY.value)

        # 2. Only target identification verified -> binding_only, cannot be automatic_exact
        eff2 = derive_capabilities(
            declared=declared,
            runtime_host_version="26.10.1",
            connector_version="1.0.0",
            evidence_list=[
                CapabilityEvidence(name="can_identify_target", value=True, basis="tested", host_version="26.10.1")
            ],
        )
        self.assertNotEqual(eff2.overall_tier, CapabilityTier.AUTOMATIC_EXACT.value)
        self.assertEqual(eff2.overall_tier, CapabilityTier.BINDING_ONLY.value)
        self.assertEqual(eff2.get_basis("can_route_exact_id"), "declared")

    def test_effective_capabilities_derivation_with_tested_evidence(self):
        declared = {
            "can_identify_target": True,
            "can_check_project": True,
            "can_submit_existing": True,
            "can_route_exact_id": True,
            "can_detect_busy": True,
            "host_queue_verified": True,
            "atomic_target_submit": True,
            "can_report_native_start": True,
            "can_report_native_finish": True,
            "can_reconcile": True,
        }
        evidence = [
            CapabilityEvidence(name="can_identify_target", value=True, basis="tested", host_version="26.10.1", connector_version="1.0.0"),
            CapabilityEvidence(name="can_check_project", value=True, basis="tested", host_version="26.10.1", connector_version="1.0.0"),
            CapabilityEvidence(name="can_submit_existing", value=True, basis="tested", host_version="26.10.1", connector_version="1.0.0"),
            CapabilityEvidence(name="can_route_exact_id", value=True, basis="tested", host_version="26.10.1", connector_version="1.0.0"),
            CapabilityEvidence(name="can_detect_busy", value=True, basis="tested", host_version="26.10.1", connector_version="1.0.0"),
            CapabilityEvidence(name="atomic_target_submit", value=True, basis="tested", host_version="26.10.1", connector_version="1.0.0"),
            CapabilityEvidence(name="can_report_native_start", value=True, basis="tested", host_version="26.10.1", connector_version="1.0.0"),
            CapabilityEvidence(name="can_report_native_finish", value=True, basis="tested", host_version="26.10.1", connector_version="1.0.0"),
            CapabilityEvidence(name="can_reconcile", value=True, basis="tested", host_version="26.10.1", connector_version="1.0.0"),
            CapabilityEvidence(name="host_queue_verified", value=True, basis="tested", host_version="26.10.1", connector_version="1.0.0"),
        ]

        eff = derive_capabilities(
            declared=declared,
            runtime_host_version="26.10.1",
            connector_version="1.0.0",
            evidence_list=evidence,
        )

        self.assertEqual(eff.overall_tier, CapabilityTier.AUTOMATIC_EXACT.value)
        self.assertEqual(eff.get_basis("can_route_exact_id"), "tested")
        self.assertTrue(eff.is_capable("can_route_exact_id"))

    def test_version_change_pauses_affected_capabilities(self):
        declared = {"can_route_exact_id": True, "can_submit_existing": True}
        evidence = [
            CapabilityEvidence(name="can_route_exact_id", value=True, basis="tested", host_version="26.10.1", connector_version="1.0.0"),
            CapabilityEvidence(name="can_submit_existing", value=True, basis="tested", host_version="26.10.1", connector_version="1.0.0"),
        ]

        # Host upgraded to 27.0.0
        eff = derive_capabilities(
            declared=declared,
            runtime_host_version="27.0.0",
            connector_version="1.0.0",
            evidence_list=evidence,
        )

        # Evidence was for 26.10.1, so on 27.0.0 it requires recheck and cannot be automatic_exact
        self.assertNotEqual(eff.overall_tier, CapabilityTier.AUTOMATIC_EXACT.value)
        self.assertEqual(eff.get_basis("can_route_exact_id"), "stale_version")
        self.assertFalse(eff.is_capable("can_route_exact_id"))


if __name__ == "__main__":
    unittest.main()
