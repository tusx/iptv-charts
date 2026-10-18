"""Language code normalisation for metadata and stream probes.

TMDB gives ISO 639-1 codes (``en``, ``es``); ffprobe writes ISO 639-2/B
(``eng``, ``spa``) and sometimes the terminologic form (``fra``, ``deu``).
Quality profiles have to compare them, so everything is folded to the
639-2/B form used by ffprobe.
"""

from __future__ import annotations

from typing import Any, Iterable

#: ISO 639-1 -> ISO 639-2/B. Deliberately limited to languages a provider
#: catalogue realistically carries; unknown codes are preserved rather than
#: guessed at.
_TWO_TO_THREE = {
    "aa": "aar", "ab": "abk", "af": "afr", "am": "amh", "ar": "ara",
    "as": "asm", "az": "aze", "ba": "bak", "be": "bel", "bg": "bul",
    "bn": "ben", "bo": "tib", "bs": "bos", "ca": "cat", "cs": "cze",
    "cy": "wel", "da": "dan", "de": "ger", "dz": "dzo", "el": "gre",
    "en": "eng", "eo": "epo", "es": "spa", "et": "est", "eu": "baq",
    "fa": "per", "fi": "fin", "fil": "tgl", "fo": "fao", "fr": "fre",
    "ga": "gle", "gl": "glg", "gu": "guj", "he": "heb", "hi": "hin",
    "hr": "hrv", "hu": "hun", "hy": "arm", "id": "ind", "is": "ice",
    "it": "ita", "ja": "jpn", "jv": "jav", "ka": "geo", "kk": "kaz",
    "km": "khm", "kn": "kan", "ko": "kor", "ku": "kur", "ky": "kir",
    "lo": "lao", "lt": "lit", "lv": "lav", "mk": "mac", "ml": "mal",
    "mn": "mon", "mr": "mar", "ms": "may", "mt": "mlt", "my": "bur",
    "nb": "nor", "ne": "nep", "nl": "dut", "no": "nor", "pa": "pan",
    "ps": "pus", "pt": "por", "ro": "rum", "ru": "rus", "si": "sin",
    "sk": "slo", "sl": "slv", "sq": "alb", "sr": "srp", "sv": "swe",
    "sw": "swa", "ta": "tam", "te": "tel", "th": "tha", "tl": "tgl",
    "tr": "tur", "uk": "ukr", "ur": "urd", "uz": "uzb", "vi": "vie",
    "zh": "chi", "zu": "zul",
}

#: Terminologic (639-2/T) and legacy spellings mapped onto 639-2/B.
_ALIASES = {
    "alb": "alb", "arm": "arm", "baq": "baq", "bur": "bur", "chi": "chi",
    "cze": "cze", "dut": "dut", "fre": "fre", "geo": "geo", "ger": "ger",
    "gre": "gre", "ice": "ice", "mac": "mac", "may": "may", "per": "per",
    "rum": "rum", "slo": "slo", "tib": "tib", "wel": "wel",
    "sqi": "alb", "hye": "arm", "eus": "baq", "mya": "bur", "zho": "chi",
    "ces": "cze", "nld": "dut", "fra": "fre", "kat": "geo", "deu": "ger",
    "ell": "gre", "isl": "ice", "mkd": "mac", "msa": "may", "fas": "per",
    "ron": "rum", "slk": "slo", "bod": "tib", "cym": "wel",
    "iw": "heb", "in": "ind", "ji": "yid", "jw": "jav",
    "pt-br": "por", "pt_br": "por", "zh-cn": "chi", "zh-tw": "chi",
    "zh-hans": "chi", "zh-hant": "chi",
}

_NAMES = {
    "English": "eng", "Spanish": "spa", "French": "fre", "German": "ger",
    "Italian": "ita", "Portuguese": "por", "Russian": "rus", "Arabic": "ara",
    "Hindi": "hin", "Tamil": "tam", "Telugu": "tel", "Malayalam": "mal",
    "Bengali": "ben", "Urdu": "urd", "Turkish": "tur", "Polish": "pol",
    "Dutch": "dut", "Swedish": "swe", "Norwegian": "nor", "Danish": "dan",
    "Finnish": "fin", "Japanese": "jpn", "Korean": "kor", "Chinese": "chi",
    "Thai": "tha", "Indonesian": "ind", "Vietnamese": "vie", "Ukrainian": "ukr",
    "Hebrew": "heb", "Greek": "gre", "Czech": "cze", "Hungarian": "hun",
    "Romanian": "rum", "Slovak": "slo", "Slovenian": "slv", "Croatian": "hrv",
    "Serbian": "srp", "Bulgarian": "bul", "Catalan": "cat", "Basque": "baq",
    "Galician": "glg", "Georgian": "geo", "Armenian": "arm", "Azerbaijani": "aze",
    "Kazakh": "kaz", "Uzbek": "uzb", "Persian": "per", "Pashto": "pus",
    "Kurdish": "kur", "Swahili": "swa", "Afrikaans": "afr", "Albanian": "alb",
    "Macedonian": "mac", "Bosnian": "bos", "Icelandic": "ice", "Irish": "gle",
    "Welsh": "wel", "Maltese": "mlt", "Estonian": "est", "Latvian": "lav",
    "Lithuanian": "lit", "Belarusian": "bel", "Mongolian": "mon", "Nepali": "nep",
    "Sinhala": "sin", "Khmer": "khm", "Lao": "lao", "Burmese": "bur",
    "Tibetan": "tib", "Tagalog": "tgl", "Malay": "may", "Javanese": "jav",
}

_NAME_MAP = {name.lower(): code for name, code in _NAMES.items()}

