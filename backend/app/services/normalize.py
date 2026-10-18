"""Deterministic normalization primitives (Source of Truth §29).

Pure functions with no dependency on the fact *schemas*, so both the schema
layer and the fact engine can reuse them. Normalization never invents values:
it converts a value into its canonical representation or raises
`NormalizationError` (ambiguous/unsupported) so callers can surface it rather
than guessing silently.
"""

from __future__ import annotations

import re
import unicodedata
from datetime import date as _date

from ..errors import NormalizationError

# ── Indic digit scripts → ASCII ───────────────────────────────────────
_DIGITS = {
    "०": "0", "१": "1", "२": "2", "३": "3", "४": "4",
    "५": "5", "६": "6", "७": "7", "८": "8", "९": "9",  # Devanagari
    "০": "0", "১": "1", "২": "2", "৩": "3", "৪": "4",
    "৫": "5", "৬": "6", "৭": "7", "৮": "8", "৯": "9",  # Bengali
    "૦": "0", "૧": "1", "૨": "2", "૩": "3", "૪": "4",
    "૫": "5", "૬": "6", "૭": "7", "૮": "8", "૯": "9",  # Gujarati
    "੦": "0", "੧": "1", "੨": "2", "੩": "3", "੪": "4",
    "੫": "5", "੬": "6", "੭": "7", "੮": "8", "੯": "9",  # Gurmukhi
    "௦": "0", "௧": "1", "௨": "2", "௩": "3", "௪": "4",
    "௫": "5", "௬": "6", "௭": "7", "௮": "8", "௯": "9",  # Tamil
    "౦": "0", "౧": "1", "౨": "2", "౩": "3", "౪": "4",
    "౫": "5", "౬": "6", "౭": "7", "౮": "8", "౯": "9",  # Telugu
    "೦": "0", "೧": "1", "೨": "2", "೩": "3", "೪": "4",
    "೫": "5", "೬": "6", "೭": "7", "೮": "8", "೯": "9",  # Kannada
    "൦": "0", "൧": "1", "൨": "2", "൩": "3", "൪": "4",
    "൫": "5", "൬": "6", "൭": "7", "൮": "8", "൯": "9",  # Malayalam
}
_DIGIT_TABLE = str.maketrans(_DIGITS)

# ── English number words ──────────────────────────────────────────────
_EN_UNITS = {
    "zero": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
    "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11,
    "twelve": 12, "thirteen": 13, "fourteen": 14, "fifteen": 15,
    "sixteen": 16, "seventeen": 17, "eighteen": 18, "nineteen": 19,
}
_EN_TENS = {
    "twenty": 20, "thirty": 30, "forty": 40, "fifty": 50, "sixty": 60,
    "seventy": 70, "eighty": 80, "ninety": 90,
}
_EN_SCALES = {"hundred": 100, "thousand": 1000}

# ── Romanized Hindi (Hinglish) numerals ───────────────────────────────
_HI_NUMBERS = {
    "ek": 1, "do": 2, "teen": 3, "char": 4, "chaar": 4, "panch": 5,
    "paanch": 5, "chhah": 6, "chhe": 6, "che": 6, "saat": 7, "aath": 8,
    "nau": 9, "das": 10, "gyarah": 11, "barah": 12, "terah": 13,
    "chaudah": 14, "pandrah": 15, "solah": 16, "satrah": 17,
    "atharah": 18, "unnis": 19, "bees": 20, "tees": 30, "chalis": 40,
    "pachas": 50, "pachaas": 50, "saath": 60, "sattar": 70, "assi": 80,
    "nabbe": 90, "sau": 100, "hazar": 1000, "hazaar": 1000,
}

_NUMBER_NOISE = re.compile(
    r"(?:₹|rs\.?|inr|rupees?|rupee|only\b|percent\b|per\s+cent\b|%|off\b|flat\b)",
    re.IGNORECASE,
)


def normalize_digits(text: str) -> str:
    return str(text).translate(_DIGIT_TABLE)


def clean_text(value: object) -> str | None:
    if value is None:
        return None
    text = unicodedata.normalize("NFC", str(value))
    text = " ".join(text.split()).strip()
    return text or None


def _parse_number_words(text: str) -> int | None:
    tokens = [t for t in text.replace("-", " ").split() if t and t != "and"]
    if not tokens:
        return None
    total = 0
    current = 0
    matched = False
    for token in tokens:
        if token in _EN_UNITS:
            current += _EN_UNITS[token]
        elif token in _EN_TENS:
            current += _EN_TENS[token]
        elif token in _EN_SCALES:
            current = max(current, 1) * _EN_SCALES[token]
        elif token in _HI_NUMBERS:
            value = _HI_NUMBERS[token]
            if value >= 100:
                current = max(current, 1) * value
            else:
                current += value
        else:
            return None
        matched = True
    return total + current if matched else None


