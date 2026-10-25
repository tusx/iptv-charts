#!/usr/bin/env python3
"""Section-owned settings pages and APIs (0.8.0 settings split).

Covers what moved off the global Settings page: external playback on
Playlists, movie and series naming/default/search pages, and the shared
bworker task-policy editor. Offline and media-player free: PATH is pointed at
an empty directory so detection cannot depend on what the developer has
installed.

Run with:  .venv/bin/python tests/settings_sections.py
"""

from __future__ import annotations

import os
import re
import sys
import tempfile
import time
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="iptv-charts-sections-"))
_EMPTY_BIN = _TMP / "bin"
_EMPTY_BIN.mkdir()
os.environ["IPTV_CHARTS_DATA_DIR"] = str(_TMP / "data")
os.environ["IPTV_CHARTS_CONFIG_DIR"] = str(_TMP / "config")
os.environ["IPTV_CHARTS_CACHE_DIR"] = str(_TMP / "cache")
os.environ["IPTV_CHARTS_MEDIA_DIR"] = str(_TMP / "media")
os.environ.pop("IPTV_CHARTS_PLAY_QUEUE", None)
os.environ["IPTV_CHARTS_SCHEDULER"] = "0"
# No mpv/VLC/flatpak: the page must still render and the external-player
# default must be off independently of what happens to be installed.
os.environ["PATH"] = str(_EMPTY_BIN)

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from iptv_charts import bworker_store as store, db, player  # noqa: E402
from iptv_charts.app import create_app  # noqa: E402
from iptv_charts.settings_sections import bp as settings_sections_bp  # noqa: E402

RESULTS: list[tuple[bool, str, str]] = []


def check(label: str, got, want) -> None:
    ok = got == want
    RESULTS.append((ok, label, f"{got!r}" if ok else f"{got!r} != {want!r}"))


def check_true(label: str, got) -> None:
    check(label, bool(got), True)


def section(name: str) -> None:
    print(f"\n\033[1m{name}\033[0m")


def page(response) -> str:
    return response.get_data(as_text=True)


def body(response) -> dict:
    return response.get_json(silent=True) or {}


def setting(key: str, default=None):
    conn = db.connect()
    try:
        return db.get_setting(conn, key, default)
    finally:
        conn.close()


_REMOTE_ASSET = re.compile(
    r'(?:<link\b[^>]*\bhref|<script\b[^>]*\bsrc)\s*=\s*["\']\s*(?:https?:)?//',
    re.IGNORECASE,
)


# ----------------------------------------------------------------- global page
def test_global_page(client) -> None:
    section("global settings page keeps only global sections")
    response = client.get("/settings")
    check("global settings status", response.status_code, 200)
    text = page(response)

    for marker in (
        'id="player-settings"',
        'id="player-command"',
        'id="player-choices"',
        'id="media-settings"',
        'id="movie-folder-template"',
        'id="download-settings"',
        'id="download-attempts"',
        'id="probes-clear"',
        'id="search-interval"',
        'id="settings-preview-card"',
    ):
        check_true(f"moved control is gone from /settings: {marker}",
                   marker not in text)

    for marker in (
        'id="settings-form"',
        'id="metadata-settings"',
        'id="tmdb-token"',
        'id="metadata-language"',
        'id="scheduler-settings"',
        'id="refresh-interval"',
        'id="catalog-interval"',
        'id="settings-save"',
        'id="profiles-settings"',
        'id="scan-settings"',
        'id="backup-settings"',
    ):
        check_true(f"global control remains: {marker}", marker in text)

    check("exactly one save button in the global form",
          len(re.findall(r'type="submit"', text)), 1)
    check_true("IPTV settings link points at the playlist section",
               'href="/playlists#iptv-settings"' in text)
    check_true("movie settings link", 'href="/movies/settings"' in text)
    check_true("series settings link", 'href="/series/settings"' in text)
    check("no remote CSS/JS on the global page", _REMOTE_ASSET.findall(text), [])

    # A saved provider secret is never rendered again; the page only says that
    # one is configured. The save response likewise carries booleans, not keys.
    token = "eyJ" + "s" * 80
    tvdb_key = "tvdb-secret-key-value"
    tvdb_pin = "tvdb-secret-pin-value"
    response = client.post("/api/settings/save", json={
        "metadata": {
            "tmdb_read_token": token,
            "tvdb_api_key": tvdb_key,
            "tvdb_pin": tvdb_pin,
            "language": "en-US",
        },
        "scheduler": {
            "refresh_interval_hours": 168,
            "catalog_refresh_hours": 24,
        },
    })
    check("global settings save status", response.status_code, 200)
    saved = body(response)
    check("global save reports providers", saved.get("metadata"), {
        "tmdb": True, "tvdb": True, "language": "en-US",
    })
    serialized = page(response)
    for secret in (token, tvdb_key, tvdb_pin):
        check_true("save response does not echo a secret", secret not in serialized)
    after = page(client.get("/settings"))
    for secret in (token, tvdb_key, tvdb_pin):
        check_true("saved secret is not rendered again", secret not in after)
    check_true("configured TMDB placeholder is shown",
               "Configured — leave blank to keep it" in after)

    # One bad field means the whole save is refused; the valid scheduler value
    # in the same payload must not be written.
    before = setting("refresh_interval_hours")
    response = client.post("/api/settings/save", json={
        "metadata": {"language": "not a language"},
        "scheduler": {"refresh_interval_hours": 42, "catalog_refresh_hours": 24},
    })
    check("bad global field is refused", response.status_code, 400)
    check("bad global field names its section", body(response).get("section"),
          "metadata")
    check("bad global save does not partially write",
          setting("refresh_interval_hours"), before)


