import os
import tempfile

# the apps the tests start never look for updates on the network (tests/test_updates.py checks that part offline)
os.environ.setdefault("DECOMP_UPDATES", "off")
# nothing a test starts may reach the user's own app or projects: GET /api/state lists agents' models by running the
# agent CLIs installed on this computer, and Antigravity's `decomp mcp` (the user's settings) started the user's real
# app, with this test environment (updates off), while it was restarting into an update
os.environ["DECOMP_HOME"] = tempfile.mkdtemp(prefix="decomp-tests-")
