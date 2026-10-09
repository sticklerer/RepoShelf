#!/usr/bin/env python3
"""Local web app for browsing and downloading packages from jailbreak repos."""

from __future__ import annotations

import base64
import bz2
import email.utils
from contextlib import contextmanager
from datetime import datetime, timezone
import http.client
import gzip
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from io import BytesIO
import ipaddress
import json
import logging
import lzma
import math
import os
from pathlib import Path
import re
import secrets
import socket
import ssl
import threading
import time
import traceback
import urllib.error
import urllib.parse
import uuid


ROOT = Path(__file__).resolve().parent
DATA_DIR = Path(
    os.environ.get("REPOSHELF_DATA_DIR", str(ROOT / "data"))
).expanduser()
DOWNLOAD_DIR = DATA_DIR / "downloads"
HTTP_CACHE_DIR = DATA_DIR / "http_cache"
STATE_FILE = DATA_DIR / "state.json"
PACKAGED_INSTALL = os.environ.get("REPOSHELF_PACKAGED") == "1"
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
GENERATED_DEVICE_PROFILES = (
    {
        "model": "iPhone13,2",
        "os_version": "16.0",
        "architecture": "iphoneos-arm64",
        "client_version": "2.4.4",
    },
    {
        "model": "iPhone14,5",
        "os_version": "16.7.10",
        "architecture": "iphoneos-arm64",
        "client_version": "2.4.4",
    },
    {
        "model": "iPhone15,2",
        "os_version": "17.0",
        "architecture": "iphoneos-arm64",
        "client_version": "2.4.4",
    },
    {
        "model": "iPhone16,1",
        "os_version": "18.0",
        "architecture": "iphoneos-arm64",
        "client_version": "2.4.4",
    },
)
LOCK = threading.RLock()
HOST_LIMITER_LOCK = threading.Lock()
HOST_LIMITERS: dict[str, dict] = {}
DEVICE_ID_LOCK = threading.Lock()
APP_LOGGER = logging.getLogger("reposhelf")
APP_LOGGER.propagate = False
APP_LOG_FILE_HANDLER: logging.FileHandler | None = None
JOBS: dict[str, dict] = {}
REPO_ICON_CACHE: dict[str, tuple[bytes, str] | None] = {}
PACKAGE_ICON_CACHE: dict[str, tuple[bytes, str] | None] = {}
UNINSTALL_REQUEST: bool | None = None
STATE: dict = {
    "repos": [],
    "groups": [],
    "downloads": {},
    "automation": {
        "enabled": False,
        "interval_seconds": 24 * 60 * 60,
        "per_repo_intervals_seconds": {},
        "per_group_intervals_seconds": {},
    },
    "settings": {
        "logging_enabled": False,
        "show_automation_banner": True,
        "keep_download_status_until_done": False,
        "show_device_info_on_add": True,
        "grouping_enabled": True,
    },
}
MAX_AUTOMATION_SECONDS = 99 * 365 * 24 * 60 * 60


def log_exception(message: str, *args) -> None:
    if APP_LOG_FILE_HANDLER is not None:
        APP_LOGGER.exception(message, *args)
    else:
        traceback.print_exc()


def save_state() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    temporary = STATE_FILE.with_suffix(".tmp")
    temporary.write_text(json.dumps(STATE, indent=2), encoding="utf-8")
    temporary.replace(STATE_FILE)


def configure_logging(enabled: bool) -> None:
    global APP_LOG_FILE_HANDLER
    if APP_LOG_FILE_HANDLER is not None:
        APP_LOGGER.removeHandler(APP_LOG_FILE_HANDLER)
        APP_LOG_FILE_HANDLER.close()
        APP_LOG_FILE_HANDLER = None
    if enabled:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        APP_LOG_FILE_HANDLER = logging.FileHandler(
            DATA_DIR / "reposhelf.log", encoding="utf-8"
        )
        APP_LOG_FILE_HANDLER.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)s %(threadName)s %(message)s")
        )
        APP_LOGGER.addHandler(APP_LOG_FILE_HANDLER)
        APP_LOGGER.setLevel(logging.INFO)


def set_logging_enabled(enabled: bool) -> None:
    update_settings(logging_enabled=enabled)


def update_settings(
    *,
    logging_enabled: bool | None = None,
    show_automation_banner: bool | None = None,
    keep_download_status_until_done: bool | None = None,
    show_device_info_on_add: bool | None = None,
    grouping_enabled: bool | None = None,
) -> None:
    updates = {}
    if logging_enabled is not None:
        updates["logging_enabled"] = logging_enabled
    if show_automation_banner is not None:
        updates["show_automation_banner"] = show_automation_banner
    if keep_download_status_until_done is not None:
        updates["keep_download_status_until_done"] = keep_download_status_until_done
    if show_device_info_on_add is not None:
        updates["show_device_info_on_add"] = show_device_info_on_add
    if grouping_enabled is not None:
        updates["grouping_enabled"] = grouping_enabled
    if not updates:
        return
    with LOCK:
        settings = STATE.setdefault(
            "settings",
            {
                "logging_enabled": False,
                "show_automation_banner": True,
                "keep_download_status_until_done": False,
                "show_device_info_on_add": True,
                "grouping_enabled": True,
            },
        )
        previous = {
            "logging_enabled": bool(settings.get("logging_enabled", False)),
            "show_automation_banner": bool(settings.get("show_automation_banner", True)),
            "keep_download_status_until_done": bool(
                settings.get("keep_download_status_until_done", False)
            ),
            "show_device_info_on_add": bool(settings.get("show_device_info_on_add", True)),
            "grouping_enabled": bool(settings.get("grouping_enabled", True)),
        }
        if all(previous.get(key) == value for key, value in updates.items()):
            return
        next_settings = {**previous, **updates}
        logging_changed = previous["logging_enabled"] != next_settings["logging_enabled"]
        if logging_changed:
            configure_logging(next_settings["logging_enabled"])
        STATE["settings"] = next_settings
        try:
            save_state()
        except OSError:
            STATE["settings"] = previous
            if logging_changed:
                configure_logging(previous["logging_enabled"])
            raise
        if logging_changed and next_settings["logging_enabled"]:
            APP_LOGGER.info("File logging enabled.")


def request_uninstall(delete_data: bool) -> None:
    global UNINSTALL_REQUEST
    with LOCK:
        if UNINSTALL_REQUEST is not None:
            raise ValueError("An uninstall is already in progress.")
        UNINSTALL_REQUEST = delete_data


def consume_uninstall_request() -> bool | None:
    global UNINSTALL_REQUEST
    with LOCK:
        request = UNINSTALL_REQUEST
        UNINSTALL_REQUEST = None
        return request


