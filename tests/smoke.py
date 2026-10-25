#!/usr/bin/env python3
"""Smoke tests for the parts of IPTV Charts that can be checked without network.

Run with:  ./setup.test.sh --check
       or:  .venv/bin/python tests/smoke.py

Deliberately dependency-free (no pytest) so it runs anywhere the app runs, and
deliberately offline -- nothing here contacts a provider. The goal is to catch
the specific traps this codebase was built around, not to chase coverage.
"""

from __future__ import annotations

import gzip
import json
import sys
import tempfile
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from iptv_charts import (config, connections, db, items, jobs, languages,  # noqa: E402
                         matching, metadata, naming, player, probe, quality,
                         sources, streams)
from iptv_charts.clients import m3u, tmdb, xtream  # noqa: E402
from iptv_charts.errors import SyncCancelled  # noqa: E402

_results: list[tuple[bool, str, str]] = []


def check(label: str, got, want) -> None:
    ok = got == want
    _results.append((ok, label, f"{got!r}" if ok else f"{got!r} != {want!r}"))


def check_true(label: str, got) -> None:
    check(label, bool(got), True)


def section(name: str) -> None:
    print(f"\n\033[1m{name}\033[0m")


def raises(fn, *args, exc: type[BaseException] = Exception, **kwargs) -> str:
    """'raised' or 'returned' -- for the checks that something must be refused.

    ``exc`` narrows it to the exception the caller promised, so a TypeError from
    a bug does not read as a successful refusal.
    """
    try:
        fn(*args, **kwargs)
    except exc:
        return "raised"
    return "returned"


# ---------------------------------------------------------------------- config
def test_config() -> None:
    section("config")
    check_true("data_dir resolves", config.data_dir().is_dir())
    check_true("config_dir resolves", config.config_dir().is_dir())
    check_true("cache_dir resolves", config.cache_dir().is_dir())
    check_true("spool_dir under cache", config.spool_dir().parent == config.cache_dir())

    check("no play queue by default", config.play_queue_path(), None)

    # The explicit override must win outright, with no app-slug appended --
    # that is what lets the container say "write to /data".
    with tempfile.TemporaryDirectory() as tmp:
        import os

        os.environ["IPTV_CHARTS_DATA_DIR"] = tmp
        try:
            check("override wins outright", config.data_dir(), Path(tmp))
        finally:
            del os.environ["IPTV_CHARTS_DATA_DIR"]

    check_true("port probe finds a free port", config.find_free_port() > 0)

    # Occupy a port for real and confirm the probe notices. This has to bind
    # an actual socket -- probing a guessed "usually busy" port would be a
    # tautology on a machine where that guess happens to be wrong.
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as occupied:
        occupied.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        occupied.bind((config.BIND_HOST, 0))
        occupied.listen(1)
        taken = occupied.getsockname()[1]
        check("busy port detected", config.port_is_available(taken), False)

        # The invariant is that it never hands back a port that is in use. It
        # may legitimately return a different free port below the busy one
        # (a remembered port), so asserting "greater than" would be wrong.
        chosen = config.find_free_port(taken)
        check("find_free_port avoids the busy port", chosen != taken, True)
        check("find_free_port returns a free port",
              config.port_is_available(chosen), True)


# -------------------------------------------------------------------------- db
def test_db() -> None:
    section("database")
    with tempfile.TemporaryDirectory() as tmp:
        conn = db.connect(Path(tmp) / "t.db")
        db.apply_schema(conn)

        tables = {r["name"] for r in db.get_all(
            conn, "SELECT name FROM sqlite_master WHERE type='table'")}
        for expected in ("sources", "categories", "items", "items_fts", "settings"):
            check_true(f"table {expected} exists", expected in tables)

        triggers = db.get_all(
            conn, "SELECT name FROM sqlite_master WHERE type='trigger'")
        check("three FTS triggers", len(triggers), 3)
        check("journal is WAL", db.get_one(conn, "PRAGMA journal_mode")[0], "wal")

        conn.execute("INSERT INTO sources (id,name,kind,created_at) VALUES (1,'T','m3u',0)")
        for rid, name in [("1", "BBC News"), ("2", "Café Society"),
                          ("3", "The Good, the Bad and the Ugly")]:
            conn.execute(
                "INSERT INTO items (source_id,kind,remote_id,name) VALUES (1,'vod',?,?)",
                (rid, name))
        conn.commit()

        def hits(q: str) -> int:
            return db.get_one(
                conn, "SELECT COUNT(*) c FROM items_fts WHERE items_fts MATCH ?", (q,))["c"]

        check("prefix search 'new*'", hits("new*"), 1)
        # Diacritic folding is the reason for `remove_diacritics 2`.
        check("diacritic fold 'cafe'", hits("cafe"), 1)
        check("diacritic fold 'café'", hits("café"), 1)
        check("comma title searchable", hits("ugly"), 1)

        # Re-applying the schema must be safe; bulk_load depends on it to
        # restore the triggers it dropped.
        db.apply_schema(conn)
        check("apply_schema idempotent", len(db.get_all(
            conn, "SELECT name FROM sqlite_master WHERE type='trigger'")), 3)

        with db.bulk_load(conn) as c:
            c.execute("DELETE FROM items")
            c.executemany(
                "INSERT INTO items (source_id,kind,remote_id,name) VALUES (1,'vod',?,?)",
                [(str(1000 + i), f"Bulk {i}") for i in range(2000)])
            c.commit()

        check("bulk rows inserted", db.get_one(conn, "SELECT COUNT(*) c FROM items")["c"], 2000)
        check("FTS rebuilt after bulk", db.get_one(
            conn,
            "SELECT COUNT(*) c FROM items_fts WHERE items_fts MATCH 'bulk*'")["c"], 2000)
        check("triggers restored", len(db.get_all(
            conn, "SELECT name FROM sqlite_master WHERE type='trigger'")), 3)
        check("WAL restored", db.get_one(conn, "PRAGMA journal_mode")[0], "wal")
        check("synchronous restored to NORMAL",
              db.get_one(conn, "PRAGMA synchronous")[0], 1)

        # Triggers must work again after the bulk path.
        conn.execute(
            "INSERT INTO items (source_id,kind,remote_id,name) VALUES (1,'vod','9999','Zebra')")
        conn.commit()
        check("trigger active post-bulk", db.get_one(
            conn, "SELECT COUNT(*) c FROM items_fts WHERE items_fts MATCH 'zebra'")["c"], 1)
        conn.close()


# ------------------------------------------------------------------------- m3u
def test_m3u() -> None:
    section("m3u parser")
    playlist = "﻿" + "\n".join([
        '#EXTM3U url-tvg="http://epg/x.xml"',
        '#EXTINF:-1 tvg-id="cnn.us" tvg-name="CNN" tvg-logo="http://x/c.png" '
        'group-title="News;US",CNN (US)',
        "http://e/live/cnn.ts",
        # The trap: a comma inside a quoted attribute precedes the real delimiter.
        '#EXTINF:-1 tvg-name="The Good, the Bad and the Ugly" group-title="Movies",'
        'The Good, the Bad and the Ugly',
        "http://e/movie/gbu.mkv",
        '#EXTINF:-1 tvg-id="bbc.uk" group-title="News",BBC One',
        "#EXTGRP:UK;Terrestrial",
        "http://e/live/bbc.ts",
        '#EXTINF:-1 tvg-name="Radio 1" radio="true" group-title="Radio",BBC Radio 1',
        "#EXTVLCOPT:http-user-agent=VLC/3.0.18",
        "#EXTVLCOPT:http-referrer=http://bbc/",
        "http://e/r1.mp3",
        "#EXTINF:-1,Header Injected",
        "http://e/h.ts|user-agent=CustomUA/1.0&referrer=http://ref/",
    ])

    chans = list(m3u.parse_text(playlist))

    check("BOM stripped, 5 entries", len(chans), 5)
    check("comma-in-quotes name intact", chans[1].name, "The Good, the Bad and the Ugly")
    check("comma-in-quotes tvg-name intact", chans[1].tvg_name,
          "The Good, the Bad and the Ugly")
    check("plain name", chans[0].name, "CNN (US)")
    check("group-title splits on semicolon", chans[0].group, "News;US")
    check("EXTGRP unions with group-title", chans[2].group, "News;UK;Terrestrial")
    check("radio flag", chans[3].radio, True)
    # mpv ignores #EXTVLCOPT entirely, so these must be lifted out.
    check("EXTVLCOPT user-agent extracted", chans[3].user_agent, "VLC/3.0.18")
    check("EXTVLCOPT referrer extracted", chans[3].referrer, "http://bbc/")
    check("URL |suffix user-agent", chans[4].user_agent, "CustomUA/1.0")
    check("URL |suffix stripped", chans[4].url, "http://e/h.ts")
    check("empty tvg-id becomes None", chans[1].tvg_id, None)

    check("real HLS detected",
          m3u.looks_like_hls("#EXTM3U\n#EXT-X-VERSION:3\n#EXT-X-TARGETDURATION:10\n"), True)
    check("channel list is not HLS", m3u.looks_like_hls(playlist), False)
    check("extra comma in name preserved",
          m3u.split_extinf('#EXTINF:-1 group-title="A,B",Name, With Comma')[2],
          "Name, With Comma")


