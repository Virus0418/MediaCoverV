import base64
import tempfile
import unittest
from io import BytesIO
from pathlib import Path

from PIL import Image, ImageDraw

from mediacovergenerator.models import AppConfig, LibraryTextOverlayOverride, LibraryTitleConfig
from mediacovergenerator.style.style_text_overlay import create_style_text_overlay
from mediacovergenerator.text_layers import (
    TextLayerMissingError,
    builtin_layer_file,
    builtin_thumb_file,
    list_builtin_layers,
    match_builtin_name,
    require_text_layer,
    resolve_text_layer,
    resolve_text_overlay_settings,
    store_text_layer,
)
from mediacovergenerator.utils.image_manager import ResolutionConfig


def _backdrop(path: Path) -> None:
    image = Image.new("RGB", (960, 540), (40, 90, 140))
    draw = ImageDraw.Draw(image)
    draw.rectangle((0, 360, 960, 540), fill=(20, 40, 30))
    draw.ellipse((620, 40, 860, 280), fill=(230, 170, 70))
    image.save(path, format="JPEG", quality=90)
    image.close()


def _text_layer(path: Path) -> None:
    image = Image.new("RGBA", (800, 360), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    draw.rectangle((80, 40, 720, 320), outline=(255, 255, 255, 230), width=6)
    draw.text((250, 150), "ANIMATED", fill=(255, 240, 210, 255))
    image.save(path, format="PNG")
    image.close()


class TextOverlayTests(unittest.TestCase):
    def test_composite_keeps_canvas_and_places_text(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            background = root / "background.jpg"
            layer = root / "layer.png"
            _backdrop(background)
            _text_layer(layer)
            encoded = create_style_text_overlay(
                background,
                layer,
                resolution_config=ResolutionConfig((640, 360)),
                darken=0.35,
                blur_radius=6,
                saturation=0.8,
                vignette=0.5,
                text_scale=0.7,
                text_offset_x=0,
                text_offset_y=0,
            )
            raw = base64.b64decode(encoded)
            with Image.open(BytesIO(raw)) as result:
                self.assertEqual(result.size, (640, 360))
                center = result.convert("RGB").getpixel((320, 180))
                corner = result.convert("RGB").getpixel((8, 8))
            self.assertGreater(sum(center), sum(corner))

    def test_missing_layer_raises(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            background = root / "background.jpg"
            _backdrop(background)
            with self.assertRaises(TextLayerMissingError):
                create_style_text_overlay(background, root / "missing.png", resolution_config=ResolutionConfig((320, 180)))

    def test_library_override_and_upload(self) -> None:
        config = AppConfig(
            library_titles=[
                LibraryTitleConfig(
                    library_id="lib1",
                    library_name="动画",
                    text_overlay=LibraryTextOverlayOverride(enabled=True, darken=0.2, blur_radius=3, background_pick="latest", fixed_item_id="42"),
                )
            ]
        )
        resolved = resolve_text_overlay_settings(config, "lib1")
        self.assertEqual(resolved.darken, 0.2)
        self.assertEqual(resolved.blur_radius, 3)
        self.assertEqual(resolved.background_pick, "latest")
        self.assertEqual(config.cover.text_overlay.darken, 0.45)

        disabled = config.model_copy(deep=True)
        disabled.library_titles[0].text_overlay.enabled = False
        self.assertEqual(resolve_text_overlay_settings(disabled, "lib1").darken, 0.45)

        legacy = AppConfig.model_validate({"cover": {"style": "static_1"}, "emby": {"base_url": "http://emby"}})
        self.assertEqual(legacy.cover.text_overlay.background_pick, "random")

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config.paths.data_dir = str(root)
            config.paths.text_layers_dir = str(root / "text_layers")
            layer = BytesIO()
            sample = Image.new("RGBA", (32, 32), (255, 255, 255, 0))
            sample.save(layer, format="PNG")
            sample.close()
            saved = store_text_layer(root, config, "lib1", layer.getvalue())
            self.assertEqual(saved.name, "lib1.png")
            with self.assertRaises(ValueError):
                store_text_layer(root, config, "../etc", layer.getvalue())
            jpeg = BytesIO()
            Image.new("RGB", (8, 8), (1, 2, 3)).save(jpeg, format="JPEG")
            with self.assertRaises(ValueError):
                store_text_layer(root, config, "lib1", jpeg.getvalue())


class TextLayerResolutionTests(unittest.TestCase):
    def test_match_exact_then_contains_either_way(self) -> None:
        names = ["动漫", "动画电影", "国产剧", "电影", "剧集"]
        self.assertEqual(match_builtin_name("动画电影", names), "动画电影")
        self.assertEqual(match_builtin_name("我的国产剧", names), "国产剧")
        self.assertEqual(match_builtin_name("动画", names), "动画电影")
        self.assertEqual(match_builtin_name("电影剧集", names), "剧集")
        self.assertIsNone(match_builtin_name("纪录片", names))
        self.assertIsNone(match_builtin_name("   ", names))

    def test_resolution_order(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            builtin = root / "builtins"
            builtin.mkdir()
            for name in ("动漫", "动画电影", "综艺"):
                image = Image.new("RGBA", (4, 4), (255, 255, 255, 200))
                image.save(builtin / f"{name}.png")
                image.close()
            config = AppConfig()
            config.paths.text_layers_dir = str(root / "layers")

            auto = resolve_text_layer(root, config, "libA", "动画电影", builtin_dir=builtin)
            self.assertEqual((auto.source, auto.name, auto.label), ("auto", "动画电影", "自动匹配:动画电影"))

            config.library_titles = [
                LibraryTitleConfig(
                    library_id="libA",
                    library_name="动画电影",
                    text_overlay=LibraryTextOverlayOverride(builtin_layer="综艺"),
                )
            ]
            chosen = resolve_text_layer(root, config, "libA", "动画电影", builtin_dir=builtin)
            self.assertEqual((chosen.source, chosen.name, chosen.label), ("builtin", "综艺", "内置:综艺"))

            config.library_titles[0].text_overlay.builtin_layer = "不存在"
            fallback = resolve_text_layer(root, config, "libA", "动画电影", builtin_dir=builtin)
            self.assertEqual(fallback.source, "auto")

            explicit = resolve_text_layer(
                root,
                config,
                "libA",
                "随便",
                builtin_dir=builtin,
                explicit_builtin="动漫",
            )
            self.assertEqual((explicit.source, explicit.name), ("builtin", "动漫"))

            custom_dir = root / "layers"
            custom_dir.mkdir()
            image = Image.new("RGBA", (4, 4), (255, 0, 0, 255))
            image.save(custom_dir / "libA.png")
            image.close()
            custom = resolve_text_layer(
                root,
                config,
                "libA",
                "动画电影",
                builtin_dir=builtin,
                explicit_builtin="综艺",
            )
            self.assertEqual(custom.source, "custom")
            self.assertEqual(custom.label, "自定义")
            self.assertEqual(custom.path.name, "libA.png")

            missing = resolve_text_layer(root, config, "libB", "没有这种库", builtin_dir=builtin, explicit_builtin="")
            self.assertIsNone(missing)
            with self.assertRaises(TextLayerMissingError) as caught:
                require_text_layer(root, AppConfig(paths=config.paths), "libB", "没有这种库")
            self.assertIn("没有可用的文字层", str(caught.exception))

    def test_shipped_layers_stay_full_resolution(self) -> None:
        self.assertEqual(
            set(list_builtin_layers()),
            {"动漫", "动画电影", "华语电影", "合集", "国产剧", "外语电影", "日韩剧", "欧美剧", "纪录片", "综艺"},
        )
        path = builtin_layer_file("动漫")
        self.assertIsNotNone(path)
        with Image.open(path) as image:
            self.assertEqual(image.size, (3840, 2160))
            low, high = image.getchannel("A").getextrema()
        self.assertEqual((low, high), (0, 255))
        thumb = builtin_thumb_file("动漫")
        self.assertIsNotNone(thumb)
        self.assertLess(thumb.stat().st_size, path.stat().st_size)
        with Image.open(thumb) as image:
            self.assertLessEqual(max(image.size), 480)


if __name__ == "__main__":
    unittest.main()
