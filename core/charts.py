"""Small server-drawn SVG charts (no JavaScript library). Colors come from CSS classes so light, dark, and PDF
each pick their own validated values; hover tooltips read the data-tip attributes (see core/static/core/charts.js).
"""
from dataclasses import dataclass
from decimal import Decimal
from html import escape

W, H = 720, 240
# Light-theme colors as SVG attributes: the PDF renderer ignores CSS classes inside inline SVG. On screen the
# classes in app.css win over these attributes, so dark mode still gets its own validated colors.
FILL = {"s1": "#14805a", "s2": "#e8823a"}
GRID, AXIS = "#eee8db", "#6f655a"
FONT = 'font-family="Inter, sans-serif" font-size="14"'
PAD_L, PAD_R, PAD_T, PAD_B = 56, 12, 12, 30


@dataclass
class Series:
    name: str
    css: str  # s1, s2
    values: list[Decimal]


def _money(v) -> str:
    return f"{v:,.2f}"


def _short(v: float, step: float = 1_000.0) -> str:
    """Compact tick label with just enough decimals that neighboring ticks never read the same."""
    a = abs(v)
    if a >= 1_000_000:
        places = 0 if step >= 1_000_000 else 1 if step >= 100_000 else 2
        s = f"{v / 1_000_000:.{places}f}M"
    elif a >= 1_000:
        places = 0 if step >= 1_000 else 1 if step >= 100 else 2
        s = f"{v / 1_000:.{places}f}k"
    else:
        s = f"{v:,.0f}"
    return s


def _nice_step(span: float) -> float:
    raw = span / 4 or 1
    mag = 10 ** (len(str(int(raw))) - 1)
    for m in (1, 2, 2.5, 5, 10):
        if m * mag >= raw:
            return m * mag
    return 10 * mag


