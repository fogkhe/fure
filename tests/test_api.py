import asyncio
import json
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from unittest.mock import AsyncMock, patch
from uuid import UUID

import httpx2
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from openai import APIConnectionError, AsyncOpenAI

from fure.api.app import create_app
from fure.core.implementations import PythonImplementation
from fure.core.runners import OpenAIJinjaRunMeta, RunResult
from fure.core.runners.python.inline import InlinePythonRunner
from fure.database.models import VariableRow
from fure.services.resources import FunctionService
from fure.settings import Settings
from tests.auth_support import bootstrap, login
from tests.database_support import isolated_database

MISSING_ID = "01900000-0000-7000-8000-000000000099"


def tool_call(name, index, arguments="{}"):
    return {
        "id": f"call_{index}",
        "type": "function",
        "function": {"name": name, "arguments": arguments},
    }


def mock_chat(respond):
    async def handle(request):
        message = await respond(json.loads(request.content))
        return httpx2.Response(
            200,
            json={
                "id": "chatcmpl-agent",
                "object": "chat.completion",
                "created": 0,
                "model": "test",
                "choices": [
                    {
                        "index": 0,
                        "finish_reason": "tool_calls"
                        if "tool_calls" in message
                        else "stop",
                        "message": {"role": "assistant", **message},
                    }
                ],
                "usage": {
                    "prompt_tokens": 4,
                    "completion_tokens": 2,
                    "total_tokens": 6,
                },
            },
        )

    def client(**kwargs):
        return AsyncOpenAI(
            **kwargs,
            max_retries=0,
            http_client=httpx2.AsyncClient(transport=httpx2.MockTransport(handle)),
        )

    return patch("fure.core.runners.jinja.openai.AsyncOpenAI", side_effect=client)