def load_state() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if not STATE_FILE.exists():
        return
    try:
        saved = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        if isinstance(saved.get("repos"), list):
            STATE["repos"] = saved["repos"]
        groups = saved.get("groups", [])
        STATE["groups"] = groups if isinstance(groups, list) else []
        if isinstance(saved.get("downloads"), dict):
            STATE["downloads"] = saved["downloads"]
        automation = saved.get("automation", {})
        if isinstance(automation, dict):
            legacy_hours = "interval_seconds" not in automation
            try:
                interval_seconds = (
                    int(automation.get("interval_hours", 24)) * 3600
                    if legacy_hours
                    else int(automation["interval_seconds"])
                )
            except (ValueError, TypeError):
                interval_seconds = 24 * 3600
            legacy_repo_intervals = automation.get("per_repo_intervals", {})
            repo_intervals = automation.get("per_repo_intervals_seconds", {})
            group_intervals = automation.get("per_group_intervals_seconds", {})
            if not isinstance(group_intervals, dict):
                group_intervals = {}
            if legacy_hours and isinstance(legacy_repo_intervals, dict):
                repo_intervals = {}
                for repo_id, hours in legacy_repo_intervals.items():
                    try:
                        hours = int(hours)
                    except (ValueError, TypeError):
                        continue
                    if 1 <= hours <= 720:
                        repo_intervals[repo_id] = hours * 3600
            if not isinstance(repo_intervals, dict):
                repo_intervals = {}
            STATE["automation"] = {
                "enabled": bool(automation.get("enabled", False)),
                "interval_seconds": max(1, min(MAX_AUTOMATION_SECONDS, interval_seconds)),
                "per_repo_intervals_seconds": {
                    str(repo_id): max(1, min(MAX_AUTOMATION_SECONDS, int(interval)))
                    for repo_id, interval in repo_intervals.items()
                    if str(interval).isdigit() and 1 <= int(interval) <= MAX_AUTOMATION_SECONDS
                },
                "per_group_intervals_seconds": {
                    str(group_id): max(1, min(MAX_AUTOMATION_SECONDS, int(interval)))
                    for group_id, interval in group_intervals.items()
                    if str(interval).isdigit()
                    and 1 <= int(interval) <= MAX_AUTOMATION_SECONDS
                },
            }
        settings = saved.get("settings", {})
        STATE["settings"] = {
            "logging_enabled": bool(
                settings.get("logging_enabled", False)
                if isinstance(settings, dict)
                else False
            ),
            "show_automation_banner": bool(
                settings.get("show_automation_banner", True)
                if isinstance(settings, dict)
                else True
            ),
            "keep_download_status_until_done": bool(
                settings.get("keep_download_status_until_done", False)
                if isinstance(settings, dict)
                else False
            ),
            "show_device_info_on_add": bool(
                settings.get("show_device_info_on_add", True)
                if isinstance(settings, dict)
                else True
            ),
            "grouping_enabled": bool(
                settings.get("grouping_enabled", True)
                if isinstance(settings, dict)
                else True
            ),
        }
        configure_logging(STATE["settings"]["logging_enabled"])
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


def resolve_public_https_target(url: str) -> tuple[urllib.parse.SplitResult, tuple[str, ...]]:
    parsed = urllib.parse.urlsplit(url)
    if (
        parsed.scheme.lower() != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
    ):
        raise ValueError("Repository requests must use a valid HTTPS URL.")
    try:
        port = parsed.port if parsed.port is not None else 443
    except ValueError as error:
        raise ValueError("Repository URL has an invalid port.") from error
    if port == 0:
        raise ValueError("Repository URL has an invalid port.")

    hostname = parsed.hostname.rstrip(".").lower()
    try:
        answers = socket.getaddrinfo(hostname, port, type=socket.SOCK_STREAM)
    except socket.gaierror as error:
        raise ValueError(f"Could not resolve repository host {hostname}: {error}") from error
    addresses = list(dict.fromkeys(answer[4][0] for answer in answers))
    if not addresses:
        raise ValueError(f"Repository host {hostname} did not resolve to an address.")
    try:
        parsed_addresses = [ipaddress.ip_address(address.split("%", 1)[0]) for address in addresses]
    except ValueError as error:
        raise ValueError(f"Repository host {hostname} resolved to an invalid address.") from error
    if any(not address.is_global for address in parsed_addresses):
        raise ValueError(
            "Repository hosts must resolve only to public IP addresses; local and private network targets are blocked."
        )
    return parsed, tuple(addresses)


class PinnedHTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, host: str, *, addresses: tuple[str, ...], **kwargs):
        self._resolved_addresses = addresses
        super().__init__(host, **kwargs)

    def connect(self):
        if self._tunnel_host:
            raise OSError("HTTPS proxy tunnels are disabled for repository requests.")
        last_error = None
        for address in self._resolved_addresses:
            try:
                sock = socket.create_connection(
                    (address, self.port),
                    self.timeout,
                    self.source_address,
                )
            except OSError as error:
                last_error = error
                continue
            try:
                self.sock = self._context.wrap_socket(sock, server_hostname=self.host)
            except OSError:
                sock.close()
                raise
            return
        if last_error:
            raise last_error
        raise OSError("No validated public address is available for the repository host.")


def open_public_response(
    url: str, headers: dict[str, str], timeout: int
) -> tuple[PinnedHTTPSConnection, http.client.HTTPResponse]:
    current_url = url
    original_origin = None
    for redirect_count in range(6):
        parsed, addresses = resolve_public_https_target(current_url)
        hostname = parsed.hostname.rstrip(".").lower()
        try:
            ipaddress.ip_address(hostname)
        except ValueError:
            hostname = hostname.encode("idna").decode("ascii")
        port = parsed.port if parsed.port is not None else 443
        origin = ("https", hostname, port)
        if original_origin is None:
            original_origin = origin
        elif origin != original_origin:
            raise urllib.error.HTTPError(
                current_url, 403, "Cross-origin repository redirects are blocked.", None, None
            )

        host_header = f"[{hostname}]" if ":" in hostname else hostname
        if port != 443:
            host_header += f":{port}"
        request_headers = dict(headers)
        request_headers["Host"] = host_header
        path = urllib.parse.urlunsplit(("", "", parsed.path or "/", parsed.query, ""))
        connection = PinnedHTTPSConnection(
            hostname,
            addresses=addresses,
            port=port,
            timeout=timeout,
            context=ssl.create_default_context(),
        )
        try:
            connection.request("GET", path, headers=request_headers)
            response = connection.getresponse()
        except (OSError, http.client.HTTPException) as error:
            connection.close()
            raise urllib.error.URLError(error) from error

        location = response.getheader("Location")
        if response.status in (301, 302, 303, 307, 308) and location:
            redirected_url = urllib.parse.urljoin(current_url, location)
            redirected = urllib.parse.urlsplit(redirected_url)
            redirected_host = (redirected.hostname or "").rstrip(".").lower()
            redirected_port = (
                redirected.port
                if redirected.port is not None
                else (443 if redirected.scheme == "https" else 80)
            )
            if (
                (redirected.scheme.lower(), redirected_host, redirected_port) != original_origin
                or redirected.username is not None
                or redirected.password is not None
            ):
                response.close()
                connection.close()
                raise urllib.error.HTTPError(
                    redirected_url,
                    403,
                    "Cross-origin repository redirects are blocked.",
                    response.headers,
                    None,
                )
            response.close()
            connection.close()
            current_url = redirected_url
            continue
        if response.status in (301, 302, 303, 307, 308) and redirect_count == 5:
            response.close()
            connection.close()
            raise urllib.error.HTTPError(
                current_url, 310, "Too many repository redirects.", None, None
            )
        return connection, response
    raise urllib.error.HTTPError(url, 310, "Too many repository redirects.", None, None)


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
    parsed_url = urllib.parse.urlsplit(url)
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

            connection = None
            try:
                connection, response = open_public_response(url, headers, timeout)
                if response.status >= 300:
                    raise urllib.error.HTTPError(
                        url,
                        response.status,
                        response.reason,
                        response.headers,
                        response,
                    )
            except urllib.error.HTTPError as error:
                if connection:
                    connection.close()
                retry_after = retry_after_seconds(
                    error.headers.get("Retry-After") if error.headers else None
                )
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
                connection.close()
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