# ---------------------------------------------------------------------- xtream
def test_xtream() -> None:
    section("xtream client")
    check("C ijson backend in use", xtream.IJSON_BACKEND, "yajl2_c")

    # Base URL normalisation -- users paste every variation.
    cases = [
        ("http://h:8080/player_api.php?username=a", "http://h:8080"),
        ("h:8080/", "http://h:8080"),
        ("https://h/panel/player_api.php", "https://h/panel"),
        ("  http://h:80  ", "http://h:80"),
    ]
    for raw, want in cases:
        try:
            check(f"normalise {raw!r}", xtream.normalise_base_url(raw), want)
        except xtream.XtreamError as exc:
            check(f"normalise {raw!r}", f"raised {exc}", want)

    try:
        xtream.normalise_base_url("http://user:pw@host:8080")
        check("credentials in URL rejected", "accepted", "rejected")
    except xtream.XtreamError:
        check("credentials in URL rejected", "rejected", "rejected")

    client = xtream.XtreamClient("http://h:8080", "u", "p")
    # stream_id is the real id; `num` is only a display hint and 404s in a URL.
    check("live .ts", client.stream_url("live", "94214"),
          "http://h:8080/live/u/p/94214.ts")
    check("live .m3u8", client.stream_url("live", "94214", prefer_hls=True),
          "http://h:8080/live/u/p/94214.m3u8")
    check("vod uses container ext", client.stream_url("vod", "101", "mkv"),
          "http://h:8080/movie/u/p/101.mkv")
    check("series", client.stream_url("series", "55", "mp4"),
          "http://h:8080/series/u/p/55.mp4")

    # Spool parsing, and the progress accounting the whole design hinges on.
    payload = json.dumps(
        [{"stream_id": i, "name": f"Movie {i}", "rating": 7.5} for i in range(3000)]
    ).encode()

    spool = config.spool_dir() / "smoke.json.gz"
    spool.write_bytes(gzip.compress(payload))
    progress: list[tuple[int, int]] = []
    items = list(client._parse_spool(
        spool, "gzip", on_progress=lambda d, t: progress.append((d, t))))

    check("gzip spool parsed", len(items), 3000)
    check("first item intact", items[0]["name"], "Movie 0")
    # Progress must be measured in COMPRESSED bytes. Counting decompressed
    # bytes against a compressed Content-Length is the bug this guards.
    check("progress ends at compressed size", progress[-1][0], spool.stat().st_size)
    check("progress total is compressed size", progress[-1][1], spool.stat().st_size)
    check("progress monotonic",
          all(a[0] <= b[0] for a, b in zip(progress, progress[1:])), True)
    ratio = len(payload) / spool.stat().st_size
    check_true(f"compression ratio {ratio:.1f}x (why the old maths was wrong)", ratio > 1)

    plain = config.spool_dir() / "smoke.json"
    plain.write_bytes(payload)
    prog2: list[tuple[int, int]] = []
    items2 = list(client._parse_spool(
        plain, None, on_progress=lambda d, t: prog2.append((d, t))))
    check("uncompressed spool parsed", len(items2), 3000)
    check("uncompressed progress ends at size", prog2[-1][0], len(payload))

    # The silent-data-loss trap: an object where an array was expected makes
    # ijson yield zero items with NO error. It must fail loudly instead.
    bad = config.spool_dir() / "bad.json"
    bad.write_bytes(b'{"status":"error","message":"nope"}')
    try:
        client._check_array_shape(bad, None)
        check("object-instead-of-array rejected", "accepted", "rejected")
    except xtream.XtreamError:
        check("object-instead-of-array rejected", "rejected", "rejected")

    empty = config.spool_dir() / "empty.json"
    empty.write_bytes(b"[]")
    try:
        client._check_array_shape(empty, None)
        check("empty array accepted", True, True)
    except xtream.XtreamError:
        check("empty array accepted", False, True)

    cancel = threading.Event()
    cancel.set()
    try:
        for _ in client._parse_spool(plain, None, cancel=cancel):
            pass
        check("cancellation aborts parse", "no raise", "SyncCancelled")
    except SyncCancelled:
        check("cancellation aborts parse", "raised", "raised")

    for path in (spool, plain, bad, empty):
        path.unlink(missing_ok=True)


# ---------------------------------------------------------------------- player
MPV = player.Player(id="native-mpv", label="mpv", flavour="mpv",
                    argv_prefix=("/usr/bin/mpv",))
VLC = player.Player(id="native-vlc", label="VLC", flavour="vlc",
                    argv_prefix=("/usr/bin/vlc",))


def test_player() -> None:
    section("player")

    # Each player gets its own flag dialect, because it does not understand
    # anyone else's: VLC exits on startup given mpv's options.
    argv = MPV.command_for("http://e/s.ts", user_agent="UA/1.0",
                           referrer="http://r/", title="T")
    check_true("mpv argv carries user-agent", "--user-agent=UA/1.0" in argv)
    check_true("mpv argv carries referrer", "--referrer=http://r/" in argv)
    check("mpv argv ends with the url", argv[-2:], ["--", "http://e/s.ts"])

    argv = VLC.command_for("http://e/s.ts", user_agent="UA/1.0",
                           referrer="http://r/", title="T")
    check_true("vlc argv carries user-agent", "--http-user-agent=UA/1.0" in argv)
    check_true("vlc argv carries referrer", "--http-referrer=http://r/" in argv)
    check_true("vlc argv carries title", "--meta-title=T" in argv)
    check_true("vlc is not handed mpv's flags",
               not any(a.startswith("--user-agent=") or a.startswith("--referrer=")
                       for a in argv))
    check("vlc argv ends with the url", argv[-1], "http://e/s.ts")

    # The URL is the last argument handed to the player, and it is not always
    # ours -- an M3U item's stream URL is whatever the playlist said. A "url"
    # that is really an option is code execution, so only stream URLs pass.
    for address in ("--script=/tmp/x.lua", "file:///etc/passwd", "-x", "not a url", ""):
        check(f"refuses {address[:24]!r}",
              raises(MPV.command_for, address, exc=player.PlayerError), "raised")

    section("player commands")
    check("a command splits into argv",
          player.parse_command('mpv --title="two words" {url}'),
          ("mpv", "--title=two words", "{url}"))
    check("an unbalanced quote is refused",
          raises(player.parse_command, 'mpv "unclosed'), "raised")
    check("an empty command is refused", raises(player.parse_command, "   "), "raised")
    check("control characters are refused",
          raises(player.parse_command, "mpv\n--title=x {url}"), "raised")
    check("a placeholder in the program name is refused",
          raises(player.parse_command, "{player} {url}"), "raised")

    custom = player.Player(id="custom", label="Custom", flavour="custom",
                           argv_prefix=player.parse_command(
                               "mpv --title={title} --user-agent={user_agent} {url}"))
    check("placeholders expand",
          custom.command_for("http://e/s.ts", title="T", user_agent="UA"),
          ["mpv", "--title=T", "--user-agent=UA", "http://e/s.ts"])
    check("a flag with no value is dropped, not emptied",
          custom.command_for("http://e/s.ts", title="T"),
          ["mpv", "--title=T", "http://e/s.ts"])
    check("the url is appended when the command never mentions it",
          player.Player(id="custom", label="Custom", flavour="custom",
                        argv_prefix=("mpv", "--fs")).command_for("http://e/s.ts"),
          ["mpv", "--fs", "http://e/s.ts"])
    # Substitution happens in one pass, so a value that looks like a placeholder
    # is never substituted again.
    check("a value is not itself re-expanded",
          custom.command_for("http://e/s.ts", title="{url}"),
          ["mpv", "--title={url}", "http://e/s.ts"])

    # With no queue configured, launch() must never silently succeed.
    check("no queue configured", config.play_queue_path(), None)
    check("launch refuses without a player",
          raises(player.launch, "http://e/s.ts", player=None), "raised")


