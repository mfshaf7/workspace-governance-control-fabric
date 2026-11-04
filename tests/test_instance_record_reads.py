from __future__ import annotations

import os
from unittest import TestCase
from unittest.mock import patch

from fastapi.testclient import TestClient
from sqlalchemy import event

from control_fabric_core.governance_history import GovernanceHistoryAuthorizer
from control_fabric_core.instance_record_reads import InstanceRecordReadService, build_instance_record_read_runtime
from control_fabric_core.instance_records import InstanceRecordUnavailable
from wgcf_api.app import create_app
import test_instance_records as fixtures


class InstanceRecordReadTests(TestCase):
    def setUp(self):
        self.fixture = fixtures.InstanceRecordStoreTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.service = InstanceRecordReadService(store=self.fixture.store, instance_id="installation-a")
        self.result = self.fixture.apply(self.fixture.command())
        self.auth = GovernanceHistoryAuthorizer(caller_id="oos:test-reader", caller_secret="a" * 32)
        self.headers = {"x-wgcf-caller-id": "oos:test-reader", "x-wgcf-caller-secret": "a" * 32}
        self.client = TestClient(create_app(instance_record_reads=self.service, governance_history_authorizer=self.auth))
        self.base = "/v1/instance-records/repositories"

    def test_detail_and_history_require_authenticated_reader(self):
        for suffix in ("", "/client-project", "/client-project/history"):
            self.assertEqual(self.client.get(self.base + suffix).status_code, 401)
            wrong = {**self.headers, "x-wgcf-caller-secret": "b" * 32}
            self.assertEqual(self.client.get(self.base + suffix, headers=wrong).status_code, 401)
        detail = self.client.get(self.base + "/client-project", headers=self.headers)
        self.assertEqual(detail.status_code, 200)
        self.assertEqual(detail.json()["record"], self.result["record"])
        self.assertEqual(detail.json()["authority"], {"kind": "internal-instance-record", "instance_id": "installation-a"})
        history = self.client.get(self.base + "/client-project/history", headers=self.headers)
        self.assertEqual(history.status_code, 200)
        self.assertEqual(len(history.json()["records"]), 1)

    def test_compact_list_has_no_descriptions_or_retained_commands(self):
        response = self.client.get(self.base, headers=self.headers)
        self.assertEqual(response.status_code, 200)
        item = response.json()["records"][0]
        self.assertEqual(item["record_id"], "repo:client-project")
        self.assertEqual(item["provider_repository"], self.result["record"]["provider_repository"])
        self.assertNotIn("notes", item)
        self.assertNotIn("decision", item)
        self.assertNotIn("command", item)

    def test_selected_status_is_one_indexed_query_with_explicit_selection_and_no_history_or_foreign_installation(self):
        self.fixture.apply(self.fixture.command(name="foreign-only", instance="installation-b", key="foreign"))
        queries = []
        def observe(connection, cursor, statement, parameters, context, executemany): queries.append(statement)
        event.listen(self.fixture.engine, "before_cursor_execute", observe)
        self.addCleanup(event.remove, self.fixture.engine, "before_cursor_execute", observe)
        target = "/v1/instance-records/repository-status"
        self.assertEqual(self.client.get(target, params=[("name", "client-project")]).status_code, 401)
        response = self.client.get(target, params=[("name", "client-project"), ("name", "foreign-only"), ("name", "missing")], headers=self.headers)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(queries), 1)
        body = response.json()
        self.assertEqual(body["record_ids"], ["repo:client-project", "repo:foreign-only", "repo:missing"])
        self.assertEqual([row["record_id"] for row in body["records"]], ["repo:client-project"])
        self.assertNotIn("notes", body["records"][0]); self.assertNotIn("command", response.text)
        for names in ([], ["client-project"] * 2, ["missing", "client-project"], [f"project-{n:02}" for n in range(26)], ["../escape"]):
            denied = self.client.get(target, params=[("name", name) for name in names], headers=self.headers)
            self.assertEqual(denied.status_code, 400)
        self.assertEqual(len(queries), 1)

    def test_unknown_installation_selector_or_query_is_rejected(self):
        for suffix in ("?instance_id=installation-b", "/client-project?instance_id=installation-b", "/client-project/history?unexpected=true"):
            self.assertEqual(self.client.get(self.base + suffix, headers=self.headers).status_code, 400)
        self.fixture.apply(self.fixture.command(name="other-installation-only", instance="installation-b"))
        absent = self.client.get(self.base + "/other-installation-only", headers=self.headers)
        self.assertEqual(absent.status_code, 200)
        self.assertEqual(absent.json()["authority"]["instance_id"], "installation-a")
        self.assertFalse(absent.json()["found"])
        self.assertIsNone(absent.json()["record"])

    def test_page_limits_and_unsupported_mutations_are_rejected(self):
        for query in ("?limit=0", "?limit=101", "?limit=not-an-integer"):
            self.assertEqual(self.client.get(self.base + query, headers=self.headers).status_code, 422)
        for method in ("post", "put", "delete", "patch"):
            self.assertEqual(getattr(self.client, method)(self.base + "/client-project", headers=self.headers).status_code, 405)

    def test_storage_outage_is_unavailable_never_empty_success(self):
        with patch.object(self.fixture.store, "list", side_effect=InstanceRecordUnavailable("private internal details")):
            response = self.client.get(self.base, headers=self.headers)
        self.assertEqual(response.status_code, 503)
        self.assertNotIn("private internal details", response.text)
        self.assertNotIn("records", response.json())

    def test_default_runtime_disabled_and_missing_installation_fails_closed(self):
        for env in ({}, {"WGCF_INSTANCE_RECORDS_ENABLED": "true", "WGCF_RUNTIME_PROFILE": "prod"},
                    {"WGCF_INSTANCE_RECORDS_ENABLED": "true", "WGCF_RUNTIME_PROFILE": "dev-integration"}):
            with patch.dict(os.environ, env, clear=True), self.assertRaises(InstanceRecordUnavailable):
                build_instance_record_read_runtime(self.fixture.url)
        with patch.dict(os.environ, {}, clear=True):
            client = TestClient(create_app(governance_history_authorizer=self.auth))
            self.assertEqual(client.get(self.base, headers=self.headers).status_code, 503)

    def test_listing_query_count_does_not_grow_per_record(self):
        for index in range(6):
            self.fixture.apply(self.fixture.command(name=f"project-{index}", key=f"request-{index}", provider_id=str(index + 1)))
        queries = []
        def observe(connection, cursor, statement, parameters, context, executemany):
            queries.append(statement)
        event.listen(self.fixture.engine, "before_cursor_execute", observe)
        try:
            response = self.client.get(self.base + "?limit=100", headers=self.headers)
        finally:
            event.remove(self.fixture.engine, "before_cursor_execute", observe)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.json()["records"]), 7)
        self.assertEqual(len(queries), 1)

    def test_large_history_is_byte_bounded_and_pagination_skips_nothing(self):
        result = self.result
        for index in range(2, 9):
            command = self.fixture.command(key=f"large-{index}", expected=self.fixture.expected(result))
            command["record"]["notes"] = "x" * 100_000
            result = self.fixture.apply(command)
        seen, before = [], None
        while True:
            suffix = "/client-project/history?limit=100" + (f"&before_version={before}" if before is not None else "")
            response = self.client.get(self.base + suffix, headers=self.headers)
            self.assertEqual(response.status_code, 200)
            self.assertLessEqual(len(response.content), 524_288)
            body = response.json()
            seen.extend(row["receipt"]["version"] for row in body["records"])
            before = body["next_before_version"]
            if before is None:
                break
        self.assertEqual(seen, list(range(8, 0, -1)))

    def test_catalog_is_bounded_with_global_counts_and_no_per_repository_queries(self):
        for index in range(4):
            command = self.fixture.command(name=f"project-{index}", key=f"catalog-{index}", provider_id=str(index + 1))
            command["record"]["notes"] = "x" * 100_000
            self.fixture.apply(command)
        queries = []
        def observe(connection, cursor, statement, parameters, context, executemany): queries.append(statement)
        event.listen(self.fixture.engine, "before_cursor_execute", observe)
        try:
            response = self.client.get("/v1/instance-records/repository-catalog?limit=2", headers=self.headers)
        finally:
            event.remove(self.fixture.engine, "before_cursor_execute", observe)
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(len(body["records"]), 2)
        self.assertEqual(body["counts"]["intake"]["proposed"], 5)
        self.assertEqual(body["next_after"], body["records"][-1]["receipt"]["record_id"])
        self.assertEqual(len(queries), 2)
        self.assertLessEqual(len(response.content), 524288)

    def test_catalog_search_is_literal_scoped_and_counts_cover_the_whole_selection(self):
        for index, name in enumerate(("matching-one", "matching-two", "other-project"), start=1):
            command = self.fixture.command(name=name, key=f"search-{index}", provider_id=str(index))
            command["record"]["status"] = "admitted"
            self.fixture.apply(command)
        base = "/v1/instance-records/repository-catalog"
        first = self.client.get(base + "?scope=eligible&query=%20MATCHING%20&limit=1", headers=self.headers)
        self.assertEqual(first.status_code, 200)
        body = first.json()
        self.assertEqual(body["selection"], {"scope": "eligible", "query": "matching"})
        self.assertEqual(body["counts"]["intake"]["admitted"], 2)
        self.assertEqual([row["receipt"]["record_id"] for row in body["records"]], ["repo:matching-one"])
        second = self.client.get(base, params={"scope": "eligible", "query": "matching", "limit": 1, "after": body["next_after"]}, headers=self.headers).json()
        self.assertEqual([row["receipt"]["record_id"] for row in second["records"]], ["repo:matching-two"])
        self.assertIsNone(second["next_after"])
        self.assertEqual(second["counts"]["intake"]["admitted"], 2)
        for query in ("%", "_", "missing"):
            empty = self.client.get(base, params={"scope": "eligible", "query": query}, headers=self.headers)
            self.assertEqual(empty.status_code, 200)
            self.assertEqual(empty.json()["records"], [])
            self.assertEqual(sum(empty.json()["counts"]["intake"].values()), 0)
        empty = self.client.get(base + "?scope=inventory&query=matching", headers=self.headers).json()
        self.assertEqual(empty["records"], [])
        self.assertEqual(sum(empty["counts"]["inventory"].values()), 0)
