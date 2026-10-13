"""Move the site and the app's links to a new domain, e.g. when decomp.wtf is ready:

    python3 -I site/tools/set_domain.py decomp.wtf            # links, installers, canonical URLs, routes
    python3 -I site/tools/set_domain.py decomp.wtf --email    # also legal@ contact (set up Email Routing first)
    python3 -I site/tools/set_domain.py decomp.wtf --api      # also the agent network's api. host (route it first)

Then: add the domain to Cloudflare, `cd site && npx wrangler deploy`, rebuild + republish the wheel, and add a
redirect rule from the old domain. For the agent network also route api.<domain> to the tunnel
(`cloudflared tunnel route dns decompnet api.<domain>` and the ingress in ~/.cloudflared/decompnet.yml).
"""

import pathlib
import re
import sys

OLD = "decomp.online"
ROOT = pathlib.Path(__file__).resolve().parents[2]
TEXT = {".html", ".js", ".css", ".sh", ".ps1", ".py", ".jsonc", ".json", ".md", ".txt", ".toml"}
PLACES = ["site/public", "site/legal", "site/build-legal.py", "site/wrangler.jsonc", "site/legal.json",
          "src/decompnet", "README.md", "deploy"]


def main() -> int:
    if len(sys.argv) < 2 or not re.fullmatch(r"[a-z0-9-]+(\.[a-z0-9-]+)+", sys.argv[1]):
        print(__doc__)
        return 2
    new, email = sys.argv[1], "--email" in sys.argv
    changed = 0
    for place in PLACES:
        base = ROOT / place
        files = [base] if base.is_file() else [p for p in base.rglob("*") if p.is_file()]
        for f in files:
            if f.suffix not in TEXT or "/dl/" in str(f) or "__pycache__" in str(f):
                continue
            text = f.read_text(errors="strict")
            # The agent network's API host and the contact address need their own DNS/Email Routing setup on the new
            # domain first, so they stay unless asked (--api / --email).
            out = text if email else text.replace(f"legal@{OLD}", "\0EMAIL\0")
            out = out if "--api" in sys.argv else out.replace(f"api.{OLD}", "\0API\0")
            out = out.replace(OLD, new).replace("\0EMAIL\0", f"legal@{OLD}").replace("\0API\0", f"api.{OLD}")
            if out != text:
                f.write_text(out)
                changed += 1
                print(f"  {f.relative_to(ROOT)}")
    kept = [k for k, on in ((f"legal@{OLD}", not email), (f"api.{OLD}", "--api" not in sys.argv)) if on]
    print(f"{changed} files now point at {new}" + (f" (kept: {', '.join(kept)})" if kept else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
