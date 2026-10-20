import math
import re
import shlex
from dataclasses import dataclass


@dataclass(frozen=True)
class Command:
    action: str
    target: str = ""
    count: int = 1
    ids: tuple[int, ...] = ()
    option: str = ""
    alias: str = ""
    gallery_id: int | None = None


def gallery_name(value: str) -> str:
    value = value.strip()
    if not value or len(value) > 80 or any(ord(c) < 32 for c in value):
        raise ValueError("图库名须为 1～80 个可见字符。")
    if value.isdecimal():
        raise ValueError("图库名不能是纯数字，以免与图片 ID 冲突。")
    if re.search(r"(?:[x*]\d+|\s+\d+)$", value):
        raise ValueError("图库名不能以 x数字、*数字或空格加数字结尾。")
    return value


def parse_command(text: str) -> Command | None:
    text = text.strip()
    if text == "图库帮助":
        return Command("help")
    if re.match(r"^图库重命名(?:\s|$)", text):
        try:
            args = shlex.split(text[len("图库重命名"):].strip())
        except ValueError:
            raise ValueError("引号未闭合；用法：图库重命名 图库名 图库新名。") from None
        if len(args) != 2:
            raise ValueError("用法：图库重命名 图库名 图库新名；含空格的名称请用双引号包围。")
        return Command("rename", gallery_name(args[0]), option=gallery_name(args[1]))
    if text == "图库别名" or text.startswith("图库别名 "):
        try:
            args = shlex.split(text[len("图库别名"):].strip())
        except ValueError:
            raise ValueError("引号未闭合；用法：图库别名 图库名 [添加/删除 别名]。") from None
        if len(args) == 1:
            return Command("alias", gallery_name(args[0]))
        if len(args) == 3 and args[1] in ("添加", "删除"):
            return Command("alias", gallery_name(args[0]), option=args[1],
                           alias=gallery_name(args[2]))
        raise ValueError("用法：图库别名 图库名 [添加/删除 别名]；含空格的名称请用双引号包围。")
    if text == "删除" or text.startswith("删除 "):
        rest = text[2:].strip()
        if not re.fullmatch(r"\d+(?:\s+\d+)*", rest):
            raise ValueError("用法：删除 图片ID1 图片ID2；ID 须为正整数。")
        ids = tuple(dict.fromkeys(int(value) for value in rest.split()))
        if any(value < 1 or value > 2**63 - 1 for value in ids):
            raise ValueError("图片 ID 须为有效的正整数。")
        return Command("delete", ids=ids)
    if text.startswith("创建"):
        return Command("create", gallery_name(text[2:]))
    if text in ("图库列表", "看所有"):
        return Command("list")
    if text.startswith("看所有"):
        return Command("sheet", gallery_name(text[3:]))
    for prefix in ("上传", "添加"):
        if text.startswith(prefix):
            return Command("upload", gallery_name(text[len(prefix) :]))
    if text.startswith("看"):
        rest = text[1:].strip()
        if not rest:
            raise ValueError("用法：看图库名、看图库名x数量，或 看 ID1 ID2。")
        if re.fullmatch(r"\d+(?:\s+\d+)*", rest):
            ids = tuple(int(x) for x in rest.split())
            if len(ids) > 20 or any(x < 1 for x in ids):
                raise ValueError("一次可查看 1～20 个正整数图片 ID。")
            return Command("view", ids=ids)
        match = re.fullmatch(r"(.+?)(?:[x*]|\s+)(\d+)", rest)
        name, count = (match[1], int(match[2])) if match else (rest, 1)
        if not 1 <= count <= 20:
            raise ValueError("一次最多发送 20 张图片，数量须大于 0。")
        return Command("view", gallery_name(name), count=count)
    match = re.fullmatch(
        r"(翻转|flip|镜像|mirror|加速|speed|倒转|reverse)(?:\s+(.*))?", text,
        flags=re.IGNORECASE,
    )
    if not match:
        return None
    verb, arg = match[1].lower(), (match[2] or "").strip().lower()
    if verb in ("翻转", "flip"):
        options = {"": "horizontal", "水平": "horizontal", "垂直": "vertical",
                   "horizontal": "horizontal", "vertical": "vertical"}
        if arg not in options:
            raise ValueError("翻转方向须为 水平/垂直 或 horizontal/vertical。")
        return Command("flip", option=options[arg])
    if verb in ("镜像", "mirror"):
        options = {"": "left", "左": "left", "右": "right", "上": "top",
                   "下": "bottom", **{x: x for x in ("left", "right", "top", "bottom")}}
        if arg not in options:
            raise ValueError("镜像方向须为 上/左/下/右 或 top/left/bottom/right。")
        return Command("mirror", option=options[arg])
    if verb in ("加速", "speed"):
        try:
            speed = float(arg.removesuffix("x"))
        except ValueError:
            raise ValueError("用法：加速 2x；倍数须为大于 0 的有限数字。") from None
        if not math.isfinite(speed) or speed <= 0:
            raise ValueError("加速倍数须为大于 0 的有限数字。")
        return Command("speed", option=str(speed))
    if arg:
        raise ValueError("倒转命令不接受参数。")
    return Command("reverse")