_DISPLAY = {
    "eng": "English", "spa": "Spanish", "fre": "French", "ger": "German",
    "ita": "Italian", "por": "Portuguese", "rus": "Russian", "ara": "Arabic",
    "hin": "Hindi", "tam": "Tamil", "tel": "Telugu", "mal": "Malayalam",
    "ben": "Bengali", "urd": "Urdu", "tur": "Turkish", "pol": "Polish",
    "dut": "Dutch", "swe": "Swedish", "nor": "Norwegian", "dan": "Danish",
    "fin": "Finnish", "jpn": "Japanese", "kor": "Korean", "chi": "Chinese",
    "tha": "Thai", "ind": "Indonesian", "vie": "Vietnamese", "ukr": "Ukrainian",
    "heb": "Hebrew", "gre": "Greek", "cze": "Czech", "hun": "Hungarian",
    "rum": "Romanian", "slo": "Slovak", "slv": "Slovenian", "hrv": "Croatian",
    "srp": "Serbian", "bul": "Bulgarian", "cat": "Catalan", "baq": "Basque",
    "glg": "Galician", "geo": "Georgian", "arm": "Armenian", "aze": "Azerbaijani",
    "kaz": "Kazakh", "uzb": "Uzbek", "per": "Persian", "pus": "Pashto",
    "kur": "Kurdish", "swa": "Swahili", "afr": "Afrikaans", "alb": "Albanian",
    "mac": "Macedonian", "bos": "Bosnian", "ice": "Icelandic", "gle": "Irish",
    "wel": "Welsh", "mlt": "Maltese", "est": "Estonian", "lav": "Latvian",
    "lit": "Lithuanian", "bel": "Belarusian", "mon": "Mongolian", "nep": "Nepali",
    "sin": "Sinhala", "khm": "Khmer", "lao": "Lao", "bur": "Burmese",
    "tib": "Tibetan", "tgl": "Tagalog", "may": "Malay", "jav": "Javanese",
}

#: Languages offered in the profile editor, in a sensible reading order.
COMMON_LANGUAGES = tuple(
    (code, _DISPLAY[code])
    for code in (
        "eng", "spa", "fre", "ger", "ita", "por", "rus", "ara", "hin",
        "tam", "tel", "mal", "ben", "urd", "tur", "pol", "dut", "swe",
        "nor", "dan", "fin", "jpn", "kor", "chi", "tha", "ind", "vie",
        "ukr", "heb", "gre", "cze", "hun", "rum", "slo", "hrv", "srp",
        "bul", "cat", "per", "swa", "afr",
    )
)

#: Values that mean "no usable language tag" in ffprobe/containers.
_UNKNOWN = {"", "und", "unknown", "mis", "mul", "zxx", "none", "null"}

ORIGINAL = "original"


def normalise(value: Any) -> str | None:
    """Fold any recognised language spelling into a 639-2/B code."""
    text = str(value or "").strip().lower().replace("_", "-")
    if not text:
        return None
    if text in _UNKNOWN:
        return None
    if text in _NAME_MAP:
        return _NAME_MAP[text]
    # "en-US", "pt-BR", "zh-Hans" -> the primary subtag.
    primary = text.split("-", 1)[0]
    if primary in _UNKNOWN:
        return None
    if primary in _TWO_TO_THREE:
        return _TWO_TO_THREE[primary]
    if primary in _ALIASES:
        return _ALIASES[primary]
    if len(primary) == 3 and primary.isalpha():
        # Preserve an already-valid-looking three-letter code. Providers and
        # older containers occasionally use one outside the common table.
        return primary
    return None


def normalise_many(values: Any) -> list[str]:
    """Normalise a JSON list/tuple/string of languages, preserving order."""
    if values is None:
        return []
    if isinstance(values, str):
        values = [part for part in values.replace(";", ",").split(",") if part.strip()]
    if not isinstance(values, (list, tuple, set)):
        return []
    out: list[str] = []
    for value in values:
        code = normalise(value)
        if code and code not in out:
            out.append(code)
    return out


def resolve(value: Any, original_language: Any = None) -> str | None:
    """Resolve a profile language, including the ``original`` sentinel."""
    text = str(value or "").strip().lower()
    if text == ORIGINAL:
        return normalise(original_language)
    return normalise(value)


def resolve_many(values: Any, original_language: Any = None) -> list[str]:
    if values is None:
        return []
    if isinstance(values, str):
        values = [part for part in values.replace(";", ",").split(",") if part.strip()]
    if not isinstance(values, (list, tuple, set)):
        return []
    out: list[str] = []
    for value in values:
        code = resolve(value, original_language)
        if code and code not in out:
            out.append(code)
    return out


def display(code: Any) -> str:
    """Human label for a code; unknown codes are shown as-is."""
    text = str(code or "").strip().lower()
    if text == ORIGINAL:
        return "Original language"
    return _DISPLAY.get(text, text.upper() if text else "Unknown")


def labels(codes: Iterable[Any]) -> list[str]:
    return [display(code) for code in codes or []]


def matches(required: Iterable[Any], available: Iterable[Any]) -> bool:
    """Whether at least one required language is present in ``available``."""
    required_set = {normalise(code) for code in required}
    required_set.discard(None)
    available_set = {normalise(code) for code in available}
    available_set.discard(None)
    if not required_set:
        return False
    return bool(required_set & available_set)


__all__ = [
    "COMMON_LANGUAGES",
    "ORIGINAL",
    "display",
    "labels",
    "matches",
    "normalise",
    "normalise_many",
    "resolve",
    "resolve_many",
]
