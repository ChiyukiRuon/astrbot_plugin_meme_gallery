from __future__ import annotations

import base64
import re
from collections.abc import Mapping
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import aiohttp


def segments(value) -> list[dict]:
    if isinstance(value, list):
        return value
    if not isinstance(value, str):
        return []
    # Some implementations return CQ strings from get_msg/get_forward_msg.
    def unescape(text):
        return (text.replace("&#44;", ",").replace("&#91;", "[")
                .replace("&#93;", "]").replace("&amp;", "&"))
    result = []
    offset = 0
    for match in re.finditer(r"\[CQ:([a-zA-Z_]+)((?:,[^\]]*)?)\]", value):
        if match.start() > offset:
            result.append({"type": "text", "data": {"text": unescape(value[offset:match.start()])}})
        fields = {}
        for pair in match[2].lstrip(",").split(","):
            if "=" in pair:
                key, content = pair.split("=", 1)
                fields[key] = unescape(content)
        result.append({"type": match[1], "data": fields})
        offset = match.end()
    if offset < len(value):
        result.append({"type": "text", "data": {"text": unescape(value[offset:])}})
    return result


def payload(response):
    if isinstance(response, Mapping) and "data" in response and "status" in response:
        return response["data"]
    return response


async def collect_images(bot, message, allow_forward: bool) -> list[dict]:
    found = []

    async def visit(chain, allow_reply: bool, expand_forward: bool):
        for segment in segments(chain):
            kind, data = segment.get("type"), segment.get("data", {})
            if kind == "image":
                found.append(data)
            elif kind == "reply" and allow_reply:
                response = payload(await bot.call_action("get_msg", message_id=int(data["id"])))
                await visit(response.get("message", []), False, expand_forward)
            elif kind == "forward" and expand_forward:
                response = payload(await bot.call_action("get_forward_msg", id=str(data["id"])))
                for node in response.get("messages", []):
                    content = node.get("content", node.get("message", []))
                    if node.get("type") == "node":
                        content = node.get("data", {}).get("content", [])
                    await visit(content, False, False)
            elif kind == "node" and expand_forward:
                await visit(data.get("content", []), False, False)

    await visit(message, True, allow_forward)
    return found


async def download_image(bot, session: aiohttp.ClientSession, image: dict, limit: int) -> bytes:
    source = image.get("url") or image.get("file", "")
    if source.startswith("base64://"):
        if len(source) > limit * 4 // 3 + 32:
            raise ValueError("图片超过下载体积上限。")
        try:
            result = base64.b64decode(source[9:], validate=True)
        except ValueError:
            raise ValueError("图片 Base64 数据无效。") from None
        if len(result) > limit:
            raise ValueError("图片超过下载体积上限。")
        return result
    if not source.startswith(("http://", "https://")):
        response = payload(await bot.call_action("get_image", file=image.get("file", "")))
        source = response.get("url", "")
    if not source.startswith(("http://", "https://")):
        raise ValueError("协议端未提供可下载的图片 URL。")
    async with session.get(source) as response:
        response.raise_for_status()
        if response.content_length and response.content_length > limit:
            raise ValueError("图片超过下载体积上限。")
        result = bytearray()
        async for chunk in response.content.iter_chunked(65536):
            result.extend(chunk)
            if len(result) > limit:
                raise ValueError("图片超过下载体积上限。")
        return bytes(result)


def image_segment(data: bytes, as_meme: bool) -> dict:
    return {"type": "image", "data": {
        "file": "base64://" + base64.b64encode(data).decode("ascii"),
        "sub_type": 1 if as_meme else 0,
    }}


async def send(bot, event, message: list[dict], forward: bool = False):
    group = event.get_group_id()
    target = {"group_id": int(group)} if group else {"user_id": int(event.get_sender_id())}
    prefix = "send_group" if group else "send_private"
    if forward:
        nodes = [{"type": "node", "data": {
            "name": "表情包图库", "uin": str(event.get_self_id()), "content": [image],
        }} for image in message]
        await bot.call_action(prefix + "_forward_msg", messages=nodes, **target)
    else:
        await bot.call_action(prefix + "_msg", message=message, **target)