# ------------------------------------------------------------------------ IPTV
def test_iptv_settings(client) -> None:
    section("IPTV settings live on /playlists and start disabled")
    response = client.get("/playlists")
    check("playlists page status", response.status_code, 200)
    text = page(response)
    check_true("IPTV settings block rendered", "data-iptv-settings" in text)
    check_true("section anchor exists", 'id="iptv-settings"' in text)
    check_true("player controls are disabled until switched on",
               re.search(r'id="iptv-player-fields"[^>]*\bdisabled\b', text)
               is not None)
    toggle = re.search(r'<input[^>]*id="iptv-external-enabled"[^>]*>', text)
    check_true("external-player toggle is rendered", toggle is not None)
    check_true("external-player toggle is unchecked",
               toggle is not None and "checked" not in toggle.group(0))
    save_button = re.search(
        r'<button[^>]*id="iptv-settings-save"[^>]*>', text)
    check_true("IPTV save waits for the loaded snapshot",
               save_button is not None and "disabled" in save_button.group(0))
    check_true("IPTV form loads from the section API",
               'data-load-url="/api/settings/sections/iptv"' in text)
    check_true("IPTV form saves through the section API",
               'data-save-url="/api/settings/sections/iptv"' in text)
    check_true("IPTV page loads its local script",
               'src="/static/js/iptv_settings.js"' in text)
    check_true("IPTV page loads the section stylesheet",
               'href="/static/css/settings_sections.css"' in text)
    check("no remote CSS/JS on playlists", _REMOTE_ASSET.findall(text), [])

    response = client.get("/api/settings/sections/iptv")
    check("IPTV snapshot status", response.status_code, 200)
    snapshot = body(response)
    check("external player is off by default",
          snapshot.get("external_player_enabled"), False)
    check("preferred player defaults to auto", snapshot.get("preferred_player"),
          "auto")
    check("stale-after defaults to 24 hours", snapshot.get("stale_after_hours"),
          "24")
    check("no custom command is configured", snapshot.get("custom_ready"), False)
    check("no local players are detected in this test", snapshot.get("players"),
          [])

    conn = db.connect()
    try:
        check_true("resolve() is gated off by default",
                   player.resolve(conn) is None)
    finally:
        conn.close()

    section("IPTV save, validation and atomicity")
    command = "mpv --title={title} {url}"
    response = client.post("/api/settings/sections/iptv", json={
        "player": {
            "enabled": True,
            "preferred_player": "custom",
            "player_command": command,
        },
        "playlist": {"stale_after_hours": 12},
    })
    check("IPTV save status", response.status_code, 200)
    saved = body(response)
    check("IPTV save reports the toggle", saved.get("external_player_enabled"),
          True)
    check("IPTV save resolves the custom player",
          (saved.get("player") or {}).get("id"), "custom")
    check("external-player gate persisted",
          setting("external_player_enabled"), "1")
    check("custom command persisted", setting("player_command"), command)
    check("stale-after persisted", setting("stale_after_hours"), "12")

    snapshot = body(client.get("/api/settings/sections/iptv"))
    check("snapshot reports enabled playback",
          snapshot.get("external_player_enabled"), True)
    check("snapshot reports a ready custom command",
          snapshot.get("custom_ready"), True)
    check("snapshot returns the saved command",
          snapshot.get("player_command"), command)

    response = client.post("/api/settings/sections/iptv", json={
        "player": {
            "enabled": True,
            "preferred_player": "custom",
            "player_command": "",
        },
        "playlist": {"stale_after_hours": 18},
    })
    check("enabling custom without a command is refused",
          response.status_code, 400)
    check("custom-without-command error names the player section",
          body(response).get("section"), "player")
    check("refused save keeps the stale value",
          setting("stale_after_hours"), "12")
    check("refused save keeps playback enabled",
          setting("external_player_enabled"), "1")
    check("refused save keeps the existing command",
          setting("player_command"), command)

    response = client.post("/api/settings/sections/iptv", json={
        "player": {
            "enabled": False,
            "preferred_player": "auto",
            "player_command": "",
        },
        "playlist": {"stale_after_hours": 0},
    })
    check("out-of-range stale-after is refused", response.status_code, 400)
    check("bad stale-after names the playlist section",
          body(response).get("section"), "playlist")
    check("bad stale-after does not disable the player",
          setting("external_player_enabled"), "1")
    check("bad stale-after does not clear the command",
          setting("player_command"), command)

    response = client.post("/api/settings/sections/iptv", json={
        "player": {"enabled": "maybe"},
    })
    check("non-boolean toggle is refused", response.status_code, 400)
    check("non-boolean toggle names the player section",
          body(response).get("section"), "player")

    response = client.post("/api/settings/sections/iptv", json={
        "player": {"enabled": False, "preferred_player": "video-game-console"},
    })
    check("unknown player is refused", response.status_code, 400)
    check("unknown player names the player section",
          body(response).get("section"), "player")

    response = client.post("/api/settings/sections/iptv", json={
        "player": {"enabled": False, "player_command": 'mpv "unclosed'},
    })
    check("unparsable command is refused", response.status_code, 400)
    check("unparsable command names the player section",
          body(response).get("section"), "player")

    section("custom command check")
    response = client.post("/api/settings/sections/iptv/command-check", json={
        "player_command": "mpv --title={title} {url}",
    })
    check("command check status", response.status_code, 200)
    checked = body(response)
    check("command check parses argv without a shell",
          checked.get("argv"),
          ["mpv", "--title={title}", "{url}"])
    check_true("command check says nothing ran",
               "Nothing was run" in str(checked.get("message")))
    for label, command in (
        ("empty command", ""),
        ("unclosed quote", 'mpv "unclosed'),
        ("placeholder as program", "{url} --flag"),
    ):
        response = client.post(
            "/api/settings/sections/iptv/command-check",
            json={"player_command": command})
        check(f"command check refuses {label}", response.status_code, 400)


