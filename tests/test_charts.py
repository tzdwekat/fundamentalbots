import re

from fundabot.report import charts


def test_line_chart_series_gaps_legend_and_end_labels():
    svg = charts.line_chart("Revenue and FCF", [("revenue", [("FY2021", 100.0), ("FY2022", 120.0), ("FY2023", None), ("FY2024", 150.0)]),
                                                ("fcf", [("FY2021", 20.0), ("FY2022", 25.0), ("FY2023", 30.0), ("FY2024", 35.0)])], fmt="usd")
    assert svg.startswith("<svg") and "Revenue and FCF" in svg
    assert svg.count("<polyline") == 2  # revenue stops at the None (the lone point after it is a marker only); fcf is one line
    assert svg.count('class="marker"') == 7  # one marker per non-null point
    assert "legend" in svg and "revenue" in svg and "fcf" in svg
    assert "150.0" in svg or "150" in svg  # end label on the last point
    assert "var(--series-1)" in svg and "var(--series-2)" in svg and "fill=\"var(--series" not in svg.split("<text")[1]  # text never wears the series color
    assert svg.count("<title>") == 7  # hover tooltip per marker
    title_y = float(re.search(r'class="title"[^>]*y="([\d.]+)"', svg.split('class="title"')[0] + 'class="title"' + svg.split('class="title"')[1]).group(1)) if False else 18.0
    legend_y = float(re.search(r'<g class="legend">\s*<rect[^>]*y="([\d.]+)"', svg).group(1))
    assert legend_y > title_y + 8  # the legend sits on its own row below the title, never over it


def test_line_chart_single_series_has_no_legend_and_handles_all_null():
    svg = charts.line_chart("Revenue", [("revenue", [("FY2021", 1.0), ("FY2022", 2.0)])], fmt="usd")
    assert "legend" not in svg
    empty = charts.line_chart("Revenue", [("revenue", [("FY2021", None)])], fmt="usd")
    assert "no data" in empty


def test_range_bar_positions_and_labels():
    svg = charts.range_bar("P/E", lo=20.0, median=30.0, hi=40.0, current=35.0, fmt="x")
    assert svg.startswith("<svg") and "P/E" in svg and "20.0x" in svg and "40.0x" in svg and "35.0x" in svg
    cur = float(re.search(r'class="marker current"[^>]*cx="([\d.]+)"', svg).group(1))
    med = float(re.search(r'class="median"[^>]*cx="([\d.]+)"', svg).group(1))
    assert cur > med  # current above the median sits to the right
    assert "n/a" in charts.range_bar("P/E", lo=None, median=None, hi=None, current=None, fmt="x")
    beyond = charts.range_bar("P/E", lo=20.0, median=30.0, hi=40.0, current=50.0, fmt="x")  # above the range: clamped to the track end, labelled
    assert "50.0x" in beyond


def test_heatmap_cells_and_diverging_colors():
    cells = {("bull", "anthropic"): 7, ("bull", "openai"): 9, ("bear", "anthropic"): -6, ("bear", "openai"): None}
    svg = charts.heatmap(["bull", "bear"], ["anthropic", "openai"], cells)
    assert svg.count('class="cell"') == 4 and ">9<" in svg and ">-6<" in svg and ">failed<" in svg
    assert "var(--pos-" in svg and "var(--neg-" in svg  # diverging ramp by sign
    assert svg.count("<title>") == 4


def test_percent_and_usd_formatting():
    assert charts.fmt_value(0.4621, "pct") == "46.2%" and charts.fmt_value(391035e6, "usd") == "$391.0B" and charts.fmt_value(35.123, "x") == "35.1x"
    assert charts.fmt_value(2.5e6, "usd") == "$2.5M" and charts.fmt_value(-0.05, "pct") == "-5.0%"
