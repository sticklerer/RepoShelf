#!/usr/bin/env python3
"""Local web app for browsing and downloading packages from jailbreak repos."""

from __future__ import annotations

import base64
import bz2
import email.utils
from contextlib import contextmanager
from datetime import datetime, timezone
import gzip
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from io import BytesIO
import ipaddress
import json
import lzma
import math
import os
from pathlib import Path
import re
import threading
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
import uuid


ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "data"
DOWNLOAD_DIR = DATA_DIR / "downloads"
HTTP_CACHE_DIR = DATA_DIR / "http_cache"
STATE_FILE = DATA_DIR / "state.json"
MAX_INDEX_SIZE = 60 * 1024 * 1024
MAX_DECOMPRESSED_INDEX_SIZE = MAX_INDEX_SIZE * 4
MAX_PACKAGE_SIZE = 1024 * 1024 * 1024
USER_AGENT = "RepoShelf/1.0 (local package manager)"
HOST_REQUEST_INTERVAL = 1.0
MAX_HTTP_RETRIES = 3
MAX_AUTOMATIC_RETRY_WAIT = 30
NOT_FOUND_CACHE_SECONDS = 15 * 60
DEFAULT_INDEX_CACHE_SECONDS = 60
KEYRING_SERVICE = "RepoShelf repository credentials"
GENERATED_DEVICE_ID_ACCOUNT = "generated-archive-device-id"
PAID_REPOSITORY_DOMAINS = ("havoc.app", "chariz.com", "yourepo.com")
DEVICE_ARCHITECTURES = {
    "iphoneos-arm",
    "iphoneos-arm64",
    "iphoneos-arm64e",
}
LOCK = threading.RLock()
HOST_LIMITER_LOCK = threading.Lock()
HOST_LIMITERS: dict[str, dict] = {}
DEVICE_ID_LOCK = threading.Lock()
JOBS: dict[str, dict] = {}
STATE: dict = {
    "repos": [],
    "downloads": {},
    "automation": {"enabled": False, "interval_hours": 24},
}


def save_state() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    temporary = STATE_FILE.with_suffix(".tmp")
    temporary.write_text(json.dumps(STATE, indent=2), encoding="utf-8")
    temporary.replace(STATE_FILE)


def load_state() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if not STATE_FILE.exists():
        return
    try:
        saved = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        if isinstance(saved.get("repos"), list):
            STATE["repos"] = saved["repos"]
        if isinstance(saved.get("downloads"), dict):
            STATE["downloads"] = saved["downloads"]
        automation = saved.get("automation", {})
        if isinstance(automation, dict):
            STATE["automation"] = {
                "enabled": bool(automation.get("enabled", False)),
                "interval_hours": max(1, min(168, int(automation.get("interval_hours", 24)))),
            }
    except (OSError, ValueError, TypeError) as error:
        raise RuntimeError(f"Could not read {STATE_FILE}: {error}") from error


def host_limiter(host: str) -> dict:
    with HOST_LIMITER_LOCK:
        return HOST_LIMITERS.setdefault(
            host,
            {"lock": threading.Lock(), "next_request": 0.0, "blocked_until": 0.0},
        )


def retry_after_seconds(value: str | None) -> float | None:
    if not value:
        return None
    try:
        seconds = float(value)
        return max(0.0, seconds) if math.isfinite(seconds) else None
    except ValueError:
        try:
            retry_at = email.utils.parsedate_to_datetime(value)
            if retry_at.tzinfo is None:
                retry_at = retry_at.replace(tzinfo=timezone.utc)
            seconds = (retry_at - datetime.now(timezone.utc)).total_seconds()
            return max(0.0, seconds) if math.isfinite(seconds) else None
        except (TypeError, ValueError, OverflowError):
            return None


def cache_paths(url: str, scope: str = "public") -> tuple[Path, Path]:
    key = hashlib.sha256(f"{scope}\0{url}".encode("utf-8")).hexdigest()
    return HTTP_CACHE_DIR / f"{key}.body", HTTP_CACHE_DIR / f"{key}.json"


def read_http_cache(url: str, scope: str = "public") -> tuple[bytes, dict] | None:
    body_path, metadata_path = cache_paths(url, scope)
    try:
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        body = body_path.read_bytes()
        if metadata.get("status") == 404 and metadata.get("expires_at", 0) <= time.time():
            return None
        return body, metadata
    except (OSError, ValueError, TypeError):
        return None


def remove_http_cache(url: str, scope: str = "public") -> None:
    body_path, metadata_path = cache_paths(url, scope)
    body_path.unlink(missing_ok=True)
    metadata_path.unlink(missing_ok=True)


def write_http_cache(url: str, body: bytes, metadata: dict, scope: str = "public") -> None:
    HTTP_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    body_path, metadata_path = cache_paths(url, scope)
    nonce = uuid.uuid4().hex
    temporary_body = body_path.with_name(f"{body_path.name}.{nonce}.tmp")
    temporary_metadata = metadata_path.with_name(f"{metadata_path.name}.{nonce}.tmp")
    try:
        temporary_body.write_bytes(body)
        temporary_metadata.write_text(json.dumps(metadata), encoding="utf-8")
        temporary_body.replace(body_path)
        temporary_metadata.replace(metadata_path)
    finally:
        temporary_body.unlink(missing_ok=True)
        temporary_metadata.unlink(missing_ok=True)


