"""Render a self-contained HTML report (no external requests; safe to open offline)."""

from __future__ import annotations

import re
from datetime import datetime, timedelta
from html import escape

from .analyze import KIND_LABEL, Cluster, Totals, short_project
from .parse import Session


def _money(x: float) -> str:
    return f"${x:,.2f}" if x < 100 else f"${x:,.0f}"


def _dur(s: float) -> str:
    if s < 90:
        return f"{s:.0f}s"
    if s < 5400:
        return f"{s / 60:.0f}m"
    return f"{s / 3600:.1f}h"


def _hbars(data: dict[str, float], fmt=_money, limit: int = 12) -> str:
    items = list(data.items())[:limit]
    if not items:
        return "<p class=muted>No data.</p>"
    peak = max(v for _, v in items) or 1
    rows = []
    for label, v in items:
        pct = max(v / peak * 100, 0.5)
        rows.append(
            f'<div class=hrow data-tip="{escape(label)}: {fmt(v)}"><span class=hlabel title="{escape(label)}">{escape(label)}</span>'
            f'<span class=htrack><span class=hbar style="width:{pct:.2f}%"></span></span><span class=hval>{fmt(v)}</span></div>'
        )
    return "<div class=hbars>" + "".join(rows) + "</div>"


def _columns(data: dict[str, float]) -> str:
    if not data:
        return "<p class=muted>No data.</p>"
    last_day = datetime.strptime(max(data), "%Y-%m-%d")
    first_day = max(datetime.strptime(min(data), "%Y-%m-%d"), last_day - timedelta(days=59))
    data = {(first_day + timedelta(days=i)).strftime("%Y-%m-%d"): 0.0 for i in range((last_day - first_day).days + 1)} | {
        k: v for k, v in data.items() if k >= first_day.strftime("%Y-%m-%d")}
    peak = max(data.values()) or 1
    cols = []
    for day, v in data.items():
        h = v / peak * 100 if v else 0
        d = datetime.strptime(day, "%Y-%m-%d").strftime("%a %d %b")
        cols.append(f'<div class=col data-tip="{d}: {_money(v)}"><span class=cbar style="height:{h:.1f}%"></span></div>')
    first = datetime.strptime(next(iter(data)), "%Y-%m-%d").strftime("%d %b")
    last = datetime.strptime(list(data)[-1], "%Y-%m-%d").strftime("%d %b")
    return (
        f'<div class=cols role=img aria-label="Spend per day, peak {_money(peak)}">{"".join(cols)}</div>'
        f'<div class=caxis><span>{first}</span><span>peak {_money(peak)}</span><span>{last}</span></div>'
    )


def _md(text: str) -> str:
    """Escape, then render `inline code`."""
    return re.sub(r"`([^`]+)`", r"<code>\1</code>", escape(text))


def _cluster_card(i: int, c: Cluster, redact: bool) -> str:
    n, ns = len(c.events), len(c.sessions)
    meta = [f"{n}× across {ns} session{'s' if ns != 1 else ''}"]
    if len(c.projects) > 1:
        meta.append(f"{len(c.projects)} projects")
    if c.est_cost >= 0.01:
        meta.append(f"~{_money(c.est_cost)} in recovery turns")
    if c.seconds:
        meta.append(f"{_dur(c.seconds)} wall time")
    examples = ""
    if not redact:
        ex = []
        for e in c.events[:4]:
            when = e.ts.astimezone().strftime("%d %b %H:%M") if e.ts else ""
            ex.append(f"<li><span class=muted>{when} · {escape(short_project(e.project))}</span><pre>{escape(e.snippet or '—')}</pre></li>")
        examples = f"<details><summary>Examples</summary><ul class=ex>{''.join(ex)}</ul></details>"
    remedy = ""
    if c.remedy:
        remedy = (
            f'<label class=remedy><input type=checkbox class=pick data-rule="{escape(c.remedy)}" data-target="{escape(c.target)}"{" checked" if i < 5 and ns > 1 else ""}>'
            f'<span>{_md(c.remedy)}<span class=target>→ <code>{escape(c.target)}</code></span></span></label>'
        )
    if c.advice:
        remedy += f"<p class=advice>{_md(c.advice)}</p>"
    return (
        f'<article class=card><header><span class="badge k-{c.kind}">{escape(KIND_LABEL.get(c.kind, c.kind))}</span>'
        f"<code>{escape(c.signature)}</code></header><p class=meta>{' · '.join(meta)}</p>{remedy}{examples}</article>"
    )


