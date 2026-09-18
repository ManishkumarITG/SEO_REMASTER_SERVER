"""Turn a store profile + a subreddit's rules into an honest, reviewable Reddit draft.

The system instruction below is the product guardrail, not a style preference: the
post must be written in first person as the store's own team, must never pose as a
customer or unaffiliated fan, must comply with the subreddit's rules, and must
mention the store URL at most once. Gemini also self-reports a fit_score and
rule_check_notes so the reviewer can see why a draft may not belong in that sub.

Lengths are validated server-side afterwards. Nothing is silently truncated - an
over-long draft becomes an error the UI shows, so the user stays in control.
"""

from __future__ import annotations

import json
import logging
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field

from exceptions.backlinks import DraftValidationError
from integrations.google.gemini import GeminiService, get_gemini_service
from services.backlinks.store_profile import StoreProfile

logger = logging.getLogger("seo.backlinks.generator")

#: Reddit's own hard limits.
TITLE_MAX_CHARS = 300
BODY_MAX_CHARS = 40_000


class PostAngle(StrEnum):
    INTRO = "intro"
    BEHIND_THE_BRAND = "behind_the_brand"
    FEEDBACK = "feedback"
    ANNOUNCEMENT = "announcement"
    HELPFUL = "helpful"


ANGLE_BRIEFS: dict[PostAngle, str] = {
    PostAngle.INTRO: "Introduce the shop and the story behind starting it.",
    PostAngle.BEHIND_THE_BRAND: "Explain how and why the products are made, with concrete detail.",
    PostAngle.FEEDBACK: "Genuinely ask the community for feedback, with specific questions.",
    PostAngle.ANNOUNCEMENT: "Share a new product or collection plainly, without hype.",
    PostAngle.HELPFUL: (
        "Write a post that is useful to this subreddit's readers on its own merits, "
        "mentioning the shop only once."
    ),
}


class SubredditRule(BaseModel):
    short_name: str = ""
    description: str = ""


class LinkFlair(BaseModel):
    id: str = ""
    text: str = ""


class SubredditContext(BaseModel):
    name: str
    title: str = ""
    public_description: str = ""
    rules: list[SubredditRule] = Field(default_factory=list)
    flairs: list[LinkFlair] = Field(default_factory=list)


class RedditDraftSchema(BaseModel):
    """The exact JSON contract handed to Gemini as `response_schema`."""

    title: str = Field(description="Post title, at most 300 characters, no clickbait")
    body_markdown: str = Field(description="Reddit markdown body, 120-350 words")
    suggested_flair_text: str | None = Field(
        default=None, description="Flair text to use, or null when none applies"
    )
    disclosure_included: bool = Field(
        description="True only if the post clearly states the author runs the store"
    )
    fit_score: int = Field(
        ge=1, le=5, description="1-5: how well this post fits the subreddit's rules and culture"
    )
    rule_check_notes: list[str] = Field(
        default_factory=list,
        description="One line per relevant rule and how the draft complies, or a warning",
    )



SYSTEM_INSTRUCTION = """\
You write Reddit posts for a small e-commerce brand. The author is the store's owner or team, \
posting from their own Reddit account.

Rules:
1. Transparency: write in first person as the store team ("we run a small shop that..."). Never \
pose as a customer, neutral reviewer, or unaffiliated fan. Set disclosure_included=true only if \
the post clearly states the author runs the store.
2. Value first: lead with something the subreddit's readers want - a story, a lesson learned, a \
useful detail, or a real request for feedback. This is not an ad. No sales pitch structure, no \
calls to buy.
3. Follow the subreddit rules provided. If a rule forbids self-promotion, says a flair or format \
is required, or restricts links, comply - and record what you did (or why the post may not fit) \
in rule_check_notes. Give a low fit_score (1-2) if the post likely violates a rule.
4. Mention the store URL at most once, naturally, near the end. Never use urgency/scarcity \
language, superlatives you cannot support, invented numbers, or fake testimonials.
5. Reddit markdown only. No emojis, no hashtags, no marketing cliches ("game-changer", \
"revolutionary"), no ALL CAPS.
6. Title: specific, honest, <= 300 characters, no clickbait. Body: 120-350 words, short \
paragraphs.
7. Output only JSON matching the provided schema."""


