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

from iptv_charts import config, db, player  # noqa: E402
from iptv_charts.clients import m3u, xtream  # noqa: E402
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


def main() -> int:
    test_config()
    test_db()
    test_m3u()
    test_xtream()
    test_streaming()
    test_items()
    test_player()
    test_player_bridge()
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
