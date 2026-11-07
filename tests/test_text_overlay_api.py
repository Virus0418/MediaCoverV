import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from io import BytesIO
from pathlib import Path
from urllib.parse import urlparse

from PIL import Image
from fastapi.testclient import TestClient

from mediacovergenerator.api import app


class _FakeEmby(BaseHTTPRequestHandler):
    posts: list[str] = []
    backdrop = b""

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        if path == "/emby/System/Info/Public":
            self._send({"ServerName": "fake", "Version": "4.8.0"})
            return
        if path == "/emby/Library/VirtualFolders/Query":
            self._send({"Items": [{"Id": "lib1", "Name": "动画电影", "CollectionType": "movies"}]})
            return
        if path == "/emby/Items":
            self._send(
                {
                    "Items": [
                        {
                            "Id": "movie1",
                            "Name": "测试片",
                            "Type": "Movie",
                            "BackdropImageTags": ["tag1"],
                        }
                    ]
                }
            )
            return
        if "/Images/" in path:
            body = type(self).backdrop
            self.send_response(200)
            self.send_header("Content-Type", "image/jpeg")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self.send_error(404, path)

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        if length:
            self.rfile.read(length)
        type(self).posts.append(urlparse(self.path).path)
        self._send({"ok": True})

    def log_message(self, fmt: str, *args) -> None:
        return

    def _send(self, payload: dict) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def _png(color: tuple[int, int, int, int]) -> bytes:
    image = Image.new("RGBA", (320, 180), (0, 0, 0, 0))
    draw_box = Image.new("RGBA", (80, 40), color)
    image.paste(draw_box, (120, 70), draw_box)
    buffer = BytesIO()
    image.save(buffer, format="PNG")
    image.close()
    draw_box.close()
    return buffer.getvalue()


def _jpeg() -> bytes:
    image = Image.new("RGB", (640, 360), (30, 80, 140))
    buffer = BytesIO()
    image.save(buffer, format="JPEG", quality=85)
    image.close()
    return buffer.getvalue()


class TextOverlayApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        _FakeEmby.backdrop = _jpeg()
        _FakeEmby.posts = []
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), _FakeEmby)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        host, port = cls.server.server_address
        cls.client = TestClient(app)
        response = cls.client.put(
            "/config",
            json={
                "emby": {"base_url": f"http://{host}:{port}", "api_key": "test-key"},
                "cover": {"style": "text_overlay", "resolution": "720p", "text_overlay": {"darken": 0.2, "blur_radius": 0}},
            },
        )
        assert response.status_code == 200, response.text

    def setUp(self) -> None:
        layer_dir = Path(__file__).resolve().parents[1] / "data" / "text_layers"
        if layer_dir.exists():
            for path in layer_dir.glob("*"):
                path.unlink(missing_ok=True)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.thread.join(timeout=3)
        data = Path(__file__).resolve().parents[1] / "data"
        for name in ("config.json",):
            (data / name).unlink(missing_ok=True)
        layer_dir = data / "text_layers"
        if layer_dir.exists():
            for path in layer_dir.glob("*"):
                path.unlink(missing_ok=True)

    def test_upload_replace_preview_without_emby_write(self) -> None:
        first = _png((220, 30, 30, 255))
        uploaded = self.client.post(
            "/libraries/lib1/text-layer",
            files={"file": ("layer.png", first, "image/png")},
        )
        self.assertEqual(uploaded.status_code, 200, uploaded.text)
        fetched = self.client.get("/libraries/lib1/text-layer")
        self.assertEqual(fetched.status_code, 200)
        self.assertEqual(fetched.headers["content-type"], "image/png")
        listed = self.client.get("/text-layers")
        self.assertIn("lib1", listed.json()["library_ids"])

        preview = self.client.post(
            "/libraries/lib1/preview",
            json={"resolution": "720p", "text_overlay": {"darken": 0.1, "blur_radius": 0, "text_scale": 1}},
        )
        self.assertEqual(preview.status_code, 200, preview.text)
        payload = preview.json()
        self.assertEqual(payload["source_kind"], "backdrop")
        self.assertEqual(payload["source_item_id"], "movie1")
        self.assertEqual(_FakeEmby.posts, [])
        with Image.open(BytesIO(__import__("base64").b64decode(payload["image_base64"]))) as image:
            red = image.convert("RGB").getpixel((image.width // 2, image.height // 2))
        self.assertGreater(red[0], red[2] + 40)

        replaced = self.client.post(
            "/libraries/lib1/text-layer",
            files={"file": ("layer.png", _png((20, 40, 220, 255)), "image/png")},
        )
        self.assertEqual(replaced.status_code, 200, replaced.text)
        again = self.client.post("/libraries/lib1/preview", json={"resolution": "720p", "text_overlay": {"darken": 0.1, "blur_radius": 0}})
        self.assertEqual(again.status_code, 200, again.text)
        with Image.open(BytesIO(__import__("base64").b64decode(again.json()["image_base64"]))) as image:
            blue = image.convert("RGB").getpixel((image.width // 2, image.height // 2))
        self.assertGreater(blue[2], blue[0] + 40)
        self.assertEqual(_FakeEmby.posts, [])

        removed = self.client.delete("/libraries/lib1/text-layer")
        self.assertEqual(removed.status_code, 200, removed.text)
        fallback = self.client.post("/libraries/lib1/preview", json={"resolution": "480p"})
        self.assertEqual(fallback.status_code, 200, fallback.text)
        self.assertEqual(fallback.json()["text_layer_source"], "auto")
        self.assertEqual(fallback.json()["text_layer_name"], "动画电影")
        self.assertEqual(fallback.json()["text_layer_label"], "自动匹配:动画电影")
        self.assertEqual(_FakeEmby.posts, [])

    def test_builtin_choice_and_unmatched(self) -> None:
        listed = self.client.get("/text-layers/builtins")
        self.assertEqual(listed.status_code, 200, listed.text)
        names = [item["name"] for item in listed.json()["layers"]]
        self.assertIn("综艺", names)
        thumb = self.client.get("/text-layers/builtins/综艺/thumb")
        self.assertEqual(thumb.status_code, 200)
        self.assertEqual(thumb.headers["content-type"], "image/png")
        self.assertLess(len(thumb.content), 200_000)
        self.assertEqual(self.client.get("/text-layers/builtins/不存在/thumb").status_code, 404)

        resolved = self.client.post(
            "/text-layers/resolve",
            json={
                "items": [
                    {"library_id": "lib1", "library_name": "动画电影", "builtin_layer": ""},
                    {"library_id": "lib1", "library_name": "动画电影", "builtin_layer": "综艺"},
                    {"library_id": "other", "library_name": "没有这个分类", "builtin_layer": ""},
                    {"library_id": "../x", "library_name": "综艺", "builtin_layer": ""},
                ]
            },
        )
        self.assertEqual(resolved.status_code, 200, resolved.text)
        rows = {row["library_id"] + row["label"]: row for row in resolved.json()}
        self.assertEqual(rows["lib1自动匹配:动画电影"]["source"], "auto")
        self.assertEqual(rows["lib1内置:综艺"]["source"], "builtin")
        self.assertEqual(rows["other未匹配"]["source"], "none")
        self.assertEqual(rows["../x未匹配"]["source"], "none")

        preview = self.client.post(
            "/libraries/lib1/preview",
            json={"resolution": "480p", "library_text_overlay": {"builtin_layer": "综艺", "enabled": False}},
        )
        self.assertEqual(preview.status_code, 200, preview.text)
        self.assertEqual(preview.json()["text_layer_source"], "builtin")
        self.assertEqual(preview.json()["text_layer_name"], "综艺")
        self.assertEqual(_FakeEmby.posts, [])


if __name__ == "__main__":
    unittest.main()
