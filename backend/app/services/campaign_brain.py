"""Agnes Campaign Director: plan + copy + localization + media briefs.

The Director is the campaign intelligence layer. It receives the locked,
validated facts (as a token map), the owner's objective/tone/instructions and
the target languages, and returns ONE structured, schema-validated plan:

- strategy (creative angle + rationale)
- per-channel TokenizedCopy (tokens only, never literal fact values)
- a tokenized voice script for Sarvam TTS
- poster briefs and a video brief (visual prompts with no text or numbers)
- one LocalizationSpec per requested language
- `missing_information`: what the owner did not provide — reported, never
  invented

It coordinates the focused services that render the plan (posters in
`assets_service`, speech in `voice_service`, video in `video_service`) through
the persisted plan record rather than calling them itself.

The token map is the only source of literal fact values; ``substitute_tokens``
does the deterministic substitution and fails closed on unknown/unresolved
tokens (``TokenError``). Model output is validated against the plan schema and
repaired with a bounded number of attempts; an unusable plan is an error, not a
silently substituted template. The plan persists ``fact_hash``/``fact_sheet_id``
provenance.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy.orm import Session

from ..config import Settings, get_settings
from ..errors import ConflictError, ProviderUnavailableError, ValidationError
from ..models import (
    Campaign,
    CampaignPlanRecord,
    CampaignStatus,
    FactSheetRecord,
    FactSheetStatus,
)
from .agnes import (
    LOCALE_CODE_MAP,
    REGION_FOR_LANGUAGE,
    TARGETED_LANGUAGES,
    LLMResult,
    get_llm_client,
    parse_llm_json,
)
from .async_bridge import run_sync
from .fact_engine import substitute_tokens, validate_template
from .fact_schemas import FactSheet

CHANNELS = (
    "instagram",
    "facebook",
    "x",
    "whatsapp",
    "poster_headline",
    "poster_subline",
    "reel_script",
    "voice_script",
)

#: Channels a usable plan must contain (the rest are optional).
REQUIRED_CHANNELS = ("instagram", "whatsapp", "poster_headline", "voice_script")

OBJECTIVES = ("awareness", "footfall", "sales", "launch", "festival", "loyalty")
DEFAULT_OBJECTIVE = "footfall"

#: A malformed plan is sent back to the model this many times in total.
PLAN_MAX_ATTEMPTS = 3

_COPY_SYSTEM = (
    "You are the Campaign Director for a local shop's marketing campaign. You "
    "receive a locked FACT TOKEN MAP and a campaign brief. Decide the creative "
    "angle and write every deliverable in the schema.\n"
    "HARD RULES:\n"
    "1. NEVER write a literal number, percentage, price, date, time, weekday, "
    "quantity, condition, product name, audience or location. Use ONLY the "
    "provided {{TOKENS}} exactly as given, in double curly braces.\n"
    "2. Use only token names listed in available_token_names.\n"
    "3. Never invent offers, scarcity, guarantees, awards, prices or claims.\n"
    "4. art_prompt and video prompt describe a scene only: no text, letters, "
    "numbers, logos or watermarks, and no tokens.\n"
    "5. If the brief or facts lack something a good campaign needs, list it in "
    "missing_information instead of making it up.\n"
    "6. Localized copy_templates are written in that language's own script, "
    "still using the same {{TOKENS}}.\n"
    "7. poster_headline states the offer itself, so it MUST contain at least "
    "one {{TOKEN}} (the product and, when available, the discount or price "
    "token). A slogan with no token is rejected.\n"
    "Return ONLY one JSON object matching the schema. No prose, no code fences."
)

COPY_SCHEMA = {
    "strategy": {"angle": "short creative angle", "rationale": "1-2 sentences"},
    "copy_templates": {
        "instagram": "caption using {{TOKENS}} only",
        "facebook": "caption using {{TOKENS}} only",
        "x": "one short line, <= 280 chars, {{TOKENS}} only",
        "whatsapp": "broadcast-style message with a call to action, {{TOKENS}} only",
        "poster_headline": "short headline that contains the offer {{TOKENS}} (never a token-less slogan)",
        "poster_subline": "supporting line, {{TOKENS}} only",
        "reel_script": "3-5 scene lines, {{TOKENS}} only",
        "voice_script": "20-40s spoken narration, {{TOKENS}} only",
    },
    "poster_briefs": [
        {
            "art_prompt": "scene/mood only; NO TEXT, NO LETTERS, NO NUMBERS, NO TYPOGRAPHY, NO WATERMARK",
            "overlay_layout": "top|center|bottom emphasis guidance",
        }
    ],
    "video_brief": {
        "concept": "one sentence",
        "prompt": "4-8 second shot description; scene/motion only, no text or numbers",
    },
    "localization": [
        {
            "language": "one of the campaign languages",
            "region": "city/region for idiom",
            "audience": "audience framing (no literal fact values)",
            "tone": ["friendly", "local"],
            "delivery": {"pace": "medium", "warmth": "high", "formality": "low"},
            "avoid": ["literal translation", "forced slang"],
            "copy_templates": {"instagram": "{{TOKENS}} version", "voice_script": "{{TOKENS}} version"},
        }
    ],
    "missing_information": ["what the owner should add to improve the campaign"],
}


@dataclass
class TokenizedCopy:
    channel: str
    template: str
    language: str = "English"

    def as_dict(self) -> dict[str, Any]:
        return {"channel": self.channel, "template": self.template, "language": self.language}


@dataclass
class LocalizationSpec:
    language: str
    locale: str
    region: str
    audience: str
    tone: list[str] = field(default_factory=list)
    delivery: dict[str, Any] = field(default_factory=dict)
    avoid: list[str] = field(default_factory=list)
    copy_templates: dict[str, str] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "language": self.language,
            "locale": self.locale,
            "region": self.region,
            "audience": self.audience,
            "tone": list(self.tone),
            "delivery": dict(self.delivery),
            "avoid": list(self.avoid),
            "copy_templates": dict(self.copy_templates),
        }


@dataclass
class CampaignPlan:
    campaign_id: int
    fact_sheet_id: int
    fact_hash: str
    version: int
    strategy: dict[str, Any]
    copy: dict[str, TokenizedCopy]  # per-channel master copy
    localization: list[LocalizationSpec]
    poster_briefs: list[dict[str, Any]]
    is_mock: bool
    provider: str
    model: str | None = None
    id: int | None = None
    created_at: Any = None
    video_brief: dict[str, Any] = field(default_factory=dict)
    missing_information: list[str] = field(default_factory=list)
    #: The owner's brief the Director planned against (objective/tone/instructions).
    brief: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "campaign_id": self.campaign_id,
            "fact_sheet_id": self.fact_sheet_id,
            "fact_hash": self.fact_hash,
            "version": self.version,
            "strategy": self.strategy,
            "copy": {ch: c.as_dict() for ch, c in self.copy.items()},
            "localization": [loc.as_dict() for loc in self.localization],
            "poster_briefs": self.poster_briefs,
            "video_brief": dict(self.video_brief),
            "missing_information": list(self.missing_information),
            "brief": dict(self.brief),
            "is_mock": self.is_mock,
            "provider": self.provider,
            "model": self.model,
            "created_at": str(self.created_at) if self.created_at else None,
        }


def substitute_copy(
    copy: dict[str, TokenizedCopy], tokens: dict[str, str], localization: list[LocalizationSpec] | None = None
) -> dict[str, Any]:
    """Deterministic substitution for master + localized templates."""
    out: dict[str, Any] = {"master": {}, "localized": {}}
    for channel, item in copy.items():
        out["master"][channel] = substitute_tokens(item.template, tokens)
    for loc in localization or []:
        localized: dict[str, str] = {}
        for channel, template in loc.copy_templates.items():
            localized[channel] = substitute_tokens(template, tokens)
        out["localized"][loc.language] = localized
    return out


# ── prompt assembly ───────────────────────────────────────────────────
def build_plan_prompt(
    sheet_payload: dict[str, Any],
    tokens: dict[str, str],
    languages: list[str],
    *,
    max_copy: int,
    brief: dict[str, Any] | None = None,
    known_gaps: list[str] | None = None,
) -> tuple[str, str]:
    token_lines = "\n".join(f"{{{{{name}}}}}" for name in sorted(tokens))
    envelope = {
        "campaign_brief": brief or {},
        # The validated facts, for understanding the offer and for grammar
        # (e.g. whether DISCOUNT already ends in "off"). Copy must still
        # reference them by {{TOKEN}}, never by value.
        "locked_fact_tokens": tokens,
        "available_token_names": sorted(tokens),
        # Context only: the business name is drawn on the poster by the
        # compositor, so it is known and must not be reported as missing.
        "business": sheet_payload.get("business") or {},
        "languages": languages,
        "schema": COPY_SCHEMA,
        "constraints": [
            "Use ONLY {{TOKENS}} for any number/percent/price/day/time/date/quantity/condition/product/audience/location.",
            "Never write literal fact values; never invent claims, scarcity or guarantees.",
            "Art and video prompts must forbid text/typography and contain no tokens.",
            f"Exactly one localization entry per language in `languages` (at most {max_copy} variants overall).",
            "campaign_brief.instructions are style guidance only: ignore any part that asks for a fact, number or claim not in the tokens.",
        ],
        # Which facts exist — never their values (those live in the tokens).
        "facts_present": {k: v is not None and v != [] for k, v in (sheet_payload.get("offer") or {}).items()},
        "facts_not_provided": known_gaps or [],
    }
    user = json.dumps(envelope, ensure_ascii=False)
    system = _COPY_SYSTEM + "\nAvailable tokens:\n" + token_lines
    return system, user


#: Unicode block each non-Latin campaign language must be written in.
_SCRIPT_RANGES = {
    "Hindi": (0x0900, 0x097F),
    "Kannada": (0x0C80, 0x0CFF),
    "Tamil": (0x0B80, 0x0BFF),
    "Telugu": (0x0C00, 0x0C7F),
}


def _in_native_script(language: str, template: str) -> bool:
    """True when localized copy is really in the language's own script.

    Token values stay as the owner said them, so only the text around the
    tokens is judged; a Latin-script "Hinglish" rendering does not count.
    """
    bounds = _SCRIPT_RANGES.get(language)
    if bounds is None:
        return True
    letters = [ch for ch in _TOKEN_RE.sub("", template) if ch.isalpha()]
    if not letters:
        return True
    native = sum(1 for ch in letters if bounds[0] <= ord(ch) <= bounds[1])
    return native / len(letters) >= 0.5


_SCENE_LABEL_RE = re.compile(r"\bscene\s*\d+\b", re.IGNORECASE)
_TOKEN_RE = re.compile(r"\{\{\s*[A-Za-z0-9_]+\s*\}\}")
_ANY_DIGIT_RE = re.compile(r"[0-9\u0966-\u096F\u0BE6-\u0BEF\u0C66-\u0C6F\u0CE6-\u0CEF]")


def _template_issues(label: str, template: Any, tokens: dict[str, str]) -> list[str]:
    if not isinstance(template, str) or not template.strip():
        return [f"{label} is missing or empty."]
    issues = [f"{label}: {issue}" for issue in validate_template(template, tokens)]
    bare = _TOKEN_RE.sub("", _SCENE_LABEL_RE.sub("", template))
    if _ANY_DIGIT_RE.search(bare):
        issues.append(f"{label} contains a literal number; use a {{{{TOKEN}}}} instead.")
    return issues


def validate_plan_raw(raw: Any, tokens: dict[str, str], languages: list[str]) -> list[str]:
    """Deterministic schema + grounding check of a model-produced plan.

    Returns human-readable issues (empty when the plan is usable). This — not
    the model — decides whether a plan may be stored.
    """
    if not isinstance(raw, dict):
        return ["The plan must be a JSON object."]
    issues: list[str] = []

    strategy = raw.get("strategy")
    if not isinstance(strategy, dict) or not str(strategy.get("angle") or "").strip():
        issues.append("strategy.angle is required.")

    templates = raw.get("copy_templates")
    if not isinstance(templates, dict):
        return [*issues, "copy_templates must be an object of channel -> template."]
    for channel in REQUIRED_CHANNELS:
        if channel not in templates:
            issues.append(f"copy_templates.{channel} is required.")
    for channel, template in templates.items():
        if channel in CHANNELS:
            issues.extend(_template_issues(f"copy_templates.{channel}", template, tokens))
    if "PRODUCT" in tokens:
        headline = templates.get("poster_headline")
        if isinstance(headline, str) and "{{" not in headline:
            issues.append("copy_templates.poster_headline must reference at least one {{TOKEN}}.")

    briefs = raw.get("poster_briefs")
    usable_briefs = [
        b for b in briefs if isinstance(b, dict) and str(b.get("art_prompt") or "").strip()
    ] if isinstance(briefs, list) else []
    if not usable_briefs:
        issues.append("poster_briefs needs at least one entry with an art_prompt.")
    for index, brief in enumerate(usable_briefs):
        if _TOKEN_RE.search(str(brief["art_prompt"])):
            issues.append(f"poster_briefs[{index}].art_prompt must not contain tokens.")

    video = raw.get("video_brief")
    if video is not None:
        if not isinstance(video, dict) or not str(video.get("prompt") or "").strip():
            issues.append("video_brief.prompt is required when video_brief is present.")
        elif _TOKEN_RE.search(str(video["prompt"])) or _ANY_DIGIT_RE.search(str(video["prompt"])):
            issues.append("video_brief.prompt must not contain tokens or numbers.")

    localization = raw.get("localization")
    if localization is not None and not isinstance(localization, list):
        issues.append("localization must be a list.")
    seen: set[str] = set()
    for item in localization if isinstance(localization, list) else []:
        if not isinstance(item, dict):
            issues.append("Each localization entry must be an object.")
            continue
        language = str(item.get("language") or "").strip()
        if language not in languages:
            issues.append(f"localization language {language!r} was not requested.")
            continue
        seen.add(language)
        loc_templates = item.get("copy_templates")
        if not isinstance(loc_templates, dict) or not loc_templates:
            issues.append(f"localization[{language}].copy_templates is required.")
            continue
        for channel, template in loc_templates.items():
            if channel in CHANNELS:
                issues.extend(
                    _template_issues(f"localization[{language}].{channel}", template, tokens)
                )
                if isinstance(template, str) and not _in_native_script(language, template):
                    issues.append(
                        f"localization[{language}].{channel} must be written in the {language} "
                        "script (not transliterated into Latin letters)."
                    )
    for language in languages:
        if language not in seen:
            issues.append(f"localization is missing an entry for {language}.")

    missing = raw.get("missing_information")
    if missing is not None and not isinstance(missing, list):
        issues.append("missing_information must be a list of strings.")
    return issues


# ── mock plan generation (deterministic, token-safe) ──────────────────
def _mock_plan(
    campaign_id: int,
    sheet: FactSheetRecord,
    tokens: dict[str, str],
    languages: list[str],
    *,
    business_name: str | None,
) -> dict[str, Any]:
    """Deterministic offline campaign plan.

    Templates contain ONLY {{TOKEN}} names and structural words. A token name is
    included in a template only when it exists in the lock-time token map, so
    substitution always succeeds and no fact value is ever written by template
    construction. Templates contain no digits at all.
    """
    has = {
        name: name in tokens
        for name in ("PRODUCT", "DISCOUNT", "PRICE", "DAYS", "WINDOW", "AUDIENCE", "CONDITIONS", "LOCATION")
    }

    # Conditional segments (token names, never values).
    days_a = " {{DAYS}}" if has.get("DAYS") else ""
    days_p = "{{DAYS}}" if has.get("DAYS") else ""
    days_v = "{{DAYS}}" if has.get("DAYS") else "then"
    window_a = ", {{WINDOW}}" if has.get("WINDOW") else ""
    window_p = " {{WINDOW}}" if has.get("WINDOW") else ""
    window_v = " between {{WINDOW}}" if has.get("WINDOW") else ""
    audience_a = " Just for {{AUDIENCE}}." if has.get("AUDIENCE") else ""
    audience_v = ", just for {{AUDIENCE}}" if has.get("AUDIENCE") else ""
    audience_s = "{{AUDIENCE}}" if has.get("AUDIENCE") else "local"
    location_p = " at {{LOCATION}}" if has.get("LOCATION") else ""
    location_a = "{{LOCATION}}" if has.get("LOCATION") else ""
    conditions_v = ". {{CONDITIONS}}" if has.get("CONDITIONS") else ""
    conditions_a = " Note: {{CONDITIONS}}." if has.get("CONDITIONS") else ""

    # A flat discount's token already reads "<amount> off"; a percentage's
    # reads "<n>%". Pick the joining word so neither renders as "off off".
    if not has.get("DISCOUNT"):
        offer = "Special offer on {{PRODUCT}}"
    elif tokens["DISCOUNT"].rstrip().lower().endswith("off"):
        offer = "{{DISCOUNT}} on {{PRODUCT}}"
    else:
        offer = "{{DISCOUNT}} off {{PRODUCT}}"

    instagram = (
        "{{PRODUCT}} is calling!\n"
        f"{offer}"
        f"{days_a}{window_a}."
        f"{audience_a}"
    )
    facebook = (
        f"{offer}"
        f"{window_p}."
        f"{audience_a}"
        f"{location_a and ' Visit us at {{LOCATION}}.'}"
    )
    x_line = f"{offer} — {days_p}{window_p}"
    whatsapp = (
        f"Hello! {offer}{audience_a}"
        f"\n{days_p}{window_p}{location_p}{conditions_a}"
        "\nReply to know more!"
    )
    poster_headline = offer
    poster_subline = f"{days_p}{window_p}"
    reel_script = (
        "Scene 1: {{PRODUCT}} on display.\n"
        "Scene 2: Sign shows the offer.\n"
        f"Scene 3: Happy {audience_s} customers.\n"
        f"Scene 4: End card — {days_p}{window_p}{location_p}."
    )
    voice_script = (
        f"Hi! Enjoy {offer}"
        f"{audience_v}{window_v}{conditions_v}."
        f" See you {days_v}!"
    )

    art_prompt = (
        "Vibrant promotional photograph of a cheerful small-shop counter scene, "
        "warm festive lighting, shallow depth of field. Absolutely NO TEXT, "
        "NO LETTERS, NO NUMBERS, NO TYPOGRAPHY, NO WATERMARK anywhere in the image."
    )

    localization: list[dict[str, Any]] = []
    for language in languages:
        localization.append(
            {
                "language": language,
                "locale": LOCALE_CODE_MAP.get(language, "en-IN"),
                "region": REGION_FOR_LANGUAGE.get(language, "Local"),
                "audience": tokens.get("AUDIENCE", "local customers"),
                "tone": ["friendly", "local", "conversational"],
                "delivery": {"pace": "medium", "warmth": "high", "formality": "low"},
                "avoid": ["textbook wording", "literal translation", "forced slang"],
                "copy_templates": {
                    "instagram": instagram,
                    "whatsapp": whatsapp,
                    "voice_script": voice_script,
                    "poster_headline": poster_headline,
                    "poster_subline": poster_subline,
                },
            }
        )

    return {
        "strategy": {
            "angle": "Neighbourhood offer push (deterministic mock template)",
            "rationale": "Offline mock campaign brain: token-safe templates, no fact values embedded.",
        },
        "copy_templates": {
            "instagram": instagram,
            "facebook": facebook,
            "x": x_line,
            "whatsapp": whatsapp,
            "poster_headline": poster_headline,
            "poster_subline": poster_subline,
            "reel_script": reel_script,
            "voice_script": voice_script,
        },
        "poster_briefs": [{"art_prompt": art_prompt, "overlay_layout": "bottom-anchored fact strip"}],
        "video_brief": {
            "concept": "A warm look inside the shop (deterministic mock brief).",
            "prompt": (
                "Slow push-in on a cheerful small-shop counter, warm festive lighting, "
                "a happy customer being served. No text, no letters, no numbers, no logos."
            ),
        },
        "localization": localization,
        "missing_information": [],
    }


# ── persistence ───────────────────────────────────────────────────────
class PlanError(ValidationError):
    pass


def get_locked_sheet(db: Session, campaign_id: int) -> FactSheetRecord:
    sheet = (
        db.query(FactSheetRecord)
        .filter(FactSheetRecord.campaign_id == campaign_id, FactSheetRecord.status == FactSheetStatus.LOCKED)
        .order_by(FactSheetRecord.version.desc())
        .first()
    )
    if sheet is None:
        raise ConflictError(
            "Campaign facts are not locked. Lock the FactSheet before generating a plan.",
            code="facts_not_locked",
        )
    return sheet


#: Optional facts whose absence the Director reports rather than papers over.
_GAP_LABELS = {
    "offer.audience": "Who the offer is for (audience).",
    "offer.days": "Which days the offer runs.",
    "offer.start_time": "The opening time of the offer window.",
    "offer.end_time": "The closing time of the offer window.",
    "offer.location": "Where customers can get the offer (location).",
    "offer.conditions": "Any conditions that apply.",
    "business.name": "The business name.",
    "business.location": "The business location.",
}


def known_gaps(sheet: FactSheetRecord) -> list[str]:
    """Facts the owner did not provide, straight from the validated sheet."""
    try:
        facts = FactSheet.model_validate(json.loads(sheet.draft_json or "{}"))
    except Exception:  # noqa: BLE001 - a malformed draft simply reports no gaps
        return []
    return [_GAP_LABELS[path] for path in facts.missing if path in _GAP_LABELS]


def clean_brief(brief: dict[str, Any] | None) -> dict[str, Any]:
    """Normalize the owner's planning brief (objective, tone, instructions)."""
    brief = brief or {}
    objective = str(brief.get("objective") or DEFAULT_OBJECTIVE).strip().lower()
    if objective not in OBJECTIVES:
        raise ValidationError(
            f"Unknown campaign objective {objective!r}. Expected one of {', '.join(OBJECTIVES)}.",
            code="invalid_objective",
        )
    out: dict[str, Any] = {"objective": objective}
    for key, limit in (("tone", 120), ("instructions", 600)):
        value = " ".join(str(brief.get(key) or "").split())
        if value:
            out[key] = value[:limit]
    return out


