"""The data and the three small PNG charts of the HTML success e-mail (action send_success_report).

Everything here is calculated from the validated dataset with the project's own functions (dashboard.window_metrics, window_slice, drawdown_series, indexed_series); no financial method is repeated.
build_report() returns a plain dict (strict JSON) that daily_automation.py puts into the request body. The web app builds the HTML itself from these validated fields and never accepts HTML from here.

The charts are drawn with the standard library only: a 600 x 240 palette image written as a PNG (zlib + struct), with anti-aliased lines and bars. They carry no text; titles, legends and the
scale are written by the web app as HTML text next to the picture. The result is deterministic: the same data gives the same bytes on the same zlib. The module reads nothing and writes nothing.
"""

import base64
import binascii
import json
import math
import re
import struct
import zlib
from datetime import datetime, timezone

import dashboard

WINDOW_DAYS = 30
CHART_IDS = ("indexed", "returns", "drawdown")                  # price indexed to 100, daily returns, drawdown from the highest close
CHART_WIDTH, CHART_HEIGHT = 600, 240
MAX_CHART_BYTES = 6000                                          # one PNG (about 2.5 KB in practice); the whole request has to fit the web app's 20,000-character limit
MAX_REPORT_CHARS = 15000                                        # the report alone, as JSON
MAX_REQUEST_CHARS = 19000                                       # the whole request body, with the token and the artifact link: below the web app's limit of 20,000
REQUEST_EXTRAS_CHARS = 400                                      # what is added when the request is posted: token, artifact link, sheet_update
MAX_LATEST_CHARS = 40
TIME_FORMAT = "%Y-%m-%d %H:%M:%S UTC"

BTC_COLOUR = (247, 147, 26)                                     # the Bitcoin orange
ETH_COLOUR = (98, 126, 234)                                     # the Ethereum blue
WHITE, GRID, AXIS = (255, 255, 255), (226, 229, 234), (150, 156, 168)
MARGIN = 8                                                      # pixels between the plot and the edge of the picture
LINE_HALF_WIDTH = 1.2
LEVEL_FLOORS = (0.12, 0.45, 0.80)                               # anti-aliasing: coverage below the first floor is background, then a light, a medium and the full colour
LEVEL_STRENGTH = (0.30, 0.62, 1.0)
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
CHART_KEYS = {"id", "png_base64", "width", "height", "low", "high", "btc_last", "eth_last"}
ASSET_KEYS = {"return_7d", "return_14d", "return_30d", "volatility_annualised", "worst_day", "negative_days", "drawdown_peak", "drawdown_trough"}
PAIR_KEYS = {"volatility_ratio", "joint_negative_days", "overlap_share"}
META_KEYS = {"rows", "first_date", "data_fetched", "generated_at"}
REPORT_KEYS = {"meta", "assets", "pair", "coverage", "charts"}
DATE_TEXT = re.compile(r"\d{4}-\d{2}-\d{2}")


class EmailReportError(Exception):
    """The report could not be built or is not valid. The message is one line and safe to print."""


# ---------------------------------------------------------------- the PNG writer and the drawing
def _check_series(btc, eth):
    for values in (btc, eth):
        if not isinstance(values, (list, tuple)) or len(values) < 2:
            raise EmailReportError("a chart needs two series of at least two numbers")
        for value in values:
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
                raise EmailReportError("a chart series holds a value that is not a finite number")
    if len(btc) != len(eth):
        raise EmailReportError("the two chart series must have the same length")


def _scale(values, include_zero):
    """A scale with round gridline steps that covers every value with some room: returns (low, high, step)."""
    low, high = min(values), max(values)
    if include_zero:
        low, high = min(low, 0.0), max(high, 0.0)
    span = high - low
    pad = span * 0.08 if span > 0 else max(abs(high) * 0.05, 1.0)
    low, high = low - pad, high + pad
    raw = (high - low) / 4
    magnitude = 10 ** math.floor(math.log10(raw))
    step = next(factor * magnitude for factor in (1, 2, 5, 10) if raw <= factor * magnitude * (1 + 1e-12))
    return math.floor(low / step + 1e-9) * step, math.ceil(high / step - 1e-9) * step, step