def response_cache_metadata(response, *, previous: dict | None = None) -> dict:
    cache_control = response.headers.get("Cache-Control", "")
    directives = {
        part.strip().lower()
        for part in cache_control.split(",")
        if part.strip()
    }
    max_age = None
    for directive in directives:
        if directive.startswith("max-age="):
            try:
                max_age = max(0, int(directive.split("=", 1)[1].strip('"')))
            except ValueError:
                pass
            break

    if "no-cache" in directives:
        max_age = 0
    elif max_age is None:
        expires = response.headers.get("Expires")
        if expires:
            try:
                expiry_date = email.utils.parsedate_to_datetime(expires)
                if expiry_date.tzinfo is None:
                    expiry_date = expiry_date.replace(tzinfo=timezone.utc)
                max_age = max(0, int((expiry_date - datetime.now(timezone.utc)).total_seconds()))
            except (TypeError, ValueError, OverflowError):
                max_age = DEFAULT_INDEX_CACHE_SECONDS
        else:
            max_age = DEFAULT_INDEX_CACHE_SECONDS

    metadata = dict(previous or {})
    metadata.update({
        "status": getattr(response, "status", 200),
        "checked_at": int(time.time()),
        "fresh_until": time.time() + max_age,
        "cache_control": cache_control,
        "etag": response.headers.get("ETag") or metadata.get("etag"),
        "last_modified": response.headers.get("Last-Modified") or metadata.get("last_modified"),
    })
    return metadata


@contextmanager
def polite_response(url: str, headers: dict[str, str], timeout: int):
    parsed_url = urllib.parse.urlparse(url)
    host = parsed_url.netloc.lower()
    limiter = host_limiter(host)
    with limiter["lock"]:
        for attempt in range(MAX_HTTP_RETRIES + 1):
            wait_until = max(limiter["next_request"], limiter["blocked_until"])
            delay = wait_until - time.monotonic()
            if delay > MAX_AUTOMATIC_RETRY_WAIT:
                raise ValueError(
                    f"{host} is still rate-limiting RepoShelf. Retry after "
                    f"{delay:.0f} seconds; RepoShelf will not send a request early."
                )
            if delay > 0:
                time.sleep(delay)
            limiter["next_request"] = time.monotonic() + HOST_REQUEST_INTERVAL

            request = urllib.request.Request(url, headers=headers)
            try:
                opener = urllib.request.build_opener(SameOriginRedirectHandler())
                response = opener.open(request, timeout=timeout)
            except urllib.error.HTTPError as error:
                retry_after = retry_after_seconds(error.headers.get("Retry-After"))
                if error.code == 429:
                    if retry_after is None:
                        retry_after = min(2 ** attempt, MAX_AUTOMATIC_RETRY_WAIT + 1)
                    limiter["blocked_until"] = max(
                        limiter["blocked_until"], time.monotonic() + retry_after
                    )
                    if retry_after > MAX_AUTOMATIC_RETRY_WAIT:
                        error.close()
                        raise ValueError(
                            f"{host} rate-limited RepoShelf; retry after {retry_after:.0f} seconds. "
                            "RepoShelf will not shorten the server's Retry-After."
                        ) from error
                    if attempt < MAX_HTTP_RETRIES:
                        error.close()
                        continue
                elif error.code in (500, 502, 503, 504) and attempt < MAX_HTTP_RETRIES:
                    backoff = retry_after if retry_after is not None else min(2 ** attempt, 8)
                    if backoff > MAX_AUTOMATIC_RETRY_WAIT:
                        if retry_after is not None:
                            limiter["blocked_until"] = max(
                                limiter["blocked_until"], time.monotonic() + retry_after
                            )
                        error.close()
                        raise ValueError(
                            f"{host} requested a retry after {backoff:.0f} seconds; "
                            "retry the source later."
                        ) from error
                    error.close()
                    time.sleep(backoff)
                    continue
                error.close()
                raise
            except (TimeoutError, urllib.error.URLError):
                if attempt >= MAX_HTTP_RETRIES:
                    raise
                time.sleep(min(2 ** attempt, 8))
                continue

            try:
                yield response
            finally:
                response.close()
            return

    raise RuntimeError(f"Request retries exhausted for {url}")


def read_limited_body(response, url: str, limit: int) -> bytes:
    chunks = bytearray()
    while True:
        chunk = response.read(min(1024 * 1024, limit + 1 - len(chunks)))
        if not chunk:
            break
        chunks.extend(chunk)
        if len(chunks) > limit:
            raise ValueError(f"Response from {url} exceeds the {limit // (1024 * 1024)} MB limit")
    return bytes(chunks)


class SameOriginRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, file_pointer, code, message, headers, new_url):
        source = urllib.parse.urlparse(request.full_url)
        destination = urllib.parse.urlparse(new_url)
        source_origin = (source.scheme.lower(), source.hostname, source.port)
        destination_origin = (destination.scheme.lower(), destination.hostname, destination.port)
        if source_origin != destination_origin:
            file_pointer.close()
            raise urllib.error.HTTPError(
                new_url,
                403,
                "Cross-origin redirect blocked to protect repository credentials",
                headers,
                None,
            )
        return super().redirect_request(
            request, file_pointer, code, message, headers, new_url
        )


