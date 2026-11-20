"""Loss-aware trimming for oversized compaction handoffs."""

from __future__ import annotations

import re

_PROTECTED_HEADINGS = (
    "Active Task", "Goal", "Constraints & Preferences", "Constraints",
    "Key Decisions", "Active State", "Blocked", "Critical Context", "Pruned Skills",
)
_TRUNCATION_MARKER = "\n…[summary truncated so compaction can shrink]\n\n"


def _is_protected_heading(title: str, historical_heading: str) -> bool:
    norm = title.rstrip(":").strip().casefold()
    return any(
        norm == protected.casefold()
        or any(norm.startswith(protected.casefold() + suffix) for suffix in (" ", ":", "("))
        for protected in (historical_heading.lstrip("#").strip(), *_PROTECTED_HEADINGS)
    )


def _protected_sections(content: str, bound: int, historical_heading: str) -> list[str] | None:
    headings = list(re.finditer(r"(?m)^##[ \t]+([^\r\n]+)", content[:bound]))
    if not headings:
        return None
    wrapper = content[:headings[0].start()]
    parts = [wrapper.rstrip()] if wrapper.strip() else []
    protected = dropped = 0
    for index, heading in enumerate(headings):
        end = headings[index + 1].start() if index + 1 < len(headings) else bound
        if _is_protected_heading(heading.group(1).strip(), historical_heading):
            parts.append(content[heading.start():end].strip())
            protected += 1
        else:
            dropped += 1
    return parts if protected and dropped else None


def trim_summary(content: str, max_chars: int | None = None) -> str | None:
    """Drop whole nonessential sections, or refuse if protected context cannot fit."""
    from agent.context_compressor import HISTORICAL_TASK_HEADING, _SALVAGE_SUMMARY_MAX_CHARS, _SUMMARY_END_MARKER
    from agent.context_compressor_tail import SKILL_SAFETY_RULE

    max_chars = _SALVAGE_SUMMARY_MAX_CHARS if max_chars is None else max_chars
    if len(content) <= max_chars:
        return content
    user_heading = re.search(r"(?m)^##[ \t]+User Messages.*$", content)
    bound = user_heading.start() if user_heading else content.rfind(_SUMMARY_END_MARKER)
    if bound < 0:
        bound = len(content)
    parts = _protected_sections(content, bound, HISTORICAL_TASK_HEADING)
    if parts is None:
        return None
    tail = content[bound:] if user_heading else _SUMMARY_END_MARKER if _SUMMARY_END_MARKER in content else ""
    assembled = "\n\n".join(parts).rstrip() + _TRUNCATION_MARKER + tail.lstrip()
    if SKILL_SAFETY_RULE in content and SKILL_SAFETY_RULE not in assembled:
        if assembled.endswith(_SUMMARY_END_MARKER):
            assembled = (assembled[:-len(_SUMMARY_END_MARKER)].rstrip() + "\n\n" + SKILL_SAFETY_RULE
                         + "\n\n" + _SUMMARY_END_MARKER)
        else:
            assembled += "\n\n" + SKILL_SAFETY_RULE
    return assembled if len(assembled) <= max_chars else None
