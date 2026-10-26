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
    (
        "card",
        [
            ("lambda_", "λ", "Orchestrator Lambda", "scans companies, fans out"),
            ("db", "≣", "DynamoDB", "companies table · Scan"),
        ],
        False,
    ),
    ("edge", "↓ SendMessage, 1/company", None),
    ("card", [("integ", "⬡", "SQS Queue", "batch_size = 1"), ("integ", "⬡", "SQS DLQ", "failed messages")], False),
    ("edge", "↓ trigger", None),
    ("card", [("lambda_", "λ", "Worker Lambda", "fetches, filters, dedupes postings")], False),
    ("edge", "↓ filter + PutItem", None),
    ("card", [("ext", "◍", "Greenhouse / Lever / Workday / Oracle / Taleo / Built In", "JSON API or HTML")], True),
    ("edge", "↓", None),
    ("card", [("db", "≣", "DynamoDB", "jobs table (deduplicated)")], False),
    ("edge", "↓ Query pending-digest-index", None),
    (
        "card",
        [
            ("lambda_", "λ", "Notifier Lambda", "builds HTML digest"),
            ("integ", "◇", "EventBridge Scheduler", "triggers the Notifier"),
        ],
        False,
    ),
    ("edge", "↓ SendEmail", None),
    ("card", [("ses", "✉", "SES", "grouped-by-company digest")], False),
    ("edge", "↓", None),
    ("inbox",),
    ("gap", 28),
    ("label", "Optional — application tracking (enable_tracking_api)"),
    (
        "card",
        [
            ("integ", "⇄", "API Gateway HTTP API", "AWS_IAM · GET/PATCH /jobs"),
            ("lambda_", "λ", "Tracker Lambda", "status, notes, dates"),
        ],
        True,
    ),
    ("edge", "↑ updates tracking fields on the jobs table", None),
]


def text(x, y, content, fill, size=13, weight=None, anchor=None, family=FONT):
    attrs = f' text-anchor="{anchor}"' if anchor else ""
    attrs += f' font-weight="{weight}"' if weight else ""
    return (
        f'<text x="{x}" y="{y}"{attrs} font-size="{size}" fill="{fill}" font-family="{family}">{escape(content)}</text>'
    )


def card(x, y, w, t, color, glyph, title, sub, dashed):
    dash = ' stroke-dasharray="6 5"' if dashed else ""
    return (
        f'<rect x="{x}" y="{y}" width="{w}" height="{H}" rx="10" fill="{t["card"]}" stroke="{t["border"]}"{dash}/>'
        f'<rect x="{x + 18}" y="{y + 15}" width="40" height="40" rx="8" fill="{COLORS[color]}"/>'
        + text(x + 38, y + 42, glyph, "#fff", 22, anchor="middle")
        + text(x + 74, y + 31, title, t["text"], 16, 700)
        + text(x + 74, y + 52, sub, t["muted"])
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
            out.append(text(W / 2, y + 20, row[1], t["edge"], 12, anchor="middle", family="Menlo, monospace"))
            y += 30
        elif kind == "inbox":
            out.append(
                f'<rect x="{W / 2 - 22}" y="{y}" width="44" height="44" rx="8" fill="{COLORS["ext"]}"/>'
                + text(W / 2, y + 30, "✉", "#fff", 22, anchor="middle")
                + text(W / 2, y + 62, "your inbox", t["muted"], anchor="middle")
            )
            y += 74
        elif kind == "gap":
            y += row[1]
        elif kind == "label":
            out.append(text(M, y + 4, row[1], t["muted"], 13, 700))
            y += 18
    height = y + 70
    legend = [
        ("lambda_", "Compute (Lambda)"),
        ("db", "Database (DynamoDB)"),
        ("integ", "App Integration (SQS / EventBridge / API Gateway)"),
        ("ses", "Business Applications (SES)"),
    ]
    lx = M
    out.append(f'<line x1="{M}" y1="{height - 50}" x2="{W - M}" y2="{height - 50}" stroke="{t["border"]}"/>')
    for key, label in legend:
        out.append(
            f'<circle cx="{lx + 5}" cy="{height - 24}" r="5" fill="{COLORS[key]}"/>'
            + text(lx + 16, height - 20, label, t["muted"], 12)
        )
        lx += 16 + len(label) * 6.6 + 24
    header = (
        text(W / 2, 78, "req-aggregator", t["text"], 28, 700, "middle")
        + text(
            W / 2,
            118,
            "Serverless job-board monitor on AWS — EventBridge fan-out, Lambda scraping,",
            t["muted"],
            14,
            anchor="middle",
        )
        + text(W / 2, 140, "DynamoDB storage, SES digest.", t["muted"], 14, anchor="middle")
    )
    svg = (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{height}" viewBox="0 0 {W} {height}">'
        f'<rect width="100%" height="100%" fill="{t["bg"]}"/>{header}{"".join(out)}</svg>'
    )
    svg_path = DOCS / f"architecture-{name}.svg"
    svg_path.write_text(svg)
    subprocess.run(["rsvg-convert", "-z", "2", "-o", str(DOCS / f"architecture-{name}.png"), str(svg_path)], check=True)
    svg_path.unlink()


for theme in THEMES:
    render(theme)
