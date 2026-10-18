#!/usr/bin/env python3
"""End-to-end tests: the whole app driven through its HTTP API.

Goes one level further than integration.py. That suite talks to the sync
engine directly; this one uses the real Flask application -- real routes, real
templates, real background worker -- so it catches the class of bug where
every unit passes but a page returns 500.

Run with:  .venv/bin/python tests/endtoend.py
"""

from __future__ import annotations

import os
import sys
import tempfile
import time
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="iptv-charts-e2e-"))
os.environ["IPTV_CHARTS_DATA_DIR"] = str(_TMP)
os.environ["IPTV_CHARTS_CONFIG_DIR"] = str(_TMP)
os.environ["IPTV_CHARTS_CACHE_DIR"] = str(_TMP)
os.environ.pop("IPTV_CHARTS_PLAY_QUEUE", None)

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from integration import CHANNEL_COUNT, serve  # noqa: E402
from iptv_charts import db  # noqa: E402
from iptv_charts.app import create_app  # noqa: E402

_results: list[tuple[bool, str, str]] = []


def check(label: str, got, want) -> None:
    ok = got == want
    _results.append((ok, label, f"{got!r}" if ok else f"{got!r} != {want!r}"))


def check_true(label: str, got) -> None:
    check(label, bool(got), True)


def section(name: str) -> None:
    print(f"\n\033[1m{name}\033[0m")