# ------------------------------------------------------------ section defaults
def test_section_page_defaults(client) -> None:
    section("movies/series section pages render the contract defaults")
    response = client.get("/movies/settings")
    check("movie settings status", response.status_code, 200)
    movie = page(response)
    check_true("movie page has its nav context",
               "settings-section-page" in movie and "Settings</a> / Movies" in movie)
    check_true("movie page has the viewport meta",
               'name="viewport" content="width=device-width, initial-scale=1"' in movie)
    check_true("movie page loads the local section script",
               'src="/static/js/settings_sections.js"' in movie)
    check_true("movie page loads the section stylesheet",
               'href="/static/css/settings_sections.css"' in movie)
    check("no remote CSS/JS on the movie page", _REMOTE_ASSET.findall(movie), [])
    check_true("movie form declares its section",
               'data-settings-section="movies"' in movie)
    check_true("movie form save URL",
               'data-save-url="/api/settings/sections/movies"' in movie)
    check_true("movie form preview URL",
               'data-preview-url="/api/settings/sections/movies/preview"' in movie)
    for marker in (
        'id="movie-naming-settings"',
        'data-setting="movie_folder_template"',
        'data-setting="movie_file_template"',
        'id="movie-naming-preview"',
        'id="movie-defaults-settings"',
        'data-setting="default_profile_key"',
        'data-setting="default_minimum_availability"',
        'id="movie-search-settings"',
        'data-setting="movie_search_interval_hours"',
        'data-setting="candidate_batch_size"',
        'data-setting="probe_batch_per_loop"',
        'id="movie-settings-save"',
        'id="policy-form"',
    ):
        check_true(f"movie page hook: {marker}", marker in movie)
    check_true("movie folder default template",
               'value="{title} ({year})"' in movie)
    check_true("movie file default template",
               'value="{title} ({year}) [{resolution}]"' in movie)
    check_true("1080p is preselected",
               re.search(r'<option value="1080p"[^>]*selected', movie) is not None)
    check_true("released is the minimum-availability default",
               re.search(r'<option value="released"[^>]*selected', movie) is not None)
    check_true("movie search defaults to 6 hours",
               re.search(r'data-setting="movie_search_interval_hours"[^>]*'
                         r'value="6"', movie) is not None)
    check_true("candidate batch defaults to 25",
               re.search(r'data-setting="candidate_batch_size"[^>]*value="25"',
                         movie) is not None)
    check_true("probes default to 3 per iteration",
               re.search(r'data-setting="probe_batch_per_loop"[^>]*value="3"',
                         movie) is not None)
    check("one save button inside the movie settings form",
          len(re.findall(r'type="submit"',
                         re.search(r'<form[^>]*id="movie-settings-form".*?</form>',
                                   movie, re.DOTALL).group(0))), 1)

    response = client.get("/series/settings")
    check("series settings status", response.status_code, 200)
    series = page(response)
    check_true("series page has its nav context",
               "settings-section-page" in series and "Settings</a> / Series" in series)
    check_true("series form declares its section",
               'data-settings-section="series"' in series)
    check_true("series form save URL",
               'data-save-url="/api/settings/sections/series"' in series)
    check_true("series form preview URL",
               'data-preview-url="/api/settings/sections/series/preview"' in series)
    for marker in (
        'id="series-naming-settings"',
        'data-setting="series_folder_template"',
        'data-setting="season_folder_template"',
        'data-setting="episode_file_template"',
        'id="series-naming-preview"',
        'id="series-defaults-settings"',
        'data-setting="default_profile_key"',
        'data-setting="default_monitor_mode"',
        'data-setting="default_monitor_new_seasons"',
        'id="series-search-settings"',
        'data-setting="series_search_interval_hours"',
        'data-setting="candidate_batch_size"',
        'data-setting="probe_batch_per_loop"',
        'id="series-settings-save"',
        'id="policy-form"',
    ):
        check_true(f"series page hook: {marker}", marker in series)
    check_true("series folder default template",
               'value="{title} ({year})"' in series)
    check_true("season folder default template",
               'value="Season {season:00}"' in series)
    check_true("episode file default template",
               ('value="{series} - S{season:00}E{episode:00} - '
                '{episode_title} [{resolution}]"') in series)
    check_true("series profile defaults to 1080p",
               re.search(r'<option value="1080p"[^>]*selected', series) is not None)
    check_true("monitor mode defaults to all",
               re.search(r'<option value="all"[^>]*selected', series) is not None)
    check_true("new-season policy defaults to all",
               re.search(r'<option value="all"[^>]*selected[^>]*>[^<]*\n?\s*'
                         'Monitor all newly discovered episodes', series) is not None)
    check_true("series search defaults to 4 hours",
               re.search(r'data-setting="series_search_interval_hours"[^>]*'
                         r'value="4"', series) is not None)
    check_true("candidate batch defaults to 25 on series",
               re.search(r'data-setting="candidate_batch_size"[^>]*value="25"',
                         series) is not None)
    check_true("probes default to 3 on series",
               re.search(r'data-setting="probe_batch_per_loop"[^>]*value="3"',
                         series) is not None)
    check("one save button inside the series settings form",
          len(re.findall(r'type="submit"',
                         re.search(r'<form[^>]*id="series-settings-form".*?</form>',
                                   series, re.DOTALL).group(0))), 1)

    section("responsive section-page hooks")
    css = (ROOT / "iptv_charts/static/css/settings_sections.css").read_text(
        encoding="utf-8")
    check_true("section page width is bounded", ".settings-section-page" in css)
    check_true("tablet breakpoint downgrades the policy grid",
               "@media (max-width: 980px)" in css
               and ".policy-controls { grid-template-columns: repeat(2, "
                   "minmax(0, 1fr)); }" in css)
    check_true("phone breakpoint stacks policy rows",
               "@media (max-width: 720px)" in css
               and re.search(r"\.policy-row\s*\{\s*grid-template-columns:\s*1fr",
                             css) is not None)
    check_true("phone breakpoint stacks policy controls",
               re.search(r"\.policy-controls\s*\{\s*grid-template-columns:\s*1fr",
                         css) is not None)
    check_true("section links stack on a phone",
               re.search(r"\.settings-link-grid\s*\{\s*grid-template-columns:\s*1fr",
                         css) is not None)


