"""generate_reddit_draft: prompt contents, schema validation, and the single JSON retry.

No network: GeminiService is replaced by a stub, so no Gemini call is ever made.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from pydantic import BaseModel, ValidationError

from services.backlinks.reddit_post_generator import (
    SYSTEM_INSTRUCTION,
    TITLE_MAX_CHARS,
    DraftValidationError,
    PostAngle,
    RedditDraftSchema,
    SubredditContext,
    SubredditRule,
    build_prompt,
    generate_reddit_draft,
    validate_draft,
)
from services.backlinks.store_profile import StoreProduct, StoreProfile
from services.integrations.gemini import GeminiResponseError, GeminiService, _strip_fences

PROFILE = StoreProfile(
    store_url="https://shop.example.com/",
    store_name="Ember & Oak",
    tagline="Hand-poured candles",
    description_excerpt="We started in a kitchen in 2019.",
    is_shopify=True,
    products=[StoreProduct(title="Cedar & Smoke Candle", product_type="Candle", price="24.00")],
    categories=["Candle"],
)

SUBREDDIT = SubredditContext(
    name="candlemaking",
    title="Candle Making",
    public_description="For people who make candles.",
    rules=[SubredditRule(short_name="No self-promotion", description="No shop links.")],
)

VALID_DRAFT = {
    "title": "We have poured candles by hand since 2019 - here is what we learned",
    "body_markdown": "We run a small candle shop and wanted to share a few lessons.\n\nMore text.",
    "suggested_flair_text": "Discussion",
    "disclosure_included": True,
    "fit_score": 4,
    "rule_check_notes": ["Rule 1 (no self-promotion): the shop is mentioned once, at the end."],
}


class StubGemini(GeminiService):
    """Stands in for GeminiService: returns queued payloads, records the calls."""

    def __init__(self, payloads: list[str]) -> None:
        self.model = "stub-model"
        self._payloads = payloads
        self.prompts: list[str] = []
        self.systems: list[str] = []

    @property
    def is_configured(self) -> bool:
        return True

    async def generate_structured(
        self,
        *,
        prompt: str,
        system_instruction: str,
        schema: type[BaseModel],
        temperature: float = 0.7,
    ) -> Any:
        # Mirror the real service: validate, and retry once on a bad payload.
        self.systems.append(system_instruction)
        last: ValidationError | None = None
        raw = ""
        for _ in range(2):
            if not self._payloads:
                break
            raw = self._payloads.pop(0)
            self.prompts.append(prompt)
            try:
                return schema.model_validate_json(_strip_fences(raw))
            except ValidationError as exc:
                last = exc
        raise GeminiResponseError(f"stub: no valid JSON ({last})", raw=raw)


def test_prompt_carries_store_facts_subreddit_rules_and_angle() -> None:
    prompt = build_prompt(PROFILE, SUBREDDIT, PostAngle.FEEDBACK, "Mention the new scent.")

    assert "Ember & Oak" in prompt
    assert "Cedar & Smoke Candle" in prompt
    assert "candlemaking" in prompt
    assert "No self-promotion" in prompt
    assert "feedback" in prompt
    assert "Mention the new scent." in prompt


def test_system_instruction_states_the_transparency_rules() -> None:
    # These are product guardrails, not prose - a rewrite that drops them is a bug.
    assert "first person as the store team" in SYSTEM_INSTRUCTION
    assert "Never pose as a customer" in SYSTEM_INSTRUCTION
    assert "at most once" in SYSTEM_INSTRUCTION
    assert "fake testimonials" in SYSTEM_INSTRUCTION


async def test_valid_payload_produces_a_draft() -> None:
    stub = StubGemini([json.dumps(VALID_DRAFT)])
    draft = await generate_reddit_draft(PROFILE, SUBREDDIT, PostAngle.INTRO, service=stub)

    assert draft.fit_score == 4
    assert draft.disclosure_included is True
    assert draft.rule_check_notes
    assert stub.systems == [SYSTEM_INSTRUCTION]


async def test_invalid_json_retries_once_then_succeeds() -> None:
    stub = StubGemini(["not json at all", json.dumps(VALID_DRAFT)])
    draft = await generate_reddit_draft(PROFILE, SUBREDDIT, PostAngle.INTRO, service=stub)

    assert draft.title == VALID_DRAFT["title"]
    assert len(stub.prompts) == 2  # one retry, not more


async def test_two_bad_payloads_raise() -> None:
    stub = StubGemini(["nope", "still nope"])
    with pytest.raises(GeminiResponseError):
        await generate_reddit_draft(PROFILE, SUBREDDIT, PostAngle.INTRO, service=stub)


async def test_over_long_title_is_a_validation_error_not_a_silent_truncation() -> None:
    payload = {**VALID_DRAFT, "title": "x" * (TITLE_MAX_CHARS + 1)}
    stub = StubGemini([json.dumps(payload)])
    with pytest.raises(DraftValidationError) as excinfo:
        await generate_reddit_draft(PROFILE, SUBREDDIT, PostAngle.INTRO, service=stub)

    assert str(TITLE_MAX_CHARS) in str(excinfo.value)


def test_validate_draft_rejects_empty_fields() -> None:
    with pytest.raises(DraftValidationError):
        validate_draft(RedditDraftSchema(**{**VALID_DRAFT, "title": "   "}))
    with pytest.raises(DraftValidationError):
        validate_draft(RedditDraftSchema(**{**VALID_DRAFT, "body_markdown": ""}))


def test_fit_score_outside_one_to_five_fails_the_schema() -> None:
    with pytest.raises(ValidationError):
        RedditDraftSchema(**{**VALID_DRAFT, "fit_score": 9})