def store_repo_credentials(repo_id: str, method: str, username: str = "", secret: str = "") -> None:
    if method == "none":
        return
    try:
        import keyring

        value = {"username": username, "secret": secret}
        keyring.set_password(KEYRING_SERVICE, repo_id, json.dumps(value))
    except Exception as error:
        raise ValueError(
            "Could not save credentials to the system keyring. Unlock or enable your desktop keyring and try again."
        ) from error


def remove_repo_credentials(repo_id: str) -> None:
    try:
        import keyring

        for account in (repo_id, f"{repo_id}:device-id"):
            if keyring.get_password(KEYRING_SERVICE, account) is not None:
                keyring.delete_password(KEYRING_SERVICE, account)
    except Exception as error:
        raise ValueError(
            "Could not remove credentials from the system keyring. Unlock your desktop keyring and try again."
        ) from error


def generated_device_identifier() -> str:
    try:
        import keyring

        with DEVICE_ID_LOCK:
            value = keyring.get_password(KEYRING_SERVICE, GENERATED_DEVICE_ID_ACCOUNT)
            if value and re.fullmatch(r"[0-9a-f]{40}", value):
                return value
            value = os.urandom(20).hex()
            keyring.set_password(KEYRING_SERVICE, GENERATED_DEVICE_ID_ACCOUNT, value)
            return value
    except Exception as error:
        raise ValueError(
            "Could not access the system keyring to create a local archive ID. "
            "Unlock or enable your desktop keyring and try again."
        ) from error


def requires_manual_device_identifier(url: str) -> bool:
    hostname = (urllib.parse.urlparse(url).hostname or "").lower().rstrip(".")
    return any(
        hostname == domain or hostname.endswith(f".{domain}")
        for domain in PAID_REPOSITORY_DOMAINS
    )


def require_https_repository(url: str) -> urllib.parse.SplitResult:
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme.lower() != "https" or not parsed.hostname:
        raise ValueError("Repository sources must use HTTPS to protect package indexes and downloads.")
    try:
        parsed.port
    except ValueError as error:
        raise ValueError("Enter a valid repository URL and port.") from error
    return parsed


def store_repo_device_identifier(repo_id: str, identifier: str) -> None:
    try:
        import keyring

        keyring.set_password(KEYRING_SERVICE, f"{repo_id}:device-id", identifier)
    except Exception as error:
        raise ValueError(
            "Could not save the repository device ID to the system keyring. "
            "Unlock or enable your desktop keyring and try again."
        ) from error


def repository_request_headers(repo: dict) -> dict[str, str]:
    method = repo.get("auth", {}).get("method", "none")
    headers = {"User-Agent": USER_AGENT}
    if method != "none":
        try:
            import keyring

            saved = keyring.get_password(KEYRING_SERVICE, repo["id"])
        except Exception as error:
            raise ValueError(
                "Could not access the system keyring. Unlock or enable your desktop keyring and try again."
            ) from error
        if not saved:
            raise ValueError(
                f"No saved credentials are available for {repo['name']}. Remove and re-add the source with authorization."
            )
        try:
            credentials = json.loads(saved)
            secret = credentials["secret"]
            if not isinstance(secret, str) or not secret:
                raise ValueError("The saved credential is empty.")
            if method == "basic":
                username = credentials.get("username")
                if not isinstance(username, str) or not username:
                    raise ValueError("The saved username is empty.")
                encoded = base64.b64encode(f"{username}:{secret}".encode("utf-8")).decode("ascii")
                headers["Authorization"] = f"Basic {encoded}"
            elif method == "bearer":
                scheme = "Bear" + "er "
                headers["Authorization"] = scheme + secret
            else:
                raise ValueError(f"Unsupported authentication method for {repo['name']}.")
        except (KeyError, TypeError, json.JSONDecodeError) as error:
            raise ValueError(f"Could not read saved credentials for {repo['name']}.") from error

    profile = repo.get("device_profile", {})
    if profile.get("enabled"):
        try:
            import keyring

            identifier_account = (
                f"{repo['id']}:device-id"
                if profile.get("device_id_mode") == "manual"
                else GENERATED_DEVICE_ID_ACCOUNT
            )
            device_identifier = keyring.get_password(KEYRING_SERVICE, identifier_account)
        except Exception as error:
            raise ValueError(
                "Could not access the system keyring for the repository device ID. "
                "Unlock or enable your desktop keyring and try again."
            ) from error
        if not device_identifier:
            raise ValueError(
                f"No device ID is saved for {repo['name']}. Remove and re-add the source with its device ID."
            )
        model = profile["model"]
        os_version = profile["os_version"]
        architecture = profile["architecture"]
        client_version = profile["client_version"]
        headers.update({
            "User-Agent": f"Sileo/{client_version} (iPhone; iOS {os_version})",
            "X-Machine": model,
            "X-Unique-ID": device_identifier,
            "X-Firmware": os_version,
            "Sec-CH-UA": f"Sileo;v={client_version};t=client",
            "Sec-CH-UA-Platform": "iphoneos",
            "Sec-CH-UA-Platform-Version": os_version,
            "Sec-CH-UA-Arch": architecture,
            "Sec-CH-UA-Bitness": "64" if architecture != "iphoneos-arm" else "32",
            "Sec-CH-UA-Model": model,
        })
    return headers