# --------------------------------------------------------------------- movies
def test_movie_settings(client) -> None:
    section("movie naming preview")
    response = client.post("/api/settings/sections/movies/preview", json={
        "templates": {
            "movie_folder_template": "{title} - {year}",
            "movie_file_template": "{title} ({year}) [{resolution}] {source}",
        },
    })
    check("movie preview status", response.status_code, 200)
    preview = body(response).get("preview") or {}
    check("movie folder preview", preview.get("movie_folder"),
          "Example Movie - 2024")
    check("movie file preview", preview.get("movie_file"),
          "Example Movie (2024) [1080p] WEB-DL")
    check("preview does not save the folder template",
          setting("movie_folder_template"), "{title} ({year})")
    response = client.post("/api/settings/sections/movies/preview", json={
        "templates": {"movie_folder_template": "{title} {bogus}"},
    })
    check("invalid movie preview is refused", response.status_code, 400)
    check("invalid movie preview names the naming section",
          body(response).get("section"), "naming")

    section("movie settings save and validation")
    response = client.post("/api/settings/sections/movies", json={
        "naming": {
            "movie_folder_template": "{title} - {year}",
            "movie_file_template": "{title} ({year}) {resolution} {source}",
        },
        "defaults": {
            "default_profile_key": "4k",
            "default_minimum_availability": "announced",
        },
        "search": {
            "movie_search_interval_hours": 12.5,
            "candidate_batch_size": 40,
            "probe_batch_per_loop": 2,
        },
    })
    check("movie settings save status", response.status_code, 200)
    saved = body(response)
    check("movie save reports the rendered preview",
          (saved.get("preview") or {}).get("movie_file"),
          "Example Movie (2024) 1080p WEB-DL")
    check("movie folder saved", setting("movie_folder_template"),
          "{title} - {year}")
    check("movie file saved", setting("movie_file_template"),
          "{title} ({year}) {resolution} {source}")
    check("movie default profile saved", setting("default_profile_key"), "4k")
    check("movie minimum availability saved",
          setting("default_minimum_availability"), "announced")
    check("movie search interval saved (fractional)",
          setting("movie_search_interval_hours"), "12.5")
    check("movie candidate batch saved", setting("candidate_batch_size"), "40")
    check("movie probe batch saved", setting("probe_batch_per_loop"), "2")

    movie = page(client.get("/movies/settings"))
    check_true("saved movie folder is shown",
               'value="{title} - {year}"' in movie)
    check_true("saved 4K default is selected",
               re.search(r'<option value="4k"[^>]*selected', movie) is not None)
    check_true("saved announced default is selected",
               re.search(r'<option value="announced"[^>]*selected', movie) is not None)

    # A partial save only writes what it was given.
    response = client.post("/api/settings/sections/movies", json={
        "search": {"movie_search_interval_hours": 7},
    })
    check("partial movie save status", response.status_code, 200)
    check("partial movie save only writes one key",
          body(response).get("saved"), ["movie_search_interval_hours"])
    check("partial movie save leaves naming alone",
          setting("movie_folder_template"), "{title} - {year}")
    check("partial movie save leaves probes alone",
          setting("probe_batch_per_loop"), "2")

    before_folder = setting("movie_folder_template")
    for label, payload, section_name in (
        ("unknown placeholder",
         {"naming": {"movie_folder_template": "{title} {bogus}"}}, "naming"),
        ("empty template",
         {"naming": {"movie_file_template": ""}}, "naming"),
        ("path separator",
         {"naming": {"movie_folder_template": "{title}/{year}"}}, "naming"),
        ("unordered template object",
         {"naming": "not-an-object"}, "naming"),
        ("unknown profile",
         {"defaults": {"default_profile_key": "custom-profile"}}, "defaults"),
        ("unknown availability",
         {"defaults": {"default_minimum_availability": "someday"}}, "defaults"),
        ("zero interval",
         {"search": {"movie_search_interval_hours": 0}}, "search"),
        ("interval over a year",
         {"search": {"movie_search_interval_hours": 8761}}, "search"),
        ("zero candidate batch",
         {"search": {"candidate_batch_size": 0}}, "search"),
        ("candidate batch over the cap",
         {"search": {"candidate_batch_size": 201}}, "search"),
        ("zero probes",
         {"search": {"probe_batch_per_loop": 0}}, "search"),
        ("probe cap exceeded",
         {"search": {"probe_batch_per_loop": 4}}, "search"),
    ):
        response = client.post("/api/settings/sections/movies", json=payload)
        check(f"movie save refuses {label}", response.status_code, 400)
        check(f"movie refusal names {section_name} for {label}",
              body(response).get("section"), section_name)

    # Valid naming plus an invalid interval: nothing is written.
    response = client.post("/api/settings/sections/movies", json={
        "naming": {"movie_folder_template": "{title}"},
        "search": {"movie_search_interval_hours": 0},
    })
    check("invalid movie interval rejects the whole save",
          response.status_code, 400)
    check("rejected movie save keeps the old folder",
          setting("movie_folder_template"), before_folder)


