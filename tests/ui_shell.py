#!/usr/bin/env python3
"""UI shell tests for 0.8.0.

Covers the pieces that live outside any one feature page: the brand-only top
bar, locally vendored Font Awesome/Bootstrap, the icon macro on home buttons,
the missing-count metrics, the external-player off switch and the small-screen
layout hooks.

Deliberately offline and dependency-free: no provider, no TMDB call, no media
player launch. Run with:

    .venv/bin/python tests/ui_shell.py
"""

from __future__ import annotations

import os
import re
import sys
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="iptv-charts-shell-"))
os.environ["IPTV_CHARTS_DATA_DIR"] = str(_TMP / "data")
os.environ["IPTV_CHARTS_CONFIG_DIR"] = str(_TMP / "config")
os.environ["IPTV_CHARTS_CACHE_DIR"] = str(_TMP / "cache")
os.environ["IPTV_CHARTS_MEDIA_DIR"] = str(_TMP / "media")
os.environ.pop("IPTV_CHARTS_PLAY_QUEUE", None)
os.environ["IPTV_CHARTS_SCHEDULER"] = "0"

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from iptv_charts import db, metadata, player, sources  # noqa: E402
from iptv_charts.app import create_app  # noqa: E402

_results: list[tuple[bool, str, str]] = []


def check(label: str, got, want) -> None:
    ok = got == want
    _results.append((ok, label, f"{got!r}" if ok else f"{got!r} != {want!r}"))


def check_true(label: str, got) -> None:
    check(label, bool(got), True)


def section(name: str) -> None:
    print(f"\n\033[1m{name}\033[0m")


# --------------------------------------------------------------- file helpers
VENDOR = ROOT / "iptv_charts" / "static" / "vendor"
TEMPLATES = ROOT / "iptv_charts" / "templates"
SHELL_CSS = ROOT / "iptv_charts" / "static" / "css" / "shell.css"

#: An external stylesheet, script, font or @import. Relative and url_for
#: paths are fine; a scheme or protocol-relative URL is not. Ordinary <a>
#: links (for example the TMDB API settings documentation) are not assets and
#: are deliberately not matched.
_REMOTE_ASSET = re.compile(
    r"(?:<link\b[^>]*\bhref|<script\b[^>]*\bsrc)\s*=\s*[\"']\s*(?:https?:)?//",
    re.IGNORECASE,
)
_REMOTE_CSS_URL = re.compile(r"(?:url\(|@import\s+)[\"']?\s*(?:https?:)?//", re.IGNORECASE)
_FA_CSS_URL = re.compile(r"url\(([^)]+)\)")


def test_vendored_assets() -> None:
    section("vendored Font Awesome / Bootstrap")
    fa_css = VENDOR / "fontawesome" / "css" / "all.min.css"
    check_true("Font Awesome CSS is vendored", fa_css.is_file())
    css_text = fa_css.read_text(encoding="utf-8")
    check_true("Font Awesome version pinned at 6.7.2",
               "Font Awesome Free 6.7.2" in css_text)

    referenced = {url.strip("'\" ") for url in _FA_CSS_URL.findall(css_text)}
    check_true("Font Awesome CSS only references local files",
               all(not url.startswith(("http:", "https:", "//")) for url in referenced))
    for name in ("fa-solid-900.woff2", "fa-regular-400.woff2", "fa-brands-400.woff2"):
        font = VENDOR / "fontawesome" / "webfonts" / name
        check_true(f"{name} is vendored", font.is_file())
        check_true(f"{name} is a WOFF2 font",
                   font.is_file() and font.read_bytes()[:4] == b"wOF2")
        check_true(f"{name} is referenced by the CSS", f"../webfonts/{name}" in referenced)
    check_true("Font Awesome license is present",
               (VENDOR / "fontawesome" / "LICENSE.txt").is_file())

    bs_css = VENDOR / "bootstrap" / "css" / "bootstrap.min.css"
    bs_js = VENDOR / "bootstrap" / "js" / "bootstrap.bundle.min.js"
    check_true("Bootstrap CSS is vendored", bs_css.is_file())
    check_true("Bootstrap bundle JS is vendored", bs_js.is_file())
    check_true("Bootstrap CSS version pinned at 5.3.8",
               bool(re.search(r"Bootstrap\s+v5\.3\.8", bs_css.read_text(encoding="utf-8"))))
    check_true("Bootstrap JS version pinned at 5.3.8",
               bool(re.search(r"Bootstrap\s+v5\.3\.8", bs_js.read_text(encoding="utf-8"))))
    check_true("Bootstrap license is present",
               (VENDOR / "bootstrap" / "LICENSE").is_file())


