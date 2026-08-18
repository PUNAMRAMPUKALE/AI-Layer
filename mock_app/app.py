"""Mock 'legacy' core-banking back-office app.

Stands in for the real target described in the assignment brief: no API,
server-rendered HTML, nested tables, no test IDs. Deliberately includes a
one-time interstitial (recoverable), a not-found business outcome, a
restricted-member business outcome, a hard session-expiry failure, and
input validation -- so the discovery agent and the replay engine both have
real runtime conditions to handle, not just a happy path.

Special member IDs:
  10234, 10500  -> normal members, happy path
  30500         -> status "restricted" -> business outcome on open-subaccount
  40999         -> GET .../open-subaccount simulates an expired session
  (anything else) -> not found
"""
from __future__ import annotations

import os

from flask import Flask, redirect, render_template, request, session, url_for

from mock_app import data

app = Flask(__name__)
app.secret_key = os.environ.get("MOCK_APP_SECRET", "dev-only-not-for-real-use")


@app.route("/", methods=["GET"])
def index():
    if not session.get("seen_notice"):
        return render_template("system_notice.html", title="System Notice")
    return render_template("search.html", title="Member Search", query=None, not_found=None)


@app.route("/dismiss-notice", methods=["POST"])
def dismiss_notice():
    session["seen_notice"] = True
    return redirect(url_for("index"))


@app.route("/search", methods=["POST"])
def search():
    member_id = (request.form.get("member_id") or "").strip()
    if member_id in data.MEMBERS:
        return redirect(url_for("member_detail", member_id=member_id))
    return render_template("search.html", title="Member Search", query=member_id, not_found=member_id)


@app.route("/members/<member_id>", methods=["GET"])
def member_detail(member_id: str):
    m = data.MEMBERS.get(member_id)
    if m is None:
        return render_template("search.html", title="Member Search", query=None, not_found=member_id)
    return render_template("member_detail.html", title="Member Detail", m=m)


@app.route("/members/<member_id>/open-subaccount", methods=["GET"])
def open_subaccount_form(member_id: str):
    if member_id == "40999":
        return render_template("session_expired.html", title="Session Expired")
    m = data.MEMBERS.get(member_id)
    if m is None:
        return render_template("search.html", title="Member Search", query=None, not_found=member_id)
    return render_template(
        "open_subaccount_form.html", title="Open Sub-Account", m=m, error=None, initial_deposit=None
    )


@app.route("/members/<member_id>/open-subaccount", methods=["POST"])
def open_subaccount_submit(member_id: str):
    m = data.MEMBERS.get(member_id)
    if m is None:
        return render_template("search.html", title="Member Search", query=None, not_found=member_id)

    if m.status == "restricted":
        return render_template("restricted.html", title="Action Not Available", m=m)

    account_type = request.form.get("account_type", "")
    raw_deposit = (request.form.get("initial_deposit") or "").strip()

    error = None
    deposit_value = None
    try:
        deposit_value = float(raw_deposit)
        if deposit_value <= 0:
            error = "Initial deposit must be greater than $0.00."
        elif deposit_value > 10000:
            error = "Initial deposit cannot exceed $10,000.00 for a sub-account opened without a supervisor override."
    except ValueError:
        error = f'"{raw_deposit}" is not a valid dollar amount.'

    if error:
        return render_template(
            "open_subaccount_form.html",
            title="Open Sub-Account",
            m=m,
            error=error,
            initial_deposit=raw_deposit,
        )

    return render_template(
        "confirm_subaccount.html",
        title="Confirm Sub-Account",
        m=m,
        account_type=account_type,
        initial_deposit=deposit_value,
    )


@app.route("/members/<member_id>/open-subaccount/confirm", methods=["POST"])
def open_subaccount_confirm(member_id: str):
    m = data.MEMBERS.get(member_id)
    if m is None:
        return render_template("search.html", title="Member Search", query=None, not_found=member_id)

    account_type = request.form.get("account_type", "")
    initial_deposit = float(request.form.get("initial_deposit", "0"))
    ref = data.next_subaccount_ref()
    m.subaccounts.append({"ref": ref, "account_type": account_type, "initial_deposit": initial_deposit})

    return render_template(
        "subaccount_success.html",
        title="Sub-Account Opened",
        m=m,
        ref=ref,
        account_type=account_type,
        initial_deposit=initial_deposit,
    )


@app.route("/__reset", methods=["POST"])
def admin_reset():
    """Test-harness-only endpoint: resets in-memory state between discovery/replay
    runs so they're reproducible. Not on the agent's allowlist -- the agent/replay
    engine never calls this; only run scripts do, directly via requests."""
    data.reset_state()
    return {"ok": True}


if __name__ == "__main__":
    port = int(os.environ.get("MOCK_APP_PORT", "5055"))
    app.run(host="127.0.0.1", port=port, debug=False)