# --------------------------------------------------------------------- series
def test_series_settings(client) -> None:
    section("series naming preview")
    response = client.post("/api/settings/sections/series/preview", json={
        "templates": {
            "series_folder_template": "{title} ({year})",
            "season_folder_template": "Season {season:00}",
            "episode_file_template": ("{series} - S{season:00}E{episode:00} - "
                                      "{episode_title} [{resolution}] {source}"),
        },
    })
    check("series preview status", response.status_code, 200)
    preview = body(response).get("preview") or {}
    check("series folder preview", preview.get("series_folder"),
          "Example Series (2024)")
    check("season folder preview", preview.get("season_folder"), "Season 01")
    check("episode file preview", preview.get("episode_file"),
          "Example Series - S01E02 - Pilot [1080p] WEB-DL")
    check("preview does not save the season template",
          setting("season_folder_template"), "Season {season:00}")
    response = client.post("/api/settings/sections/series/preview", json={
        "templates": {"series_folder_template": "{title} {quality}"},
    })
    check("invalid series preview is refused", response.status_code, 400)
    check("invalid series preview names the naming section",
          body(response).get("section"), "naming")

    section("series settings save and validation")
    response = client.post("/api/settings/sections/series", json={
        "naming": {
            "series_folder_template": "{title} ({year})",
            "season_folder_template": "Season {season:00}",
            "episode_file_template": ("{series} S{season:00}E{episode:00} "
                                      "{episode_title} [{resolution}]"),
        },
        "defaults": {
            "default_profile_key": "720p",
            "default_monitor_mode": "latest",
            "default_monitor_new_seasons": "future",
        },
        "search": {
            "series_search_interval_hours": 8.5,
            "candidate_batch_size": 30,
            "probe_batch_per_loop": 1,
        },
    })
    check("series settings save status", response.status_code, 200)
    saved = body(response)
    check("series save reports the rendered preview",
          (saved.get("preview") or {}).get("episode_file"),
          "Example Series S01E02 Pilot [1080p]")
    check("series folder saved", setting("series_folder_template"),
          "{title} ({year})")
    check("season template saved", setting("season_folder_template"),
          "Season {season:00}")
    check("episode template saved", setting("episode_file_template"),
          "{series} S{season:00}E{episode:00} {episode_title} [{resolution}]")
    check("series default profile saved", setting("default_profile_key"), "720p")
    check("series monitor mode saved", setting("default_monitor_mode"), "latest")
    check("series new-season policy saved",
          setting("default_monitor_new_seasons"), "future")
    check("series search interval saved (fractional)",
          setting("series_search_interval_hours"), "8.5")
    check("series candidate batch saved", setting("candidate_batch_size"), "30")
    check("series probe batch saved", setting("probe_batch_per_loop"), "1")

    series = page(client.get("/series/settings"))
    check_true("saved series episode template is shown",
               ('value="{series} S{season:00}E{episode:00} '
                '{episode_title} [{resolution}]"') in series)
    check_true("saved 720p default is selected",
               re.search(r'<option value="720p"[^>]*selected', series) is not None)
    check_true("saved latest monitor mode is selected",
               re.search(r'<option value="latest"[^>]*selected', series) is not None)
    check_true("saved future new-season policy is selected",
               re.search(r'<option value="future"[^>]*selected[^>]*>[^<]*\n?\s*'
                         'Monitor only future', series) is not None)

    before_episode = setting("episode_file_template")
    for label, payload, section_name in (
        ("unknown series token",
         {"naming": {"episode_file_template": "{series} {network}"}}, "naming"),
        ("slash in series folder",
         {"naming": {"series_folder_template": "{title}/{year}"}}, "naming"),
        ("unknown monitor mode",
         {"defaults": {"default_monitor_mode": "every-other"}}, "defaults"),
        ("unknown new-season policy",
         {"defaults": {"default_monitor_new_seasons": "sometimes"}}, "defaults"),
        ("unknown series profile",
         {"defaults": {"default_profile_key": "custom-profile"}}, "defaults"),
        ("zero series interval",
         {"search": {"series_search_interval_hours": 0}}, "search"),
        ("series interval over a year",
         {"search": {"series_search_interval_hours": 8761}}, "search"),
        ("series batch over the cap",
         {"search": {"candidate_batch_size": 201}}, "search"),
        ("series probe cap exceeded",
         {"search": {"probe_batch_per_loop": 4}}, "search"),
    ):
        response = client.post("/api/settings/sections/series", json=payload)
        check(f"series save refuses {label}", response.status_code, 400)
        check(f"series refusal names {section_name} for {label}",
              body(response).get("section"), section_name)

    response = client.post("/api/settings/sections/series", json={
        "naming": {"episode_file_template": "{series} E{episode:00}"},
        "defaults": {"default_monitor_mode": "every-other"},
    })
    check("invalid series default rejects the whole save",
          response.status_code, 400)
    check("rejected series save keeps the old episode template",
          setting("episode_file_template"), before_episode)


