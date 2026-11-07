"""文字层叠加：剧照铺满背景，再叠上每个媒体库自己的透明 PNG。

内存预算按 512MB 容器来。全程用 Pillow，暗角只在很小的 float32 网格上计算，
不分配整幅 float64 数组。
"""

from __future__ import annotations

import base64
from io import BytesIO
from pathlib import Path

import numpy as np
from PIL import Image, ImageEnhance, ImageFilter, ImageOps

from mediacovergenerator.logging import logger
from mediacovergenerator.text_layers import TextLayerMissingError
from mediacovergenerator.utils.image_manager import ResolutionConfig

_MAX_CANVAS_PIXELS = 3840 * 2160
_MAX_CANVAS_SIDE = 3840


def create_style_text_overlay(
    image_path,
    text_layer_path,
    *,
    resolution_config=None,
    darken: float = 0.45,
    blur_radius: int = 18,
    saturation: float = 0.85,
    vignette: float = 0.40,
    text_scale: float = 1.0,
    text_offset_x: float = 0.0,
    text_offset_y: float = 0.0,
) -> str:
    if resolution_config is None:
        resolution_config = ResolutionConfig("1080p")
    canvas = _cap_canvas(resolution_config.size)
    layer_file = Path(text_layer_path)
    if not layer_file.is_file():
        raise TextLayerMissingError(f"找不到文字层：{layer_file}")

    logger.info(
        "文字层叠加 %sx%s 压暗=%.2f 模糊=%s 饱和=%.2f 暗角=%.2f 缩放=%.2f 偏移=(%.1f, %.1f)",
        canvas[0],
        canvas[1],
        float(darken),
        int(blur_radius),
        float(saturation),
        float(vignette),
        float(text_scale),
        float(text_offset_x),
        float(text_offset_y),
    )

    background = _load_cover(image_path, canvas)
    layer = None
    result = None
    try:
        background = _replace(background, _blur(background, int(blur_radius)))
        background = _replace(background, _saturate(background, float(saturation)))
        background = _replace(background, _darken(background, float(darken)))
        background = _replace(background, _vignette(background, float(vignette)))
        with Image.open(layer_file) as source:
            source.load()
            layer = source.convert("RGBA")
        fitted = _fit_layer(layer, canvas[0], canvas[1], float(text_scale))
        if fitted is not layer:
            layer.close()
            layer = fitted
        offset_x = int(round(canvas[0] * float(text_offset_x) / 100.0))
        offset_y = int(round(canvas[1] * float(text_offset_y) / 100.0))
        origin_x = (canvas[0] - layer.width) // 2 + offset_x
        origin_y = (canvas[1] - layer.height) // 2 + offset_y
        result = _composite(background, layer, origin_x, origin_y)
        return _encode(result)
    finally:
        background.close()
        if layer is not None:
            layer.close()
        if result is not None:
            result.close()


def _cap_canvas(size: tuple[int, int]) -> tuple[int, int]:
    width = max(1, int(size[0]))
    height = max(1, int(size[1]))
    longest = max(width, height)
    pixels = width * height
    if longest <= _MAX_CANVAS_SIDE and pixels <= _MAX_CANVAS_PIXELS:
        return width, height
    scale = min(_MAX_CANVAS_SIDE / longest, (_MAX_CANVAS_PIXELS / pixels) ** 0.5)
    capped = (max(1, int(width * scale)), max(1, int(height * scale)))
    logger.warning("输出分辨率 %sx%s 超出内存预算，已限制为 %sx%s", width, height, capped[0], capped[1])
    return capped


def _replace(previous: Image.Image, current: Image.Image) -> Image.Image:
    if current is not previous:
        previous.close()
    return current


def _load_cover(image_path, canvas: tuple[int, int]) -> Image.Image:
    with Image.open(image_path) as raw:
        if raw.format == "JPEG":
            raw.draft("RGB", (canvas[0] * 2, canvas[1] * 2))
        if raw.width * raw.height > 24_000_000:
            raw.thumbnail((4096, 4096), Image.Resampling.BILINEAR)
        raw.load()
        oriented = ImageOps.exif_transpose(raw)
        rgb = oriented.convert("RGB")
        if oriented is not raw and oriented is not rgb:
            oriented.close()
        target_w, target_h = canvas
        if rgb.width > target_w * 2.2 and rgb.height > target_h * 2.2:
            scale = max(target_w / rgb.width, target_h / rgb.height) * 1.2
            reduced = rgb.resize(
                (max(1, int(rgb.width * scale)), max(1, int(rgb.height * scale))),
                Image.Resampling.BILINEAR,
            )
            rgb.close()
            rgb = reduced
        fitted = ImageOps.fit(rgb, canvas, method=Image.Resampling.LANCZOS, centering=(0.5, 0.5))
        rgb.close()
        return fitted


