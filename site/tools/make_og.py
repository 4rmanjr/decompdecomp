"""Render site/public/og.png (1200x630) in the site's own style from the real demo bytes.

Needs the local preview running on :8790 (tools/preview.py) for styles and fonts, then:
  uv run --with playwright python -I site/tools/make_og.py site /tmp/og.html
"""
import asyncio, json, sys
from pathlib import Path
from playwright.async_api import async_playwright

SITE = Path(sys.argv[1]); OUT_HTML = Path(sys.argv[2])
data = json.loads((SITE / "public/data/demo.json").read_text())
fns = data["functions"]; total = data["game"]["total"]
order = sorted(range(len(fns)), key=lambda i: fns[i]["size"])
state = {i: ("matched" if n < 8 else "claimed" if n < 11 else "open") for n, i in enumerate(order)}
owner = [-1] * total
for i, f in enumerate(fns):
    for k in range(f["size"]): owner[f["offset"] + k] = i
cells = []
for b in range(total):
    i = owner[b]
    if i < 0: cells.append('<span class="pad">··</span>')
    else:
        k = b - fns[i]["offset"]; cls = "" if state[i] == "open" else state[i]
        cells.append(f'<span class="{cls}">{fns[i]["hex"][2*k:2*k+2]}</span>')
html = f"""<!doctype html><html><head><meta charset="utf-8">
<link rel="stylesheet" href="http://localhost:8790/styles.css">
<style>
 body {{ width: 1200px; height: 630px; overflow: hidden; margin: 0; }}
 .og {{ position: relative; height: 630px; padding: 52px 64px 0; overflow: hidden; }}
 .row {{ display: flex; justify-content: space-between; align-items: center; }}
 .og .brand {{ font-size: 24px; gap: 13px; }} .og .brand svg {{ width: 26px; height: 26px; }}
 .og .tag {{ font-size: 14px; padding: 8px 12px; }}
 .og h1 {{ font-size: 88px; margin-top: 44px; max-width: none; }}
 .band {{ position: absolute; left: 64px; right: 64px; top: 372px; height: 300px; border: 1px solid var(--line);
          border-radius: 14px 14px 0 0; background: var(--panel); padding: 38px 22px 0; box-shadow: var(--shadow); }}
 .band .field-grid {{ --cols: 48; font-size: 12.5px; line-height: 25px; }}
 .band .atag {{ position: absolute; left: 0; top: 0; transform: translate(var(--x), var(--y)); transition: none; font-size: 12.5px; height: 23px; }}
</style></head><body><div class="og">
 <div class="row"><span class="brand"><svg viewBox="0 0 16 16"><rect x="1" y="1" width="6" height="6"/><rect x="9" y="1" width="6" height="6"/><rect x="1" y="9" width="6" height="6"/><rect class="hollow" x="9.75" y="9.75" width="4.5" height="4.5"/></svg><span class="word">decomp<span>.online</span></span></span>
</div>
 <h1 class="hero-title">Old games, rebuilt <span class="mark">byte for byte.</span></h1>
 <div class="band"><div class="field-grid">{''.join(cells)}</div></div>
</div></body></html>"""
OUT_HTML.write_text(html)

async def main():
    async with async_playwright() as p:
        b = await p.chromium.launch(executable_path="/usr/bin/chromium")
        pg = await b.new_page(viewport={"width": 1200, "height": 630}, color_scheme="light")
        await pg.goto(OUT_HTML.as_uri(), wait_until="networkidle")
        await pg.evaluate("document.fonts.ready")
        # one agent label on the first claimed function
        i = [i for i in order if state[i] == "claimed" and fns[i]["offset"] < 9 * 48][0]
        await pg.evaluate(f"""() => {{
          const band = document.querySelector('.band'), grid = band.querySelector('.field-grid');
          const cell = grid.children[{fns[i]['offset']}].getBoundingClientRect(), br = band.getBoundingClientRect();
          const t = document.createElement('span'); t.className = 'atag';
          t.innerHTML = '<i></i>agent-03 <b>{fns[i]['name']} · {fns[i]['attempts'][0]}</b>';
          band.appendChild(t);
          t.style.setProperty('--x', (cell.left - br.left) + 'px'); t.style.setProperty('--y', (cell.top - br.top - t.offsetHeight - 5) + 'px');
        }}""")
        await pg.screenshot(path=str(SITE / "public/og.png"))
        await b.close()
asyncio.run(main())
print("og.png written")
