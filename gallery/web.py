"""AstrBot 插件可视化视图的管理接口。"""

import asyncio
import base64
import io
import logging
from functools import wraps

from PIL import Image, ImageOps
from astrbot.api.web import PluginUploadFile, error_response, json_response, request

from .commands import gallery_name


def positive_id(value) -> int:
    if isinstance(value, bool) or not str(value).isascii() or not str(value).isdecimal():
        raise ValueError("ID 和页码须为正整数。")
    result = int(value)
    if not 1 <= result <= 2**63 - 1:
        raise ValueError("ID 或页码超出有效范围。")
    return result


def text_value(value) -> str:
    if not isinstance(value, str):
        raise ValueError("名称或查询条件须为文字。")
    if len(value) > 80:
        raise ValueError("名称或查询条件最多 80 个字符。")
    return value.strip()


def endpoint(handler):
    @wraps(handler)
    async def guarded(self, *args, **kwargs):
        if self.closed:
            return error_response("插件已停用，请刷新页面。", status_code=503)
        try:
            return json_response(await handler(self, *args, **kwargs),
                                 headers={"Cache-Control": "no-store"})
        except ValueError as exc:
            return error_response(str(exc))
        except Exception:
            logging.getLogger(__name__).exception("图库管理操作失败")
            return error_response("操作失败，请检查插件日志后重试。", status_code=500)
    return guarded


class WebAdmin:
    def __init__(self, store, work_lock, config):
        self.store = store
        self.work_lock = work_lock
        self.config = config
        self.closed = False

    def register(self, context):
        for route, handler, method in (
            ("galleries", self.galleries, "GET"),
            ("galleries/save", self.save_gallery, "POST"),
            ("galleries/delete", self.delete_gallery, "POST"),
            ("images", self.images, "GET"),
            ("images/<image_id>/preview", self.preview, "GET"),
            ("images/<image_id>/original", self.original, "GET"),
            ("images/<image_id>/replace", self.replace, "POST"),
            ("images/move", self.move, "POST"),
            ("images/delete", self.delete_images, "POST"),
            ("galleries/<gallery_id>/import", self.import_image, "POST"),
        ):
            context.register_web_api(f"/astrbot_plugin_meme_gallery/{route}", handler,
                                     [method], "表情包图库管理")

    async def run(self, function, *args):
        # 与聊天命令共用解码限流；取消请求时等待线程结束再释放锁。
        async with self.work_lock:
            task = asyncio.create_task(asyncio.to_thread(function, *args))
            try:
                return await asyncio.shield(task)
            except asyncio.CancelledError:
                await task
                raise

    async def payload(self):
        data = await request.json(default={})
        if not isinstance(data, dict):
            raise ValueError("请求内容须为 JSON 对象。")
        return data

    @endpoint
    async def galleries(self):
        query = text_value(request.query.get("q", ""))
        return {"items": await self.run(self.store.catalog, query),
                "max_upload_bytes": self.upload_limit()}

    @endpoint
    async def save_gallery(self):
        data = await self.payload()
        action = data.get("action", "create")
        name = gallery_name(text_value(data.get("name")))
        if action == "create":
            return {"created": await self.run(self.store.create, name)}
        gallery_id = positive_id(data.get("id"))

        def update():
            with self.store.lock:
                canonical = self.store.name_by_id(gallery_id)
                if action == "rename":
                    self.store.rename(canonical, name)
                elif action == "add_alias":
                    self.store.add_alias(canonical, name)
                elif action == "remove_alias":
                    self.store.remove_alias(canonical, name)
                else:
                    raise ValueError("不支持的图库操作。")
        await self.run(update)
        return {"saved": True}

    @endpoint
    async def delete_gallery(self):
        data = await self.payload()
        await self.run(self.store.delete_gallery, positive_id(data.get("id")))
        return {"deleted": True}

    @endpoint
    async def images(self):
        gallery = request.query.get("gallery_id")
        return await self.run(self.store.image_page,
                              positive_id(gallery) if gallery else None,
                              text_value(request.query.get("q", "")),
                              positive_id(request.query.get("page", "1")))

    @endpoint
    async def preview(self, image_id):
        image_id = positive_id(image_id)

        def render():
            with self.store.lock:
                row = self.store.by_ids((image_id,))[0]
                with Image.open(self.store.path(row)) as source:
                    animated = getattr(source, "n_frames", 1) > 1
                    image = ImageOps.exif_transpose(source)
                    image.thumbnail((320, 320))
                    output = io.BytesIO()
                    image.convert("RGBA").save(output, format="PNG")
                return {"src": "data:image/png;base64," + base64.b64encode(output.getvalue()).decode("ascii"),
                        "animated": animated}
        return await self.run(render)

    @endpoint
    async def original(self, image_id):
        image_id = positive_id(image_id)

        def read_original():
            with self.store.lock:
                row = self.store.by_ids((image_id,))[0]
                data = self.store.path(row).read_bytes()
                with Image.open(io.BytesIO(data)) as source:
                    mime = Image.MIME.get(source.format, "application/octet-stream")
                    width, height = source.size
                    animated = getattr(source, "n_frames", 1) > 1
                return {"src": f"data:{mime};base64," + base64.b64encode(data).decode("ascii"),
                        "width": width, "height": height, "animated": animated}
        return await self.run(read_original)

    def upload_limit(self):
        try:
            return max(1, int(self.config.get("max_download_mb", 20))) * 1024 * 1024
        except (ValueError, TypeError):
            return 20 * 1024 * 1024

    async def upload_bytes(self):
        files = await request.files()
        upload = files.get("file")
        if not isinstance(upload, PluginUploadFile):
            raise ValueError("请选择要上传的图片。")
        limit = self.upload_limit()
        try:
            data = bytearray()
            while len(data) <= limit:
                chunk = await upload.read(min(65536, limit + 1 - len(data)))
                if not chunk:
                    break
                data.extend(chunk)
            if len(data) > limit:
                raise ValueError(f"单张图片不得超过 {limit // (1024 * 1024)} MB。")
            if not data:
                raise ValueError("不能上传空文件。")
            return bytes(data)
        finally:
            await upload.close()

    @endpoint
    async def import_image(self, gallery_id):
        gallery_id = positive_id(gallery_id)
        # 上传读取也纳入共用工作锁，避免并发请求积攒完整图片。
        async with self.work_lock:
            data = await self.upload_bytes()
            task = asyncio.create_task(asyncio.to_thread(self.store.add_by_gallery_id, gallery_id, data))
            try:
                image_id, added = await asyncio.shield(task)
            except asyncio.CancelledError:
                await task
                raise
        return {"id": image_id, "added": added}

    @endpoint
    async def replace(self, image_id):
        image_id = positive_id(image_id)
        async with self.work_lock:
            data = await self.upload_bytes()
            task = asyncio.create_task(asyncio.to_thread(self.store.replace_image, image_id, data))
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                await task
                raise
        return {"id": image_id, "saved": True}

    @endpoint
    async def move(self):
        data = await self.payload()
        return await self.run(self.store.move_image, positive_id(data.get("id")),
                              positive_id(data.get("gallery_id")))

    @endpoint
    async def delete_images(self):
        data = await self.payload()
        ids = data.get("ids")
        if not isinstance(ids, list) or not 1 <= len(ids) <= 20:
            raise ValueError("一次请选择 1～20 张图片。")
        deleted, errors = await self.run(self.store.delete, tuple(positive_id(i) for i in ids))
        return {"deleted": deleted, "errors": errors}
