#!/usr/bin/env python3
"""Local web app for browsing and downloading packages from jailbreak repos."""

from __future__ import annotations

import bz2
import gzip
import hashlib
import json
import lzma
import os
import re
import threading
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "data"
DOWNLOAD_DIR = DATA_DIR / "downloads"
STATE_FILE = DATA_DIR / "state.json"
MAX_INDEX_SIZE = 60 * 1024 * 1024
MAX_PACKAGE_SIZE = 1024 * 1024 * 1024
USER_AGENT = "RepoShelf/1.0 (local package manager)"
LOCK = threading.RLock()
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


def fetch_bytes(url: str, limit: int) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=25) as response:
        chunks = bytearray()
        while True:
            chunk = response.read(min(1024 * 1024, limit + 1 - len(chunks)))
            if not chunk:
                break
            chunks.extend(chunk)
            if len(chunks) > limit:
                raise ValueError(f"Response from {url} exceeds the {limit // (1024 * 1024)} MB limit")
        return bytes(chunks)


def decompress_index(data: bytes, name: str) -> bytes:
    if name.endswith(".gz"):
        return gzip.decompress(data)
    if name.endswith(".bz2"):
        return bz2.decompress(data)
    if name.endswith(".xz"):
        return lzma.decompress(data)
    return data


def release_index_paths(base_url: str) -> list[str]:
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
                urllib.parse.urljoin(base_url, release_name), 2 * 1024 * 1024
            ).decode("utf-8", errors="replace")
            if "-----BEGIN PGP SIGNATURE-----" in release_text:
                release_text = release_text.split("-----BEGIN PGP SIGNATURE-----", 1)[0]
            break
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
    errors = []
    for index_path in release_index_paths(repo["url"]):
        index_url = urllib.parse.urljoin(repo["url"], index_path)
        try:
            compressed = fetch_bytes(index_url, MAX_INDEX_SIZE)
            content = decompress_index(compressed, index_path)
            if len(content) > MAX_INDEX_SIZE * 4:
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
        except (urllib.error.URLError, TimeoutError, OSError, EOFError, ValueError, lzma.LZMAError) as error:
            errors.append(f"{index_path}: {error}")
    detail = errors[-1] if errors else "No usable package index was found"
    raise ValueError(f"Could not load a Packages index from {repo['url']}. {detail}")


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
                "packages": packages,
            })
        return {
            "repos": repos,
            "automation": dict(STATE["automation"]),
            "download_dir": str(DOWNLOAD_DIR),
        }


def download_package(repo: dict, package: dict, job: dict) -> None:
    identifier = package_id(repo["id"], package)
    existing = STATE["downloads"].get(identifier, {})
    if existing.get("version") == package["version"] and Path(existing.get("path", "")).is_file():
        return

    filename = package["filename"]
    package_url = urllib.parse.urljoin(repo["url"], filename.lstrip("./"))
    if urllib.parse.urlparse(package_url).netloc != urllib.parse.urlparse(repo["url"]).netloc:
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
    request = urllib.request.Request(package_url, headers={"User-Agent": USER_AGENT})
    digest = hashlib.sha256()
    received = 0
    try:
        with urllib.request.urlopen(request, timeout=45) as response, temporary.open("wb") as output:
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
            try:
                with LOCK:
                    repos = list(STATE["repos"])
                for repo in repos:
                    refresh_repo(repo)
                with LOCK:
                    save_state()
                    refs = [
                        (repo["id"], package_id(repo["id"], package))
                        for repo in STATE["repos"]
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

    def read_json(self) -> dict:
        length = int(self.headers.get("Content-Length", "0"))
        if length > 1024 * 1024:
            raise ValueError("Request body is too large")
        return json.loads(self.rfile.read(length) or b"{}")

    def do_GET(self) -> None:
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
        path = urllib.parse.urlparse(self.path).path
        try:
            data = self.read_json()
            if path == "/api/repos":
                raw_url = str(data.get("url", "")).strip()
                parsed = urllib.parse.urlparse(raw_url)
                if parsed.scheme not in ("http", "https") or not parsed.netloc:
                    raise ValueError("Enter a valid http:// or https:// repository URL.")
                repo = {
                    "id": uuid.uuid4().hex,
                    "url": raw_url.rstrip("/") + "/",
                    "name": parsed.netloc + (parsed.path.rstrip("/") or ""),
                    "packages": [],
                }
                count = refresh_repo(repo)
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
                errors = []
                for repo in repos:
                    try:
                        refresh_repo(repo)
                    except Exception as error:
                        errors.append(f"{repo['name']}: {error}")
                with LOCK:
                    save_state()
                    refs = [
                        (repo["id"], package_id(repo["id"], package))
                        for repo in STATE["repos"]
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
        except urllib.error.URLError as error:
            self.send_json({"error": f"Repository request failed: {error.reason}"}, 502)
        except Exception as error:
            traceback.print_exc()
            self.send_json({"error": str(error)}, 500)

    def do_DELETE(self) -> None:
        path = urllib.parse.urlparse(self.path).path
        if not path.startswith("/api/repos/"):
            self.send_json({"error": "Not found"}, 404)
            return
        repo_id = path.rsplit("/", 1)[-1]
        with LOCK:
            before = len(STATE["repos"])
            STATE["repos"] = [repo for repo in STATE["repos"] if repo["id"] != repo_id]
            if len(STATE["repos"]) == before:
                self.send_json({"error": "Repository not found"}, 404)
                return
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
