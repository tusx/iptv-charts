#!/usr/bin/env python3
"""Weighted quality profile tests (0.8.0).

Covers the scoring contract in docs/implementation/0.8.0-contracts.md, probe
fact normalisation, current-file re-scoring, builtin immutability, the default
1080p profile, and the profile CRUD API.

Run with:  .venv/bin/python tests/profiles.py
"""

from __future__ import annotations

import json
import os
import re
import sys
import tempfile
import traceback
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))
_TMP = Path(tempfile.mkdtemp(prefix="iptv-profiles-test-"))
os.environ["IPTV_CHARTS_DATA_DIR"] = str(_TMP / "data")
os.environ["IPTV_CHARTS_CONFIG_DIR"] = str(_TMP / "config")
os.environ["IPTV_CHARTS_CACHE_DIR"] = str(_TMP / "cache")
os.environ["IPTV_CHARTS_MEDIA_DIR"] = str(_TMP / "media")
os.environ["IPTV_CHARTS_SCHEDULER"] = "0"

from iptv_charts import db, library, quality  # noqa: E402

_results: list[tuple[bool, str, str]] = []


def check(label: str, got, want) -> None:
    ok = got == want
    _results.append((ok, label, f"{got!r}" if ok else f"{got!r} != {want!r}"))


def check_true(label: str, got) -> None:
    check(label, bool(got), True)


def section(name: str) -> None:
    print(f"\n\033[1m{name}\033[0m")


def raises(label: str, fn, *, message: str | None = None) -> None:
    try:
        fn()
    except ValueError as exc:
        ok = message is None or message.lower() in str(exc).lower()
        _results.append((ok, label, str(exc) if ok
                         else f"{exc!r} does not contain {message!r}"))
    except Exception as exc:  # noqa: BLE001 - show the wrong exception
        _results.append((False, label, f"raised {type(exc).__name__}: {exc}"))
    else:
        _results.append((False, label, "returned without raising"))


def report() -> int:
    failed = [entry for entry in _results if not entry[0]]
    print(f"\n{len(_results) - len(failed)} checks passed, {len(failed)} failed")
    for _ok, label, detail in failed:
        print(f"  FAIL  {label}: {detail}")
    if failed:
        print(f"\nTemp data: {_TMP}")
    return 1 if failed else 0


# ------------------------------------------------------------------- helpers
def payload(**overrides) -> dict:
    data = {
        "name": "Test profile",
        "resolution": "1080p",
        "resolution_weight": 10,
        "upgrades": True,
    }
    data.update(overrides)
    return data


def build(**overrides) -> quality.Profile:
    return quality.Profile.from_values(quality.validate_profile(payload(**overrides)))


def probe(**overrides) -> dict:
    data = {"ok": True, "resolution": "1080p", "size": 1000}
    data.update(overrides)
    return data


def detail(profile: quality.Profile, release: dict) -> dict:
    return profile.evaluate(release)


def test_builtins(conn) -> None:
    section("builtins, default 1080p and library default helper")
    ids = db.ensure_builtin_profiles(conn)
    check("three builtin keys", sorted(ids), ["1080p", "4k", "720p"])
    rows = {
        row["builtin_key"]: row
        for row in conn.execute("SELECT * FROM quality_profiles")
    }
    check("builtin names", [rows[key]["name"] for key in ("4k", "1080p", "720p")],
          ["4K", "1080p", "720p"])
    check("builtin resolutions",
          [rows[key]["resolution"] for key in ("4k", "1080p", "720p")],
          ["2160p", "1080p", "720p"])
    for key, row in rows.items():
        check(f"{key} is builtin", row["is_builtin"], 1)
        check(f"{key} resolution weight", row["resolution_weight"], 10)
        check(f"{key} target score", row["target_score"], 10)
        check(f"{key} max score", row["max_score"], 10)
        check(f"{key} revision", row["revision"], 1)
        criteria = json.loads(row["criteria_json"])
        check(f"{key} criteria version", criteria["version"], 1)
        check(f"{key} source options removed", criteria["quality_options"], [])
        for optional in (
            "video_codecs", "audio_codecs", "audio_languages",
            "audio_channels", "subtitle_formats", "subtitle_languages",
        ):
            check(f"{key} {optional} empty", criteria[optional], [])

    profile = quality.Profile.from_row(rows["1080p"])
    check("builtin profile resolution", profile.resolution, "1080p")
    check("builtin profile base score", profile.max_score, 10)
    check("builtin profile target", profile.target_score, 10)
    check("builtin profile option count", len(profile.quality_options), 0)
    check("builtin profile immutable flag", profile.is_builtin, True)

    check("default setting", db.get_setting(conn, "default_profile_key"), "1080p")
    check("library default is 1080p", library.default_profile_id(conn),
          ids["1080p"])
    db.set_setting(conn, "default_profile_key", "4k")
    check("library default follows setting", library.default_profile_id(conn),
          ids["4k"])
    db.set_setting(conn, "default_profile_key", "720p")
    check("library default follows 720p", library.default_profile_id(conn),
          ids["720p"])
    db.set_setting(conn, "default_profile_key", "missing")
    check("library default falls back to 1080p",
          library.default_profile_id(conn), ids["1080p"])
    db.set_setting(conn, "default_profile_key", "1080p")

    again = db.ensure_builtin_profiles(conn)
    check("ensure builtins is idempotent", again, ids)


