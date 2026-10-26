"""Renders docs/architecture-{light,dark}.png. Usage: python3 docs/generate_architecture.py (needs rsvg-convert)."""

import subprocess
from pathlib import Path
from xml.sax.saxutils import escape

DOCS = Path(__file__).parent

THEMES = {
    "light": dict(bg="#f5f6f8", card="#ffffff", border="#dcdfe4", text="#1a1d21", muted="#5b6470", edge="#5b6470"),
    "dark": dict(bg="#14171c", card="#1e2229", border="#343a44", text="#eef0f3", muted="#9aa4b2", edge="#9aa4b2"),
}
COLORS = dict(lambda_="#ed7100", db="#a020b8", integ="#e7157b", ses="#dd344c", ext="#5b6470")

W, M, GAP, H = 980, 60, 16, 70
HALF = (W - 2 * M - GAP) / 2
FONT = "Helvetica, Arial, sans-serif"

# (kind, [(color, glyph, title, subtitle)], dashed)
ROWS = [
    ("card", [("integ", "◇", "EventBridge Scheduler", "triggers the Orchestrator")], False),
    ("edge", "↓", None),
    ("card", [("lambda_", "λ", "Orchestrator Lambda", "scans companies, fans out"), ("db", "≣", "DynamoDB", "companies table · Scan")], False),
    ("edge", "↓ SendMessage, 1/company", None),
    ("card", [("integ", "⬡", "SQS Queue", "batch_size = 1"), ("integ", "⬡", "SQS DLQ", "failed messages")], False),
    ("edge", "↓ trigger", None),
    ("card", [("lambda_", "λ", "Worker Lambda", "fetches, filters, dedupes postings")], False),
    ("edge", "↓ filter + PutItem", None),
    ("card", [("ext", "◍", "Greenhouse / Lever / Workday / Oracle / Taleo / Built In", "JSON API or HTML")], True),
    ("edge", "↓", None),
    ("card", [("db", "≣", "DynamoDB", "jobs table (deduplicated)")], False),
    ("edge", "↓ Query pending-digest-index", None),
    ("card", [("lambda_", "λ", "Notifier Lambda", "builds HTML digest"), ("integ", "◇", "EventBridge Scheduler", "triggers the Notifier")], False),
    ("edge", "↓ SendEmail", None),
    ("card", [("ses", "✉", "SES", "grouped-by-company digest")], False),
    ("edge", "↓", None),
    ("inbox",),
    ("gap", 28),
    ("label", "Optional — application tracking (enable_tracking_api)"),
    ("card", [("integ", "⇄", "API Gateway HTTP API", "AWS_IAM · GET/PATCH /jobs"), ("lambda_", "λ", "Tracker Lambda", "status, notes, dates")], True),
    ("edge", "↑ updates tracking fields on the jobs table", None),
]


def card(x, y, w, t, color, glyph, title, sub, dashed):
    dash = ' stroke-dasharray="6 5"' if dashed else ""
    return (
        f'<rect x="{x}" y="{y}" width="{w}" height="{H}" rx="10" fill="{t["card"]}" stroke="{t["border"]}"{dash}/>'
        f'<rect x="{x + 18}" y="{y + 15}" width="40" height="40" rx="8" fill="{COLORS[color]}"/>'
        f'<text x="{x + 38}" y="{y + 42}" text-anchor="middle" font-size="22" fill="#fff" font-family="{FONT}">{escape(glyph)}</text>'
        f'<text x="{x + 74}" y="{y + 31}" font-size="16" font-weight="700" fill="{t["text"]}" font-family="{FONT}">{escape(title)}</text>'
        f'<text x="{x + 74}" y="{y + 52}" font-size="13" fill="{t["muted"]}" font-family="{FONT}">{escape(sub)}</text>'
    )


def render(name):
    t = THEMES[name]
    out, y = [], 190
    for row in ROWS:
        kind = row[0]
        if kind == "card":
            cards = row[1]
            w = HALF if len(cards) == 2 else W - 2 * M
            for i, c in enumerate(cards):
                out.append(card(M + i * (HALF + GAP), y, w, t, *c, row[2]))
            y += H
        elif kind == "edge":
            out.append(f'<text x="{W / 2}" y="{y + 20}" text-anchor="middle" font-size="12" fill="{t["edge"]}" font-family="Menlo, monospace">{escape(row[1])}</text>')
            y += 30
        elif kind == "inbox":
            out.append(f'<rect x="{W / 2 - 22}" y="{y}" width="44" height="44" rx="8" fill="{COLORS["ext"]}"/>'
                       f'<text x="{W / 2}" y="{y + 30}" text-anchor="middle" font-size="22" fill="#fff" font-family="{FONT}">✉</text>'
                       f'<text x="{W / 2}" y="{y + 62}" text-anchor="middle" font-size="13" fill="{t["muted"]}" font-family="{FONT}">your inbox</text>')
            y += 74
        elif kind == "gap":
            y += row[1]
        elif kind == "label":
            out.append(f'<text x="{M}" y="{y + 4}" font-size="13" font-weight="700" fill="{t["muted"]}" font-family="{FONT}">{escape(row[1])}</text>')
            y += 18
    height = y + 70
    legend = [("lambda_", "Compute (Lambda)"), ("db", "Database (DynamoDB)"), ("integ", "App Integration (SQS / EventBridge / API Gateway)"), ("ses", "Business Applications (SES)")]
    lx = M
    out.append(f'<line x1="{M}" y1="{height - 50}" x2="{W - M}" y2="{height - 50}" stroke="{t["border"]}"/>')
    for key, label in legend:
        out.append(f'<circle cx="{lx + 5}" cy="{height - 24}" r="5" fill="{COLORS[key]}"/>'
                   f'<text x="{lx + 16}" y="{height - 20}" font-size="12" fill="{t["muted"]}" font-family="{FONT}">{escape(label)}</text>')
        lx += 16 + len(label) * 6.6 + 24
    header = (
        f'<text x="{W / 2}" y="78" text-anchor="middle" font-size="28" font-weight="700" fill="{t["text"]}" font-family="{FONT}">req-aggregator</text>'
        f'<text x="{W / 2}" y="118" text-anchor="middle" font-size="14" fill="{t["muted"]}" font-family="{FONT}">Serverless job-board monitor on AWS — EventBridge fan-out, Lambda scraping,</text>'
        f'<text x="{W / 2}" y="140" text-anchor="middle" font-size="14" fill="{t["muted"]}" font-family="{FONT}">DynamoDB storage, SES digest.</text>'
    )
    svg = (f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{height}" viewBox="0 0 {W} {height}">'
           f'<rect width="100%" height="100%" fill="{t["bg"]}"/>{header}{"".join(out)}</svg>')
    svg_path = DOCS / f"architecture-{name}.svg"
    svg_path.write_text(svg)
    subprocess.run(["rsvg-convert", "-z", "2", "-o", str(DOCS / f"architecture-{name}.png"), str(svg_path)], check=True)
    svg_path.unlink()


for theme in THEMES:
    render(theme)
