"""Render site/legal/*.html into site/public/ with the operator details from legal.json.

Refuses to run while operator or contact is empty, so incomplete legal pages never go live.
Usage: python3 -I build-legal.py   (then: npx wrangler deploy)
"""

import json
import pathlib
import sys

HERE = pathlib.Path(__file__).resolve().parent
# Optional for previews: build-legal.py <values.json> <out-dir>
values = json.loads(pathlib.Path(sys.argv[1] if len(sys.argv) > 1 else HERE / "legal.json").read_text())
OUT = pathlib.Path(sys.argv[2]) if len(sys.argv) > 2 else HERE / "public"
missing = [k for k in ("operator", "contact") if not values.get(k, "").strip()]
if missing:
    sys.exit(f"legal.json is missing: {', '.join(missing)}. Fill them in before publishing.")

SHELL = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<title>{title} · decomp.wtf</title>
<link rel="canonical" href="https://decomp.wtf/{slug}">
<link rel="icon" href="/favicon.svg" type="image/svg+xml">
<link rel="preload" href="/fonts/Geist-Variable.woff2" as="font" type="font/woff2" crossorigin>
<link rel="stylesheet" href="/site.css">
</head>
<body>
<header class="nav scrolled">
  <div class="wrap">
    <a class="brand" href="/" aria-label="decomp.wtf home">
      <svg viewBox="0 0 16 16" aria-hidden="true"><rect x="1" y="1" width="6" height="6"/><rect x="9" y="1" width="6" height="6"/><rect x="1" y="9" width="6" height="6"/><rect class="hollow" x="9.75" y="9.75" width="4.5" height="4.5"/></svg>
      <span>decomp<b>.wtf</b></span>
    </a>
    <nav class="links" aria-label="Legal"><a href="/legal">Legal</a><a href="/privacy">Privacy</a><a class="cta" href="/#get">Install</a></nav>
  </div>
</header>
<main class="doc">
  <article class="wrap">
{body}
    <p class="updated">Last updated {updated}.</p>
  </article>
</main>
<footer class="foot">
  <div class="wrap">
    <a class="brand" href="/" aria-label="decomp.wtf home"><span>decomp<b>.wtf</b></span></a>
    <nav aria-label="Site"><a href="/legal">Legal</a><a href="/privacy">Privacy</a><a href="mailto:{contact}">Contact</a></nav>
  </div>
</footer>
</body>
</html>
"""

for src in sorted((HERE / "legal").glob("*.html")):
    body = src.read_text()
    title = body.split("<h1>", 1)[1].split("</h1>", 1)[0]
    for key, val in values.items():
        body = body.replace("{{" + key + "}}", val)
    if "{{" in body:
        sys.exit(f"{src.name}: unfilled placeholder left")
    out = SHELL.format(title=title, slug=src.stem, body=body, updated=values["updated"], contact=values["contact"])
    (OUT / src.name).write_text(out)
    print(f"wrote {OUT / src.name}")
