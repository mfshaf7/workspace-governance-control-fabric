from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest import TestCase
from unittest.mock import patch

from sqlalchemy import create_engine, inspect


REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "packages/control_fabric_core/src"))

from control_fabric_core.database import DATABASE_URL_ENV, database_settings, redact_database_url
from control_fabric_core.db import metadata
from control_fabric_core.foundation import status_snapshot


EXPECTED_TABLES = {
    "authority_references",
    "control_receipts",
    "escalation_records",
    "governance_edges",
    "governance_nodes",
    "ledger_events",
    "readiness_decisions",
    "source_snapshots",
    "validation_plans",
    "validation_runs",
}


class DatabaseFoundationTests(TestCase):
    def test_metadata_declares_graph_receipt_and_ledger_tables(self) -> None:
        self.assertTrue(EXPECTED_TABLES.issubset(metadata.tables))

    def test_metadata_can_create_in_memory_schema_for_fast_validation(self) -> None:
        engine = create_engine("sqlite:///:memory:")
        metadata.create_all(engine)
        table_names = set(inspect(engine).get_table_names())

        self.assertTrue(EXPECTED_TABLES.issubset(table_names))

    def test_database_status_redacts_passwords(self) -> None:
        raw_url = "postgresql+psycopg://wgcf:secret@example.local:5432/wgcf"

        self.assertEqual(
            redact_database_url(raw_url),
            "postgresql+psycopg://wgcf:***@example.local:5432/wgcf",
        )

    def test_status_snapshot_reports_database_without_revealing_secret(self) -> None:
        raw_url = "postgresql+psycopg://wgcf:secret@example.local:5432/wgcf"
        with patch.dict(os.environ, {DATABASE_URL_ENV: raw_url}, clear=False):
            snapshot = status_snapshot(REPO_ROOT)

        database = snapshot["database"]
        self.assertTrue(database["configured_from_env"])
        self.assertIn("***", database["safe_url"])
        self.assertNotIn("url", database)
        self.assertNotIn("secret", database["safe_url"])

    def test_database_settings_defaults_to_postgres_driver(self) -> None:
        settings = database_settings()

        self.assertTrue(settings.url.startswith("postgresql+psycopg://"))
