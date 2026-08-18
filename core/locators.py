"""Locator strategy: how a Step's ElementTarget is built at discovery time and
resolved at replay time.

Both the discovery loop and the replay engine go through this module -- that
symmetry matters. If discovery and replay used different targeting logic, a
capability could "work" during recording and then fail on replay for reasons
that have nothing to do with the app changing. Using the same resolver for
both means replay failures are signal about the app (or a bad recording),
not about a mismatch between two implementations.

Fallback order, most to least robust:
  1. role_name  -- accessibility role + accessible name. This is what a
     screen reader (and a human) relies on; it survives markup/CSS churn
     and works identically on a modern or legacy DOM as long as the app
     remains usable by a human.
  2. label      -- <label for=...> association, for form controls. A second,
     independent signal (different DOM mechanism than accessible name) so a
     role/name change alone doesn't break the step.
  3. css        -- a structural nth-of-type path computed at recording time.
     Explicitly the last resort: legacy apps in this project have no test
     IDs, so this is the only fallback available when 1 and 2 both miss --
     and it's exactly as fragile as that implies. Every artifact records
     *why* each candidate was chosen (robustness_notes) rather than hiding
     the trade-off.
"""
from __future__ import annotations

from playwright.sync_api import FrameLocator, Locator, Page

from core.artifact import ElementTarget, LocatorCandidate, LocatorStrategy

_CSS_PATH_JS = """
(el) => {
  function cssPath(node) {
    if (node.id) return '#' + CSS.escape(node.id);
    const path = [];
    while (node && node.nodeType === 1 && node.tagName.toLowerCase() !== 'html') {
      let selector = node.tagName.toLowerCase();
      const parent = node.parentElement;
      if (parent) {
        const siblings = Array.from(parent.children).filter(s => s.tagName === node.tagName);
        if (siblings.length > 1) {
          selector += `:nth-of-type(${siblings.indexOf(node) + 1})`;
        }
      }
      path.unshift(selector);
      node = parent;
    }
    return path.join(' > ');
  }
  return cssPath(el);
}
"""


class LocatorResolutionError(Exception):
    def __init__(self, target: ElementTarget, attempts: list[str]):
        self.target = target
        self.attempts = attempts
        detail = "; ".join(attempts) if attempts else "no candidates"
        super().__init__(f"Could not resolve target '{target.description}': {detail}")


def _scope(page: Page, frame_path: list[str]) -> Page | FrameLocator:
    scope: Page | FrameLocator = page
    for name in frame_path:
        scope = scope.frame_locator(f'iframe[name="{name}"]')
    return scope


def _locator_for(scope: Page | FrameLocator, cand: LocatorCandidate) -> Locator:
    if cand.strategy == LocatorStrategy.ROLE_NAME:
        return scope.get_by_role(cand.role, name=cand.name or "", exact=cand.exact)
    if cand.strategy == LocatorStrategy.LABEL:
        return scope.get_by_label(cand.name or "", exact=cand.exact)
    if cand.strategy == LocatorStrategy.TEXT:
        return scope.get_by_text(cand.name or "", exact=cand.exact)
    if cand.strategy == LocatorStrategy.TABLE_ROW_VALUE:
        # get_by_text(exact=True) targets the innermost element whose normalized text
        # is exactly the label -- unlike has_text on an ancestor `tr`, this can't get
        # fooled by an outer layout row whose full subtree text happens to contain the
        # label as a substring (which is exactly what broke here during testing).
        label_cell = scope.get_by_text(cand.name or "", exact=True)
        return label_cell.locator("xpath=./ancestor-or-self::td[1]/following-sibling::td[1]")
    if cand.strategy == LocatorStrategy.CSS:
        return scope.locator(cand.css or "")
    raise ValueError(f"Unknown locator strategy: {cand.strategy}")