def render(sessions: list[Session], clusters: list[Cluster], t: Totals, redact: bool = False, top: int = 15) -> str:
    err_rate = t.tool_errors / t.tool_calls if t.tool_calls else 0
    friction_n = sum(len(c.events) for c in clusters if c.kind not in ("slow_tool", "compaction"))
    span = ""
    starts = [s.start for s in sessions if s.start]
    if starts:
        span = f"{min(starts).astimezone():%d %b %Y} – {max(starts).astimezone():%d %b %Y}"

    tiles = [
        ("Spend", _money(t.cost), "list-price estimate"),
        ("Active time", f"{t.active_h:.1f}h", "gaps over 5 min excluded"),
        ("Sessions", str(t.sessions), f"{t.human_turns} prompts from you"),
        ("Tool calls", f"{t.tool_calls:,}", f"{err_rate:.1%} failed"),
        ("Cache hit", f"{t.cache_hit:.0%}", "of input tokens"),
        ("Friction events", str(friction_n), "errors, denials, interrupts, corrections"),
    ]
    tiles_html = "".join(f"<div class=tile><div class=tl>{a}</div><div class=tv>{b}</div><div class=ts>{c}</div></div>" for a, b, c in tiles)

    cards = "".join(_cluster_card(i, c, redact) for i, c in enumerate(clusters[:top]))

    tool_rows = "".join(
        f"<tr><td>{escape(k)}</td><td class=n>{calls:,}</td><td class=n>{errs}</td><td class=n>{errs / calls:.1%}</td></tr>"
        for k, (calls, errs) in list(t.by_tool.items())[:20]
        if calls
    )

    sess_rows = []
    for s in sorted(sessions, key=lambda s: s.start or datetime.min.astimezone(), reverse=True):
        fr = sum(1 for f in s.friction if f.kind not in ("slow_tool", "compaction"))
        title = "" if redact else s.title
        sess_rows.append(
            f"<tr><td data-v='{s.start.timestamp() if s.start else 0}'>{s.start.astimezone():%d %b %H:%M}</td>"
            f"<td>{escape(short_project(s.project))}</td><td>{escape(title)}</td>"
            f"<td class=n data-v='{s.cost:.4f}'>{_money(s.cost)}</td><td class=n data-v='{s.active_s:.0f}'>{_dur(s.active_s)}</td>"
            f"<td class=n>{s.human_turns}</td><td class=n>{sum(s.tool_calls.values())}</td><td class=n>{s.tool_errors}</td>"
            f"<td class=n>{fr}</td><td class=n data-v='{s.cache_hit:.4f}'>{s.cache_hit:.0%}</td></tr>"
        )

    return (
        TEMPLATE.replace("{{span}}", escape(span))
        .replace("{{generated}}", datetime.now().strftime("%d %b %Y %H:%M"))
        .replace("{{tiles}}", tiles_html)
        .replace("{{cards}}", cards or "<p class=muted>No friction found. Nice.</p>")
        .replace("{{by_project}}", _hbars(t.by_project))
        .replace("{{by_day}}", _columns(t.by_day))
        .replace("{{tool_rows}}", tool_rows)
        .replace("{{sess_rows}}", "".join(sess_rows))
    )


