import asyncio
import random
import time
from dataclasses import dataclass, replace

import aiohttp
from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, StarTools

from .gallery.commands import Command, parse_command
from .gallery.imaging import contact_sheet, help_image, transform
from .gallery.onebot import collect_images, download_image, image_segment, segments, send
from .gallery.storage import GalleryStore


@dataclass
class Pending:
    command: Command
    message_id: str
    expires: float


class MemeGallery(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self.store = GalleryStore(StarTools.get_data_dir("astrbot_plugin_meme_gallery"))
        self.pending: dict[tuple[str, str, str], Pending] = {}
        self.session: aiohttp.ClientSession | None = None
        self.work_lock = asyncio.Lock()

    def number_config(self, key: str, default: int, minimum: int = 1) -> int:
        try:
            return max(minimum, int(self.config.get(key, default)))
        except (TypeError, ValueError):
            return default

    async def http(self):
        if self.session is None or self.session.closed:
            self.session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=60),
                connector=aiohttp.TCPConnector(limit=4),
            )
        return self.session

    async def reply(self, event, text: str, message_id: str):
        chain = []
        if message_id:
            chain.append({"type": "reply", "data": {"id": message_id}})
        chain.append({"type": "text", "data": {"text": text}})
        await send(event.bot, event, chain)

    async def send_images(self, event, images: list[bytes], preview: bool = False):
        forward = len(images) > self.number_config("forward_threshold", 5, 0) or (
            len(images) > 1 and self.config.get("multi_image_mode", "direct") == "forward"
        )
        await send(event.bot, event, [
            image_segment(data, bool(self.config.get("send_as_meme", True)) and not preview)
            for data in images
        ], forward=forward and not preview)

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def on_message(self, event: AstrMessageEvent):
        if event.get_platform_name() != "aiocqhttp":
            return
        raw = event.message_obj.raw_message
        chain = segments(raw.get("message", []))
        message_id = str(raw.get("message_id", event.message_obj.message_id))
        key = (str(event.get_platform_id()), str(event.unified_msg_origin), str(event.get_sender_id()))
        now = time.monotonic()
        self.pending = {k: v for k, v in self.pending.items() if v.expires > now}
        try:
            # Match AstrBot's CommandFilter gate and consume its normalized text.
            # is_wake alone is insufficient: ALL-message listeners set it themselves.
            command = (
                parse_command(event.get_message_str())
                if event.is_at_or_wake_command else None
            )
            if command is None:
                pending = self.pending.get(key)
                if not pending or not any(s.get("type") in ("image", "reply", "forward", "node") for s in chain):
                    return
                images = await collect_images(event.bot, chain, pending.command.action == "upload")
                if not images:
                    return
                if self.pending.get(key) is not pending:
                    return
                self.pending.pop(key, None)
                command, message_id = pending.command, pending.message_id
            else:
                self.pending.pop(key, None)
                images = []
                if command.action == "upload":
                    gallery_id, canonical = await asyncio.to_thread(self.store.gallery_identity, command.target)
                    command = replace(command, target=canonical, gallery_id=gallery_id)
                if command.action in ("upload", "flip", "mirror", "speed", "reverse"):
                    images = await collect_images(event.bot, chain, command.action == "upload")
                    if not images:
                        timeout = self.number_config("wait_timeout", 120)
                        self.pending[key] = Pending(command, message_id, now + timeout)
                        event.stop_event()
                        await self.reply(event, f"请在 {timeout} 秒内发送图片。", message_id)
                        return
            event.stop_event()
            # Bound decoding/rendering memory across concurrent commands.
            async with self.work_lock:
                await self.execute(event, command, images, message_id)
        except ValueError as exc:
            event.stop_event()
            await self.reply(event, str(exc), message_id)
        except Exception:
            logger.exception("表情包图库处理失败")
            event.stop_event()
            await self.reply(event, "处理失败，请检查协议端连接、网络或后台日志。", message_id)

    async def execute(self, event, command: Command, images: list[dict], message_id: str):
        if command.action == "help":
            data = await asyncio.to_thread(
                help_image, self.config.get("font_path", ""),
                self.number_config("wait_timeout", 120),
                self.number_config("forward_threshold", 5, 0),
                self.config.get("multi_image_mode", "direct"),
                font_dir=self.config.get("font_dir", ""),
                font_order=self.config.get("font_order", []),
            )
            await self.send_images(event, [data], preview=True)
        elif command.action == "create":
            created = await asyncio.to_thread(self.store.create, command.target)
            text = f"已创建图库“{command.target}”。" if created else f"图库“{command.target}”已存在。"
            await self.reply(event, text, message_id)
        elif command.action == "rename":
            old_name = await asyncio.to_thread(self.store.rename, command.target, command.option)
            await self.reply(event, f"已将图库“{old_name}”重命名为“{command.option}”。", message_id)
        elif command.action == "alias":
            canonical = await asyncio.to_thread(self.store.require_gallery, command.target)
            if command.option:
                operation = self.store.add_alias if command.option == "添加" else self.store.remove_alias
                results = []
                for alias in command.aliases:
                    try:
                        await asyncio.to_thread(operation, canonical, alias)
                    except ValueError as exc:
                        results.append(str(exc))
                    else:
                        results.append(f"已为图库“{canonical}”{command.option}别名“{alias}”。")
                text = "\n".join(results)
            else:
                aliases = await asyncio.to_thread(self.store.aliases, canonical)
                text = f"图库“{canonical}”的别名：" + ("、".join(aliases) if aliases else "暂无别名。")
            await self.reply(event, text, message_id)
        elif command.action == "delete":
            deleted, errors = await asyncio.to_thread(self.store.delete, command.ids)
            text = f"已删除 {len(deleted)}/{len(command.ids)} 张图片。"
            if deleted:
                text += "\n图片 ID：" + "、".join(map(str, deleted))
            if errors:
                text += "\n" + "\n".join(errors)
            await self.reply(event, text, message_id)
        elif command.action in ("list", "sheet"):
            title, aliases = "图库列表", None
            if command.action == "sheet":
                title = await asyncio.to_thread(self.store.require_gallery, command.target)
                aliases = await asyncio.to_thread(self.store.aliases, title)
            rows = await asyncio.to_thread(
                self.store.covers if command.action == "list" else self.store.entries,
                *(() if command.action == "list" else (command.target,)),
            )
            if not rows and command.action == "list":
                raise ValueError("暂无图库，请先使用“创建图库名”创建图库。")
            items = [(self.store.path(row) if row["filename"] else None,
                      row["gallery"] if command.action == "list" else f"ID {row['id']}")
                     for row in rows]
            if not items:
                items = [(None, "空图库")]
            data = await asyncio.to_thread(
                contact_sheet, items, title,
                self.config.get("font_path", ""), aliases,
                font_dir=self.config.get("font_dir", ""),
                font_order=self.config.get("font_order", []),
            )
            await self.send_images(event, [data], preview=True)
        elif command.action == "view":
            if command.ids:
                rows = await asyncio.to_thread(self.store.by_ids, command.ids)
            else:
                rows = await asyncio.to_thread(self.store.entries, command.target)
                if not rows:
                    raise ValueError("图库为空，请先上传图片。")
                rows = random.sample(rows, min(command.count, len(rows)))
            data = [await asyncio.to_thread(self.store.path(row).read_bytes) for row in rows]
            await self.send_images(event, data)
        elif command.action == "upload":
            if command.gallery_id is not None:
                canonical = await asyncio.to_thread(self.store.name_by_id, command.gallery_id)
                command = replace(command, target=canonical)
            await asyncio.to_thread(self.store.require_gallery, command.target)
            success, errors, ids = 0, [], []
            for index, image in enumerate(images, 1):
                try:
                    data = await download_image(event.bot, await self.http(), image,
                                                self.number_config("max_download_mb", 20) * 1024 * 1024)
                    image_id, added = await asyncio.to_thread(self.store.add, command.target, data)
                    if added:
                        success += 1
                        ids.append(str(image_id))
                    else:
                        errors.append(f"第 {index} 张：图片已存在（ID {image_id}）。")
                except ValueError as exc:
                    errors.append(f"第 {index} 张：{exc}")
                except Exception:
                    logger.exception("图库图片上传失败")
                    errors.append(f"第 {index} 张：下载或保存失败。")
            text = f"已将 {success}/{len(images)} 张图片上传到{command.target}"
            if ids:
                text += "\n图片 ID：" + "、".join(ids)
            if errors:
                text += "\n" + "\n".join(errors)
            await self.reply(event, text, message_id)
        else:
            if len(images) != 1:
                raise ValueError("图片处理命令一次仅支持单张图片。")
            data = await download_image(event.bot, await self.http(), images[0],
                                        self.number_config("max_download_mb", 20) * 1024 * 1024)
            result = await asyncio.to_thread(transform, data, command.action, command.option)
            await self.send_images(event, [result])

    async def terminate(self):
        self.pending.clear()
        if self.session and not self.session.closed:
            await self.session.close()