def _scale(lo: float, hi: float, zero: bool = True):
    """Nice ticks for the value range. Bars always include zero; a line may zoom to its data."""
    if zero:
        lo, hi = min(lo, 0.0), max(hi, 0.0)
    else:
        span = max(hi - lo, abs(hi) * 0.05, 1.0)
        lo, hi = lo - span * 0.15, hi + span * 0.15
        if lo < 0 < hi or (lo >= 0 and lo < span):  # near or across zero: show it
            lo = min(lo, 0.0)
    if lo == hi:
        hi = lo + 1
    step = _nice_step(hi - lo)
    lo = (lo // step) * step
    hi = -((-hi) // step) * step
    ticks = []
    t = lo
    while t <= hi + step / 2:
        ticks.append(t)
        t += step
    plot_h = H - PAD_T - PAD_B

    def y(v: float) -> float:
        return PAD_T + plot_h * (hi - v) / (hi - lo)

    return y, ticks


def _axis(y, ticks, labels, x_of, every=1) -> list[str]:
    out = []
    step = ticks[1] - ticks[0] if len(ticks) > 1 else 1_000.0
    for t in ticks:
        yy = y(t)
        cls = "zero" if t == 0 else "grid"
        color = AXIS if t == 0 else GRID
        out.append(f'<line class="{cls}" x1="{PAD_L}" x2="{W - PAD_R}" y1="{yy:.1f}" y2="{yy:.1f}" '
                   f'stroke="{color}" stroke-width="1"/>')
        out.append(f'<text class="tick" x="{PAD_L - 8}" y="{yy + 5:.1f}" text-anchor="end" fill="{AXIS}" {FONT}>'
                   f'{_short(t, step)}</text>')
    for i, label in enumerate(labels):
        if i % every == 0:
            x = x_of(i)
            # Keep the first and last labels inside the chart.
            anchor = "start" if x - PAD_L < 20 else "end" if W - PAD_R - x < 20 else "middle"
            out.append(f'<text class="tick" x="{x:.1f}" y="{H - 8}" text-anchor="{anchor}" fill="{AXIS}" {FONT}>'
                       f'{escape(label)}</text>')
    return out


def _bar_path(x, w, y0, y1, r=4.0) -> str:
    """Bar with rounded corners on the end away from the baseline."""
    top, bottom = min(y0, y1), max(y0, y1)
    h = bottom - top
    r = min(r, w / 2, h)
    if h <= 0:
        return ""
    if y1 <= y0:  # positive: round the top
        return (f"M{x:.1f},{bottom:.1f}V{top + r:.1f}Q{x:.1f},{top:.1f} {x + r:.1f},{top:.1f}"
                f"H{x + w - r:.1f}Q{x + w:.1f},{top:.1f} {x + w:.1f},{top + r:.1f}V{bottom:.1f}Z")
    return (f"M{x:.1f},{top:.1f}V{bottom - r:.1f}Q{x:.1f},{bottom:.1f} {x + r:.1f},{bottom:.1f}"
            f"H{x + w - r:.1f}Q{x + w:.1f},{bottom:.1f} {x + w:.1f},{bottom - r:.1f}V{top:.1f}Z")


def grouped_bars(labels: list[str], series: list[Series], tips: list[str], title: str) -> str:
    values = [float(v) for s in series for v in s.values]
    y, ticks = _scale(min(values or [0]), max(values or [0]))
    n = len(labels)
    plot_w = W - PAD_L - PAD_R
    slot = plot_w / max(n, 1)
    gap = 2
    bar_w = min(18.0, (slot * 0.7 - gap * (len(series) - 1)) / len(series))
    group_w = bar_w * len(series) + gap * (len(series) - 1)

    def x_of(i):
        return PAD_L + slot * i + slot / 2

    parts = _axis(y, ticks, labels, x_of, every=1 if n <= 12 else 2)
    for i in range(n):
        x0 = x_of(i) - group_w / 2
        for k, s in enumerate(series):
            path = _bar_path(x0 + k * (bar_w + gap), bar_w, y(0), y(float(s.values[i])))
            if path:
                parts.append(f'<path class="bar {s.css}" d="{path}" fill="{FILL[s.css]}"/>')
        parts.append(f'<rect class="hit" fill="none" pointer-events="all" x="{PAD_L + slot * i:.1f}" y="{PAD_T}" '
                     f'width="{slot:.1f}" height="{H - PAD_T - PAD_B}" data-tip="{escape(tips[i])}"><title>{escape(tips[i])}</title></rect>')
    return _svg(parts, title)


def line(labels: list[str], values: list[Decimal], tips: list[str], title: str, every: int = 1) -> str:
    vals = [float(v) for v in values]
    y, ticks = _scale(min(vals or [0]), max(vals or [0]), zero=False)
    n = len(labels)
    plot_w = W - PAD_L - PAD_R
    step = plot_w / max(n - 1, 1)

    def x_of(i):
        return PAD_L + step * i if n > 1 else PAD_L + plot_w / 2

    parts = _axis(y, ticks, labels, x_of, every=every)
    pts = " ".join(f"{x_of(i):.1f},{y(v):.1f}" for i, v in enumerate(vals))
    if n > 1:
        floor = y(0) if ticks[0] <= 0 else y(ticks[0])
        area = f"{x_of(0):.1f},{floor:.1f} {pts} {x_of(n - 1):.1f},{floor:.1f}"
        parts.append(f'<polygon class="area s1" points="{area}" fill="{FILL["s1"]}" fill-opacity="0.1"/>')
    parts.append(f'<polyline class="line s1" points="{pts}" fill="none" stroke="{FILL["s1"]}" stroke-width="2" '
                 f'stroke-linejoin="round"/>')
    for i, v in enumerate(vals):
        neg = " neg" if v < 0 else ""
        dot_attrs = 'fill="#ffffff" stroke="#b42318" stroke-width="2"' if neg else 'fill="none" stroke="none"'
        parts.append(f'<circle class="dot s1{neg}" cx="{x_of(i):.1f}" cy="{y(v):.1f}" r="4" {dot_attrs}/>')
        half = step / 2 if n > 1 else plot_w / 2
        parts.append(f'<rect class="hit" fill="none" pointer-events="all" x="{x_of(i) - half:.1f}" y="{PAD_T}" width="{half * 2:.1f}" '
                     f'height="{H - PAD_T - PAD_B}" data-tip="{escape(tips[i])}"><title>{escape(tips[i])}</title></rect>')
    return _svg(parts, title)


def _svg(parts: list[str], title: str) -> str:
    return (f'<svg class="chart-svg" viewBox="0 0 {W} {H}" role="img" aria-label="{escape(title)}">' + "".join(parts) + "</svg>")


def money_tip(label: str, pairs: list[tuple[str, Decimal]]) -> str:
    return label + " · " + " · ".join(f"{name} {_money(v)}" for name, v in pairs)