# ------------------------------------------------------------------- policies
def test_policies(client) -> None:
    section("policy editor renders every work type and locks cleanup")
    movie = page(client.get("/movies/settings"))
    series = page(client.get("/series/settings"))
    expected = list(store.WORK_TYPES)
    check("movie page lists every policy in display order",
          re.findall(r'data-work-type="([^"]+)"', movie), expected)
    check("series page lists every policy in display order",
          re.findall(r'data-work-type="([^"]+)"', series), expected)
    check_true("work-type labels are rendered", "Movie search" in movie)
    check_true("reset button is present", 'id="policy-reset"' in movie)
    check_true("policy save button is present", 'id="policy-save"' in movie)

    chunks = re.split(r'(?=data-work-type=")', movie)
    cleanup = next((chunk for chunk in chunks
                    if chunk.startswith('data-work-type="activity_cleanup"')), "")
    check_true("cleanup row is marked locked", 'data-locked="1"' in cleanup)
    check_true("cleanup enabled toggle is checked and disabled",
               re.search(r'data-policy-field="enabled"[^>]*checked[^>]*disabled',
                         cleanup) is not None)
    check_true("cleanup retryforever is checked and disabled",
               re.search(r'data-policy-field="infinite_retry"[^>]*checked'
                         r'[^>]*disabled', cleanup) is not None)
    check_true("cleanup lock note is shown",
               "Always on — cleanup continues while paused." in cleanup)

    section("policy edits are validated and saved")
    response = client.patch("/api/settings/sections/policies", json={
        "policies": [
            {
                "work_type": "movie_search",
                "changes": {
                    "enabled": False,
                    "max_failures": 5,
                    "infinite_retry": False,
                    "retry_delay_seconds": 120,
                    "backoff_multiplier": 1.5,
                    "max_delay_seconds": 600,
                },
            },
            {
                "work_type": "download",
                "changes": {"retry_delay_seconds": 90},
            },
        ],
    })
    check("policy PATCH status", response.status_code, 200)
    result = body(response)
    check("policy PATCH reports two rows", result.get("updated"), 2)
    by_type = {row["work_type"]: row for row in result.get("policies") or []}
    movie_policy = by_type.get("movie_search") or {}
    check("movie search policy is disabled", movie_policy.get("enabled"), False)
    check("movie search max failures saved",
          movie_policy.get("max_failures"), 5)
    check("movie search infinite retry off",
          movie_policy.get("infinite_retry"), False)
    check("movie search delay saved",
          movie_policy.get("retry_delay_seconds"), 120)
    check("movie search backoff saved",
          movie_policy.get("backoff_multiplier"), 1.5)
    check("movie search max delay saved",
          movie_policy.get("max_delay_seconds"), 600)
    check("activity cleanup remains enabled",
          (by_type.get("activity_cleanup") or {}).get("enabled"), True)

    conn = db.connect()
    try:
        stored = store.policy(conn, "movie_search") or {}
        cleanup_row = store.policy(conn, "activity_cleanup") or {}
    finally:
        conn.close()
    check("policy changes are durable", stored.get("retry_delay_seconds"), 120)
    check("cleanup cannot be persisted as disabled",
          cleanup_row.get("enabled"), True)

    # A valid change in the same batch must not be written when another entry
    # fails validation: the dry pass covers the whole request first.
    response = client.patch("/api/settings/sections/policies", json={
        "policies": [
            {"work_type": "movie_search",
             "changes": {"enabled": True}},
            {"work_type": "activity_cleanup",
             "changes": {"enabled": False}},
        ],
    })
    check("cleanup cannot be disabled", response.status_code, 400)
    check("cleanup refusal names the policies section",
          body(response).get("section"), "policies")
    conn = db.connect()
    try:
        still_disabled = not (store.policy(conn, "movie_search") or {})["enabled"]
    finally:
        conn.close()
    check_true("refused policy batch is atomic", still_disabled)

    for label, payload in (
        ("unknown work type",
         {"policies": [{"work_type": "teleport", "changes": {"enabled": True}}]}),
        ("unsupported field",
         {"policies": [{"work_type": "download",
                        "changes": {"secret_flag": True}}]}),
        ("zero max failures",
         {"policies": [{"work_type": "download",
                        "changes": {"max_failures": 0}}]}),
        ("max delay below delay",
         {"policies": [{"work_type": "download",
                        "changes": {"retry_delay_seconds": 300,
                                    "max_delay_seconds": 60}}]}),
        ("backoff over the cap",
         {"policies": [{"work_type": "download",
                        "changes": {"backoff_multiplier": 11}}]}),
        ("empty policy list", {"policies": []}),
    ):
        response = client.patch("/api/settings/sections/policies", json=payload)
        check(f"policy PATCH refuses {label}", response.status_code, 400)
        check(f"policy refusal names policies for {label}",
              body(response).get("section"), "policies")

    section("reset only failed work")
    now = int(time.time())
    conn = db.connect()
    try:
        conn.executemany(
            "INSERT INTO bworker_work (work_key, work_type, entity_type, "
            "status, stage, failure_count, last_error, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                ("test:failed", "movie_search", "global", "failed_stopped",
                 "search", 3, "previous failure", now, now),
                ("test:pending", "movie_search", "global", "pending",
                 "search", 2, None, now, now),
                ("test:complete", "movie_search", "global", "complete",
                 "done", 0, None, now, now),
                ("test:running", "movie_search", "global", "running",
                 "search", 0, None, now, now),
            ],
        )
        conn.commit()
    finally:
        conn.close()

    response = client.post("/api/settings/sections/policies/reset", json={})
    check("failed-work reset status", response.status_code, 200)
    reset = body(response)
    check("reset reports only the failed unit", reset.get("reset"), 1)
    check("reset message counts units", reset.get("message"),
          "Reset 1 failed unit.")

    conn = db.connect()
    try:
        rows = {
            row["work_key"]: dict(row)
            for row in conn.execute(
                "SELECT work_key, status, stage, failure_count, last_error, "
                "next_run_at, reset_requested FROM bworker_work "
                "WHERE work_key LIKE 'test:%'"
            ).fetchall()
        }
    finally:
        conn.close()
    failed = rows.get("test:failed") or {}
    check("failed unit is pending again", failed.get("status"), "pending")
    check("failed unit failure count is cleared",
          failed.get("failure_count"), 0)
    check("failed unit stage is reset", failed.get("stage"), "pending")
    check("failed unit error is cleared", failed.get("last_error"), None)
    check_true("failed unit is due immediately",
               failed.get("next_run_at") is not None)
    check("pending unit is not reset",
          (rows.get("test:pending") or {}).get("failure_count"), 2)
    check("complete unit is not reset",
          (rows.get("test:complete") or {}).get("status"), "complete")
    check("running unit is not touched",
          (rows.get("test:running") or {}).get("status"), "running")
    check("running unit did not get a reset request",
          (rows.get("test:running") or {}).get("reset_requested"), 0)


