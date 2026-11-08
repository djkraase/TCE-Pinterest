#!/usr/bin/env python3
"""Render deterministic, review-first commercial Pin artwork from approved copy specs.

This tool deliberately does not edit the Pinterest publication manifest or call Buffer.
Publishing requires the existing individual editorial, visual and URL gates.
"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont


ROOT = Path(__file__).resolve().parents[1]
SPEC_PATH = ROOT / "campaign" / "creative_specs.json"
WIDTH, HEIGHT = 1000, 1500
INK = "#0c2d1d"
CREAM = "#f5f0e6"
WHITE = "#fffcf5"
MUTED = "#52675b"

FONT_DIRS = (
    Path("/usr/share/fonts/truetype/dejavu"),
    Path("/usr/local/share/fonts"),
)


class SpecError(ValueError):
    pass


def font(filename: str, size: int) -> ImageFont.FreeTypeFont:
    for folder in FONT_DIRS:
        candidate = folder / filename
        if candidate.is_file():
            return ImageFont.truetype(str(candidate), size)
    # Pillow ships a compatible font reference on standard Ubuntu runners.
    try:
        return ImageFont.truetype(filename, size)
    except OSError as exc:
        raise SpecError(f"Missing required typography font: {filename}") from exc


def read_specs(path: Path) -> list[dict]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if data.get("schema_version") != 1 or not isinstance(data.get("items"), list):
        raise SpecError("Expected creative spec schema_version=1 with items array")
    items = data["items"]
    if not items:
        raise SpecError("The creative spec has no items")
    seen_ids, seen_assets = set(), set()
    for item in items:
        if not isinstance(item, dict):
            raise SpecError("Each creative item must be an object")
        identifier = item.get("id", "")
        asset = item.get("asset", "")
        if not isinstance(identifier, str) or not re.fullmatch(r"pin-[a-z0-9-]{1,70}", identifier):
            raise SpecError(f"Invalid Pin ID: {identifier!r}")
        if not isinstance(asset, str) or not re.fullmatch(r"tce_pin_[0-9]{2}_publish\.png", asset):
            raise SpecError(f"Invalid flat publication asset name: {asset!r}")
        if identifier in seen_ids or asset in seen_assets:
            raise SpecError(f"Duplicate Pin ID or asset: {identifier}")
        seen_ids.add(identifier)
        seen_assets.add(asset)
        for field, max_chars in (("eyebrow", 42), ("headline", 100), ("support", 145), ("button", 38)):
            value = item.get(field)
            if not isinstance(value, str) or not value.strip() or len(value) > max_chars:
                raise SpecError(f"{identifier}: {field} must have 1–{max_chars} characters")
        if item.get("editorial_approved") is not False:
            raise SpecError(f"{identifier}: creative specs must enter as unapproved drafts")
    return items


def line_width(draw: ImageDraw.ImageDraw, words: str, typeface: ImageFont.FreeTypeFont) -> float:
    return draw.textlength(words, font=typeface)


def wrap(draw: ImageDraw.ImageDraw, text: str, typeface: ImageFont.FreeTypeFont, max_width: int) -> list[str]:
    rows: list[str] = []
    row = ""
    for word in text.split():
        candidate = f"{row} {word}" if row else word
        if line_width(draw, candidate, typeface) > max_width and row:
            rows.append(row)
            row = word
        else:
            row = candidate
    if row:
        rows.append(row)
    return rows


def fit_lines(draw: ImageDraw.ImageDraw, text: str, font_name: str, max_width: int,
              max_lines: int, upper: int, lower: int) -> tuple[ImageFont.FreeTypeFont, list[str]]:
    for size in range(upper, lower - 1, -2):
        typeface = font(font_name, size)
        lines = wrap(draw, text, typeface, max_width)
        if len(lines) <= max_lines and all(line_width(draw, line, typeface) <= max_width for line in lines):
            return typeface, lines
    raise SpecError(f"Text does not fit its design area: {text!r}")


def tracked_preview(draw: ImageDraw.ImageDraw, item: dict) -> None:
    """Render one honest product-description Pin, not a fabricated workbook screenshot."""
    brand_font = font("DejaVuSans-Bold.ttf", 34)
    draw.text((86, 82), "THE CONTEXT ENGINE", font=brand_font, fill=INK, stroke_width=0)
    draw.line((86, 151, 914, 151), fill="#c6c7b4", width=3)

    eyebrow_font = font("DejaVuSans-Bold.ttf", 28)
    draw.text((86, 208), item["eyebrow"].upper(), font=eyebrow_font, fill=MUTED)

    heading_font, heading_rows = fit_lines(draw, item["headline"], "DejaVuSerif-Bold.ttf", 830, 5, 106, 62)
    heading_spacing = int(heading_font.size * 1.20)
    heading_top = 289
    for i, line in enumerate(heading_rows):
        draw.text((82, heading_top + i * heading_spacing), line, font=heading_font, fill=INK)

    support_top = heading_top + len(heading_rows) * heading_spacing + 45
    if support_top > 1040:
        raise SpecError(f"Headline leaves no support area: {item['id']}")
    support_font, support_rows = fit_lines(draw, item["support"], "DejaVuSans.ttf", 820, 4, 42, 30)
    for i, line in enumerate(support_rows):
        draw.text((86, support_top + i * 61), line, font=support_font, fill="#294639")
    if support_top + len(support_rows) * 61 > 1176:
        raise SpecError(f"Support text collides with footer: {item['id']}")

    draw.rounded_rectangle((78, 1195, 922, 1269), radius=25, fill="#e2e7db")
    descriptor_font = font("DejaVuSans-Bold.ttf", 27)
    draw.text((110, 1213), "KNOWLEDGE GOVERNANCE DIAGNOSTIC", font=descriptor_font, fill=INK)
    draw.rounded_rectangle((78, 1308, 922, 1432), radius=35, fill=INK)
    button_font, button_rows = fit_lines(draw, item["button"], "DejaVuSans-Bold.ttf", 730, 1, 46, 30)
    if len(button_rows) != 1:
        raise SpecError("CTA must fit on one line")
    button = button_rows[0]
    button_x = (WIDTH - line_width(draw, button, button_font)) / 2
    draw.text((button_x, 1340), button, font=button_font, fill=WHITE)


def render(item: dict, destination: Path) -> Path:
    image = Image.new("RGB", (WIDTH, HEIGHT), CREAM)
    draw = ImageDraw.Draw(image)
    tracked_preview(draw, item)
    destination.mkdir(parents=True, exist_ok=True)
    output = destination / item["asset"]
    image.save(output, format="PNG", optimize=True)
    if output.stat().st_size > 5_000_000:
        raise SpecError(f"Pin exceeds existing 5 MB publisher limit: {item['id']}")
    return output


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--spec", type=Path, default=SPEC_PATH)
    parser.add_argument("--output-dir", type=Path, default=ROOT / "assets")
    parser.add_argument("--check", action="store_true", help="Validate only, do not write images")
    args = parser.parse_args()
    items = read_specs(args.spec)
    for item in items:
        if args.check:
            print(f"VALID {item['id']}: {item['asset']}")
        else:
            print(f"RENDERED {item['id']}: {render(item, args.output_dir)}")


if __name__ == "__main__":
    main()
