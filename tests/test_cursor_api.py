from __future__ import annotations

import base64
import json
import unittest

import httpx

from cursor_tensorlake.cursor_api import CursorAPI, CursorAPIError, pending_entries


class CursorAPITests(unittest.TestCase):
    def _api(self, handler):
        return CursorAPI("sa_key", "https://api.cursor.com", transport=httpx.MockTransport(handler))

    def test_basic_auth_and_pool_registration_body(self) -> None:
        seen = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["auth"] = request.headers["Authorization"]
            seen["path"] = request.url.path
            seen["body"] = json.loads(request.content)
            return httpx.Response(200, json={"ok": True})

        api = self._api(handler)
        api.register_pool("tensorlake", worker_ready_timeout_seconds=900)
        expected = "Basic " + base64.b64encode(b"sa_key:").decode()
        self.assertEqual(seen["auth"], expected)
        self.assertEqual(seen["path"], "/v0/private-workers/pools")
        self.assertEqual(seen["body"], {"scope": "team", "poolName": "tensorlake", "workerReadyTimeoutSeconds": 900})
        api.register_pool("tensorlake", worker_ready_timeout_seconds=900, repo_url="https://github.com/acme/widgets")
        self.assertEqual(seen["body"]["repoUrl"], "https://github.com/acme/widgets")

    def test_deregister_pool_uses_snake_case_query(self) -> None:
        seen = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["method"] = request.method
            seen["params"] = dict(request.url.params)
            return httpx.Response(200, json={"deregistered": True})

        api = self._api(handler)
        api.deregister_pool("tensorlake")
        self.assertEqual(seen["method"], "DELETE")
        self.assertEqual(seen["params"], {"scope": "team", "pool_name": "tensorlake"})
        api.deregister_pool("tensorlake", repo_url="https://github.com/acme/widgets.git")
        self.assertEqual(seen["params"]["repo_owner"], "acme")
        self.assertEqual(seen["params"]["repo_name"], "widgets")

    def test_release_claim_encodes_id(self) -> None:
        seen = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["path"] = request.url.raw_path.decode()
            return httpx.Response(204)

        self._api(handler).release_claim("bc/odd id")
        self.assertEqual(seen["path"], "/v0/private-workers/claims/bc%2Fodd%20id/release")

    def test_create_agent_targets_pool(self) -> None:
        seen = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["body"] = json.loads(request.content)
            return httpx.Response(200, json={"id": "bc-1"})

        self._api(handler).create_agent("hello", "tensorlake")
        self.assertEqual(seen["body"]["env"], {"type": "pool", "name": "tensorlake"})

    def test_http_error_raises(self) -> None:
        api = self._api(lambda r: httpx.Response(400, text="unknown pool"))
        with self.assertRaises(CursorAPIError) as ctx:
            api.list_pools()
        self.assertEqual(ctx.exception.status_code, 400)

    def test_pending_entries_tolerates_keys(self) -> None:
        self.assertEqual(pending_entries({"requests": [{"id": 1}]}), [{"id": 1}])
        self.assertEqual(pending_entries({"items": [{"id": 2}, "junk"]}), [{"id": 2}])
        self.assertEqual(pending_entries({}), [])