def test_mandatory() -> None:
    section("mandatory eligibility: resolution only, source ignored")
    profile = build(target_score=10)
    exact = detail(profile, {"resolution": "1080p", "source": "web-dl"})
    check("exact resolution eligible", exact["eligible"], True)
    check("base score is the resolution weight", exact["score"], 10)
    check("exact target met", exact["target_met"], True)
    check("exact does not need upgrade", exact["needs_upgrade"], False)

    lower = detail(profile, {"resolution": "720p", "source": "web-dl"})
    check("lower resolution eligible with upgrades", lower["eligible"], True)
    check("lower resolution base score", lower["score"], 10)
    check("lower resolution never target_met", lower["target_met"], False)
    check("lower resolution needs upgrade", lower["needs_upgrade"], True)
    check("lower resolution exact flag", lower["exact_resolution"], False)

    no_upgrades = build(target_score=10, upgrades=False)
    blocked = detail(no_upgrades, {"resolution": "720p", "source": "web-dl"})
    check("lower resolution rejected without upgrades", blocked["eligible"], False)
    check_true("rejection mentions upgrades", "upgrades" in blocked["reason"])

    higher = detail(profile, {"resolution": "2160p", "source": "web-dl"})
    check("higher resolution rejected", higher["eligible"], False)
    check_true("rejection mentions higher", "higher" in higher["reason"])

    unknown = detail(profile, {"resolution": "unknown", "source": "web-dl"})
    check("unknown resolution rejected", unknown["eligible"], False)

    for source in ("cam", "unknown", "web-dl", "bluray"):
        candidate = detail(profile, {"resolution": "1080p", "source": source})
        check(f"source {source} is ignored", candidate["eligible"], True)
        check(f"source {source} does not score", candidate["score"], 10)
        check(f"source {source} has no source weight",
              candidate["source_weight"], 0)

    # Optional matches add to the resolution base; they never make a release
    # eligible when the resolution is outside the profile.
    fancy = build(
        target_score=20,
        video_codecs=[{"value": "hevc", "label": "HEVC", "weight": 10}],
    )
    accepted = detail(fancy, {
        "resolution": "1080p",
        "source": "cam",
        "probe": probe(video=[{"codec": "hevc"}]),
    })
    check("optional match adds to resolution base", accepted["score"], 20)
    rejected = detail(fancy, {
        "resolution": "2160p",
        "source": "cam",
        "probe": probe(resolution="2160p", video=[{"codec": "hevc"}]),
    })
    check("optional match cannot rescue a bad resolution",
          rejected["eligible"], False)
    check("ineligible score is zero", rejected["score"], 0)