class APITests(unittest.TestCase):
    def setUp(self):
        url = self.enterContext(isolated_database())
        self.settings = Settings(
            database_url=url,
            execution_heartbeat_interval=0.05,
            variables_encryption_key=Fernet.generate_key().decode(),
        )
        self.app = create_app(self.settings)
        self.client = self.enterContext(TestClient(self.app))
        bootstrap(self.client)

    def function(self, name="greet", **fields):
        response = self.client.post("/v1/functions", json={"name": name, **fields})
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()

    def revision(
        self,
        function_id,
        code="async def function(input: None) -> str: return 'hello'",
        *,
        timeout=5,
        activate=True,
    ):
        response = self.client.post(
            "/v1/revisions",
            json={
                "function_id": function_id,
                "implementation": {"type": "python", "code": code},
                "runner_options": {"type": "inline_python", "timeout": timeout},
            },
        )
        self.assertEqual(response.status_code, 201, response.text)
        revision = response.json()
        if activate:
            selected = self.client.patch(
                f"/v1/functions/{function_id}", json={"revision_id": revision["id"]}
            )
            self.assertEqual(selected.status_code, 200, selected.text)
        return revision

    def test_schema_docs_and_health(self):
        schema = self.client.get("/openapi.json").json()
        expected = {
            "/v1/functions",
            "/v1/functions/{function_id}",
            "/v1/functions/{function_id}/undelete",
            "/v1/functions/{function_id}/run",
            "/v1/revisions",
            "/v1/revisions/{revision_id}",
            "/v1/runs",
            "/v1/runs/{run_id}",
            "/v1/runs/{run_id}/cancel",
            "/v1/run/{function_name}",
        }
        self.assertTrue(expected.issubset(schema["paths"]))
        for path in expected:
            for operation in schema["paths"][path].values():
                self.assertNotIn("501", operation["responses"])
        self.assertCountEqual(
            schema["paths"]["/v1/functions/{function_id}/run"]["post"]["security"],
            [{"APIKey": []}, {"Session": []}],
        )
        self.assertCountEqual(
            schema["paths"]["/v1/run/{function_name}"]["post"]["security"],
            [{"APIKey": []}, {"Session": []}, {}],
        )
        self.assertFalse(schema["paths"]["/health"]["get"].get("security"))
        self.assertEqual(self.client.get("/docs").status_code, 200)
        self.assertEqual(self.client.get("/health").json(), {"status": "ok"})

    def test_uuid_execution_and_name_alias_share_current_revision(
        self,
    ):
        function = self.function()
        self.revision(function["id"])
        endpoint = f"/v1/functions/{function['id']}/run"
        for path in (endpoint, "/v1/run/greet"):
            response = self.client.post(path, json={})
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.json()["output"], "hello")
            self.assertIn("X-Run-Id", response.headers)
        self.client.patch(f"/v1/functions/{function['id']}", json={"name": "renamed"})
        self.assertEqual(self.client.post(endpoint, json={}).json()["output"], "hello")
        self.assertEqual(self.client.post("/v1/run/greet", json={}).status_code, 404)
        self.assertEqual(self.client.post("/v1/run/renamed", json={}).status_code, 200)
        self.client.patch(f"/v1/functions/{function['id']}", json={"enabled": False})
        self.assertEqual(self.client.post(endpoint, json={}).status_code, 409)
        self.client.delete(f"/v1/functions/{function['id']}")
        self.assertEqual(self.client.post(endpoint, json={}).status_code, 404)

    def _wait_terminal(self, id):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            response = self.client.get(f"/v1/runs/{id}")
            self.assertEqual(response.status_code, 200, response.text)
            record = response.json()
            if record["status"] not in {"pending", "running"}:
                return record
            time.sleep(0.01)
        self.fail("Run did not finish")

    def test_async_execution_returns_an_id_before_completion_and_uses_existing_cancel(
        self,
    ):
        function = self.function()
        self.revision(
            function["id"],
            "import asyncio\nasync def function(input: None) -> str:\n    await asyncio.sleep(60)\n    return 'late'",
            timeout=None,
        )
        endpoint = f"/v1/functions/{function['id']}/run?wait=false"
        response = self.client.post(endpoint, json={})
        self.assertEqual(response.status_code, 202, response.text)
        record = response.json()
        self.assertIn(record["status"], {"pending", "running"})
        self.assertEqual(record["id"], response.headers["X-Run-Id"])
        self.assertEqual(self._wait_running(self.client), record["id"])
        cancelled = self.client.post(f"/v1/runs/{record['id']}/cancel")
        self.assertEqual(cancelled.status_code, 200, cancelled.text)
        self.assertEqual(cancelled.json()["status"], "cancelled")
        self.assertEqual(self._wait_terminal(record["id"])["status"], "cancelled")
        self.assertEqual(
            self.client.post(f"/v1/runs/{record['id']}/cancel").status_code, 409
        )

    def test_async_completion_and_failure_are_persisted_for_both_routes(self):
        function = self.function()
        for path in (f"/v1/functions/{function['id']}/run", "/v1/run/greet"):
            for code, expected_status in (
                ("async def function(input: None) -> int: return 42", "succeeded"),
                (
                    "async def function(input: None) -> int: raise ValueError('failed')",
                    "failed",
                ),
            ):
                self.revision(function["id"], code)
                response = self.client.post(path, params={"wait": "false"}, json={})
                self.assertEqual(response.status_code, 202, response.text)
                record = self._wait_terminal(response.json()["id"])
                self.assertEqual(record["status"], expected_status)
                self.assertEqual(record["function_id"], function["id"])
                if expected_status == "succeeded":
                    self.assertEqual(record["output"], 42)
                    self.assertEqual(record["meta"], {})
                else:
                    self.assertEqual(record["meta"]["code"], "execution_error")

    def test_async_execution_requires_readable_history_and_sync_public_execution_remains_available(
        self,
    ):
        function = self.function(public=True)
        self.revision(function["id"])
        endpoint = f"/v1/functions/{function['id']}/run"
        key = self.client.post(
            "/v1/keys",
            json={
                "name": "run-only",
                "permissions": ["functions.run"],
                "function_ids": [function["id"]],
            },
        ).json()["secret"]
        headers = {"Authorization": f"Bearer {key}"}
        for path in (endpoint, "/v1/run/greet"):
            self.assertEqual(
                self.client.post(
                    path, params={"wait": "false"}, json={}, headers=headers
                ).status_code,
                403,
            )
            self.assertEqual(
                self.client.post(path, json={}, headers=headers).status_code, 200
            )
        # The rejected submissions must not have created any runs.
        self.assertEqual(self.client.get("/v1/runs").json()["total"], 2)
        self.client.cookies.clear()
        for path in (endpoint, "/v1/run/greet"):
            self.assertEqual(
                self.client.post(path, params={"wait": "false"}, json={}).status_code,
                401,
            )
        self.assertEqual(self.client.post(endpoint, json={}).status_code, 401)
        self.assertEqual(self.client.post("/v1/run/greet", json={}).status_code, 200)

    def test_function_name_search_is_case_insensitive_and_treats_underscores_literally(
        self,
    ):
        self.function("pre-GREET_alpha")
        self.function("pre_greet_beta", enabled=False)
        self.function("pre-greetXalpha")

        names = []
        for page in (0, 1):
            response = self.client.get(
                "/v1/functions", params={"name": "Greet_", "size": 1, "page": page}
            )
            self.assertEqual(response.status_code, 200, response.text)
            result = response.json()
            self.assertEqual(result["total"], 2)
            names.extend(item["name"] for item in result["items"])
        self.assertCountEqual(names, ["pre-GREET_alpha", "pre_greet_beta"])

        enabled = self.client.get(
            "/v1/functions", params={"name": "Greet_", "enabled": True}
        ).json()
        self.assertEqual(enabled["total"], 1)
        self.assertEqual(enabled["items"][0]["name"], "pre-GREET_alpha")

    def test_shared_variables_are_live_and_secret_values_are_hidden_from_the_catalogue(
        self,
    ):
        secret = "test-catalogue-secret"
        variables = {
            "apiKey": (True, secret),
            "API_KEY": (False, {"enabled": True}),
            "empty": (False, None),
        }
        for name, (is_secret, value) in variables.items():
            response = self.client.post(
                "/v1/variables",
                json={"name": name, "secret": is_secret, "value": value},
            )
            self.assertEqual(response.status_code, 201, response.text)
            if is_secret:
                self.assertNotIn("value", response.json())
            else:
                self.assertEqual(response.json()["value"], value)
        listing = self.client.get("/v1/variables")
        self.assertEqual(listing.status_code, 200, listing.text)
        self.assertEqual(listing.json()["total"], 3)
        self.assertNotIn(secret, listing.text)

        async def read_payload():
            async with self.app.state.database.sessions() as session:
                row = await session.get(VariableRow, "apiKey")
                return row.payload

        payload = self.client.portal.call(read_payload)
        self.assertNotIn(secret, payload)
        cipher = Fernet(self.settings.variables_encryption_key.get_secret_value())
        self.assertEqual(cipher.decrypt(payload.encode()).decode(), f'"{secret}"')

        key = self.client.post(
            "/v1/keys",
            json={"name": "variable-reader", "permissions": ["variables.read"]},
        )
        self.assertEqual(key.status_code, 201, key.text)
        headers = {"Authorization": f"Bearer {key.json()['secret']}"}
        self.assertEqual(
            self.client.get("/v1/variables", headers=headers).status_code, 200
        )
        self.assertEqual(
            self.client.delete("/v1/variables/apiKey", headers=headers).status_code, 403
        )

        function = self.function()
        revision = self.revision(
            function["id"],
            "from fure.core.types import JSONValue\n"
            "async def function(input: None, vars: dict[str, JSONValue]) -> dict:\n"
            "    return {'length': len(vars['apiKey']), 'config': vars['API_KEY']}\n",
        )
        self.assertEqual(revision["implementation"]["input_schema"], {"type": "null"})
        first = self.client.post("/v1/run/greet", json={})
        self.assertEqual(first.status_code, 200, first.text)
        self.assertEqual(
            first.json()["output"], {"length": len(secret), "config": {"enabled": True}}
        )
        run = self.client.get(f"/v1/runs/{first.headers['X-Run-Id']}")
        self.assertIsNone(run.json()["input"])
        self.assertNotIn(secret, run.text)

        changed = self.client.patch("/v1/variables/apiKey", json={"value": "rotated"})
        self.assertEqual(changed.status_code, 200, changed.text)
        self.assertNotIn("value", changed.json())
        self.assertEqual(
            self.client.post("/v1/run/greet", json={}).json()["output"]["length"], 7
        )
        self.assertEqual(self.client.delete("/v1/variables/apiKey").status_code, 204)
        self.assertEqual(self.client.get("/v1/variables").json()["total"], 2)

    def test_function_crud_filters_pagination_and_name_reuse(self):
        first = self.function(description="old")
        self.assertFalse(first["runnable"])
        self.function("other", enabled=False)
        self.assertEqual(
            self.client.post("/v1/functions", json={"name": "greet"}).status_code, 409
        )
        page = self.client.get("/v1/functions?size=1&page=0").json()
        self.assertEqual(page["total"], 2)
        self.assertEqual(len(page["items"]), 1)
        self.assertEqual(
            self.client.get("/v1/functions?enabled=false").json()["total"], 1
        )
        self.assertEqual(self.client.get("/v1/functions?name=greet").json()["total"], 1)
        updated = self.client.patch(
            f"/v1/functions/{first['id']}",
            json={"name": "renamed", "description": None, "enabled": False},
        )
        self.assertEqual(updated.status_code, 200)
        self.assertIsNone(updated.json()["description"])
        self.assertFalse(updated.json()["enabled"])
        self.assertEqual(
            self.client.delete(f"/v1/functions/{first['id']}").status_code, 204
        )
        self.assertEqual(self.client.get("/v1/functions").json()["total"], 1)
        self.assertEqual(
            self.client.get("/v1/functions?deleted=true").json()["total"], 1
        )
        replacement = self.function("renamed")
        self.assertEqual(
            self.client.post(f"/v1/functions/{first['id']}/undelete").status_code, 409
        )
        self.client.delete(f"/v1/functions/{replacement['id']}")
        restored = self.client.post(f"/v1/functions/{first['id']}/undelete")
        self.assertEqual(restored.status_code, 200)
        self.assertFalse(restored.json()["deleted"])
        self.assertFalse(restored.json()["enabled"])

    def test_revision_selection_history_and_ownership(self):
        function = self.function()
        first = self.revision(function["id"], activate=False)
        self.assertIsNone(
            self.client.get(f"/v1/functions/{function['id']}").json()["revision_id"]
        )
        self.client.patch(
            f"/v1/functions/{function['id']}", json={"revision_id": first["id"]}
        )
        run = self.client.post("/v1/run/greet", json={})
        self.assertEqual(run.status_code, 200)
        run_id = run.headers["X-Run-Id"]
        second = self.revision(
            function["id"], "async def function(input: None) -> int: return 42"
        )
        self.assertEqual(
            self.client.post("/v1/run/greet", json={}).json()["output"], 42
        )
        self.assertEqual(
            self.client.get(f"/v1/runs/{run_id}").json()["revision_id"], first["id"]
        )
        versions = self.client.get(f"/v1/revisions?function_id={function['id']}").json()
        self.assertEqual(
            [item["id"] for item in versions["items"]], [second["id"], first["id"]]
        )
        other = self.function("other")
        self.assertEqual(
            self.client.patch(
                f"/v1/functions/{other['id']}", json={"revision_id": first["id"]}
            ).status_code,
            409,
        )
        self.assertEqual(
            self.client.get(f"/v1/revisions/{first['id']}").status_code, 200
        )
        self.assertEqual(
            self.client.patch(f"/v1/revisions/{first['id']}", json={}).status_code, 405
        )

    def test_python_execution_validation_failures_and_timeouts_are_recorded(self):
        function = self.function()
        code = "from pydantic import BaseModel\nclass Input(BaseModel):\n    value: int\nasync def function(input: Input) -> int: return input.value + 1"
        revision = self.revision(function["id"], code)
        response = self.client.post("/v1/run/greet", json={"input": {"value": 4}})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["output"], 5)
        self.assertEqual(response.json()["meta"], {})
        self.assertGreaterEqual(response.json()["duration"], 0)
        detail = self.client.get("/v1/runs/" + response.headers["X-Run-Id"]).json()
        self.assertEqual(detail["status"], "succeeded")
        self.assertEqual(detail["revision_id"], revision["id"])
        self.assertIsNotNone(detail["finished_at"])
        invalid = self.client.post("/v1/run/greet", json={"input": {}})
        self.assertEqual(invalid.status_code, 422)
        failure = self.client.get("/v1/runs/" + invalid.headers["X-Run-Id"]).json()
        self.assertEqual(failure["status"], "failed")
        self.assertEqual(failure["meta"]["code"], "invalid_input")
        self.revision(
            function["id"], "async def function(input: None) -> int: return 'bad'"
        )
        failed = self.client.post("/v1/run/greet", json={})
        self.assertEqual(failed.status_code, 500)
        self.assertEqual(
            self.client.get("/v1/runs/" + failed.headers["X-Run-Id"]).json()["meta"][
                "code"
            ],
            "execution_error",
        )
        self.revision(
            function["id"],
            "import asyncio\nasync def function(input: None) -> str:\n    await asyncio.sleep(10)\n    return 'late'",
            timeout=0.02,
        )
        timed_out = self.client.post("/v1/run/greet", json={})
        self.assertEqual(timed_out.status_code, 504)
        timed_record = self.client.get(
            "/v1/runs/" + timed_out.headers["X-Run-Id"]
        ).json()
        self.assertEqual(timed_record["meta"]["code"], "timeout")
        self.assertIsNotNone(timed_record["finished_at"])
        history = self.client.get(
            f"/v1/runs?function_id={function['id']}&status=failed"
        ).json()
        self.assertEqual(history["total"], 3)
        self.assertEqual(
            self.client.get(f"/v1/runs?revision_id={revision['id']}").json()["total"], 2
        )

    def test_missing_disabled_deleted_and_unversioned_functions(self):
        self.assertEqual(self.client.post("/v1/run/missing", json={}).status_code, 404)
        function = self.function()
        self.assertEqual(self.client.post("/v1/run/greet", json={}).status_code, 409)
        self.revision(function["id"])
        self.client.patch(f"/v1/functions/{function['id']}", json={"enabled": False})
        self.assertEqual(self.client.post("/v1/run/greet", json={}).status_code, 409)
        self.client.delete(f"/v1/functions/{function['id']}")
        self.assertEqual(self.client.post("/v1/run/greet", json={}).status_code, 404)
        self.assertEqual(
            self.client.patch(
                f"/v1/functions/{function['id']}", json={"enabled": True}
            ).status_code,
            409,
        )
        for path in (
            f"/v1/functions/{MISSING_ID}",
            f"/v1/revisions/{MISSING_ID}",
            f"/v1/runs/{MISSING_ID}",
        ):
            self.assertEqual(self.client.get(path).status_code, 404)
        self.assertEqual(
            self.client.post(f"/v1/runs/{MISSING_ID}/cancel").status_code, 404
        )

    def test_concurrent_function_creation_has_one_winner(self):
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [
                pool.submit(self.client.post, "/v1/functions", json={"name": "same"})
                for _ in range(2)
            ]
            codes = sorted(future.result(timeout=5).status_code for future in futures)
        self.assertEqual(codes, [201, 409])
        self.assertEqual(self.client.get("/v1/functions?name=same").json()["total"], 1)

    def test_nonfinite_outputs_are_failed_before_success_is_saved(self):
        function = self.function()
        self.revision(
            function["id"],
            "async def function(input: None) -> float: return float('nan')",
        )
        response = self.client.post("/v1/run/greet", json={})
        self.assertEqual(response.status_code, 500)
        record = self.client.get("/v1/runs/" + response.headers["X-Run-Id"]).json()
        self.assertEqual(record["status"], "failed")
        self.assertIsNone(record["output"])

    def test_invalid_definitions_and_payloads_do_not_create_revisions(self):
        function = self.function()
        for implementation, options in (
            ({"type": "python", "code": "bad syntax!"}, {"type": "inline_python"}),
            (
                {"type": "jinja", "template": "{{ broken"},
                {
                    "type": "openai_jinja",
                    "base_url": "http://localhost:11434/v1",
                    "model": "test",
                },
            ),
            ({"type": "jinja", "template": "hello"}, {"type": "inline_python"}),
        ):
            response = self.client.post(
                "/v1/revisions",
                json={
                    "function_id": function["id"],
                    "implementation": implementation,
                    "runner_options": options,
                },
            )
            self.assertEqual(response.status_code, 422, response.text)
        self.assertEqual(self.client.get("/v1/revisions").json()["total"], 0)
        for payload in ({}, {"enabled": None}, {"revision_id": None}):
            self.assertEqual(
                self.client.patch(
                    f"/v1/functions/{function['id']}", json=payload
                ).status_code,
                422,
            )
        self.assertEqual(
            self.client.post("/v1/functions", json={"name": "bad name"}).status_code,
            422,
        )
        self.assertEqual(
            self.client.post(
                "/v1/functions", json={"name": "x", "input_schema": {}}
            ).status_code,
            422,
        )
        self.assertEqual(self.client.get("/v1/runs?size=1001").status_code, 422)
        self.assertEqual(self.client.get("/v1/runs?status=unknown").status_code, 422)

    def test_openai_metadata_credentials_and_provider_failures(self):
        function = self.function()
        revision = self.client.post(
            "/v1/revisions",
            json={
                "function_id": function["id"],
                "implementation": {"type": "jinja", "template": "Hello, {{ name }}!"},
                "runner_options": {
                    "type": "openai_jinja",
                    "base_url": "http://localhost:11434/v1",
                    "model": "test",
                    "api_key": "private-key",
                    "timeout": 5,
                },
            },
        )
        self.assertEqual(revision.status_code, 201, revision.text)
        self.assertNotIn("api_key", revision.json()["runner_options"])
        self.assertNotIn("private-key", revision.text)
        self.client.patch(
            f"/v1/functions/{function['id']}",
            json={"revision_id": revision.json()["id"]},
        )
        result = RunResult(
            output="Hello!", meta=OpenAIJinjaRunMeta(10, 261, 271), duration=0.223
        )
        with patch(
            "fure.services.execution.OpenAIJinjaRunner.run",
            new=AsyncMock(return_value=result),
        ) as execute:
            response = self.client.post(
                "/v1/run/greet", json={"input": {"name": "world"}}
            )
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(
                response.json()["meta"],
                {"input_tokens": 10, "output_tokens": 261, "total_tokens": 271},
            )
            self.assertEqual(execute.await_count, 1)
        detail = self.client.get("/v1/runs/" + response.headers["X-Run-Id"]).json()
        self.assertEqual(detail["meta"], response.json()["meta"])
        with patch(
            "fure.services.execution.OpenAIJinjaRunner.run",
            new=AsyncMock(
                side_effect=APIConnectionError(
                    request=httpx2.Request("POST", "http://provider.test")
                )
            ),
        ):
            failure = self.client.post(
                "/v1/run/greet", json={"input": {"name": "world"}}
            )
            self.assertEqual(failure.status_code, 502)
            self.assertNotIn("private-key", failure.text)
        self.assertEqual(self.client.get("/v1/runs?status=failed").json()["total"], 1)

    def agent_revision(self, function, tools, *, output_schema=None, **options):
        response = self.client.post(
            "/v1/revisions",
            json={
                "function_id": function["id"],
                "implementation": {
                    "type": "jinja",
                    "template": function["name"],
                    "output_schema": output_schema or {},
                },
                "runner_options": {
                    "type": "openai_jinja",
                    "model": "test",
                    "base_url": "http://provider.test/v1",
                    "timeout": 10,
                    "tool_function_ids": [tool["id"] for tool in tools],
                    **options,
                },
            },
        )
        self.assertEqual(response.status_code, 201, response.text)
        revision = response.json()
        activated = self.client.patch(
            f"/v1/functions/{function['id']}", json={"revision_id": revision["id"]}
        )
        self.assertEqual(activated.status_code, 200, activated.text)
        return revision

    def tool_runs(self, run_id):
        response = self.client.get("/v1/runs", params={"parent_run_id": run_id})
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()["items"]

    def test_agent_calls_tools_in_parallel_using_the_revision_at_start(self):
        agent = self.function("agent")
        tool = self.function("lookup", description="Look up data")
        original = self.revision(
            tool["id"], "async def function(input: None) -> str: return 'original'"
        )
        replacement = self.revision(tool["id"], activate=False)
        schema = {
            "type": "object",
            "properties": {"done": {"type": "boolean"}},
            "required": ["done"],
            "additionalProperties": False,
        }
        revision = self.agent_revision(
            agent, [tool], output_schema=schema, max_steps=None, max_tool_calls=None
        )
        self.assertIsNone(revision["runner_options"]["max_steps"])
        self.assertIsNone(revision["runner_options"]["max_tool_calls"])
        both_called = asyncio.Event()
        entered = 0
        run_python = InlinePythonRunner.run

        async def parallel(runner, *args, **kwargs):
            nonlocal entered
            entered += 1
            if entered == 2:
                both_called.set()
            await asyncio.wait_for(both_called.wait(), timeout=5)
            return await run_python(runner, *args, **kwargs)

        async def respond(payload):
            self.assertEqual(
                payload["response_format"]["json_schema"]["schema"], schema
            )
            messages = payload["messages"]
            if messages[-1]["role"] == "user":
                (system,) = (
                    message for message in messages if message["role"] == "system"
                )
                self.assertIn("Only group independent calls.", system["content"])
                self.assertIn("Wait for its tool result", system["content"])
                self.assertIn(
                    "The final output must conform to the schema.", system["content"]
                )
                definition = payload["tools"][0]["function"]
                self.assertEqual(definition["description"], "Look up data")
                self.assertEqual(definition["parameters"]["type"], "object")
                async with self.app.state.database.sessions() as session:
                    await FunctionService(session).update(
                        UUID(tool["id"]), {"revision_id": UUID(replacement["id"])}
                    )
                return {
                    "content": "Checking",
                    "tool_calls": [tool_call("lookup", 1), tool_call("lookup", 2)],
                }
            self.assertEqual(messages[-3]["content"], "Checking")
            self.assertEqual(
                [message["tool_call_id"] for message in messages[-2:]],
                ["call_1", "call_2"],
            )
            self.assertEqual(
                [json.loads(message["content"]) for message in messages[-2:]],
                ["original", "original"],
            )
            return {"content": '{"done":true}'}

        with mock_chat(respond), patch.object(InlinePythonRunner, "run", parallel):
            response = self.client.post("/v1/run/agent", json={})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["output"], {"done": True})
        meta = response.json()["meta"]
        calls = meta.pop("tool_calls")
        (comment,) = meta.pop("assistant_comments")
        self.assertEqual((comment["text"], comment["step"]), ("Checking", 1))
        self.assertEqual(
            meta,
            {"input_tokens": 8, "output_tokens": 4, "total_tokens": 12},
        )
        children = self.tool_runs(response.headers["X-Run-Id"])
        self.assertEqual([child["tool_call_index"] for child in children], [1, 2])
        self.assertEqual(
            [child["revision_id"] for child in children],
            [original["id"], original["id"]],
        )
        self.assertTrue(all(child["input"] is None for child in children))
        self.assertLessEqual(
            datetime.fromisoformat(comment["received_at"]),
            min(datetime.fromisoformat(child["started_at"]) for child in children),
        )
        self.assertEqual(
            calls,
            [
                {
                    "index": index,
                    "step": 1,
                    "function_id": tool["id"],
                    "name": "lookup",
                    "arguments": {},
                    "run_id": child["id"],
                }
                for index, child in enumerate(children, 1)
            ],
        )

    def test_agent_checks_authentication_scope_and_tool_arguments(self):
        agent = self.function("agent", public=True)
        allowed = self.function("allowed")
        forbidden = self.function("forbidden", public=True)
        self.revision(allowed["id"])
        self.revision(forbidden["id"])
        self.agent_revision(agent, [allowed, forbidden], structured_outputs=False)
        self.assertEqual(
            self.client.post(
                "/v1/run/agent", json={}, headers={"cookie": ""}
            ).status_code,
            401,
        )
        key = self.client.post(
            "/v1/keys",
            json={
                "name": "agent key",
                "permissions": ["functions.run", "runs.read"],
                "function_ids": [agent["id"], allowed["id"]],
            },
        ).json()

        async def respond(payload):
            self.assertEqual(
                [tool["function"]["name"] for tool in payload["tools"]], ["allowed"]
            )
            messages = payload["messages"]
            if messages[-1]["role"] == "user":
                self.assertEqual(messages[0]["role"], "system")
                self.assertIn("run in parallel", messages[0]["content"])
                return {
                    "tool_calls": [
                        tool_call("forbidden", 1),
                        tool_call("allowed", 2, '{"extra":true}'),
                        tool_call("allowed", 3),
                    ]
                }
            self.assertEqual(
                [json.loads(message["content"]) for message in messages[-3:]],
                [
                    {"error": {"code": "tool_unavailable"}},
                    {"error": {"code": "invalid_input"}},
                    "hello",
                ],
            )
            return {"content": "Done"}

        with mock_chat(respond):
            response = self.client.post(
                "/v1/run/agent",
                json={},
                headers={"Authorization": f"Bearer {key['secret']}"},
            )
        self.assertEqual(response.status_code, 200, response.text)
        (child,) = self.tool_runs(response.headers["X-Run-Id"])
        self.assertEqual(child["function_id"], allowed["id"])
        self.assertEqual(child["api_key_id"], key["id"])
        self.assertEqual(child["tool_call_index"], 3)
        run_id = response.headers["X-Run-Id"]
        self.assertEqual(
            [call["index"] for call in response.json()["meta"]["tool_calls"]], [3]
        )
        changed = self.client.patch(
            f"/v1/keys/{key['id']}", json={"function_ids": [agent["id"]]}
        )
        self.assertEqual(changed.status_code, 200, changed.text)
        restricted = self.client.get(
            f"/v1/runs/{run_id}", headers={"Authorization": f"Bearer {key['secret']}"}
        )
        self.assertEqual(restricted.status_code, 200, restricted.text)
        self.assertNotIn("tool_calls", restricted.json()["meta"])

    def test_agent_limits_reject_a_batch_before_running_tools(self):
        agent = self.function("agent")
        tool = self.function("tool")
        self.revision(tool["id"])

        async def respond(_payload):
            return {"tool_calls": [tool_call("tool", 1), tool_call("tool", 2)]}

        for limits in ({"max_steps": 1}, {"max_tool_calls": 1}):
            with self.subTest(limits=limits), mock_chat(respond):
                self.agent_revision(agent, [tool], **limits)
                response = self.client.post("/v1/run/agent", json={})
                self.assertEqual(response.status_code, 500, response.text)
                run_id = response.headers["X-Run-Id"]
                self.assertEqual(
                    self.client.get(f"/v1/runs/{run_id}").json()["meta"]["code"],
                    "agent_limit_exceeded",
                )
                self.assertEqual(self.tool_runs(run_id), [])

    def test_agent_cancellation_cascades_through_nested_agents(self):
        outer = self.function("outer")
        inner = self.function("inner")
        tool = self.function("slow")
        self.revision(
            tool["id"],
            "import asyncio\nasync def function(input: None) -> str:\n    await asyncio.sleep(60)\n    return 'late'",
            timeout=None,
        )
        self.agent_revision(outer, [inner], timeout=None)
        self.agent_revision(inner, [outer, tool], timeout=None)

        async def respond(payload):
            parent = payload["messages"][-1]["content"]
            target = {"outer": "inner", "inner": "slow"}[parent]
            self.assertEqual(
                [tool["function"]["name"] for tool in payload["tools"]], [target]
            )
            return {
                "content": f"Calling {target}",
                "tool_calls": [tool_call(target, 1)],
            }

        with mock_chat(respond):
            submitted = self.client.post("/v1/run/outer?wait=false", json={})
            self.assertEqual(submitted.status_code, 202, submitted.text)
            parent_id = submitted.json()["id"]
            deadline = time.monotonic() + 8
            while time.monotonic() < deadline:
                children = self.tool_runs(parent_id)
                if children:
                    child_id = children[0]["id"]
                    grandchildren = self.tool_runs(child_id)
                    if grandchildren and grandchildren[0]["status"] == "running":
                        break
                time.sleep(0.02)
            else:
                self.fail("Nested tool was never running")
            running = self.client.get(f"/v1/runs/{parent_id}").json()
            self.assertEqual(running["status"], "running")
            self.assertEqual(
                running["meta"]["assistant_comments"][0]["text"], "Calling inner"
            )
            self.assertEqual(running["meta"]["tool_calls"][0]["run_id"], child_id)
            cancelled = self.client.post(f"/v1/runs/{parent_id}/cancel")
            self.assertEqual(cancelled.status_code, 200, cancelled.text)
            self.assertEqual(cancelled.json()["meta"], running["meta"])
            child = self.tool_runs(parent_id)[0]
            self.assertEqual(child["status"], "cancelled")
            self.assertEqual(
                child["meta"]["assistant_comments"][0]["text"], "Calling slow"
            )
            self.assertEqual(child["meta"]["tool_calls"][0]["name"], "slow")
            self.assertEqual(self.tool_runs(child_id)[0]["status"], "cancelled")

    def test_agent_failure_keeps_committed_tool_calls(self):
        agent = self.function("agent")
        tool = self.function("lookup")
        self.revision(tool["id"])
        self.agent_revision(
            agent,
            [tool],
            max_tool_calls=2,
            structured_outputs=False,
            reasoning_end_tags=["</think>"],
        )

        async def respond(payload):
            step = 1 + sum(message["role"] == "tool" for message in payload["messages"])
            return {
                "content": "<think>Internal trace</think>Looking up"
                if step == 1
                else "Trying again",
                "tool_calls": [tool_call("lookup", step)],
            }

        with mock_chat(respond):
            response = self.client.post("/v1/run/agent", json={})
        self.assertEqual(response.status_code, 500, response.text)
        run_id = response.headers["X-Run-Id"]
        parent = self.client.get(f"/v1/runs/{run_id}").json()
        self.assertEqual(parent["meta"]["code"], "agent_limit_exceeded")
        comments = parent["meta"]["assistant_comments"]
        self.assertEqual(
            [(item["step"], item["text"]) for item in comments],
            [(1, "Looking up"), (2, "Trying again")],
        )
        children = self.tool_runs(run_id)
        self.assertEqual(
            [(call["step"], call["run_id"]) for call in parent["meta"]["tool_calls"]],
            [(step, child["id"]) for step, child in enumerate(children, start=1)],
        )

    def test_saved_state_survives_restart_and_reads_do_not_execute_code(self):
        function = self.function()
        revision = self.revision(function["id"])
        response = self.client.post("/v1/run/greet", json={})
        run_id = response.headers["X-Run-Id"]
        with (
            TestClient(create_app(self.settings)) as other,
            patch.object(
                PythonImplementation,
                "_inspect",
                side_effect=AssertionError("read executed code"),
            ),
        ):
            login(other)
            for path in (
                "/v1/functions",
                f"/v1/functions/{function['id']}",
                "/v1/revisions",
                f"/v1/revisions/{revision['id']}",
                "/v1/runs",
                f"/v1/runs/{run_id}",
            ):
                self.assertEqual(other.get(path).status_code, 200)

    def _wait_running(self, client):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            items = client.get("/v1/runs?status=running").json()["items"]
            if items:
                return items[0]["id"]
            time.sleep(0.01)
        self.fail("Run did not start")

    def test_cancellation_between_workers_and_terminal_state_protection(self):
        function = self.function()
        self.revision(
            function["id"],
            "import asyncio\nasync def function(input: None) -> str:\n    await asyncio.sleep(10)\n    return 'late'",
            timeout=3,
        )
        with (
            TestClient(create_app(self.settings)) as other,
            ThreadPoolExecutor(max_workers=1) as pool,
        ):
            login(other)
            future = pool.submit(self.client.post, "/v1/run/greet", json={})
            id = self._wait_running(other)
            cancelled = other.post(f"/v1/runs/{id}/cancel")
            self.assertEqual(cancelled.status_code, 200, cancelled.text)
            self.assertEqual(cancelled.json()["status"], "cancelled")
            self.assertEqual(future.result(timeout=5).status_code, 409)
            self.assertEqual(other.post(f"/v1/runs/{id}/cancel").status_code, 409)
            self.assertEqual(other.get(f"/v1/runs/{id}").json()["status"], "cancelled")


if __name__ == "__main__":
    unittest.main()
