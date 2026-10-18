"""Brag Director — a creative subagent that storyboards the campaign visuals.

The agent runs on the Agnes 3.0 Flash text model (``TITAN_BRAG_AGENT_MODEL``)
and follows the brag-slim method: a short launch-style video shaped as
hook → reveal → sharp highlights → outro, specific to this one shop, with
every line on screen long enough to read. The same art direction (palette,
type, copy) also dresses the poster, so both read as one campaign.

Division of labour — the integrity rule of this product is unchanged:

- the agent decides *creative* things only: mood, colours, type style, layout,
  scene order, and short framing copy (hook, kicker, call to action);
- it never writes a fact. Its copy may reference facts only as ``{{TOKENS}}``
  and may not contain a digit; this module rejects anything else;
- the renderer (`brag_renderer`) draws every fact from the locked token map.

A storyboard that fails validation is sent back to the agent (bounded), and
if the agent is unavailable a plain default storyboard is used — that default
contains no facts either, so the visuals stay truthful and merely less styled.
"""

from __future__ import annotations

import json
import re
from dataclasses import replace
from typing import Any

from ..config import Settings, get_settings
from ..errors import ProviderUnavailableError, TitanError
from .agnes import AgnesLLMClient, parse_llm_json
from .async_bridge import run_sync
from .fact_engine import validate_template

STYLES = ("bold", "modern", "elegant", "friendly")
POSTER_LAYOUTS = ("badge", "block")
MOTIONS = ("pop", "slide", "rise")
MIDDLE_SCENES = ("offer", "when")
MAX_ATTEMPTS = 2

_HEX = re.compile(r"^#[0-9a-fA-F]{6}$")
_DIGIT = re.compile(r"\d")
_TOKEN = re.compile(r"\{\{[A-Z_]+\}\}")

DEFAULT_STORYBOARD: dict[str, Any] = {
    "concept": "A warm neighbourhood offer, stated plainly.",
    "style": "bold",
    "motion": "pop",
    "palette": {"bg": "#16130f", "accent": "#ffc53d"},
    "poster": {"layout": "badge", "kicker": "This week only", "cta": "Come on in"},
    "video": {
        "hook": "Your neighbourhood has news",
        "reveal_kicker": "Now serving",
        "offer_caption": "Yes, really",
        "when_caption": "Mark the days",
        "cta": "See you there",
        "order": ["offer", "when"],
    },
}

_SYSTEM = (
    "You are the Brag Director: you art-direct a short vertical launch video and a matching "
    "poster for ONE local shop's offer. Work the brag-slim way:\n"
    "- Shape: hook (first 2 seconds decide everything) -> reveal the product -> 2 sharp "
    "highlights (the offer, then when) -> outro with a call to action.\n"
    "- Specific: it must feel made for this exact shop and product. No generic marketing "
    "language ('unlock', 'elevate', 'streamline', 'best in town').\n"
    "- Readable: every line is short enough to read at a glance.\n"
    "- Funny or warm only if it comes from the offer itself.\n"
    "- One coherent look: a background colour and ONE accent colour that suit the product "
    "and mood (coffee -> deep browns/cream, pizza -> tomato red/basil, sweets -> festive, "
    "etc.). The accent must stand out strongly against the background.\n"
    "HARD RULES:\n"
    "1. You write framing copy only. NEVER write a number, price, percentage, date, time or "
    "day, in digits or in words. Banned words include: weekend, today, tonight, tomorrow, any weekday name, percent, half, and number words (one, two, ...). The renderer adds all facts itself.\n"
    "2. You may mention the product or audience ONLY through the given {{TOKENS}}.\n"
    "3. Never invent claims, scarcity, awards or guarantees.\n"
    "4. Respect the word limits in the schema.\n"
    "Return ONLY one JSON object matching the schema. No prose, no code fences."
)

_SCHEMA = {
    "concept": "one sentence: the creative angle",
    "style": "one of: bold | modern | elegant | friendly",
    "motion": "one of: pop | slide | rise",
    "palette": {"bg": "#RRGGBB background", "accent": "#RRGGBB accent"},
    "poster": {
        "layout": "one of: badge | block",
        "kicker": "<= 4 words above the headline",
        "cta": "<= 4 words call to action",
    },
    "video": {
        "hook": "<= 7 words opening line that stops the scroll",
        "reveal_kicker": "<= 3 words shown above the product name",
        "offer_caption": "<= 4 words shown under the offer",
        "when_caption": "<= 4 words shown above the days and times",
        "cta": "<= 4 words closing call to action",
        "order": "['offer','when'] or ['when','offer']",
    },
}

