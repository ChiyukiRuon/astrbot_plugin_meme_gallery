import io
import logging
import math
import unicodedata
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

import regex
from bidi import algorithm as bidi
from fontTools.ttLib import TTFont
from PIL import Image, ImageDraw, ImageFont, features

logger = logging.getLogger(__name__)
SYSTEM_FONTS = (
    'C:/Windows/Fonts/msyh.ttc',
    '/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc',
    '/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc',
    'C:/Windows/Fonts/seguiemj.ttf',
    '/usr/share/fonts/truetype/noto/NotoColorEmoji.ttf',
)
EXTENSIONS = {'.ttf', '.otf', '.ttc', '.otc'}


def graphemes(text: str) -> list[str]:
    return regex.findall(r'\X', text)


def font_files(directory: str, order: list[str] | tuple[str, ...] = (),
               legacy_path: str = '') -> tuple[tuple[str, int, int], ...]:
    if not isinstance(order, (list, tuple)) or any(not isinstance(n, str) for n in order):
        raise ValueError('font_order 必须是按优先级排列的字体文件名列表。')
    if directory:
        root = Path(directory).expanduser()
        if not root.is_dir():
            raise ValueError('字体文件夹不存在，请检查 font_dir；Docker 请填写容器内路径。')
        files = sorted((p for p in root.iterdir() if p.is_file() and p.suffix.lower() in EXTENSIONS),
                       key=lambda p: (p.name.casefold(), p.name))
        by_name = {p.name: p for p in files}
        missing = [name for name in order if name not in by_name]
        if missing:
            raise ValueError('字体排序中的文件不存在：' + '、'.join(missing))
        files = list(dict.fromkeys([by_name[name] for name in order] + files))
    else:
        files = [Path(p) for p in (legacy_path, *SYSTEM_FONTS) if p and Path(p).is_file()]
    if not files:
        raise ValueError('未找到字体，请在 font_dir 指定包含字体文件的文件夹。')
    try:
        return tuple((str(p.resolve()), p.stat().st_mtime_ns, p.stat().st_size) for p in files)
    except OSError as exc:
        raise ValueError('无法读取字体文件，请检查文件权限。') from exc


@lru_cache(maxsize=128)
def font_info(signature: tuple[str, int, int]):
    with open(signature[0], 'rb') as source, TTFont(source, fontNumber=0, lazy=True) as font:
        coverage = frozenset(code for code, glyph in (font.getBestCmap() or {}).items()
                             if glyph != '.notdef')
        strikes = []
        if 'CBLC' in font:
            strikes = [strike.bitmapSizeTable.ppemY for strike in font['CBLC'].strikes]
        elif 'EBLC' in font:
            strikes = [strike.bitmapSizeTable.ppemY for strike in font['EBLC'].strikes]
        elif 'sbix' in font:
            strikes = list(font['sbix'].strikes)
        return coverage, tuple(strikes)


@dataclass
class Face:
    font: ImageFont.FreeTypeFont
    coverage: frozenset[int]
    scale: float
    path: str


