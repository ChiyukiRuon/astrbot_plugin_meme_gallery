"""使用本地官方 AstrBot Web API 和 bridge SDK 验证受限 iframe 中的管理页。

运行：python tests/verify_web_view.py --astrbot <源码目录> --output <截图目录>
需额外安装 fastapi、python-multipart、playwright，并有本地 Chrome。
最小宿主模拟 Dashboard 转发，不启动完整 AstrBot，也不验证登录鉴权。
"""

import argparse
import asyncio
import importlib.util
import json
import re
import sys
import tempfile
import types
from pathlib import Path

from aiohttp import web as http
from playwright.async_api import async_playwright
from starlette.requests import Request

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from gallery.storage import GalleryStore
from test_gallery import animation, encoded, picture


HOST = """<!doctype html><html><head><meta name="viewport" content="width=device-width,initial-scale=1"></head>
<body style="margin:0"><iframe id="view" src="/view/index.html" sandbox="allow-scripts allow-forms allow-downloads" style="border:0;width:100%;height:100vh"></iframe>
<script>
const frame = document.getElementById('view')
const send = message => frame.contentWindow.postMessage({ channel: 'astrbot-plugin-page', ...message }, '*')
window.setDark = isDark => send({ kind: 'context', context: { isDark } })
window.addEventListener('message', async event => {
    if (event.source !== frame.contentWindow || event.data.channel !== 'astrbot-plugin-page') return
    const message = event.data
    if (message.kind === 'ready') {
        send({ kind: 'context', context: { pluginName: 'astrbot_plugin_meme_gallery', isDark: false, locale: 'zh-CN' } })
        return
    }
    if (message.kind !== 'request') return
    try {
        const url = new URL('/api/' + message.endpoint, location.origin)
        let options = {}
        if (message.action === 'api:get') {
            for (const [key, value] of Object.entries(message.params || {})) url.searchParams.set(key, value)
        } else if (message.action === 'api:post') {
            options = { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(message.body) }
        } else if (message.action === 'files:upload') {
            const body = new FormData()
            body.append('file', new Blob([message.fileBuffer], { type: message.fileType }), message.fileName)
            options = { method: 'POST', body }
        } else throw new Error('不支持的测试请求')
        const response = await fetch(url, options)
        const data = await response.json()
        if (!response.ok || data.status === 'error') throw new Error(data.message)
        send({ kind: 'response', requestId: message.requestId, ok: true, data })
    } catch (error) { send({ kind: 'response', requestId: message.requestId, ok: false, error: error.message }) }
})
</script></body></html>"""


