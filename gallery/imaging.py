import hashlib
import io
import math
import re
from functools import lru_cache
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageOps, UnidentifiedImageError

from .fonts import load_fonts

MAX_PIXELS = 16_000_000
MAX_FRAMES = 500
MAX_TOTAL_PIXELS = 100_000_000
Image.MAX_IMAGE_PIXELS = MAX_PIXELS


def decode(data: bytes) -> tuple[list[Image.Image], list[int], int | None]:
    try:
        with Image.open(io.BytesIO(data)) as source:
            total = getattr(source, "n_frames", 1)
            pixels = source.width * source.height
            if (pixels > MAX_PIXELS or total > MAX_FRAMES
                    or pixels * total > MAX_TOTAL_PIXELS):
                raise ValueError("图片尺寸或动画帧数超出处理上限。")
            frames, durations = [], []
            loop = source.info.get("loop")
            for i in range(total):
                source.seek(i)
                frames.append(ImageOps.exif_transpose(source).convert("RGBA"))
                durations.append(max(10, int(source.info.get("duration", 100))))
            return frames, durations, loop
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError) as exc:
        raise ValueError("无法读取图片，文件损坏或格式不受支持。") from exc


def fingerprint(frames: list[Image.Image]) -> str:
    # Match the conventional 32x32 DCT / 8x8 pHash, including the DC coefficient.
    n = 32
    basis = np.cos(np.pi * np.arange(8)[:, None] * (2 * np.arange(n) + 1) / (2 * n))
    hashes = []
    for frame in frames:
        background = Image.new("RGBA", frame.size, "white")
        background.alpha_composite(frame)
        pixels = np.asarray(background.convert("L").resize((n, n), Image.Resampling.LANCZOS),
                            dtype=np.float64)
        values = basis @ pixels @ basis.T
        bits = np.packbits(values > np.median(values))
        hashes.append(bits.tobytes())
    return hashlib.sha256(len(frames).to_bytes(4, "big") + b"".join(hashes)).hexdigest()


