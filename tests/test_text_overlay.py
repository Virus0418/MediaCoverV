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


if __name__ == "__main__":
    unittest.main()