def fetch_bytes(
    url: str,
    limit: int,
    *,
    cache: bool = True,
    headers: dict[str, str] | None = None,
    cache_scope: str = "public",
) -> bytes:
    cached = read_http_cache(url, cache_scope) if cache else None
    if cached and cached[1].get("status") == 404:
        raise urllib.error.HTTPError(url, 404, "Not Found (cached)", None, None)
    if cached and cached[1].get("fresh_until", 0) > time.time():
        body = cached[0]
        if len(body) > limit:
            raise ValueError(
                f"Cached response from {url} exceeds the {limit // (1024 * 1024)} MB limit"
            )
        return body

    request_headers = {"User-Agent": USER_AGENT}
    if headers:
        request_headers.update(headers)
    if cached:
        _, metadata = cached
        if metadata.get("etag"):
            request_headers["If-None-Match"] = metadata["etag"]
        if metadata.get("last_modified"):
            request_headers["If-Modified-Since"] = metadata["last_modified"]

    try:
        with polite_response(url, request_headers, timeout=25) as response:
            body = read_limited_body(response, url, limit)
            if cache and "no-store" not in response.headers.get("Cache-Control", "").lower():
                write_http_cache(url, body, response_cache_metadata(response), cache_scope)
            elif cache:
                remove_http_cache(url, cache_scope)
            return body
    except urllib.error.HTTPError as error:
        if error.code == 304 and cached:
            error.close()
            body, metadata = cached
            if len(body) > limit:
                raise ValueError(
                    f"Cached response from {url} exceeds the {limit // (1024 * 1024)} MB limit"
                ) from error
            updated = response_cache_metadata(error, previous=metadata)
            if "no-store" in updated.get("cache_control", "").lower():
                remove_http_cache(url, cache_scope)
            else:
                write_http_cache(url, body, updated, cache_scope)
            return body
        if error.code == 404 and cache:
            write_http_cache(
                url,
                b"",
                {"status": 404, "expires_at": time.time() + NOT_FOUND_CACHE_SECONDS},
                cache_scope,
            )
        error.close()
        raise


def decompress_index(data: bytes, name: str) -> bytes:
    source = BytesIO(data)
    if name.endswith(".gz"):
        stream = gzip.GzipFile(fileobj=source)
    elif name.endswith(".bz2"):
        stream = bz2.BZ2File(source)
    elif name.endswith(".xz"):
        stream = lzma.LZMAFile(source)
    else:
        return data
    with stream:
        output = bytearray()
        while True:
            chunk = stream.read(
                min(1024 * 1024, MAX_DECOMPRESSED_INDEX_SIZE + 1 - len(output))
            )
            if not chunk:
                return bytes(output)
            output.extend(chunk)
            if len(output) > MAX_DECOMPRESSED_INDEX_SIZE:
                raise ValueError(
                    "Decompressed package index exceeds the "
                    f"{MAX_DECOMPRESSED_INDEX_SIZE // (1024 * 1024)} MB limit"
                )


def release_index_paths(
    base_url: str,
    auth_headers: dict[str, str] | None = None,
    cache_scope: str = "public",
) -> list[str]:
    """Find common flat repo indexes and Debian-style iOS indexes."""
    paths = [
        "Packages",
        "Packages.gz",
        "Packages.bz2",
        "Packages.xz",
    ]
    release_text = ""
    for release_name in ("InRelease", "Release"):
        try:
            release_text = fetch_bytes(
                urllib.parse.urljoin(base_url, release_name),
                2 * 1024 * 1024,
                headers=auth_headers,
                cache_scope=cache_scope,
            ).decode("utf-8", errors="replace")
            if "-----BEGIN PGP SIGNATURE-----" in release_text:
                release_text = release_text.split("-----BEGIN PGP SIGNATURE-----", 1)[0]
            break
        except urllib.error.HTTPError as error:
            if error.code in (401, 403, 429):
                raise
            continue
        except (urllib.error.URLError, TimeoutError, ValueError):
            continue

    fields = {}
    current_key = None
    for line in release_text.splitlines():
        if line[:1].isspace() and current_key:
            fields[current_key] += " " + line.strip()
        elif ":" in line:
            current_key, value = line.split(":", 1)
            fields[current_key] = value.strip()

    suites = list(dict.fromkeys(
        value for value in (fields.get("Codename"), fields.get("Suite"), "stable") if value
    ))
    components = fields.get("Components", "main").split()
    architectures = fields.get("Architectures", "").split()
    ios_arches = [
        arch for arch in architectures
        if arch in {"iphoneos-arm", "iphoneos-arm64", "iphoneos-arm64e", "all"}
        or arch.startswith(("darwin-arm", "iphoneos-"))
    ]
    if not ios_arches:
        ios_arches = ["iphoneos-arm64", "iphoneos-arm", "iphoneos-arm64e", "all"]

    for suite in suites:
        for component in components:
            for architecture in ios_arches:
                prefix = f"dists/{suite}/{component}/binary-{architecture}/Packages"
                paths.extend(prefix + suffix for suffix in ("", ".gz", ".bz2", ".xz"))
        # Some flat Debian repositories publish indexes directly under dists/<suite>.
        paths.extend(f"dists/{suite}/Packages{suffix}" for suffix in ("", ".gz", ".bz2", ".xz"))
    return list(dict.fromkeys(paths))