def regenerate_generated_device_identifier() -> str:
    try:
        import keyring

        with DEVICE_ID_LOCK:
            value = os.urandom(20).hex()
            keyring.set_password(KEYRING_SERVICE, GENERATED_DEVICE_ID_ACCOUNT, value)
            return value
    except Exception as error:
        raise ValueError(
            "Could not update the generated archive ID in the system keyring. "
            "Unlock or enable your desktop keyring and try again."
        ) from error


def generate_device_compatibility_profile() -> dict[str, str]:
    profile = dict(secrets.choice(GENERATED_DEVICE_PROFILES))
    profile["generated_device_id"] = regenerate_generated_device_identifier()
    return profile


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


def repository_icon(repo: dict) -> tuple[bytes, str] | None:
    with LOCK:
        if repo["id"] in REPO_ICON_CACHE:
            return REPO_ICON_CACHE[repo["id"]]

    icon = None
    for filename in ("CydiaIcon.png", "Icon.png", "icon.png", "repo-icon.png"):
        url = urllib.parse.urljoin(repo["url"], filename)
        try:
            body = fetch_bytes(
                url,
                512 * 1024,
                headers=repository_request_headers(repo),
                cache_scope=repo["id"],
            )
        except urllib.error.HTTPError as error:
            if error.code in (404, 410):
                error.close()
                continue
            raise
        if body.startswith(b"\x89PNG\r\n\x1a\n"):
            content_type = "image/png"
        elif body.startswith(b"\xff\xd8\xff"):
            content_type = "image/jpeg"
        elif body.startswith((b"GIF87a", b"GIF89a")):
            content_type = "image/gif"
        elif body.startswith(b"RIFF") and body[8:12] == b"WEBP":
            content_type = "image/webp"
        else:
            continue
        icon = (body, content_type)
        break

    with LOCK:
        REPO_ICON_CACHE[repo["id"]] = icon
    return icon


def package_icon(repo: dict, package: dict) -> tuple[bytes, str] | None:
    identifier = package_id(repo["id"], package)
    icon_path = str(package.get("icon", "")).strip()
    cache_key = f"{identifier}:{icon_path}"
    with LOCK:
        if cache_key in PACKAGE_ICON_CACHE:
            return PACKAGE_ICON_CACHE[cache_key]

    icon = None
    if icon_path:
        parsed_path = urllib.parse.urlsplit(icon_path)
        if parsed_path.scheme:
            if parsed_path.scheme.lower() != "https" or parsed_path.fragment:
                return None
            icon_url = icon_path
        else:
            icon_path = urllib.parse.quote(icon_path.lstrip("./"), safe="/-._~")
            icon_url = urllib.parse.urljoin(repo["url"], icon_path)
        parsed_icon = urllib.parse.urlsplit(icon_url)
        parsed_repo = urllib.parse.urlsplit(repo["url"])
        same_origin = (
            parsed_icon.hostname == parsed_repo.hostname
            and (parsed_icon.port or 443) == (parsed_repo.port or 443)
        )
        if (
            parsed_icon.scheme.lower() == "https"
            and parsed_icon.username is None
            and parsed_icon.password is None
            and not parsed_icon.fragment
        ):
            try:
                body = fetch_bytes(
                    icon_url,
                    512 * 1024,
                    headers=repository_request_headers(repo) if same_origin else None,
                    cache_scope=repo["id"],
                )
            except urllib.error.HTTPError as error:
                if error.code not in (404, 410):
                    raise
                error.close()
            else:
                if body.startswith(b"\x89PNG\r\n\x1a\n"):
                    icon = (body, "image/png")
                elif body.startswith(b"\xff\xd8\xff"):
                    icon = (body, "image/jpeg")
                elif body.startswith((b"GIF87a", b"GIF89a")):
                    icon = (body, "image/gif")
                elif body.startswith(b"RIFF") and body[8:12] == b"WEBP":
                    icon = (body, "image/webp")

    with LOCK:
        PACKAGE_ICON_CACHE[cache_key] = icon
    return icon


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
    repo: dict | None = None,
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

    if repo:
        display_name = fields.get("Origin") or fields.get("Label")
        if display_name and len(display_name) <= 120 and not any(ord(char) < 32 for char in display_name):
            repo["name"] = display_name
            repo["display_name"] = display_name

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
                "display_name": fields.get("Name", "").strip(),
                "version": fields.get("Version", ""),
                "architecture": fields.get("Architecture", "all"),
                "description": fields.get("Description", "").splitlines()[0]
                if fields.get("Description") else "",
                "filename": filename,
                "icon": fields.get("Icon", "").strip(),
                "size": fields.get("Size", ""),
                "sha256": fields.get("SHA256", ""),
            }
            entries.append(package)
    return entries


def refresh_repo(repo: dict) -> int:
    require_https_repository(repo["url"])
    errors = []
    auth_headers = repository_request_headers(repo)
    for index_path in release_index_paths(repo["url"], auth_headers, repo["id"], repo):
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