TEMPLATE = """<!doctype html>
<html lang=en><head><meta charset=utf-8><meta name=viewport content="width=device-width,initial-scale=1">
<title>Claude Code Retro</title>
<style>
:root{color-scheme:light;--bg:#f6f5f2;--surface:#fcfcfb;--line:#e4e2dc;--text:#0b0b0b;--text2:#52514e;--muted:#7a7873;
--series:#2a78d6;--track:#eceae5;--accent:#c4532b;--code:#f0efec;
--k-err:#e34948;--k-human:#4a3aa7;--k-perm:#c98500;--k-time:#52514e}
@media (prefers-color-scheme:dark){:root:not([data-theme=light]){color-scheme:dark;--bg:#121211;--surface:#1a1a19;--line:#2f2f2c;
--text:#fff;--text2:#c3c2b7;--muted:#8d8b84;--series:#3987e5;--track:#2a2a27;--accent:#e2774f;--code:#242422;
--k-err:#e66767;--k-human:#9085e9;--k-perm:#eda100;--k-time:#c3c2b7}}
:root[data-theme=dark]{color-scheme:dark;--bg:#121211;--surface:#1a1a19;--line:#2f2f2c;--text:#fff;--text2:#c3c2b7;--muted:#8d8b84;
--series:#3987e5;--track:#2a2a27;--accent:#e2774f;--code:#242422;--k-err:#e66767;--k-human:#9085e9;--k-perm:#eda100;--k-time:#c3c2b7}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--text);font:15px/1.5 ui-sans-serif,-apple-system,"Segoe UI",sans-serif}
main{max-width:1100px;margin:0 auto;padding:32px 16px 64px}
h1{font-size:28px;margin:0 0 4px;letter-spacing:-.02em}h2{font-size:18px;margin:40px 0 12px}
.muted,.sub{color:var(--muted)}.sub{margin:0 0 24px}
code,pre{font:12.5px/1.45 ui-monospace,SFMono-Regular,Menlo,monospace}
.tiles{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px}
.tile{background:var(--surface);border:1px solid var(--line);border-radius:10px;padding:14px 16px}
.tl{font-size:12px;color:var(--text2);text-transform:uppercase;letter-spacing:.05em}.tv{font-size:26px;font-weight:650;margin:2px 0}.ts{font-size:12px;color:var(--muted)}
.grid2{display:grid;grid-template-columns:1fr 1fr;gap:16px}@media(max-width:760px){.grid2{grid-template-columns:1fr}}
.panel{background:var(--surface);border:1px solid var(--line);border-radius:10px;padding:16px;min-width:0}
.panel h3{margin:0 0 12px;font-size:14px;color:var(--text2);font-weight:600}
.hbars{display:flex;flex-direction:column;gap:6px}
.hrow{display:grid;grid-template-columns:minmax(0,38%) 1fr auto;gap:8px;align-items:center;font-size:13px;padding:2px 0;cursor:default}
.hlabel{overflow:hidden;text-overflow:ellipsis;white-space:nowrap;color:var(--text2)}
.htrack{height:12px;display:block}.hbar{display:block;height:100%;background:var(--series);border-radius:0 4px 4px 0}
.hval{font-variant-numeric:tabular-nums;color:var(--text)}
.cols{display:flex;align-items:flex-end;gap:2px;height:160px;border-bottom:1px solid var(--line)}
.col{flex:1;height:100%;display:flex;align-items:flex-end;cursor:default}.cbar{display:block;width:100%;background:var(--series);border-radius:4px 4px 0 0}
.col:hover .cbar,.hrow:hover .hbar{filter:brightness(1.15)}
.caxis{display:flex;justify-content:space-between;font-size:12px;color:var(--muted);margin-top:4px}
.cards{display:grid;gap:10px}
.card{background:var(--surface);border:1px solid var(--line);border-radius:10px;padding:12px 16px}
.card header{display:flex;gap:10px;align-items:baseline;flex-wrap:wrap}.card code{word-break:break-word}
.badge{font-size:11px;font-weight:600;padding:2px 8px;border-radius:99px;border:1px solid currentColor;white-space:nowrap}
.k-tool_error,.k-retry_loop,.k-api_error{color:var(--k-err)}.k-correction,.k-interrupt{color:var(--k-human)}
.k-denial{color:var(--k-perm)}.k-slow_tool,.k-timeout,.k-compaction{color:var(--k-time)}
.meta{margin:6px 0;font-size:13px;color:var(--text2)}
.remedy{display:flex;gap:8px;align-items:flex-start;font-size:14px;padding:8px 10px;background:var(--code);border-radius:8px;cursor:pointer}
.remedy input{margin-top:4px}.target{display:block;font-size:12px;color:var(--muted);margin-top:2px}.remedy code,.advice code{background:var(--surface);padding:0 4px;border-radius:4px}.advice{margin:6px 0 0;font-size:13px;color:var(--text2);font-style:italic}
details{margin-top:8px}summary{cursor:pointer;font-size:13px;color:var(--text2)}
.ex{list-style:none;padding:0;margin:8px 0 0}.ex li{margin-bottom:8px;font-size:12px}
.ex pre{margin:2px 0 0;padding:8px;background:var(--code);border-radius:6px;white-space:pre-wrap;word-break:break-word;max-height:180px;overflow:auto}
.claudemd{margin-top:16px;background:var(--surface);border:1px solid var(--line);border-radius:10px;padding:12px 16px}
.claudemd textarea{width:100%;min-height:90px;font:12.5px/1.45 ui-monospace,monospace;background:var(--code);color:var(--text);border:1px solid var(--line);border-radius:6px;padding:8px;resize:vertical}
.row{display:flex;justify-content:space-between;align-items:center;gap:8px;margin-bottom:6px;flex-wrap:wrap}
button{font:inherit;font-size:13px;padding:5px 12px;border-radius:6px;border:1px solid var(--line);background:var(--text);color:var(--bg);cursor:pointer}
.tablewrap{overflow-x:auto;background:var(--surface);border:1px solid var(--line);border-radius:10px}
table{border-collapse:collapse;width:100%;font-size:13px}
th,td{padding:7px 10px;border-bottom:1px solid var(--line);text-align:left;white-space:nowrap}
td:nth-child(3){white-space:normal;min-width:160px}
th{color:var(--text2);font-weight:600;cursor:pointer;user-select:none;position:sticky;top:0;background:var(--surface)}
th[aria-sort=ascending]::after{content:" ↑"}th[aria-sort=descending]::after{content:" ↓"}
.n{text-align:right;font-variant-numeric:tabular-nums}
#tip{position:fixed;pointer-events:none;background:var(--text);color:var(--bg);font-size:12px;padding:4px 8px;border-radius:6px;opacity:0;transition:opacity .08s;z-index:9}
</style></head><body><main>
<h1>Claude Code Retro</h1>
<p class=sub>{{span}} · generated {{generated}} · local transcripts only, nothing leaves this machine</p>
<section class=tiles>{{tiles}}</section>

<h2>Where sessions got stuck</h2>
<p class=muted style="margin-top:-6px">Ranked by how often each pattern recurs across sessions. Each rule is placed in the CLAUDE.md of the lowest directory that covers every place it happened. Tick one to add it below.</p>
<section class=cards>{{cards}}</section>
<div class=claudemd><div class=row><strong>CLAUDE.md additions</strong><button id=copy>Copy</button></div><textarea id=md readonly></textarea></div>

<h2>Spend</h2>
<section class=grid2>
<div class=panel><h3>By project</h3>{{by_project}}</div>
<div class=panel><h3>Per day (last 60 days with activity)</h3>{{by_day}}</div>
</section>

<h2>Tools</h2>
<div class=tablewrap><table class=sortable><thead><tr><th>Tool</th><th class=n>Calls</th><th class=n>Errors</th><th class=n>Error rate</th></tr></thead><tbody>{{tool_rows}}</tbody></table></div>

<h2>Sessions</h2>
<div class=tablewrap><table class=sortable><thead><tr><th>Started</th><th>Project</th><th>Title</th><th class=n>Cost</th><th class=n>Active</th><th class=n>Prompts</th><th class=n>Tool calls</th><th class=n>Errors</th><th class=n>Friction</th><th class=n>Cache</th></tr></thead><tbody>{{sess_rows}}</tbody></table></div>
</main><div id=tip role=tooltip></div>
<script>
const md=document.getElementById('md');
function build(){const by={};document.querySelectorAll('.pick:checked').forEach(c=>{(by[c.dataset.target]??=new Set).add(c.dataset.rule)});
md.value=Object.entries(by).sort(([a],[b])=>b.length-a.length).map(([t,rs])=>'# '+t+'\\n## Lessons from past sessions (cc-retro)\\n'+[...rs].map(r=>'- '+r).join('\\n')).join('\\n\\n');}
document.querySelectorAll('.pick').forEach(c=>c.addEventListener('change',build));build();
document.getElementById('copy').onclick=async e=>{try{await navigator.clipboard.writeText(md.value)}catch{md.select();document.execCommand('copy')}e.target.textContent='Copied';setTimeout(()=>e.target.textContent='Copy',1200)};
const tip=document.getElementById('tip');
document.addEventListener('mousemove',e=>{const t=e.target.closest('[data-tip]');if(!t){tip.style.opacity=0;return}
tip.textContent=t.dataset.tip;tip.style.opacity=1;const r=tip.getBoundingClientRect();
tip.style.left=Math.min(e.clientX+12,innerWidth-r.width-8)+'px';tip.style.top=(e.clientY-r.height-10)+'px'});
document.querySelectorAll('table.sortable th').forEach((th,i)=>th.addEventListener('click',()=>{
const tb=th.closest('table').tBodies[0],asc=th.getAttribute('aria-sort')!=='ascending';
th.parentNode.querySelectorAll('th').forEach(h=>h.removeAttribute('aria-sort'));th.setAttribute('aria-sort',asc?'ascending':'descending');
const key=td=>{const v=td.dataset.v??td.textContent.replace(/[$,%]/g,'');return isNaN(parseFloat(v))?v.toLowerCase():parseFloat(v)};
[...tb.rows].sort((a,b)=>{const x=key(a.cells[i]),y=key(b.cells[i]);return (x>y?1:x<y?-1:0)*(asc?1:-1)}).forEach(r=>tb.appendChild(r))}));
</script></body></html>
"""