def parse_packages(data: bytes) -> list[dict]:
    text = data.decode("utf-8", errors="replace")
    entries = []
    for block in re.split(r"\n\s*\n", text):
        fields: dict[str, str] = {}
        last_key = None
        for line in block.splitlines():
            if line[:1].isspace() and last_key:
                fields[last_key] += "\n" + line[1:]
            elif ":" in line:
                key, value = line.split(":", 1)
                last_key = key
                fields[key] = value.strip()
        if fields.get("Package") and fields.get("Filename"):
            filename = fields["Filename"].strip()
            parsed_path = urllib.parse.urlparse(filename)
            if parsed_path.scheme or parsed_path.netloc or filename.startswith("/"):
                continue
            if any(part == ".." for part in filename.split("/")):
                continue
            package = {
                "name": fields["Package"],
                "version": fields.get("Version", ""),
                "architecture": fields.get("Architecture", "all"),
                "description": fields.get("Description", "").splitlines()[0]
                if fields.get("Description") else "",
                "filename": filename,
                "size": fields.get("Size", ""),
                "sha256": fields.get("SHA256", ""),
            }
            entries.append(package)
    return entries


def refresh_repo(repo: dict) -> int:
    require_https_repository(repo["url"])
    errors = []
    auth_headers = repository_request_headers(repo)
    for index_path in release_index_paths(repo["url"], auth_headers, repo["id"]):
        index_url = urllib.parse.urljoin(repo["url"], index_path)
        try:
            compressed = fetch_bytes(
                index_url,
                MAX_INDEX_SIZE,
                headers=auth_headers,
                cache_scope=repo["id"],
            )
            content = decompress_index(compressed, index_path)
            if len(content) > MAX_DECOMPRESSED_INDEX_SIZE:
                raise ValueError("Decompressed package index exceeds the 240 MB limit")
            packages = parse_packages(content)
            if packages:
                unique = {}
                for package in packages:
                    key = (package["name"], package["version"], package["architecture"], package["filename"])
                    unique[key] = package
                repo["packages"] = list(unique.values())
                repo["refreshed_at"] = int(time.time())
                return len(repo["packages"])
        except urllib.error.HTTPError as error:
            if error.code in (401, 403, 429):
                raise
            errors.append(f"{index_path}: HTTP {error.code}")
        except (urllib.error.URLError, TimeoutError, OSError, EOFError, ValueError, lzma.LZMAError) as error:
            errors.append(f"{index_path}: {error}")
    detail = errors[-1] if errors else "No usable package index was found"
    raise ValueError(f"Could not load a Packages index from {repo['url']}. {detail}")


def refresh_repositories(repos: list[dict]) -> tuple[list[str], set[str]]:
    errors = []
    failed_ids = set()
    for repo in repos:
        try:
            refresh_repo(repo)
        except Exception as error:
            failed_ids.add(repo["id"])
            errors.append(f"{repo['name']}: {error}")
    return errors, failed_ids


def package_id(repo_id: str, package: dict) -> str:
    source = "\0".join(
        [repo_id, package["name"], package["version"], package["architecture"], package["filename"]]
    )
    return hashlib.sha256(source.encode()).hexdigest()


def public_state() -> dict:
    with LOCK:
        repos = []
        for repo in STATE["repos"]:
            packages = []
            for package in repo.get("packages", []):
                item = {key: value for key, value in package.items() if key != "sha256"}
                item["id"] = package_id(repo["id"], package)
                item["repo_id"] = repo["id"]
                item["repo_name"] = repo["name"]
                item["downloaded"] = (
                    STATE["downloads"].get(item["id"], {}).get("version") == item["version"]
                )
                packages.append(item)
            repos.append({
                "id": repo["id"],
                "name": repo["name"],
                "url": repo["url"],
                "refreshed_at": repo.get("refreshed_at"),
                "auth_method": repo.get("auth", {}).get("method", "none"),
                "device_profile": dict(repo.get("device_profile", {"enabled": False})),
                "packages": packages,
            })
        return {
            "repos": repos,
            "automation": dict(STATE["automation"]),
            "download_dir": str(DOWNLOAD_DIR),
        }


def download_package(repo: dict, package: dict, job: dict) -> None:
    repo_url = require_https_repository(repo["url"])
    identifier = package_id(repo["id"], package)
    existing = STATE["downloads"].get(identifier, {})
    if existing.get("version") == package["version"] and Path(existing.get("path", "")).is_file():
        return

    filename = package["filename"]
    package_url = urllib.parse.urljoin(repo["url"], filename.lstrip("./"))
    package_url_parts = urllib.parse.urlsplit(package_url)
    if (
        package_url_parts.scheme.lower() != "https"
        or package_url_parts.hostname != repo_url.hostname
        or package_url_parts.port != repo_url.port
        or package_url_parts.username is not None
        or package_url_parts.password is not None
    ):
        raise ValueError(f"Package URL is outside the repository host: {filename}")
    repo_folder = re.sub(r"[^A-Za-z0-9._-]+", "_", repo["name"]).strip("._") or "repo"
    basename = Path(filename).name
    safe_name = re.sub(r"[^A-Za-z0-9._+-]+", "_", package["name"])
    safe_version = re.sub(r"[^A-Za-z0-9._+-]+", "_", package["version"])
    safe_arch = re.sub(r"[^A-Za-z0-9._+-]+", "_", package["architecture"])
    destination_dir = DOWNLOAD_DIR / repo_folder
    destination_dir.mkdir(parents=True, exist_ok=True)
    destination = destination_dir / f"{safe_name}_{safe_version}_{safe_arch}_{basename}"
    temporary = destination.with_suffix(destination.suffix + ".part")
    headers = repository_request_headers(repo)
    digest = hashlib.sha256()
    received = 0
    try:
        with polite_response(package_url, headers, timeout=45) as response, temporary.open("wb") as output:
            while True:
                chunk = response.read(1024 * 1024)
                if not chunk:
                    break
                received += len(chunk)
                if received > MAX_PACKAGE_SIZE:
                    raise ValueError(f"Package exceeds the {MAX_PACKAGE_SIZE // (1024 * 1024)} MB limit")
                output.write(chunk)
                digest.update(chunk)
        expected_size = package.get("size", "")
        if expected_size.isdigit() and int(expected_size) != received:
            raise ValueError(f"Size check failed for {package['name']}: expected {expected_size}, got {received}")
        expected_hash = package.get("sha256", "").lower()
        if expected_hash and digest.hexdigest().lower() != expected_hash:
            raise ValueError(f"SHA256 check failed for {package['name']}")
        temporary.replace(destination)
        with LOCK:
            STATE["downloads"][identifier] = {
                "version": package["version"],
                "path": str(destination),
                "downloaded_at": int(time.time()),
            }
            save_state()
    finally:
        if temporary.exists():
            temporary.unlink()