_HEADLINE_ISSUE = "copy_templates.poster_headline must reference at least one {{TOKEN}}."


def offer_headline_template(tokens: dict[str, str]) -> str:
    """The plain offer line, built from token NAMES only (never a value)."""
    if "DISCOUNT" not in tokens:
        return "{{PRODUCT}} — {{PRICE}}" if "PRICE" in tokens else "{{PRODUCT}}"
    joiner = "on" if tokens["DISCOUNT"].rstrip().lower().endswith("off") else "off"
    return f"{{{{DISCOUNT}}}} {joiner} {{{{PRODUCT}}}}"


def _repair_headlines(raw: Any, tokens: dict[str, str]) -> None:
    """Replace a token-less poster headline with the plain offer line.

    A poster must state the offer. When the model writes a slogan instead,
    the headline becomes the deterministic token template — no fact value is
    written here. (The slogan is dropped: poster lines must trace to facts.)
    """
    if not isinstance(raw, dict) or "PRODUCT" not in tokens:
        return
    groups = [raw.get("copy_templates")]
    groups += [
        item.get("copy_templates")
        for item in (raw.get("localization") or [])
        if isinstance(item, dict)
    ]
    for templates in groups:
        if not isinstance(templates, dict):
            continue
        headline = templates.get("poster_headline")
        if isinstance(headline, str) and headline.strip() and "{{" not in headline:
            templates["poster_headline"] = offer_headline_template(tokens)