def _stroke(cover, width, height, points):
    """Anti-aliased polyline: each pixel gets the coverage of its centre's distance to the nearest segment."""
    half = LINE_HALF_WIDTH
    for (x0, y0), (x1, y1) in zip(points, points[1:]):
        dx, dy = x1 - x0, y1 - y0
        length_squared = dx * dx + dy * dy
        left, right = max(0, math.floor(min(x0, x1) - half - 1)), min(width - 1, math.ceil(max(x0, x1) + half + 1))
        top, bottom = max(0, math.floor(min(y0, y1) - half - 1)), min(height - 1, math.ceil(max(y0, y1) + half + 1))
        for py in range(top, bottom + 1):
            cy = py + 0.5
            for px in range(left, right + 1):
                cx = px + 0.5
                t = ((cx - x0) * dx + (cy - y0) * dy) / length_squared if length_squared else 0.0
                t = 0.0 if t < 0.0 else 1.0 if t > 1.0 else t
                coverage = half + 0.5 - math.hypot(cx - (x0 + t * dx), cy - (y0 + t * dy))
                if coverage > 0.0:
                    index = py * width + px
                    cover[index] = max(cover[index], min(coverage, 1.0))


def _bar(cover, width, height, x0, x1, y0, y1):
    """Anti-aliased rectangle: each pixel gets the share of its area that lies inside."""
    for py in range(max(0, math.floor(y0)), min(height, math.ceil(y1))):
        vertical = min(py + 1, y1) - max(py, y0)
        for px in range(max(0, math.floor(x0)), min(width, math.ceil(x1))):
            coverage = (min(px + 1, x1) - max(px, x0)) * vertical
            if coverage > 0.0:
                index = py * width + px
                cover[index] = max(cover[index], min(coverage, 1.0))


def _level(coverage):
    return sum(1 for floor in LEVEL_FLOORS if coverage >= floor)


def _blend(colour, strength):
    return tuple(round(255 + (channel - 255) * strength) for channel in colour)


def _png(width, height, palette, pixels):
    """A palette PNG with 4 bits per pixel (at most 16 colours), filter type 0, one IDAT chunk."""
    def chunk(tag, body):
        return struct.pack(">I", len(body)) + tag + body + struct.pack(">I", zlib.crc32(tag + body) & 0xFFFFFFFF)
    rows = bytearray()
    for y in range(height):
        rows.append(0)
        line = pixels[y * width:(y + 1) * width]
        rows.extend((line[x] << 4) | (line[x + 1] if x + 1 < width else 0) for x in range(0, width, 2))
    palette_bytes = b"".join(bytes(colour) for colour in palette)
    return (PNG_SIGNATURE + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 4, 3, 0, 0, 0)) + chunk(b"PLTE", palette_bytes)
            + chunk(b"IDAT", zlib.compress(bytes(rows), 9)) + chunk(b"IEND", b""))


