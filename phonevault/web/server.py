"""Local web interface.

Built on the standard library only, so the phone backup tool does not drag in
a web framework. It serves a small single-page app and a JSON API.

It binds to localhost by default and checks the ``Host`` header, because the
API can start a backup and write files anywhere this user can write. Exposing
it on a LAN address is possible but is an explicit decision.
"""

from __future__ import annotations

import json
import mimetypes
import threading
import time
import traceback
import webbrowser
from collections import OrderedDict
from collections.abc import Callable
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from .. import __version__
from ..backends import open_backend
from ..backup import BackupRunner
from ..config import Config, Profile, apply_setting
from ..errors import BackupPaused, PhonevaultError
from ..progress import CallbackProgress
from ..prune import RetentionPolicy, check, forget, prune
from ..repair import repair
from ..repo import Repository
from ..restore import AdbTarget, LocalTarget, RestoreRunner
from ..snapshot import KIND_DIR
from ..sources import open_source

STATIC_DIR = Path(__file__).parent / "static"


class Job:
    """One long-running operation, observable while it runs."""

    def __init__(self, kind: str, profile: str) -> None:
        self.id = f"{int(time.time() * 1000):x}"
        self.kind = kind
        self.profile = profile
        self.state = "running"
        self.started = time.time()
        self.finished: float | None = None
        self.progress: dict[str, Any] = {}
        self.result: dict[str, Any] | None = None
        self.error: str | None = None
        #: Set for jobs that can be asked to stop cleanly (backups).
        self.pausable: object | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind,
            "profile": self.profile,
            "state": self.state,
            "started": self.started,
            "finished": self.finished,
            "elapsed": (self.finished or time.time()) - self.started,
            "progress": self.progress,
            "result": self.result,
            "error": self.error,
            "pausable": self.pausable is not None and self.state == "running",
        }


class JobRunner:
    """Runs one job at a time and keeps a short history.

    Serialising jobs is a deliberate constraint, not a limitation: two
    concurrent backups of the same phone would fight over the adb transport,
    and a prune running alongside a backup is exactly what the repository
    lock exists to prevent.
    """

    def __init__(self, history_size: int = 25) -> None:
        self.current: Job | None = None
        self.history: list[Job] = []
        self._lock = threading.Lock()
        self._history_size = history_size

    def attach_pausable(self, runner) -> None:
        """Register the engine object that :meth:`pause` should signal."""
        if self.current is not None:
            self.current.pausable = runner

    def pause(self) -> bool:
        job = self.current
        if job is None or job.state != "running" or job.pausable is None:
            raise PhonevaultError("nothing is running that can be paused")
        job.pausable.request_pause()
        return True

    def start(self, kind: str, profile: str, work: Callable[[CallbackProgress], Any]) -> Job:
        with self._lock:
            if self.current and self.current.state == "running":
                raise PhonevaultError(
                    f"a {self.current.kind} is already running; wait for it to finish"
                )
            job = Job(kind, profile)
            self.current = job

        def runner() -> None:
            progress = CallbackProgress(lambda state: setattr(job, "progress", state))
            try:
                result = work(progress)
                job.result = result if isinstance(result, dict) else {"ok": True}
                job.state = "done"
            except BackupPaused as exc:
                # A pause is a normal, resumable outcome, not a failure.
                job.state = "paused"
                job.result = {"paused": True, "message": str(exc), "stats": exc.stats}
            except Exception as exc:
                job.state = "failed"
                job.error = str(exc) or exc.__class__.__name__
                job.progress.setdefault("warnings", []).append(job.error)
                if not isinstance(exc, PhonevaultError):
                    traceback.print_exc()
            finally:
                job.finished = time.time()
                with self._lock:
                    self.history.insert(0, job)
                    del self.history[self._history_size :]

        threading.Thread(target=runner, name=f"phonevault-{kind}", daemon=True).start()
        return job


