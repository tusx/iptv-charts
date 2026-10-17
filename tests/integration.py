#!/usr/bin/env python3
"""End-to-end tests for ingest, against a local HTTP server.

Unlike smoke.py, this exercises the real path: a real HTTP fetch, a real
streaming parse, real batched inserts, and the real background worker. It
still touches no network -- everything is served from a fixture on loopback.

Run with:  .venv/bin/python tests/integration.py
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

# Point the app at a throwaway data directory *before* importing anything that
# resolves paths, so a test run can never touch the real catalog.
_TMP = Path(tempfile.mkdtemp(prefix="iptv-charts-test-"))
os.environ["IPTV_CHARTS_DATA_DIR"] = str(_TMP)
os.environ["IPTV_CHARTS_CONFIG_DIR"] = str(_TMP)
os.environ["IPTV_CHARTS_CACHE_DIR"] = str(_TMP)
os.environ.pop("IPTV_CHARTS_PLAY_QUEUE", None)

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from iptv_charts import db, sources, sync  # noqa: E402
from iptv_charts.clients import m3u  # noqa: E402

#: Deliberately larger than sync.BATCH_SIZE so the batching path is exercised
#: rather than only ever taking the single-flush shortcut.
CHANNEL_COUNT = sync.BATCH_SIZE + 500

_results: list[tuple[bool, str, str]] = []


def check(label: str, got, want) -> None:
    ok = got == want
    _results.append((ok, label, f"{got!r}" if ok else f"{got!r} != {want!r}"))


def check_true(label: str, got) -> None:
    check(label, bool(got), True)


def section(name: str) -> None:
    print(f"\n\033[1m{name}\033[0m")


# --------------------------------------------------------------------- fixtures
def build_playlist() -> str:
    lines = ['#EXTM3U url-tvg="http://localhost/epg.xml"']
    # A quoted comma before the real delimiter, which naive parsers truncate.
    lines += [
        '#EXTINF:-1 tvg-id="gbu" tvg-name="The Good, the Bad and the Ugly" '
        'group-title="Movies",The Good, the Bad and the Ugly',
        "http://localhost/movie/gbu.mkv",
    ]
    for index in range(CHANNEL_COUNT):
        group = ["News", "Sports", "Movies"][index % 3]
        lines += [
            f'#EXTINF:-1 tvg-id="ch{index}" tvg-logo="http://localhost/l{index}.png" '
            f'group-title="{group}",Channel {index}',
            f"http://localhost/live/{index}.ts",
        ]
    return "\n".join(lines) + "\n"


PLAYLIST = build_playlist()


def xtream_array(kind: str, count: int) -> list[dict]:
    if kind == "live":
        return [
            {"num": i + 1, "name": f"Live {i}", "stream_type": "live",
             "stream_id": 90000 + i, "stream_icon": f"http://localhost/i{i}.png",
             "epg_channel_id": f"epg{i}", "added": "1700000000",
             "category_id": "1", "custom_sid": "", "tv_archive": 0,
             "direct_source": "", "tv_archive_duration": 7}
            for i in range(count)
        ]
    if kind == "vod":
        return [
            {"num": i + 1, "name": f"Movie {i}", "stream_type": "movie",
             "stream_id": 50000 + i, "stream_icon": f"http://localhost/m{i}.jpg",
             "rating": "7.5", "rating_5based": 3.8, "added": "1700100000",
             "category_id": "2", "container_extension": "mkv",
             "custom_sid": "", "direct_source": ""}
            for i in range(count)
        ]
    return [
        {"num": i + 1, "name": f"Series {i}", "series_id": 1000 + i,
         "cover": f"http://localhost/s{i}.jpg", "plot": "A plot",
         "category_id": "3", "last_modified": "1700200000"}
        for i in range(count)
    ]


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):  # keep the test output clean
        pass

    def _send(self, body: bytes, content_type: str, *, gzip_body: bool = False):
        # Honour Accept-Encoding, as a real server would. Pretending to always
        # gzip would make the probe's identity request look compressed and
        # hide whether size comparisons are correct.
        accepted = self.headers.get("Accept-Encoding", "")
        if gzip_body and "identity" not in accepted:
            import gzip as gz
            body = gz.compress(body)
        else:
            gzip_body = False

        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        if gzip_body:
            self.send_header("Content-Encoding", "gzip")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        parsed = urlparse(self.path)
        params = parse_qs(parsed.query)

        if parsed.path == "/playlist.m3u":
            self._send(PLAYLIST.encode(), "audio/x-mpegurl", gzip_body=True)
            return

        if parsed.path == "/slowplaylist.m3u":
            # Trickled out in chunks so the download phase lasts long enough
            # to observe progress deterministically rather than racing a
            # localhost transfer that finishes in a few milliseconds.
            body = PLAYLIST.encode()
            self.send_response(200)
            self.send_header("Content-Type", "audio/x-mpegurl")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            step = max(1, len(body) // 12)
            for offset in range(0, len(body), step):
                self.wfile.write(body[offset:offset + step])
                self.wfile.flush()
                time.sleep(0.03)
            return

        if parsed.path == "/empty.m3u":
            self._send(b"", "audio/x-mpegurl")
            return

        if parsed.path == "/hls.m3u8":
            self._send(
                b"#EXTM3U\n#EXT-X-VERSION:3\n#EXT-X-TARGETDURATION:10\n"
                b"#EXTINF:9.9,\nseg1.ts\n",
                "application/vnd.apple.mpegurl",
            )
            return

        if parsed.path == "/notm3u.txt":
            self._send(b"<html><body>hello</body></html>", "text/html")
            return

        if parsed.path == "/player_api.php":
            action = (params.get("action") or [""])[0]
            user = (params.get("username") or [""])[0]

            if user != "gooduser":
                # Real panels answer with auth:0 and a 200 status rather than
                # an HTTP error, so the client must not rely on status codes.
                self._send(json.dumps({
                    "user_info": {"auth": 0, "status": "Disabled"},
                    "server_info": {},
                }).encode(), "application/json")
                return

            if action == "get_live_categories":
                body = [{"category_id": "1", "category_name": "Live TV", "parent_id": 0}]
            elif action == "get_vod_categories":
                body = [{"category_id": "2", "category_name": "Films", "parent_id": 0}]
            elif action == "get_series_categories":
                body = [{"category_id": "3", "category_name": "Shows", "parent_id": 0}]
            elif action == "get_live_streams":
                body = xtream_array("live", 120)
            elif action == "get_vod_streams":
                body = xtream_array("vod", 300)
            elif action == "get_series":
                body = xtream_array("series", 80)
            elif action == "get_vod_info":
                body = {
                    "info": {"plot": "A plot", "cast": "Someone", "genre": "Drama",
                             "duration": "01:30:00", "rating": 7.5},
                    "movie_data": {"stream_id": 50000, "container_extension": "mkv"},
                }
            else:
                body = {
                    "user_info": {
                        "username": "gooduser", "auth": 1, "status": "Active",
                        "exp_date": "1893456000", "is_trial": "0",
                        "active_cons": "1", "max_connections": "2",
                        "created_at": "1700000000",
                        "allowed_output_formats": ["m3u8", "ts"],
                    },
                    "server_info": {
                        "url": "localhost", "port": "8080", "https_port": "8443",
                        "server_protocol": "http", "timezone": "Europe/London",
                        "timestamp_now": 1700000000, "time_now": "2024-01-01 12:00:00",
                    },
                }
            # Gzipped, because that is the case that broke progress accounting.
            self._send(json.dumps(body).encode(), "application/json", gzip_body=True)
            return

        self.send_error(404)


def serve():
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, f"http://127.0.0.1:{server.server_address[1]}"


def wait_for_sync(timeout: float = 90.0) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        snapshot = sync.snapshot()
        if not snapshot["active"]:
            return snapshot
        time.sleep(0.05)
    raise TimeoutError("sync did not finish in time")


# ------------------------------------------------------------------------- m3u
def test_m3u_sync(base_url: str) -> int:
    section("m3u ingest")
    conn = db.connect()

    # --- probe ---
    result = sources.probe_m3u(f"{base_url}/playlist.m3u")
    check_true("probe reports a channel estimate", result["channels_estimate"])
    check("probe sample covers whole file", result["sample_covers_whole_file"], False)
    check_true("probe found the EPG url", result["epg_url"])
    check_true("probe found groups", result["group_count_sampled"] >= 3)

    for path, expect in [("/hls.m3u8", "HLS"), ("/notm3u.txt", "M3U"),
                         ("/empty.m3u", "no content")]:
        try:
            sources.probe_m3u(f"{base_url}{path}")
            check(f"probe rejects {path}", "accepted", "rejected")
        except sources.ProbeError as exc:
            check_true(f"probe rejects {path}", expect.lower() in str(exc).lower()
                       or "empty" in str(exc).lower())

    # --- create + sync ---
    source_id = sources.create(conn, "m3u", "Test playlist",
                               m3u_url=f"{base_url}/playlist.m3u")
    sync.enqueue([source_id])
    snapshot = wait_for_sync()

    job = snapshot["jobs"][str(source_id)] if str(source_id) in snapshot["jobs"] \
        else snapshot["jobs"][source_id]
    check("sync finished cleanly", job["state"], "done")

    total = db.get_one(
        conn, "SELECT COUNT(*) n FROM items WHERE source_id = ?", (source_id,))["n"]
    check("all channels imported", total, CHANNEL_COUNT + 1)

    # The quoted-comma title is the one a naive parser truncates.
    row = db.get_one(
        conn, "SELECT name FROM items WHERE source_id = ? AND name LIKE '%Ugly%'",
        (source_id,))
    check_true("comma-in-quotes title intact",
               row is not None and row["name"] == "The Good, the Bad and the Ugly")

    kinds = {r["kind"]: r["n"] for r in db.get_all(
        conn, "SELECT kind, COUNT(*) n FROM items WHERE source_id = ? GROUP BY kind",
        (source_id,))}
    check("live classified", kinds.get("live"), CHANNEL_COUNT)
    check("movie classified as vod", kinds.get("vod"), 1)

    cats = db.get_all(
        conn, "SELECT name, item_count FROM categories WHERE source_id = ? ORDER BY name",
        (source_id,))
    check_true("categories created", len(cats) >= 4)
    check_true("category counts populated", all(c["item_count"] > 0 for c in cats))

    # --- search ---
    hits = db.get_one(
        conn, "SELECT COUNT(*) n FROM items_fts f JOIN items i ON i.id = f.rowid "
              "WHERE items_fts MATCH 'channel*' AND i.source_id = ?", (source_id,))["n"]
    check("full-text search finds items", hits, CHANNEL_COUNT)

    # --- idempotent re-sync ---
    sync.enqueue([source_id])
    wait_for_sync()
    again = db.get_one(
        conn, "SELECT COUNT(*) n FROM items WHERE source_id = ?", (source_id,))["n"]
    check("re-sync does not duplicate", again, total)

    # Count the index entries for a term every channel has. A resync that
    # appended to the index instead of rebuilding it would show double here.
    # ('channel181' would be the wrong query: FTS tokenises "Channel 181" into
    # two tokens, so a single-token query legitimately matches nothing.)
    indexed = db.get_one(
        conn, "SELECT COUNT(*) n FROM items_fts WHERE items_fts MATCH 'channel'")["n"]
    check("index not duplicated after resync", indexed, CHANNEL_COUNT)

    conn.close()
    return source_id


# ---------------------------------------------------------------------- xtream
def test_xtream_sync(base_url: str) -> int:
    section("xtream ingest")
    conn = db.connect()

    # --- probe rejects bad credentials ---
    try:
        sources.probe_xtream(base_url, "baduser", "whatever")
        check("bad credentials rejected", "accepted", "rejected")
    except sources.ProbeError:
        check("bad credentials rejected", "rejected", "rejected")

    # --- probe accepts good ones ---
    result = sources.probe_xtream(base_url, "gooduser", "goodpass")
    check("account authenticated", result["account"]["authenticated"], True)
    check("status reported", result["account"]["status"], "Active")
    check("expiry parsed", result["account"]["exp_unknown"], False)
    check("max connections", result["account"]["max_connections"], 2)
    check("formats parsed", result["account"]["allowed_formats"], ["m3u8", "ts"])
    check("live categories counted", result["category_counts"]["live"], 1)

    # --- sync ---
    source_id = sources.create(conn, "xtream", "Test panel", base_url=base_url,
                               username="gooduser", password="goodpass")
    sync.enqueue([source_id])
    snapshot = wait_for_sync()
    job = snapshot["jobs"].get(source_id) or snapshot["jobs"].get(str(source_id))
    check("sync finished cleanly", job["state"], "done")

    counts = {r["kind"]: r["n"] for r in db.get_all(
        conn, "SELECT kind, COUNT(*) n FROM items WHERE source_id = ? GROUP BY kind",
        (source_id,))}
    check("live imported", counts.get("live"), 120)
    check("vod imported", counts.get("vod"), 300)
    check("series imported", counts.get("series"), 80)

    # stream_id is the playable id. `num` is 1-based ordering and would 404.
    vod = db.get_one(
        conn, "SELECT remote_id, num, name FROM items "
              "WHERE source_id = ? AND kind = 'vod' ORDER BY id LIMIT 1", (source_id,))
    check("vod uses stream_id, not num", vod["remote_id"], "50000")
    check("num stored separately", vod["num"], 1)

    # Account snapshot should have been written to the source row.
    source = db.get_one(conn, "SELECT * FROM sources WHERE id = ?", (source_id,))
    check("account status persisted", source["account_status"], "Active")
    check("expiry persisted", source["exp_unknown"], 0)
    check("server host persisted", source["server_host"], "localhost")
    check_true("last_synced_at set", source["last_synced_at"])

    # Categories should carry denormalised counts for the filter dropdown.
    cat = db.get_one(
        conn, "SELECT item_count FROM categories WHERE source_id = ? AND kind = 'vod'",
        (source_id,))
    check("category item_count correct", cat["item_count"], 300)

    conn.close()
    return source_id


# ------------------------------------------------------------------ streaming
def test_progress_reporting(base_url: str) -> None:
    section("progress reporting")
    conn = db.connect()
    # The slow endpoint trickles the body so the download phase is
    # observable; against the normal one the whole sync can finish between
    # two polls and the test would prove nothing.
    source_id = sources.create(conn, "m3u", "Progress test",
                               m3u_url=f"{base_url}/slowplaylist.m3u")

    seen: list[dict] = []

    def watch():
        while True:
            snapshot = sync.snapshot()
            job = snapshot["jobs"].get(source_id)
            if job:
                seen.append(dict(job))
                # Only stop once this source has reached a terminal state --
                # checking `active` alone would exit on the very first poll,
                # before the worker has even picked the job up.
                if job["state"] not in ("queued", "running"):
                    return
            time.sleep(0.01)

    sync.enqueue([source_id])
    watcher = threading.Thread(target=watch, daemon=True)
    watcher.start()
    watcher.join(timeout=90)

    terminal = seen[-1] if seen else None
    check_true("reached a terminal state", terminal and terminal["state"] == "done")
    check("final item count reported", terminal["items_done"], CHANNEL_COUNT + 1)

    running = [s for s in seen if s["state"] == "running"]
    check_true("saw a running state", running)

    # The gzipped fixture is the case that would otherwise report nonsense.
    with_bytes = [s for s in running if s["bytes_total"]]
    check_true("byte totals were reported", with_bytes)
    if with_bytes:
        check_true("byte progress never exceeds the total",
                   all(0 <= s["bytes_done"] <= s["bytes_total"] for s in with_bytes))
        # Look for the moment the download finished, rather than assuming the
        # last *running* sample is the last thing that happened.
        best = max(with_bytes, key=lambda s: s["bytes_done"])
        check_true("download reached 100%",
                   best["bytes_done"] == best["bytes_total"])

    # Progress fills twice by design: once while the bytes are downloaded and
    # once while the spooled file is parsed. Each fill must be monotonic; the
    # reset between them is the phase boundary, not a regression.
    by_phase: dict[str, list[float]] = {}
    for sample in running:
        if sample["fraction"] is not None:
            by_phase.setdefault(sample["phase"], []).append(sample["fraction"])

    check_true("progress tracked across both phases", len(by_phase) >= 2)
    check_true("download and parse phases both observed",
               {"download", "parse"} <= set(by_phase))
    for phase, values in by_phase.items():
        check_true(f"fraction monotonic during '{phase}'",
                   all(a <= b + 1e-9 for a, b in zip(values, values[1:])))
    conn.close()


def test_cancellation(base_url: str) -> None:
    section("cancellation")
    conn = db.connect()
    source_id = sources.create(conn, "m3u", "Cancel test",
                               m3u_url=f"{base_url}/playlist.m3u")

    sync.enqueue([source_id])
    # Cancel as soon as it is genuinely running.
    deadline = time.time() + 10
    while time.time() < deadline:
        if sync.snapshot()["jobs"].get(source_id, {}).get("state") == "running":
            break
        time.sleep(0.01)
    sync.cancel(source_id)

    snapshot = wait_for_sync()
    job = snapshot["jobs"].get(source_id)
    check_true("ended in a cancelled or done state",
               job["state"] in ("cancelled", "done"))

    # Whatever happened, the transaction must not have left a partial catalog.
    count = db.get_one(
        conn, "SELECT COUNT(*) n FROM items WHERE source_id = ?", (source_id,))["n"]
    check_true("no partial catalog left behind",
               count == 0 or count == CHANNEL_COUNT + 1)
    conn.close()


def main() -> int:
    # The app calls this from create_app(); the tests talk to the database
    # layer directly, so they have to create the schema themselves.
    db.init_db()

    server, base_url = serve()
    try:
        m3u_id = test_m3u_sync(base_url)
        xtream_id = test_xtream_sync(base_url)
        test_progress_reporting(base_url)
        test_cancellation(base_url)

        section("cleanup")
        conn = db.connect()
        check("delete removes the source", sources.delete(conn, m3u_id), True)
        check("delete cascades items",
              db.get_one(conn, "SELECT COUNT(*) n FROM items WHERE source_id = ?",
                         (m3u_id,))["n"], 0)
        check("delete cascades categories",
              db.get_one(conn, "SELECT COUNT(*) n FROM categories WHERE source_id = ?",
                         (m3u_id,))["n"], 0)
        check("other source untouched",
              db.get_one(conn, "SELECT COUNT(*) n FROM items WHERE source_id = ?",
                         (xtream_id,))["n"], 500)
        conn.close()
    finally:
        server.shutdown()

    passed = sum(1 for ok, _, _ in _results if ok)
    failed = [(label, detail) for ok, label, detail in _results if not ok]

    print(f"\n{'=' * 60}")
    if failed:
        print(f"\033[31m{len(failed)} FAILED\033[0m, {passed} passed\n")
        for label, detail in failed:
            print(f"  \033[31m✗\033[0m {label}\n      {detail}")
        return 1

    print(f"\033[32mall {passed} checks passed\033[0m")
    return 0


if __name__ == "__main__":
    sys.exit(main())