def mirror(frame: Image.Image, direction: str) -> Image.Image:
    result = frame.copy()
    w, h = frame.size
    if direction == "left":
        result.paste(ImageOps.mirror(frame.crop((0, 0, w // 2, h))), ((w + 1) // 2, 0))
    elif direction == "right":
        result.paste(ImageOps.mirror(frame.crop(((w + 1) // 2, 0, w, h))), (0, 0))
    elif direction == "top":
        result.paste(ImageOps.flip(frame.crop((0, 0, w, h // 2))), (0, (h + 1) // 2))
    else:
        result.paste(ImageOps.flip(frame.crop((0, (h + 1) // 2, w, h))), (0, 0))
    return result


def transform(data: bytes, action: str, option: str = "") -> bytes:
    frames, durations, loop = decode(data)
    if action == "flip":
        operation = ImageOps.mirror if option == "horizontal" else ImageOps.flip
        frames = [operation(frame) for frame in frames]
    elif action == "mirror":
        frames = [mirror(frame, option) for frame in frames]
    elif action == "reverse":
        if len(frames) < 2:
            raise ValueError("倒转需要动态图。")
        frames.reverse()
        durations.reverse()
    elif action == "speed":
        if len(frames) < 2:
            raise ValueError("加速需要动态图。")
        factor = float(option)
        adjusted = [duration / factor for duration in durations]
        if any(not math.isfinite(d) or d > 655350 for d in adjusted):
            raise ValueError("倍率过小，单帧时长超过 GIF 支持范围。")
        durations = [max(10, round(d / 10) * 10) for d in adjusted]
    else:
        raise ValueError("未知的图片处理操作。")
    output = io.BytesIO()
    if len(frames) == 1:
        frames[0].save(output, format="PNG")
    else:
        kwargs = {"loop": loop} if loop is not None else {}
        frames[0].save(output, format="GIF", save_all=True, append_images=frames[1:],
                       duration=durations, disposal=2, optimize=False, **kwargs)
    return output.getvalue()


def help_image(font_path: str = "", wait_timeout: int = 120,
               forward_threshold: int = 5, multi_image_mode: str = "direct", *,
               font_dir: str = "", font_order=(), admin_ids=(),
               admin_gallery_permissions=(), admin_image_permissions=(),
               command_prefix: str | None = None) -> bytes:
    font = load_fonts(font_dir, 24, font_order, font_path)
    heading = load_fonts(font_dir, 34, font_order, font_path)
    subheading = load_fonts(font_dir, 28, font_order, font_path)
    source = (Path(__file__).resolve().parent.parent / "docs" / "help.md").read_text(encoding="utf-8")
    restricted = set()
    if any(str(value).strip() for value in admin_ids):
        restricted.update(admin_gallery_permissions)
        restricted.update(admin_image_permissions)
    blocks = []
    rows = []
    for line in source.splitlines():
        text = line.strip()
        if "/" in text and not text.startswith(("#", "-")):
            cells = re.split(r"\s{2,}", text)
            if text.startswith("/"):
                cells.insert(0, "")
            cells += [""] * (3 - len(cells))
            label, command, description = cells
            description = description.removesuffix("(管理员)")
            permission = ""
            if command.startswith("/创建"):
                permission = "创建图库"
            elif command.startswith("/图库重命名"):
                permission = "重命名图库"
            elif command.startswith("/图库删除"):
                permission = "删除图库"
            elif command.startswith("/图库别名"):
                if "[添加]" in command:
                    permission = "添加图库别名"
                elif "[删除]" in command:
                    permission = "删除图库别名"
            elif command.startswith("/上传"):
                permission = "上传图片"
            elif command.startswith("/删除"):
                permission = "删除图片"
            if permission in restricted:
                description += "(管理员)"
            # 仅在图片中放大原有空格，保留紧凑命令写法及原始文案。
            prefix = "/" if command_prefix is None else command_prefix
            row = (label, prefix + command[1:].replace(" ", "   "), description)
            rows.append(row)
            blocks.append(("row", row))
        elif text.startswith("## "):
            blocks.append(("subheading", text[3:]))
        elif text.startswith("# "):
            blocks.append(("heading", text[2:]))
        else:
            blocks.append(("text" if text else "space", text))
            if command_prefix is None and text == "|  连接多个可以使用的参数":
                blocks.append(("text", "命令前缀请按实际情况使用，/仅做示意"))

    # 全部命令共用列宽，按字体实际像素宽度定位，不依赖空格补齐。
    widths = [math.ceil(max(font.length(row[i]) for row in rows)) for i in range(3)]
    columns = (40, 40 + widths[0] + 32, 40 + widths[0] + widths[1] + 64)
    width = max(1000, columns[2] + widths[2] + 40)
    instructions = []
    y = 32
    for kind, value in blocks:
        if kind == "space":
            y += 16
        elif kind == "row":
            for x, cell in zip(columns, value):
                if cell:
                    instructions.append((font, (x, y), cell, "#222222"))
            y += 40
        elif kind in ("heading", "subheading"):
            face = heading if kind == "heading" else subheading
            color = "#244a86" if kind == "heading" else "#3675a6"
            instructions.append((face, (40, y), value, color))
            y += 54 if kind == "heading" else 46
        else:
            bullet = value.startswith("- ")
            content = value[2:] if bullet else value
            if bullet:
                instructions.append((font, (40, y), "•", "#222222"))
            x = 64 if bullet else 40
            for line in font.wrap(content, width - x - 40):
                instructions.append((font, (x, y), line, "#222222"))
                y += 40
    if width * (y + 32) > MAX_PIXELS:
        raise ValueError("帮助图片超出尺寸上限。")
    result = Image.new("RGB", (width, y + 32), "white")
    for face, position, text, color in instructions:
        face.draw(result, position, text, fill=color)
    output = io.BytesIO()
    result.save(output, format="PNG")
    return output.getvalue()


@lru_cache(maxsize=512)
def _sheet_thumbnail(path: str, modified_ns: int, changed_ns: int, size: int,
                     show_gif_badge: bool) -> tuple[Image.Image, bool]:
    # 缓存仅保存小图；文件元数据变化后重新生成，不保留原图或文件句柄。
    with Image.open(path) as image:
        if image.format == "JPEG":
            # 在解码前缩小，允许 Pillow 使用 JPEG 解码器降采样。
            # 旋转 90° 的 EXIF 方向需交换缩放边界，再校正方向。
            rotated = image.getexif().get(274) in (5, 6, 7, 8)
            image.thumbnail((150, 180) if rotated else (180, 150))
            thumb = ImageOps.exif_transpose(image).convert("RGBA")
        else:
            thumb = ImageOps.exif_transpose(image)
            if thumb.mode in ("RGB", "L", "P") and "transparency" not in thumb.info:
                # 不透明图无需对全尺寸图像做 alpha 预乘和四通道缩放。
                if thumb.mode == "P":
                    thumb = thumb.convert("RGB")
                thumb.thumbnail((180, 150))
                thumb = thumb.convert("RGBA")
            else:
                thumb = thumb.convert("RGBA")
                thumb.thumbnail((180, 150))
        animated = False
        if show_gif_badge:
            # 只探测第二帧，避免为显示标记扫描整段 GIF。
            try:
                image.seek(1)
            except EOFError:
                pass
            else:
                animated = True
    return thumb, animated


def contact_sheet(items: list[tuple[Path | None, str]], title: str, font_path: str = "",
                  aliases: list[str] | None = None, *, font_dir: str = "", font_order=(),
                  counts: list[int] | None = None, show_gif_badge: bool = True) -> bytes:
    font = load_fonts(font_dir, 20, font_order, font_path)
    small_font = load_fonts(font_dir, 16, font_order, font_path)
    columns = min(5, max(1, len(items)))
    rows = math.ceil(len(items) / columns)
    width = columns * 200
    header = [title]
    if aliases is not None:
        header.append("别名：" + ("、".join(aliases) if aliases else "暂无"))
    lines = []
    for text in header:
        lines.extend(font.wrap(text, width - 24))
    header_height = max(60, len(lines) * 30 + 24)
    # Keep the complete gallery in one image, while avoiding unbounded allocation.
    if width * (rows * 200 + header_height) > MAX_PIXELS:
        raise ValueError("图库过大，缩略图超出单张图片尺寸上限，请按 ID 查看。")
    sheet = Image.new("RGB", (width, max(100, rows * 200 + header_height)), "#f5f5f5")
    draw = ImageDraw.Draw(sheet)
    for index, line in enumerate(lines):
        font.draw(sheet, (12, 12 + index * 30), line, fill="#222222")
    for index, (path, label) in enumerate(items):
        x, y = index % columns * 200, index // columns * 200 + header_height
        if path is None:
            draw.rounded_rectangle((x + 10, y, x + 190, y + 150), radius=12, fill="#e5e7eb")
            font.draw(sheet, (x + 60, y + 60), "空图库", fill="#666666")
        else:
            path = path.resolve()
            stat = path.stat()
            thumb, animated = _sheet_thumbnail(
                str(path), stat.st_mtime_ns, stat.st_ctime_ns, stat.st_size, show_gif_badge,
            )
            sheet.paste(thumb, (x + (200 - thumb.width) // 2, y), thumb)
            if animated:
                draw.rounded_rectangle((x + 140, y + 6, x + 190, y + 32),
                                       radius=6, fill="#262626")
                small_font.draw(sheet, (x + 150, y + 9), "GIF", fill="#ffffff")
        suffix = f"({counts[index]})" if counts is not None else ""
        label = font.ellipsize(label, max(0, int(184 - font.length(suffix)))) + suffix
        font.draw(sheet, (x + 8, y + 158), label, fill="#222222")
    output = io.BytesIO()
    sheet.save(output, format="PNG")
    return output.getvalue()