class Api:
    """The JSON API, independent of HTTP plumbing so it stays testable."""

    #: How many snapshots' browse indexes to keep. Snapshots are immutable,
    #: so a cached index can never go stale -- only unused.
    TREE_CACHE_SIZE = 3

    def __init__(self, config: Config) -> None:
        self.config = config
        self.jobs = JobRunner()
        #: snapshot id -> {directory: [child rows]}. Browsing used to pull the
        #: whole tree off the NAS, decompress and parse it, and throw it away
        #: on *every* click; a quarter of a million entries made opening a
        #: folder take minutes. The tree is now read once per snapshot.
        self._tree_cache: OrderedDict[str, dict[str, list[dict]]] = OrderedDict()
        self._tree_lock = threading.Lock()

    # -- helpers ----------------------------------------------------------

    def profile(self, name: str | None) -> Profile:
        # Every endpoint reaches a profile through here, so this is the one
        # place that has to notice the config being edited from a terminal
        # while the server is up. A running job keeps the Profile object it
        # captured, so a reload never changes a backup already under way.
        self.config.reload_if_changed()
        return self.config.profile(name)

    def open_repo(self, profile: Profile) -> Repository:
        if not profile.repository:
            raise PhonevaultError(
                "no repository is configured yet -- set one on the Settings tab"
            )
        return Repository.open(open_backend(profile.repository, **profile.backend_options()))

    # -- read endpoints ----------------------------------------------------

    def status(self, params: dict) -> dict:
        profile = self.profile(params.get("profile"))
        payload: dict[str, Any] = {
            "version": __version__,
            "profile": profile.name,
            "profiles": sorted(self.config.profiles),
            "repository": profile.repository,
            "source": profile.source,
            "roots": profile.roots,
            "schedule": profile.schedule,
            "config_path": str(self.config.path),
            "job": self.jobs.current.as_dict() if self.jobs.current else None,
            "problems": profile.validate(),
        }
        try:
            repo = self.open_repo(profile)
        except PhonevaultError as exc:
            payload["repo_error"] = str(exc)
            return payload
        try:
            snapshots = repo.list_snapshots()
            payload["repo_id"] = repo.id
            payload["snapshot_count"] = len(snapshots)
            if snapshots:
                newest = snapshots[-1]
                payload["latest"] = {
                    "id": newest.id,
                    "short_id": newest.short_id,
                    "time": newest.time,
                    "when": newest.datetime_str,
                    "files": newest.stats.get("files_total", 0),
                    "size": newest.stats.get("bytes_total", 0),
                    "device": newest.device,
                }
            payload["locks"] = repo.list_locks()
        finally:
            repo.close()
        return payload

    def devices(self, params: dict) -> dict:
        from ..sources.adb import AdbDevice, find_adb

        try:
            adb_path = find_adb(self.profile(params.get("profile")).adb_path)
        except PhonevaultError as exc:
            return {"adb": None, "error": str(exc), "devices": []}
        found = []
        for info in AdbDevice.list_devices(adb_path):
            row = dict(info)
            if info["state"] == "device":
                try:
                    device = AdbDevice(info["serial"], adb_path)
                    row["description"] = device.describe()
                    row["root"] = device.has_root
                    row["storage"] = device.storage_summary()
                except PhonevaultError as exc:
                    row["error"] = str(exc)
            found.append(row)
        return {"adb": adb_path, "devices": found}

    def snapshots(self, params: dict) -> dict:
        repo = self.open_repo(self.profile(params.get("profile")))
        try:
            return {
                "snapshots": [
                    {
                        "id": s.id,
                        "short_id": s.short_id,
                        "time": s.time,
                        "when": s.datetime_str,
                        "roots": s.roots,
                        "tags": s.tags,
                        "device": s.device,
                        "source": s.source,
                        "parent": s.parent,
                        "stats": s.stats,
                    }
                    for s in reversed(repo.list_snapshots())
                ]
            }
        finally:
            repo.close()

    def _browse_index(self, repo: Repository, snapshot) -> dict[str, list[dict]]:
        """Directory -> its children, built once per snapshot and cached.

        Only what the browser draws is kept. Holding whole FileEntry objects
        would mean carrying every chunk digest in memory -- the bulk of a
        tree, and of no use to a folder listing.
        """
        with self._tree_lock:
            cached = self._tree_cache.get(snapshot.id)
            if cached is not None:
                self._tree_cache.move_to_end(snapshot.id)
                return cached

        buckets: dict[str, dict[str, dict]] = {}
        # Never synthesise above a root. Climbing past one would put "C:" (or
        # "sdcard") under "/", which makes "/" look populated and defeats the
        # fallback that lands the browser on the snapshot's own root.
        stop_at = {r.rstrip("/") or "/" for r in snapshot.roots}

        def ensure_ancestors(directory: str) -> None:
            """Make every level of ``directory`` reachable from the one above.

            A tree need not contain an entry for each directory -- one whose
            own root was excluded appears only through its descendants -- so
            the levels between are synthesised. Walking stops at the first
            ancestor already present, since that one's parents were linked
            when it was added.
            """
            child = directory
            while child and child != "/" and child not in stop_at:
                parent, _, name = child.rpartition("/")
                parent = parent or "/"
                bucket = buckets.setdefault(parent, {})
                if name in bucket:
                    return
                bucket[name] = {
                    "name": name, "path": child, "kind": KIND_DIR,
                    "size": 0, "mtime": 0.0, "target": "",
                }
                child = parent

        for entry in repo.read_tree(snapshot):
            here = entry.path.rstrip("/") or "/"
            if here in stop_at:
                # A tree includes an entry for the root itself, whose parent
                # lies outside the backup. Filing it there would invent the
                # directories above the root -- "C:" for a Windows source --
                # and make "/" look populated. Just make the root listable.
                buckets.setdefault(here, {})
                continue
            parent = entry.path.rsplit("/", 1)[0] or "/"
            ensure_ancestors(parent)
            # A real entry always wins over a directory synthesised for it,
            # so the listing shows the recorded mtime rather than a zero.
            buckets.setdefault(parent, {})[entry.name] = {
                "name": entry.name,
                "path": entry.path,
                "kind": entry.kind,
                "size": entry.size,
                "mtime": entry.mtime,
                "target": entry.target,
            }

        index = {
            directory: sorted(
                rows.values(), key=lambda r: (r["kind"] != KIND_DIR, r["name"].lower())
            )
            for directory, rows in buckets.items()
        }

        # The top level lists the roots themselves. Synthesising the path
        # above each one instead would bury /data/data three clicks down and,
        # for a Windows source, invent a "C:" that was never backed up. Roots
        # are labelled with their full path: at this level "ABCD-1234" says
        # far less than "/storage/ABCD-1234".
        index["/"] = [
            {
                "name": root, "path": root, "kind": KIND_DIR,
                "size": 0, "mtime": 0.0, "target": "",
            }
            for root in sorted(stop_at)
            if root in index
        ]

        with self._tree_lock:
            self._tree_cache[snapshot.id] = index
            while len(self._tree_cache) > self.TREE_CACHE_SIZE:
                self._tree_cache.popitem(last=False)
        return index

    def tree(self, params: dict) -> dict:
        repo = self.open_repo(self.profile(params.get("profile")))
        try:
            snapshot = repo.resolve_snapshot(params.get("snapshot", "latest"))
            path = params.get("path") or "/"
            index = self._browse_index(repo, snapshot)
            path = path.rstrip("/") or "/"
            if path == "/" and len(index.get("/", [])) == 1:
                # With a single root there is nothing to choose between, and
                # a folder holding one folder is a wasted click -- so open it.
                # With several, the top level is how you reach the others.
                path = index["/"][0]["path"]
            children = index.get(path, [])
            return {
                "snapshot": snapshot.short_id,
                "path": path,
                "roots": snapshot.roots,
                "entries": children,
            }
        finally:
            repo.close()

    def search(self, params: dict) -> dict:
        query = (params.get("q") or "").strip()
        if len(query) < 2:
            raise PhonevaultError("type at least two characters to search")
        repo = self.open_repo(self.profile(params.get("profile")))
        try:
            snapshot = repo.resolve_snapshot(params.get("snapshot", "latest"))
            needle = query.lower()
            hits = []
            for entry in repo.read_tree(snapshot):
                if needle in entry.name.lower():
                    hits.append(
                        {
                            "name": entry.name,
                            "path": entry.path,
                            "kind": entry.kind,
                            "size": entry.size,
                            "mtime": entry.mtime,
                        }
                    )
                    if len(hits) >= 500:
                        break
            return {"query": query, "snapshot": snapshot.short_id, "entries": hits}
        finally:
            repo.close()

    def job(self, params: dict) -> dict:
        job_id = params.get("id")
        if job_id:
            for job in ([self.jobs.current] if self.jobs.current else []) + self.jobs.history:
                if job and job.id == job_id:
                    return job.as_dict()
            raise PhonevaultError(f"no job with id {job_id}")
        return {
            "current": self.jobs.current.as_dict() if self.jobs.current else None,
            "history": [j.as_dict() for j in self.jobs.history],
        }

    def get_config(self, params: dict) -> dict:
        profile = self.profile(params.get("profile"))
        data = profile.to_dict()
        data["smb_password"] = "***" if profile.smb_password else ""
        return {
            "profile": profile.name,
            "profiles": sorted(self.config.profiles),
            "default_profile": self.config.default_profile,
            "path": str(self.config.path),
            "settings": data,
        }

    # -- write endpoints ---------------------------------------------------

    def start_backup(self, body: dict) -> dict:
        profile = self.profile(body.get("profile"))
        for problem in profile.validate():
            raise PhonevaultError(problem)
        roots = body.get("roots") or profile.roots
        tags = body.get("tags") or profile.tags
        dry_run = bool(body.get("dry_run"))
        rescan = bool(body.get("rescan"))
        resume = body.get("resume", True)
        do_forget = bool(body.get("forget"))

        def work(progress: CallbackProgress) -> dict:
            source = open_source(profile.source, **profile.source_options())
            repo = self.open_repo(profile)
            try:
                runner = BackupRunner(repo, source, progress)
                self.jobs.attach_pausable(runner)
                snapshot = runner.run(
                    roots,
                    excludes=profile.excludes,
                    use_default_excludes=profile.use_default_excludes,
                    tags=tags,
                    rescan=rescan,
                    dry_run=dry_run,
                    resume=bool(resume),
                )
                summary = {
                    "snapshot": snapshot.short_id,
                    "stats": snapshot.stats,
                    "dry_run": dry_run,
                }
                if do_forget and not dry_run:
                    policy = profile.retention.to_policy()
                    if not policy.is_empty():
                        _kept, removed = forget(repo, policy, progress=progress)
                        summary["forgotten"] = len(removed)
                        if removed and profile.prune_after_forget:
                            summary["pruned_bytes"] = prune(repo, progress=progress).bytes_freed
                return summary
            finally:
                source.close()
                repo.close()

        return self.jobs.start("backup", profile.name, work).as_dict()

    def start_restore(self, body: dict) -> dict:
        profile = self.profile(body.get("profile"))
        snapshot_ref = body.get("snapshot") or "latest"
        include = body.get("include") or []
        overwrite = body.get("overwrite", "different")
        to_phone = bool(body.get("to_phone"))
        target_path = (body.get("target") or "").strip()
        if not target_path:
            raise PhonevaultError("choose a destination folder")

        def work(progress: CallbackProgress) -> dict:
            repo = self.open_repo(profile)
            source = None
            try:
                snapshot = repo.resolve_snapshot(snapshot_ref)
                if to_phone:
                    source = open_source("adb", **profile.source_options())
                    target = AdbTarget(source.device, target_path)
                else:
                    target = LocalTarget(target_path)
                stats = RestoreRunner(repo, progress).restore(
                    snapshot, target, include=include, overwrite=overwrite
                )
                return {
                    "snapshot": snapshot.short_id,
                    "target": target.describe(),
                    "stats": stats.as_dict(),
                }
            finally:
                if source is not None:
                    source.close()
                repo.close()

        return self.jobs.start("restore", profile.name, work).as_dict()

    def start_maintenance(self, body: dict) -> dict:
        profile = self.profile(body.get("profile"))
        action = body.get("action")

        def work(progress: CallbackProgress) -> dict:
            repo = self.open_repo(profile)
            try:
                if action == "check":
                    problems = check(repo, read_data=bool(body.get("read_data")), progress=progress)
                    return {"problems": problems, "healthy": not problems}
                if action == "prune":
                    return {"stats": prune(repo, dry_run=bool(body.get("dry_run")),
                                           progress=progress).as_dict()}
                if action == "forget":
                    policy = _policy_from_body(body) or profile.retention.to_policy()
                    kept, removed = forget(
                        repo, policy, dry_run=bool(body.get("dry_run")), progress=progress
                    )
                    result = {
                        "kept": len(kept),
                        "removed": [{"id": s.id, "short_id": s.short_id, "when": s.datetime_str}
                                    for s in removed],
                        "policy": policy.describe(),
                    }
                    if removed and body.get("prune") and not body.get("dry_run"):
                        result["stats"] = prune(repo, progress=progress).as_dict()
                    return result
                if action == "repair":
                    return repair(
                        repo,
                        discard_damaged=bool(body.get("discard_damaged")),
                        progress=progress,
                    ).as_dict()
                if action == "unlock":
                    return {"broken": repo.break_locks()}
                raise PhonevaultError(f"unknown maintenance action {action!r}")
            finally:
                repo.close()

        return self.jobs.start(action or "maintenance", profile.name, work).as_dict()

    def pause_job(self, body: dict) -> dict:
        self.jobs.pause()
        return {"pausing": True, "note": "the backup will stop after the current file"}

    def delete_snapshot(self, body: dict) -> dict:
        profile = self.profile(body.get("profile"))
        repo = self.open_repo(profile)
        try:
            snapshot = repo.resolve_snapshot(body["snapshot"])
            repo.delete_snapshot(snapshot.id)
            return {
                "deleted": snapshot.short_id,
                "note": "run Prune on the Maintenance tab to reclaim the space",
            }
        finally:
            repo.close()

    def set_config(self, body: dict) -> dict:
        profile = self.profile(body.get("profile"))
        changed = {}
        for key, value in (body.get("settings") or {}).items():
            if key == "smb_password" and value in ("", "***"):
                continue  # the UI shows a placeholder; do not overwrite with it
            raw = ",".join(value) if isinstance(value, list) else str(value)
            changed[key] = apply_setting(profile, key, raw)
        self.config.add_profile(profile)
        self.config.save()
        return {"saved": True, "changed": list(changed), "path": str(self.config.path)}

    def init_repo(self, body: dict) -> dict:
        profile = self.profile(body.get("profile"))
        location = (body.get("repository") or profile.repository or "").strip()
        if not location:
            raise PhonevaultError("give a repository location first")
        repo = Repository.init(open_backend(location, **profile.backend_options()))
        try:
            profile.repository = location
            self.config.add_profile(profile)
            self.config.save()
            return {"repository": location, "id": repo.id}
        finally:
            repo.close()

    def set_schedule(self, body: dict) -> dict:
        from ..scheduler import describe_schedule, install_task, remove_task

        profile = self.profile(body.get("profile"))
        spec = (body.get("schedule") or "").strip()
        if not spec:
            profile.schedule = ""
            self.config.add_profile(profile)
            self.config.save()
            return {"message": remove_task(profile.name), "schedule": ""}
        message = install_task(
            profile.name, spec, config_path=self.config.path, forget=bool(body.get("forget", True))
        )
        profile.schedule = spec
        self.config.add_profile(profile)
        self.config.save()
        return {"message": message, "schedule": spec, "description": describe_schedule(spec)}


