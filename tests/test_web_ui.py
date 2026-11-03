"""The web interface.

The API is exercised directly, which is fast and needs nothing installed.
The browser tests are skipped unless Playwright is available:

    pip install -e ".[dev]"
    python -m playwright install chromium

Each test builds its own repository and server, so runs do not contaminate
each other.
"""

from __future__ import annotations

import json
import os
import threading
import time
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

from phonevault.backends import open_backend
from phonevault.backup import BackupRunner
from phonevault.config import Config, Profile
from phonevault.repo import Repository
from phonevault.sources.local import LocalSource
from phonevault.web.server import Api, Handler


@pytest.fixture
def seeded(tmp_path: Path) -> Config:
    """A repository with two snapshots and a config pointing at it."""
    source = tmp_path / "phone"
    (source / "DCIM" / "Camera").mkdir(parents=True)
    (source / "Documents").mkdir()
    for i in range(4):
        (source / "DCIM" / "Camera" / f"IMG_{i:03d}.jpg").write_bytes(os.urandom(60_000))
    (source / "Documents" / "notes.txt").write_bytes(b"hello\n")

    repo_dir = tmp_path / "nas"
    repo = Repository.init(open_backend(str(repo_dir)), cache_root=tmp_path / "cache")
    runner = BackupRunner(repo, LocalSource(label="Test Phone"))
    runner.run([str(source)], tags=["seed"])
    time.sleep(1.1)
    (source / "DCIM" / "Camera" / "IMG_004.jpg").write_bytes(os.urandom(70_000))
    runner.run([str(source)], tags=["nightly"])
    repo.close()

    config = Config(tmp_path / "config.json")
    config.profiles = {
        "phone": Profile(
            name="phone", repository=str(repo_dir), source="local", roots=[str(source)]
        )
    }
    config.save()
    return config


@pytest.fixture
def server(seeded: Config):
    """A live server on a free port, shut down afterwards."""
    handler = type(
        "TestHandler",
        (Handler,),
        {"api": Api(seeded), "allowed_hosts": {"localhost", "127.0.0.1"}},
    )
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()
    httpd.server_close()


def get(base: str, path: str):
    with urllib.request.urlopen(base + path, timeout=30) as response:
        return json.load(response)


