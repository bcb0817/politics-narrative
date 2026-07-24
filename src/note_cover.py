"""Generate a local, copyright-safe note cover image."""

from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from PIL import Image, ImageDraw, ImageFont


WIDTH = 1280
HEIGHT = 670
JST = ZoneInfo("Asia/Tokyo")

_ACCENTS = {
    "weekly_top5": (239, 72, 82),
    "weekly_deep_dive": (239, 72, 82),
    "legislative_process": (44, 128, 214),
    "cabinet_decision_vs_law": (44, 128, 214),
    "social_insurance_burden": (34, 172, 144),
    "party_policy_comparison": (141, 91, 210),
    "evergreen_institutional_explainer": (44, 128, 214),
}

_TYPE_LABELS = {
    "weekly_top5": "WEEKLY TOP 5",
    "weekly_deep_dive": "WEEKLY DEEP DIVE",
    "legislative_process": "LEGISLATIVE PROCESS",
    "cabinet_decision_vs_law": "POLICY EXPLAINER",
    "social_insurance_burden": "SOCIAL POLICY",
    "party_policy_comparison": "POLICY COMPARISON",
    "evergreen_institutional_explainer": "INSTITUTIONAL EXPLAINER",
}


def _font_path(bold: bool) -> Path:
    env_name = (
        "FREE_NOTE_COVER_FONT_BOLD" if bold
        else "FREE_NOTE_COVER_FONT_REGULAR"
    )
    configured = os.environ.get(env_name, "").strip()
    candidates = [
        configured,
        r"C:\Windows\Fonts\YuGothB.ttc" if bold
        else r"C:\Windows\Fonts\YuGothM.ttc",
        r"C:\Windows\Fonts\meiryob.ttc" if bold
        else r"C:\Windows\Fonts\meiryo.ttc",
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc" if bold
        else "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    ]
    for candidate in candidates:
        if candidate and Path(candidate).is_file():
            return Path(candidate)
    raise FileNotFoundError("Japanese cover font not found")


def _font(size: int, *, bold: bool = False) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(str(_font_path(bold)), size=size)


def _text_width(draw: ImageDraw.ImageDraw, text: str,
                font: ImageFont.FreeTypeFont) -> int:
    box = draw.textbbox((0, 0), text, font=font)
    return box[2] - box[0]


def _wrap_title(draw: ImageDraw.ImageDraw, title: str,
                font: ImageFont.FreeTypeFont, max_width: int,
                max_lines: int = 3) -> list[str]:
    lines: list[str] = []
    current = ""
    for character in str(title).strip():
        candidate = current + character
        if current and _text_width(draw, candidate, font) > max_width:
            lines.append(current.rstrip())
            current = character.lstrip()
            if len(lines) == max_lines - 1:
                break
        else:
            current = candidate
    consumed = "".join(lines) + current
    remaining = str(title).strip()[len(consumed):]
    if current:
        if remaining:
            ellipsis = "…"
            while current and _text_width(
                draw, current + ellipsis, font
            ) > max_width:
                current = current[:-1]
            current = current.rstrip() + ellipsis
        lines.append(current)
    return lines[:max_lines]


def _gradient(accent: tuple[int, int, int]) -> Image.Image:
    image = Image.new("RGB", (WIDTH, HEIGHT))
    pixels = image.load()
    for y in range(HEIGHT):
        vertical = y / max(1, HEIGHT - 1)
        for x in range(WIDTH):
            horizontal = x / max(1, WIDTH - 1)
            glow = max(0.0, 1.0 - (
                (horizontal - .83) ** 2 + (vertical - .18) ** 2
            ) ** .5 * 1.8)
            pixels[x, y] = (
                int(13 + accent[0] * glow * .22),
                int(20 + accent[1] * glow * .22),
                int(34 + accent[2] * glow * .22),
            )
    return image


def generate_cover(
    title: str,
    article_type: str,
    output_path: Path,
    *,
    generated_at: datetime | None = None,
) -> dict:
    """Create a 1280x670 PNG and return metadata."""
    generated_at = generated_at or datetime.now(JST)
    accent = _ACCENTS.get(article_type, (44, 128, 214))
    image = _gradient(accent)
    draw = ImageDraw.Draw(image, "RGBA")

    # Editorial frame and abstract civic architecture.
    draw.rounded_rectangle(
        (42, 42, WIDTH - 42, HEIGHT - 42),
        radius=28,
        outline=(255, 255, 255, 42),
        width=2,
    )
    draw.polygon(
        [(880, 0), (1280, 0), (1280, 670), (1120, 670)],
        fill=(*accent, 42),
    )
    for index, alpha in enumerate((44, 34, 26, 18)):
        x = 930 + index * 72
        draw.rounded_rectangle(
            (x, 170 - index * 12, x + 38, 540),
            radius=10,
            fill=(255, 255, 255, alpha),
        )
    draw.polygon(
        [(900, 184), (1102, 82), (1262, 184)],
        fill=(255, 255, 255, 25),
    )
    draw.line((82, 112, 316, 112), fill=(*accent, 255), width=8)

    label_font = _font(28, bold=True)
    small_font = _font(24)
    brand_font = _font(28, bold=True)
    draw.text(
        (82, 67),
        _TYPE_LABELS.get(article_type, "POLITICAL NOTE"),
        font=label_font,
        fill=(236, 241, 250, 235),
    )

    max_title_width = 790
    title_font = _font(64, bold=True)
    lines = _wrap_title(draw, title, title_font, max_title_width)
    while len(lines) > 3 and title_font.size > 44:
        title_font = _font(title_font.size - 4, bold=True)
        lines = _wrap_title(draw, title, title_font, max_title_width)
    line_height = int(title_font.size * 1.38)
    title_y = 182
    for index, line in enumerate(lines):
        draw.text(
            (82, title_y + index * line_height),
            line,
            font=title_font,
            fill=(255, 255, 255, 255),
            stroke_width=1,
            stroke_fill=(4, 8, 16, 130),
        )

    draw.text(
        (82, 574),
        generated_at.astimezone(JST).strftime("%Y.%m.%d"),
        font=small_font,
        fill=(205, 215, 232, 220),
    )
    brand = "久世ゆい｜政治と制度を読み解く"
    brand_width = _text_width(draw, brand, brand_font)
    draw.text(
        (WIDTH - 82 - brand_width, 568),
        brand,
        font=brand_font,
        fill=(245, 247, 252, 240),
    )

    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = output_path.with_name(
        f".{output_path.stem}.{os.getpid()}.tmp.png")
    image.save(temporary, format="PNG", optimize=True)
    os.replace(temporary, output_path)
    return {
        "cover_status": "generated",
        "cover_path": str(output_path),
        "cover_width": WIDTH,
        "cover_height": HEIGHT,
        "cover_aspect_ratio": round(WIDTH / HEIGHT, 4),
        "cover_generator": "local_pillow_v1",
    }