def test_target_and_fallback() -> None:
    section("target score, exact vs lower fallback")
    profile = build(
        target_score=20,
        video_codecs=[{"value": "hevc", "label": "HEVC", "weight": 10}],
    )
    exact_low = detail(profile, {"resolution": "1080p", "source": "web-dl"})
    check("exact score below target eligible", exact_low["eligible"], True)
    check("exact below target not target_met", exact_low["target_met"], False)
    exact_high = detail(profile, {
        "resolution": "1080p", "source": "bluray",
        "probe": probe(video=[{"codec": "hevc"}]),
    })
    check("exact at target target_met", exact_high["target_met"], True)
    lower_high = detail(profile, {
        "resolution": "720p", "source": "bluray",
        "probe": probe(resolution="720p", video=[{"codec": "hevc"}]),
    })
    check("lower fallback can reach target score", lower_high["score"], 20)
    check("lower fallback not target_met", lower_high["target_met"], False)
    check("lower fallback flagged for upgrade", lower_high["needs_upgrade"], True)


def test_optional_scoring() -> None:
    section("optional sections: exclusive versus multi-valued")
    profile = build(
        target_score=20,
        video_codecs=[
            {"value": "h264", "label": "H.264", "weight": 7},
            {"value": "hevc", "label": "HEVC", "weight": 10},
        ],
        audio_codecs=[
            {"value": "aac", "label": "AAC", "weight": 7},
            {"value": "eac3", "label": "E-AC-3", "weight": 10},
        ],
        audio_channels=[
            {"value": "stereo", "label": "Stereo", "weight": 3},
            {"value": "5.1", "label": "5.1", "weight": 5},
        ],
        audio_languages=[
            {"value": "eng", "label": "English", "weight": 5},
            {"value": "spa", "label": "Spanish", "weight": 5},
            {"value": "any", "label": "Any", "weight": 5},
        ],
        subtitle_formats=[
            {"value": "srt", "label": "SRT", "weight": 5},
            {"value": "pgs", "label": "PGS", "weight": 4},
        ],
        subtitle_languages=[
            {"value": "eng", "label": "English", "weight": 5},
            {"value": "any", "label": "Any", "weight": 5},
        ],
    )
    check("max score adds resolution and exclusive/multi maxima",
          profile.max_score, 69)
    rich = detail(profile, {
        "resolution": "1080p",
        "source": "bluray",
        "probe": probe(
            video=[{"codec": "h264"}, {"codec": "hevc"}],
            audio=[
                {"codec": "aac", "channels": 2, "language": "eng"},
                {"codec": "eac3", "channels": 6, "language": "spa"},
                {"codec": "eac3", "channels": 6, "language": "eng"},
            ],
            subtitles=[
                {"codec": "subrip", "language": "eng"},
                {"codec": "hdmv_pgs_subtitle", "language": "eng"},
                {"codec": "ass", "language": "spa"},
            ],
        ),
    })
    check("rich candidate eligible", rich["eligible"], True)
    check("rich candidate score", rich["score"], 69)
    check("exclusive video uses best match", rich["section_scores"]["video_codecs"], 10)
    check("exclusive audio codec uses best match",
          rich["section_scores"]["audio_codecs"], 10)
    check("exclusive channels uses best match",
          rich["section_scores"]["audio_channels"], 5)
    check("multi audio languages add each unique match",
          rich["section_scores"]["audio_languages"], 15)
    check("multi subtitle formats add each unique match",
          rich["section_scores"]["subtitle_formats"], 9)
    check("multi subtitle languages add each unique match",
          rich["section_scores"]["subtitle_languages"], 10)
    check("max target is target_met", rich["target_met"], True)

    plain = detail(profile, {"resolution": "1080p", "source": "web-dl"})
    check("empty optional sections do not reject", plain["eligible"], True)
    check("empty optional sections add zero",
          sum(plain["section_scores"].values()), 0)
    check("empty optional sections score base", plain["score"], 10)
    check("empty optional target_met", plain["target_met"], False)

    mismatch = build(
        target_score=20,
        video_codecs=[{"value": "hevc", "label": "HEVC", "weight": 10}],
        audio_codecs=[{"value": "eac3", "label": "E-AC-3", "weight": 10}],
        audio_languages=[{"value": "eng", "label": "English", "weight": 5}],
        subtitle_formats=[{"value": "srt", "label": "SRT", "weight": 5}],
    )
    none = detail(mismatch, {
        "resolution": "1080p",
        "source": "web-dl",
        "probe": probe(
            video=[{"codec": "av1"}],
            audio=[{"codec": "flac", "channels": 8, "language": "spa"}],
            subtitles=[{"codec": "mov_text", "language": "spa"}],
        ),
    })
    check("nonmatching optional sections stay eligible", none["eligible"], True)
    check("nonmatching optional score is base only", none["score"], 10)
    check("nonmatching optional contributions zero",
          sum(none["section_scores"].values()), 0)