def _profile_payload(profile: StoreProfile) -> dict[str, Any]:
    """Only facts we actually fetched - the model must not invent numbers."""
    return {
        "store_url": profile.store_url,
        "store_name": profile.store_name,
        "tagline": profile.tagline,
        "description_excerpt": profile.description_excerpt,
        "is_shopify": profile.is_shopify,
        "categories": profile.categories,
        "products": [
            {
                "title": product.title,
                "product_type": product.product_type,
                "vendor": product.vendor,
                "blurb": product.blurb,
                "price": product.price,
            }
            for product in profile.products
        ],
    }


def _subreddit_payload(subreddit: SubredditContext) -> dict[str, Any]:
    return {
        "name": subreddit.name,
        "title": subreddit.title,
        "public_description": subreddit.public_description,
        "rules": [
            {"short_name": rule.short_name, "description": rule.description}
            for rule in subreddit.rules
        ],
        "available_flairs": [flair.text for flair in subreddit.flairs if flair.text],
    }


def build_prompt(
    profile: StoreProfile,
    subreddit: SubredditContext,
    angle: PostAngle,
    extra_notes: str | None = None,
) -> str:
    sections = [
        "Write one Reddit post for this store, for this subreddit.",
        "",
        f"Chosen angle: {angle.value} - {ANGLE_BRIEFS[angle]}",
        "",
        "STORE PROFILE (JSON - these are the only facts you may state):",
        json.dumps(_profile_payload(profile), indent=2, ensure_ascii=False),
        "",
        f"SUBREDDIT CONTEXT (JSON) for r/{subreddit.name}:",
        json.dumps(_subreddit_payload(subreddit), indent=2, ensure_ascii=False),
    ]
    if extra_notes and extra_notes.strip():
        sections += ["", "AUTHOR'S EXTRA NOTES (honour these unless they break a rule):", extra_notes.strip()]
    return "\n".join(sections)


def validate_draft(draft: RedditDraftSchema) -> None:
    """Enforce Reddit's hard limits. Raises rather than trimming."""
    problems: list[str] = []
    title = draft.title.strip()
    body = draft.body_markdown.strip()
    if not title:
        problems.append("the title is empty")
    if len(title) > TITLE_MAX_CHARS:
        problems.append(f"the title is {len(title)} characters (Reddit's limit is {TITLE_MAX_CHARS})")
    if not body:
        problems.append("the body is empty")
    if len(body) > BODY_MAX_CHARS:
        problems.append(f"the body is {len(body)} characters (Reddit's limit is {BODY_MAX_CHARS})")
    if problems:
        raise DraftValidationError(
            "The generated draft cannot be published as-is: " + "; ".join(problems) + "."
        )


async def generate_reddit_draft(
    profile: StoreProfile,
    subreddit: SubredditContext,
    angle: PostAngle,
    extra_notes: str | None = None,
    *,
    service: GeminiService | None = None,
) -> RedditDraftSchema:
    """Generate one draft and validate it against Reddit's limits."""
    gemini = service or get_gemini_service()
    draft = await gemini.generate_structured(
        prompt=build_prompt(profile, subreddit, angle, extra_notes),
        system_instruction=SYSTEM_INSTRUCTION,
        schema=RedditDraftSchema,
        temperature=0.7,
    )
    draft.title = draft.title.strip()
    draft.body_markdown = draft.body_markdown.strip()
    validate_draft(draft)
    if not draft.disclosure_included:
        logger.warning(
            "Draft for r/%s reports disclosure_included=false - the UI will warn the reviewer",
            subreddit.name,
        )
    return draft