#: (path, max words) for every piece of agent-written copy.
_COPY_FIELDS = (
    (("poster", "kicker"), 4),
    (("poster", "cta"), 4),
    (("video", "hook"), 7),
    (("video", "reveal_kicker"), 3),
    (("video", "offer_caption"), 4),
    (("video", "when_caption"), 4),
    (("video", "cta"), 4),
)
_NUMBER_WORDS = re.compile(
    r"\b(one|two|three|four|five|six|seven|eight|nine|ten|twenty|thirty|forty|fifty|"
    r"sixty|seventy|eighty|ninety|hundred|thousand|half|percent|rupees?|"
    r"monday|tuesday|wednesday|thursday|friday|saturday|sunday|weekend|today|tomorrow|tonight)\b",
    re.IGNORECASE,
)


def _get(data: dict[str, Any], path: tuple[str, ...]) -> Any:
    node: Any = data
    for key in path:
        node = node.get(key) if isinstance(node, dict) else None
    return node


def validate_storyboard(raw: Any, tokens: dict[str, str]) -> list[str]:
    """Issues that make a storyboard unusable (empty list = usable)."""
    if not isinstance(raw, dict):
        return ["The storyboard must be a JSON object."]
    issues: list[str] = []
    if raw.get("style") not in STYLES:
        issues.append(f"style must be one of {', '.join(STYLES)}.")
    if raw.get("motion") not in MOTIONS:
        issues.append(f"motion must be one of {', '.join(MOTIONS)}.")
    palette = raw.get("palette")
    for name in ("bg", "accent"):
        value = palette.get(name) if isinstance(palette, dict) else None
        if not isinstance(value, str) or not _HEX.match(value):
            issues.append(f"palette.{name} must be a #RRGGBB colour.")
    if _get(raw, ("poster", "layout")) not in POSTER_LAYOUTS:
        issues.append(f"poster.layout must be one of {', '.join(POSTER_LAYOUTS)}.")
    order = _get(raw, ("video", "order"))
    if not isinstance(order, list) or sorted(order) != sorted(MIDDLE_SCENES):
        issues.append("video.order must list 'offer' and 'when' once each.")
    for path, limit in _COPY_FIELDS:
        label = ".".join(path)
        value = _get(raw, path)
        if not isinstance(value, str) or not value.strip():
            issues.append(f"{label} is required.")
            continue
        bare = re.sub(r"\{\{[A-Z_]+\}\}", "", value)
        if _DIGIT.search(bare) or _NUMBER_WORDS.search(bare):
            issues.append(f"{label} must not state a number, price, day or time — the renderer adds facts.")
        if len(value.split()) > limit + 1:
            issues.append(f"{label} is too long (max {limit} words).")
        issues.extend(f"{label}: {problem}" for problem in validate_template(value, tokens))
    return issues


def _clean(raw: dict[str, Any]) -> dict[str, Any]:
    """Keep only the schema's fields (nothing else the agent wrote is used)."""
    board: dict[str, Any] = {
        "concept": str(raw.get("concept") or "")[:200],
        "style": raw["style"],
        "motion": raw["motion"],
        "palette": {"bg": raw["palette"]["bg"].lower(), "accent": raw["palette"]["accent"].lower()},
        "poster": {"layout": raw["poster"]["layout"]},
        "video": {"order": list(raw["video"]["order"])},
    }
    for path, _limit in _COPY_FIELDS:
        board[path[0]][path[1]] = " ".join(str(_get(raw, path)).split())
    return board


def _copy_ok(value: Any, limit: int, tokens: dict[str, str]) -> bool:
    if not isinstance(value, str) or not value.strip():
        return False
    bare = _TOKEN.sub("", value)
    if _DIGIT.search(bare) or _NUMBER_WORDS.search(bare) or len(value.split()) > limit + 1:
        return False
    return not validate_template(value, tokens)