def test_validation() -> None:
    section("profile payload validation and max score")
    values = quality.validate_profile(payload(
        resolution_weight=12,
        video_codecs=[
            {"value": "hevc", "weight": 10},
            {"value": "h264", "weight": 7},
        ],
        audio_languages=[
            {"value": "eng", "weight": 5},
            {"value": "spa", "weight": 5},
            {"value": "any", "weight": 5},
        ],
    ))
    check("default target equals max", values["target_score"], 37)
    check("computed max score", values["max_score"], 37)
    check("criteria version", values["criteria"]["version"], 1)
    check("source criteria removed", values["criteria"]["quality_options"], [])
    check("omitted optional option weight defaults to section default",
          values["audio_languages"][0]["weight"], 5)
    check("criteria JSON round-trips",
          json.loads(values["criteria_json"])["video_codecs"][0]["weight"], 10)

    check("target may equal max",
          quality.validate_profile(payload(
              resolution_weight=25, target_score=25))["target_score"], 25)
    check("target may be the floor",
          quality.validate_profile(payload(target_score=10))["target_score"], 10)
    raises("missing resolution rejected",
           lambda: quality.validate_profile(payload(resolution=None)),
           message="resolution")
    raises("unknown resolution rejected",
           lambda: quality.validate_profile(payload(resolution="1440p")),
           message="resolution")
    raises("duplicate language alias rejected",
           lambda: quality.validate_profile(payload(
               audio_languages=[
                   {"value": "en", "weight": 5},
                   {"value": "eng", "weight": 5},
               ])),
           message="duplicate")
    raises("unknown optional value rejected",
           lambda: quality.validate_profile(payload(
               video_codecs=[{"value": "mpeg1", "weight": 5}])),
           message="unknown")
    raises("target below 10 rejected",
           lambda: quality.validate_profile(payload(target_score=9)),
           message="between 10")
    raises("target above max rejected",
           lambda: quality.validate_profile(payload(target_score=11)),
           message="between 10 and 10")
    raises("target must be an integer",
           lambda: quality.validate_profile(payload(target_score=10.5)),
           message="between 10")
    raises("maximum below 10 rejected",
           lambda: quality.validate_profile({
               "name": "tiny", "resolution": "1080p",
               "resolution_weight": 1,
           }), message="at least 10")

    mapped = quality.validate_profile(payload(
        audio_languages=[{"value": "en", "weight": 5}],
    ))
    check("language alias canonicalised", mapped["audio_languages"][0]["value"], "eng")
    check("language canonical label", mapped["audio_languages"][0]["label"], "English")