def load_public_api(root):
    # 隔离框架初始化，只加载未修改的官方公共 API 及其上传辅助模块。
    for name in ("astrbot", "astrbot.api", "astrbot.core", "astrbot.core.utils"):
        module = types.ModuleType(name)
        module.__path__ = [str(root.joinpath(*name.split(".")))]
        sys.modules[name] = module
    spec = importlib.util.spec_from_file_location("astrbot.api.web", root / "astrbot/api/web.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


async def main(args):
    api = load_public_api(args.astrbot)
    from gallery.web import WebAdmin

    with tempfile.TemporaryDirectory() as directory:
        store = GalleryStore(Path(directory))
        store.create("猫猫收藏")
        store.create("旅行与日常")
        store.add("猫猫收藏", encoded())
        store.add("猫猫收藏", encoded(picture(12)))
        animation_id, _ = store.add("猫猫收藏", animation())
        routes = []
        admin = WebAdmin(store, asyncio.Lock(), {})
        admin.register(types.SimpleNamespace(register_web_api=lambda *route: routes.append(route)))

        async def api_handler(req):
            path = "/astrbot_plugin_meme_gallery/" + req.match_info["endpoint"]
            for route, handler, methods, _ in routes:
                pattern = re.sub(r"<(\w+)>", r"(?P<\1>[^/]+)", route)
                matched = re.fullmatch(pattern, path)
                if matched and req.method in methods:
                    body = await req.read()

                    async def receive():
                        return {"type": "http.request", "body": body, "more_body": False}

                    scope = {"type": "http", "method": req.method, "path": path,
                             "query_string": req.query_string.encode(), "scheme": "http",
                             "server": ("localhost", 80), "client": ("127.0.0.1", 1234),
                             "headers": [(k.lower().encode(), v.encode()) for k, v in req.headers.items()]}
                    raw = Request(scope, receive)
                    public = api.PluginRequest(raw, username="test-admin", plugin_name="astrbot_plugin_meme_gallery")
                    with api.bind_request_context(public):
                        response = await handler(**matched.groupdict())
                    await raw.close()
                    return http.Response(body=response.body, status=response.status_code, headers=dict(response.headers))
            raise http.HTTPNotFound()

        async def assets(req):
            name = req.match_info["name"]
            if name == "bridge.js":
                path = args.astrbot / "astrbot/dashboard/plugin_page_bridge.js"
            elif name in ("index.html", "app.js", "style.css"):
                path = ROOT / "views/manage" / name
            else:
                raise http.HTTPNotFound()
            text = path.read_text(encoding="utf-8")
            if name == "index.html":
                text = text.replace('<script type="module"', '<script src="./bridge.js"></script><script type="module"')
            content_type = "text/html" if name.endswith("html") else "text/css" if name.endswith("css") else "text/javascript"
            return http.Response(text=text, content_type=content_type, headers={"Access-Control-Allow-Origin": "*"})

        async def host(req):
            return http.Response(text=HOST, content_type="text/html")

        app = http.Application(client_max_size=21 * 1024 * 1024)
        app.router.add_get("/", host)
        app.router.add_get("/view/{name}", assets)
        app.router.add_route("*", "/api/{endpoint:.*}", api_handler)
        runner = http.AppRunner(app)
        await runner.setup()
        site = http.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        args.output.mkdir(parents=True, exist_ok=True)
        try:
            async with async_playwright() as playwright:
                browser = await playwright.chromium.launch(channel="chrome", headless=True)
                page = await browser.new_page(viewport={"width": 1280, "height": 900})
                errors = []
                page.on("pageerror", lambda error: errors.append(str(error)))
                await page.goto(f"http://127.0.0.1:{port}")
                view = page.frame_locator("#view")
                await view.locator(".image-card img").first.wait_for()
                await page.screenshot(path=str(args.output / "desktop.png"), full_page=True)
                animated_thumb = view.get_by_role("button", name=f"查看原图 ID {animation_id}", exact=True)
                await animated_thumb.locator(".gif-badge").wait_for()
                assert await animated_thumb.locator(".gif-badge").inner_text() == "GIF"
                assert await view.locator(".gif-badge").count() == 1
                await animated_thumb.click()
                await view.locator("#original-status").filter(has_text="动图").wait_for()
                original = view.locator("#original-image")
                assert (await original.get_attribute("src")).startswith("data:image/gif;base64,")
                frames = set()
                for _ in range(8):
                    frames.add(await original.screenshot())
                    await asyncio.sleep(0.08)
                assert len(frames) > 1, "原图窗口未观察到动图帧变化"
                await view.get_by_role("button", name="原始尺寸", exact=True).click()
                await view.get_by_role("button", name="适应窗口", exact=True).click()
                await page.screenshot(path=str(args.output / "original.png"), full_page=True)
                await page.set_viewport_size({"width": 375, "height": 900})
                await page.screenshot(path=str(args.output / "original-mobile.png"), full_page=True)
                assert await page.frames[1].evaluate("document.documentElement.scrollWidth <= innerWidth")
                await view.locator("#close-original").click()
                assert await original.get_attribute("src") is None
                await page.set_viewport_size({"width": 1280, "height": 900})
                await view.get_by_role("button", name="新建图库", exact=True).click()
                await view.get_by_label("图库名称", exact=True).fill("测试图库")
                await view.get_by_role("button", name="创建图库", exact=True).click()
                await view.locator("#editor").wait_for(state="hidden")
                await view.get_by_role("button", name="导入图片", exact=True).click()
                payloads = [{"name": "first.png", "mimeType": "image/png", "buffer": encoded()},
                            {"name": "duplicate.png", "mimeType": "image/png", "buffer": encoded()},
                            {"name": "invalid.png", "mimeType": "image/png", "buffer": b"invalid"}]
                await view.locator("#files").set_input_files(payloads)
                await view.get_by_role("button", name="开始导入").click()
                await view.locator("#import-summary").filter(has_text="导入完成").wait_for()
                assert "成功 1，重复 1，失败 1" in await view.locator("#import-summary").inner_text()
                await view.locator("#close-import").click()
                await view.get_by_role("button", name="管理图库", exact=True).click()
                await view.get_by_label("图库名称", exact=True).fill("已重命名")
                await view.locator("#editor-submit").click()
                await view.locator("#editor").wait_for(state="hidden")
                assert store.catalog("已重命名")[0]["count"] == 1
                await view.get_by_role("button", name="编辑图片", exact=True).click()
                await view.get_by_label("选择替换图片", exact=True).set_input_files({"name": "new.png", "mimeType": "image/png", "buffer": encoded(picture(12))})
                await view.get_by_role("button", name="替换原图", exact=True).click()
                await view.locator("#editor").wait_for(state="hidden")
                await view.get_by_role("button", name="编辑图片", exact=True).click()
                await view.get_by_label("所属图库", exact=True).select_option(label="旅行与日常")
                await view.get_by_role("button", name="保存所属图库").click()
                await view.locator("#editor").wait_for(state="hidden")
                assert len(store.entries("旅行与日常")) == 1
                await view.get_by_role("button", name="全部图片").click()
                await view.locator(".image-card img").first.wait_for()
                await page.set_viewport_size({"width": 375, "height": 900})
                await page.screenshot(path=str(args.output / "mobile.png"), full_page=True)
                frame = page.frames[1]
                assert await frame.evaluate("document.documentElement.scrollWidth <= innerWidth")
                await page.evaluate("setDark(true)")
                await page.screenshot(path=str(args.output / "mobile-dark.png"), full_page=True)
                await view.get_by_label("选择本页", exact=True).check()
                await view.get_by_role("button", name="删除所选").click()
                await view.get_by_role("button", name="确认删除").click()
                await view.locator("#editor").wait_for(state="hidden")
                assert store.image_page(None, "", 1)["total"] == 0
                assert not errors, errors
                await browser.close()
                print(json.dumps({"result": "passed", "screenshots": str(args.output), "browser_errors": errors}))
        finally:
            await runner.cleanup()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--astrbot", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    asyncio.run(main(parser.parse_args()))