def test_player_bridge() -> None:
    """The upgrade path that must not lose the button.

    A new app against an older host bridge has no players.json to read. That
    bridge can still play -- it just cannot say with what -- so the app has to
    keep offering it. Falling back to "no players" here is exactly how the
    external-player button vanished from a container whose bridge had not been
    restarted after the upgrade.
    """
    import os

    section("player and an older host bridge")
    saved = os.environ.get("IPTV_CHARTS_PLAY_QUEUE")
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        queue = root / "queue"
        (root / player.BRIDGE_HEARTBEAT).write_text("0")
        os.environ["IPTV_CHARTS_PLAY_QUEUE"] = str(queue)
        try:
            player._CANDIDATES = None
            found = player.available(None)
            check("an old bridge still offers a player",
                  [p.id for p in found], [player.BRIDGE_PLAYER_ID])

            player.launch("http://e/s.ts", player=found[0])
            request = json.loads(queue.read_text().splitlines()[-1])
            check("no command is sent to a bridge that cannot run one",
                  request["command"], None)
            check("the stream address is still sent", request["url"], "http://e/s.ts")

            # A current bridge that reports nothing is authoritative: it has
            # looked and found no player, so there is nothing to offer.
            (root / player.PLAYERS_FILE).write_text(json.dumps({
                "version": player.PLAYERS_FILE_VERSION, "players": [],
            }))
            player._CANDIDATES = None
            check("a report of no players hides the button", player.available(None), [])

            # A current bridge with a player is used, and the argv is ours.
            (root / player.PLAYERS_FILE).write_text(json.dumps({
                "version": player.PLAYERS_FILE_VERSION,
                "players": [{
                    "id": "native-mpv", "label": "mpv", "flavour": "mpv",
                    "command": ["/usr/bin/mpv"], "extra_args": ["--fs"],
                }],
            }))
            player._CANDIDATES = None
            found = player.available(None)
            check("the reported player is used", [p.id for p in found], ["native-mpv"])

            player.launch("http://e/s.ts", player=found[0])
            request = json.loads(queue.read_text().splitlines()[-1])
            check("its command travels with the request", request["command"],
                  ["/usr/bin/mpv", "--fs", "--", "http://e/s.ts"])

            # No heartbeat: the bridge is gone, so nothing is offered.
            (root / player.BRIDGE_HEARTBEAT).unlink()
            player._CANDIDATES = None
            check("a dead bridge hides the button", player.available(None), [])
        finally:
            if saved is None:
                os.environ.pop("IPTV_CHARTS_PLAY_QUEUE", None)
            else:
                os.environ["IPTV_CHARTS_PLAY_QUEUE"] = saved
            player._CANDIDATES = None

    check("the queue setting was restored", config.play_queue_path(), None)


# ---------------------------------------------------------------- streaming
def test_streaming() -> None:
    """The shared spool/line helpers, compressed and not.

    Both encodings are checked because they take different code paths: a
    gzipped file is read through a GzipFile, a plain one directly from the
    CountingReader. Only testing the gzipped case once hid a crash on the
    plain path, which is the one most real M3U servers take.
    """
    section("streaming")
    from iptv_charts.clients import streaming

    payload = "\n".join(f"line {i}" for i in range(1000)).encode()

    plain = config.spool_dir() / "lines.txt"
    plain.write_bytes(payload)
    lines = list(streaming.iter_text_lines(plain, None))
    check("uncompressed line count", len(lines), 1000)
    check("uncompressed first line", lines[0], "line 0")

    packed = config.spool_dir() / "lines.txt.gz"
    packed.write_bytes(gzip.compress(payload))
    gz_lines = list(streaming.iter_text_lines(packed, "gzip"))
    check("compressed line count", len(gz_lines), 1000)
    check("compressed first line", gz_lines[0], "line 0")

    bom = config.spool_dir() / "bom.txt"
    bom.write_bytes(b"\xef\xbb\xbf#EXTM3U\nhello\n")
    check("BOM stripped", list(streaming.iter_text_lines(bom, None))[0], "#EXTM3U")

    crlf = config.spool_dir() / "crlf.txt"
    crlf.write_bytes(b"a\r\nb\r\n")
    check("CRLF handled", list(streaming.iter_text_lines(crlf, None)), ["a", "b"])

    nonl = config.spool_dir() / "nonl.txt"
    nonl.write_bytes(b"x\ny")  # no trailing newline
    check("final line without newline kept",
          list(streaming.iter_text_lines(nonl, None)), ["x", "y"])

    empty = config.spool_dir() / "empty.txt"
    empty.write_bytes(b"")
    check("empty file yields nothing", list(streaming.iter_text_lines(empty, None)), [])

    cancel = threading.Event()
    cancel.set()
    try:
        list(streaming.iter_text_lines(plain, None, cancel=cancel))
        check("cancel aborts line iteration", "no raise", "SyncCancelled")
    except SyncCancelled:
        check("cancel aborts line iteration", "raised", "raised")

    for path in (plain, packed, bom, crlf, nonl, empty):
        path.unlink(missing_ok=True)


