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
                "Version: 1.0\n"
                "Architecture: iphoneos-arm64\n"
                "Filename: pool/test.deb\n"
                f"Size: {len(FixtureHandler.package_bytes)}\n"
                f"SHA256: {hashlib.sha256(FixtureHandler.package_bytes).hexdigest()}\n\n"
            ).encode()
        )
        self.state_patch = patch.object(
            server,
            "STATE",
            {"repos": [], "downloads": {}, "automation": {"enabled": False, "interval_hours": 24}},
        )
        self.state_patch.start()
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
        status, _ = self.request_json(f"/api/repos/{repo_id}", "DELETE")
        self.assertEqual(status, 200)
        self.assertNotIn((server.KEYRING_SERVICE, f"{repo_id}:device-id"), self.keyring_values)

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
        server.STATE["repos"].append(repo)
        repo["url"] = "https://packages.example.test/"

        @contextmanager
        def package_response(_url, _headers, timeout):
            self.assertEqual(timeout, 45)
            yield BytesIO(FixtureHandler.package_bytes)

        with patch.object(server, "polite_response", package_response):
            server.download_package(repo, repo["packages"][0], {})
        downloaded = server.STATE["downloads"][server.package_id("fixture-repo", repo["packages"][0])]
        self.assertEqual(
            Path(downloaded["path"]).read_bytes(),
            FixtureHandler.package_bytes,
        )

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


if __name__ == "__main__":
    unittest.main()
