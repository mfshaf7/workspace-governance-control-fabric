#!/usr/bin/env python3
"""Read every packaged operation contract through its normal runtime loader."""

from __future__ import annotations

import importlib
import json
import os
from pathlib import Path

from jsonschema import Draft202012Validator


LOADERS = (
    ("agent_action_policy", "AgentActionContractBundle", "load"),
    ("delivery_art_contracts", "DeliveryArtContractBundle", "load"),
    ("prototype_ingress_contracts", "PrototypeIngressContractBundle", "load"),
    ("prototype_landing_contracts", "PrototypeLandingContracts", "load"),
    ("prototype_maturity_contracts", "PrototypeMaturityContracts", "load"),
    ("prototype_closure_authority", None, "load_bundle_manifest"),
    ("lifecycle_transition_contracts", "LifecycleTransitionContractBundle", "load"),
    ("repository_readiness_contracts", "RepositoryReadinessContractBundle", "load"),
    ("repository_custody_contracts", "RepositoryCustodyContractBundle", "load"),
    ("workspace_intake_contracts", "IntakeContracts", "load"),
    ("workspace_inventory_contracts", "InventoryContracts", "load"),
    ("product_publication_contracts", "ProductPublicationContracts", "load"),
)


def verify_contracts(*, require_installed: bool = True) -> int:
    for module_name, class_name, method in LOADERS:
        module = importlib.import_module(f"control_fabric_core.{module_name}")
        if require_installed and "site-packages" not in Path(module.__file__).parts:
            raise RuntimeError(f"{module_name}: smoke must use the installed package")
        target = getattr(module, class_name) if class_name else module
        # No root override: the image's own configuration must locate the bundle.
        getattr(target, method)()
    schema = Path(os.environ["WGCF_WORKSPACE_CONTINUITY_SCHEMA_PATH"])
    Draft202012Validator.check_schema(json.loads(schema.read_text(encoding="utf-8")))
    return len(LOADERS) + 1


if __name__ == "__main__":
    print(f"packaged operation contracts passed: {verify_contracts()}")
