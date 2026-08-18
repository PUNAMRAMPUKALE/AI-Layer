"""End-to-end sanity check of agent/browser.py + core/locators.py against the
real mock app, without the LLM in the loop. This is what caught locator and
guardrail bugs before spending real Anthropic API calls on the discovery run."""
from pathlib import Path

from core.guardrails import Guardrails

from agent.browser import BrowserSession

ROOT = Path(__file__).resolve().parent.parent


def _session(page, guardrails) -> BrowserSession:
    return BrowserSession(page, guardrails)


def test_happy_path_open_subaccount(page, mock_app_url, reset_mock_app):
    guardrails = Guardrails(ROOT / "config" / "allowlist.yaml")
    s = _session(page, guardrails)

    s.navigate(mock_app_url + "/", expect_text=None, reasoning="entry")
    assert "System Notice" in page.inner_text("body")

    s.click("button", "Continue", expect_text="Member ID", reasoning="dismiss notice")
    assert "Member ID" in page.inner_text("body")

    s.fill("textbox", "Member ID", "10234", parameter_name="member_id", reasoning="enter id")
    s.click("button", "Search", expect_text="Member Detail", reasoning="search")
    assert "Alicia Ferro" in page.inner_text("body")

    balance_step, balance = s.extract_text("Savings Balance", "savings_balance", reasoning="read balance")
    assert balance == "$4210.55"

    s.click("link", "Open Sub-Account", expect_text="Open Sub-Account", reasoning="go to form")
    s.select_option("combobox", "Account Type", "Youth Savings", parameter_name=None, expect_text=None, reasoning="pick type")
    s.fill("textbox", "Initial Deposit (USD)", "150", parameter_name="initial_deposit", reasoning="enter deposit")
    s.click("button", "Continue", expect_text="Confirm Sub-Account Opening", reasoning="submit form")
    assert "cannot be undone" in page.inner_text("body")

    confirm_step = s.click("button", "Confirm and Open Account", expect_text="Sub-Account Opened", reasoning="confirm")
    assert confirm_step.risk_level == "risky"

    ref_step, ref = s.extract_text("Reference Number", "reference_number", reasoning="capture ref")
    assert ref.startswith("SUB-")


def test_not_found_business_outcome(page, mock_app_url, reset_mock_app):
    guardrails = Guardrails(ROOT / "config" / "allowlist.yaml")
    s = _session(page, guardrails)
    s.navigate(mock_app_url + "/members/99999", expect_text=None, reasoning="direct link to unknown member")
    assert 'No member found matching ID "99999"' in page.inner_text("body")


def test_restricted_business_outcome(page, mock_app_url, reset_mock_app):
    guardrails = Guardrails(ROOT / "config" / "allowlist.yaml")
    s = _session(page, guardrails)
    s.navigate(mock_app_url + "/members/30500/open-subaccount", expect_text=None, reasoning="go to form")
    s.select_option("combobox", "Account Type", "Youth Savings", parameter_name=None, expect_text=None, reasoning="pick type")
    s.fill("textbox", "Initial Deposit (USD)", "100", parameter_name="initial_deposit", reasoning="enter deposit")
    s.click("button", "Continue", expect_text=None, reasoning="submit form")
    assert "compliance hold" in page.inner_text("body")


def test_guardrail_blocks_off_allowlist_navigation(page, mock_app_url, reset_mock_app):
    guardrails = Guardrails(ROOT / "config" / "allowlist.yaml")
    s = _session(page, guardrails)
    try:
        s.navigate("http://example.com/", expect_text=None, reasoning="should be blocked")
        assert False, "expected GuardrailViolation"
    except Exception as e:
        assert "not in the allowlist" in str(e)


def test_guardrail_blocks_reset_endpoint(page, mock_app_url, reset_mock_app):
    guardrails = Guardrails(ROOT / "config" / "allowlist.yaml")
    s = _session(page, guardrails)
    try:
        s.navigate(mock_app_url + "/__reset", expect_text=None, reasoning="should be blocked")
        assert False, "expected GuardrailViolation"
    except Exception as e:
        assert "blocked" in str(e)
