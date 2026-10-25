"""管理接口使用 AstrBot 公共 Web API 的轻量替身，真实接口另行验证。"""

import asyncio
import base64
import importlib.util
import io
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from gallery.storage import GalleryStore
from test_gallery import animation, encoded, picture


class Upload:
    def __init__(self, data):
        self.stream = io.BytesIO(data)
        self.closed = False

    async def read(self, size):
        return self.stream.read(size)

    async def close(self):
        self.closed = True


api = types.ModuleType("astrbot.api.web")
api.PluginUploadFile = Upload
api.request = None
api.json_response = lambda data, **kwargs: {"code": 200, "data": data}
api.error_response = lambda message, status_code=400: {"code": status_code, "message": message}
spec = importlib.util.spec_from_file_location("gallery.web_test", Path(__file__).parents[1] / "gallery/web.py")
web = importlib.util.module_from_spec(spec)
with patch.dict(sys.modules, {"astrbot.api.web": api}):
    spec.loader.exec_module(web)


class WebTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = GalleryStore(Path(self.temp.name))
        self.store.create("猫猫")
        self.gallery_id = self.store.gallery_identity("猫猫")[0]
        self.admin = web.WebAdmin(self.store, asyncio.Lock(), {})

    async def call(self, method, payload=None, query=None, upload=None, **kwargs):
        req = types.SimpleNamespace(query=query or {}, json=AsyncMock(return_value=payload),
                                    files=AsyncMock(return_value={"file": upload}))
        with patch.object(web, "request", req):
            return await getattr(self.admin, method)(**kwargs)

    async def test_registration_and_disabled_handler(self):
        routes = []
        self.admin.register(types.SimpleNamespace(register_web_api=lambda *args: routes.append(args)))
        self.assertEqual(len(routes), 10)
        self.assertTrue(all(route[0].startswith("/astrbot_plugin_meme_gallery/") for route in routes))
        self.admin.closed = True
        self.assertEqual((await self.call("galleries"))["code"], 503)

    async def test_plugin_initialization_and_termination(self):
        from test_plugin import plugin

        routes = []
        context = types.SimpleNamespace(register_web_api=lambda *args: routes.append(args))
        with patch.object(plugin.StarTools, "get_data_dir", return_value=Path(self.temp.name)):
            instance = plugin.MemeGallery(context, {})
        with patch.dict(sys.modules, {"plugin_under_test.gallery.web": web}):
            await instance.initialize()
        self.assertEqual(len(routes), 10)
        self.assertIs(instance.web_admin.work_lock, instance.work_lock)
        await instance.terminate()
        self.assertTrue(instance.web_admin.closed)

    async def test_gallery_names_alias_search_and_stable_identity(self):
        for name in (None, "", "123", "猫x2", 123, ["猫"]):
            result = await self.call("save_gallery", {"name": name})
            self.assertEqual(result["code"], 400)
        result = await self.call("save_gallery", {"action": "add_alias", "id": self.gallery_id, "name": "小猫"})
        self.assertEqual(result["code"], 200)
        result = await self.call("galleries", query={"q": "小猫"})
        self.assertEqual(result["data"]["items"][0]["id"], self.gallery_id)
        await self.call("save_gallery", {"action": "rename", "id": self.gallery_id, "name": "猫咪"})
        self.store.create("猫猫")
        result = await self.call("import_image", upload=Upload(encoded()), gallery_id=str(self.gallery_id))
        self.assertEqual(result["code"], 200)
        self.assertEqual(len(self.store.entries("猫咪")), 1)
        self.assertEqual(len(self.store.entries("猫猫")), 0)

    async def test_import_dedup_preview_and_invalid_uploads(self):
        upload = Upload(encoded())
        first = await self.call("import_image", upload=upload, gallery_id=str(self.gallery_id))
        self.assertTrue(first["data"]["added"])
        self.assertTrue(upload.closed)
        duplicate = await self.call("import_image", upload=Upload(encoded()), gallery_id=str(self.gallery_id))
        self.assertFalse(duplicate["data"]["added"])
        preview = await self.call("preview", image_id=str(first["data"]["id"]))
        self.assertTrue(preview["data"]["src"].startswith("data:image/png;base64,"))
        for data in (b"", b"<svg>bad</svg>"):
            result = await self.call("import_image", upload=Upload(data), gallery_id=str(self.gallery_id))
            self.assertEqual(result["code"], 400)
        self.admin.config["max_download_mb"] = 1
        upload = Upload(b"a" * (1024 * 1024 + 1))
        self.assertEqual((await self.call("import_image", upload=upload, gallery_id=str(self.gallery_id)))["code"], 400)
        self.assertTrue(upload.closed)
        self.assertEqual((await self.call("import_image", gallery_id=str(self.gallery_id)))["code"], 400)

    async def test_original_preserves_static_and_animation_bytes(self):
        for data, mime in ((encoded(), "image/png"), (animation(), "image/gif")):
            image_id, _ = self.store.add("猫猫", data)
            result = await self.call("original", image_id=str(image_id))
            original = result["data"]
            prefix, payload = original["src"].split(",", 1)
            self.assertEqual(prefix, f"data:{mime};base64")
            self.assertEqual(base64.b64decode(payload), data)
            self.assertEqual((original["width"], original["height"]), (32, 24))
            self.assertEqual(original["animated"], mime == "image/gif")
        self.assertEqual((await self.call("original", image_id="99999"))["code"], 400)
        self.assertEqual((await self.call("original", image_id="../images"))["code"], 400)
        self.admin.closed = True
        self.assertEqual((await self.call("original", image_id="1"))["code"], 503)

    async def test_json_ids_and_partial_delete(self):
        for payload in (None, [], "bad", {"ids": []}, {"ids": [True]}, {"ids": [2**63]}, {"ids": [1.5]}, {"ids": [1] * 21}):
            self.assertEqual((await self.call("delete_images", payload))["code"], 400)
        image_id, _ = self.store.add("猫猫", encoded())
        result = await self.call("delete_images", {"ids": [image_id, image_id, 999]})
        self.assertEqual(result["data"]["deleted"], [image_id])
        self.assertEqual(len(result["data"]["errors"]), 1)
        self.assertEqual(self.store.name_by_id(self.gallery_id), "猫猫")

    async def test_move_replace_and_missing_gallery(self):
        image_id, _ = self.store.add("猫猫", encoded())
        self.store.create("狗狗")
        target = self.store.gallery_identity("狗狗")[0]
        moved = await self.call("move", {"id": image_id, "gallery_id": target})
        self.assertEqual(moved["data"]["gallery"], "狗狗")
        data = encoded(picture(12))
        result = await self.call("replace", upload=Upload(data), image_id=str(image_id))
        self.assertEqual(result["code"], 200)
        self.assertEqual(self.store.path(self.store.by_ids((image_id,))[0]).read_bytes(), data)
        self.store.delete_gallery(self.gallery_id)
        self.store.create("猫猫")
        self.assertEqual((await self.call("import_image", upload=Upload(encoded()), gallery_id=str(self.gallery_id)))["code"], 400)


class ManagementStorageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = GalleryStore(Path(self.temp.name))
        self.store.create("猫猫")
        self.gallery_id = self.store.gallery_identity("猫猫")[0]
        self.image_id, _ = self.store.add("猫猫", encoded())

    def test_gallery_delete_rolls_back_files_and_metadata(self):
        self.store.add_alias("猫猫", "小猫")
        path = self.store.path(self.store.by_ids((self.image_id,))[0])
        with self.store.connect() as db:
            db.execute("CREATE TRIGGER fail_delete BEFORE DELETE ON galleries BEGIN SELECT RAISE(ABORT, 'fail'); END")
        with self.assertRaises(Exception):
            self.store.delete_gallery(self.gallery_id)
        self.assertTrue(path.exists())
        self.assertEqual(self.store.require_gallery("小猫"), "猫猫")
        self.assertEqual(len(self.store.entries("猫猫")), 1)
        with self.store.connect() as db:
            db.execute("DROP TRIGGER fail_delete")
        self.store.delete_gallery(self.gallery_id)
        self.assertFalse(path.exists())
        self.assertEqual(self.store.catalog(), [])
        self.store.create("猫猫")
        new_id, _ = self.store.add("猫猫", encoded())
        self.assertGreater(new_id, self.image_id)

    def test_replace_rollback_and_recovery(self):
        path = self.store.path(self.store.by_ids((self.image_id,))[0])
        with self.store.connect() as db:
            db.execute("CREATE TRIGGER fail_replace BEFORE UPDATE ON images BEGIN SELECT RAISE(ABORT, 'fail'); END")
        with self.assertRaises(Exception):
            self.store.replace_image(self.image_id, encoded(picture(12)))
        self.assertEqual(path.read_bytes(), encoded())
        self.assertEqual(list(self.store.images.glob("*.replacement.img")), [])
        path.rename(path.with_suffix(".deleting"))
        orphan = self.store.images / "orphan.replacement.img"
        orphan.write_bytes(encoded())
        self.store.recover_deletions()
        self.assertTrue(path.exists())
        self.assertFalse(orphan.exists())

    def test_duplicate_move_replace_and_paging(self):
        self.store.create("狗狗")
        target = self.store.gallery_identity("狗狗")[0]
        self.store.add("狗狗", encoded())
        with self.assertRaises(ValueError):
            self.store.move_image(self.image_id, target)
        other, _ = self.store.add("猫猫", encoded(picture(12)))
        with self.assertRaises(ValueError):
            self.store.replace_image(other, encoded())
        with self.store.connect() as db:
            for i in range(25):
                db.execute("INSERT INTO images(gallery,filename,fingerprint) VALUES(?,?,?)", ("猫猫", f"fake-{i}", f"fake-{i}"))
        result = self.store.image_page(self.gallery_id, "", 1)
        self.assertEqual(result["total"], 27)
        self.assertEqual(len(result["items"]), 20)
        self.assertEqual(len(self.store.image_page(self.gallery_id, "", 2)["items"]), 7)
        self.assertEqual(self.store.image_page(None, str(other), 1)["items"][0]["id"], other)
