"""Turns the live page into text the model can reason over.

Uses the accessibility tree, not a screenshot+coordinates or raw HTML dump --
per the brief, this is meant to be biased toward an approach that still
works when the surface has no clean DOM. The accessibility tree is exactly
the representation that's available and meaningful on both a messy
server-rendered legacy web app and (per REPORT.md section 4) a native
desktop app, which a screenshot-with-coordinates or a raw-HTML approach
isn't.
"""
from __future__ import annotations

from playwright.sync_api import Page

_INTERESTING_ROLES = {
    "button",
    "link",
    "textbox",
    "combobox",
    "checkbox",
    "radio",
    "searchbox",
    "menuitem",
}


def _walk(node: dict, lines: list[str], depth: int = 0) -> None:
    role = node.get("role")
    name = node.get("name")
    if role in _INTERESTING_ROLES:
        entry = f'{"  " * depth}- {role} "{name}"'
        value = node.get("value")
        if value not in (None, ""):
            entry += f" value={value!r}"
        lines.append(entry)
    for child in node.get("children", []) or []:
        _walk(child, lines, depth + (1 if role in _INTERESTING_ROLES else 0))


def perceive(page: Page, body_text_chars: int = 1500) -> str:
    tree = page.accessibility.snapshot(interesting_only=True) or {}
    lines: list[str] = []
    _walk(tree, lines)
    elements_block = "\n".join(lines) if lines else "(none found)"

    try:
        body_text = page.inner_text("body")[:body_text_chars]
    except Exception:  # noqa: BLE001
        body_text = "(could not read page text)"

    return (
        f"URL: {page.url}\n"
        f"Title: {page.title()}\n\n"
        f"Interactive elements (role \"accessible name\"):\n{elements_block}\n\n"
        f"Visible page text (truncated to {body_text_chars} chars):\n{body_text}"
    )