def resolve(page: Page, target: ElementTarget, timeout_ms: int = 4000) -> Locator:
    """Try each candidate in priority order; return the first that resolves to
    exactly one visible element. Raises LocatorResolutionError with the full
    attempt trail if every candidate fails -- that trail is what makes a
    replay failure debuggable instead of a bare 'element not found'."""
    scope = _scope(page, target.frame_path)
    attempts: list[str] = []
    for cand in target.candidates:
        try:
            loc = _locator_for(scope, cand)
            loc.first.wait_for(state="visible", timeout=timeout_ms)
            count = loc.count()
            if count >= 1:
                if count > 1:
                    attempts.append(f"{cand.strategy}: matched {count} elements, using first")
                return loc.first
            attempts.append(f"{cand.strategy}({cand.role or ''!r},{cand.name or cand.css!r}): 0 matches")
        except Exception as e:  # noqa: BLE001 -- deliberately broad: any candidate can fail differently
            attempts.append(f"{cand.strategy}({cand.role or ''!r},{cand.name or cand.css!r}): {e.__class__.__name__}")
    raise LocatorResolutionError(target, attempts)


def build_target(
    page: Page,
    *,
    description: str,
    role: str,
    name: str,
    frame_path: list[str] | None = None,
    label_text: str | None = None,
) -> ElementTarget:
    """Construct an ElementTarget for a role+name the discovery agent chose,
    including a computed CSS structural fallback. Called at discovery time,
    right after the agent successfully acted on the element, so the CSS path
    reflects the real DOM."""
    frame_path = frame_path or []
    scope = _scope(page, frame_path)
    candidates = [
        LocatorCandidate(
            strategy=LocatorStrategy.ROLE_NAME,
            role=role,
            name=name,
            exact=True,
            robustness_notes="Primary: accessibility role+name, stable as long as the control "
            "remains usable by a human via assistive tech.",
        )
    ]
    if label_text:
        candidates.append(
            LocatorCandidate(
                strategy=LocatorStrategy.LABEL,
                name=label_text,
                exact=True,
                robustness_notes="Secondary: <label for> association, independent of accessible-name computation.",
            )
        )
    try:
        handle = _locator_for(scope, candidates[0]).first.element_handle(timeout=2000)
        css_path = page.evaluate(_CSS_PATH_JS, handle)
        if css_path:
            candidates.append(
                LocatorCandidate(
                    strategy=LocatorStrategy.CSS,
                    css=css_path,
                    robustness_notes="Last resort: structural nth-of-type path. This app has no test IDs, "
                    "so this candidate breaks on any layout change -- kept only as a final fallback.",
                )
            )
    except Exception:  # noqa: BLE001 -- CSS fallback is best-effort, never required
        pass

    return ElementTarget(description=description, candidates=candidates, frame_path=frame_path)


def build_extract_target(
    page: Page,
    *,
    description: str,
    label_text: str,
    frame_path: list[str] | None = None,
) -> ElementTarget:
    """Construct an ElementTarget for a value read out of a label/value table
    row -- the pattern this app (and plenty of real back-office UIs) uses for
    a member's balance, a generated reference number, etc."""
    frame_path = frame_path or []
    scope = _scope(page, frame_path)
    candidates = [
        LocatorCandidate(
            strategy=LocatorStrategy.TABLE_ROW_VALUE,
            name=label_text,
            robustness_notes="Primary: value cell of the table row labeled "
            f"'{label_text}'. Breaks only if the row's label text or its two-cell "
            "layout changes.",
        )
    ]
    try:
        handle = _locator_for(scope, candidates[0]).element_handle(timeout=2000)
        css_path = page.evaluate(_CSS_PATH_JS, handle)
        if css_path:
            candidates.append(
                LocatorCandidate(
                    strategy=LocatorStrategy.CSS,
                    css=css_path,
                    robustness_notes="Last resort: structural nth-of-type path to this specific cell.",
                )
            )
    except Exception:  # noqa: BLE001
        pass
    return ElementTarget(description=description, candidates=candidates, frame_path=frame_path)