class FontStack:
    def __init__(self, signatures: tuple, size: int):
        self.size = size
        self.faces = []
        self.raqm = features.check_feature('raqm')
        for signature in signatures:
            try:
                coverage, strikes = font_info(signature)
                native_size = size
                data = Path(signature[0]).read_bytes()
                try:
                    font = ImageFont.truetype(io.BytesIO(data), size)
                except OSError:
                    if not strikes:
                        raise
                    native_size = min(strikes, key=lambda n: abs(n - size))
                    font = ImageFont.truetype(io.BytesIO(data), native_size)
                self.faces.append(Face(font, coverage, size / native_size, signature[0]))
            except Exception as exc:
                logger.warning('跳过无法加载的字体 %s：%s', signature[0], exc)
        if not self.faces:
            raise ValueError('文件夹中没有可加载的字体，请检查字体格式和文件完整性。')
        self.select = lru_cache(maxsize=4096)(self._select)
        self.layout = lru_cache(maxsize=512)(self._layout)
        self.unrenderable_faces = set()

    def _select(self, cluster: str) -> int:
        required = {ord(c) for c in cluster if unicodedata.category(c) != 'Cf'
                    and not 0xFE00 <= ord(c) <= 0xFE0F
                    and not 0xE0100 <= ord(c) <= 0xE01EF}
        blank = False
        needs_ink = any(not chr(code).isspace() for code in required)
        for index, face in enumerate(self.faces):
            if required <= face.coverage:
                if not needs_ink:
                    return index
                try:
                    if face.font.getmask(cluster, mode='RGBA').getbbox() is not None:
                        return index
                except (OSError, ValueError):
                    pass
                blank = True
                if index not in self.unrenderable_faces:
                    logger.warning(
                        '字体声明包含字符但无法绘制，已尝试后续字体：%s（%s）。'
                        '若为 COLRv1 或 SVG 彩色字体，请换用当前 Pillow 支持的版本，'
                        '例如官方 CBDT 版 NotoColorEmoji.ttf。',
                        face.path, ' '.join(f'U+{code:04X}' for code in sorted(required)),
                    )
                    self.unrenderable_faces.add(index)
        return -1 if blank else 0

    def _layout(self, text: str):
        clusters = graphemes(text)
        visual = [(i, 0) for i in range(len(clusters))]
        if any(unicodedata.bidirectional(c) in ('R', 'AL', 'RLE', 'RLO', 'RLI') for c in text):
            if not self.raqm:
                raise ValueError('当前 Pillow 缺少 libraqm，无法正确排版从右到左的文字，请安装支持 libraqm 的 Pillow。')
            storage = bidi.get_empty_storage()
            storage['base_level'] = bidi.get_base_level(text)
            storage['base_dir'] = 'R' if storage['base_level'] else 'L'
            bidi.get_embedding_levels(text, storage)
            indices = [i for i, cluster in enumerate(clusters) for _ in cluster]
            for char, index in zip(storage['chars'], indices):
                char['cluster'] = index
            try:
                bidi.explicit_embed_and_overrides(storage)
                bidi.resolve_weak_types(storage)
                bidi.resolve_neutral_types(storage, False)
                bidi.resolve_implicit_levels(storage, False)
                bidi.reorder_resolved_levels(storage, False)
            except (AssertionError, KeyError) as exc:
                raise ValueError('文字包含暂不支持的双向控制字符，请移除这些控制字符后重试。') from exc
            visual, seen = [], set()
            for char in storage['chars']:
                index = char['cluster']
                if index not in seen:
                    visual.append((index, char['level']))
                    seen.add(index)
        groups = []
        for index, level in visual:
            face = self.select(clusters[index])
            if groups and groups[-1][0:2] == (face, level):
                groups[-1][2].append(clusters[index])
            else:
                groups.append((face, level, [clusters[index]]))
        runs = []
        for index, level, parts in groups:
            value = ''.join(reversed(parts) if level % 2 else parts)
            if index == -1:
                runs.append((index, value, None, self.size * len(parts)))
                continue
            face = self.faces[index]
            direction = ('rtl' if level % 2 else 'ltr') if self.raqm else None
            width = face.font.getlength(value, direction=direction) * face.scale
            runs.append((index, value, direction, width))
        return tuple(runs)

    def length(self, text: str) -> float:
        return sum(run[3] for run in self.layout(text))

    def draw(self, image: Image.Image, xy: tuple, text: str, fill: str):
        x, y = xy
        baseline = y + self.size
        for index, value, direction, width in self.layout(text):
            draw = ImageDraw.Draw(image)
            if index == -1:
                for offset, _ in enumerate(graphemes(value)):
                    left = x + offset * self.size
                    draw.rectangle((left + 2, y + 2, left + self.size - 2, baseline - 2),
                                   outline=fill, width=1)
                x += width
                continue
            face = self.faces[index]
            if face.scale == 1:
                draw.text((x, baseline), value, font=face.font, anchor='ls', fill=fill,
                          direction=direction, embedded_color=True)
            else:
                box = draw.textbbox((0, 0), value, font=face.font, anchor='ls',
                                    direction=direction, embedded_color=True)
                left, top, right, bottom = box
                if right > left and bottom > top:
                    tile = Image.new('RGBA', (right - left, bottom - top))
                    ImageDraw.Draw(tile).text((-left, -top), value, font=face.font,
                                             anchor='ls', fill=fill, direction=direction,
                                             embedded_color=True)
                    tile = tile.resize((max(1, math.ceil(tile.width * face.scale)),
                                        max(1, math.ceil(tile.height * face.scale))),
                                       Image.Resampling.LANCZOS)
                    image.paste(tile, (round(x + left * face.scale),
                                       round(baseline + top * face.scale)), tile)
            x += width

    def wrap(self, text: str, width: int) -> list[str]:
        lines, line = [], ''
        for cluster in graphemes(text):
            if line and self.length(line + cluster) > width:
                lines.append(line)
                line = ''
            line += cluster
        return lines + [line]

    def ellipsize(self, text: str, width: int) -> str:
        if self.length(text) <= width:
            return text
        parts = graphemes(text)
        while parts and self.length(''.join(parts) + '…') > width:
            parts.pop()
        return ''.join(parts) + '…' if self.length('…') <= width else ''


@lru_cache(maxsize=12)
def cached_stack(signatures: tuple, size: int) -> FontStack:
    return FontStack(signatures, size)


def load_fonts(directory: str = '', size: int = 20, order=(), legacy_path: str = '') -> FontStack:
    return cached_stack(font_files(directory, order, legacy_path), size)