def test_fact_mapping() -> None:
    section("probe facts and normalisation helpers")
    release = {
        "resolution": "unknown",
        "source": "WEB-DL",
        "probe": probe(
            resolution="1080p",
            video=[{"codec": "h264"}, {"codec": "HEVC"}],
            audio=[
                {"codec": "eac3", "channels": 6, "channel_layout": "5.1(side)",
                 "language": "eng"},
                {"codec": "truehd", "title": "TrueHD Atmos", "channels": 8,
                 "language": "eng"},
                {"codec": "dts", "profile": "DTS-HD MA", "channels": 6,
                 "language": "eng"},
            ],
            subtitles=[
                {"codec": "subrip", "language": "eng"},
                {"codec": "hdmv_pgs_subtitle", "language": "spa"},
            ],
            size=1234,
        ),
    }
    facts = quality.release_facts(release)
    check("probe resolution wins", facts["resolution"], "1080p")
    check("source normalised", facts["source"], "web-dl")
    check("video codecs", sorted(facts["video_codecs"]), ["h264", "hevc"])
    check("audio codecs (DTS-HD keeps its DTS core)", sorted(facts["audio_codecs"]),
          ["atmos", "dts", "dts-hd", "eac3", "truehd"])
    check("channels", sorted(facts["audio_channels"]), ["5.1", "7.1"])
    check("audio languages include any sentinel",
          sorted(facts["audio_languages"]), ["any", "eng"])
    check("subtitle formats", sorted(facts["subtitle_formats"]), ["pgs", "srt"])
    check("subtitle languages include any sentinel",
          sorted(facts["subtitle_languages"]), ["any", "eng", "spa"])
    check("probe size", facts["size"], 1234)

    stored = quality.release_facts({
        "resolution": "720p",
        "source": "webdl",
        "media_info": json.dumps(probe(
            resolution="1080p",
            video=[{"codec": "av1"}],
            audio=[{"codec": "opus", "channels": 2, "language": "eng"}],
            subtitles=[{"codec": "webvtt", "language": "eng"}],
            size=4321,
        )),
    })
    check("media_info resolution wins", stored["resolution"], "1080p")
    check("legacy source alias", stored["source"], "web-dl")
    check("media_info video", sorted(stored["video_codecs"]), ["av1"])
    check("media_info audio codec", sorted(stored["audio_codecs"]), ["opus"])
    check("media_info subtitle format",
          sorted(stored["subtitle_formats"]), ["webvtt"])
    check("media_info size", stored["size"], 4321)

    width_only = quality.release_facts({
        "probe": {"ok": True, "width": 3840, "height": 1600},
    })
    check("probe width classifies scope 4K", width_only["resolution"], "2160p")
    check("height 2160", quality.normalise_resolution(2160), "2160p")
    check("height 1080", quality.normalise_resolution(1080), "1080p")
    check("height 720", quality.normalise_resolution(720), "720p")
    check("scope 4K uses width", quality.resolution_from_dimensions(3840, 1600),
          "2160p")
    check("AVC normalises to h264", quality.normalise_video_codec("AVC"), "h264")
    check("h265 normalises to hevc", quality.normalise_video_codec("H.265"), "hevc")
    check("DTS-HD audio", quality.normalise_audio_codec("DTS-HD"), "dts-hd")
    check("TrueHD Atmos audio",
          quality.normalise_audio_codec("truehd", detail="Atmos"), "atmos")
    check("stereo channels", sorted(quality.normalise_channels(2, "stereo")),
          ["2.0", "stereo"])
    check("PGS subtitle",
          quality.normalise_subtitle_format("hdmv_pgs_subtitle"), "pgs")
    check("SRT subtitle", quality.normalise_subtitle_format("subrip"), "srt")
    check("release resolution", quality.parse_resolution("Movie.2160p.WEB-DL"),
          "2160p")
    check("release source", quality.parse_source("Movie.1080p.WEB-DL.DDP5.1"),
          "web-dl")
    check("HDCAM source", quality.normalise_source("HDCAM"), "cam")


