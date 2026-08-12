from __future__ import annotations

import sys
from pathlib import Path
from unittest import TestCase


REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from scripts.validate_project import (  # noqa: E402
    ALEMBIC_REVISION_ID_MAX_LENGTH,
    validate_migration_revision_ids,
    validate_migration_revisions,
)


class MigrationContractTests(TestCase):
    def test_revision_ids_fit_alembic_version_storage(self) -> None:
        self.assertEqual(validate_migration_revisions(REPO_ROOT), [])

    def test_revision_limit_matches_alembic_default_storage(self) -> None:
        self.assertEqual(ALEMBIC_REVISION_ID_MAX_LENGTH, 32)

    def test_oversized_revision_id_is_rejected(self) -> None:
        oversized_revision = "x" * (ALEMBIC_REVISION_ID_MAX_LENGTH + 1)

        self.assertEqual(
            validate_migration_revision_ids([oversized_revision]),
            [
                "Alembic revision id exceeds the default 32-character version "
                f"column: {oversized_revision}"
            ],
        )