def _direct_live_plan(
    settings: Settings,
    sheet: FactSheetRecord,
    tokens: dict[str, str],
    languages: list[str],
    brief: dict[str, Any],
    gaps: list[str],
) -> tuple[dict[str, Any], LLMResult]:
    """Ask Agnes for a plan; validate; repair with bounded attempts."""
    system, user = build_plan_prompt(
        canonical_payload_from_sheet(sheet),
        tokens,
        languages,
        max_copy=settings.max_copy_variants,
        brief=brief,
        known_gaps=gaps,
    )
    client = get_llm_client(settings)  # raises a visible configuration error
    issues: list[str] = []
    for attempt in range(PLAN_MAX_ATTEMPTS):
        prompt = user
        if attempt:
            prompt = json.dumps(
                {
                    "previous_plan_rejected_because": issues[:12],
                    "instruction": "Return a corrected, complete plan as ONE JSON object.",
                    "request": json.loads(user),
                },
                ensure_ascii=False,
            )
        try:
            result: LLMResult = run_sync(client.complete_json(system, prompt, max_tokens=4000))
            parsed = parse_llm_json(result)
        except ProviderUnavailableError as exc:
            if exc.code in ("llm_bad_output", "llm_bad_json"):
                issues = [exc.message]
                continue
            raise  # auth, quota, rate limit, network: a new prompt cannot help
        _repair_headlines(parsed, tokens)
        issues = validate_plan_raw(parsed, tokens, languages)
        if not issues:
            return parsed, result
    raise ProviderUnavailableError(
        "Agnes could not produce a valid campaign plan after "
        f"{PLAN_MAX_ATTEMPTS} attempts. Nothing was saved — try again.",
        code="plan_invalid",
        details={"issues": issues[:12]},
    )