def test_templates_use_only_local_assets() -> None:
    section("templates: no remote CSS/JS/font assets")
    offenders = []
    for template in sorted(TEMPLATES.glob("*.html")):
        text = template.read_text(encoding="utf-8")
        if _REMOTE_ASSET.search(text) or _REMOTE_CSS_URL.search(text):
            offenders.append(template.name)
    check("no template loads an external asset", offenders, [])


# ------------------------------------------------------------------ app tests
def _header(page: str) -> str:
    match = re.search(r"<header class=\"topbar\">(.*?)</header>", page, re.DOTALL)
    return match.group(1) if match else ""


def _card(page: str, section_class: str) -> str:
    match = re.search(
        rf"<section class=\"home-card {re.escape(section_class)}\">(.*?)</section>",
        page,
        re.DOTALL,
    )
    return match.group(1) if match else ""


def test_external_player_gate(client) -> None:
    section("external player: off by default, still detectable")
    conn = db.connect()
    try:
        check("external_player_enabled defaults to 0",
              db.get_setting(conn, "external_player_enabled"), "0")
        check("setting is read as disabled", player.enabled(conn), False)
        check("resolve returns None while disabled", player.resolve(conn), None)

        # The settings page must still be able to show what could be used.
        db.set_setting(conn, player.COMMAND_KEY, "/bin/echo {url}")
        available = player.available(conn)
        check_true("players are still enumerated while disabled", available)
        check_true("the custom command is enumerated",
                   any(found.id == "custom" for found in available))
        check("resolve is still gated while disabled", player.resolve(conn), None)

        source_id = sources.create(
            conn, "m3u", "Shell fixture", m3u_url="http://example.invalid/list.m3u"
        )

        response = client.get(f"/sources/{source_id}/browse")
        body = response.get_data(as_text=True)
        check("browse page renders", response.status_code, 200)
        check_true("external player button is hidden while disabled",
                   'id="external-btn"' not in body)
        check_true("page is told no external player is available",
                   "EXTERNAL_PLAYER_AVAILABLE = false" in body)

        # Switching it on restores the existing behaviour, unchanged.
        db.set_setting(conn, "external_player_enabled", "1")
        check("setting is read as enabled", player.enabled(conn), True)
        chosen = player.resolve(conn)
        check_true("resolve returns the configured player when enabled", chosen is not None)
        check_true("enabled resolve picks one of the detected players",
                   chosen is not None and chosen.id in {found.id for found in available})

        response = client.get(f"/sources/{source_id}/browse")
        body = response.get_data(as_text=True)
        check_true("external player button returns when enabled",
                   'id="external-btn"' in body)
        check_true("page is told a player is available",
                   "EXTERNAL_PLAYER_AVAILABLE = true" in body)

        # The top bar never shows player status any more, even when enabled.
        page = client.get("/").get_data(as_text=True)
        header = _header(page)
        check_true("top bar has no player status pill", "pill" not in header)
        check_true("top bar does not name a player", "External player" not in header)

        # Anything that is not a truthy switch stays off.
        db.set_setting(conn, "external_player_enabled", "off")
        check("unknown values keep external playback off", player.enabled(conn), False)
        check("resolve is gated for unknown values", player.resolve(conn), None)
        db.set_setting(conn, "external_player_enabled", "0")
    finally:
        conn.close()