def coerce_number(value: object) -> float | None:
    """Parse a number from digits, Indic digits, English words or Hinglish words.

    Raises `NormalizationError` when a non-empty value cannot be understood.
    """
    if value is None:
        return None
    if isinstance(value, bool):
        raise NormalizationError(f"Boolean {value!r} is not a valid number.")
    if isinstance(value, (int, float)):
        return float(value)

    text = normalize_digits(str(value)).strip().lower()
    if not text:
        return None
    text = text.replace(",", "")
    text = _NUMBER_NOISE.sub(" ", text)
    text = " ".join(text.split())
    if not text:
        raise NormalizationError(f"Could not read a number from {value!r}.")

    if re.fullmatch(r"\d+(?:\.\d+)?", text):
        return float(text)
    if re.fullmatch(r"\.\d+", text):
        return float(text)

    words = _parse_number_words(text)
    if words is not None:
        return float(words)
    raise NormalizationError(f"Could not read a number from {value!r}.")


# ── Time ──────────────────────────────────────────────────────────────
_TIME_RE = re.compile(r"^(\d{1,2})(?::(\d{2}))?\s*(am|pm)?$")


def normalize_time(value: object) -> str | None:
    """Normalize an unambiguous time to 24-hour `HH:MM`.

    Rules:
    - `4 PM` / `4:30pm` — meridiem supplied;
    - `16:00` / `12:00` — a colon means explicit 24-hour notation;
    - a bare hour (`4`) is ambiguous and is rejected: the caller must add
      AM/PM rather than have Titan guess.
    """
    if value is None:
        return None
    text = normalize_digits(str(value)).strip().lower()
    text = text.replace("o'clock", " ").replace("o clock", " ")
    text = text.replace("a.m.", "am").replace("p.m.", "pm").replace(".", "")
    text = " ".join(text.split())
    if not text:
        return None
    if text in ("noon",):
        return "12:00"
    if text in ("midnight",):
        return "00:00"

    match = _TIME_RE.match(text)
    if not match:
        raise NormalizationError(f"Could not read a time from {value!r}.")
    hour = int(match.group(1))
    minute = int(match.group(2) or 0)
    meridiem = match.group(3)
    has_minutes = match.group(2) is not None
    if minute > 59:
        raise NormalizationError(f"Invalid minutes in time {value!r}.")

    if meridiem:
        if hour < 1 or hour > 12:
            raise NormalizationError(f"Invalid 12-hour clock value {value!r}.")
        if meridiem == "pm" and hour != 12:
            hour += 12
        elif meridiem == "am" and hour == 12:
            hour = 0
    else:
        if hour > 23:
            raise NormalizationError(f"Invalid hour in time {value!r}.")
        # A bare hour is ambiguous (4 could be AM or PM). An explicit minute
        # means 24-hour notation and is unambiguous.
        if 1 <= hour <= 12 and not has_minutes:
            raise NormalizationError(
                f"Time {value!r} is ambiguous; include AM or PM."
            )
    return f"{hour:02d}:{minute:02d}"


# ── Days ──────────────────────────────────────────────────────────────
WEEKDAYS = (
    "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday",
)
WEEKDAY_INDEX = {name: i for i, name in enumerate(WEEKDAYS)}

_DAY_ALIASES: dict[str, int] = {
    # English
    "monday": 0, "mon": 0,
    "tuesday": 1, "tue": 1, "tues": 1,
    "wednesday": 2, "wed": 2, "weds": 2,
    "thursday": 3, "thu": 3, "thur": 3, "thurs": 3,
    "friday": 4, "fri": 4,
    "saturday": 5, "sat": 5,
    "sunday": 6, "sun": 6,
    # Romanized Hindi / Urdu
    "somvar": 0, "somwar": 0,
    "mangalvar": 1, "mangalwar": 1,
    "budhvar": 2, "budhwar": 2,
    "guruvar": 3, "guruwar": 3, "brihaspativar": 3,
    "shukravar": 4, "shukrawar": 4, "jumma": 4, "juma": 4,
    "shanivar": 5, "shaniwar": 5,
    "ravivar": 6, "raviwar": 6, "itvar": 6, "itwar": 6,
    # Devanagari
    "सोमवार": 0, "मंगलवार": 1, "बुधवार": 2, "गुरुवार": 3,
    "बृहस्पतिवार": 3, "शुक्रवार": 4, "शनिवार": 5, "रविवार": 6,
}


def normalize_day(value: object) -> str:
    if value is None:
        raise NormalizationError("Empty day value.")
    key = unicodedata.normalize("NFC", str(value)).strip().lower().rstrip(".")
    if key in _DAY_ALIASES:
        return WEEKDAYS[_DAY_ALIASES[key]]
    raise NormalizationError(f"Unrecognized day {value!r}.")


def normalize_days(values: object) -> list[str]:
    if values is None:
        return []
    if isinstance(values, str):
        values = [values]
    indexes: set[int] = set()
    for item in values:
        if item is None or str(item).strip() == "":
            continue
        indexes.add(WEEKDAY_INDEX[normalize_day(item)])
    return [WEEKDAYS[i] for i in sorted(indexes)]