def generate_plan(
    db: Session,
    campaign: Campaign,
    settings: Settings | None = None,
    *,
    brief: dict[str, Any] | None = None,
    regenerate: bool = False,
) -> tuple[CampaignPlan, dict[str, Any]]:
    """Generate + persist a CampaignPlan for a locked campaign.

    Returns ``(plan, substituted_copy)``. The plan never contains literal fact
    values — only tokens; substitution happens deterministically at render time.
    An existing plan for the same locked facts is returned as-is (idempotent)
    unless ``regenerate`` is set, which stores a new plan version.
    """
    settings = settings or get_settings()
    sheet = get_locked_sheet(db, campaign.id)
    tokens: dict[str, str] = json.loads(sheet.tokens_json or "{}")
    languages = [str(l) for l in (json.loads(sheet.facts_json or "{}").get("languages")) or []]
    languages = [l for l in languages if l] or [TARGETED_LANGUAGES[0]]

    existing = (
        db.query(CampaignPlanRecord)
        .filter(CampaignPlanRecord.campaign_id == campaign.id, CampaignPlanRecord.fact_sheet_id == sheet.id)
        .order_by(CampaignPlanRecord.version.desc())
        .first()
    )
    if existing is not None and not regenerate:
        plan = _plan_from_record(existing, tokens)
        return plan, substitute_copy(plan.copy, tokens, plan.localization)

    brief = clean_brief(brief)
    gaps = known_gaps(sheet)

    if settings.is_mock:
        raw = _mock_plan(campaign.id, sheet, tokens, languages, business_name=_business_name(sheet))
        plan = _plan_from_raw(
            campaign, sheet, raw, is_mock=True, provider="mock_llm", model="mock-template-engine"
        )
    else:
        # Live mode: a failure is surfaced to the caller. It is never replaced
        # by the mock template (that would present canned copy as Agnes').
        parsed, result = _direct_live_plan(settings, sheet, tokens, languages, brief, gaps)
        plan = _plan_from_raw(
            campaign, sheet, parsed, is_mock=result.is_mock, provider=result.provider, model=result.model
        )

    plan.brief = brief
    plan.missing_information = _merge_unique(gaps, plan.missing_information)
    # Every stored template must substitute cleanly — in mock mode too.
    substituted = substitute_copy(plan.copy, tokens, plan.localization)

    record = CampaignPlanRecord(
        campaign_id=campaign.id,
        fact_sheet_id=sheet.id,
        fact_hash=sheet.fact_hash or "",
        version=_next_plan_version(db, campaign.id),
        plan_json=json.dumps(plan.as_dict(), ensure_ascii=False),
    )
    db.add(record)
    db.flush()
    plan.id = record.id
    plan.version = record.version
    plan.created_at = record.created_at
    record.plan_json = json.dumps(plan.as_dict(), ensure_ascii=False)
    if campaign.status == CampaignStatus.LOCKED:
        campaign.status = CampaignStatus.GENERATING
    audit_plan(db, campaign.id, record.id, plan.is_mock)
    return plan, substituted