def render_chart(kind, btc, eth):
    """One chart as (PNG bytes, low, high); low and high are the ends of the vertical scale. kind: indexed (lines), drawdown (lines) or returns (bars from the zero line)."""
    if kind not in CHART_IDS:
        raise EmailReportError("unknown chart kind")
    _check_series(btc, eth)
    width, height, count = CHART_WIDTH, CHART_HEIGHT, len(btc)
    low, high, step = _scale(list(btc) + list(eth), include_zero=kind == "returns")
    plot_left, plot_right, plot_top, plot_bottom = MARGIN, width - MARGIN, MARGIN, height - MARGIN

    def y_of(value):
        return plot_bottom - (value - low) / (high - low) * (plot_bottom - plot_top)

    covers = {"btc": [0.0] * (width * height), "eth": [0.0] * (width * height)}
    if kind == "returns":
        slot = (plot_right - plot_left) / count
        gap = slot * 0.12
        zero = y_of(0.0)
        for name, values, offset in (("btc", btc, 0), ("eth", eth, 1)):
            for i, value in enumerate(values):
                x0 = plot_left + i * slot + (gap if offset == 0 else slot / 2)
                x1 = plot_left + i * slot + (slot / 2 if offset == 0 else slot - gap)
                top, bottom = (y_of(value), zero) if value >= 0 else (zero, y_of(value))
                _bar(covers[name], width, height, x0, x1, top, bottom)
    else:
        for name, values in (("btc", btc), ("eth", eth)):
            _stroke(covers[name], width, height, [(plot_left + i * (plot_right - plot_left) / (count - 1), y_of(value)) for i, value in enumerate(values)])

    palette = [WHITE, GRID, AXIS] + [_blend(BTC_COLOUR, s) for s in LEVEL_STRENGTH] + [_blend(ETH_COLOUR, s) for s in LEVEL_STRENGTH]
    pixels = bytearray(width * height)
    tick = math.ceil(low / step - 1e-9)
    while tick * step <= high + step * 1e-9:                                           # horizontal gridlines at round values; the zero line is darker
        y = min(height - 1, max(0, round(y_of(tick * step) - 0.5)))
        colour_index = 2 if abs(tick * step) < step * 1e-9 else 1
        pixels[y * width:(y + 1) * width] = bytes([colour_index]) * width
        tick += 1
    for index in range(width * height):
        btc_level, eth_level = _level(covers["btc"][index]), _level(covers["eth"][index])
        if eth_level and eth_level >= btc_level:
            pixels[index] = 5 + eth_level
        elif btc_level:
            pixels[index] = 2 + btc_level
    return _png(width, height, palette, pixels), low, high


def _chart_entry(kind, btc, eth):
    data, low, high = render_chart(kind, btc, eth)
    if len(data) > MAX_CHART_BYTES:
        raise EmailReportError(f"the {kind} chart is {len(data)} bytes, more than the limit of {MAX_CHART_BYTES}")
    return {"id": kind, "png_base64": base64.b64encode(data).decode("ascii"), "width": CHART_WIDTH, "height": CHART_HEIGHT,
            "low": round(low, 4), "high": round(high, 4), "btc_last": round(btc[-1], 2), "eth_last": round(eth[-1], 2)}


# ---------------------------------------------------------------- the figures
def _is_number_text(text):
    return bool(dashboard.NUMBER_TEXT.match(text)) and math.isfinite(float(text))


def _coverage(dataset):
    rows, latest, entries = dataset["rows"], dataset["rows"][-1], []
    for index, name in enumerate(dashboard.EXPECTED_COLUMNS):
        valid = sum(1 for row in rows if (DATE_TEXT.fullmatch(row[index]) if index == 0 else _is_number_text(row[index])))
        entries.append({"name": name, "valid_rows": valid, "latest": latest[index]})
    return entries


def _asset_figures(asset, metrics, shorter_windows):
    drawdown, peak, trough = metrics["max_drawdown"][asset]
    worst_value, worst_date = metrics["worst_day"][asset]
    return {
        "return_7d": shorter_windows[7]["cumulative_return"][asset],
        "return_14d": shorter_windows[14]["cumulative_return"][asset],
        "return_30d": metrics["cumulative_return"][asset],
        "volatility_annualised": metrics["volatility"][asset]["annualised"],
        "worst_day": {"value": worst_value, "date": worst_date},
        "negative_days": metrics["negative_days"][asset],
        "drawdown_peak": peak,
        "drawdown_trough": trough,
    }


def _fetched_time(report_text):
    found = dashboard.FETCHED_LINE.search(report_text)
    if found is None:
        raise EmailReportError("the report has no 'Data fetched' line")
    return found.group(1) + " UTC"


