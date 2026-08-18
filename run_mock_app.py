

"""Entry point that works regardless of caller's cwd (used by .claude/launch.json)."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from mock_app.app import app  # noqa: E402

if __name__ == "__main__":
    port = int(os.environ.get("MOCK_APP_PORT", "5055"))
    app.run(host="127.0.0.1", port=port, debug=False)