def test_selection() -> None:
    section("choose_release: highest score, target pool and tie-breaks")
    profile = build(
        target_score=20,
        video_codecs=[{"value": "hevc", "label": "HEVC", "weight": 10}],
    )
    # The lower-source-weight candidate has the higher total score because of
    # its optional codec match. Score must win over source weight and name.
    lower_score = {
        "name": "aaa", "resolution": "1080p", "source": "bluray",
    }
    higher_score = {
        "name": "zzz", "resolution": "1080p", "source": "web-dl",
        "probe": probe(video=[{"codec": "hevc"}]),
    }
    check("highest total score wins over source weight and name",
          quality.choose_release([lower_score, higher_score], profile)["name"],
          "zzz")
    profile_exact = build(
        target_score=20,
        video_codecs=[{"value": "hevc", "label": "HEVC", "weight": 10}],
    )
    check("target_met pool beats a higher-scoring fallback",
          quality.choose_release([
              {"name": "exact", "resolution": "1080p", "source": "web-dl",
               "probe": probe(video=[{"codec": "hevc"}])},
              {"name": "lower", "resolution": "720p", "source": "bluray",
               "probe": probe(video=[{"codec": "hevc"}])},
          ], profile_exact)["name"], "exact")

    tie_profile = build(
        target_score=15,
        video_codecs=[{"value": "hevc", "label": "HEVC", "weight": 5}],
    )
    tie_win = quality.choose_release([
        {
            "name": "aaa", "resolution": "1080p", "source": "web-dl",
            "probe": probe(video=[{"codec": "hevc"}]),
        },
        {
            "name": "zzz", "resolution": "1080p", "source": "bluray",
            "probe": probe(video=[{"codec": "hevc"}]),
        },
    ], tie_profile)
    check("equal optional score uses the deterministic name tie-break",
          tie_win["name"], "aaa")

    match_profile = build(target_score=10, upgrades=False)
    match_win = quality.choose_release([
        {"name": "weak", "resolution": "1080p", "source": "web-dl",
         "score": 0.5},
        {"name": "strong", "resolution": "1080p", "source": "web-dl",
         "score": 0.95},
    ], match_profile)
    check("title/year score breaks an equal-score tie", match_win["name"], "strong")

    size_win = quality.choose_release([
        {"name": "small", "resolution": "1080p", "source": "web-dl",
         "score": 0.9, "size": 100},
        {"name": "large", "resolution": "1080p", "source": "web-dl",
         "score": 0.9, "size": 900},
    ], match_profile)
    check("size breaks an equal-score tie", size_win["name"], "large")

    fallback_profile = build(
        target_score=25,
        video_codecs=[{"value": "hevc", "label": "HEVC", "weight": 5}],
        audio_codecs=[{"value": "eac3", "label": "E-AC-3", "weight": 10}],
    )
    exact_win = quality.choose_release([
        {
            "name": "exact", "resolution": "1080p", "source": "web-dl",
            "probe": probe(video=[{"codec": "hevc"}]),
        },
        {"name": "lower", "resolution": "720p", "source": "bluray"},
    ], fallback_profile)
    check("exact resolution breaks an equal-score fallback tie",
          exact_win["name"], "exact")
    score_win = quality.choose_release([
        {"name": "exact-low", "resolution": "1080p", "source": "web-dl"},
        {"name": "lower-high", "resolution": "720p", "source": "bluray",
         "probe": probe(video=[{"codec": "hevc"}],
                        audio=[{"codec": "eac3"}])},
    ], fallback_profile)
    check("highest-scoring fallback wins over exact resolution",
          score_win["name"], "lower-high")

    no_eligible = quality.choose_release(
        [{"name": "unknown", "resolution": "unknown", "source": "web-dl"}],
        profile,
    )
    check("no eligible candidate returns None", no_eligible, None)


def test_current_file() -> None:
    section("current file re-scoring and upgrade decisions")
    profile = build(
        target_score=20,
        video_codecs=[{"value": "hevc", "label": "HEVC", "weight": 10}],
    )
    current = {
        "resolution": "1080p",
        "source": "web-dl",
        "file_score": 999,
        "media_info": json.dumps(probe(
            resolution="1080p",
            video=[{"codec": "h264"}],
            audio=[{"codec": "aac", "channels": 2, "language": "eng"}],
            size=500,
        )),
    }
    check("current file below target", profile.cutoff_met(current), False)
    better = {
        "name": "better", "resolution": "1080p", "source": "bluray",
        "probe": probe(video=[{"codec": "hevc"}]),
    }
    chosen = quality.choose_release([better], profile, current=current)
    check("stale file_score does not block a better release",
          chosen["name"], "better")
    check("equal score does not upgrade",
          quality.choose_release([
              {"name": "equal", "resolution": "1080p", "source": "web-dl"},
          ], profile, current=current), None)
    check("upgrades disabled never replaces",
          quality.choose_release(
              [better], build(target_score=20, upgrades=False,
                              video_codecs=[{"value": "hevc", "weight": 10}]),
              current=current,
          ), None)

    met = build(target_score=10)
    check("current file target_met via media_info",
          met.cutoff_met(current), True)
    check("current target_met blocks a higher release",
          quality.choose_release([better], met, current=current), None)

    stale_column = {
        "resolution": "720p",
        "source": "web-dl",
        "file_score": 0,
        "media_info": json.dumps(probe(
            resolution="1080p",
            video=[{"codec": "h264"}],
            audio=[{"codec": "aac", "channels": 2, "language": "eng"}],
        )),
    }
    check("media_info resolution overrides a stale column",
          met.cutoff_met(stale_column), True)

    ineligible_current = {"resolution": "unknown", "source": "web-dl"}
    chosen = quality.choose_release([better], profile, current=ineligible_current)
    check("ineligible current is replaced", chosen["name"], "better")