def test_topbar_and_home(client) -> None:
    section("top bar: brand only")
    page = client.get("/").get_data(as_text=True)
    header = _header(page)
    check_true("top bar rendered", bool(header))
    check_true("brand is still present", 'class="brand"' in header)
    check_true("brand links home", re.search(r'<a class="brand" href="/"', header) is not None)
    check_true("no nav element in the top bar", "<nav" not in header)
    check_true("no leftover topbar-nav class", "topbar-nav" not in header)
    check("the brand is the only top-bar link", header.count("<a "), 1)
    for label in ("Home", "Playlists", "Movies", "Series", "Activity", "Settings"):
        check(f"no top-bar link named {label}", f">{label}<" in header, False)

    section("home: missing metrics")
    conn = db.connect()
    try:
        db.set_setting(conn, metadata.TMDB_KEY, "eyJ" + "a" * 48)
        conn.execute(
            "INSERT INTO movies (tmdb_id, title, added_at, monitored, file_path) "
            "VALUES (9101, 'Missing Movie', 0, 1, NULL)"
        )
        conn.execute(
            "INSERT INTO movies (tmdb_id, title, added_at, monitored, file_path) "
            "VALUES (9102, 'Downloaded Movie', 0, 1, '/media/have.mkv')"
        )
        series_id = conn.execute(
            "INSERT INTO series (tmdb_id, title, added_at, monitored) "
            "VALUES (9201, 'Shell Show', 0, 1)"
        ).lastrowid
        conn.execute(
            "INSERT INTO series_episodes (series_id, season, episode, air_date, file_path) "
            "VALUES (?, 1, 1, '2000-01-01', NULL)",
            (series_id,),
        )
        conn.execute(
            "INSERT INTO series_episodes (series_id, season, episode, air_date, file_path) "
            "VALUES (?, 1, 2, '2000-01-02', '/media/episode.mkv')",
            (series_id,),
        )
        conn.commit()
    finally:
        conn.close()

    page = client.get("/").get_data(as_text=True)
    movies = _card(page, "home-card-movies")
    series = _card(page, "home-card-series")
    iptv = _card(page, "home-card-iptv")
    check_true("movies card rendered", bool(movies))
    check_true("series card rendered", bool(series))
    check_true("IPTV card rendered", bool(iptv))

    check_true("movies stats include a Missing count",
               re.search(r"<dt>Missing</dt>\s*<dd>1</dd>", movies) is not None)
    check_true("series stats include a Missing episodes count",
               re.search(r"<dt>Missing episodes</dt>\s*<dd>1</dd>", series) is not None)
    check_true("movies subtitle line removed", "downloaded ·" not in movies)
    check_true("series subtitle line removed", "episodes downloaded ·" not in series)

    section("home: icons and action buttons")
    for icon in ("fa-chart-line", "fa-gear"):
        check_true(f"page actions use {icon}", icon in page)
    for icon in ("fa-server", "fa-arrows-rotate", "fa-sliders", "fa-magnifying-glass",
                 "fa-pen-to-square", "fa-xmark", "fa-trash", "fa-gear"):
        check_true(f"IPTV card uses {icon}", icon in iptv)
    for icon in ("fa-film", "fa-plus", "fa-calendar-days", "fa-bullseye", "fa-gear"):
        check_true(f"movies card uses {icon}", icon in movies)
    for icon in ("fa-tv", "fa-plus", "fa-calendar-days", "fa-bullseye", "fa-gear"):
        check_true(f"series card uses {icon}", icon in series)

    check_true("movies Settings button points at the settings page",
               'href="/settings"' in movies)
    check_true("series Settings button points at the settings page",
               'href="/settings"' in series)
    check_true("IPTV Settings button points at the settings page",
               'href="/settings"' in iptv)

    check_true("movies open button keeps its href", 'href="/movies"' in movies)
    check_true("movies add button keeps its href", 'href="/movies?add=1"' in movies)
    check_true("movies calendar button keeps its href",
               'href="/movies/calendar"' in movies)
    check_true("movies wanted button keeps its href", 'href="/movies/wanted"' in movies)
    check_true("series open button keeps its href", 'href="/series"' in series)
    check_true("series add button keeps its href", 'href="/series?add=1"' in series)
    check_true("series calendar button keeps its href",
               'href="/series/calendar"' in series)
    check_true("series wanted button keeps its href", 'href="/series/wanted"' in series)

    check_true("add playlist button keeps its id", 'id="add-open-btn"' in iptv)
    check_true("update-all button keeps its id", 'id="sync-all-btn"' in iptv)
    check_true("batch progress block kept", 'id="batch-progress"' in iptv)
    check("search-missing buttons kept", page.count("data-search-missing"), 2)
    check_true("movies search scope kept", 'data-search-scope="movies"' in movies)
    check_true("series search scope kept", 'data-search-scope="series"' in series)
    check_true("playlist edit link kept",
               re.search(r'href="/playlists/\d+/edit"', iptv) is not None)
    check_true("playlist browse link kept",
               re.search(r'href="/sources/\d+/browse"', iptv) is not None)

    shell_css = SHELL_CSS.read_text(encoding="utf-8")
    check_true("search-missing icon is defined without child nodes",
               '[data-search-missing]::before' in shell_css)