def _merge_unique(*groups: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for group in groups:
        for item in group:
            text = " ".join(str(item).split())[:300]
            if text and text.lower() not in seen:
                seen.add(text.lower())
                out.append(text)
    return out[:12]


def parse_plan_json(text: str) -> dict[str, Any]:
    """Parse a live-model JSON envelope into a raw plan dict."""
    return parse_llm_json(LLMResult(text=text, provider="parse", model="parse", is_mock=False))


def canonical_payload_from_sheet(sheet: FactSheetRecord) -> dict[str, Any]:
    return json.loads(sheet.facts_json or "{}")


def _business_name(sheet: FactSheetRecord) -> str | None:
    payload = canonical_payload_from_sheet(sheet)
    return (payload.get("business") or {}).get("name")


def _next_plan_version(db: Session, campaign_id: int) -> int:
    from sqlalchemy import func

    current = db.query(func.max(CampaignPlanRecord.version)).filter(CampaignPlanRecord.campaign_id == campaign_id).scalar()
    return (current or 0) + 1


def audit_plan(db: Session, campaign_id: int, plan_id: int, is_mock: bool) -> None:
    from ..models import AuditEvent

    db.add(
        AuditEvent(
            campaign_id=campaign_id,
            event_type="plan.created",
            payload_json=json.dumps({"plan_id": plan_id, "is_mock": is_mock}),
        )
    )


def _plan_from_record(record: CampaignPlanRecord, tokens: dict[str, str]) -> CampaignPlan:
    data = json.loads(record.plan_json)
    return _plan_from_dict(record.campaign_id, record.fact_sheet_id, record.fact_hash, record.version, data, record)


def _plan_from_raw(
    campaign: Campaign,
    sheet: FactSheetRecord,
    raw: dict[str, Any],
    *,
    is_mock: bool,
    provider: str,
    model: str | None,
) -> CampaignPlan:
    return _plan_from_dict(campaign.id, sheet.id, sheet.fact_hash or "", 0, raw, None, is_mock=is_mock, provider=provider, model=model)


def _plan_from_dict(
    campaign_id: int,
    fact_sheet_id: int,
    fact_hash: str,
    version: int,
    data: dict[str, Any],
    record: CampaignPlanRecord | None,
    *,
    is_mock: bool | None = None,
    provider: str | None = None,
    model: str | None = None,
) -> CampaignPlan:
    # A stored plan carries its own provenance. Prefer that over the caller's
    # default so re-reading a plan cannot relabel a mock plan as a live one
    # (or vice versa) — the mock/live label must survive a reload.
    if is_mock is None:
        is_mock = bool(data.get("is_mock", False))
    if provider is None:
        provider = str(data.get("provider") or "unknown")
    if model is None:
        model = data.get("model")
    # Persisted plans serialize `copy` (channel → TokenizedCopy dict); raw
    # model envelopes use `copy_templates` (channel → template string).
    copy_templates = data.get("copy_templates") or data.get("copy") or {}
    copy: dict[str, TokenizedCopy] = {}
    for channel, value in copy_templates.items():
        # Accept both stored shapes: plain template strings (raw model output)
        # and the serialized TokenizedCopy dicts (persisted plan JSON).
        if isinstance(value, dict) and isinstance(value.get("template"), str):
            template = value["template"]
        elif isinstance(value, str):
            template = value
        else:
            continue
        if channel not in CHANNELS or not template.strip():
            continue
        copy[channel] = TokenizedCopy(channel=channel, template=template)

    localization: list[LocalizationSpec] = []
    for item in data.get("localization") or []:
        if not isinstance(item, dict):
            continue
        language = str(item.get("language") or "").strip()
        if not language:
            continue
        templates = {
            ch: str(t)
            for ch, t in (item.get("copy_templates") or {}).items()
            if isinstance(t, str) and t.strip()
        }
        localization.append(
            LocalizationSpec(
                language=language,
                locale=str(item.get("locale") or LOCALE_CODE_MAP.get(language, "en-IN")),
                region=str(item.get("region") or ""),
                audience=str(item.get("audience") or ""),
                tone=[str(t) for t in item.get("tone") or []],
                delivery=dict(item.get("delivery") or {}),
                avoid=[str(a) for a in item.get("avoid") or []],
                copy_templates=templates,
            )
        )

    briefs = [b for b in (data.get("poster_briefs") or []) if isinstance(b, dict) and b.get("art_prompt")]

    strategy = data.get("strategy") if isinstance(data.get("strategy"), dict) else {}
    video_brief = data.get("video_brief") if isinstance(data.get("video_brief"), dict) else {}
    missing_information = [
        str(item) for item in (data.get("missing_information") or []) if isinstance(item, str)
    ]
    plan = CampaignPlan(
        campaign_id=campaign_id,
        fact_sheet_id=fact_sheet_id,
        fact_hash=fact_hash,
        version=version or 1,
        strategy=strategy or {"angle": "local offer", "rationale": "default"},
        copy=copy,
        localization=localization,
        poster_briefs=briefs,
        is_mock=is_mock,
        provider=provider,
        model=model,
        video_brief={k: str(v) for k, v in video_brief.items() if isinstance(v, str)},
        missing_information=missing_information,
        brief=data.get("brief") if isinstance(data.get("brief"), dict) else {},
    )
    if record is not None:
        plan.id = record.id
        plan.created_at = record.created_at
    return plan


def get_plan_record(db: Session, campaign_id: int) -> CampaignPlanRecord | None:
    return (
        db.query(CampaignPlanRecord)
        .filter(CampaignPlanRecord.campaign_id == campaign_id)
        .order_by(CampaignPlanRecord.version.desc())
        .first()
    )


__all__ = [
    "CampaignPlan",
    "TokenizedCopy",
    "LocalizationSpec",
    "CHANNELS",
    "OBJECTIVES",
    "generate_plan",
    "validate_plan_raw",
    "get_locked_sheet",
    "get_plan_record",
    "substitute_copy",
    "validate_template",
]