def start_download_job(package_refs: list[tuple[str, str]], automatic: bool = False) -> str:
    job_id = uuid.uuid4().hex
    job = {"id": job_id, "status": "running", "total": len(package_refs), "completed": 0,
           "errors": [], "automatic": automatic, "started_at": int(time.time())}
    with LOCK:
        JOBS[job_id] = job

    def run() -> None:
        try:
            for repo_id, identifier in package_refs:
                with LOCK:
                    repo = next((r for r in STATE["repos"] if r["id"] == repo_id), None)
                    package = next(
                        (p for p in repo.get("packages", []) if package_id(repo_id, p) == identifier),
                        None,
                    ) if repo else None
                if not repo or not package:
                    job["errors"].append("A package or repository was removed before download.")
                else:
                    try:
                        download_package(repo, package, job)
                    except Exception as error:
                        job["errors"].append(f"{package['name']}: {error}")
                job["completed"] += 1
            job["status"] = "completed"
            job["finished_at"] = int(time.time())
        except Exception as error:
            job["status"] = "failed"
            job["errors"].append(str(error))
            traceback.print_exc()

    threading.Thread(target=run, daemon=True, name=f"download-{job_id[:8]}").start()
    return job_id


def automation_loop() -> None:
    next_run = None
    previous_settings = None
    while True:
        time.sleep(2)
        with LOCK:
            settings = dict(STATE["automation"])
        if settings != previous_settings:
            next_run = time.time() + settings["interval_hours"] * 3600 if settings["enabled"] else None
            previous_settings = settings
        if settings["enabled"] and next_run is not None and time.time() >= next_run:
            next_run = time.time() + settings["interval_hours"] * 3600
            with LOCK:
                repos = list(STATE["repos"])
            errors, failed_ids = refresh_repositories(repos)
            for error in errors:
                print(f"Automation could not refresh a source: {error}")
            try:
                with LOCK:
                    save_state()
                    refs = [
                        (repo["id"], package_id(repo["id"], package))
                        for repo in STATE["repos"]
                        if repo["id"] not in failed_ids
                        for package in repo.get("packages", [])
                        if STATE["downloads"].get(package_id(repo["id"], package), {}).get("version")
                        != package["version"]
                    ]
                if refs:
                    start_download_job(refs, automatic=True)
            except Exception:
                traceback.print_exc()