def main() -> int:
    db.init_db()
    server, base_url = serve()

    try:
        app = create_app()
        # Real error handling: a traceback in a template should surface as a
        # 500 in the test, exactly as it would for a user.
        app.config["TESTING"] = False
        client = app.test_client()

        # ---------------------------------------------------------- empty state
        section("initial pages")
        response = client.get("/")
        check("GET / returns 200", response.status_code, 200)
        check_true("home shows the IPTV card", b'id="iptv-card"' in response.data
                   or b"home-card-iptv" in response.data)
        check_true("home shows the Movies card", b"home-card-movies" in response.data)
        check_true("home shows the Series card", b"home-card-series" in response.data)

        response = client.get("/playlists")
        check("GET /playlists returns 200", response.status_code, 200)
        check_true("playlist empty state rendered", b"No playlists yet" in response.data)
        check_true("playlist add button present", b'id="add-open-btn"' in response.data)

        response = client.get("/api/health")
        check("health returns 200", response.status_code, 200)
        check("no sources yet", response.get_json()["sources"], 0)

        response = client.get("/api/sync/status")
        check("sync status returns 200", response.status_code, 200)
        check("nothing running", response.get_json()["active"], False)

        # --------------------------------------------------------------- probe
        section("probing")
        response = client.post("/api/probe", json={
            "kind": "m3u", "m3u_url": f"{base_url}/playlist.m3u",
        })
        payload = response.get_json()
        check("probe succeeds", payload["ok"], True)
        check_true("probe reports channels", payload["result"]["channels_sampled"] > 0)

        response = client.post("/api/probe", json={
            "kind": "m3u", "m3u_url": f"{base_url}/hls.m3u8",
        })
        check("probe rejects an HLS playlist", response.get_json()["ok"], False)

        response = client.post("/api/probe", json={
            "kind": "xtream", "base_url": base_url,
            "username": "baduser", "password": "nope",
        })
        check("probe rejects bad credentials", response.get_json()["ok"], False)

        response = client.post("/api/probe", json={
            "kind": "xtream", "base_url": base_url,
            "username": "gooduser", "password": "goodpass",
        })
        check("probe accepts good credentials", response.get_json()["ok"], True)

        # -------------------------------------------------------------- create
        section("adding a playlist")
        response = client.post("/api/sources", json={
            "kind": "m3u", "name": "E2E playlist",
            "m3u_url": f"{base_url}/playlist.m3u",
        })
        check("create returns 201", response.status_code, 201)
        source_id = response.get_json()["source_id"]
        check_true("source id assigned", source_id)

        # A reload mid-sync must already show the card with its progress bar.
        response = client.get("/")
        check("home page renders with a source", response.status_code, 200)
        check_true("source name shown on home", b"E2E playlist" in response.data)
        check_true("home progress slot present", b"sync-slot" in response.data)

        response = client.get("/playlists")
        check("playlists page renders with a source", response.status_code, 200)
        check_true("source name shown on playlists", b"E2E playlist" in response.data)

        # ---------------------------------------------------------------- sync
        section("sync completes")
        deadline = time.time() + 90
        while time.time() < deadline:
            snapshot = client.get("/api/sync/status").get_json()
            if not snapshot["active"]:
                break
            time.sleep(0.05)

        job = snapshot["jobs"].get(source_id) or snapshot["jobs"].get(str(source_id))
        check_true("job reported", job is not None)
        check("sync succeeded", job["state"], "done")
        check("item count reported", job["items_done"], CHANNEL_COUNT + 1)

        # ------------------------------------------------------- rendered page
        section("rendered page after sync")
        response = client.get("/playlists")
        check("playlists page still 200", response.status_code, 200)
        body = response.data
        # Rendered with a thousands separator, so compare against the
        # formatted form rather than the raw integer.
        check_true(f"live count rendered ({CHANNEL_COUNT:,})",
                   f">{CHANNEL_COUNT:,}<".encode() in body)
        check_true("update button present", b'class="btn btn-ghost js-sync"' in body)
        check_true("remove button present", b"js-delete" in body)
        check_true("stale reminder markup present", b"js-stale" in body)

        response = client.get("/")
        home_body = response.data
        check("home page after sync", response.status_code, 200)
        check_true("home movie card renders", b"home-card-movies" in home_body)
        check_true("home series card renders", b"home-card-series" in home_body)
        check_true("home links to the movie library", b"/movies" in home_body)
        check_true("home links to the series library", b"/series" in home_body)

        # ----------------------------------------------------------- edit page
        section("edit playlist page")
        response = client.get(f"/playlists/{source_id}/edit")
        check("edit page returns 200", response.status_code, 200)
        edit_body = response.data
        check_true("edit page shows the playlist name", b"E2E playlist" in edit_body)
        check_true("edit page shows the M3U URL", b"playlist.m3u" in edit_body)
        check_true("edit page has a save button", b'id="source-save"' in edit_body)
        check_true("edit page never renders a password value", b'id="source-password"' not in edit_body)

        response = client.patch(f"/api/sources/{source_id}", json={
            "name": "E2E playlist renamed",
            "connection_limit": 2,
            "user_agent": "E2E-Agent",
        })
        check("playlist edit accepted", response.status_code, 200)
        check("playlist name updated", response.get_json()["source"]["name"],
              "E2E playlist renamed")
        check("playlist limit updated", response.get_json()["source"]["connection_limit"], 2)
        check("header-only edit does not re-sync", response.get_json()["catalog_changed"], False)

        response = client.post(f"/api/sources/{source_id}/probe", json={
            "m3u_url": f"{base_url}/playlist.m3u",
            "user_agent": "E2E-Agent",
        })
        check("edit-page connection test accepted", response.status_code, 200)
        check("edit-page connection test result", response.get_json()["ok"], True)

        response = client.patch(f"/api/sources/{source_id}", json={
            "connection_limit": None,
        })
        check("playlist limit restored to auto", response.status_code, 200)

        # --------------------------------------------------------------- browse
        section("browse page")
        response = client.get(f"/sources/{source_id}/browse")
        check("browse page returns 200", response.status_code, 200)
        browse_body = response.data
        check_true("search box rendered", b'id="search"' in browse_body)
        check_true("kind tabs rendered", b'id="kind-tabs"' in browse_body)
        check_true("category filter rendered", b'id="category"' in browse_body)
        check_true("sort control rendered", b'id="sort"' in browse_body)

        response = client.get("/sources/99999/browse")
        check("browse of a missing playlist is 404", response.status_code, 404)

        # ----------------------------------------------------------- items API
        section("item search and filters")
        payload = client.get(f"/api/sources/{source_id}/items").get_json()
        check("items returned", payload["ok"], True)
        check("total is everything", payload["total"], CHANNEL_COUNT + 1)
        check("page is capped", len(payload["items"]), 60)
        check("more available", payload["has_more"], True)
        check_true("stream url included for copying",
                   all(item["stream_url"] for item in payload["items"]))

        payload = client.get(f"/api/sources/{source_id}/items?q=ugly").get_json()
        check("search narrows results", payload["total"], 1)
        check("search found the right item", payload["items"][0]["name"],
              "The Good, the Bad and the Ugly")

        # Prefix matching: "matr"-style partial words must work.
        payload = client.get(f"/api/sources/{source_id}/items?q=chan").get_json()
        check("prefix search works", payload["total"], CHANNEL_COUNT)

        payload = client.get(f"/api/sources/{source_id}/items?kind=vod").get_json()
        check("kind filter", payload["total"], 1)

        payload = client.get(f"/api/sources/{source_id}/items?kind=live").get_json()
        check("live filter", payload["total"], CHANNEL_COUNT)

        payload = client.get(f"/api/sources/{source_id}/items?sort=newest").get_json()
        check("sorting does not error", payload["ok"], True)

        # Filters must compose rather than replace one another.
        payload = client.get(
            f"/api/sources/{source_id}/items?q=chan&kind=vod").get_json()
        check("search and kind compose", payload["total"], 0)

        categories = client.get(f"/api/sources/{source_id}/categories").get_json()
        check_true("categories listed", len(categories["categories"]) >= 3)
        check_true("category counts present",
                   all(c["item_count"] > 0 for c in categories["categories"]))

        first_category = categories["categories"][0]
        payload = client.get(
            f"/api/sources/{source_id}/items?category={first_category['id']}").get_json()
        check("category filter works", payload["total"], first_category["item_count"])

        # A query of pure punctuation must not reach FTS and blow up. It
        # legitimately matches nothing, so take the sample item from a
        # separate unfiltered query.
        payload = client.get(f"/api/sources/{source_id}/items?q=%22%22%22").get_json()
        check("punctuation-only query is handled", payload["ok"], True)
        check("punctuation-only query matches nothing", payload["total"], 0)

        sample = client.get(f"/api/sources/{source_id}/items?q=ugly").get_json()

        # ------------------------------------------------------------- player
        section("player dialog and stream resolution")
        item_id = sample["items"][0]["id"]

        # The player is a dialog on the browse page, so everything it needs --
        # video element, engines, script -- has to be on that page rather than
        # on a page of its own.
        player_body = client.get(f"/sources/{source_id}/browse").data
        check_true("player dialog rendered", b'id="player-dialog"' in player_body)
        check_true("video element rendered", b'id="video"' in player_body)
        check_true("episodes section rendered", b'id="episode-list"' in player_body)
        check_true("hls.js vendored and linked", b"vendor/hls.min.js" in player_body)
        check_true("player script linked", b"js/playback.js" in player_body)
        # The browser gets a plain video element now; everything else is the
        # external player's job.
        check_true("no MPEG-TS engine linked", b"mpegts.js" not in player_body)
        check_true("no player overlay rendered", b"player-overlay" not in player_body)

        # The page it replaced is gone, not merely unlinked.
        check("the watch page no longer exists",
              client.get(f"/watch/{item_id}").status_code, 404)
        check("nor for a missing item",
              client.get("/watch/999999").status_code, 404)

        stream = client.get(f"/api/items/{item_id}/stream").get_json()
        check("stream resolves", stream["ok"], True)
        check_true("stream url is absolute", stream["url"].startswith("http"))
        check("m3u item url is its stored url", stream["url"],
              sample["items"][0]["remote_id"])

        # ------------------------------------------------------------- playing
        section("play actions")
        from iptv_charts import player as player_module

        # Replace resolution so the test never launches a real media player on
        # the machine running it. Asserted first because patching a name the
        # code no longer calls is legal Python and would quietly turn this into
        # a test that opens mpv for real.
        check_true("player.resolve is the entry point",
                   hasattr(player_module, "resolve"))
        original_resolve, original_available = player_module.resolve, player_module.available
        player_module.resolve = lambda *a, **k: None
        player_module.available = lambda *a, **k: []
        try:
            response = client.post(f"/api/items/{item_id}/play")
            check("play without a player is a clean 409", response.status_code, 409)
            check_true("error explains what to do",
                       b"bridge" in response.data or b"Settings" in response.data)

            response = client.get(f"/sources/{source_id}/browse")
            check_true("external-player button hidden when there is none",
                       b'id="external-btn"' not in response.data)

            # A machine with nothing installed is the case the settings page has
            # to stay useful for: it is where the fix is entered.
            response = client.get("/settings")
            check("settings page renders with no players", response.status_code, 200)
            check_true("and says what to do about it",
                       b"Nothing can play a stream externally yet" in response.data)
        finally:
            player_module.resolve = original_resolve
            player_module.available = original_available

        response = client.post("/api/items/999999/play")
        check("play of a missing item is 404", response.status_code, 404)

        # ------------------------------------------------------------ settings
        section("settings page")
        response = client.get("/settings")
        check("settings page returns 200", response.status_code, 200)
        settings_body = response.data
        check_true("form rendered", b'id="settings-form"' in settings_body)
        check_true("player choices rendered", b'id="player-choices"' in settings_body)
        check_true("automatic offered", b'value="auto"' in settings_body)
        check_true("custom command offered", b'value="custom"' in settings_body)
        check_true("command field rendered", b'id="player-command"' in settings_body)
        check_true("settings script linked", b"js/settings.js" in settings_body)

        response = client.post("/api/settings", json={
            "preferred_player": "custom",
            "player_command": "mpv --title={title} {url}",
        })
        payload = response.get_json()
        check("saving succeeds", payload["ok"], True)
        check("the choice is recorded", payload["player"],
              {"id": "custom", "label": "Custom command"})
        # The response is the resolved command with placeholders filled in, so
        # the page can show what pressing play will actually do.
        check("expanded exactly as it will run", payload["command"],
              ["mpv", "--title=Example stream", "http://example.com/stream.ts"])

        body = client.get("/settings").data
        check_true("the command round-trips", b"{title}" in body)
        check_true("the choice round-trips", b'value="custom"' in body)

        response = client.post("/api/settings", json={
            "preferred_player": "custom", "player_command": 'mpv "unclosed',
        })
        check("a command that cannot be parsed is refused", response.status_code, 400)
        check_true("with a readable reason",
                   b"cannot be parsed" in response.data)

        response = client.post("/api/settings", json={"preferred_player": "vlc"})
        check("an unknown player is refused", response.status_code, 400)

        # The queue decides what the host runs, so it has to be JSON-only and
        # same-origin: a form-encoded POST needs no preflight and any website
        # could aim one at loopback.
        response = client.post("/api/settings", data={"player_command": "rm -rf /"})
        check("a form-encoded write is refused", response.status_code, 400)
        response = client.post("/api/settings", json={"player_command": "rm -rf /"},
                               headers={"Origin": "http://evil.example.com"})
        check("a cross-site write is refused", response.status_code, 403)
        response = client.post("/api/settings", json={"player_command": "rm -rf /"},
                               headers={"Sec-Fetch-Site": "cross-site"})
        check("so is one the browser labels cross-site", response.status_code, 403)

        # Put it back, so the rest of the run sees a clean slate.
        client.post("/api/settings", json={
            "preferred_player": "auto", "player_command": "",
        })
        body = client.get("/settings").data
        check_true("the command was cleared", b'id="player-command"' in body)
        check_true("one save button rendered", b'id="settings-save"' in body)
        check_true("per-section save buttons removed", b'id="metadata-save"' not in body)

        section("unified settings save")
        response = client.post("/api/settings/save", json={
            "player": {"preferred_player": "auto", "player_command": ""},
            "metadata": {"language": "es-ES"},
            "media": {
                "movie_folder_template": "{title} ({year})",
                "movie_file_template": "{title} ({year}) [{resolution}]",
                "series_folder_template": "{title} ({year})",
                "season_folder_template": "Season {season:00}",
                "episode_file_template":
                    "{series} - S{season:00}E{episode:00} - {episode_title} [{resolution}]",
            },
            "downloads": {
                "concurrency": 1, "max_attempts": 3,
                "probe_cache_hours": 24, "probe_timeout_seconds": 30,
                "probe_max_candidates": 6, "probe_search_budget": 60,
            },
            "scheduler": {
                "search_interval_hours": 6, "refresh_interval_hours": 168,
                "catalog_refresh_hours": 24,
            },
        })
        payload = response.get_json()
        check("unified save succeeds", response.status_code, 200)
        check("unified save returns the language", payload["metadata"]["language"], "es-ES")
        check_true("unified save returns a media preview",
                   "movie_file" in payload["media"]["preview"])
        check_true("unified save returns the resolved player",
                   "player" in payload)

        response = client.post("/api/settings/save", json={
            "metadata": {"language": "fr-FR"},
            "media": {"movie_file_template": "{title}/{year}"},
        })
        check("invalid section rejects the whole save", response.status_code, 400)
        check("error names the failing section", response.get_json()["section"], "media")
        body = client.get("/settings").data
        check_true("an earlier section was not partially saved",
                   b'value="es-ES"' in body and b'value="fr-FR"' not in body)

        response = client.post("/api/settings/save", json={
            "metadata": {"language": "es-ES", "tmdb_api_key": "secret-e2e-key"},
        })
        check("metadata key save accepted", response.status_code, 200)
        check("metadata key reported configured",
              response.get_json()["metadata"]["tmdb"], True)
        check_true("saved key is not echoed", b"secret-e2e-key" not in client.get("/settings").data)
        response = client.post("/api/settings/save", json={
            "metadata": {"tmdb_clear": True},
        })
        check("metadata key cleared", response.status_code, 200)
        check("metadata clear reported", response.get_json()["metadata"]["tmdb"], False)

        response = client.post("/api/settings/save", data={"language": "fr-FR"})
        check("unified settings is JSON-only", response.status_code, 400)
        response = client.post("/api/settings/save", json={"metadata": {"language": "fr-FR"}},
                               headers={"Origin": "http://evil.example.com"})
        check("unified settings rejects cross-site writes", response.status_code, 403)

        # ------------------------------------------------- bridge compatibility
        # A bridge older than the players.json protocol can still play, so
        # upgrading the app must not take the button away from it. This is the
        # case that bit once: a rebuilt container and a bridge that had not been
        # restarted, leaving the browse page with no external-player button.
        section("an older host bridge")
        saved_queue = os.environ.get("IPTV_CHARTS_PLAY_QUEUE")
        with tempfile.TemporaryDirectory() as tmp:
            os.environ["IPTV_CHARTS_PLAY_QUEUE"] = f"{tmp}/queue"
            (Path(tmp) / "play-bridge.alive").write_text("0")
            player_module._CANDIDATES = None
            try:
                response = client.get(f"/sources/{source_id}/browse")
                check_true("the external-player button survives the upgrade",
                           b'id="external-btn"' in response.data)
                response = client.get("/settings")
                check_true("and the settings page says why the choice is limited",
                           b"predates this version" in response.data)
            finally:
                if saved_queue is None:
                    os.environ.pop("IPTV_CHARTS_PLAY_QUEUE", None)
                else:
                    os.environ["IPTV_CHARTS_PLAY_QUEUE"] = saved_queue
                player_module._CANDIDATES = None

        # ---------------------------------------------------------- re-sync all
        section("update all")
        response = client.post("/api/sources/sync-all")
        check("sync-all accepted", response.get_json()["ok"], True)

        deadline = time.time() + 90
        while time.time() < deadline:
            snapshot = client.get("/api/sync/status").get_json()
            if not snapshot["active"]:
                break
            time.sleep(0.05)

        job = snapshot["jobs"].get(source_id) or snapshot["jobs"].get(str(source_id))
        check("re-sync succeeded", job["state"], "done")

        # ------------------------------------------------------------ deletion
        section("removing a playlist")
        response = client.delete(f"/api/sources/{source_id}")
        check("delete returns 200", response.status_code, 200)

        response = client.get("/")
        check_true("back to the empty state", b"No playlists yet" in response.data)

        response = client.delete(f"/api/sources/{source_id}")
        check("deleting again is a 404", response.status_code, 404)

        # -------------------------------------------------------------- guards
        section("request guards")
        response = client.get("/")
        check("framing is denied", response.headers.get("X-Frame-Options"), "DENY")
        check("CSP frame ancestors is denied",
              "frame-ancestors 'none'" in (response.headers.get("Content-Security-Policy") or ""),
              True)

        response = client.get("/", base_url="http://evil.example.com")
        check("non-loopback Host rejected", response.status_code, 403)

        response = client.post("/api/sources", json={"kind": "m3u", "m3u_url": ""})
        check("empty URL rejected", response.status_code, 400)

        response = client.post("/api/sources/99999/sync")
        check("sync of a missing playlist is 404", response.status_code, 404)

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
