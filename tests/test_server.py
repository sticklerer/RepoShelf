import bz2
from contextlib import contextmanager
import gzip
import hashlib
from io import BytesIO
import json
import lzma
import sys
import socket
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import server

resolve_public_target = server.resolve_public_https_target


class FixtureHandler(BaseHTTPRequestHandler):
    requests = []
    package_index = b""
    package_bytes = b""
    cache_requests = 0
    retry_requests = 0

    def log_message(self, *_args):
        pass

    def do_GET(self):
        type(self).requests.append((self.path, dict(self.headers)))
        if self.path == "/InRelease" or self.path == "/Release":
            self.send_error(404)
        elif self.path == "/Packages.gz":
            body = type(self).package_index
            self.send_response(200)
            self.end_headers()
            self.wfile.write(body)
        elif self.path == "/pool/test.deb":
            self.send_response(200)
            self.end_headers()
            self.wfile.write(type(self).package_bytes)
        elif self.path == "/cache":
            type(self).cache_requests += 1
            if self.headers.get("If-None-Match") == '"test-etag"':
                self.send_response(304)
                self.send_header("Cache-Control", "max-age=60")
                self.end_headers()
            else:
                self.send_response(200)
                self.send_header("ETag", '"test-etag"')
                self.send_header("Cache-Control", "max-age=0")
                self.end_headers()
                self.wfile.write(b"cached body")
        elif self.path == "/retry":
            type(self).retry_requests += 1
            if type(self).retry_requests == 1:
                self.send_response(429)
                self.send_header("Retry-After", "0")
                self.end_headers()
            else:
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b"retried")
        elif self.path == "/redirect":
            self.send_response(302)
            self.send_header("Location", "http://example.invalid/private")
            self.end_headers()
        else:
            self.send_error(404)


class RepoShelfTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fixture_httpd = ThreadingHTTPServer(("127.0.0.1", 0), FixtureHandler)
        cls.fixture_url = f"http://127.0.0.1:{cls.fixture_httpd.server_port}/"
        cls.fixture_thread = threading.Thread(
            target=cls.fixture_httpd.serve_forever, daemon=True
        )
        cls.fixture_thread.start()
        cls.api_httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        cls.api_url = f"http://127.0.0.1:{cls.api_httpd.server_port}/"
        cls.api_thread = threading.Thread(target=cls.api_httpd.serve_forever, daemon=True)
        cls.api_thread.start()
        cls.lan_httpd = ThreadingHTTPServer(("0.0.0.0", 0), server.Handler)
        cls.lan_url = f"http://127.0.0.1:{cls.lan_httpd.server_port}/"
        cls.lan_thread = threading.Thread(target=cls.lan_httpd.serve_forever, daemon=True)
        cls.lan_thread.start()

    @classmethod
    def tearDownClass(cls):
        for httpd, thread in (
            (cls.fixture_httpd, cls.fixture_thread),
            (cls.api_httpd, cls.api_thread),
            (cls.lan_httpd, cls.lan_thread),
        ):
            httpd.shutdown()
            httpd.server_close()
            thread.join(timeout=2)

    def setUp(self):
        server.JOBS.clear()
        self.keyring_values = {}
        self.keyring = SimpleNamespace(
            get_password=lambda service, account: self.keyring_values.get((service, account)),
            set_password=lambda service, account, value: self.keyring_values.__setitem__(
                (service, account), value
            ),
            delete_password=lambda service, account: self.keyring_values.pop(
                (service, account), None
            ),
        )
        self.keyring_patch = patch.dict(sys.modules, {"keyring": self.keyring})
        self.keyring_patch.start()
        def resolve_fixture_or_public(url):
            parsed = urllib.parse.urlsplit(url)
            if parsed.scheme == "http" and parsed.hostname == "127.0.0.1":
                return parsed, ("127.0.0.1",)
            return resolve_public_target(url)

        self.resolver_patch = patch.object(
            server,
            "resolve_public_https_target",
            side_effect=resolve_fixture_or_public,
        )
        self.resolver_patch.start()
        self.open_response_patch = patch.object(
            server, "open_public_response", side_effect=self.fixture_open_response
        )
        self.open_response_patch.start()
        self.tempdir = tempfile.TemporaryDirectory()
        self.cache_patch = patch.object(
            server, "HTTP_CACHE_DIR", Path(self.tempdir.name) / "http_cache"
        )
        self.cache_patch.start()
        server.HOST_LIMITERS.clear()
        self.interval_patch = patch.object(server, "HOST_REQUEST_INTERVAL", 0)
        self.interval_patch.start()
        FixtureHandler.requests = []
        FixtureHandler.cache_requests = 0
        FixtureHandler.retry_requests = 0
        FixtureHandler.package_bytes = b"fixture package"
        FixtureHandler.package_index = gzip.compress(
            (
                "Package: fixture.package\n"
                "Name: Fixture Package\n"
                "Version: 1.0\n"
                "Architecture: iphoneos-arm64\n"
                "Icon: icons/fixture.png\n"
                "Filename: pool/test.deb\n"
                f"Size: {len(FixtureHandler.package_bytes)}\n"
                f"SHA256: {hashlib.sha256(FixtureHandler.package_bytes).hexdigest()}\n\n"
            ).encode()
        )
        self.state_patch = patch.object(
            server,
            "STATE",
            {"repos": [], "downloads": {}, "automation": {"enabled": False, "interval_seconds": 86400, "per_repo_intervals_seconds": {}}},
        )
        self.state_patch.start()
        server.UNINSTALL_REQUEST = None
        self.data_patch = patch.object(server, "DATA_DIR", Path(self.tempdir.name))
        self.data_patch.start()
        self.state_file_patch = patch.object(
            server, "STATE_FILE", Path(self.tempdir.name) / "state.json"
        )
        self.state_file_patch.start()
        self.download_patch = patch.object(
            server, "DOWNLOAD_DIR", Path(self.tempdir.name) / "downloads"
        )
        self.download_patch.start()

    def tearDown(self):
        for active_patch in (
            self.download_patch,
            self.state_file_patch,
            self.data_patch,
            self.state_patch,
            self.interval_patch,
            self.open_response_patch,
            self.cache_patch,
            self.resolver_patch,
            self.keyring_patch,
        ):
            active_patch.stop()
        self.tempdir.cleanup()

    def fixture_open_response(self, url, headers, timeout):
        request = urllib.request.Request(url, headers=headers)
        try:
            response = urllib.request.urlopen(request, timeout=timeout)
        except urllib.error.HTTPError as error:
            return error, error
        return response, response

    def request_json(self, path, method="GET", payload=None, headers=None):
        data = json.dumps(payload).encode() if payload is not None else None
        request = urllib.request.Request(
            self.api_url.rstrip("/") + path,
            data=data,
            headers={
                **({"Content-Type": "application/json"} if data is not None else {}),
                **(headers or {}),
            },
            method=method,
        )
        try:
            with urllib.request.urlopen(request, timeout=3) as response:
                return response.status, json.load(response)
        except urllib.error.HTTPError as error:
            with error:
                return error.code, json.load(error)

    def test_paid_sources_require_manual_provider_id(self):
        captured_headers = []
        with patch.object(
            server, "refresh_repo",
            side_effect=lambda repo: captured_headers.append(server.repository_request_headers(repo)) or 0,
        ):
            profile = {
                "enabled": True,
                "client": "sileo",
                "client_version": "2.4.4",
                "model": "iPhone15,2",
                "os_version": "17.0",
                "architecture": "iphoneos-arm64",
            }
            status, body = self.request_json(
                "/api/repos",
                "POST",
                {"url": "https://repo.chariz.com/", "device_profile": profile},
            )
            self.assertEqual(status, 400)
            self.assertIn("manually", body["error"])

            profile.update(manual_device_id=True, device_id="a" * 39)
            status, body = self.request_json(
                "/api/repos",
                "POST",
                {"url": "https://repo.chariz.com/", "device_profile": profile},
            )
            self.assertEqual(status, 400)
            self.assertIn("40-character", body["error"])

            device_id = "a" * 40
            profile.update(manual_device_id=True, device_id=device_id)
            status, body = self.request_json(
                "/api/repos",
                "POST",
                {"url": "https://repo.chariz.com/", "device_profile": profile},
            )
        self.assertEqual(status, 201)
        self.assertEqual(captured_headers[0]["X-Unique-ID"], device_id)
        self.assertNotIn(device_id, json.dumps(body["state"]))
        repo_id = body["repo_id"]
        self.assertEqual(
            self.keyring_values[(server.KEYRING_SERVICE, f"{repo_id}:device-id")],
            device_id,
        )
        server.STATE["automation"]["per_repo_intervals_seconds"] = {repo_id: 4}
        status, _ = self.request_json(f"/api/repos/{repo_id}", "DELETE")
        self.assertEqual(status, 200)
        self.assertNotIn((server.KEYRING_SERVICE, f"{repo_id}:device-id"), self.keyring_values)
        self.assertNotIn(repo_id, server.STATE["automation"]["per_repo_intervals_seconds"])

    def test_http_repository_is_rejected_for_new_and_saved_sources(self):
        status, body = self.request_json(
            "/api/repos",
            "POST",
            {"url": self.fixture_url, "device_profile": {"enabled": False}},
        )
        self.assertEqual(status, 400)
        self.assertIn("HTTPS", body["error"])
        with self.assertRaisesRegex(ValueError, "must use HTTPS"):
            server.refresh_repo({"url": self.fixture_url})
        with self.assertRaisesRegex(ValueError, "must use HTTPS"):
            server.download_package(
                {"id": "old-http", "url": self.fixture_url},
                {"name": "fixture", "version": "1", "architecture": "all", "filename": "x.deb"},
                {},
            )

    def test_public_https_resolver_rejects_private_or_mixed_dns_results(self):
        public_answer = [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443)),
        ]
        with patch.object(server.socket, "getaddrinfo", return_value=public_answer):
            parsed, addresses = resolve_public_target("https://repo.example.test/")
        self.assertEqual(parsed.hostname, "repo.example.test")
        self.assertEqual(addresses, ("93.184.216.34",))

        private_answer = [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 443)),
        ]
        with patch.object(server.socket, "getaddrinfo", return_value=private_answer):
            with self.assertRaisesRegex(ValueError, "public IP"):
                resolve_public_target("https://repo.example.test/")

        mixed_answers = public_answer + private_answer
        with patch.object(server.socket, "getaddrinfo", return_value=mixed_answers):
            with self.assertRaisesRegex(ValueError, "public IP"):
                resolve_public_target("https://repo.example.test/")

    def test_public_https_resolver_rejects_non_https_urls(self):
        with self.assertRaisesRegex(ValueError, "valid HTTPS"):
            resolve_public_target("http://repo.example.test/")
        with self.assertRaisesRegex(ValueError, "valid HTTPS"):
            server.resolve_public_https_target("https://user:pass@repo.example.test/")

    def test_local_server_rejects_rebinding_and_cross_origin_requests(self):
        status, _ = self.request_json(
            "/api/state",
            headers={"Host": "attacker.example:8765"},
        )
        self.assertEqual(status, 403)
        status, _ = self.request_json(
            "/api/state",
            headers={"Host": "8.8.8.8:8765"},
        )
        self.assertEqual(status, 403)
        status, _ = self.request_json(
            "/api/state",
            headers={
                "Host": urllib.parse.urlsplit(self.api_url).netloc,
                "Origin": "http://attacker.example:8765",
            },
        )
        self.assertEqual(status, 403)
        status, _ = self.request_json(
            "/api/state",
            headers={
                "Host": urllib.parse.urlsplit(self.api_url).netloc,
                "Sec-Fetch-Site": "cross-site",
            },
        )
        self.assertEqual(status, 403)
        status, body = self.request_json(
            "/api/state",
            headers={
                "Host": urllib.parse.urlsplit(self.api_url).netloc,
                "Origin": self.api_url.rstrip("/"),
            },
        )
        self.assertEqual(status, 200)
        self.assertIn("repos", body)

    def test_top_level_app_navigation_allows_fetch_site_none(self):
        request = urllib.request.Request(
            self.api_url,
            headers={"Sec-Fetch-Site": "none"},
        )
        with urllib.request.urlopen(request, timeout=3) as response:
            self.assertEqual(response.status, 200)
            self.assertIn("text/html", response.headers.get("Content-Type", ""))

        status, _ = self.request_json(
            "/api/state",
            headers={"Sec-Fetch-Site": "none"},
        )
        self.assertEqual(status, 403)

    def test_trusted_lan_origin_remains_supported_when_server_is_bound_wildcard(self):
        port = self.lan_httpd.server_port
        host = f"192.168.1.10:{port}"
        request = urllib.request.Request(
            self.lan_url + "api/state",
            headers={"Host": host, "Origin": f"http://{host}"},
        )
        with urllib.request.urlopen(request, timeout=3) as response:
            self.assertEqual(response.status, 200)

    def test_generated_id_is_persisted_in_keyring_and_sent(self):
        captured_headers = []
        with patch.object(
            server, "refresh_repo",
            side_effect=lambda repo: captured_headers.append(server.repository_request_headers(repo)) or 0,
        ):
            profile = {
                "enabled": True,
                "client": "sileo",
                "client_version": "2.4.4",
                "model": "iPhone15,2",
                "os_version": "17.0",
                "architecture": "iphoneos-arm64",
            }
            status, body = self.request_json(
                "/api/repos",
                "POST",
                {"url": "https://packages.example.test/", "device_profile": profile},
            )
        self.assertEqual(status, 201)
        generated_id = self.keyring_values[
            (server.KEYRING_SERVICE, server.GENERATED_DEVICE_ID_ACCOUNT)
        ]
        self.assertRegex(generated_id, r"^[0-9a-f]{40}$")
        self.assertEqual(captured_headers[0]["X-Unique-ID"], generated_id)
        self.assertNotIn(generated_id, json.dumps(body["state"]))
        self.assertNotIn(
            (server.KEYRING_SERVICE, f"{body['repo_id']}:device-id"),
            self.keyring_values,
        )
        status, _ = self.request_json(f"/api/repos/{body['repo_id']}", "DELETE")
        self.assertEqual(status, 200)
        self.assertEqual(
            self.keyring_values[(server.KEYRING_SERVICE, server.GENERATED_DEVICE_ID_ACCOUNT)],
            generated_id,
        )

    def test_corrupt_generated_id_is_replaced(self):
        self.keyring.set_password(
            server.KEYRING_SERVICE,
            server.GENERATED_DEVICE_ID_ACCOUNT,
            "invalid",
        )
        generated_id = server.generated_device_identifier()
        self.assertRegex(generated_id, r"^[0-9a-f]{40}$")
        self.assertEqual(
            self.keyring_values[(server.KEYRING_SERVICE, server.GENERATED_DEVICE_ID_ACCOUNT)],
            generated_id,
        )

    def test_generated_device_identifier_can_be_regenerated(self):
        previous = server.generated_device_identifier()
        regenerated = server.regenerate_generated_device_identifier()
        self.assertRegex(regenerated, r"^[0-9a-f]{40}$")
        self.assertNotEqual(regenerated, previous)
        self.assertEqual(
            self.keyring_values[(server.KEYRING_SERVICE, server.GENERATED_DEVICE_ID_ACCOUNT)],
            regenerated,
        )
        self.assertNotIn(regenerated, json.dumps(server.STATE))

    def test_repository_authorization_headers_are_keyring_backed(self):
        repo_id = "test-repo"
        secret = "provider-token"
        self.keyring.set_password(
            server.KEYRING_SERVICE,
            repo_id,
            json.dumps({"username": "test-user", "secret": secret}),
        )
        headers = server.repository_request_headers(
            {"id": repo_id, "name": "fixture", "auth": {"method": "basic"}}
        )
        self.assertTrue(headers["Authorization"].startswith("Basic "))
        self.assertNotIn(secret, headers["Authorization"])

    def test_automation_refresh_continues_after_a_source_failure(self):
        refreshed = []
        repos = [
            {"id": "broken", "name": "broken source"},
            {"id": "working", "name": "working source"},
        ]

        def refresh(repo):
            if repo["id"] == "broken":
                raise ValueError("fixture unavailable")
            refreshed.append(repo["id"])

        with patch.object(server, "refresh_repo", side_effect=refresh):
            errors, failed_ids = server.refresh_repositories(repos)
        self.assertEqual(refreshed, ["working"])
        self.assertEqual(failed_ids, {"broken"})
        self.assertEqual(errors, ["broken source: fixture unavailable"])

    def test_automation_supports_default_and_per_source_intervals(self):
        server.STATE["repos"] = [{"id": "repo-a"}, {"id": "repo-b"}]
        status, result = self.request_json(
            "/api/automation",
            method="POST",
            payload={
                "enabled": True,
                "interval_seconds": 12 * 3600,
                "per_repo_intervals_seconds": {"repo-a": 3 * 60},
            },
        )
        self.assertEqual(status, 200)
        self.assertEqual(result["automation"]["interval_seconds"], 12 * 3600)
        self.assertEqual(
            result["automation"]["per_repo_intervals_seconds"], {"repo-a": 3 * 60}
        )
        self.assertEqual(server.automation_interval_for_repo(server.STATE["automation"], "repo-a"), 180)
        self.assertEqual(server.automation_interval_for_repo(server.STATE["automation"], "repo-b"), 12 * 3600)
        server.STATE["automation"]["per_repo_intervals_seconds"] = {}
        server.load_state()
        self.assertEqual(server.STATE["automation"]["per_repo_intervals_seconds"], {"repo-a": 180})
        self.assertEqual(server.automation_interval_for_repo(server.STATE["automation"], "repo-a"), 180)

        status, _ = self.request_json(
            "/api/automation",
            method="POST",
            payload={
                "enabled": True,
                "interval_seconds": 12 * 3600,
                "per_repo_intervals_seconds": {"repo-a": server.MAX_AUTOMATION_SECONDS + 1},
            },
        )
        self.assertEqual(status, 400)
        self.assertEqual(server.STATE["automation"]["per_repo_intervals_seconds"], {"repo-a": 180})

        status, _ = self.request_json(
            "/api/automation",
            method="POST",
            payload={
                "enabled": True,
                "interval_seconds": server.MAX_AUTOMATION_SECONDS,
                "per_repo_intervals_seconds": {},
            },
        )
        self.assertEqual(status, 200)
        self.assertEqual(
            server.STATE["automation"]["interval_seconds"], server.MAX_AUTOMATION_SECONDS
        )

    def test_nested_groups_filter_downloads_and_inherit_automation(self):
        package_parent = {
            "name": "parent.package",
            "version": "1.0",
            "architecture": "iphoneos-arm64",
            "filename": "pool/parent.deb",
        }
        package_child = {
            "name": "child.package",
            "version": "1.0",
            "architecture": "iphoneos-arm64",
            "filename": "pool/child.deb",
        }
        server.STATE["repos"] = [
            {
                "id": "repo-parent", "name": "Parent source",
                "url": "https://parent.example.test/", "auth": {"method": "none"},
                "device_profile": {"enabled": False}, "packages": [package_parent],
            },
            {
                "id": "repo-child", "name": "Child source",
                "url": "https://child.example.test/", "auth": {"method": "none"},
                "device_profile": {"enabled": False}, "packages": [package_child],
            },
            {
                "id": "repo-unassigned", "name": "Unassigned source",
                "url": "https://unassigned.example.test/", "auth": {"method": "none"},
                "device_profile": {"enabled": False}, "packages": [],
            },
        ]
        status, parent = self.request_json(
            "/api/groups", "POST", {"action": "create", "name": "Library"}
        )
        self.assertEqual(status, 201)
        parent_id = parent["group_id"]
        status, child = self.request_json(
            "/api/groups",
            "POST",
            {"action": "create", "name": "Themes", "parent_id": parent_id},
        )
        self.assertEqual(status, 201)
        child_id = child["group_id"]
        for repo_id, group_id in (("repo-parent", parent_id), ("repo-child", child_id)):
            status, _ = self.request_json(
                f"/api/repos/{repo_id}/group", "POST", {"group_id": group_id}
            )
            self.assertEqual(status, 200)

        status, result = self.request_json(
            "/api/automation",
            "POST",
            {
                "enabled": True,
                "interval_seconds": 3600,
                "per_repo_intervals_seconds": {},
                "per_group_intervals_seconds": {
                    parent_id: 1800,
                    child_id: 900,
                },
            },
        )
        self.assertEqual(status, 200)
        self.assertEqual(
            server.automation_interval_for_repo(
                result["automation"], "repo-parent", server.STATE["repos"][0]
            ),
            1800,
        )
        self.assertEqual(
            server.automation_interval_for_repo(
                result["automation"], "repo-child", server.STATE["repos"][1]
            ),
            900,
        )
        result["automation"]["per_repo_intervals_seconds"]["repo-child"] = 300
        self.assertEqual(
            server.automation_interval_for_repo(
                result["automation"], "repo-child", server.STATE["repos"][1]
            ),
            300,
        )

        captured = {}
        def capture_refs(refs):
            captured["refs"] = refs
            return "group-job"

        with patch.object(server, "start_download_job", side_effect=capture_refs):
            status, _ = self.request_json(
                "/api/downloads",
                "POST",
                {
                    "selection": {
                        "mode": "visible",
                        "repo_id": None,
                        "group_ids": [parent_id],
                        "filter": "all",
                        "search": "",
                    }
                },
            )
        self.assertEqual(status, 202)
        self.assertEqual(
            {repo_id for repo_id, _ in captured["refs"]},
            {"repo-parent", "repo-child"},
        )

        status, _ = self.request_json(
            "/api/groups",
            "POST",
            {
                "action": "update",
                "group_id": parent_id,
                "name": "Library",
                "parent_id": child_id,
            },
        )
        self.assertEqual(status, 400)
        status, deleted = self.request_json(f"/api/groups/{parent_id}", "DELETE", {})
        self.assertEqual(status, 200)
        self.assertEqual(
            next(group for group in deleted["state"]["groups"] if group["id"] == child_id)["parent_id"],
            None,
        )
        self.assertIsNone(next(repo for repo in deleted["state"]["repos"] if repo["id"] == "repo-parent")["group_id"])

    def test_automation_migrates_legacy_hour_intervals(self):
        server.STATE["automation"] = {
            "enabled": True,
            "interval_hours": 48,
            "per_repo_intervals": {"repo-a": 6},
        }
        server.STATE_FILE.write_text(
            json.dumps({"automation": server.STATE["automation"]}), encoding="utf-8"
        )
        server.load_state()
        self.assertEqual(server.STATE["automation"]["interval_seconds"], 48 * 3600)
        self.assertEqual(
            server.STATE["automation"]["per_repo_intervals_seconds"], {"repo-a": 6 * 3600}
        )

    def test_cache_revalidates_and_retries_rate_limit(self):
        url = urllib.parse.urljoin(self.fixture_url, "cache")
        self.assertEqual(server.fetch_bytes(url, 1024), b"cached body")
        self.assertEqual(server.fetch_bytes(url, 1024), b"cached body")
        self.assertEqual(FixtureHandler.cache_requests, 2)
        retry_url = urllib.parse.urljoin(self.fixture_url, "retry")
        self.assertEqual(server.fetch_bytes(retry_url, 1024, cache=False), b"retried")
        self.assertEqual(FixtureHandler.retry_requests, 2)

    def test_decompression_is_bounded_for_supported_formats(self):
        payload = b"x" * 101
        with patch.object(server, "MAX_DECOMPRESSED_INDEX_SIZE", 100):
            for name, compressed in (
                ("Packages.gz", gzip.compress(payload)),
                ("Packages.bz2", bz2.compress(payload)),
                ("Packages.xz", lzma.compress(payload)),
            ):
                with self.subTest(name=name), self.assertRaisesRegex(ValueError, "exceeds"):
                    server.decompress_index(compressed, name)

    def test_cross_origin_redirect_is_blocked(self):
        response = SimpleNamespace(
            status=302,
            headers={},
            getheader=lambda name: (
                "https://attacker.example/private" if name == "Location" else None
            ),
            close=lambda: None,
        )
        connection = MagicMock()
        connection.getresponse.return_value = response
        self.open_response_patch.stop()
        try:
            with (
                patch.object(
                    server,
                    "resolve_public_https_target",
                    return_value=(
                        urllib.parse.urlsplit("https://repo.example.test/redirect"),
                        ("93.184.216.34",),
                    ),
                ),
                patch.object(server, "PinnedHTTPSConnection", return_value=connection),
                self.assertRaises(urllib.error.HTTPError) as raised,
            ):
                server.open_public_response(
                    "https://repo.example.test/redirect", {}, timeout=5
                )
        finally:
            self.open_response_patch.start()
        self.assertEqual(raised.exception.code, 403)
        raised.exception.close()

    def test_https_connection_uses_only_the_resolved_ip(self):
        fake_socket = MagicMock()
        context = MagicMock()
        connection = server.PinnedHTTPSConnection(
            "repo.example.test",
            addresses=("93.184.216.34",),
            port=443,
            timeout=3,
            context=context,
        )
        with patch.object(server.socket, "create_connection", return_value=fake_socket) as connect:
            connection.connect()

        connect.assert_called_once_with(("93.184.216.34", 443), 3, None)
        context.wrap_socket.assert_called_once_with(
            fake_socket, server_hostname="repo.example.test"
        )
        self.assertIs(connection.sock, context.wrap_socket.return_value)

    def test_server_cooldown_never_attempts_early_or_sleeps_unbounded(self):
        host = urllib.parse.urlparse(self.fixture_url).netloc.lower()
        limiter = server.host_limiter(host)
        limiter["blocked_until"] = (
            time.monotonic() + server.MAX_AUTOMATIC_RETRY_WAIT + 100
        )
        with self.assertRaisesRegex(ValueError, "will not send a request early"):
            server.fetch_bytes(
                urllib.parse.urljoin(self.fixture_url, "cache"),
                1024,
                cache=False,
            )
        self.assertEqual(FixtureHandler.cache_requests, 0)

    def test_index_and_package_download_integrity(self):
        repo = {
            "id": "fixture-repo",
            "url": self.fixture_url,
            "name": "fixture",
            "auth": {"method": "none"},
            "device_profile": {"enabled": False},
            "packages": [],
        }
        with patch.object(server, "require_https_repository", return_value=urllib.parse.urlsplit(self.fixture_url)):
            self.assertEqual(server.refresh_repo(repo), 1)
        self.assertEqual(repo["packages"][0]["name"], "fixture.package")
        self.assertEqual(repo["packages"][0]["display_name"], "Fixture Package")
        self.assertEqual(repo["packages"][0]["icon"], "icons/fixture.png")
        server.STATE["repos"].append(repo)
        repo["url"] = "https://packages.example.test/"

        @contextmanager
        def package_response(_url, _headers, timeout):
            self.assertEqual(timeout, 45)
            class StreamingResponse(BytesIO):
                read1_calls = 0

                def read1(self, size=-1):
                    self.read1_calls += 1
                    return super().read1(size)

            response = StreamingResponse(FixtureHandler.package_bytes)
            yield response
            self.assertGreater(response.read1_calls, 0)

        job = {"current_bytes": 0, "current_size": 0, "downloaded_bytes": 0}
        with patch.object(server, "polite_response", package_response):
            server.download_package(repo, repo["packages"][0], job)
        self.assertEqual(job["current_bytes"], len(FixtureHandler.package_bytes))
        self.assertEqual(job["current_size"], len(FixtureHandler.package_bytes))
        self.assertEqual(job["downloaded_bytes"], len(FixtureHandler.package_bytes))
        downloaded = server.STATE["downloads"][server.package_id("fixture-repo", repo["packages"][0])]
        self.assertEqual(
            Path(downloaded["path"]).read_bytes(),
            FixtureHandler.package_bytes,
        )

    def test_download_quotes_unicode_repository_filenames(self):
        repo = {
            "id": "check0ver-fixture",
            "url": "https://packages.example.test/",
            "name": "check0ver-fixture",
            "auth": {"method": "none"},
            "device_profile": {"enabled": False},
        }
        package = {
            "name": "check0ver.package",
            "version": "1.0",
            "architecture": "iphoneos-arm64",
            "filename": "all/A Check0ver ™ /debs/1.deb",
            "size": "3",
        }
        captured = []

        @contextmanager
        def package_response(url, _headers, timeout):
            self.assertEqual(timeout, 45)
            captured.append(url)
            yield BytesIO(b"pkg")

        job = {"current_bytes": 0, "current_size": 3, "downloaded_bytes": 0}
        with patch.object(server, "polite_response", package_response):
            server.download_package(repo, package, job)
        self.assertEqual(
            urllib.parse.urlsplit(captured[0]).path,
            "/all/A%20Check0ver%20%E2%84%A2%20%EF%A3%BF/debs/1.deb",
        )

    def test_repository_icon_finds_cydia_icon(self):
        repo = {
            "id": "icon-fixture",
            "url": "https://packages.example.test/",
            "auth": {"method": "none"},
            "device_profile": {"enabled": False},
        }
        png = b"\x89PNG\r\n\x1a\nfixture"

        def fetch(url, *_args, **_kwargs):
            if url.endswith("/CydiaIcon.png"):
                raise urllib.error.HTTPError(url, 404, "Not Found", {}, None)
            self.assertTrue(url.endswith("/Icon.png"))
            return png

        with patch.object(server, "fetch_bytes", side_effect=fetch):
            self.assertEqual(server.repository_icon(repo), (png, "image/png"))

    def test_package_icon_uses_same_repository_and_package_icon_path(self):
        repo = {
            "id": "package-icon-fixture",
            "url": "https://packages.example.test/",
            "auth": {"method": "none"},
            "device_profile": {"enabled": False},
        }
        package = {
            "name": "icon-package",
            "version": "1.0",
            "architecture": "iphoneos-arm64",
            "filename": "pool/icon.deb",
            "icon": "icons/icon package.png",
        }
        png = b"\x89PNG\r\n\x1a\nfixture"
        captured = []

        def fetch(url, *_args, **_kwargs):
            captured.append(url)
            return png

        with patch.object(server, "fetch_bytes", side_effect=fetch):
            self.assertEqual(server.package_icon(repo, package), (png, "image/png"))
        self.assertEqual(
            urllib.parse.urlsplit(captured[0]).path,
            "/icons/icon%20package.png",
        )

    def test_package_icon_supports_external_https_icon_without_repository_credentials(self):
        repo = {
            "id": "external-icon-fixture",
            "url": "https://packages.example.test/",
            "auth": {"method": "bearer"},
            "device_profile": {"enabled": False},
        }
        package = {
            "name": "icon-package",
            "version": "1.0",
            "architecture": "iphoneos-arm64",
            "filename": "pool/icon.deb",
            "icon": "https://cdn.example.test/icons/package.png",
        }
        png = b"\x89PNG\r\n\x1a\nfixture"
        with patch.object(server, "fetch_bytes", return_value=png) as fetch:
            self.assertEqual(server.package_icon(repo, package), (png, "image/png"))
        self.assertEqual(fetch.call_args.args[0], package["icon"])
        self.assertIsNone(fetch.call_args.kwargs["headers"])

    def test_repo_display_name_uses_repository_release_name_or_domain(self):
        repo = {
            "name": "cydia.tmp.onl/all",
            "url": "https://cydia.tmp.onl/all/",
        }
        self.assertEqual(server.repo_display_name(repo), "cydia.tmp.onl")
        repo["display_name"] = "Check0ver"
        self.assertEqual(server.repo_display_name(repo), "Check0ver")

    def test_release_origin_updates_repository_display_name(self):
        repo = {"name": "packages.example.test", "display_name": "", "url": "https://packages.example.test/"}
        release = b"Origin: Friendly Jailbreak Repo\nLabel: packages.example.test\nSuite: stable\n"
        with patch.object(server, "fetch_bytes", return_value=release):
            server.release_index_paths(repo["url"], repo=repo)
        self.assertEqual(repo["name"], "Friendly Jailbreak Repo")
        self.assertEqual(repo["display_name"], "Friendly Jailbreak Repo")

    def test_failed_downloads_are_listed_and_can_be_retried(self):
        repo_id = "retry-fixture"
        package = {
            "name": "retry-package",
            "version": "1.0",
            "architecture": "iphoneos-arm64",
            "filename": "pool/retry.deb",
        }
        server.STATE["repos"].append({"id": repo_id, "packages": [package]})
        identifier = server.package_id(repo_id, package)
        job_id = "failed-job-fixture"
        server.JOBS[job_id] = {
            "id": job_id,
            "status": "completed",
            "total": 1,
            "item_count": 1,
            "completed": 1,
            "error_count": 1,
            "errors": ["retry-package: temporary failure"],
            "automatic": False,
            "started_at": time.time(),
            "items": [{
                "id": identifier,
                "repo_id": repo_id,
                "package_id": identifier,
                "name": package["name"],
                "status": "failed",
                "error": "temporary failure",
            }],
        }
        status, failures = self.request_json(f"/api/jobs/{job_id}/failures")
        self.assertEqual(status, 200)
        self.assertEqual(failures["total"], 1)
        self.assertEqual(failures["items"][0]["error"], "temporary failure")

        with patch.object(server, "start_download_job", return_value="retry-job-fixture") as retry:
            status, result = self.request_json(
                f"/api/jobs/{job_id}/retry",
                "POST",
                {},
            )
        self.assertEqual(status, 202)
        self.assertEqual(result["retry_count"], 1)
        self.assertEqual(result["job_id"], "retry-job-fixture")
        retry.assert_called_once_with([(repo_id, identifier)], automatic=False)

    def test_download_rejects_bad_hash_and_cleans_temporary_file(self):
        repo = {
            "id": "fixture-repo",
            "url": self.fixture_url,
            "name": "fixture",
            "auth": {"method": "none"},
            "device_profile": {"enabled": False},
            "packages": [],
        }
        package = {
            "name": "fixture.package",
            "version": "1.0",
            "architecture": "iphoneos-arm64",
            "filename": "pool/test.deb",
            "size": str(len(FixtureHandler.package_bytes)),
            "sha256": "0" * 64,
        }

        @contextmanager
        def package_response(_url, _headers, timeout):
            self.assertEqual(timeout, 45)
            yield BytesIO(FixtureHandler.package_bytes)

        repo["url"] = "https://packages.example.test/"
        with patch.object(server, "polite_response", package_response):
            with self.assertRaisesRegex(ValueError, "SHA256 check failed"):
                server.download_package(repo, package, {})
        self.assertFalse(list((Path(self.tempdir.name) / "downloads").rglob("*.part")))
        self.assertEqual(server.STATE["downloads"], {})

    def test_api_rejects_non_object_json(self):
        request = urllib.request.Request(
            self.api_url + "api/automation",
            data=b"[]",
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            urllib.request.urlopen(request, timeout=3)
        except urllib.error.HTTPError as error:
            with error:
                self.assertEqual(error.code, 400)
        else:
            self.fail("Expected a 400 response for non-object JSON")

    def test_logging_setting_persists_and_writes_to_app_log(self):
        try:
            server.set_logging_enabled(True)
            log_path = Path(self.tempdir.name) / "reposhelf.log"
            self.assertTrue(log_path.is_file())
            server.APP_LOGGER.info("logging regression marker")
            self.assertIn("logging regression marker", log_path.read_text(encoding="utf-8"))
            saved = json.loads(server.STATE_FILE.read_text(encoding="utf-8"))
            self.assertTrue(saved["settings"]["logging_enabled"])

            server.set_logging_enabled(False)
            saved = json.loads(server.STATE_FILE.read_text(encoding="utf-8"))
            self.assertFalse(saved["settings"]["logging_enabled"])
            self.assertIsNone(server.APP_LOG_FILE_HANDLER)
        finally:
            if server.APP_LOG_FILE_HANDLER is not None:
                server.configure_logging(False)

    def test_settings_and_uninstall_api_requests(self):
        status, result = self.request_json(
            "/api/settings",
            method="POST",
            payload={"logging_enabled": True},
        )
        self.assertEqual(status, 200)
        self.assertTrue(result["settings"]["logging_enabled"])
        self.assertTrue(server.STATE["settings"]["logging_enabled"])
        server.set_logging_enabled(False)

        status, result = self.request_json(
            "/api/settings",
            method="POST",
            payload={"show_automation_banner": False},
        )
        self.assertEqual(status, 200)
        self.assertFalse(result["settings"]["show_automation_banner"])
        saved = json.loads(server.STATE_FILE.read_text(encoding="utf-8"))
        self.assertFalse(saved["settings"]["show_automation_banner"])
        server.STATE["settings"]["show_automation_banner"] = True
        server.load_state()
        self.assertFalse(server.STATE["settings"]["show_automation_banner"])

        status, result = self.request_json(
            "/api/settings",
            method="POST",
            payload={"keep_download_status_until_done": True},
        )
        self.assertEqual(status, 200)
        self.assertTrue(result["settings"]["keep_download_status_until_done"])
        server.STATE["settings"]["keep_download_status_until_done"] = False
        server.load_state()
        self.assertTrue(server.STATE["settings"]["keep_download_status_until_done"])

        status, result = self.request_json(
            "/api/settings",
            method="POST",
            payload={"show_device_info_on_add": False},
        )
        self.assertEqual(status, 200)
        self.assertFalse(result["settings"]["show_device_info_on_add"])
        server.STATE["settings"]["show_device_info_on_add"] = True
        server.load_state()
        self.assertFalse(server.STATE["settings"]["show_device_info_on_add"])

        status, result = self.request_json(
            "/api/settings",
            method="POST",
            payload={"grouping_enabled": False},
        )
        self.assertEqual(status, 200)
        self.assertFalse(result["settings"]["grouping_enabled"])
        server.STATE["settings"]["grouping_enabled"] = True
        server.load_state()
        self.assertFalse(server.STATE["settings"]["grouping_enabled"])

        status, _ = self.request_json(
            "/api/uninstall",
            method="POST",
            payload={"delete_data": True},
        )
        self.assertEqual(status, 202)
        self.assertTrue(server.consume_uninstall_request())
        self.assertIsNone(server.consume_uninstall_request())

    def test_removing_repository_can_delete_only_its_downloaded_files(self):
        repo_id = "remove-download-fixture"
        package = {
            "name": "remove.fixture",
            "version": "1.0",
            "architecture": "iphoneos-arm64",
            "filename": "pool/remove.deb",
        }
        identifier = server.package_id(repo_id, package)
        server.STATE["repos"] = [{"id": repo_id, "name": "remove fixture", "packages": [package]}]
        server.DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
        saved_path = server.DOWNLOAD_DIR / "remove-fixture.deb"
        saved_path.write_bytes(b"fixture")
        server.STATE["downloads"][identifier] = {
            "version": "1.0",
            "path": str(saved_path),
            "downloaded_at": 1,
        }

        status, result = self.request_json(
            f"/api/repos/{repo_id}",
            method="DELETE",
            payload={"delete_downloads": False},
        )
        self.assertEqual(status, 200)
        self.assertTrue(saved_path.is_file())
        self.assertIn(identifier, server.STATE["downloads"])

        repo_id = "delete-download-fixture"
        package = {
            "name": "delete.fixture",
            "version": "1.0",
            "architecture": "iphoneos-arm64",
            "filename": "pool/delete.deb",
        }
        identifier = server.package_id(repo_id, package)
        server.STATE["repos"] = [{"id": repo_id, "name": "delete fixture", "packages": [package]}]
        saved_path = server.DOWNLOAD_DIR / "delete-fixture.deb"
        saved_path.write_bytes(b"fixture")
        server.STATE["downloads"][identifier] = {
            "version": "1.0",
            "path": str(saved_path),
            "downloaded_at": 1,
        }
        status, result = self.request_json(
            f"/api/repos/{repo_id}",
            method="DELETE",
            payload={"delete_downloads": True},
        )
        self.assertEqual(status, 200)
        self.assertEqual(result["deleted_downloads"], 1)
        self.assertFalse(saved_path.exists())
        self.assertNotIn(identifier, server.STATE["downloads"])

    def test_repository_removal_refuses_to_delete_downloads_outside_download_dir(self):
        repo_id = "unsafe-download-fixture"
        package = {
            "name": "unsafe.fixture",
            "version": "1.0",
            "architecture": "iphoneos-arm64",
            "filename": "pool/unsafe.deb",
        }
        identifier = server.package_id(repo_id, package)
        outside_path = Path(self.tempdir.name) / "outside.deb"
        outside_path.write_bytes(b"keep")
        server.STATE["repos"] = [{"id": repo_id, "name": "unsafe fixture", "packages": [package]}]
        server.STATE["downloads"][identifier] = {
            "version": "1.0",
            "path": str(outside_path),
            "downloaded_at": 1,
        }
        status, result = self.request_json(
            f"/api/repos/{repo_id}",
            method="DELETE",
            payload={"delete_downloads": True},
        )
        self.assertEqual(status, 400)
        self.assertTrue(outside_path.is_file())
        self.assertTrue(any(repo["id"] == repo_id for repo in server.STATE["repos"]))

    def test_get_all_resolves_large_visible_selection_on_server(self):
        repo_id = "large-fixture"
        repo = {
            "id": repo_id,
            "name": "large source",
            "packages": [
                {
                    "name": f"package-{index:05d}",
                    "version": "1.0",
                    "architecture": "iphoneos-arm64",
                    "filename": f"pool/package-{index:05d}.deb",
                }
                for index in range(12000)
            ],
        }
        server.STATE["repos"].append(repo)
        first_identifier = server.package_id(repo_id, repo["packages"][0])
        server.STATE["downloads"][first_identifier] = {"version": "1.0"}
        captured = {}

        def capture_refs(refs):
            captured["refs"] = refs
            return "fixture-job"

        with patch.object(server, "start_download_job", side_effect=capture_refs):
            status, result = self.request_json(
                "/api/downloads",
                method="POST",
                payload={
                    "selection": {
                        "mode": "visible",
                        "repo_id": repo_id,
                        "filter": "all",
                        "search": "",
                    }
                },
            )

        self.assertEqual(status, 202)
        self.assertEqual(result["job_id"], "fixture-job")
        self.assertEqual(len(captured["refs"]), 11999)
        self.assertNotIn((repo_id, first_identifier), captured["refs"])
        self.assertEqual(captured["refs"][0][0], repo_id)

    def test_large_download_job_initialization_is_linear(self):
        repo_id = "large-job-fixture"
        packages = [
            {
                "name": f"package-{index:05d}",
                "version": "1.0",
                "architecture": "iphoneos-arm64",
                "filename": f"pool/package-{index:05d}.deb",
            }
            for index in range(12000)
        ]
        server.STATE["repos"].append({"id": repo_id, "packages": packages})
        refs = [(repo_id, server.package_id(repo_id, package)) for package in packages]
        with (
            patch.object(server, "package_id", wraps=server.package_id) as package_id_spy,
            patch.object(server, "download_package"),
        ):
            job_id = server.start_download_job(refs)
            deadline = time.monotonic() + 10
            while server.JOBS[job_id]["status"] not in ("completed", "failed"):
                self.assertLess(time.monotonic(), deadline)
                time.sleep(0.01)

        job = server.JOBS[job_id]
        self.assertEqual(len(job["items"]), len(packages))
        self.assertTrue(all(item["name"].startswith("package-") for item in job["items"]))
        self.assertEqual(job["status"], "completed")
        self.assertLessEqual(package_id_spy.call_count, len(packages))

    def test_download_queue_controls_and_item_removal(self):
        repo_id = "queue-fixture"
        packages = [
            {
                "name": f"queue-package-{index}",
                "version": "1.0",
                "architecture": "iphoneos-arm64",
                "filename": f"pool/queue-{index}.deb",
            }
            for index in range(2)
        ]
        server.STATE["repos"].append({"id": repo_id, "packages": packages})
        refs = [(repo_id, server.package_id(repo_id, package)) for package in packages]
        with patch.object(server.threading.Thread, "start"):
            job_id = server.start_download_job(refs)
        job = server.JOBS[job_id]

        status, result = self.request_json(
            f"/api/jobs/{job_id}/control",
            "POST",
            {"action": "pause"},
        )
        self.assertEqual(status, 200)
        self.assertEqual(result["status"], "paused")
        self.assertFalse(job["_resume_event"].is_set())

        status, result = self.request_json(
            f"/api/jobs/{job_id}/control",
            "POST",
            {"action": "resume"},
        )
        self.assertEqual(status, 200)
        self.assertEqual(result["status"], "running")
        self.assertTrue(job["_resume_event"].is_set())

        first_item = result["items"][0]["id"]
        status, result = self.request_json(
            f"/api/jobs/{job_id}/control",
            "POST",
            {"action": "remove", "item_id": first_item},
        )
        self.assertEqual(status, 200)
        self.assertEqual(result["total"], 1)
        self.assertEqual(result["item_count"], 1)
        self.assertEqual(job["items"][0]["status"], "removed")

        status, result = self.request_json("/api/jobs")
        self.assertEqual(status, 200)
        self.assertEqual(len(result["jobs"]), 1)
        self.assertNotIn("_resume_event", result["jobs"][0])
        self.assertEqual(result["jobs"][0]["id"], job_id)
        self.assertEqual(len(result["jobs"][0]["items"]), 1)

        status, result = self.request_json("/api/jobs?limit=0")
        self.assertEqual(status, 200)
        self.assertEqual(result["jobs"][0]["items"], [])

        status, result = self.request_json("/api/jobs?limit=1")
        self.assertEqual(status, 200)
        self.assertEqual(len(result["jobs"][0]["items"]), 1)
        self.assertEqual(result["jobs"][0]["item_offset"], 0)

        status, result = self.request_json("/api/jobs?offset=1&limit=1")
        self.assertEqual(status, 200)
        self.assertEqual(result["jobs"][0]["items"], [])
        self.assertEqual(result["jobs"][0]["item_offset"], 1)

        server.control_download_job(job_id, "pause")
        status, result = self.request_json(
            f"/api/jobs/{job_id}/control",
            "POST",
            {"action": "remove", "item_id": job["items"][1]["id"]},
        )
        self.assertEqual(status, 200)
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["total"], 0)
        self.assertTrue(job["_resume_event"].is_set())

    def test_download_queue_cancel_releases_paused_worker(self):
        repo_id = "queue-cancel-fixture"
        package = {
            "name": "queue-package",
            "version": "1.0",
            "architecture": "iphoneos-arm64",
            "filename": "pool/queue.deb",
        }
        server.STATE["repos"].append({"id": repo_id, "packages": [package]})
        ref = (repo_id, server.package_id(repo_id, package))
        with patch.object(server.threading.Thread, "start"):
            job_id = server.start_download_job([ref])
        server.control_download_job(job_id, "pause")

        status, result = self.request_json(
            f"/api/jobs/{job_id}/control",
            "POST",
            {"action": "cancel"},
        )
        self.assertEqual(status, 200)
        self.assertEqual(result["status"], "cancelling")
        self.assertTrue(server.JOBS[job_id]["_cancel_event"].is_set())
        self.assertTrue(server.JOBS[job_id]["_resume_event"].is_set())

    def test_download_worker_honors_pause_and_resumes_to_completion(self):
        repo_id = "queue-worker-fixture"
        package = {
            "name": "queue-worker-package",
            "version": "1.0",
            "architecture": "iphoneos-arm64",
            "filename": "pool/queue-worker.deb",
        }
        server.STATE["repos"].append({"id": repo_id, "packages": [package]})
        ref = (repo_id, server.package_id(repo_id, package))
        download_started = threading.Event()
        begin_control_check = threading.Event()

        def controlled_download(_repo, _package, job):
            download_started.set()
            begin_control_check.wait(2)
            while not job["_resume_event"].wait(0.01):
                if job["_cancel_event"].is_set():
                    raise server.DownloadCancelled()

        with patch.object(server, "download_package", side_effect=controlled_download):
            job_id = server.start_download_job([ref])
            self.assertTrue(download_started.wait(2))
            server.control_download_job(job_id, "pause")
            begin_control_check.set()
            time.sleep(0.05)
            self.assertEqual(server.JOBS[job_id]["status"], "paused")
            server.control_download_job(job_id, "resume")
            deadline = time.monotonic() + 2
            while server.JOBS[job_id]["status"] not in ("completed", "failed"):
                self.assertLess(time.monotonic(), deadline)
                time.sleep(0.01)

        self.assertEqual(server.JOBS[job_id]["status"], "completed")
        self.assertEqual(server.JOBS[job_id]["items"][0]["status"], "completed")

    def test_regenerate_device_profile_endpoint(self):
        previous = server.generated_device_identifier()
        status, result = self.request_json(
            "/api/device-profile/regenerate",
            method="POST",
            payload={},
        )
        self.assertEqual(status, 200)
        self.assertRegex(result["generated_device_id"], r"^[0-9a-f]{40}$")
        self.assertNotEqual(result["generated_device_id"], previous)
        self.assertEqual(
            self.keyring_values[(server.KEYRING_SERVICE, server.GENERATED_DEVICE_ID_ACCOUNT)],
            result["generated_device_id"],
        )

    def test_generate_device_compatibility_profile_endpoint(self):
        status, result = self.request_json("/api/device-profile/generate")
        self.assertEqual(status, 200)
        self.assertIn(
            {key: result[key] for key in ("model", "os_version", "architecture", "client_version")},
            server.GENERATED_DEVICE_PROFILES,
        )
        self.assertRegex(result["generated_device_id"], r"^[0-9a-f]{40}$")
        self.assertEqual(
            self.keyring_values[(server.KEYRING_SERVICE, server.GENERATED_DEVICE_ID_ACCOUNT)],
            result["generated_device_id"],
        )


if __name__ == "__main__":
    unittest.main()