def post(base: str, path: str, body: dict):
    request = urllib.request.Request(
        base + path, data=json.dumps(body).encode(), headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(request, timeout=60) as response:
        return json.load(response)


def wait_for_job(base: str, timeout: float = 120.0) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        current = get(base, "/api/job")["current"]
        if current and current["state"] != "running":
            return current
        time.sleep(0.2)
    raise AssertionError("the job never finished")


# -- API -------------------------------------------------------------------


def test_status(server):
    status = get(server, "/api/status")
    assert status["snapshot_count"] == 2
    assert status["profile"] == "phone"
    assert status["problems"] == []
    assert status["latest"]["files"] == 6


def test_snapshots_are_listed_newest_first(server):
    snapshots = get(server, "/api/snapshots")["snapshots"]
    assert len(snapshots) == 2
    assert snapshots[0]["time"] > snapshots[1]["time"]
    assert "nightly" in snapshots[0]["tags"]


def test_tree_falls_back_to_the_snapshot_root(server):
    """Asking for "/" must not show an empty folder."""
    tree = get(server, "/api/tree?path=/")
    assert tree["entries"], "the browser should land somewhere useful"
    assert {e["name"] for e in tree["entries"]} == {"DCIM", "Documents"}
    assert tree["path"] != "/"


def test_tree_navigation(server):
    root = get(server, "/api/tree?path=/")["path"]
    documents = get(server, f"/api/tree?path={root}/Documents")
    assert [e["name"] for e in documents["entries"]] == ["notes.txt"]


def test_search(server):
    hits = get(server, "/api/search?q=IMG_00")["entries"]
    assert len(hits) == 5
    assert all("IMG_00" in h["name"] for h in hits)

    with pytest.raises(urllib.error.HTTPError):
        get(server, "/api/search?q=x")  # too short


def test_backup_through_the_api(server, tmp_path):
    post(server, "/api/backup", {"forget": False})
    job = wait_for_job(server)
    assert job["state"] == "done", job.get("error")
    assert job["result"]["stats"]["files_total"] == 6
    assert get(server, "/api/status")["snapshot_count"] == 3


def test_restore_through_the_api(server, tmp_path):
    out = tmp_path / "restored"
    post(server, "/api/restore", {"target": str(out), "snapshot": "latest"})
    job = wait_for_job(server)
    assert job["state"] == "done", job.get("error")
    assert job["result"]["stats"]["failed"] == 0
    assert next(out.rglob("notes.txt")).read_bytes() == b"hello\n"


def test_maintenance_check(server):
    post(server, "/api/maintenance", {"action": "check", "read_data": True})
    job = wait_for_job(server)
    assert job["state"] == "done"
    assert job["result"]["healthy"] is True


def test_forget_and_prune(server):
    post(server, "/api/maintenance", {"action": "forget", "keep_last": 1, "dry_run": False,
                                      "prune": True})
    job = wait_for_job(server)
    assert job["state"] == "done", job.get("error")
    assert len(job["result"]["removed"]) == 1
    assert get(server, "/api/status")["snapshot_count"] == 1


def test_two_jobs_cannot_run_at_once(server):
    post(server, "/api/backup", {"rescan": True, "forget": False})
    try:
        with pytest.raises(urllib.error.HTTPError) as caught:
            post(server, "/api/backup", {"forget": False})
        assert caught.value.code == 400
    finally:
        wait_for_job(server)


def test_pause_endpoint_reports_when_nothing_is_running(server):
    with pytest.raises(urllib.error.HTTPError):
        post(server, "/api/job/pause", {})


def test_config_can_be_read_and_written(server):
    before = get(server, "/api/config")
    assert before["settings"]["source"] == "local"

    post(server, "/api/config", {"settings": {"tags": "nightly, weekly"}})
    after = get(server, "/api/config")
    assert after["settings"]["tags"] == ["nightly", "weekly"]


def test_config_edited_on_disk_is_picked_up_without_a_restart(seeded, tmp_path):
    """A `config set roots` in a terminal must reach an already-running server.

    This is not cosmetic. The server used to read the config once at startup,
    so a page opened before the roots were widened went on showing -- and
    backing up -- the single original root, while reporting success.
    """
    api = Api(seeded)
    original = api.status({})["roots"]

    raw = json.loads((tmp_path / "config.json").read_text(encoding="utf-8"))
    raw["profiles"]["phone"]["roots"] = [*original, "/data/data", "/data/app"]
    # Same size is possible but not with these additions; bump mtime anyway so
    # the test does not depend on filesystem timestamp granularity.
    (tmp_path / "config.json").write_text(json.dumps(raw, indent=2), encoding="utf-8")
    os.utime(tmp_path / "config.json", (time.time() + 2, time.time() + 2))

    assert api.status({})["roots"] == [*original, "/data/data", "/data/app"]
    # and the backup the page would start uses them, not the stale list
    assert api.profile(None).roots == [*original, "/data/data", "/data/app"]


def test_reload_does_not_discard_a_save_made_through_the_ui(seeded):
    api = Api(seeded)
    api.set_config({"settings": {"tags": "from-ui"}})
    assert api.get_config({})["settings"]["tags"] == ["from-ui"]


def test_password_placeholder_is_not_saved(server):
    """The UI shows '***'; sending it back must not overwrite the real value."""
    post(server, "/api/config", {"settings": {"smb_password": "***"}})
    assert get(server, "/api/config")["settings"]["smb_password"] == ""


def test_unknown_endpoint_is_a_clean_404(server):
    with pytest.raises(urllib.error.HTTPError) as caught:
        post(server, "/api/nope", {})
    assert caught.value.code == 404


def test_foreign_host_header_is_rejected(server):
    """Guards against DNS rebinding reaching an API that can write files."""
    request = urllib.request.Request(server + "/api/status", headers={"Host": "evil.example"})
    with pytest.raises(urllib.error.HTTPError) as caught:
        urllib.request.urlopen(request, timeout=10)
    assert caught.value.code == 403


def test_static_files_are_served(server):
    with urllib.request.urlopen(server + "/", timeout=10) as response:
        body = response.read().decode()
    assert "<title>Phonevault</title>" in body
    for asset in ("/app.js", "/style.css"):
        with urllib.request.urlopen(server + asset, timeout=10) as response:
            assert response.status == 200


def test_static_serving_refuses_traversal(server):
    with pytest.raises(urllib.error.HTTPError) as caught:
        urllib.request.urlopen(server + "/../../../../etc/passwd", timeout=10)
    assert caught.value.code == 404


# -- browser ---------------------------------------------------------------

playwright = pytest.importorskip("playwright.sync_api", reason="playwright not installed")


@pytest.fixture
def page(server):
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        try:
            browser = p.chromium.launch()
        except Exception as exc:  # pragma: no cover - browser not downloaded
            pytest.skip(f"chromium unavailable: {exc}")
        context = browser.new_page(viewport={"width": 1280, "height": 900})
        errors: list[str] = []
        context.on("pageerror", lambda e: errors.append(str(e)))
        context.on(
            "console", lambda m: errors.append(m.text) if m.type == "error" else None
        )
        context.goto(server, wait_until="networkidle")
        context.wait_for_selector(".stat", timeout=15000)
        yield context, errors
        browser.close()


def test_ui_renders_the_overview(page):
    view, errors = page
    values = view.locator(".stat .value").all_text_contents()
    assert len(values) == 4
    assert values[0] == "2"  # snapshot count
    assert view.locator("#backup-paths code").count() >= 1
    assert not errors


def test_ui_lists_snapshots(page):
    view, errors = page
    view.click('button[data-tab="snapshots"]')
    view.wait_for_selector("#snapshot-table tbody tr", timeout=15000)
    assert view.locator("#snapshot-table tbody tr").count() == 2
    assert not errors


def test_ui_browses_and_selects(page):
    view, errors = page
    view.click('button[data-tab="browse"]')
    view.wait_for_selector("#browse-list .frow", timeout=15000)
    view.locator("#browse-list .frow .name.dir", has_text="DCIM").first.click()
    view.wait_for_timeout(600)
    view.locator("#browse-list .frow .name.dir", has_text="Camera").first.click()
    view.wait_for_timeout(600)
    names = view.locator("#browse-list .frow .name").all_text_contents()
    assert any("IMG_" in n for n in names)

    view.locator('#browse-list input[type="checkbox"]').first.check()
    view.wait_for_timeout(300)
    assert view.locator("#restore-selection .chip").count() == 1
    assert not errors


def test_ui_search(page):
    view, errors = page
    view.click('button[data-tab="browse"]')
    view.wait_for_selector("#browse-list .frow", timeout=15000)
    view.fill("#browse-search", "IMG_00")
    view.wait_for_timeout(1200)
    assert view.locator("#browse-list .frow").count() == 5
    assert not errors


def test_ui_maintenance_loads_retention_without_visiting_settings(page):
    view, errors = page
    view.click('button[data-tab="maintenance"]')
    view.wait_for_selector("#keep-grid input", timeout=10000)
    assert view.locator("#keep-grid input").count() == 6
    assert not errors


def test_ui_runs_a_check(page):
    view, errors = page
    view.click('button[data-tab="maintenance"]')
    view.wait_for_selector("#keep-grid input", timeout=10000)
    view.click("#btn-check")
    view.wait_for_function(
        "document.querySelector('#maint-result').innerText.toLowerCase().includes('healthy')",
        timeout=30000,
    )
    assert not errors


def test_ui_runs_a_repair(page):
    view, errors = page
    view.click('button[data-tab="maintenance"]')
    view.wait_for_selector("#btn-repair", timeout=10000)
    view.click("#btn-repair")
    view.wait_for_function(
        "document.querySelector('#repair-result').innerText.toLowerCase()"
        ".includes('nothing to repair')",
        timeout=30000,
    )
    assert not errors


def test_ui_repair_reports_a_damaged_repository(page, seeded):
    """Deleting an index object must surface in the page, then be repaired."""
    from phonevault.index import index_object_path

    repo_dir = Path(seeded.profile("phone").repository)
    victim = sorted(repo_dir.glob("packs/*/*.pack"))[0].stem
    (repo_dir / index_object_path(victim)).unlink()

    view, errors = page
    view.click('button[data-tab="maintenance"]')
    view.wait_for_selector("#btn-repair", timeout=10000)
    view.click("#btn-repair")
    view.wait_for_function(
        "document.querySelector('#repair-result').innerText.toLowerCase().includes('repaired')",
        timeout=30000,
    )
    assert (repo_dir / index_object_path(victim)).exists()
    assert not errors


def test_ui_settings_round_trip(page):
    view, errors = page
    view.click('button[data-tab="settings"]')
    view.wait_for_selector("#settings-form input", timeout=10000)
    assert view.input_value("#set-repository")
    view.fill("#set-tags", "from-ui")
    view.click("#btn-save-settings")
    view.wait_for_selector(".toast", timeout=10000)
    assert "Saved" in view.locator(".toast").first.inner_text()
    assert not errors


def test_ui_has_no_horizontal_overflow_on_a_phone_width(page, server):
    view, _errors = page
    view.set_viewport_size({"width": 390, "height": 844})
    view.wait_for_timeout(400)
    overflow = view.evaluate(
        "document.documentElement.scrollWidth - document.documentElement.clientWidth"
    )
    assert overflow <= 1, f"{overflow}px of horizontal overflow at 390px wide"


def test_browsing_reads_the_tree_once_per_snapshot(seeded):
    """Every click used to re-download and re-parse the whole tree.

    On a snapshot of a quarter of a million files that made opening a folder
    take over three minutes, which reads as "the page is broken".
    """
    api = Api(seeded)
    repo = api.open_repo(seeded.profile("phone"))
    snapshot = repo.resolve_snapshot("latest")
    reads = []
    original = type(repo).read_tree

    def counting_read_tree(self, snap, reader=None):
        reads.append(snap.id)
        return original(self, snap, reader)

    type(repo).read_tree = counting_read_tree
    try:
        first = api.tree({"snapshot": snapshot.short_id})
        for _ in range(6):
            api.tree({"snapshot": snapshot.short_id, "path": first["path"]})
        assert len(reads) <= 1, f"tree was read {len(reads)} times for 7 requests"
    finally:
        type(repo).read_tree = original
        repo.close()


def test_every_level_of_the_tree_can_be_opened(seeded):
    """Walk the whole snapshot from its root; no level may be a dead end.

    Building the index from each entry's parent alone loses directories that
    the tree only implies, so a folder would list as empty and look broken.
    """
    api = Api(seeded)
    top = api.tree({})
    assert top["entries"], "the snapshot root must list something"

    seen, queue = set(), [top["path"]]
    while queue:
        path = queue.pop()
        if path in seen:
            continue
        seen.add(path)
        rows = api.tree({"path": path})["entries"]
        for row in rows:
            if row["kind"] == "d":
                queue.append(row["path"])
            else:
                assert row["path"].startswith(path.rstrip("/") + "/")
    assert len(seen) >= 3, f"expected to walk several directories, saw {seen}"


def test_a_directory_present_only_through_its_children_still_appears(seeded):
    api = Api(seeded)
    repo = api.open_repo(seeded.profile("phone"))
    snapshot = repo.resolve_snapshot("latest")
    index = api._browse_index(repo, snapshot)
    repo.close()

    # Every directory that holds children must itself be listed by its parent.
    for directory in index:
        if directory == "/":
            continue
        parent, _, name = directory.rpartition("/")
        parent = parent or "/"
        if parent not in index:
            continue
        assert any(r["name"] == name for r in index[parent]), (
            f"{directory} has children but does not appear inside {parent}"
        )


def _multi_root_config(tmp_path: Path) -> Config:
    """A snapshot covering several roots, as a real phone backup does."""
    a = tmp_path / "sdcard"
    b = tmp_path / "data" / "data"
    (a / "DCIM").mkdir(parents=True)
    b.mkdir(parents=True)
    (a / "DCIM" / "IMG_001.jpg").write_bytes(os.urandom(4096))
    (b / "app.db").write_bytes(os.urandom(2048))

    repo_dir = tmp_path / "nas"
    repo = Repository.init(open_backend(str(repo_dir)), cache_root=tmp_path / "cache")
    BackupRunner(repo, LocalSource(label="Test")).run([str(a), str(b)])
    repo.close()

    config = Config(tmp_path / "config.json")
    config.profiles = {
        "phone": Profile(
            name="phone", repository=str(repo_dir), source="local",
            roots=[str(a), str(b)],
        )
    }
    config.save()
    return config


def test_every_root_is_reachable_from_the_top(tmp_path):
    """Landing inside the first root hides the other nine.

    A ten-root phone backup that opens in /sdcard with no way back out looks
    like only /sdcard was backed up.
    """
    api = Api(_multi_root_config(tmp_path))
    top = api.tree({})
    assert top["path"] == "/", "with several roots the top level is the chooser"
    names = {e["name"] for e in top["entries"]}
    assert len(names) == 2, names
    for row in top["entries"]:
        assert api.tree({"path": row["path"]})["entries"], f"{row['path']} opened empty"


def test_roots_are_labelled_by_full_path(tmp_path):
    """At the top level "ABCD-1234" means nothing; "/storage/ABCD-1234" does."""
    api = Api(_multi_root_config(tmp_path))
    for row in api.tree({})["entries"]:
        assert row["name"] == row["path"]


def test_a_single_root_snapshot_still_opens_inside_it(seeded):
    """One folder containing one folder is a wasted click."""
    api = Api(seeded)
    top = api.tree({})
    assert top["path"] != "/"
    assert {e["name"] for e in top["entries"]} == {"DCIM", "Documents"}


@pytest.fixture
def multi_root_server(tmp_path: Path):
    """A live server over a snapshot with several roots, as a phone produces."""
    config = _multi_root_config(tmp_path)
    handler = type(
        "MultiRootHandler",
        (Handler,),
        {"api": Api(config), "allowed_hosts": {"localhost", "127.0.0.1"}},
    )
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()
    httpd.server_close()


@pytest.fixture
def multi_root_page(multi_root_server):
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        try:
            browser = p.chromium.launch()
        except Exception as exc:  # pragma: no cover - browser not downloaded
            pytest.skip(f"chromium unavailable: {exc}")
        view = browser.new_page(viewport={"width": 1280, "height": 900})
        errors: list[str] = []
        view.on("pageerror", lambda e: errors.append(str(e)))
        view.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
        view.goto(multi_root_server, wait_until="networkidle")
        view.wait_for_selector(".stat", timeout=15000)
        yield view, errors
        browser.close()


def test_ui_browse_shows_every_root_and_opens_them(multi_root_page):
    """The page itself, not just the API, must let you reach every root.

    Landing inside the first root with no way back out is what made a
    ten-root backup look like only /sdcard had been saved.
    """
    view, errors = multi_root_page
    view.click('button[data-tab="browse"]')
    view.wait_for_selector("#browse-list .frow", timeout=20000)

    rows = view.locator("#browse-list .frow .name").all_text_contents()
    roots = [r for r in rows if ".." not in r]
    assert len(roots) == 2, rows

    # Opening one must show its contents, and ".." must come back to the top.
    view.locator("#browse-list .frow .name.dir").first.click()
    view.wait_for_function(
        "document.querySelectorAll('#browse-list .frow').length > 0", timeout=20000
    )
    inside = view.locator("#browse-list .frow .name").all_text_contents()
    assert any(".." in r for r in inside), "no way back out of a root"
    assert len(inside) > 1, inside
    assert not errors, errors