def build_report(outputs, generated_at):
    """The report for the e-mail, from the validated outputs of daily_automation.validate_outputs. generated_at is a timezone-aware datetime."""
    if not isinstance(generated_at, datetime) or generated_at.tzinfo is None:
        raise EmailReportError("the generation time must be a timezone-aware datetime")
    dataset, metrics = outputs["dataset"], outputs["metrics"]
    shorter = {k: dashboard.window_metrics(dataset, k) for k in (7, 14)}
    window = dashboard.window_slice(dataset, WINDOW_DAYS)
    series = {                                                                      # (BTC values, ETH values) for each chart
        "indexed": [dashboard.indexed_series(window["closes"][asset]) for asset in dashboard.ASSETS],
        "returns": [[value * 100 for value in window["returns"][asset]] for asset in dashboard.ASSETS],
        "drawdown": [dashboard.drawdown_series(window["closes"][asset]) for asset in dashboard.ASSETS],
    }
    report = {
        "meta": {"rows": len(dataset["rows"]), "first_date": dataset["dates"][0], "data_fetched": _fetched_time(outputs["report_text"]),
                 "generated_at": generated_at.astimezone(timezone.utc).strftime(TIME_FORMAT)},
        "assets": {"btc": _asset_figures("BTC", metrics, shorter), "eth": _asset_figures("ETH", metrics, shorter)},
        "pair": {"volatility_ratio": metrics["volatility_ratio"], "joint_negative_days": metrics["joint_negative_days"], "overlap_share": metrics["down_day_overlap_share"]},
        "coverage": _coverage(dataset),
        "charts": [_chart_entry(kind, *series[kind]) for kind in CHART_IDS],
    }
    check_report(report)
    return report


# ---------------------------------------------------------------- checking a report before it is sent
def _png_problem(data):
    """Why these bytes are not the expected chart file, or None: signature, every chunk's CRC, a 600 x 240 palette header, a palette, pixel data, and the end chunk last."""
    if len(data) > MAX_CHART_BYTES:
        return f"larger than {MAX_CHART_BYTES} bytes"
    if data[:8] != PNG_SIGNATURE:
        return "not a PNG file (wrong signature)"
    position, tags = 8, []
    while position < len(data):
        if position + 12 > len(data):
            return "truncated"
        length, tag = struct.unpack(">I4s", data[position:position + 8])
        end = position + 12 + length
        if end > len(data):
            return "truncated"
        body = data[position + 8:position + 8 + length]
        if struct.unpack(">I", data[end - 4:end])[0] != zlib.crc32(tag + body) & 0xFFFFFFFF:
            return "a chunk fails its checksum"
        if tag == b"IHDR":
            if length != 13 or struct.unpack(">IIBBBBB", body)[:2] != (CHART_WIDTH, CHART_HEIGHT) or body[9] != 3:
                return "not a 600 x 240 palette image"
        tags.append(tag)
        position = end
    if not tags or tags[0] != b"IHDR" or tags[-1] != b"IEND" or b"PLTE" not in tags or b"IDAT" not in tags:
        return "missing a required part"
    return None


def _finite(value, label, low=None, high=None, integer=False, nullable=False):
    if value is None and nullable:
        return
    ok = isinstance(value, int) and not isinstance(value, bool) if integer else isinstance(value, (int, float)) and not isinstance(value, bool)
    if not ok or not math.isfinite(value) or (low is not None and value < low) or (high is not None and value > high):
        raise EmailReportError(f"{label} is not a valid number")


def _date(value, label, nullable=False):
    if value is None and nullable:
        return
    try:
        if not isinstance(value, str) or DATE_TEXT.fullmatch(value) is None:
            raise ValueError
        datetime.strptime(value, "%Y-%m-%d")
    except ValueError:
        raise EmailReportError(f"{label} is not a valid date")


def _keys(value, expected, label):
    if not isinstance(value, dict) or set(value) != expected:
        raise EmailReportError(f"{label} does not have exactly the expected fields")