# ── Dates ─────────────────────────────────────────────────────────────
_MONTHS = {
    "january": 1, "february": 2, "march": 3, "april": 4, "may": 5, "june": 6,
    "july": 7, "august": 8, "september": 9, "october": 10, "november": 11,
    "december": 12,
}
_MONTH_ABBREV = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6, "jul": 7,
    "aug": 8, "sep": 9, "sept": 9, "oct": 10, "nov": 11, "dec": 12,
}


def _resolve_month(name: str) -> int | None:
    name = name.lower().strip(".")
    if name in _MONTHS:
        return _MONTHS[name]
    return _MONTH_ABBREV.get(name[:3])


_MONTH_NAMES = {n: name for name, n in _MONTHS.items()}
_ISO_RE = re.compile(r"^(\d{4})-(\d{1,2})-(\d{1,2})$")
_DM_RE = re.compile(r"^(\d{1,2})(?:st|nd|rd|th)?\s+([a-z]+)\.?\s*(\d{4})?$")
_MD_RE = re.compile(r"^([a-z]+)\.?\s+(\d{1,2})(?:st|nd|rd|th)?,?\s*(\d{4})?$")
_NUMERIC_DATE_RE = re.compile(r"^\d{1,2}[/.]\d{1,2}[/.]\d{2,4}$")


def normalize_date(value: object) -> str | None:
    """Canonicalize an unambiguous date.

    - with a year  → ISO `YYYY-MM-DD`
    - without year → `D Month` (e.g. `10 October`)

    Raises for unsupported or ambiguous forms (e.g. `10/11/2026`, where
    day-first vs month-first is unclear).
    """
    if value is None:
        return None
    text = unicodedata.normalize("NFC", normalize_digits(str(value))).strip()
    if not text:
        return None
    if _NUMERIC_DATE_RE.match(text):
        raise NormalizationError(
            f"Date {value!r} is ambiguous; use an ISO date or a month name."
        )

    match = _ISO_RE.match(text)
    if match:
        year, month, day = (int(g) for g in match.groups())
        return _format_date(year, month, day, iso=True)

    match = _DM_RE.match(text.lower()) or _MD_RE.match(text.lower())
    if match:
        groups = match.groups()
        if _DM_RE.match(text.lower()):
            day = int(groups[0])
            month_name, year_raw = groups[1], groups[2]
        else:
            month_name, day, year_raw = groups[0], int(groups[1]), groups[2]
        month = _resolve_month(month_name)
        if month is None:
            raise NormalizationError(f"Unrecognized month in date {value!r}.")
        year = int(year_raw) if year_raw else None
        return _format_date(year, month, day, iso=year is not None)

    raise NormalizationError(f"Could not read a date from {value!r}.")


def _format_date(year: int | None, month: int, day: int, *, iso: bool) -> str:
    try:
        _date(year or 2024, month, day)
    except ValueError as exc:
        raise NormalizationError(
            f"Invalid calendar date {year or ''}-{month:02d}-{day:02d}."
        ) from exc
    if iso and year is not None:
        return f"{year:04d}-{month:02d}-{day:02d}"
    return f"{day} {_MONTH_NAMES[month].capitalize()}"


# ── Languages ─────────────────────────────────────────────────────────
_LANGUAGE_ALIASES = {
    "en": "English", "eng": "English", "english": "English",
    "hi": "Hindi", "hin": "Hindi", "hindi": "Hindi",
    "kn": "Kannada", "kan": "Kannada", "kannada": "Kannada",
    "ta": "Tamil", "tam": "Tamil", "tamil": "Tamil",
    "te": "Telugu", "tel": "Telugu", "telugu": "Telugu",
    "mr": "Marathi", "marathi": "Marathi",
    "bn": "Bengali", "bengali": "Bengali", "bangla": "Bengali",
    "ml": "Malayalam", "malayalam": "Malayalam",
    "gu": "Gujarati", "gujarati": "Gujarati",
    "pa": "Punjabi", "punjabi": "Punjabi",
    "ur": "Urdu", "urdu": "Urdu",
    "or": "Odia", "odia": "Odia", "oriya": "Odia",
    "as": "Assamese", "assamese": "Assamese",
    "kok": "Konkani", "konkani": "Konkani",
}


def normalize_language(value: object) -> str | None:
    text = clean_text(value)
    if not text:
        return None
    key = text.lower()
    if key in _LANGUAGE_ALIASES:
        return _LANGUAGE_ALIASES[key]
    # Preserve unknown languages rather than silently dropping them.
    return " ".join(word.capitalize() for word in key.split())


def normalize_language_list(values: object) -> list[str]:
    return _normalize_str_list(values, transform=normalize_language)


# ── Generic string lists ──────────────────────────────────────────────
def normalize_string_list(values: object) -> list[str]:
    return _normalize_str_list(values, transform=clean_text)


def _normalize_str_list(values: object, *, transform) -> list[str]:
    if values is None:
        return []
    if isinstance(values, str):
        values = [values]
    seen: set[str] = set()
    result: list[str] = []
    for item in values:
        cleaned = transform(item)
        if not cleaned:
            continue
        key = cleaned.casefold()
        if key in seen:
            continue
        seen.add(key)
        result.append(cleaned)
    return result