class Handler(BaseHTTPRequestHandler):
    server_version = "RepoShelf/1.0"

    def log_message(self, format_string: str, *args) -> None:
        print(f"[{self.log_date_time_string()}] {format_string % args}")

    def send_json(self, data: dict, status: int = 200) -> None:
        body = json.dumps(data).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def trusted_request_origin(self) -> bool:
        fetch_site = self.headers.get("Sec-Fetch-Site")
        if fetch_site and fetch_site != "same-origin":
            return False
        origin = self.headers.get("Origin")
        host_header = self.headers.get("Host", "")
        try:
            host_url = urllib.parse.urlsplit(f"//{host_header}")
            hostname = (host_url.hostname or "").rstrip(".").lower()
            host_port = host_url.port
            if (
                not hostname
                or host_url.username is not None
                or host_url.password is not None
                or host_url.path
                or host_url.query
                or host_url.fragment
                or host_port != self.server.server_port
            ):
                return False
        except ValueError:
            return False

        if hostname == "localhost":
            pass
        else:
            try:
                request_ip = ipaddress.ip_address(hostname)
                bound_ip = ipaddress.ip_address(self.server.server_address[0])
            except ValueError:
                return False
            if not request_ip.is_loopback:
                if bound_ip.is_unspecified:
                    if not (request_ip.is_private or request_ip.is_link_local):
                        return False
                elif request_ip != bound_ip:
                    return False
                if not origin and fetch_site != "same-origin":
                    return False

        if not origin:
            return True
        try:
            origin_url = urllib.parse.urlsplit(origin)
            origin_hostname = (origin_url.hostname or "").rstrip(".").lower()
            origin_port = origin_url.port or (443 if origin_url.scheme == "https" else 80)
            return (
                origin_url.scheme == "http"
                and origin_hostname == hostname
                and origin_port == self.server.server_port
                and not origin_url.username
                and not origin_url.password
                and origin_url.path in ("", "/")
                and not origin_url.query
                and not origin_url.fragment
            )
        except ValueError:
            return False

    def reject_untrusted_origin(self) -> bool:
        if self.trusted_request_origin():
            return False
        self.send_json({"error": "Request host or origin is not allowed."}, 403)
        return True

    def read_json(self) -> dict:
        length = int(self.headers.get("Content-Length", "0"))
        if length < 0 or length > 1024 * 1024:
            raise ValueError("Request body is too large")
        data = json.loads(self.rfile.read(length) or b"{}")
        if not isinstance(data, dict):
            raise ValueError("Request body must be a JSON object.")
        return data

    def do_GET(self) -> None:
        if self.reject_untrusted_origin():
            return
        path = urllib.parse.urlparse(self.path).path
        if path == "/api/state":
            self.send_json(public_state())
        elif path.startswith("/api/jobs/"):
            job_id = path.rsplit("/", 1)[-1]
            with LOCK:
                job = JOBS.get(job_id)
                if job:
                    job = dict(job)
            self.send_json(job or {"error": "Job not found"}, 200 if job else 404)
        elif path == "/api/device-profile":
            try:
                self.send_json({"generated_device_id": generated_device_identifier()})
            except ValueError as error:
                self.send_json({"error": str(error)}, 503)
        elif path in ("/", "/index.html", "/app.css", "/app.js"):
            filename = "index.html" if path in ("/", "/index.html") else path.lstrip("/")
            content_type = {"index.html": "text/html; charset=utf-8", "app.css": "text/css; charset=utf-8",
                            "app.js": "text/javascript; charset=utf-8"}[filename]
            body = (ROOT / "static" / filename).read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_json({"error": "Not found"}, 404)

    def do_POST(self) -> None:
        if self.reject_untrusted_origin():
            return
        path = urllib.parse.urlparse(self.path).path
        try:
            data = self.read_json()
            if path == "/api/repos":
                raw_url = str(data.get("url", "")).strip()
                parsed = urllib.parse.urlparse(raw_url)
                if (
                    parsed.scheme.lower() != "https"
                    or not parsed.netloc
                    or parsed.username is not None
                    or parsed.password is not None
                    or parsed.fragment
                ):
                    raise ValueError("Enter a valid HTTPS repository URL.")
                require_https_repository(raw_url)
                auth = data.get("auth", {})
                if not isinstance(auth, dict):
                    raise ValueError("Repository authorization settings are invalid.")
                auth_method = auth.get("method", "none")
                if auth_method not in ("none", "basic", "bearer"):
                    raise ValueError("Choose no authorization, username/password, or access token.")
                username = auth.get("username", "")
                secret = auth.get("secret", "")
                if auth_method == "basic" and (
                    not isinstance(username, str)
                    or not username.strip()
                    or not isinstance(secret, str)
                    or not secret
                ):
                    raise ValueError("Enter the username and password provided for this repository.")
                if auth_method == "bearer" and (
                    not isinstance(secret, str) or not secret.strip()
                ):
                    raise ValueError("Enter the access token provided by the repository.")
                device_profile = data.get("device_profile", {})
                if not isinstance(device_profile, dict):
                    raise ValueError("Device compatibility settings are invalid.")
                profile_enabled = bool(device_profile.get("enabled", False))
                normalized_profile = {"enabled": False}
                device_identifier = ""
                paid_repository = requires_manual_device_identifier(raw_url)
                if paid_repository and not profile_enabled:
                    raise ValueError(
                        "Paid repositories require the Sileo-compatible profile and a manually entered authorized device ID."
                    )
                if profile_enabled:
                    if device_profile.get("client", "sileo") != "sileo":
                        raise ValueError("Choose the Sileo-compatible client profile.")
                    model = str(device_profile.get("model", "")).strip()
                    os_version = str(device_profile.get("os_version", "")).strip()
                    architecture = str(device_profile.get("architecture", "")).strip()
                    client_version = str(device_profile.get("client_version", "")).strip()
                    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9,._-]{0,39}", model):
                        raise ValueError("Enter a valid device model, such as iPhone15,2.")
                    if not re.fullmatch(r"[0-9][A-Za-z0-9.+_-]{0,31}", os_version):
                        raise ValueError("Enter a valid iOS version, such as 17.0.")
                    if architecture not in DEVICE_ARCHITECTURES:
                        raise ValueError("Choose a supported iOS device architecture.")
                    if not re.fullmatch(r"[0-9][A-Za-z0-9.+_-]{0,31}", client_version):
                        raise ValueError("Enter a valid package-manager version, such as 2.4.4.")
                    manual_device_id = bool(device_profile.get("manual_device_id", False))
                    if paid_repository and not manual_device_id:
                        raise ValueError(
                            "This paid repository requires you to enter its authorized device ID manually."
                        )
                    if manual_device_id:
                        device_identifier = str(device_profile.get("device_id", "")).strip().lower()
                        if not re.fullmatch(r"[0-9a-f]{40}", device_identifier):
                            raise ValueError(
                                "Enter the 40-character device ID authorized by the paid repository."
                            )
                    normalized_profile = {
                        "enabled": True,
                        "client": "sileo",
                        "client_version": client_version,
                        "model": model,
                        "os_version": os_version,
                        "architecture": architecture,
                        "device_id_mode": "manual" if manual_device_id else "generated",
                    }
                if profile_enabled and normalized_profile["device_id_mode"] == "generated":
                    device_identifier = generated_device_identifier()
                repo = {
                    "id": uuid.uuid4().hex,
                    "url": raw_url.rstrip("/") + "/",
                    "name": parsed.netloc + (parsed.path.rstrip("/") or ""),
                    "auth": {"method": auth_method},
                    "device_profile": normalized_profile,
                    "packages": [],
                }
                store_repo_credentials(repo["id"], auth_method, username.strip(), secret)
                try:
                    if profile_enabled and normalized_profile["device_id_mode"] == "manual":
                        store_repo_device_identifier(repo["id"], device_identifier)
                    count = refresh_repo(repo)
                except Exception:
                    if auth_method != "none" or device_identifier:
                        remove_repo_credentials(repo["id"])
                    raise
                with LOCK:
                    STATE["repos"].append(repo)
                    save_state()
                self.send_json({"repo_id": repo["id"], "package_count": count, "state": public_state()}, 201)
            elif path.startswith("/api/repos/") and path.endswith("/refresh"):
                repo_id = path.split("/")[-2]
                with LOCK:
                    repo = next((item for item in STATE["repos"] if item["id"] == repo_id), None)
                if not repo:
                    raise ValueError("Repository not found.")
                count = refresh_repo(repo)
                with LOCK:
                    save_state()
                self.send_json({"package_count": count, "state": public_state()})
            elif path == "/api/downloads":
                requested = data.get("package_ids")
                if not isinstance(requested, list):
                    raise ValueError("package_ids must be a list.")
                with LOCK:
                    refs = [
                        (repo["id"], package_id(repo["id"], package))
                        for repo in STATE["repos"]
                        for package in repo.get("packages", [])
                        if package_id(repo["id"], package) in requested
                    ]
                job_id = start_download_job(refs)
                self.send_json({"job_id": job_id}, 202)
            elif path == "/api/automation":
                enabled = bool(data.get("enabled", False))
                try:
                    interval = int(data.get("interval_hours", 24))
                except (ValueError, TypeError) as error:
                    raise ValueError("Interval must be a number of hours.") from error
                if interval < 1 or interval > 168:
                    raise ValueError("Interval must be between 1 and 168 hours.")
                with LOCK:
                    STATE["automation"] = {"enabled": enabled, "interval_hours": interval}
                    save_state()
                self.send_json({"automation": dict(STATE["automation"])})
            elif path == "/api/automation/run":
                with LOCK:
                    repos = list(STATE["repos"])
                errors, failed_ids = refresh_repositories(repos)
                with LOCK:
                    save_state()
                    refs = [
                        (repo["id"], package_id(repo["id"], package))
                        for repo in STATE["repos"]
                        if repo["id"] not in failed_ids
                        for package in repo.get("packages", [])
                        if STATE["downloads"].get(package_id(repo["id"], package), {}).get("version")
                        != package["version"]
                    ]
                job_id = start_download_job(refs, automatic=True)
                self.send_json({"job_id": job_id, "errors": errors}, 202)
            else:
                self.send_json({"error": "Not found"}, 404)
        except (ValueError, json.JSONDecodeError) as error:
            self.send_json({"error": str(error)}, 400)
        except urllib.error.HTTPError as error:
            if error.code in (401, 403):
                message = (
                    "The repository denied access. Check your authorized account/token, purchase entitlement, "
                    "and repository URL with the provider."
                )
                status = 403
            elif error.code == 429:
                delay = retry_after_seconds(error.headers.get("Retry-After"))
                message = "The repository rate-limited RepoShelf."
                if delay is not None:
                    message += f" Retry after {delay:.0f} seconds."
                status = 429
            else:
                message = f"Repository returned HTTP {error.code}."
                status = 502
            error.close()
            self.send_json({"error": message}, status)
        except urllib.error.URLError as error:
            self.send_json({"error": f"Repository request failed: {error.reason}"}, 502)
        except Exception as error:
            traceback.print_exc()
            self.send_json({"error": str(error)}, 500)

    def do_DELETE(self) -> None:
        if self.reject_untrusted_origin():
            return
        path = urllib.parse.urlparse(self.path).path
        if not path.startswith("/api/repos/"):
            self.send_json({"error": "Not found"}, 404)
            return
        repo_id = path.rsplit("/", 1)[-1]
        with LOCK:
            repo = next((item for item in STATE["repos"] if item["id"] == repo_id), None)
            if not repo:
                self.send_json({"error": "Repository not found"}, 404)
                return
            if (
                repo.get("auth", {}).get("method", "none") != "none"
                or repo.get("device_profile", {}).get("enabled")
                or repo.get("device_profile", {}).get("device_id_mode") == "manual"
            ):
                try:
                    remove_repo_credentials(repo_id)
                except ValueError as error:
                    self.send_json({"error": str(error)}, 500)
                    return
            STATE["repos"] = [item for item in STATE["repos"] if item["id"] != repo_id]
            save_state()
        self.send_json({"state": public_state()})


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="Run the local RepoShelf package manager.")
    parser.add_argument("--host", default="127.0.0.1", help="Listen address (default: 127.0.0.1)")
    parser.add_argument("--port", type=int, default=8765, help="Port (default: 8765)")
    args = parser.parse_args()

    load_state()
    DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
    threading.Thread(target=automation_loop, daemon=True, name="automation").start()
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"RepoShelf is running at http://{args.host}:{args.port}")
    print(f"Downloaded packages are saved in: {DOWNLOAD_DIR}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down RepoShelf.")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
