"""Offline AgentBus member avatars based on a common robot and monogram."""
from __future__ import annotations

import base64
from dataclasses import dataclass
from html import escape
import re


@dataclass(frozen=True)
class MemberIdentity:
    key: str
    name: str
    brand_color: str
    asset_kind: str = "agentbus-neutral-monogram"
    vendor_logo_licensed: bool = False


# AgentBus accent colors distinguish members within the shared avatar design.
_IDENTITIES: dict[str, MemberIdentity] = {
    "codex": MemberIdentity("codex", "Codex", "#67E8F9"),
    "antigravity": MemberIdentity("antigravity", "Antigravity", "#C4B5FD"),
    "gemini": MemberIdentity("gemini", "Gemini", "#FDE68A"),
    "trae": MemberIdentity("trae", "Trae", "#86EFAC"),
    "workbuddy": MemberIdentity("workbuddy", "WorkBuddy", "#F9A8D4"),
    "opencode": MemberIdentity("opencode", "OpenCode", "#93C5FD"),
    "zcode": MemberIdentity("zcode", "ZCode", "#FDBA74"),
    "generic": MemberIdentity("generic", "Agent", "#CBD5E1"),
}


_ALIASES = {
    "codex": "codex",
    "openaicodex": "codex",
    "openai": "codex",
    "googleantigravity": "antigravity",
    "antigravity": "antigravity",
    "googlegemini": "gemini",
    "gemini": "gemini",
    "trae": "trae",
    "traesolo": "trae",
    "traesolocn": "trae",
    "workbuddy": "workbuddy",
    "tencentworkbuddy": "workbuddy",
    "opencode": "opencode",
    "zcode": "zcode",
}


def _key(value: str) -> str:
    return "".join(character for character in value.casefold() if character.isalnum())


def _initials(value: str) -> str:
    words = [word for word in re.split(r"[^\w]+", value.strip(), flags=re.UNICODE) if word]
    if not words:
        return "AI"
    if len(words) > 1:
        return "".join(word[0] for word in words[:2]).upper()
    word = words[0]
    return word[:2].upper()


def resolve_member_identity(member_id: str, display_name: str = "") -> MemberIdentity:
    normalized = _key(str(member_id or ""))
    canonical = _ALIASES.get(normalized)
    if canonical:
        return _IDENTITIES[canonical]
    # Check display name as fallback alias
    if display_name:
        disp_norm = _key(str(display_name))
        for alias, canon in _ALIASES.items():
            if alias in disp_norm:
                return _IDENTITIES[canon]
    label = str(display_name or member_id or "").strip()
    return MemberIdentity("custom", label or "Agent", _IDENTITIES["generic"].brand_color)


def identity_icon_svg(member_id: str, display_name: str = "") -> str:
    """Return a self-contained 64×64 member avatar as SVG."""
    identity = resolve_member_identity(member_id, display_name)
    label = str(display_name or "").strip() or identity.name
    accessible_name = escape(label, quote=True)
    monogram = escape(_initials(label), quote=True)
    accent = identity.brand_color

    return (
        '<svg xmlns="http://www.w3.org/2000/svg" width="64" height="64" '
        'viewBox="0 0 64 64" role="img" aria-labelledby="title">'
        f'<title id="title">{accessible_name} member identifier</title>'
        '<rect x="2" y="2" width="60" height="60" rx="16" '
        'fill="#181e26" stroke="#ffffff" stroke-opacity=".12" stroke-width="1.5"/>'
        '<g transform="translate(14, 10) scale(1.5)">'
        '<rect x="4" y="5" width="16" height="13" rx="3" fill="none" stroke="#94A3B8" stroke-width="1.8"/>'
        f'<circle cx="8.5" cy="11.5" r="1.5" fill="{accent}"/>'
        f'<circle cx="15.5" cy="11.5" r="1.5" fill="{accent}"/>'
        '<path d="M12 2v3M10 2h4" stroke="#94A3B8" stroke-width="1.5" stroke-linecap="round"/>'
        '<path d="M1 11h3M20 11h3" stroke="#94A3B8" stroke-width="1.5" stroke-linecap="round"/>'
        '</g>'
        f'<text x="32" y="53" text-anchor="middle" fill="{accent}" '
        'font-family="Segoe UI, -apple-system, sans-serif" font-size="11" font-weight="700" letter-spacing=".5">'
        f'{monogram}</text>'
        '</svg>'
    )


def identity_icon_data_uri(member_id: str, display_name: str = "") -> str:
    """Return an offline SVG data URI suitable for an ``img.src`` value."""
    encoded = base64.b64encode(identity_icon_svg(member_id, display_name).encode("utf-8"))
    return "data:image/svg+xml;base64," + encoded.decode("ascii")


def known_member_ids() -> tuple[str, ...]:
    return tuple(key for key in _IDENTITIES if key != "generic")