# -------------------------------------------------------------------- items
def test_items() -> None:
    """Query building and stream URL construction."""
    section("items")
    from iptv_charts import items

    # Search input is rebuilt as a safe prefix query. Raw punctuation must
    # never reach FTS, where " and * and - all mean something.
    check("single word becomes a prefix query", items.fts_query("matrix"), '"matrix"*')
    check("multiple words are ANDed", items.fts_query("the matrix"),
          '"the"* "matrix"*')
    check("punctuation is stripped", items.fts_query('a"b* OR c-d'),
          '"a"* "b"* "OR"* "c"* "d"*')
    check("empty input yields nothing", items.fts_query("   "), None)
    check("punctuation-only yields nothing", items.fts_query("!!! ???"), None)
    check("unicode words survive", items.fts_query("Café"), '"Café"*')

    check("hls detected", items.guess_media_type("http://h/x.m3u8"), "hls")
    check("mpegts detected", items.guess_media_type("http://h/x.ts"), "mpegts")
    check("mp4 is a plain file", items.guess_media_type("http://h/x.mp4"), "file")
    check("query string ignored", items.guess_media_type("http://h/x.m3u8?token=1"), "hls")

    # URL construction, including the stream_id-vs-num trap.
    xtream_source = {
        "kind": "xtream", "base_url": "http://h:8080", "username": "u",
        "password": "p", "allowed_formats": '["m3u8","ts"]',
    }
    live = {"kind": "live", "remote_id": "94214", "container_ext": None}
    vod = {"kind": "vod", "remote_id": "50000", "container_ext": "mkv"}
    series = {"kind": "series", "remote_id": "1000", "container_ext": None}

    check("live prefers hls for the browser",
          items.build_stream_url(xtream_source, live, prefer_hls=True),
          "http://h:8080/live/u/p/94214.m3u8")
    check("live falls back to ts for mpv",
          items.build_stream_url(xtream_source, live, prefer_hls=False),
          "http://h:8080/live/u/p/94214.ts")
    check("vod uses the container extension",
          items.build_stream_url(xtream_source, vod),
          "http://h:8080/movie/u/p/50000.mkv")

    # A series id is not playable; it must not silently produce a bad URL.
    try:
        items.build_stream_url(xtream_source, series)
        check("series refused without an episode", "built a url", "raised")
    except items.PlaybackUnavailable:
        check("series refused without an episode", "raised", "raised")

    # ts-only accounts must not be handed an .m3u8 they cannot play.
    ts_only = dict(xtream_source, allowed_formats='["ts"]')
    check("ts-only account gets ts",
          items.build_stream_url(ts_only, live, prefer_hls=True),
          "http://h:8080/live/u/p/94214.ts")

    # Credentials with characters meaningful in a path must be encoded.
    odd = dict(xtream_source, username="a b", password="p@ss/word")
    check_true("credentials are percent-encoded",
               "a%20b" in items.build_stream_url(odd, live)
               and "p%40ss%2Fword" in items.build_stream_url(odd, live))

    # An M3U item's remote id already is its URL.
    m3u_source = {"kind": "m3u"}
    check("m3u url passes through",
          items.build_stream_url(m3u_source, {"kind": "live", "remote_id": "http://x/y.ts"}),
          "http://x/y.ts")

    # Filters must compose, and the MATCH parameter has to bind in the right
    # position -- getting that wrong silently searches for the wrong thing.
    with tempfile.TemporaryDirectory() as tmp:
        conn = db.connect(Path(tmp) / "items.db")
        db.apply_schema(conn)
        conn.execute("INSERT INTO sources (id,name,kind,created_at) VALUES (1,'S','m3u',0)")
        conn.execute("INSERT INTO categories (id,source_id,kind,remote_id,name,item_count) "
                     "VALUES (10,1,'vod','g','Films',2)")
        rows = [
            (1, "vod", "a", "The Matrix", 10),
            (1, "vod", "b", "Matrix Reloaded", 10),
            (1, "live", "c", "Matrix News", None),
            (1, "vod", "d", "Something Else", None),
        ]
        for sid, kind, rid, name, cat in rows:
            conn.execute("INSERT INTO items (source_id,kind,remote_id,name,category_id) "
                         "VALUES (?,?,?,?,?)", (sid, kind, rid, name, cat))
        conn.commit()

        page = items.query_items(conn, 1, text="matr")
        check("prefix search finds both", page["total"], 3)
        check("empty query returns everything", items.query_items(conn, 1)["total"], 4)
        check("kind filter composes with search",
              items.query_items(conn, 1, text="matr", kind="vod")["total"], 2)
        check("category filter composes",
              items.query_items(conn, 1, kind="vod", category_id=10)["total"], 2)
        check("unmatched search returns nothing",
              items.query_items(conn, 1, text="zzzz")["total"], 0)

        # A bare "%" must be a literal, not "match everything".
        check("LIKE wildcards are escaped",
              items.query_items(conn, 1, text="%%%")["total"], 0)

        counts = items.counts_by_kind(conn, 1)
        check("counts by kind", (counts["live"], counts["vod"], counts["all"]), (1, 3, 4))

        # Regression guard for a genuinely catastrophic bug. Writing the
        # search as a plain JOIN let SQLite drive from `items` and probe the
        # FTS index once per row -- 20 seconds against a 313k catalog instead
        # of 3 ms. The plan must keep the FTS scan on the outside.
        plan = " ".join(
            row["detail"] for row in conn.execute(
                "EXPLAIN QUERY PLAN "
                "SELECT i.id FROM items_fts f CROSS JOIN items i "
                "WHERE items_fts MATCH ? AND i.id = f.rowid AND i.source_id = 1",
                ('"matrix"*',),
            )
        )
        check_true("FTS drives the search plan",
                   "SCAN f VIRTUAL TABLE" in plan)
        check_true("items is probed by primary key, not scanned",
                   "SEARCH i USING INTEGER PRIMARY KEY" in plan)

        paged = items.query_items(conn, 1, limit=2, offset=0)
        check("page size honoured", len(paged["items"]), 2)
        check("has_more set", paged["has_more"], True)
        conn.close()


# --------------------------------------------------------------- worker queue
def test_worker_queue() -> None:
    section("worker queue")
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "queue.db"
        db.init_db(path)
        conn = db.connect(path)
        first = jobs.enqueue(conn, "sync", {"source_id": 7},
                             priority=20, dedupe_key="sync:7")
        second = jobs.enqueue(conn, "sync", {"source_id": 7},
                              priority=20, dedupe_key="sync:7")
        check("dedupe collapses an active sync", second["id"], first["id"])
        check("dedupe reports it was not created", second["_created"], False)

        claimed = jobs.claim(conn)
        check("one job can be claimed", claimed["id"], first["id"])
        check("claim marks it running", claimed["status"], "running")
        check("a busy worker cannot claim a second job", jobs.claim(conn), None)

        jobs.update_progress(conn, first["id"], message="Parsing",
                             progress_data={"phase": "parse", "items_done": 12})
        running = jobs.get(conn, first["id"])
        check("progress survives in the row", running["progress_data"]["items_done"], 12)

        jobs.recover(conn)
        recovered = jobs.get(conn, first["id"])
        check("restart requeues a running job", recovered["status"], "queued")

        jobs.cancel(conn, first["id"])
        cancelled = jobs.get(conn, first["id"])
        check("a cancelled queued job becomes terminal immediately",
              cancelled["status"], "cancelled")
        check("a cancelled job is not claimable", jobs.claim(conn), None)

        replacement = jobs.enqueue(conn, "sync", {"source_id": 7},
                                   dedupe_key="sync:7")
        check("cancel releases the dedupe key", replacement["_created"], True)
        jobs.finish(conn, replacement["id"], "done", result={"items_done": 1})

        jobs.worker_started(conn, 1234)
        jobs.heartbeat(conn, status="idle")
        status = jobs.worker_status(conn)
        check("worker heartbeat is visible", status["alive"], True)
        check("worker heartbeat records idle", status["status"], "idle")
        counts = jobs.counts(conn)
        check("job counts include the finished jobs", counts["done"], 1)
        check("job counts include the cancelled job", counts["cancelled"], 1)
        conn.close()


# ---------------------------------------------------------------------- web
def test_web() -> None:
    """Render the real app through Flask's test client.

    This exists because everything else here can pass while the app still
    500s on every page: a rename in a helper used only by the context
    processor is invisible to unit-level checks but breaks every render.
    """
    section("web app")
    with tempfile.TemporaryDirectory() as tmp:
        import os

        os.environ["IPTV_CHARTS_DATA_DIR"] = tmp
        os.environ["IPTV_CHARTS_CONFIG_DIR"] = tmp
        try:
            from iptv_charts.app import create_app

            app = create_app()
            app.config["TESTING"] = False  # let real error handling run
            client = app.test_client()

            response = client.get("/")
            check("GET / returns 200", response.status_code, 200)
            check_true("homepage actually rendered",
                       b"No playlists yet" in response.data)

            response = client.get("/api/health")
            check("GET /api/health returns 200", response.status_code, 200)

            # There is deliberately no token auth: this is a local-only app and
            # a token only gets in the way of using it. Writes must therefore
            # be reachable without extra ceremony.
            response = client.post("/api/anything")
            check("POST is not blocked by auth", response.status_code != 403, True)

            # The one guard that remains: a Host header naming something other
            # than loopback is what DNS rebinding looks like.
            response = client.get("/", base_url="http://evil.example.com")
            check("non-loopback Host rejected", response.status_code, 403)
        finally:
            os.environ.pop("IPTV_CHARTS_DATA_DIR", None)
            os.environ.pop("IPTV_CHARTS_CONFIG_DIR", None)