def _blur(image: Image.Image, radius: int) -> Image.Image:
    radius = max(0, min(40, int(radius)))
    if radius <= 0:
        return image
    width, height = image.size
    if width * height > 1920 * 1080:
        scale = ((1920 * 1080) / (width * height)) ** 0.5
        small_size = (max(1, int(width * scale)), max(1, int(height * scale)))
        small = image.resize(small_size, Image.Resampling.BILINEAR)
        blurred = small.filter(ImageFilter.GaussianBlur(radius=max(1, round(radius * scale))))
        small.close()
        enlarged = blurred.resize((width, height), Image.Resampling.BILINEAR)
        blurred.close()
        return enlarged
    return image.filter(ImageFilter.GaussianBlur(radius=radius))


def _saturate(image: Image.Image, saturation: float) -> Image.Image:
    saturation = min(2.0, max(0.0, float(saturation)))
    if abs(saturation - 1.0) < 0.001:
        return image
    return ImageEnhance.Color(image).enhance(saturation)


def _darken(image: Image.Image, amount: float) -> Image.Image:
    amount = min(1.0, max(0.0, float(amount)))
    if amount <= 0:
        return image
    alpha = int(round(amount * 255))
    overlay = Image.new("RGBA", image.size, (0, 0, 0, alpha))
    base = image.convert("RGBA")
    composed = Image.alpha_composite(base, overlay).convert("RGB")
    overlay.close()
    base.close()
    return composed


def _vignette(image: Image.Image, strength: float) -> Image.Image:
    strength = min(1.0, max(0.0, float(strength)))
    if strength <= 0:
        return image
    width, height = image.size
    small_w = 160
    small_h = max(1, int(round(height * small_w / max(width, 1))))
    yy, xx = np.ogrid[0:small_h, 0:small_w]
    center_x = np.float32((small_w - 1) / 2.0)
    center_y = np.float32((small_h - 1) / 2.0)
    nx = (xx.astype(np.float32) - center_x) / max(float(center_x), 1.0)
    ny = (yy.astype(np.float32) - center_y) / max(float(center_y), 1.0)
    distance = np.sqrt(nx * nx + ny * ny)
    edge = np.clip((distance - np.float32(0.42)) / np.float32(0.9), 0.0, 1.0)
    alpha = (edge * edge * np.float32(strength * 210.0)).astype(np.uint8)
    mask = Image.fromarray(alpha, mode="L").resize((width, height), Image.Resampling.BILINEAR)
    shade = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    shade.putalpha(mask)
    base = image.convert("RGBA")
    composed = Image.alpha_composite(base, shade)
    mask.close()
    shade.close()
    base.close()
    return composed


def _fit_layer(layer: Image.Image, canvas_w: int, canvas_h: int, scale: float) -> Image.Image:
    scale = min(1.5, max(0.2, float(scale)))
    box_w = max(1, int(round(canvas_w * scale)))
    box_h = max(1, int(round(canvas_h * scale)))
    factor = min(box_w / layer.width, box_h / layer.height)
    new_size = (max(1, int(round(layer.width * factor))), max(1, int(round(layer.height * factor))))
    if new_size == layer.size:
        return layer
    return layer.resize(new_size, Image.Resampling.LANCZOS)


def _composite(background: Image.Image, layer: Image.Image, x: int, y: int) -> Image.Image:
    base = background.convert("RGBA")
    sheet = Image.new("RGBA", base.size, (0, 0, 0, 0))
    sheet.paste(layer, (x, y), layer)
    composed = Image.alpha_composite(base, sheet)
    base.close()
    sheet.close()
    return composed


def _encode(image: Image.Image) -> str:
    rgb = image.convert("RGB")
    buffer = BytesIO()
    try:
        rgb.save(buffer, format="WEBP", quality=82, method=4)
    except Exception:
        buffer = BytesIO()
        rgb.save(buffer, format="JPEG", quality=85, optimize=True)
    encoded = base64.b64encode(buffer.getvalue()).decode("ascii")
    rgb.close()
    return encoded