def select_download_refs(selection: dict) -> list[tuple[str, str]]:
    if selection.get("mode") != "visible":
        raise ValueError("Download selection mode is invalid.")
    repo_id = selection.get("repo_id")
    if repo_id is not None and not isinstance(repo_id, str):
        raise ValueError("Selected repository is invalid.")
    package_filter = selection.get("filter", "all")
    if package_filter not in ("all", "not-downloaded", "downloaded"):
        raise ValueError("Package filter is invalid.")
    search = selection.get("search", "")
    if not isinstance(search, str) or len(search) > 256:
        raise ValueError("Package search must be 256 characters or fewer.")
    search = search.casefold()
    group_ids = selection.get("group_ids", [])
    if not isinstance(group_ids, list) or any(not isinstance(group_id, str) for group_id in group_ids):
        raise ValueError("Selected groups are invalid.")

    with LOCK:
        known_groups = {group["id"] for group in STATE.get("groups", [])}
        if any(group_id not in known_groups for group_id in group_ids):
            raise ValueError("A selected group no longer exists.")
        selected_repo_ids = {
            repo["id"] for repo in STATE["repos"]
            if any(
                group_id in group_ids
                for group_id in repository_group_ancestors(repo.get("group_id"), STATE.get("groups", []))
            )
        } if group_ids else None
        repos = [
            repo for repo in STATE["repos"]
            if (repo_id is None or repo["id"] == repo_id)
            and (selected_repo_ids is None or repo["id"] in selected_repo_ids)
        ]
        refs = []
        for repo in repos:
            for package in repo.get("packages", []):
                identifier = package_id(repo["id"], package)
                downloaded = (
                    STATE["downloads"].get(identifier, {}).get("version")
                    == package["version"]
                )
                if downloaded or package_filter == "downloaded":
                    continue
                if search and search not in " ".join(
                    str(value) for value in (
                        package.get("name", ""),
                        package.get("display_name", ""),
                        package.get("description", ""),
                        repo.get("name", ""),
                        package.get("version", ""),
                    )
                ).casefold():
                    continue
                refs.append((repo["id"], identifier))
        return refs


def repository_group_ancestors(group_id: str | None, groups: list[dict]) -> set[str]:
    by_id = {group["id"]: group for group in groups}
    ancestors = set()
    current_id = group_id
    while current_id and current_id in by_id and current_id not in ancestors:
        ancestors.add(current_id)
        current_id = by_id[current_id].get("parent_id")
    return ancestors


def group_descendant_ids(group_id: str, groups: list[dict]) -> set[str]:
    descendants = {group_id}
    while True:
        children = {
            group["id"] for group in groups
            if group.get("parent_id") in descendants and group["id"] not in descendants
        }
        if not children:
            return descendants
        descendants.update(children)


def automation_interval_for_repo(settings: dict, repo_id: str, repo: dict | None = None) -> int:
    source_overrides = settings.get("per_repo_intervals_seconds", {})
    if repo_id in source_overrides:
        return source_overrides[repo_id]
    groups = STATE.get("groups", []) if STATE.get("settings", {}).get("grouping_enabled", True) else []
    group_overrides = settings.get("per_group_intervals_seconds", {})
    if repo is None:
        repo = next((item for item in STATE["repos"] if item["id"] == repo_id), {})
    groups_by_id = {group["id"]: group for group in groups}
    group_id = repo.get("group_id")
    visited = set()
    while group_id and group_id in groups_by_id and group_id not in visited:
        visited.add(group_id)
        if group_id in group_overrides:
            return group_overrides[group_id]
        group_id = groups_by_id[group_id].get("parent_id")
    return settings.get("interval_seconds", settings.get("interval_hours", 24) * 3600)


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
                item["bundle_id"] = package["name"]
                item["name"] = package.get("display_name") or package["name"]
                item["downloaded"] = (
                    STATE["downloads"].get(item["id"], {}).get("version") == item["version"]
                )
                packages.append(item)
            repos.append({
                "id": repo["id"],
                "name": repo_display_name(repo),
                "url": repo["url"],
                "refreshed_at": repo.get("refreshed_at"),
                "auth_method": repo.get("auth", {}).get("method", "none"),
                "device_profile": dict(repo.get("device_profile", {"enabled": False})),
                "group_id": repo.get("group_id"),
                "packages": packages,
            })
        return {
            "repos": repos,
            "groups": [
                {"id": group["id"], "name": group["name"], "parent_id": group.get("parent_id")}
                for group in STATE.get("groups", [])
            ],
            "automation": dict(STATE["automation"]),
            "settings": dict(
                STATE.get(
                    "settings",
                    {
                        "logging_enabled": False,
                        "show_automation_banner": True,
                        "keep_download_status_until_done": False,
                        "show_device_info_on_add": True,
                        "grouping_enabled": True,
                    },
                )
            ),
            "download_dir": str(DOWNLOAD_DIR),
            "packaged_install": PACKAGED_INSTALL,
        }


def repo_display_name(repo: dict) -> str:
    name = str(repo.get("display_name") or repo.get("name") or "").strip()
    parsed = urllib.parse.urlsplit(repo.get("url", ""))
    url_label = parsed.netloc + parsed.path.rstrip("/")
    if name and name != url_label:
        return name
    return parsed.hostname or name or "Repository"


class DownloadCancelled(Exception):
    pass


def wait_for_download_control(job: dict) -> None:
    if "_resume_event" not in job or "_cancel_event" not in job:
        return
    resume_event = job["_resume_event"]
    cancel_event = job["_cancel_event"]
    while not resume_event.wait(timeout=0.25):
        if cancel_event.is_set():
            raise DownloadCancelled("Download cancelled.")
    if cancel_event.is_set():
        raise DownloadCancelled("Download cancelled.")


def download_package(repo: dict, package: dict, job: dict) -> None:
    repo_url = require_https_repository(repo["url"])
    identifier = package_id(repo["id"], package)
    existing = STATE["downloads"].get(identifier, {})
    if existing.get("version") == package["version"] and Path(existing.get("path", "")).is_file():
        return

    filename = package["filename"]
    encoded_filename = urllib.parse.quote(filename.lstrip("./"), safe="/-._~")
    package_url = urllib.parse.urljoin(repo["url"], encoded_filename)
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
    declared_size = package.get("size", "")
    current_size = int(declared_size) if declared_size.isdigit() else 0
    try:
        with polite_response(package_url, headers, timeout=45) as response, temporary.open("wb") as output:
            if not current_size:
                response_size = response.headers.get("Content-Length", "")
                current_size = int(response_size) if response_size.isdigit() else 0
            with LOCK:
                job["current_size"] = current_size
            while True:
                wait_for_download_control(job)
                chunk = response.read1(64 * 1024) if hasattr(response, "read1") else response.read(64 * 1024)
                if not chunk:
                    break
                received += len(chunk)
                if received > MAX_PACKAGE_SIZE:
                    raise ValueError(f"Package exceeds the {MAX_PACKAGE_SIZE // (1024 * 1024)} MB limit")
                output.write(chunk)
                digest.update(chunk)
                with LOCK:
                    job["current_bytes"] = received
                    job["downloaded_bytes"] = job.get("downloaded_bytes", 0) + len(chunk)
        wait_for_download_control(job)
        expected_size = declared_size
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
                "repo_id": repo["id"],
            }
            save_state()
    finally:
        if temporary.exists():
            temporary.unlink()