def check_report(report):
    """Raises EmailReportError (one line) unless the report is complete, plausible, safe to send and small enough for the web app's request limit."""
    _keys(report, REPORT_KEYS, "the report")
    _keys(report["meta"], META_KEYS, "meta")
    _finite(report["meta"]["rows"], "meta.rows", low=dashboard.WINDOWS[0] + 1, integer=True)
    _date(report["meta"]["first_date"], "meta.first_date")
    for key in ("data_fetched", "generated_at"):
        try:
            datetime.strptime(report["meta"][key], TIME_FORMAT)
        except (TypeError, ValueError):
            raise EmailReportError(f"meta.{key} is not a UTC time in the expected format")
    _keys(report["assets"], {"btc", "eth"}, "assets")
    for asset, figures in report["assets"].items():
        _keys(figures, ASSET_KEYS, f"assets.{asset}")
        for key in ("return_7d", "return_14d", "return_30d"):
            _finite(figures[key], f"assets.{asset}.{key}", low=-1)
        _finite(figures["volatility_annualised"], f"assets.{asset}.volatility_annualised", low=0)
        _keys(figures["worst_day"], {"value", "date"}, f"assets.{asset}.worst_day")
        _finite(figures["worst_day"]["value"], f"assets.{asset}.worst_day.value", low=-1)
        _date(figures["worst_day"]["date"], f"assets.{asset}.worst_day.date")
        _finite(figures["negative_days"], f"assets.{asset}.negative_days", low=0, high=WINDOW_DAYS, integer=True)
        for key in ("drawdown_peak", "drawdown_trough"):
            _date(figures[key], f"assets.{asset}.{key}", nullable=True)
    _keys(report["pair"], PAIR_KEYS, "pair")
    _finite(report["pair"]["volatility_ratio"], "pair.volatility_ratio", low=0, nullable=True)
    _finite(report["pair"]["joint_negative_days"], "pair.joint_negative_days", low=0, high=WINDOW_DAYS, integer=True)
    _finite(report["pair"]["overlap_share"], "pair.overlap_share", low=0, high=1, nullable=True)
    coverage = report["coverage"]
    if not isinstance(coverage, list) or [entry.get("name") if isinstance(entry, dict) else None for entry in coverage] != list(dashboard.EXPECTED_COLUMNS):
        raise EmailReportError("coverage must list the 17 variables in the CSV order")
    for entry in coverage:
        _keys(entry, {"name", "valid_rows", "latest"}, "a coverage entry")
        _finite(entry["valid_rows"], f"coverage {entry['name']} valid_rows", low=0, high=report["meta"]["rows"], integer=True)
        if not isinstance(entry["latest"], str) or len(entry["latest"]) > MAX_LATEST_CHARS or not entry["latest"].isprintable():
            raise EmailReportError(f"coverage {entry['name']} latest value is not short printable text")
    charts = report["charts"]
    if not isinstance(charts, list) or len(charts) != len(CHART_IDS):
        raise EmailReportError(f"the report needs exactly {len(CHART_IDS)} charts")
    for chart, expected in zip(charts, CHART_IDS):
        _keys(chart, CHART_KEYS, "a chart")
        if chart["id"] != expected or chart["width"] != CHART_WIDTH or chart["height"] != CHART_HEIGHT or isinstance(chart["width"], bool) or isinstance(chart["height"], bool):
            raise EmailReportError(f"the {expected} chart is missing, out of order or not {CHART_WIDTH} x {CHART_HEIGHT}")
        for key in ("low", "high", "btc_last", "eth_last"):
            _finite(chart[key], f"chart {expected} {key}")
        if not chart["low"] < chart["high"]:
            raise EmailReportError(f"the {expected} chart scale is empty")
        try:
            data = base64.b64decode(chart["png_base64"], validate=True) if isinstance(chart["png_base64"], str) else b""
        except (binascii.Error, ValueError):
            raise EmailReportError(f"the {expected} chart is not valid base64")
        problem = _png_problem(data)
        if problem:
            raise EmailReportError(f"the {expected} chart is not a valid image: {problem}")
    try:
        size = len(json.dumps(report, allow_nan=False))
    except ValueError:
        raise EmailReportError("the report holds a value that is not strict JSON")
    if size > MAX_REPORT_CHARS:
        raise EmailReportError(f"the report is {size} characters, more than the limit of {MAX_REPORT_CHARS}")
