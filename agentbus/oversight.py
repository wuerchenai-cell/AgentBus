from collections.abc import Collection, Mapping
from dataclasses import dataclass


@dataclass(frozen=True)
class OversightDecision:
    required: bool
    reason: str = ""


def review_trigger(
    *,
    message_type: str,
    status: str,
    attempt: int,
    hop: int,
    completed_count: int,
    review_every_n: int = 3,
    stuck_attempts: int = 2,
    stuck_hops: int = 2,
) -> OversightDecision:
    """Return whether an ordinary task or goal should receive peer review."""
    if message_type not in {"task", "goal"}:
        return OversightDecision(False)
    if status in {"failed", "blocked"}:
        return OversightDecision(True, status)
    if attempt >= stuck_attempts:
        return OversightDecision(True, "stuck_attempts")
    if hop >= stuck_hops:
        return OversightDecision(True, "stuck_hops")
    if review_every_n > 0 and completed_count > 0 and completed_count % review_every_n == 0:
        return OversightDecision(True, "periodic")
    return OversightDecision(False)


def choose_reviewer(
    *,
    subject: str,
    author: str,
    participants: Collection[str],
    assignment_counts: Mapping[str, int],
    rotation_index: int = 0,
) -> str | None:
    """Choose the least-assigned eligible peer with stable rotating tie breaks."""
    people = set(participants)
    candidates = people - {subject}
    if not candidates:
        return None

    if len(people) >= 3:
        without_author = candidates - {author}
        if without_author:
            candidates = without_author

    ordered = sorted(candidates)
    lowest = min(assignment_counts.get(person, 0) for person in ordered)
    tied = [person for person in ordered if assignment_counts.get(person, 0) == lowest]
    start = rotation_index % len(tied)
    return tied[start]