# ------------------------------------------------------------ manager foundations
def test_manager_foundations() -> None:
    section("quality profiles")
    check("4K tag wins over 1080p", quality.parse_resolution("Movie [2160p] [1080p]"), "2160p")
    check("4K-Q bracket recognised",
          quality.parse_resolution("ES - Un talento único [[4K-Q]-Q] (2026)"), "2160p")
    check("FHD is 1080p", quality.parse_resolution("Movie [FHD]"), "1080p")
    check("no tag is unknown", quality.parse_resolution("Movie (2024)"), "unknown")
    check("height maps to tier", quality.resolution_from_height(1080), "1080p")
    check("2160 height maps to tier", quality.resolution_from_height(2160), "2160p")
    check("scope 4K uses width",
          quality.resolution_from_dimensions(3840, 1600), "2160p")
    check("WEB-DL source recognised",
          quality.parse_source("Movie (2024) [2160p] WEB-DL"), "web-dl")
    check("CAM source recognised",
          quality.parse_source("Movie (2024) HDCAM"), "cam")
    check("a title containing cam is not a CAM",
          quality.parse_source("Camelot (2024) [1080p]"), "unknown")

    def probed(resolution="1080p", audio=("eng",), subtitles=()):
        return {
            "ok": True, "resolution": resolution,
            "audio": [{"language": code} for code in audio],
            "subtitles": [{"language": code} for code in subtitles],
            "audio_languages": list(audio),
            "subtitle_languages": list(subtitles),
            "audio_track_count": len(audio),
            "subtitle_track_count": len(subtitles),
            "size": 1000,
        }

    # The engine now has one mandatory resolution (minimum score 10). Source
    # is deliberately ignored because provider metadata is unreliable.
    profile = quality.Profile(
        resolution="1080p",
        resolution_weight=10,
        target_score=10,
        upgrades=True,
    )

    def release(resolution, source, **extra):
        return {"resolution": resolution, "source": source, "score": 1.0,
                "probe": probed(resolution), **extra}

    exact = release("1080p", "web-dl")
    fallback = release("720p", "web-dl")
    higher = release("2160p", "web-dl")
    cam = release("1080p", "cam")
    untagged = {"resolution": "1080p", "score": 1.0, "probe": probed("1080p")}

    check("exact resolution is eligible", profile.allows(exact), True)
    check("exact candidate meets the target",
          profile.evaluate(exact)["target_met"], True)
    check("lower resolution is an upgrade-eligible fallback",
          profile.evaluate(fallback)["needs_upgrade"], True)
    check("lower fallback does not meet the target",
          profile.evaluate(fallback)["target_met"], False)
    check("higher resolution is rejected", profile.allows(higher), False)
    check("a CAM source is ignored", profile.allows(cam), True)
    check("an untagged source is ignored", profile.allows(untagged), True)
    check("missing file takes the exact target over a fallback",
          quality.choose_release([fallback, exact], profile)["resolution"], "1080p")
    no_upgrades = quality.Profile(
        resolution="1080p", resolution_weight=10,
        target_score=10, upgrades=False,
    )
    check("upgrades disabled rejects a lower fallback",
          no_upgrades.allows(fallback), False)
    check("upgrades disabled means no replacement",
          quality.choose_release([fallback, exact], no_upgrades,
                                 current="720p"), None)

    # Optional sections add weight only when the probe shows a preferred
    # track: a probed release can outscore a plain one and clear a higher
    # target without any preference being a hard filter.
    weighted = quality.Profile(
        resolution="1080p", resolution_weight=10,
        audio_codecs=(quality.Option("eac3", "E-AC-3", 6),),
        target_score=16, upgrades=True,
    )
    eac3_exact = release("1080p", "web-dl", audio_codecs=["eac3"])
    check("probe codec weight raises the score",
          weighted.score(eac3_exact), 16)
    check("plain release misses a target that needs the probe weight",
          weighted.evaluate(exact)["target_met"], False)
    check("probed release meets that target",
          weighted.evaluate(eac3_exact)["target_met"], True)
    current_fallback = {"resolution": "720p", "source": "web-dl",
                        "media_info": probed("720p")}
    upgraded = quality.choose_release(
        [exact, eac3_exact, higher], weighted, current=current_fallback)
    check("a strictly higher-scoring exact release replaces the fallback",
          upgraded["audio_codecs"], ["eac3"])
    check("a target-met current file stops upgrades",
          quality.choose_release(
              [eac3_exact], weighted,
              current={"resolution": "1080p", "source": "web-dl",
                       "audio_codecs": ["eac3"],
                       "media_info": probed("1080p")}), None)
    equal_score = quality.Profile(
        resolution="1080p", resolution_weight=10,
        target_score=10, upgrades=True,
    )
    check("an exact-resolution equal score upgrades a lower fallback",
          quality.choose_release([exact], equal_score,
                                 current=current_fallback)["resolution"],
          "1080p")
    target_over_score = quality.Profile(
        resolution="1080p", resolution_weight=10,
        audio_codecs=(quality.Option("eac3", "E-AC-3", 6),),
        target_score=10, upgrades=True,
    )
    chosen = quality.choose_release(
        [release("720p", "web-dl", audio_codecs=["eac3"]), exact],
        target_over_score,
    )
    check("a target-met exact release wins over a better-scoring fallback",
          chosen["resolution"], "1080p")

    # Audio/subtitle preferences are additive, not hard requirements: a
    # release without them is still allowed, it just does not reach the target.
    preferred = quality.Profile(
        resolution="1080p", resolution_weight=10,
        audio_languages=(quality.Option("original", "Original language", 5),),
        subtitle_languages=(quality.Option("eng", "English", 5),),
        target_score=20, upgrades=True,
    )
    without_tracks = release("1080p", "web-dl", audio_languages=["spa"])
    with_tracks = release("1080p", "web-dl", audio_languages=["spa"],
                          subtitle_languages=["eng"])
    check("original-language audio adds weight",
          preferred.score(without_tracks, "es"), 15)
    check("required-language subtitle adds weight",
          preferred.score(with_tracks, "es"), 20)
    check("missing preferred tracks is still eligible",
          preferred.allows(without_tracks, "es"), True)
    check("preferred tracks are needed to meet the target",
          preferred.evaluate(without_tracks, "es")["target_met"], False)
    check("release with preferred tracks meets the target",
          preferred.evaluate(with_tracks, "es")["target_met"], True)

    validated = quality.validate_profile({
        "name": "Cinema",
        "resolution": "2160p",
        "resolution_weight": 12,
        "quality_options": [
            {"value": "web-dl", "weight": 10},
            {"value": "bluray", "weight": 8},
        ],
        "audio_codecs": [{"value": "eac3", "weight": 5}],
        "audio_languages": ["original"],
        "target_score": 22,
        "upgrades": True,
    })
    check("custom profile source choices are ignored",
          validated["quality_options"], [])
    check("custom profile target stored", validated["target_score"], 22)
    check("custom profile maximum computed", validated["max_score"], 22)
    check("custom profile criteria is versioned",
          validated["criteria"]["version"], 1)
    check("custom profile audio codec stored",
          validated["criteria"]["audio_codecs"],
          [{"value": "eac3", "label": "E-AC-3 / Dolby Digital Plus", "weight": 5}])
    check("profile rejects an unknown resolution",
          raises(quality.validate_profile,
                 {"resolution": "nonsense", "quality_options": ["web-dl"]},
                 exc=ValueError), "raised")
    check("profile accepts a resolution-only definition",
          raises(quality.validate_profile, {"resolution": "1080p"},
                 exc=ValueError), "returned")
    check("profile ignores duplicate legacy source options",
          quality.validate_profile({
              "resolution": "1080p",
              "quality_options": ["web-dl", "web-dl"],
          })["quality_options"], [])
    check("profile rejects a maximum below the target floor",
          raises(quality.validate_profile,
                 {"resolution": "1080p", "resolution_weight": 5},
                 exc=ValueError), "raised")
    check("profile rejects a target below 10",
          raises(quality.validate_profile,
                 {"resolution": "1080p", "target_score": 9},
                 exc=ValueError), "raised")

    from_row = quality.Profile.from_row({
        "name": validated["name"],
        "resolution": validated["resolution"],
        "resolution_weight": validated["resolution_weight"],
        "quality_options": json.dumps(validated["quality_options"]),
        "criteria_json": validated["criteria_json"],
        "target_score": validated["target_score"],
        "upgrades": 1, "builtin_key": None, "is_builtin": 0, "revision": 1,
    })
    check("criteria round-trip keeps the audio codec",
          [(option.value, option.weight) for option in from_row.audio_codecs],
          [("eac3", 5)])
    check("criteria round-trip keeps the audio language",
          [(option.value, option.weight) for option in from_row.audio_languages],
          [("original", 5)])
    check("criteria round-trip keeps resolution and target",
          (from_row.resolution, from_row.target_score, from_row.max_score),
          ("2160p", 22, 22))
    legacy_row = quality.Profile.from_row({
        "qualities": '[{"name":"2160p","resolution":"2160p","sources":[]}]',
        "resolutions": '["2160p"]', "cutoff": "2160p", "upgrades": 1,
        "audio_mode": "any", "audio_languages": "[]",
        "subtitle_mode": "required", "subtitle_languages": '["original"]',
        "probe_required": 1,
    })
    check("a legacy row still reads its resolution",
          legacy_row.resolution, "2160p")
    check("a legacy row still reads its subtitle preference",
          [option.value for option in legacy_row.subtitle_languages],
          [languages.ORIGINAL])

    section("languages, probes and connection limits")
    check("TMDB language folds to ffprobe code", languages.normalise("en"), "eng")
    check("terminologic French folds to B code", languages.normalise("fra"), "fre")
    check("regional Portuguese folds", languages.normalise("pt-BR"), "por")
    check("language names are understood", languages.normalise("English"), "eng")
    check("unknown language tag is ignored", languages.normalise("und"), None)
    check("original resolves through metadata",
          languages.resolve("original", "es"), "spa")
    check("header injection is refused",
          streams.clean_header("X-Test", "ok\r\nX-Evil: 1"), None)

    probe_payload = {
        "streams": [
            {"index": 0, "codec_type": "video", "codec_name": "hevc",
             "width": 3840, "height": 1600, "color_transfer": "smpte2084"},
            {"index": 1, "codec_type": "audio", "codec_name": "eac3",
             "channels": 6, "tags": {"language": "eng"}},
            {"index": 2, "codec_type": "audio", "codec_name": "aac",
             "channels": 2, "tags": {"language": "spa"}},
            {"index": 3, "codec_type": "subtitle", "codec_name": "subrip",
             "tags": {"language": "eng"}},
        ],
        "format": {"size": "123456", "duration": "7200.5",
                   "format_name": "matroska,webm"},
    }
    normalised = probe.normalise(probe_payload)
    check("probe uses width for scope 4K", normalised["resolution"], "2160p")
    check("probe detects HDR10", normalised["hdr"], "HDR10")
    check("probe lists audio languages", normalised["audio_languages"], ["eng", "spa"])
    check("probe lists subtitle languages", normalised["subtitle_languages"], ["eng"])
    check("probe counts audio tracks", normalised["audio_track_count"], 2)
    check("probe reads size", normalised["size"], 123456)
    check("probe lists video codecs", normalised["video_codecs"], ["hevc"])
    check("probe lists audio codecs", normalised["audio_codecs"], ["eac3", "aac"])
    check("probe lists subtitle codecs", normalised["subtitle_codecs"], ["subrip"])

    # Cached probes written before the aggregate fields existed must still
    # expose codec and language facts to the candidate browser.
    legacy = probe.apply({}, {
        "ok": True,
        "resolution": "1080p",
        "size": 42,
        "video": [{"codec": "h264"}],
        "audio": [{"codec": "aac", "language": "eng"},
                  {"codec": "ac3", "language": "spa"}],
        "subtitles": [{"codec": "subrip", "language": "eng"}],
    })
    check("legacy probe gains video codecs", legacy["video_codecs"], ["h264"])
    check("legacy probe gains audio codecs", legacy["audio_codecs"], ["aac", "ac3"])
    check("legacy probe gains subtitle codecs", legacy["subtitle_codecs"], ["subrip"])
    check("legacy probe gains track counts", legacy["audio_track_count"], 2)
    check("legacy probe keeps language facts",
          legacy["audio_languages"], ["eng", "spa"])
    hls = probe.apply({"size": 12345}, {
        "ok": True, "size": None, "video": [{"codec": "h264"}],
    })
    check("HLS probe keeps the catalog size hint", hls["size"], 12345)

    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "connections.db"
        db.init_db(path)
        conn = db.connect(path)
        conn.execute(
            "INSERT INTO movies (tmdb_id, title, added_at, resolution, "
            "quality_label) VALUES (999001, 'Label repair', 0, '1080p', "
            "'1080p Unknown')"
        )
        conn.commit()
        db.repair_unknown_quality_labels(conn)
        repair_row = conn.execute(
            "SELECT quality_label FROM movies WHERE tmdb_id = 999001"
        ).fetchone()
        check("legacy unknown source label repaired",
              repair_row["quality_label"], "1080p")
        conn.execute("DELETE FROM movies WHERE tmdb_id = 999001")
        conn.execute(
            "INSERT INTO sources (id, name, kind, created_at, connection_limit, "
            "max_connections) VALUES (1, 'limited', 'xtream', 0, 1, 4)"
        )
        conn.execute(
            "INSERT INTO sources (id, name, kind, created_at, max_connections) "
            "VALUES (2, 'provider', 'xtream', 0, 2)"
        )
        conn.execute(
            "INSERT INTO sources (id, name, kind, created_at) "
            "VALUES (3, 'unknown', 'm3u', 0)"
        )
        conn.commit()
        check("user limit wins over provider", connections.effective_limit(conn, 1), 1)
        check("provider limit used when no override",
              connections.effective_limit(conn, 2), 2)
        check("unknown provider limit defaults to one",
              connections.effective_limit(conn, 3), 1)
        check("first connection acquired", connections.try_acquire(1, conn), True)
        check("second connection refused at limit one",
              connections.try_acquire(1, conn), False)
        check("active count visible", connections.active(1), 1)
        connections.release(1)
        check("release frees the slot", connections.try_acquire(1, conn), True)
        connections.release(1)
        with connections.acquire(conn, 1, purpose="test", timeout=0.5):
            check("context manager holds the slot", connections.active(1), 1)
        check("context manager releases", connections.active(1), 0)
        conn.close()

    section("TMDB read access token")
    valid_token = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.smoke-test-token"
    check("read token accepted", tmdb.validate_read_token(valid_token), valid_token)
    check("Bearer prefix is tolerated",
          tmdb.validate_read_token(f"Bearer {valid_token}"), valid_token)
    check("v3 API key rejected",
          raises(tmdb.validate_read_token, "a" * 32, exc=tmdb.TMDBKeyError), "raised")
    check("non-JWT token rejected",
          raises(tmdb.validate_read_token, "not-a-token", exc=tmdb.TMDBKeyError), "raised")
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "metadata.db"
        db.init_db(path)
        conn = db.connect(path)
        db.set_setting(conn, metadata.TMDB_LEGACY_KEY, valid_token)
        check("legacy JWT setting is still read", metadata.has_tmdb(conn), True)
        db.set_setting(conn, metadata.TMDB_LEGACY_KEY, "a" * 32)
        check("legacy v3 key is not accepted", metadata.has_tmdb(conn), False)
        db.set_setting(conn, metadata.TMDB_KEY, valid_token)
        check("new read-token setting is accepted", metadata.has_tmdb(conn), True)
        conn.close()

    section("playlist credential changes")
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "edit.db"
        db.init_db(path)
        conn = db.connect(path)
        source_id = sources.create(
            conn, "xtream", "Edit test", base_url="http://old.example",
            username="user", password="pass", connection_limit=2,
        )
        conn.execute(
            "INSERT INTO categories (source_id, kind, remote_id, name) "
            "VALUES (?, 'vod', '1', 'Movies')", (source_id,)
        )
        category_id = conn.execute("SELECT id FROM categories").fetchone()["id"]
        conn.execute(
            "INSERT INTO items (source_id, kind, remote_id, name, category_id) "
            "VALUES (?, 'vod', '100', 'Old Movie', ?)", (source_id, category_id)
        )
        conn.execute(
            "INSERT INTO provider_series_cache (source_id, remote_id, payload, fetched_at) "
            "VALUES (?, '900', '[]', 1)", (source_id,)
        )
        conn.execute(
            "INSERT INTO stream_probes (source_id, provider_kind, provider_remote_id, "
            "episode_stream_id, ok, payload, probed_at) "
            "VALUES (?, 'vod', '100', '', 1, '{}', 1)", (source_id,)
        )
        conn.execute(
            "INSERT INTO downloads (kind, source_id, provider_kind, provider_remote_id, "
            "title, status, requested_at) "
            "VALUES ('movie', ?, 'vod', '100', 'Old Movie', 'queued', 1)",
            (source_id,)
        )
        conn.execute(
            "UPDATE sources SET account_status = 'Active', max_connections = 4, "
            "user_info_json = '{}', last_synced_at = 1, sync_status = 'done' "
            "WHERE id = ?", (source_id,)
        )
        conn.commit()
        check("blank password with changed account is refused",
              raises(sources.update, conn, source_id,
                     base_url="http://new.example", username="user",
                     password="", exc=sources.ProbeError), "raised")
        row, changed = sources.update(
            conn, source_id, base_url="http://new.example",
            username="user2", password="newpass",
        )
        check("credential change is flagged", changed, True)
        check("credential change stores the new base", row["base_url"],
              "http://new.example")
        check("credential change clears items",
              conn.execute("SELECT COUNT(*) AS n FROM items").fetchone()["n"], 0)
        check("credential change clears categories",
              conn.execute("SELECT COUNT(*) AS n FROM categories").fetchone()["n"], 0)
        check("credential change clears provider caches",
              conn.execute("SELECT COUNT(*) AS n FROM provider_series_cache").fetchone()["n"], 0)
        check("credential change clears probes",
              conn.execute("SELECT COUNT(*) AS n FROM stream_probes").fetchone()["n"], 0)
        check("credential change resets the account snapshot", row["max_connections"], None)
        check("credential change queues sync state", row["sync_status"], "never")
        check("credential change cancels queued downloads",
              conn.execute("SELECT status FROM downloads").fetchone()["status"], "cancelled")
        conn.execute(
            "INSERT INTO items (source_id, kind, remote_id, name) "
            "VALUES (?, 'vod', '101', 'Header Movie')", (source_id,)
        )
        conn.commit()
        row, changed = sources.update(conn, source_id, user_agent="NewAgent")
        check("header-only change does not clear the catalog", changed, False)
        check("header-only change keeps items",
              conn.execute("SELECT COUNT(*) AS n FROM items").fetchone()["n"], 1)
        check("header-only change stores the header",
              json.loads(row["request_headers"])["user_agent"], "NewAgent")
        check("non-http playlist URLs are refused",
              raises(sources.normalise_playlist_url, "ftp://example/x.m3u",
                     exc=sources.ProbeError), "raised")
        conn.close()

    section("naming and path safety")
    check("illegal characters removed", naming.sanitize_component('a/b:c*?"'), "abc")
    check("reserved device name escaped", naming.sanitize_component("CON"), "_CON")
    check("empty metadata has a fallback", naming.sanitize_component(""), "Unknown")
    check("episode template renders",
          naming.render_component(
              naming.DEFAULT_EPISODE_FILE,
              {"series": "Dark Hearts", "season": 1, "episode": 2,
               "episode_title": "Pilot", "resolution": "1080p"},
              naming.EPISODE_FILE_TOKENS,
          ),
          "Dark Hearts - S01E02 - Pilot [1080p]")
    check("missing year does not leave empty brackets",
          naming.render_component(
              naming.DEFAULT_MOVIE_FOLDER, {"title": "Wonder", "year": ""},
              naming.MOVIE_FOLDER_TOKENS,
          ),
          "Wonder")
    check("a separator in a template is refused",
          raises(naming.validate_template, "{title}/{year}", naming.MOVIE_FILE_TOKENS,
                 exc=naming.TemplateError), "raised")
    check("an unknown placeholder is refused",
          raises(naming.validate_template, "{bogus}", naming.MOVIE_FILE_TOKENS,
                 exc=naming.TemplateError), "raised")
    check("extension is sanitised", naming.safe_extension("MKV?x"), "mkvx")
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "media"
        root.mkdir()
        check("path outside the media root is refused",
              raises(naming.confine, Path(tmp) / "outside.mkv", root,
                     exc=naming.TemplateError), "raised")

    section("provider name matching")
    wonder = matching.parse_name("EN - Wonder [4K-Q] (2017) JULIA ROBERTS")
    check("year extracted", wonder.year, 2017)
    check("resolution extracted", wonder.resolution, "2160p")
    check("cast noise leaves a matchable title",
          matching.score_name(wonder, ["wonder"], 2017), 1.0)
    supernatural = matching.parse_name("GR - Supernatural [1080p]")
    check("provider names without a year parse", supernatural.year, None)
    check("series title still matches",
          matching.score_name(supernatural, ["supernatural"], None), 1.0)
    check("episode marker parsed", matching.episode_marker("Show S02E03"), (2, 3))
    check("title that is a year survives",
          matching.parse_name("1917 (2019)").title_variants[0], "1917")
    not_a_year = matching.parse_name("Blade Runner 2049 (2017)")
    check("a number in the title is not the release year", not_a_year.year, 2017)
    check("the title keeps the number", not_a_year.title_variants[0], "blade runner 2049")

    section("catalog-wide matching")
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "catalog.db"
        db.init_db(path)
        conn = db.connect(path)
        conn.execute(
            "INSERT INTO sources (id, name, kind, created_at, base_url, username, password) "
            "VALUES (1, 'Fixture', 'xtream', 0, 'http://example', 'u', 'p')"
        )
        conn.executemany(
            "INSERT INTO items (source_id, kind, remote_id, name, container_ext) "
            "VALUES (1, ?, ?, ?, ?)",
            [
                ("vod", "60001", "The Matrix (1999) [1080p]", "mp4"),
                ("vod", "60002", "The Matrix (1999) [2160p]", "mkv"),
                ("vod", "60003", "The Matrix S01E01 [1080p]", "mp4"),
                ("series", "70001", "GR - Supernatural [1080p]", None),
            ],
        )
        conn.commit()
        found = items.search_catalog(conn, "vod", "the matrix", limit=20)
        check("catalog-wide FTS finds both releases", len(found), 3)
        movie_row = {"title": "The Matrix", "original_title": "The Matrix",
                     "alt_titles": "[]", "year": 1999}
        candidates = matching.movie_candidates(conn, movie_row, min_score=0.5)
        check("movie matching drops episode-shaped VODs", len(candidates), 2)
        check("candidates are ordered best resolution first", candidates[0]["resolution"], "2160p")
        series_row = {"title": "Supernatural", "original_title": "Supernatural",
                      "alt_titles": "[]", "year": None}
        series_candidates = matching.series_candidates(conn, series_row, min_score=0.5)
        check("series matching handles a missing year", len(series_candidates), 1)
        conn.close()

    section("schema migration")
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "catalog.db"
        db.init_db(path)
        conn = db.connect(path)
        conn.execute("ALTER TABLE items DROP COLUMN request_headers")
        # Simulate a version 1 database: the version 2 columns and the queue
        # index did not exist yet, and schema.sql runs before migration.
        conn.execute("DROP INDEX IF EXISTS idx_downloads_queue")
        conn.execute("ALTER TABLE downloads DROP COLUMN priority")
        for column in ("candidate_source_id", "candidate_remote_id",
                       "candidate_mode", "candidate_name", "candidate_selected_at",
                       "main_source_id", "main_remote_id"):
            conn.execute(f"ALTER TABLE movies DROP COLUMN {column}")
        conn.execute("ALTER TABLE downloads DROP COLUMN mode")
        conn.execute("DROP TABLE IF EXISTS movie_alternates")
        conn.execute("ALTER TABLE sources DROP COLUMN connection_limit")
        conn.execute("ALTER TABLE quality_profiles DROP COLUMN qualities")
        # Strip the v5 fields as well, so the migration is what restores them
        # rather than schema.sql having created the fresh table with them.
        conn.execute("DROP INDEX IF EXISTS idx_quality_profiles_builtin")
        for column in ("builtin_key", "max_score", "target_score",
                       "resolution_weight", "revision", "updated_at"):
            conn.execute(f"ALTER TABLE quality_profiles DROP COLUMN {column}")
        conn.execute("DROP INDEX IF EXISTS idx_items_kind_imdb")
        conn.execute("ALTER TABLE items DROP COLUMN imdb_id")
        for column in ("file_score", "candidate_batch_size",
                       "probe_batch_size", "search_interval_hours"):
            conn.execute(f"ALTER TABLE movies DROP COLUMN {column}")
        conn.execute("ALTER TABLE series_episodes DROP COLUMN file_score")
        for column in ("score", "score_details"):
            conn.execute(f"ALTER TABLE downloads DROP COLUMN {column}")
        conn.execute("DROP TABLE IF EXISTS bworker_policies")
        conn.execute("DROP TABLE IF EXISTS bworker_state")
        # A monitored movie with no profile yet: v5 must point it at the
        # default 1080p builtin and seed its durable search work row.
        conn.execute(
            "INSERT INTO movies (tmdb_id, title, added_at, monitored) "
            "VALUES (990001, 'Migration Fixture', 0, 1)"
        )
        conn.execute("PRAGMA user_version = 0")
        conn.execute("DELETE FROM quality_profiles")
        conn.execute("DELETE FROM task_state")
        conn.commit()
        conn.close()
        db.init_db(path)
        conn = db.connect(path)
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(items)")}
        source_columns = {row["name"] for row in conn.execute("PRAGMA table_info(sources)")}
        download_columns = {row["name"] for row in conn.execute(
            "PRAGMA table_info(downloads)"
        )}
        profile_columns = {row["name"] for row in conn.execute(
            "PRAGMA table_info(quality_profiles)"
        )}
        movie_columns = {row["name"] for row in conn.execute(
            "PRAGMA table_info(movies)"
        )}
        episode_columns = {row["name"] for row in conn.execute(
            "PRAGMA table_info(series_episodes)"
        )}
        tables = {row["name"] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        )}
        indexes = {row["name"] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'index'"
        )}
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        profiles = conn.execute("SELECT COUNT(*) AS n FROM quality_profiles").fetchone()["n"]
        tasks = conn.execute("SELECT COUNT(*) AS n FROM task_state").fetchone()["n"]
        tiers = conn.execute(
            "SELECT COUNT(*) AS n FROM quality_profiles WHERE qualities IS NOT NULL"
        ).fetchone()["n"]
        builtin_keys = {
            row["builtin_key"] for row in conn.execute(
                "SELECT builtin_key FROM quality_profiles"
            )
        }
        criteria = [
            json.loads(row["criteria_json"]) for row in conn.execute(
                "SELECT criteria_json FROM quality_profiles"
            )
        ]
        settings = {
            row["key"]: row["value"] for row in conn.execute(
                "SELECT key, value FROM settings"
            )
        }
        policies = conn.execute(
            "SELECT COUNT(*) AS n FROM bworker_policies"
        ).fetchone()["n"]
        policy_types = {
            row["work_type"] for row in conn.execute(
                "SELECT work_type FROM bworker_policies"
            )
        }
        bworker_states = conn.execute(
            "SELECT COUNT(*) AS n FROM bworker_state"
        ).fetchone()["n"]
        movie = conn.execute(
            "SELECT quality_profile_id FROM movies WHERE tmdb_id = 990001"
        ).fetchone()
        default_profile = conn.execute(
            "SELECT id FROM quality_profiles WHERE builtin_key = '1080p'"
        ).fetchone()
        work_keys = {
            row["work_key"] for row in conn.execute(
                "SELECT work_key FROM bworker_work"
            )
        }
        conn.close()
        check("migration adds the request_headers column", "request_headers" in columns, True)
        check("migration adds the connection limit", "connection_limit" in source_columns, True)
        check("migration adds download priority", "priority" in download_columns, True)
        check("migration adds the worker cancel flag",
              "cancel_requested" in download_columns, True)
        check("migration adds the queue index", "idx_downloads_queue" in indexes, True)
        check("migration keeps the legacy profile fields",
              {"qualities", "audio_mode", "subtitle_mode", "probe_required"}
              <= profile_columns, True)
        check("migration adds the weighted profile fields",
              {"builtin_key", "is_builtin", "resolution", "resolution_weight",
               "target_score", "max_score", "revision", "updated_at",
               "criteria_json"} <= profile_columns, True)
        check("migration adds the item identity column", "imdb_id" in columns, True)
        check("migration adds movie scoring and batch fields",
              {"file_score", "candidate_batch_size", "probe_batch_size",
               "search_interval_hours"} <= movie_columns, True)
        check("migration adds the episode score", "file_score" in episode_columns, True)
        check("migration adds download scores",
              {"score", "score_details"} <= download_columns, True)
        check("migration creates the probe cache", "stream_probes" in tables, True)
        check("migration creates the worker queue", "worker_jobs" in tables, True)
        check("migration creates worker state", "worker_state" in tables, True)
        check("migration creates the bworker work table", "bworker_work" in tables, True)
        check("migration creates bworker events", "bworker_events" in tables, True)
        check("migration creates bworker state", "bworker_state" in tables, True)
        check("migration creates bworker policies", "bworker_policies" in tables, True)
        check("migration creates bworker scanning tables",
              {"bworker_scan", "bworker_candidates"} <= tables, True)
        check("migration adds movie candidate pin fields",
              {"candidate_source_id", "candidate_remote_id", "candidate_mode"}
              <= movie_columns, True)
        check("migration adds download mode", "mode" in download_columns, True)
        check("migration creates additional movie versions",
              "movie_alternates" in tables, True)
        check("migration creates the worker dedupe index",
              "idx_worker_jobs_dedupe" in indexes, True)
        check("migration creates the builtin profile index",
              "idx_quality_profiles_builtin" in indexes, True)
        check("migration creates the catalogue identity indexes",
              {"idx_items_kind_tmdb", "idx_items_kind_imdb"} <= indexes, True)
        check("schema version recorded", version, db.SCHEMA_VERSION)
        check("migration seeds the three builtins", profiles, 3)
        check("migration backfills legacy tier columns", tiers, 3)
        check("migration seeds the canonical builtin keys", builtin_keys,
              {"4k", "1080p", "720p"})
        check("migration writes versioned criteria",
              sorted({entry.get("version") for entry in criteria}), [1])
        check("migration points the movie at the default profile",
              movie["quality_profile_id"], default_profile["id"])
        check("migration seeds durable movie work",
              "movie:1:search" in work_keys, True)
        check("migration seeds every bworker policy", policies,
              len(db.DEFAULT_POLICIES))
        check("migration seeds the movie search policy",
              "movie_search" in policy_types, True)
        check("migration seeds bworker singleton state", bworker_states, 1)
        check("migration defaults the external player to off",
              settings.get("external_player_enabled"), "0")
        check("migration defaults the profile key",
              settings.get("default_profile_key"), "1080p")
        check("migration seeds task state", tasks, 4)


def main() -> int:
    test_config()
    test_db()
    test_m3u()
    test_xtream()
    test_streaming()
    test_items()
    test_player()
    test_player_bridge()
    test_manager_foundations()
    test_worker_queue()
    test_web()

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
