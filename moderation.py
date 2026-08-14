"""OpenAI Moderation API helpers for AI-generated content compliance.

Per Google Play's AI-Generated Content policy, apps must prevent generation
of restricted content (child exploitation, deceptive behavior, graphic
violence, etc.). We screen both inputs (what the user wrote) and outputs
(what the model returned) using OpenAI's omni-moderation-latest model.

Functions are designed to fail open on API errors: a moderation outage
must not block legitimate users, but each failure is logged so we can
monitor and investigate.
"""

import logging
import os
from dataclasses import dataclass, field
from typing import Dict, List

from openai import OpenAI

logger = logging.getLogger("dreamr")

_client = OpenAI()


def _read_bool_env(name: str, default: bool) -> bool:
    """Parse a truthy env var. Empty/unset → default. Accepts 1/0, true/false, yes/no, on/off."""
    raw = os.environ.get(name, "").strip().lower()
    if not raw:
        return default
    if raw in ("1", "true", "yes", "on"):
        return True
    if raw in ("0", "false", "no", "off"):
        return False
    logger.warning("[MODERATION] unparseable %s=%r, using default=%s", name, raw, default)
    return default


# Master switch. Default ON so production is safe-by-default.
# Set DREAMR_MODERATION_ENABLED=false to disable all pre/post checks
# without redeploying any code — `check()` becomes a no-op that
# always returns "not flagged".
ENABLED = _read_bool_env("DREAMR_MODERATION_ENABLED", default=True)

logger.info(
    "[MODERATION] enabled=%s (set DREAMR_MODERATION_ENABLED=false to disable)",
    ENABLED,
)

# Categories where ANY True flag should block, regardless of score.
# sexual/minors is non-negotiable per Google Play policy and the law.
HARD_BLOCK_CATEGORIES = {
    "sexual/minors",
}

# For other categories, flag when category_score >= threshold.
# Thresholds tuned conservatively — OpenAI's default `flagged` field is
# often too permissive for child-safety surfaces but too aggressive for
# a dream journal where users describe fights, fear, falling, etc.
SCORE_THRESHOLDS: Dict[str, float] = {
    "sexual": 0.85,
    "hate": 0.85,
    "hate/threatening": 0.6,
    "harassment": 0.9,
    "harassment/threatening": 0.6,
    "self-harm": 0.75,
    "self-harm/intent": 0.55,
    "self-harm/instructions": 0.5,
    "violence": 0.95,
    "violence/graphic": 0.9,
    "illicit": 0.9,
    "illicit/violent": 0.75,
}


@dataclass
class ModerationResult:
    flagged: bool
    categories: List[str] = field(default_factory=list)
    raw_scores: Dict[str, float] = field(default_factory=dict)

    def __bool__(self) -> bool:
        return self.flagged


def check(text: str, *, label: str = "input") -> ModerationResult:
    """Run text through OpenAI's omni-moderation-latest model.

    Returns a ModerationResult. Fails open: on any API error returns
    flagged=False so we don't block users when OpenAI is down. Failures
    are logged at WARNING so they're visible without spamming.

    Honors the ENABLED switch — when off, returns "not flagged" without
    making an API call.
    """
    if not ENABLED:
        return ModerationResult(False)
    if not text or not text.strip():
        return ModerationResult(False)

    try:
        resp = _client.moderations.create(
            model="omni-moderation-latest",
            # API has its own limits; truncate defensively so a huge
            # paste can't blow up the request.
            input=text[:8000],
        )
        result = resp.results[0]

        cats = (
            result.categories.model_dump()
            if hasattr(result.categories, "model_dump")
            else dict(result.categories)
        )
        scores = (
            result.category_scores.model_dump()
            if hasattr(result.category_scores, "model_dump")
            else dict(result.category_scores)
        )

        flagged_categories: List[str] = []
        for cat in HARD_BLOCK_CATEGORIES:
            if cats.get(cat):
                flagged_categories.append(cat)

        for cat, threshold in SCORE_THRESHOLDS.items():
            score = float(scores.get(cat) or 0.0)
            if score >= threshold and cat not in flagged_categories:
                flagged_categories.append(cat)

        flagged = bool(flagged_categories)
        if flagged:
            logger.warning(
                "[MODERATION] %s flagged categories=%s top_scores=%s",
                label,
                flagged_categories,
                {k: round(v, 3) for k, v in sorted(
                    scores.items(), key=lambda kv: -float(kv[1] or 0)
                )[:5]},
            )
        return ModerationResult(flagged, flagged_categories, raw_scores=scores)
    except Exception as e:
        logger.warning(
            "[MODERATION] check failed for %s (failing open): %s", label, e
        )
        return ModerationResult(False)


# User-facing copy. Keep gentle: dream content is often emotional or
# weird, and we don't want to scare off legitimate users when their
# input grazes a threshold.
INPUT_REJECTED_MESSAGE = (
    "We couldn't process this entry. It looks like it may contain content "
    "we can't analyze (for example: graphic violence, hate speech, sexual "
    "content involving minors, or detailed self-harm). If this feels like "
    "a mistake, please rephrase your dream and try again."
)

OUTPUT_FALLBACK_MESSAGE = (
    "Dreamr couldn't generate an interpretation for this dream right now. "
    "Please try again, and if this keeps happening contact support."
)
