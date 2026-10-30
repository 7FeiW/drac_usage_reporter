"""Render the markdown the commands produce as aligned, wrapped, coloured terminal output."""
import os
import re
import shutil
import textwrap

SEP_ROW = re.compile(r"^\|(\s*:?-+:?\s*\|)+$")
SEVERITY = {"high": "31", "medium": "33", "low": "36"}  # red, yellow, cyan
MIN_COL = 8


class Style:
    def __init__(self, color):
        self.color = color

    def __call__(self, text, code):
        return f"\033[{code}m{text}\033[0m" if self.color and code and text.strip() else text


def use_color(mode, stream):
    if mode == "always":
        return True
    if mode == "never" or os.environ.get("NO_COLOR"):
        return False
    return stream.isatty()


def _plain(cell):
    """Cell text without markdown emphasis; second value says whether it was bold."""
    bold = cell.startswith("**") and cell.endswith("**") and len(cell) > 4
    return (cell[2:-2] if bold else cell).replace("`", ""), bold


def _min_width(text, keep):
    """Narrowest width for a cell: whole text if it is at most `keep` long, else its longest word."""
    return len(text) if len(text) <= keep else max(map(len, text.split()), default=0)


def _fit(natural, floor, avail):
    """Shrink the widest columns until the table fits in `avail` characters.

    Columns first give way down to `floor` (their longest word) so words are not split; only if
    that is not enough do they go down to MIN_COL.
    """
    widths = list(natural)
    for limit in (floor, [MIN_COL] * len(widths)):
        while sum(widths) > avail:
            shrinkable = [k for k in range(len(widths)) if widths[k] > limit[k]]
            if not shrinkable:
                break
            widths[max(shrinkable, key=lambda k: widths[k])] -= 1
    return widths


def table(lines, width, st):
    rows = [[c.strip() for c in ln.strip().strip("|").split("|")] for ln in lines]
    head, align, body = rows[0], rows[1], rows[2:]
    right = [a.endswith(":") for a in align]
    if not body:
        return [st("(none)", "2")]
    cells = [[_plain(c) for c in r] for r in [head] + body]
    if {"Detail", "Suggestion"} <= set(head):
        return cards(head, cells, width, st)
    natural = [max(len(r[i][0]) for r in cells if i < len(r)) for i in range(len(head))]
    floor = [max([_min_width(head[i], 8)] + [_min_width(r[i][0], 16) for r in cells[1:] if i < len(r)])
             for i in range(len(head))]
    widths = _fit(natural, floor, width - 3 * len(head) - 1)
    out = []

    def line(left, mid, right_, fill="─"):
        return st(left + mid.join(fill * (w + 2) for w in widths) + right_, "2")

    out.append(line("┌", "┬", "┐"))
    wraps = any(len(c[0]) > w for r in cells[1:] for c, w in zip(r, widths))
    for n, r in enumerate(cells):
        wrapped = [textwrap.wrap(text, w, break_long_words=True) or [""] for (text, _), w in zip(r, widths)]
        if wraps and n > 1:
            out.append(line("├", "┼", "┤", fill="┄"))
        for k in range(max(len(x) for x in wrapped)):
            parts = []
            for i, (w, chunk) in enumerate(zip(widths, wrapped)):
                text = chunk[k] if k < len(chunk) else ""
                padded = text.rjust(w) if right[i] and n else text.ljust(w)
                bold = n == 0 or r[i][1]
                code = SEVERITY.get(r[i][0]) if n and k == 0 else None
                parts.append(st(padded, ";".join(x for x in ("1" if bold else "", code or "") if x)))
            out.append(st("│", "2") + st("│", "2").join(f" {p} " for p in parts) + st("│", "2"))
        if n == 0:
            out.append(line("├", "┼", "┤"))
    out.append(line("└", "┴", "┘"))
    return out


def cards(head, cells, width, st):
    """Flag-style tables: one summary line per row, then its Detail and Suggestion wrapped below."""
    body_cols = ("Severity", "Detail", "Suggestion")
    out = []
    for r in cells[1:]:
        row = dict(zip(head, (t for t, _ in r)))
        sev = row.get("Severity", "")
        code = SEVERITY.get(sev)
        tag = st(f"● {sev.upper():<6}", f"1;{code}" if code else "1")
        parts = [f"{v} jobs" if h == "Jobs" else v for h, v in row.items() if h not in body_cols and v]
        out.append(f"{tag} {st(parts[0], '1')}" + "".join(st(" · ", "2") + x for x in parts[1:]))
        for line in textwrap.wrap(row["Detail"], width - 4):
            out.append("    " + line)
        for k, line in enumerate(textwrap.wrap(row["Suggestion"], width - 6)):
            out.append(("    " + st("→ ", "32") if k == 0 else "      ") + line)
        out.append("")
    return out


def inline(text, st):
    text = re.sub(r"\*\*(.+?)\*\*", lambda m: st(m.group(1), "1"), text)
    return re.sub(r"`([^`]+)`", lambda m: st(m.group(1), "36"), text)


def pretty(text, width=None, color=False):
    """Markdown (headings, tables, **bold**, `code`) -> terminal text."""
    width = width or shutil.get_terminal_size((140, 40)).columns
    st = Style(color)
    lines, out, i = text.splitlines(), [], 0
    while i < len(lines):
        ln = lines[i]
        if ln.startswith("|") and i + 1 < len(lines) and SEP_ROW.match(lines[i + 1].strip()):
            j = i
            while j < len(lines) and lines[j].startswith("|"):
                j += 1
            out += table(lines[i:j], width, st)
            i = j
            continue
        m = re.match(r"^(#{1,4}) (.*)", ln)
        if m:
            level, title = len(m.group(1)), m.group(2).replace("`", "")
            out.append(st(title.upper() if level == 1 else title, "1;4" if level <= 2 else "1"))
            if level == 1:
                out.append(st("═" * min(len(title), width), "1"))
        else:
            out.append(inline(ln, st))
        i += 1
    return "\n".join(out)
