import hashlib
import io
import math
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
               font_dir: str = "", font_order=()) -> bytes:
    font = load_fonts(font_dir, 24, font_order, font_path)
    heading = load_fonts(font_dir, 34, font_order, font_path)
    sections = [
        ("图库管理", [
            "图库帮助  →  查看本帮助图片",
            "创建猫猫 / 创建 猫猫  →  创建图库，之后才能上传",
            "图库重命名 猫猫 猫咪  →  修改图库名，保留图片和别名",
            "图库列表 / 看所有  →  查看全部图库和封面",
            "上传猫猫 / 添加猫猫  →  上传图片到已创建的图库",
            "图库别名 猫猫  →  显示该图库的全部别名",
            "图库别名 猫猫 添加 小猫 猫咪 / 图库别名 猫猫 删除 小猫 猫咪",
            "图库名和别名全局不能重复；别名可用于查看和上传。",
            "多个别名用空格分隔，重复项合并，逐项反馈结果。",
            "删除 12 34  →  删除指定图片及文件，保留图库",
            "同一图库逐帧 pHash 去重；不同图库允许收录相同图片。",
        ]),
        ("查看图片", [
            "看猫猫  →  随机取一张",
            "看猫猫x3 / 看猫猫*3 / 看 猫猫 3  →  随机取多张",
            "看12 34 / 看 12 34  →  按图片 ID 查看",
            "看所有猫猫  →  查看图库缩略图及图片 ID",
            "一次最多 20 张；随机不重复，数量不足时发送已有图片。",
        ]),
        ("图片处理 · 每次一张，支持静态图和动态图", [
            "翻转 [水平/垂直] / flip [horizontal/vertical]  →  默认水平",
            "镜像 [上/左/下/右] / mirror [top/left/bottom/right]  →  默认左",
            "加速 2x / speed 0.5x  →  动画加速或慢放，倍数须大于 0",
            "倒转 / reverse  →  倒转动画播放顺序",
        ]),
        ("如何提供图片", [
            "支持命令附图、引用图片消息，或先发命令再发图片。",
            f"后续发图限同一会话、同一发送者，等待 {wait_timeout} 秒。",
            "上传还支持引用合并转发，仅读取第一层节点中的图片。",
            "上传结果回复原指令，列出成功数量、图片 ID 和失败原因。",
        ]),
        ("提示", [
            "命令前缀跟随 AstrBot 设置；方括号为可选参数，无需输入。",
            "也可按 AstrBot 的规则通过 @ 机器人或私聊触发命令。",
            "图库名不能为纯数字，不能以 x数字、*数字或空格加数字结尾。",
            f"多图发送：{'合并转发' if multi_image_mode == 'forward' else '一条消息'}；"
            f"超过 {forward_threshold} 张强制合并转发。",
            "发送方式、表情包模式、等待时间、字体文件夹和字体优先级可在插件配置调整。",
        ]),
    ]
    width = max(1000, math.ceil(max(font.length(line)
                                   for _, lines in sections for line in lines)) + 80)
    height = 110 + sum(60 + len(lines) * 40 + 24 for _, lines in sections)
    result = Image.new("RGB", (width, height), "#f5f5f5")
    heading.draw(result, (40, 24), "表情包图库 · 使用帮助", fill="#222222")
    y = 100
    for title, lines in sections:
        heading.draw(result, (40, y), title, fill="#3455a4")
        y += 60
        for line in lines:
            font.draw(result, (40, y), line, fill="#222222")
            y += 40
        y += 24
    output = io.BytesIO()
    result.save(output, format="PNG")
    return output.getvalue()


def contact_sheet(items: list[tuple[Path | None, str]], title: str, font_path: str = "",
                  aliases: list[str] | None = None, *, font_dir: str = "", font_order=()) -> bytes:
    font = load_fonts(font_dir, 20, font_order, font_path)
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
            with Image.open(path) as image:
                thumb = ImageOps.exif_transpose(image).convert("RGBA")
                thumb.thumbnail((180, 150))
                sheet.paste(thumb, (x + (200 - thumb.width) // 2, y), thumb)
        label = font.ellipsize(label, 184)
        font.draw(sheet, (x + 8, y + 158), label, fill="#222222")
    output = io.BytesIO()
    sheet.save(output, format="PNG")
    return output.getvalue()
