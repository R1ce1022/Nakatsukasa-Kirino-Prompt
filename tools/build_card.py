#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""校验并打包《中务桐乃》SillyTavern 角色卡（Character Card V2）。

用法：
    python tools/build_card.py
    python tools/build_card.py --avatar 你的立绘.png
    python tools/build_card.py --avatar 你的立绘.png --fit contain
    python tools/build_card.py --check

脚本以 Nakatsukasa-Kirino-Card.json 为唯一内容源：
1. 校验 V2 结构；
2. 生成头像（cover 居中裁剪 / contain 完整留边；未指定 --avatar 时沿用现有卡片头像）；
3. 把压缩后的 V2 JSON 以 base64 写进 PNG 的 chara tEXt chunk；
4. 回读 PNG，确认内嵌数据与源 JSON 完全一致。
"""

from __future__ import annotations

import argparse
import base64
import binascii
import hashlib
import io
import json
import os
import struct
import sys
import zlib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CARD = ROOT / "Nakatsukasa-Kirino-Card.json"
DEFAULT_OUT = ROOT / "Nakatsukasa-Kirino-Card.png"
AVATAR_WIDTH = 512
AVATAR_HEIGHT = 768
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
POSITIONS = ("before_char", "after_char")

REQUIRED_DATA_FIELDS = [
    "name",
    "description",
    "personality",
    "scenario",
    "first_mes",
    "mes_example",
    "creator_notes",
    "system_prompt",
    "post_history_instructions",
    "alternate_greetings",
    "tags",
    "creator",
    "character_version",
    "extensions",
]
STRING_FIELDS = [
    "name",
    "description",
    "personality",
    "scenario",
    "first_mes",
    "mes_example",
    "creator_notes",
    "system_prompt",
    "post_history_instructions",
    "creator",
    "character_version",
]

WINDIR = Path(os.environ.get("WINDIR", r"C:\Windows"))
FONT_DIR = WINDIR / "Fonts"
CJK_BOLD_FONTS = [FONT_DIR / "msyhbd.ttc", FONT_DIR / "msyh.ttc", FONT_DIR / "simhei.ttf"]
CJK_REGULAR_FONTS = [FONT_DIR / "msyh.ttc", FONT_DIR / "msyhbd.ttc", FONT_DIR / "simhei.ttf"]
JP_FONTS = [FONT_DIR / "YuGothB.ttc", FONT_DIR / "YuGothM.ttc"] + CJK_BOLD_FONTS
LATIN_BOLD_FONTS = [FONT_DIR / "seguisb.ttf", FONT_DIR / "bahnschrift.ttf", FONT_DIR / "arialbd.ttf"]
LATIN_REGULAR_FONTS = [FONT_DIR / "segoeui.ttf", FONT_DIR / "bahnschrift.ttf", FONT_DIR / "arial.ttf"]


class CardError(RuntimeError):
    """可读的卡片构建错误。"""


def configure_stdio() -> None:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except (OSError, ValueError):
                pass


def load_card(path: Path) -> dict:
    if not path.is_file():
        raise CardError(f"找不到卡片 JSON：{path}")
    raw = path.read_bytes()
    if raw.startswith(b"\xef\xbb\xbf"):
        raise CardError("卡片 JSON 带 BOM，请保存为无 BOM 的 UTF-8")
    try:
        card = json.loads(raw.decode("utf-8"))
    except UnicodeDecodeError as exc:
        raise CardError(f"卡片 JSON 不是 UTF-8：{exc}") from exc
    except json.JSONDecodeError as exc:
        raise CardError(
            f"卡片 JSON 语法错误：第 {exc.lineno} 行第 {exc.colno} 列：{exc.msg}"
        ) from exc
    if not isinstance(card, dict):
        raise CardError("卡片 JSON 根节点必须是对象")
    return card


def validate_card(card: dict) -> tuple[list[str], list[str]]:
    errors: list[str] = []
    warnings: list[str] = []

    if card.get("spec") != "chara_card_v2":
        errors.append('spec 必须为 "chara_card_v2"')
    if card.get("spec_version") != "2.0":
        errors.append('spec_version 必须为 "2.0"')
    for key in card:
        if key not in ("spec", "spec_version", "data"):
            warnings.append(f"顶层存在非 V2 字段 {key!r}（建议移除，否则可能被识别成 V1）")

    data = card.get("data")
    if not isinstance(data, dict):
        errors.append("缺少 data 对象")
        return errors, warnings

    for field in REQUIRED_DATA_FIELDS:
        if field not in data:
            errors.append(f"data.{field} 缺失（V2 必需字段）")
    for field in STRING_FIELDS:
        if field in data and not isinstance(data[field], str):
            errors.append(f"data.{field} 必须是字符串")
    for field in ("alternate_greetings", "tags"):
        if field in data:
            value = data[field]
            if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
                errors.append(f"data.{field} 必须是字符串数组")

    for field in ("name", "description", "first_mes"):
        value = data.get(field)
        if isinstance(value, str) and not value.strip():
            errors.append(f"data.{field} 不能为空")

    name = data.get("name")
    if isinstance(name, str) and any(ch in name for ch in '\\/:*?"<>|'):
        warnings.append("data.name 含文件名非法字符，酒馆导入时会自动替换")

    example = data.get("mes_example")
    if isinstance(example, str):
        if "<START>" not in example:
            warnings.append("mes_example 缺少 <START> 分隔符，酒馆可能无法正确解析示例")
        for macro in ("{{user}}", "{{char}}"):
            if macro not in example:
                warnings.append(f"mes_example 未使用 {macro} 宏，示例里的说话人不会随用户/角色名变化")

    greetings = data.get("alternate_greetings")
    if isinstance(greetings, list):
        if len(greetings) < 2:
            warnings.append(f"备选开场只有 {len(greetings)} 个，建议至少 2 个")
        for index, greeting in enumerate(greetings):
            if isinstance(greeting, str) and not greeting.strip():
                errors.append(f"data.alternate_greetings[{index}] 为空")

    extensions = data.get("extensions")
    if not isinstance(extensions, dict):
        errors.append("data.extensions 必须是对象")
    else:
        depth = extensions.get("depth_prompt")
        if depth is not None:
            if not isinstance(depth, dict):
                errors.append("data.extensions.depth_prompt 必须是对象")
            else:
                prompt = depth.get("prompt")
                if prompt is not None and not isinstance(prompt, str):
                    errors.append("data.extensions.depth_prompt.prompt 必须是字符串")
                depth_value = depth.get("depth")
                if depth_value is not None and (
                    isinstance(depth_value, bool) or not isinstance(depth_value, (int, float))
                ):
                    errors.append("data.extensions.depth_prompt.depth 必须是数字")
                role = depth.get("role")
                if role is not None and role not in ("system", "user", "assistant"):
                    errors.append("data.extensions.depth_prompt.role 必须是 system/user/assistant")

    book = data.get("character_book")
    if book is not None:
        if not isinstance(book, dict):
            errors.append("data.character_book 必须是对象")
        else:
            if not isinstance(book.get("extensions"), dict):
                errors.append("data.character_book.extensions 必须是对象")
            entries = book.get("entries")
            if not isinstance(entries, list) or not entries:
                errors.append("data.character_book.entries 必须是非空数组")
            else:
                seen_ids: set[object] = set()
                key_owner: dict[str, str] = {}
                constant_entries = 0
                for index, entry in enumerate(entries):
                    where = f"data.character_book.entries[{index}]"
                    if not isinstance(entry, dict):
                        errors.append(f"{where} 必须是对象")
                        continue
                    entry_id = entry.get("id")
                    if entry_id is not None:
                        if isinstance(entry_id, bool) or not isinstance(entry_id, int):
                            errors.append(f"{where}.id 必须是整数")
                        elif entry_id in seen_ids:
                            errors.append(f"{where}.id 与其他条目重复：{entry_id}")
                        else:
                            seen_ids.add(entry_id)
                    keys = entry.get("keys")
                    if not isinstance(keys, list) or not keys:
                        errors.append(f"{where}.keys 必须是非空数组")
                    else:
                        for key in keys:
                            if not isinstance(key, str) or not key.strip():
                                errors.append(f"{where}.keys 含空关键词")
                    content = entry.get("content")
                    if not isinstance(content, str) or not content.strip():
                        errors.append(f"{where}.content 不能为空")
                    if not isinstance(entry.get("extensions"), dict):
                        errors.append(f"{where}.extensions 必须是对象")
                    if not isinstance(entry.get("enabled"), bool):
                        errors.append(f"{where}.enabled 必须是布尔值")
                    order = entry.get("insertion_order")
                    if isinstance(order, bool) or not isinstance(order, (int, float)):
                        errors.append(f"{where}.insertion_order 必须是数字")
                    position = entry.get("position")
                    if position is not None and position not in POSITIONS:
                        errors.append(f"{where}.position 必须是 before_char 或 after_char")
                    constant = entry.get("constant", False)
                    if not isinstance(constant, bool):
                        errors.append(f"{where}.constant 必须是布尔值")
                    elif constant:
                        constant_entries += 1
                    if not isinstance(entry.get("selective", False), bool):
                        errors.append(f"{where}.selective 必须是布尔值")
                    secondary = entry.get("secondary_keys", [])
                    if not isinstance(secondary, list) or not all(
                        isinstance(key, str) for key in secondary
                    ):
                        errors.append(f"{where}.secondary_keys 必须是字符串数组")
                    owner = str(entry.get("comment") or entry.get("name") or where)
                    if isinstance(keys, list):
                        for key in keys:
                            if not isinstance(key, str) or not key.strip():
                                continue
                            normalized = key.casefold()
                            if normalized in key_owner and key_owner[normalized] != owner:
                                warnings.append(
                                    f"关键词「{key}」同时出现在「{key_owner[normalized]}」与「{owner}」中，可能重复触发"
                                )
                            else:
                                key_owner[normalized] = owner
                if constant_entries:
                    warnings.append(f"有 {constant_entries} 个常驻人物书条目，会每轮进入上下文")

    permanent = 0
    for field in ("description", "personality", "scenario"):
        value = data.get(field)
        if isinstance(value, str):
            permanent += len(value)
    if permanent > 4000:
        warnings.append(f"常驻字段合计 {permanent} 字，偏长，可能挤占上下文")

    return errors, warnings


def print_stats(card: dict) -> None:
    data = card["data"]
    print(f"名称：{data.get('name')}")
    print(f"版本：{data.get('character_version')}    作者：{data.get('creator')}")
    print("字段字数：")
    labels = [
        ("description", "描述"),
        ("personality", "性格"),
        ("scenario", "场景"),
        ("first_mes", "默认开场"),
        ("mes_example", "对话示例"),
        ("creator_notes", "作者注释"),
    ]
    for field, label in labels:
        value = data.get(field, "")
        if isinstance(value, str):
            print(f"  - {label}：{len(value)} 字")
    greetings = data.get("alternate_greetings", [])
    if isinstance(greetings, list):
        print(f"  - 备选开场：{len(greetings)} 个（{', '.join(str(len(item)) for item in greetings)} 字）")
    depth = (data.get("extensions") or {}).get("depth_prompt") if isinstance(data.get("extensions"), dict) else None
    if isinstance(depth, dict) and isinstance(depth.get("prompt"), str) and depth["prompt"]:
        print(f"  - 角色小抄：depth {depth.get('depth', 4)}，{len(depth['prompt'])} 字")
    book = data.get("character_book") or {}
    entries = book.get("entries") or []
    constant = sum(1 for entry in entries if isinstance(entry, dict) and entry.get("constant"))
    print(f"人物书：{len(entries)} 条（常驻 {constant} 条）")
    permanent = sum(len(data.get(field, "")) for field in ("description", "personality", "scenario") if isinstance(data.get(field), str))
    print(f"常驻字段合计：{permanent} 字（不含人物书与示例）")


def iter_png_chunks(png: bytes):
    if not png.startswith(PNG_SIGNATURE):
        raise CardError("不是有效的 PNG 文件")
    position = len(PNG_SIGNATURE)
    while position < len(png):
        if position + 8 > len(png):
            raise CardError("PNG 数据在 chunk 头处截断")
        length = struct.unpack(">I", png[position:position + 4])[0]
        ctype = png[position + 4:position + 8]
        end = position + 12 + length
        if end > len(png):
            raise CardError("PNG 数据在 chunk 内容处截断")
        yield ctype, png[position:end], png[position + 8:position + 8 + length]
        position = end
        if ctype == b"IEND":
            return
    raise CardError("PNG 缺少 IEND chunk")


def embed_chara(png: bytes, card_json: bytes) -> bytes:
    kept = []
    for ctype, raw, data in iter_png_chunks(png):
        if ctype == b"tEXt":
            keyword = data.split(b"\x00", 1)[0].lower()
            if keyword in (b"chara", b"ccv3"):
                continue
        kept.append((ctype, raw))
    if not kept or kept[-1][0] != b"IEND":
        raise CardError("PNG 缺少 IEND chunk")
    text = b"chara\x00" + base64.b64encode(card_json)
    crc = zlib.crc32(b"tEXt" + text) & 0xFFFFFFFF
    chunk = struct.pack(">I", len(text)) + b"tEXt" + text + struct.pack(">I", crc)
    out = [PNG_SIGNATURE]
    for ctype, raw in kept:
        if ctype == b"IEND":
            out.append(chunk)
        out.append(raw)
    return b"".join(out)


def read_chara(png: bytes) -> dict:
    found = None
    for ctype, _raw, data in iter_png_chunks(png):
        if ctype != b"tEXt":
            continue
        keyword, _, text = data.partition(b"\x00")
        if keyword.lower() == b"chara":
            found = text
            break
    if found is None:
        raise CardError("PNG 里没有 chara tEXt chunk")
    try:
        raw_json = base64.b64decode(found, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise CardError(f"chara chunk 不是有效的 base64：{exc}") from exc
    try:
        return json.loads(raw_json.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CardError(f"chara chunk 不是有效的 UTF-8 JSON：{exc}") from exc


def _pillow():
    try:
        from PIL import Image, ImageDraw, ImageFilter, ImageFont, ImageOps
    except ImportError as exc:
        raise CardError("需要 Pillow 才能生成 PNG：pip install pillow") from exc
    return Image, ImageDraw, ImageFilter, ImageFont, ImageOps


def _font_path(candidates) -> Path | None:
    for candidate in candidates:
        path = Path(candidate)
        if path.is_file():
            return path
    return None


def _load_font(size: int, candidates, label: str):
    _Image, _ImageDraw, _ImageFilter, ImageFont, _ImageOps = _pillow()
    path = _font_path(candidates)
    if path is None:
        raise CardError(f"找不到{label}字体（{', '.join(str(item) for item in candidates[:2])} 等）")
    try:
        return ImageFont.truetype(str(path), size)
    except OSError as exc:
        raise CardError(f"加载字体失败：{path}：{exc}") from exc


def _text_center(draw, xy, text: str, font, fill, spacing: int = 0) -> None:
    if spacing == 0:
        draw.text(xy, text, font=font, fill=fill, anchor="mm")
        return
    widths = [draw.textlength(char, font=font) for char in text]
    total = sum(widths) + spacing * (len(text) - 1)
    x = xy[0] - total / 2
    for char, width in zip(text, widths):
        draw.text((x, xy[1]), char, font=font, fill=fill, anchor="lm")
        x += width + spacing


def _star(cx: float, cy: float, radius: float) -> list[tuple[float, float]]:
    import math

    points = []
    for index in range(10):
        angle = math.radians(-90 + index * 36)
        length = radius if index % 2 == 0 else radius * 0.42
        points.append((cx + math.cos(angle) * length, cy + math.sin(angle) * length))
    return points


def _gradient(size):
    Image, ImageDraw, _ImageFilter, _ImageFont, _ImageOps = _pillow()
    width, height = size
    image = Image.new("RGBA", size)
    draw = ImageDraw.Draw(image)
    top = (244, 251, 255)
    bottom = (176, 219, 251)
    for y in range(height):
        ratio = y / max(1, height - 1)
        color = tuple(round(top[i] + (bottom[i] - top[i]) * ratio) for i in range(3))
        draw.line([(0, y), (width, y)], fill=color + (255,))
    return image


def render_placeholder():
    Image, ImageDraw, ImageFilter, ImageFont, ImageOps = _pillow()
    width, height = AVATAR_WIDTH, AVATAR_HEIGHT
    image = _gradient((width, height)).convert("RGB")
    draw = ImageDraw.Draw(image)

    overlay = Image.new("RGBA", image.size, (0, 0, 0, 0))
    overlay_draw = ImageDraw.Draw(overlay)
    for x in range(-height, width + height, 56):
        overlay_draw.line([(x, 0), (x + height, height)], fill=(255, 255, 255, 16), width=1)
    glow = Image.new("RGBA", image.size, (0, 0, 0, 0))
    glow_draw = ImageDraw.Draw(glow)
    glow_draw.ellipse((-80, -180, 420, 340), fill=(255, 255, 255, 62))
    glow = glow.filter(ImageFilter.GaussianBlur(46))
    overlay = Image.alpha_composite(overlay, glow)
    image = Image.alpha_composite(image.convert("RGBA"), overlay)

    shadow = Image.new("RGBA", image.size, (0, 0, 0, 0))
    shadow_draw = ImageDraw.Draw(shadow)
    shadow_draw.rounded_rectangle(
        (44, 98, 468, 724), radius=30, fill=(37, 76, 120, 48)
    )
    shadow = shadow.filter(ImageFilter.GaussianBlur(13))
    image = Image.alpha_composite(image, shadow)

    panel = Image.new("RGBA", image.size, (0, 0, 0, 0))
    panel_draw = ImageDraw.Draw(panel)
    panel_draw.rounded_rectangle(
        (40, 86, 472, 712), radius=30, fill=(255, 255, 255, 242), outline=(255, 255, 255, 255), width=1
    )
    image = Image.alpha_composite(image, panel)
    draw = ImageDraw.Draw(image)

    halo_glow = Image.new("RGBA", image.size, (0, 0, 0, 0))
    halo_draw = ImageDraw.Draw(halo_glow)
    halo_draw.ellipse((132, 182, 380, 254), outline=(120, 205, 255, 96), width=14)
    halo_glow = halo_glow.filter(ImageFilter.GaussianBlur(8))
    image = Image.alpha_composite(image, halo_glow)
    draw = ImageDraw.Draw(image)
    draw.ellipse((150, 196, 362, 240), outline=(126, 206, 255), width=5)

    hat_fill = (249, 253, 255)
    hat_line = (157, 198, 232)
    draw.ellipse((176, 224, 336, 318), fill=hat_fill, outline=hat_line, width=3)
    draw.ellipse((174, 300, 338, 334), fill=(224, 241, 253), outline=hat_line, width=3)
    draw.line([(198, 303), (314, 303)], fill=(157, 198, 232), width=2)
    gold = (206, 166, 86)
    draw.ellipse((234, 246, 278, 290), outline=gold, width=2)
    draw.polygon(_star(256, 268, 9), fill=gold)

    navy = (23, 47, 85)
    slate = (62, 97, 137)
    pale = (139, 169, 197)
    soft = (160, 186, 210)
    font_label = _load_font(13, LATIN_BOLD_FONTS, "拉丁粗体")
    font_label_small = _load_font(11, LATIN_REGULAR_FONTS, "拉丁常规")
    font_name = _load_font(64, CJK_BOLD_FONTS, "中文粗体")
    font_jp = _load_font(24, JP_FONTS, "日文字体")
    font_latin = _load_font(18, LATIN_REGULAR_FONTS, "拉丁常规")
    font_subtitle = _load_font(21, CJK_REGULAR_FONTS, "中文字体")
    font_capsule = _load_font(13, LATIN_BOLD_FONTS, "拉丁粗体")
    font_footer = _load_font(10, LATIN_REGULAR_FONTS, "拉丁常规")

    _text_center(draw, (256, 121), "BLUE ARCHIVE", font_label, (124, 157, 187), spacing=6)
    _text_center(draw, (256, 143), "VALKYRIE POLICE ACADEMY", font_label_small, soft, spacing=3)
    _text_center(draw, (256, 392), "中务桐乃", font_name, navy)
    _text_center(draw, (256, 447), "中務キリノ", font_jp, slate)
    _text_center(draw, (256, 479), "NAKATSUKASA KIRINO", font_latin, (107, 143, 181), spacing=2)
    draw.line([(150, 512), (228, 512)], fill=(198, 219, 236), width=1)
    draw.line([(284, 512), (362, 512)], fill=(198, 219, 236), width=1)
    draw.polygon([(256, 505), (262, 512), (256, 519), (250, 512)], fill=(160, 192, 219))
    _text_center(draw, (256, 540), "瓦尔基里警察学院 · 生活安全局", font_subtitle, slate)
    draw.rounded_rectangle((196, 582, 316, 620), radius=19, outline=(179, 209, 232), width=2)
    _text_center(draw, (256, 601), "K.S.P.D.", font_capsule, (77, 116, 156), spacing=3)
    _text_center(draw, (256, 658), "SILLYTAVERN CHARACTER CARD", font_label_small, pale, spacing=3)
    _text_center(draw, (256, 678), "CHARACTER CARD V2", font_footer, (167, 189, 210), spacing=2)

    return image.convert("RGB")


def load_avatar(path: Path, fit: str = "cover"):
    Image, _ImageDraw, _ImageFilter, _ImageFont, ImageOps = _pillow()
    if not path.is_file():
        raise CardError(f"找不到头像文件：{path}")
    if fit not in ("cover", "contain"):
        raise CardError(f"不支持的适配方式：{fit}")
    try:
        with Image.open(path) as source:
            image = source.convert("RGBA")
            canvas = _gradient((AVATAR_WIDTH, AVATAR_HEIGHT))
            if fit == "contain":
                ratio = min(AVATAR_WIDTH / image.width, AVATAR_HEIGHT / image.height)
                size = (
                    max(1, round(image.width * ratio)),
                    max(1, round(image.height * ratio)),
                )
                image = image.resize(size, Image.Resampling.LANCZOS)
                canvas.alpha_composite(
                    image,
                    ((AVATAR_WIDTH - size[0]) // 2, (AVATAR_HEIGHT - size[1]) // 2),
                )
            else:
                image = ImageOps.fit(
                    image,
                    (AVATAR_WIDTH, AVATAR_HEIGHT),
                    method=Image.Resampling.LANCZOS,
                    centering=(0.5, 0.5),
                )
                canvas.alpha_composite(image)
            return canvas.convert("RGB")
    except (OSError, ValueError) as exc:
        raise CardError(f"读取头像失败：{exc}") from exc


def load_existing_card_image(path: Path):
    Image, _ImageDraw, _ImageFilter, _ImageFont, _ImageOps = _pillow()
    try:
        with Image.open(path) as source:
            if source.size != (AVATAR_WIDTH, AVATAR_HEIGHT):
                raise CardError(f"现有卡片尺寸异常：{source.size}")
            image = source.convert("RGBA")
            if image.getchannel("A").getextrema() == (255, 255):
                return image.convert("RGB")
            return image
    except (OSError, ValueError) as exc:
        raise CardError(f"读取现有卡片头像失败：{exc}") from exc


def save_png(image, out: Path, card: dict) -> bytes:
    buffer = io.BytesIO()
    image.save(buffer, format="PNG", optimize=True)
    png = buffer.getvalue()
    card_json = json.dumps(card, ensure_ascii=True, separators=(",", ":")).encode("utf-8")
    png = embed_chara(png, card_json)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(png)
    return png


def verify_png(path: Path, card: dict) -> bytes:
    png = path.read_bytes()
    if read_chara(png) != card:
        raise CardError("PNG 内嵌 JSON 与卡片源 JSON 不一致")
    for ctype, _raw, data in iter_png_chunks(png):
        if ctype == b"tEXt" and data.split(b"\x00", 1)[0].lower() == b"ccv3":
            raise CardError("PNG 里不应包含 ccv3 chunk（本卡是 V2）")
    Image, _ImageDraw, _ImageFilter, _ImageFont, _ImageOps = _pillow()
    with Image.open(path) as image:
        image.load()
        if image.size != (AVATAR_WIDTH, AVATAR_HEIGHT):
            raise CardError(f"PNG 尺寸异常：{image.size}")
        if image.format != "PNG":
            raise CardError(f"输出文件不是 PNG：{image.format}")
    return png


def parse_args(argv):
    parser = argparse.ArgumentParser(
        description="校验 Nakatsukasa-Kirino-Card.json，并生成内嵌角色数据的 PNG。"
    )
    parser.add_argument("--card", type=Path, default=DEFAULT_CARD, help="卡片 JSON 路径")
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT, help="输出 PNG 路径")
    parser.add_argument(
        "--avatar",
        type=Path,
        default=None,
        help="可选：头像源图；不指定时沿用输出 PNG 里已有的头像，首次构建才生成占位图",
    )
    parser.add_argument(
        "--fit",
        choices=("cover", "contain"),
        default="cover",
        help="头像适配：cover 居中裁剪（默认），contain 完整保留并留边",
    )
    parser.add_argument("--check", action="store_true", help="只校验 JSON，不生成 PNG")
    return parser.parse_args(argv)


def main(argv=None) -> int:
    configure_stdio()
    args = parse_args(argv)
    try:
        card = load_card(args.card)
        errors, warnings = validate_card(card)
        if errors:
            print("[校验失败]", file=sys.stderr)
            for error in errors:
                print(f"  - {error}", file=sys.stderr)
            print("请修正 JSON 后重试。", file=sys.stderr)
            return 1
        for warning in warnings:
            print(f"[提示] {warning}")
        print_stats(card)
        if args.check:
            print("\n[完成] 卡片结构校验通过（未生成 PNG）。")
            return 0
        if args.avatar is not None:
            image = load_avatar(args.avatar, args.fit)
            print(f"\n使用头像：{args.avatar}（{args.fit}）")
        elif args.out.is_file():
            image = load_existing_card_image(args.out)
            print(f"\n未指定 --avatar，沿用现有卡片头像：{args.out}")
        else:
            image = render_placeholder()
            print("\n未指定 --avatar 且没有现有卡片，使用内置占位头像。")
        png = save_png(image, args.out, card)
        verify_png(args.out, card)
        embedded = json.dumps(card, ensure_ascii=True, separators=(",", ":"))
        digest = hashlib.sha256(png).hexdigest()
        print(f"[完成] 已生成：{args.out}")
        print(f"  大小：{len(png):,} bytes")
        print(f"  SHA256：{digest}")
        print(f"  内嵌：chara tEXt（base64 V2 JSON，{len(embedded):,} 字符）")
        return 0
    except CardError as exc:
        print(f"[错误] {exc}", file=sys.stderr)
        return 1
    except (OSError, ValueError) as exc:
        print(f"[错误] {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