def start_download_job(package_refs: list[tuple[str, str]], automatic: bool = False) -> str:
    job_id = uuid.uuid4().hex
    items = []
    with LOCK:
        packages_by_id = {
            package_id(repo["id"], package): package
            for repo in STATE["repos"]
            for package in repo.get("packages", [])
        }
        for repo_id, identifier in package_refs:
            package = packages_by_id.get(identifier)
            items.append({
                "id": identifier,
                "repo_id": repo_id,
                "package_id": identifier,
                "name": package.get("name", "Unknown package") if package else "Unknown package",
                "status": "pending",
                "error": "",
            })
        job = {
            "id": job_id,
            "status": "queued",
            "total": len(items),
            "item_count": len(items),
            "completed": 0,
            "errors": [],
            "automatic": automatic,
            "source_ids": list(dict.fromkeys(repo_id for repo_id, _ in package_refs)),
            "started_at": time.time(),
            "current_package": "",
            "current_item_id": None,
            "current_bytes": 0,
            "current_size": 0,
            "current_started_at": 0,
            "downloaded_bytes": 0,
            "total_bytes": sum(
                int(packages_by_id[identifier]["size"])
                for _, identifier in package_refs
                if identifier in packages_by_id
                and str(packages_by_id[identifier].get("size", "")).isdigit()
                and int(packages_by_id[identifier]["size"]) > 0
            ),
            "size_known_count": sum(
                1 for _, identifier in package_refs
                if identifier in packages_by_id
                and str(packages_by_id[identifier].get("size", "")).isdigit()
                and int(packages_by_id[identifier]["size"]) > 0
            ),
            "transfer_started_at": 0,
            "items": items,
            "_resume_event": threading.Event(),
            "_cancel_event": threading.Event(),
            "_packages_by_id": packages_by_id,
        }
        job["_resume_event"].set()
        JOBS[job_id] = job

        def run() -> None:
            try:
                with LOCK:
                    if job["status"] == "queued":
                        job["status"] = "running"
                for item in job["items"]:
                    wait_for_download_control(job)
                    with LOCK:
                        if job["_cancel_event"].is_set():
                            break
                        if item["status"] == "removed":
                            continue
                        repo = next(
                            (candidate for candidate in STATE["repos"]
                             if candidate["id"] == item["repo_id"]),
                            None,
                        )
                        package = job["_packages_by_id"].get(item["package_id"])
                        if package:
                            item["status"] = "downloading"
                            job["current_package"] = item["name"]
                            job["current_item_id"] = item["id"]
                            job["current_bytes"] = 0
                            job["current_size"] = (
                                int(package["size"])
                                if str(package.get("size", "")).isdigit()
                                else 0
                            )
                            job["current_started_at"] = time.time()
                            if not job["transfer_started_at"]:
                                job["transfer_started_at"] = job["current_started_at"]
                    if not repo or not package:
                        with LOCK:
                            item["status"] = "failed"
                            item["error"] = "A package or repository was removed before download."
                            job["errors"].append(f"{item['name']}: {item['error']}")
                    else:
                        try:
                            download_package(repo, package, job)
                        except DownloadCancelled:
                            with LOCK:
                                item["status"] = "cancelled"
                            break
                        except Exception as error:
                            with LOCK:
                                item["status"] = "failed"
                                item["error"] = str(error)
                                job["errors"].append(f"{item['name']}: {error}")
                        else:
                            with LOCK:
                                item["status"] = "completed"
                    with LOCK:
                        if item["status"] in ("completed", "failed"):
                            job["completed"] += 1
                        job["current_package"] = ""
                        job["current_item_id"] = None
                        job["current_bytes"] = 0
                        job["current_size"] = 0
                        job["current_started_at"] = 0
                with LOCK:
                    job["status"] = "cancelled" if job["_cancel_event"].is_set() else "completed"
                    job["finished_at"] = time.time()
                    job["_packages_by_id"].clear()
            except DownloadCancelled:
                with LOCK:
                    job["status"] = "cancelled"
                    job["finished_at"] = time.time()
                    job["_packages_by_id"].clear()
            except Exception as error:
                with LOCK:
                    job["status"] = "failed"
                    job["errors"].append(str(error))
                    job["finished_at"] = time.time()
                    job["_packages_by_id"].clear()
                log_exception("Package download job %s failed.", job_id)

    threading.Thread(target=run, daemon=True, name=f"download-{job_id[:8]}").start()
    return job_id


def public_download_job(job: dict, item_offset: int = 0, item_limit: int = 100) -> dict:
    with LOCK:
        result = {
            key: value
            for key, value in job.items()
            if not key.startswith("_") and key not in ("items", "errors")
        }
        result["errors"] = list(job["errors"][:100])
        result["error_count"] = len(job["errors"])
        result["item_offset"] = item_offset
        result["items"] = []
        if item_limit:
            visible_index = 0
            for item in job["items"]:
                if item["status"] == "removed":
                    continue
                if visible_index >= item_offset:
                    result["items"].append({
                        key: item[key]
                        for key in ("id", "name", "status", "error")
                    })
                    if len(result["items"]) >= item_limit:
                        break
                visible_index += 1
        return result


def public_failed_items(job: dict, item_offset: int = 0, item_limit: int = 50) -> dict:
    with LOCK:
        failed = [item for item in job["items"] if item["status"] == "failed"]
        return {
            "total": len(failed),
            "offset": item_offset,
            "items": [
                {
                    key: item[key]
                    for key in ("id", "name", "error")
                }
                for item in failed[item_offset:item_offset + item_limit]
            ],
        }


def retry_failed_downloads(job_id: str, item_id: str | None = None) -> dict:
    with LOCK:
        job = JOBS.get(job_id)
        if not job:
            raise ValueError("Download queue item was not found.")
        if job["status"] in ("queued", "running", "paused", "cancelling"):
            raise ValueError("Wait for the current queue to finish before retrying failures.")
        failed_items = [
            item for item in job["items"]
            if item["status"] == "failed" and (item_id is None or item["id"] == item_id)
        ]
        if item_id is not None and not failed_items:
            raise ValueError("That failed package is no longer available to retry.")
        refs = [(item["repo_id"], item["package_id"]) for item in failed_items]
        automatic = job["automatic"]
    if not refs:
        raise ValueError("There are no failed packages to retry.")
    retry_job_id = start_download_job(refs, automatic=automatic)
    return {"job_id": retry_job_id, "retry_count": len(refs)}