def test_responsive_and_assets(client) -> None:
    section("head: local assets and viewport")
    page = client.get("/").get_data(as_text=True)
    check_true("viewport meta is present",
               '<meta name="viewport" content="width=device-width, initial-scale=1">' in page)
    check_true("Font Awesome is linked locally",
               'href="/static/vendor/fontawesome/css/all.min.css"' in page)
    check_true("app.css is linked", 'href="/static/css/app.css"' in page)
    check_true("shell.css is linked", 'href="/static/css/shell.css"' in page)
    check_true("home JS is local", 'src="/static/js/app.js"' in page)
    check("no remote href/src attributes", re.findall(r'(?:href|src)="https?://[^"]*"', page), [])
    check_true("Bootstrap is vendored but not loaded on home",
               "vendor/bootstrap" not in page)

    section("responsive CSS hooks (360/768)")
    css = SHELL_CSS.read_text(encoding="utf-8")
    check_true("home grid cannot overflow at 360px",
               "minmax(min(100%, 320px), 1fr)" in css)
    check_true("tablet breakpoint at 768px", "@media (max-width: 768px)" in css)
    check_true("phone breakpoint at 400px", "@media (max-width: 400px)" in css)
    check_true("horizontal overflow is blocked on html",
               re.search(r"html\s*\{[^}]*overflow-x:\s*hidden", css, re.DOTALL) is not None)
    check_true("horizontal overflow is blocked on body",
               re.search(r"body\s*\{[^}]*overflow-x:\s*clip", css, re.DOTALL) is not None)
    check_true("home buttons wrap on small screens",
               ".home-card-actions .btn" in css and "flex-basis: 100%" in css)
    check_true("stats reflow to two columns on tablets",
               "repeat(2, minmax(0, 1fr))" in css)


def main() -> int:
    test_vendored_assets()
    test_templates_use_only_local_assets()

    app = create_app()
    app.config["TESTING"] = False  # let template errors surface as 500s
    client = app.test_client()

    test_external_player_gate(client)
    test_topbar_and_home(client)
    test_responsive_and_assets(client)

    failures = [entry for entry in _results if not entry[0]]
    for ok, label, detail in _results:
        if not ok:
            print(f"  \033[31mFAIL\033[0m {label}: {detail}")
    print(f"\n{len(_results) - len(failures)}/{len(_results)} checks passed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