def salvage(raw: Any, tokens: dict[str, str]) -> tuple[dict[str, Any], list[str]]:
    """Keep every field of a rejected storyboard that is valid on its own.

    One bad line should not cost the whole art direction: each field is checked
    separately and only the failing ones fall back to the default. Returns the
    storyboard and the names of the fields that were replaced.
    """
    board = default_storyboard()
    if not isinstance(raw, dict):
        return board, ["everything"]
    replaced: list[str] = []
    if raw.get("style") in STYLES:
        board["style"] = raw["style"]
    else:
        replaced.append("style")
    if raw.get("motion") in MOTIONS:
        board["motion"] = raw["motion"]
    else:
        replaced.append("motion")
    palette = raw.get("palette") if isinstance(raw.get("palette"), dict) else {}
    if all(isinstance(palette.get(k), str) and _HEX.match(palette[k]) for k in ("bg", "accent")):
        board["palette"] = {"bg": palette["bg"].lower(), "accent": palette["accent"].lower()}
    else:
        replaced.append("palette")
    if _get(raw, ("poster", "layout")) in POSTER_LAYOUTS:
        board["poster"]["layout"] = raw["poster"]["layout"]
    else:
        replaced.append("poster.layout")
    order = _get(raw, ("video", "order"))
    if isinstance(order, list) and sorted(order) == sorted(MIDDLE_SCENES):
        board["video"]["order"] = list(order)
    else:
        replaced.append("video.order")
    for path, limit in _COPY_FIELDS:
        value = _get(raw, path)
        if _copy_ok(value, limit, tokens):
            board[path[0]][path[1]] = " ".join(value.split())
        else:
            replaced.append(".".join(path))
    board["concept"] = str(raw.get("concept") or board["concept"])[:200]
    return board, replaced


def default_storyboard() -> dict[str, Any]:
    return json.loads(json.dumps(DEFAULT_STORYBOARD))


def direct(
    tokens: dict[str, str],
    *,
    business_name: str | None,
    plan_angle: str | None = None,
    settings: Settings | None = None,
) -> dict[str, Any]:
    """Ask the agent for a storyboard. Always returns a usable one.

    The returned dict carries ``agent`` metadata: which model produced it, or
    why the default was used.
    """
    settings = settings or get_settings()
    board = default_storyboard()
    if settings.is_mock or not settings.agnes_configured:
        board["agent"] = {"model": None, "used_default": True, "reason": "mock mode / Agnes not configured"}
        return board

    client = AgnesLLMClient(replace(settings, agnes_text_model=settings.brag_agent_model))
    request = {
        "business_name": business_name,
        "campaign_angle": plan_angle,
        # Values are context for taste (what is sold, to whom); copy must
        # still reference them only as {{TOKENS}}.
        "locked_fact_tokens": tokens,
        "available_token_names": sorted(tokens),
        "schema": _SCHEMA,
    }
    issues: list[str] = []
    reason = ""
    last_raw: Any = None
    last_model: str | None = None
    for attempt in range(MAX_ATTEMPTS):
        payload = request if not attempt else {
            "previous_storyboard_rejected_because": issues[:8],
            "instruction": "Return a corrected, complete storyboard as ONE JSON object.",
            "request": request,
        }
        try:
            result = run_sync(
                client.complete_json(_SYSTEM, json.dumps(payload, ensure_ascii=False), max_tokens=600, temperature=0.8)
            )
            raw = parse_llm_json(result)
        except (ProviderUnavailableError, TitanError) as exc:
            reason = exc.message
            if getattr(exc, "code", "") in ("llm_bad_output", "llm_bad_json"):
                issues = [exc.message]
                continue
            break
        last_raw, last_model = raw, result.model
        issues = validate_storyboard(raw, tokens)
        if not issues:
            board = _clean(raw)
            board["agent"] = {"model": result.model, "provider": result.provider, "used_default": False, "attempts": attempt + 1}
            return board
        reason = "; ".join(issues[:3])
    if last_raw is not None:
        board, replaced = salvage(last_raw, tokens)
        if len(replaced) < 6:  # most of the agent's direction survived
            board["agent"] = {
                "model": last_model,
                "used_default": False,
                "attempts": MAX_ATTEMPTS,
                "replaced_with_default": replaced,
            }
            return board
        board = default_storyboard()
    board["agent"] = {"model": settings.brag_agent_model, "used_default": True, "reason": reason[:300]}
    return board
