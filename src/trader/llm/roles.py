"""Claude's three roles (ARCHITECTURE.md §7.2; STRATEGY.md §5.2, §5.3, §6.1; DECISIONS D-064).

A ``RoleSpec`` is everything a call needs to know about its role: the answer's model, the
system prompt's file, the instruction that follows ``-p``, which list of the answer names the
pack's candidates or targets, and the role's settings (model, effort, time and turn limits).
"""

import hashlib
from dataclasses import dataclass
from pathlib import Path

from pydantic import BaseModel

from trader.config import Settings
from trader.llm.context import Role
from trader.llm.schemas import DailyReview, PremarketBrief, VetBatch

COPY_PACK_ID = "Copy the pack's pack_id into your answer."


@dataclass(frozen=True)
class RoleSpec:
    role: Role
    output: type[BaseModel]
    prompt_file: str  # under prompts/
    instruction: str  # the text right after -p (plan decision 2)
    members: str  # the answer's list that names the pack's items
    member_key: str  # what names an item of that list
    model: str
    effort: str
    timeout_s: int
    max_turns: int
    min_attempt_s: int


def prompt_sha256(path: Path) -> str:
    """A prompt's SHA-256, its line endings made LF: a checkout that writes CRLF (this repo has
    ``core.autocrlf``) doesn't change what the model reads (D-065)."""
    return hashlib.sha256(path.read_bytes().replace(b"\r\n", b"\n")).hexdigest()


def role_specs(settings: Settings) -> dict[Role, RoleSpec]:
    roles = settings.llm.roles
    return {
        "premarket": RoleSpec(
            role="premarket",
            output=PremarketBrief,
            prompt_file="premarket_brief.md",
            instruction="Produce the PremarketBrief for the candidates in the JSON context pack "
            f"on stdin. {COPY_PACK_ID}",
            members="assessments",
            member_key="symbol",
            model=roles.premarket.model,
            effort=roles.premarket.effort,
            timeout_s=roles.premarket.timeout_s,
            max_turns=roles.premarket.max_turns,
            min_attempt_s=roles.premarket.min_attempt_s,
        ),
        "vet": RoleSpec(
            role="vet",
            output=VetBatch,
            prompt_file="catalyst_vet.md",
            instruction="Produce the VetBatch for the candidates in the JSON context pack on "
            f"stdin. {COPY_PACK_ID}",
            members="assessments",
            member_key="symbol",
            model=roles.vet.model,
            effort=roles.vet.effort,
            timeout_s=roles.vet.timeout_s,
            max_turns=roles.vet.max_turns,
            min_attempt_s=roles.vet.min_attempt_s,
        ),
        "review": RoleSpec(
            role="review",
            output=DailyReview,
            prompt_file="postclose_review.md",
            instruction="Produce the DailyReview for the review targets in the JSON context pack "
            f"on stdin. {COPY_PACK_ID}",
            members="signal_reviews",
            member_key="signal_id",
            model=roles.review.model,
            effort=roles.review.effort,
            timeout_s=roles.review.timeout_s,
            max_turns=roles.review.max_turns,
            min_attempt_s=roles.review.min_attempt_s,
        ),
    }