def _policy_from_body(body: dict) -> RetentionPolicy | None:
    keys = ("last", "hourly", "daily", "weekly", "monthly", "yearly")
    supplied = {k: body.get(f"keep_{k}") for k in keys}
    if not any(v not in (None, "", 0) for v in supplied.values()):
        return None
    return RetentionPolicy(**{k: (int(v) if v not in (None, "") else None)
                              for k, v in supplied.items()})


# -- HTTP layer -------------------------------------------------------------

GET_ROUTES: dict[str, str] = {
    "/api/status": "status",
    "/api/devices": "devices",
    "/api/snapshots": "snapshots",
    "/api/tree": "tree",
    "/api/search": "search",
    "/api/job": "job",
    "/api/config": "get_config",
}

POST_ROUTES: dict[str, str] = {
    "/api/backup": "start_backup",
    "/api/restore": "start_restore",
    "/api/maintenance": "start_maintenance",
    "/api/job/pause": "pause_job",
    "/api/snapshot/delete": "delete_snapshot",
    "/api/config": "set_config",
    "/api/repo/init": "init_repo",
    "/api/schedule": "set_schedule",
}


class Handler(BaseHTTPRequestHandler):
    server_version = f"phonevault/{__version__}"
    api: Api
    allowed_hosts: set[str]

    def log_message(self, fmt: str, *args) -> None:
        pass  # the console belongs to the progress display

    # -- guards -----------------------------------------------------------

    def _host_allowed(self) -> bool:
        host = (self.headers.get("Host") or "").split(":")[0].strip().lower()
        return host in self.allowed_hosts

    def _reject_host(self) -> None:
        self._send_json(
            HTTPStatus.FORBIDDEN,
            {
                "error": "request rejected: unexpected Host header. "
                "Reach the interface at the address it printed on startup."
            },
        )

    # -- verbs ------------------------------------------------------------

    def do_GET(self) -> None:
        if not self._host_allowed():
            return self._reject_host()
        parsed = urlparse(self.path)
        if parsed.path in GET_ROUTES:
            params = {k: v[0] for k, v in parse_qs(parsed.query).items()}
            return self._call(GET_ROUTES[parsed.path], params)
        self._serve_static(parsed.path)

    def do_POST(self) -> None:
        if not self._host_allowed():
            return self._reject_host()
        parsed = urlparse(self.path)
        route = POST_ROUTES.get(parsed.path)
        if route is None:
            return self._send_json(HTTPStatus.NOT_FOUND, {"error": f"no such endpoint {parsed.path}"})
        try:
            length = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(length) or b"{}")
        except (ValueError, OSError) as exc:
            return self._send_json(HTTPStatus.BAD_REQUEST, {"error": f"bad request body: {exc}"})
        self._call(route, body)

    # -- dispatch ----------------------------------------------------------

    def _call(self, name: str, payload: dict) -> None:
        try:
            self._send_json(HTTPStatus.OK, getattr(self.api, name)(payload))
        except PhonevaultError as exc:
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
        except Exception as exc:
            traceback.print_exc()
            self._send_json(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                {"error": f"{exc.__class__.__name__}: {exc}"},
            )

    def _send_json(self, status: HTTPStatus, payload: Any) -> None:
        body = json.dumps(payload, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass  # the browser navigated away mid-poll

    def _serve_static(self, path: str) -> None:
        relative = "index.html" if path in ("/", "") else path.lstrip("/")
        target = (STATIC_DIR / relative).resolve()
        if not str(target).startswith(str(STATIC_DIR.resolve())) or not target.is_file():
            return self._send_json(HTTPStatus.NOT_FOUND, {"error": "not found"})
        content_type = mimetypes.guess_type(str(target))[0] or "application/octet-stream"
        data = target.read_bytes()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            pass


def serve(config: Config, *, host: str = "127.0.0.1", port: int = 8723,
          open_browser: bool = True) -> None:
    handler = type(
        "BoundHandler",
        (Handler,),
        {
            "api": Api(config),
            "allowed_hosts": {"localhost", "127.0.0.1", "::1", host.lower()},
        },
    )
    try:
        server = ThreadingHTTPServer((host, port), handler)
    except OSError as exc:
        raise PhonevaultError(
            f"cannot listen on {host}:{port}: {exc}. "
            f"Another copy may already be running -- try --port {port + 1}."
        ) from exc

    url = f"http://{'localhost' if host in ('127.0.0.1', '0.0.0.0') else host}:{port}/"
    print(f"Phonevault {__version__} -- web interface at {url}")
    if host == "0.0.0.0":
        print("  Listening on every interface. Anyone who can reach this port can")
        print("  start backups and restore files. Use 127.0.0.1 unless you need otherwise.")
    print("  Press Ctrl+C to stop.")
    if open_browser:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping.")
    finally:
        server.shutdown()
        server.server_close()