# --------------------------------------------------------------------- guards
def test_write_guards(client) -> None:
    section("write guard and JSON-only saves")
    response = client.post("/api/settings/sections/movies", json={
        "search": {"movie_search_interval_hours": 9},
    }, headers={"Origin": "http://evil.example"})
    check("cross-origin settings write is refused", response.status_code, 403)

    response = client.post("/api/settings/sections/movies", json={
        "search": {"movie_search_interval_hours": 9},
    }, headers={"Sec-Fetch-Site": "cross-site"})
    check("cross-site settings write is refused", response.status_code, 403)

    response = client.get("/settings", headers={"Host": "evil.example"})
    check("non-loopback Host is refused", response.status_code, 403)

    response = client.post(
        "/api/settings/sections/movies",
        data='{"search": {"movie_search_interval_hours": 9}}',
        content_type="application/x-www-form-urlencoded",
    )
    check("form-encoded section save is refused", response.status_code, 400)
    check_true("form-encoded refusal is JSON",
               body(response).get("ok") is False)


def main() -> int:
    app = create_app()
    # Integration normally registers the blueprint in app.py; keep this suite
    # runnable on its own if that line has not landed yet.
    if "settings_sections.movies_settings" not in app.view_functions:
        app.register_blueprint(settings_sections_bp)
    app.config["TESTING"] = False  # let template errors surface as 500s
    client = app.test_client()

    section("blueprint registration")
    for endpoint in (
        "settings_sections.movies_settings",
        "settings_sections.series_settings",
        "settings_sections.get_iptv_settings",
        "settings_sections.save_task_policies",
    ):
        check_true(f"registered: {endpoint}", endpoint in app.view_functions)

    test_global_page(client)
    test_iptv_settings(client)
    test_section_page_defaults(client)
    test_movie_settings(client)
    test_series_settings(client)
    test_policies(client)
    test_write_guards(client)

    failures = [entry for entry in RESULTS if not entry[0]]
    for ok, label, detail in RESULTS:
        if not ok:
            print(f"  \033[31mFAIL\033[0m {label}: {detail}")
    print(f"\n{len(RESULTS) - len(failures)}/{len(RESULTS)} checks passed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