def test_api(conn) -> None:
    section("profile API: builtin immutability and custom CRUD")
    from iptv_charts.app import create_app  # noqa: PLC0415 - env set above

    conn.close()
    app = create_app()
    client = app.test_client()
    conn = db.connect()

    response = client.get("/api/quality-profiles")
    check("GET profiles status", response.status_code, 200)
    data = response.get_json()
    check("GET default key", data["default_profile_key"], "1080p")
    builtins = [entry for entry in data["profiles"] if entry["is_builtin"]]
    check("GET returns three builtins", len(builtins), 3)
    check("GET builtin order",
          [entry["builtin_key"] for entry in builtins],
          ["4k", "1080p", "720p"])
    default = [entry for entry in data["profiles"] if entry["is_default"]]
    check("GET marks exactly one default", len(default), 1)
    check("GET default is 1080p", default[0]["builtin_key"], "1080p")
    summary = next(entry for entry in builtins if entry["builtin_key"] == "1080p")
    for field in (
        "resolution", "resolution_weight", "quality_options", "target_score",
        "max_score", "upgrades", "revision", "criteria", "is_default",
        "qualities", "resolutions", "cutoff", "audio_mode", "subtitle_mode",
    ):
        check_true(f"GET summary has {field}", field in summary)
    check("GET summary target", summary["target_score"], 10)
    check("GET summary max", summary["max_score"], 10)

    builtin_id = summary["id"]
    response = client.patch(f"/api/quality-profiles/{builtin_id}",
                            json={"target_score": 30})
    check("PATCH builtin refused", response.status_code, 409)
    response = client.delete(f"/api/quality-profiles/{builtin_id}")
    check("DELETE builtin refused", response.status_code, 409)
    row = db.get_one(conn, "SELECT * FROM quality_profiles WHERE id = ?",
                     (builtin_id,))
    check("builtin unchanged after refused PATCH", row["target_score"], 10)

    custom = {
        "name": "Profile API",
        "resolution": "1080p",
        "resolution_weight": 12,
        "video_codecs": [{"value": "hevc", "label": "HEVC", "weight": 10}],
        "audio_languages": [{"value": "eng", "label": "English", "weight": 5}],
        "target_score": 25,
        "upgrades": False,
    }

    def create(overrides=None):
        body = dict(custom)
        body.update(overrides or {})
        return client.post("/api/quality-profiles", json=body)

    response = create({"resolution": "1440p"})
    check("POST unknown resolution refused", response.status_code, 400)
    response = create({"name": "Legacy source profile",
                       "quality_options": [{"value": "hddvd", "weight": 10}]})
    check("POST legacy source options are ignored", response.status_code, 201)
    if response.status_code == 201:
        client.delete(
            f"/api/quality-profiles/{response.get_json()['profile']['id']}"
        )
    response = create({"target_score": 9})
    check("POST target below floor refused", response.status_code, 400)
    response = create({"target_score": 99})
    check("POST target above max refused", response.status_code, 400)
    response = create({"name": ""})
    check("POST missing name refused", response.status_code, 400)

    response = create()
    check("POST custom profile status", response.status_code, 201)
    created = response.get_json()["profile"]
    profile_id = created["id"]
    check("custom not builtin", created["is_builtin"], False)
    check("custom not default", created["is_default"], False)
    check("custom revision", created["revision"], 1)
    check("custom max score", created["max_score"], 27)
    check("custom target", created["target_score"], 25)
    check("custom upgrades", created["upgrades"], False)
    check("custom resolution", created["resolution"], "1080p")
    check("custom source options removed", created["quality_options"], [])
    check("custom optional round-trip",
          created["video_codecs"][0]["value"], "hevc")
    check("custom label from server",
          created["audio_languages"][0]["label"], "English")
    check("legacy resolutions mirror", created["resolutions"], ["1080p"])
    check("legacy qualities mirror is empty", created["qualities"], [])
    check("legacy audio mode", created["audio_mode"], "any")
    check("legacy cutoff empty", created["cutoff"], None)

    response = client.post("/api/quality-profiles", json=custom)
    check("duplicate custom name refused", response.status_code, 409)
    response = client.post("/api/quality-profiles",
                           json={**custom, "name": "profile api"})
    check("duplicate name is case-insensitive", response.status_code, 409)
    response = client.post("/api/quality-profiles",
                           json={**custom, "name": "4K"})
    check("builtin name is reserved", response.status_code, 409)

    response = client.patch(f"/api/quality-profiles/{profile_id}",
                            json={"target_score": 26})
    check("PATCH custom status", response.status_code, 200)
    edited = response.get_json()["profile"]
    check("PATCH keeps source removed", edited["quality_options"], [])
    check("PATCH target updated", edited["target_score"], 26)
    check("PATCH max preserved", edited["max_score"], 27)
    check("PATCH revision bumped", edited["revision"], 2)
    response = client.patch(f"/api/quality-profiles/{profile_id}",
                            json={"video_codecs": [], "target_score": 16})
    check("PATCH clears optional section", response.status_code, 200)
    cleared = response.get_json()["profile"]
    check("PATCH cleared optional", cleared["video_codecs"], [])
    check("PATCH recomputed max after clearing", cleared["max_score"], 17)
    check("PATCH target kept in range", cleared["target_score"], 16)
    check("PATCH second revision bump", cleared["revision"], 3)
    response = client.patch(f"/api/quality-profiles/{profile_id}",
                            json={"target_score": 99})
    check("PATCH invalid target refused", response.status_code, 400)
    response = client.patch(f"/api/quality-profiles/{profile_id}",
                            json={"name": "1080p"})
    check("PATCH reserved name refused", response.status_code, 409)

    conn.execute(
        "INSERT INTO movies (tmdb_id, title, added_at, quality_profile_id) "
        "VALUES (?, ?, ?, ?)",
        (987654, "Profile test movie", 0, profile_id),
    )
    conn.commit()
    response = client.delete(f"/api/quality-profiles/{profile_id}")
    check("DELETE assigned profile refused", response.status_code, 409)
    conn.execute("DELETE FROM movies WHERE tmdb_id = ?", (987654,))
    conn.commit()
    response = client.delete(f"/api/quality-profiles/{profile_id}")
    check("DELETE unused custom", response.status_code, 200)
    response = client.delete(f"/api/quality-profiles/{profile_id}")
    check("DELETE missing profile", response.status_code, 404)

    # Default flags and library add dialogs follow settings.default_profile_key.
    db.set_setting(conn, "default_profile_key", "4k")
    fresh = client.get("/api/quality-profiles").get_json()
    defaults = [entry for entry in fresh["profiles"] if entry["is_default"]]
    check("GET default follows setting", len(defaults), 1)
    check("GET default is 4K", defaults[0]["builtin_key"], "4k")
    four_k_id = defaults[0]["id"]
    movies_page = client.get("/movies/add").get_data(as_text=True)
    series_page = client.get("/series/add").get_data(as_text=True)
    check_true("movies add page selects 4K",
               re.search(rf'value="{four_k_id}"\s+selected', movies_page) is not None)
    check_true("series add page selects 4K",
               re.search(rf'value="{four_k_id}"\s+selected', series_page) is not None)
    db.set_setting(conn, "default_profile_key", "1080p")
    hd = next(entry for entry in fresh["profiles"]
              if entry["builtin_key"] == "1080p")
    movies_page = client.get("/movies/add").get_data(as_text=True)
    check_true("movies add page selects 1080p after restore",
               re.search(rf'value="{hd["id"]}"\s+selected', movies_page) is not None)

    page = client.get("/settings/profiles")
    check("profiles page status", page.status_code, 200)
    body = page.get_data(as_text=True)
    check_true("profiles page shows builtin lock", "Built-in · Locked" in body)
    check_true("profiles page shows target/max", "Target / Max" in body)
    check_true("profiles page embeds builder schema", "PROFILE_SCHEMA" in body)

    conn.close()


def main() -> int:
    db.init_db()
    conn = db.connect()
    try:
        test_builtins(conn)
        test_mandatory()
        test_target_and_fallback()
        test_optional_scoring()
        test_validation()
        test_fact_mapping()
        test_selection()
        test_current_file()
        test_api(conn)
    except Exception:  # noqa: BLE001 - one exception should not hide the rest
        traceback.print_exc()
        check("suite completed without an exception", False, True)
    finally:
        try:
            conn.close()
        except Exception:  # noqa: BLE001
            pass
    return report()


if __name__ == "__main__":
    sys.exit(main())