def control_download_job(job_id: str, action: str, item_id: str | None = None) -> dict:
    with LOCK:
        job = JOBS.get(job_id)
        if not job:
            raise ValueError("Download queue item was not found.")
        if action == "pause":
            if job["status"] not in ("queued", "running", "paused"):
                raise ValueError("This download is no longer active.")
            job["_resume_event"].clear()
            job["status"] = "paused"
        elif action == "resume":
            if job["status"] != "paused":
                raise ValueError("This download is not paused.")
            job["status"] = "running"
            job["_resume_event"].set()
        elif action == "cancel":
            if job["status"] not in ("queued", "running", "paused"):
                raise ValueError("This download is no longer active.")
            job["_cancel_event"].set()
            job["_resume_event"].set()
            job["status"] = "cancelling"
        elif action == "remove":
            if not item_id:
                raise ValueError("Queue item ID is required.")
            item = next((entry for entry in job["items"] if entry["id"] == item_id), None)
            if not item:
                raise ValueError("Queue item was not found.")
            if item["status"] != "pending":
                raise ValueError("Only pending packages can be removed from the queue.")
            package = job["_packages_by_id"].get(item["package_id"])
            if package and str(package.get("size", "")).isdigit() and int(package["size"]) > 0:
                job["total_bytes"] = max(0, job["total_bytes"] - int(package["size"]))
                job["size_known_count"] = max(0, job["size_known_count"] - 1)
            item["status"] = "removed"
            job["total"] -= 1
            job["item_count"] -= 1
            if job["item_count"] == 0 and not job["current_item_id"]:
                job["status"] = "completed"
                job["finished_at"] = time.time()
                job["_packages_by_id"].clear()
                job["_resume_event"].set()
        else:
            raise ValueError("Unknown download queue action.")
        return public_download_job(job)


def automation_loop() -> None:
    next_runs: dict[str, float] = {}
    previous_intervals: dict[str, int] = {}
    previous_enabled = False
    while True:
        time.sleep(1)
        with LOCK:
            settings = dict(STATE["automation"])
            repos = list(STATE["repos"])
        now = time.time()
        if not settings["enabled"]:
            next_runs.clear()
            previous_intervals.clear()
            previous_enabled = False
            continue
        intervals = {
            repo["id"]: automation_interval_for_repo(settings, repo["id"], repo)
            for repo in repos
        }
        for repo_id, interval in intervals.items():
            if not previous_enabled or previous_intervals.get(repo_id) != interval:
                next_runs[repo_id] = now + interval
        for repo_id in set(next_runs) - set(intervals):
            next_runs.pop(repo_id, None)
        previous_intervals = intervals
        previous_enabled = True
        due_ids = {
            repo_id for repo_id, due_at in next_runs.items() if now >= due_at
        }
        if due_ids:
            due_repos = [repo for repo in repos if repo["id"] in due_ids]
            for repo_id in due_ids:
                next_runs[repo_id] = now + intervals[repo_id]
            errors, failed_ids = refresh_repositories(due_repos)
            for error in errors:
                print(f"Automation could not refresh a source: {error}")
                APP_LOGGER.warning("Automation could not refresh a source: %s", error)
            try:
                with LOCK:
                    save_state()
                    refs = [
                        (repo["id"], package_id(repo["id"], package))
                        for repo in STATE["repos"]
                        if repo["id"] in due_ids and repo["id"] not in failed_ids
                        for package in repo.get("packages", [])
                        if STATE["downloads"].get(package_id(repo["id"], package), {}).get("version")
                        != package["version"]
                    ]
                if refs:
                    start_download_job(refs, automatic=True)
            except Exception:
                log_exception("Automatic package download failed.")


