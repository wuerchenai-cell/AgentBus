from collections.abc import Collection
from dataclasses import dataclass
from datetime import datetime, timezone
import re
import time


# P2-b was published on 2026-09-27. Legacy body routing window extended through project convergence.
LEGACY_BODY_COMPAT_UNTIL = datetime(
    2026, 11, 11, 15, 59, 59, tzinfo=timezone.utc).timestamp()


@dataclass(frozen=True)
class RouteDecision:
    recipient: str | None
    source: str
    error: str = ""


def _participant_lookup(participants: Collection[str]) -> dict[str, str]:
    return {participant.casefold(): participant for participant in participants}


def resolve_result_to_arg(
    value: str,
    *,
    sender: str,
    director: str,
    participants: Collection[str],
) -> str:
    """Resolve a CLI result target to a concrete participant ID."""
    value = value.strip()
    lookup = _participant_lookup(participants)
    if not value:
        raise ValueError("result recipient must not be empty")

    alias = value.casefold()
    if alias == "sender":
        target = sender
    elif alias == "director":
        target = director
    else:
        target = lookup.get(alias)
        if target is None:
            raise ValueError(f"unknown result recipient: {value}")

    concrete = lookup.get(target.casefold()) if isinstance(target, str) else None
    if concrete is None:
        raise ValueError(f"unavailable result recipient: {target}")
    return concrete


def resolve_result_route(
    result_to: str | None,
    body: str,
    participants: Collection[str],
    *,
    now: float | None = None,
    body_compat_until: float | None = None,
) -> RouteDecision:
    """Resolve an explicit result target, with bounded legacy body parsing."""
    lookup = _participant_lookup(participants)
    if result_to is not None:
        explicit = result_to.strip()
        if not explicit:
            return RouteDecision(None, "invalid", "empty result recipient")
        recipient = lookup.get(explicit.casefold())
        if recipient is None:
            return RouteDecision(None, "invalid", f"unknown result recipient: {explicit}")
        return RouteDecision(recipient, "field")

    if body_compat_until is None or now is None or now > body_compat_until:
        return RouteDecision(None, "missing")

    labels = ("结果收件人", "回执投给", "结果回给", "结果发给")
    label_pattern = "|".join(map(re.escape, labels))
    pattern = re.compile(rf"^\s*(?:{label_pattern})\s*[:：]\s*(.*?)\s*$")
    found: list[str] = []
    invalid_names: list[str] = []
    empty = False
    for line in body.splitlines():
        match = pattern.match(line)
        if match is None:
            continue
        name = match.group(1).strip()
        if not name:
            empty = True
            continue
        recipient = lookup.get(name.casefold())
        if recipient is not None:
            found.append(recipient)
            continue
        mentioned_with_position = []
        for participant in participants:
            match = re.search(
                rf"(?<![\w-]){re.escape(participant)}(?![\w-])",
                name, re.IGNORECASE)
            if match:
                mentioned_with_position.append((match.start(), participant))
        mentioned = [p for _, p in sorted(mentioned_with_position)]
        if mentioned:
            found.extend(mentioned)
        else:
            invalid_names.append(name)

    # Some v1 messages used an explicit parenthetical `(to=agent)` instead of
    # one of the compatibility labels above. Accept this machine-shaped form
    # during the same bounded migration window, but never parse arbitrary prose
    # mentions as routing instructions.
    explicit_to = re.compile(r"[（(]\s*to\s*=\s*([A-Za-z0-9_.-]+)\s*[）)]",
                             re.IGNORECASE)
    for match in explicit_to.finditer(body):
        name = match.group(1).strip()
        recipient = lookup.get(name.casefold())
        if recipient is None:
            invalid_names.append(name)
        else:
            found.append(recipient)

    distinct = list(dict.fromkeys(found))
    if invalid_names:
        names = ", ".join(dict.fromkeys(invalid_names))
        return RouteDecision(None, "conflict", f"unknown result recipient: {names}")
    if empty:
        return RouteDecision(None, "conflict", "empty result recipient in body")
    if len(distinct) > 1:
        return RouteDecision(None, "conflict", f"conflicting result recipients: {', '.join(distinct)}")
    if not distinct:
        return RouteDecision(None, "missing")
    return RouteDecision(distinct[0], "body")


def resolve_message_result_to(message, participants: Collection[str]) -> RouteDecision:
    """Resolve a Message using the one shared, time-limited legacy policy."""
    return resolve_result_route(
        message.result_to, message.body, participants,
        now=time.time(), body_compat_until=LEGACY_BODY_COMPAT_UNTIL)