class Handler(BaseHTTPRequestHandler):
    server_version = "RepoShelf/1.0"

    def log_message(self, format_string: str, *args) -> None:
        message = f"[{self.log_date_time_string()}] {format_string % args}"
        print(message)
        APP_LOGGER.info(message)

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
        origin = self.headers.get("Origin")
        top_level_navigation = (
            self.command == "GET"
            and self.path in ("/", "/index.html")
            and fetch_site == "none"
            and origin is None
        )
        if fetch_site and fetch_site != "same-origin" and not top_level_navigation:
            return False
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
        parsed_path = urllib.parse.urlsplit(self.path)
        path = parsed_path.path
        if path == "/api/state":
            self.send_json(public_state())
        elif path == "/api/jobs":
            query = urllib.parse.parse_qs(parsed_path.query)
            try:
                item_offset = max(0, int(query.get("offset", ["0"])[0]))
                item_limit = min(200, max(0, int(query.get("limit", ["100"])[0])))
            except (TypeError, ValueError):
                self.send_json({"error": "Invalid queue page."}, 400)
                return
            with LOCK:
                jobs = sorted(JOBS.values(), key=lambda item: item["started_at"], reverse=True)
            self.send_json({
                "jobs": [
                    public_download_job(job, item_offset, item_limit)
                    for job in jobs[:30]
                ]
            })
        elif path.startswith("/api/jobs/") and path.endswith("/failures"):
            job_id = path.split("/")[-2]
            query = urllib.parse.parse_qs(parsed_path.query)
            try:
                item_offset = max(0, int(query.get("offset", ["0"])[0]))
                item_limit = min(100, max(1, int(query.get("limit", ["50"])[0])))
            except (TypeError, ValueError):
                self.send_json({"error": "Invalid failed-package page."}, 400)
                return
            with LOCK:
                job = JOBS.get(job_id)
            if not job:
                self.send_json({"error": "Job not found"}, 404)
                return
            self.send_json(public_failed_items(job, item_offset, item_limit))
        elif path.startswith("/api/repos/") and path.endswith("/icon"):
            repo_id = path.split("/")[-2]
            with LOCK:
                repo = next((item for item in STATE["repos"] if item["id"] == repo_id), None)
            if not repo:
                self.send_json({"error": "Repository not found."}, 404)
                return
            try:
                icon = repository_icon(repo)
            except (OSError, TimeoutError, urllib.error.URLError, ValueError) as error:
                APP_LOGGER.info("Could not load source icon for %s: %s", repo["name"], error)
                icon = None
            if not icon:
                self.send_json({"error": "No source icon is available."}, 404)
                return
            body, content_type = icon
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "public, max-age=86400")
            self.end_headers()
            self.wfile.write(body)
        elif path.startswith("/api/packages/") and path.endswith("/icon"):
            identifier = path.split("/")[-2]
            with LOCK:
                match = next(
                    (
                        (repo, package)
                        for repo in STATE["repos"]
                        for package in repo.get("packages", [])
                        if package_id(repo["id"], package) == identifier
                    ),
                    None,
                )
            if not match:
                self.send_json({"error": "Package not found."}, 404)
                return
            try:
                icon = package_icon(*match)
            except (OSError, TimeoutError, urllib.error.URLError, ValueError) as error:
                APP_LOGGER.info("Could not load package icon for %s: %s", match[1]["name"], error)
                icon = None
            if not icon:
                self.send_json({"error": "No package icon is available."}, 404)
                return
            body, content_type = icon
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "public, max-age=86400")
            self.end_headers()
            self.wfile.write(body)
        elif path.startswith("/api/jobs/"):
            job_id = path.rsplit("/", 1)[-1]
            query = urllib.parse.parse_qs(parsed_path.query)
            try:
                item_offset = max(0, int(query.get("offset", ["0"])[0]))
                item_limit = min(200, max(1, int(query.get("limit", ["100"])[0])))
            except (TypeError, ValueError):
                self.send_json({"error": "Invalid queue page."}, 400)
                return
            with LOCK:
                job = JOBS.get(job_id)
            self.send_json(
                public_download_job(job, item_offset, item_limit) if job else {"error": "Job not found"},
                200 if job else 404,
            )
        elif path == "/api/device-profile":
            try:
                self.send_json({"generated_device_id": generated_device_identifier()})
            except ValueError as error:
                self.send_json({"error": str(error)}, 503)
        elif path == "/api/device-profile/generate":
            try:
                self.send_json(generate_device_compatibility_profile())
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
            self.send_header("Cache-Control", "no-store")
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
            if path == "/api/device-profile/regenerate":
                try:
                    identifier = regenerate_generated_device_identifier()
                except ValueError as error:
                    self.send_json({"error": str(error)}, 503)
                    return
                APP_LOGGER.info("Generated archive device ID was replaced.")
                self.send_json({"generated_device_id": identifier})
            elif path == "/api/settings":
                updates = {}
                for key in (
                    "logging_enabled",
                    "show_automation_banner",
                    "keep_download_status_until_done",
                    "show_device_info_on_add",
                    "grouping_enabled",
                ):
                    if key not in data:
                        continue
                    if not isinstance(data[key], bool):
                        label = {
                            "logging_enabled": "Logging",
                            "show_automation_banner": "Automation banner",
                            "keep_download_status_until_done": "Download status",
                            "show_device_info_on_add": "Device information visibility",
                            "grouping_enabled": "Source grouping",
                        }[key]
                        raise ValueError(f"{label} setting must be enabled or disabled.")
                    updates[key] = data[key]
                if not updates:
                    raise ValueError("Provide at least one application setting.")
                try:
                    update_settings(**updates)
                except OSError as error:
                    self.send_json({"error": f"Could not save application settings: {error}"}, 500)
                    return
                self.send_json({"settings": dict(STATE["settings"])})
            elif path == "/api/uninstall":
                if PACKAGED_INSTALL:
                    raise ValueError("Remove RepoShelf with your system package manager.")
                delete_data = data.get("delete_data")
                if not isinstance(delete_data, bool):
                    raise ValueError("Choose whether to delete saved data.")
                request_uninstall(delete_data)
                self.send_json({"accepted": True}, 202)
            elif path.startswith("/api/jobs/") and path.endswith("/retry"):
                job_id = path.split("/")[-2]
                item_id = str(data.get("item_id", "")) or None
                self.send_json(retry_failed_downloads(job_id, item_id), 202)
            elif path.startswith("/api/jobs/") and path.endswith("/control"):
                job_id = path.split("/")[-2]
                result = control_download_job(
                    job_id,
                    str(data.get("action", "")),
                    str(data.get("item_id", "")) or None,
                )
                self.send_json(result)
            elif path == "/api/repos":
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
                    "name": parsed.hostname or parsed.netloc,
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
            elif path == "/api/groups":
                action = data.get("action")
                name = data.get("name")
                if not isinstance(name, str) or not name.strip() or len(name.strip()) > 80:
                    raise ValueError("Group names must contain 1 to 80 characters.")
                name = name.strip()
                parent_id = data.get("parent_id") or None
                with LOCK:
                    groups = STATE.setdefault("groups", [])
                    known_ids = {group["id"] for group in groups}
                    if parent_id is not None and (
                        not isinstance(parent_id, str) or parent_id not in known_ids
                    ):
                        raise ValueError("Parent group was not found.")
                    if action == "create":
                        group = {"id": uuid.uuid4().hex, "name": name, "parent_id": parent_id}
                        groups.append(group)
                    elif action == "update":
                        group_id = data.get("group_id")
                        group = next((item for item in groups if item["id"] == group_id), None)
                        if not group:
                            raise ValueError("Group was not found.")
                        if parent_id == group_id or (
                            parent_id and parent_id in group_descendant_ids(group_id, groups)
                        ):
                            raise ValueError("A group cannot be moved inside itself or one of its child groups.")
                        previous_group = dict(group)
                        group.update(name=name, parent_id=parent_id)
                    else:
                        raise ValueError("Choose whether to create or update a group.")
                    try:
                        save_state()
                    except OSError:
                        if action == "create":
                            groups.remove(group)
                        else:
                            group.clear()
                            group.update(previous_group)
                            raise
                        raise
                self.send_json({"state": public_state(), "group_id": group["id"]}, 201 if action == "create" else 200)
            elif path.startswith("/api/repos/") and path.endswith("/group"):
                repo_id = path.split("/")[-2]
                group_id = data.get("group_id") or None
                with LOCK:
                    repo = next((item for item in STATE["repos"] if item["id"] == repo_id), None)
                    if not repo:
                        raise ValueError("Repository not found.")
                    if group_id is not None and not any(
                        group["id"] == group_id for group in STATE.get("groups", [])
                    ):
                        raise ValueError("Group was not found.")
                    previous_group_id = repo.get("group_id")
                    repo["group_id"] = group_id
                    try:
                        save_state()
                    except OSError:
                        repo["group_id"] = previous_group_id
                        raise
                self.send_json({"state": public_state()})
            elif path == "/api/downloads":
                if "selection" in data:
                    selection = data["selection"]
                    if not isinstance(selection, dict):
                        raise ValueError("selection must be an object.")
                    refs = select_download_refs(selection)
                else:
                    requested = data.get("package_ids")
                    if not isinstance(requested, list):
                        raise ValueError("package_ids must be a list.")
                    with LOCK:
                        requested_ids = set(requested)
                        refs = [
                            (repo["id"], package_id(repo["id"], package))
                            for repo in STATE["repos"]
                            for package in repo.get("packages", [])
                            if package_id(repo["id"], package) in requested_ids
                            and STATE["downloads"].get(
                                package_id(repo["id"], package), {}
                            ).get("version") != package["version"]
                        ]
                job_id = start_download_job(refs)
                self.send_json({"job_id": job_id, "total": len(refs)}, 202)
            elif path == "/api/automation":
                enabled = bool(data.get("enabled", False))
                if "interval_seconds" in data:
                    interval = data["interval_seconds"]
                else:
                    legacy_interval = data.get("interval_hours", 24)
                    if not isinstance(legacy_interval, int) or isinstance(legacy_interval, bool):
                        raise ValueError("Interval must be a whole number of hours.")
                    interval = legacy_interval * 3600
                if not isinstance(interval, int) or isinstance(interval, bool):
                    raise ValueError("Interval must be a whole number of seconds.")
                if interval < 1 or interval > MAX_AUTOMATION_SECONDS:
                    raise ValueError("Interval must be between 1 second and 99 years.")
                if "per_repo_intervals_seconds" in data:
                    per_repo_intervals = data["per_repo_intervals_seconds"]
                    legacy_intervals = False
                else:
                    per_repo_intervals = data.get("per_repo_intervals", {})
                    legacy_intervals = True
                if not isinstance(per_repo_intervals, dict):
                    raise ValueError("Per-source intervals must be an object.")
                per_group_intervals = data.get("per_group_intervals_seconds", {})
                if not isinstance(per_group_intervals, dict):
                    raise ValueError("Per-group intervals must be an object.")
                with LOCK:
                    repo_ids = {repo["id"] for repo in STATE["repos"]}
                    group_ids = {group["id"] for group in STATE.get("groups", [])}
                    validated_intervals = {}
                    for repo_id, value in per_repo_intervals.items():
                        if repo_id not in repo_ids:
                            raise ValueError(f"Unknown repository in schedule: {repo_id}")
                        if not isinstance(value, int) or isinstance(value, bool):
                            raise ValueError("Per-source intervals must be whole numbers.")
                        repo_interval = value * 3600 if legacy_intervals else value
                        if repo_interval < 1 or repo_interval > MAX_AUTOMATION_SECONDS:
                            raise ValueError("Per-source intervals must be between 1 second and 99 years.")
                        validated_intervals[repo_id] = repo_interval
                    validated_group_intervals = {}
                    for group_id, value in per_group_intervals.items():
                        if group_id not in group_ids:
                            raise ValueError(f"Unknown group in schedule: {group_id}")
                        if not isinstance(value, int) or isinstance(value, bool):
                            raise ValueError("Per-group intervals must be whole numbers.")
                        if value < 1 or value > MAX_AUTOMATION_SECONDS:
                            raise ValueError("Per-group intervals must be between 1 second and 99 years.")
                        validated_group_intervals[group_id] = value
                    previous_automation = STATE["automation"]
                    STATE["automation"] = {
                        "enabled": enabled,
                        "interval_seconds": interval,
                        "per_repo_intervals_seconds": validated_intervals,
                        "per_group_intervals_seconds": validated_group_intervals,
                    }
                    try:
                        save_state()
                    except OSError:
                        STATE["automation"] = previous_automation
                        raise
                self.send_json({
                    "automation": {
                        **STATE["automation"],
                        "per_repo_intervals_seconds": dict(STATE["automation"]["per_repo_intervals_seconds"]),
                        "per_group_intervals_seconds": dict(STATE["automation"]["per_group_intervals_seconds"]),
                    }
                })
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
            log_exception("Request failed.")
            self.send_json({"error": str(error)}, 500)

    def do_DELETE(self) -> None:
        if self.reject_untrusted_origin():
            return
        path = urllib.parse.urlparse(self.path).path
        if path.startswith("/api/groups/"):
            group_id = path.rsplit("/", 1)[-1]
            with LOCK:
                groups = STATE.get("groups", [])
                group = next((item for item in groups if item["id"] == group_id), None)
                if not group:
                    self.send_json({"error": "Group not found."}, 404)
                    return
                parent_id = group.get("parent_id")
                children = [item for item in groups if item.get("parent_id") == group_id]
                affected_repos = [repo for repo in STATE["repos"] if repo.get("group_id") == group_id]
                automation = STATE.get("automation", {})
                group_intervals = automation.get("per_group_intervals_seconds", {})
                previous_interval = group_intervals.pop(group_id, None)
                for child in children:
                    child["parent_id"] = parent_id
                for repo in affected_repos:
                    repo["group_id"] = parent_id
                groups.remove(group)
                try:
                    save_state()
                except OSError as error:
                    groups.append(group)
                    for child in children:
                        child["parent_id"] = group_id
                    for repo in affected_repos:
                        repo["group_id"] = group_id
                    if previous_interval is not None:
                        group_intervals[group_id] = previous_interval
                    self.send_json({"error": f"Could not save group removal: {error}"}, 500)
                    return
            self.send_json({"state": public_state()})
            return
        if not path.startswith("/api/repos/"):
            self.send_json({"error": "Not found"}, 404)
            return
        repo_id = path.rsplit("/", 1)[-1]
        try:
            data = self.read_json()
            delete_downloads = data.get("delete_downloads", False)
            if not isinstance(delete_downloads, bool):
                raise ValueError("Choose whether to delete this source's downloaded files.")
        except (ValueError, json.JSONDecodeError) as error:
            self.send_json({"error": str(error)}, 400)
            return
        with LOCK:
            repo = next((item for item in STATE["repos"] if item["id"] == repo_id), None)
            if not repo:
                self.send_json({"error": "Repository not found"}, 404)
                return
            active_jobs = [
                job for job in JOBS.values()
                if job["status"] in ("queued", "running", "paused", "cancelling")
                and any(item["repo_id"] == repo_id and item["status"] in ("pending", "downloading")
                        for item in job["items"])
            ]
            if active_jobs:
                self.send_json(
                    {"error": "This source has downloads in progress. Cancel or finish them before removing the source."},
                    409,
                )
                return
            package_ids = {
                package_id(repo_id, package)
                for package in repo.get("packages", [])
            }
            download_ids = {
                identifier for identifier, item in STATE["downloads"].items()
                if item.get("repo_id") == repo_id or identifier in package_ids
            }
            downloads_to_delete = [
                STATE["downloads"][identifier]
                for identifier in download_ids
                if identifier in STATE["downloads"]
            ]
            deleted_download_count = 0
            if delete_downloads:
                download_root = DOWNLOAD_DIR.resolve()
                paths = []
                for item in downloads_to_delete:
                    saved_path = item.get("path")
                    if not saved_path:
                        continue
                    file_path = Path(saved_path)
                    resolved_path = file_path.resolve()
                    if not resolved_path.is_relative_to(download_root):
                        self.send_json(
                            {"error": "A saved package path is outside RepoShelf's downloads folder; no files were deleted."},
                            400,
                        )
                        return
                    paths.append(file_path.absolute())
                try:
                    for file_path in paths:
                        if file_path.is_file():
                            deleted_download_count += 1
                        file_path.unlink(missing_ok=True)
                except OSError as error:
                    self.send_json({"error": f"Could not delete a downloaded package: {error}"}, 500)
                    return
                for identifier in download_ids:
                    STATE["downloads"].pop(identifier, None)
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
            STATE.get("automation", {}).get("per_repo_intervals_seconds", {}).pop(repo_id, None)
            try:
                save_state()
            except OSError as error:
                self.send_json({"error": f"Could not save source removal: {error}"}, 500)
                return
        self.send_json({
            "state": public_state(),
            "deleted_downloads": deleted_download_count,
        })


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
