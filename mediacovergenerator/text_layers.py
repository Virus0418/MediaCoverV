from __future__ import annotations

import re
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path

from PIL import Image

from mediacovergenerator.logging import logger
from mediacovergenerator.models import (
    AppConfig,
    LibraryTextOverlayOverride,
    LibraryTitleConfig,
    PreviewRequest,
    TextOverlaySettings,
)
from mediacovergenerator.storage import resolve_path


class TextLayerMissingError(RuntimeError):
    """媒体库没有可用的文字层 PNG。生成时跳过该库。"""


@dataclass(frozen=True)
class TextLayerResolution:
    path: Path
    source: str
    name: str

    @property
    def label(self) -> str:
        if self.source == "custom":
            return "自定义"
        if self.source == "builtin":
            return f"内置:{self.name}"
        if self.source == "auto":
            return f"自动匹配:{self.name}"
        return "未匹配"


_LIBRARY_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")
_MAX_BYTES = 20 * 1024 * 1024
_MAX_SIDE = 3840
_MAX_PIXELS = 24_000_000
_OVERRIDE_FIELDS = (
    "darken",
    "blur_radius",
    "saturation",
    "vignette",
    "text_scale",
    "text_offset_x",
    "text_offset_y",
    "background_pick",
)


def assert_library_id(library_id: str) -> str:
    value = str(library_id or "").strip()
    if not _LIBRARY_ID.fullmatch(value) or ".." in value:
        raise ValueError("无效的媒体库 ID")
    return value


def text_layers_directory(project_root: Path, config: AppConfig) -> Path:
    return resolve_path(project_root, config.paths.text_layers_dir)


def text_layer_file(project_root: Path, config: AppConfig, library_id: str) -> Path:
    safe_id = assert_library_id(library_id)
    return text_layers_directory(project_root, config) / f"{safe_id}.png"


def list_text_layer_ids(project_root: Path, config: AppConfig) -> list[str]:
    directory = text_layers_directory(project_root, config)
    if not directory.is_dir():
        return []
    return sorted(path.stem for path in directory.glob("*.png") if path.is_file())


def builtin_layers_directory() -> Path:
    """随程序发布的内置文字层。只列这一层的 PNG，缩略图放在子目录里。"""
    return Path(__file__).resolve().parent / "assets" / "text_layers" / "picStyle1"


def list_builtin_layers(directory: Path | None = None) -> list[str]:
    directory = directory or builtin_layers_directory()
    if not directory.is_dir():
        return []
    return sorted(path.stem for path in directory.glob("*.png") if path.is_file())


def builtin_layer_file(name: str, directory: Path | None = None) -> Path | None:
    directory = directory or builtin_layers_directory()
    clean = str(name or "").strip()
    if not clean or clean not in set(list_builtin_layers(directory)):
        return None
    path = directory / f"{clean}.png"
    return path if path.is_file() else None


def builtin_thumb_file(name: str, directory: Path | None = None) -> Path | None:
    layer = builtin_layer_file(name, directory)
    if layer is None:
        return None
    thumb = layer.parent / "thumbs" / layer.name
    return thumb if thumb.is_file() else None


def match_builtin_name(library_name: str, names: list[str]) -> str | None:
    """先精确匹配库名，再看名称互相包含。同长度时按名称排序取第一个。"""
    library_name = (library_name or "").strip()
    if not library_name:
        return None
    for name in names:
        if name == library_name:
            return name
    contained = [name for name in names if name and (name in library_name or library_name in name)]
    if not contained:
        return None
    contained.sort(key=lambda item: (-len(item), item))
    return contained[0]


def selected_builtin_name(config: AppConfig, library_id: str) -> str:
    item = find_library_title(config, library_id)
    if item and item.text_overlay and item.text_overlay.builtin_layer:
        return item.text_overlay.builtin_layer.strip()
    return ""


def store_text_layer(project_root: Path, config: AppConfig, library_id: str, raw: bytes) -> Path:
    if not raw:
        raise ValueError("文件是空的")
    if len(raw) > _MAX_BYTES:
        raise ValueError("文字层不能超过 20MB")
    try:
        with Image.open(BytesIO(raw)) as image:
            image.load()
            if image.format not in {"PNG", "WEBP"}:
                raise ValueError("请上传 PNG，或带透明通道的 WebP")
            if image.width * image.height > _MAX_PIXELS:
                raise ValueError("图片尺寸过大")
            rgba = image.convert("RGBA")
    except ValueError:
        raise
    except Exception as exc:
        raise ValueError("无法解析这张图片，请换一张 PNG") from exc

    try:
        if max(rgba.size) > _MAX_SIDE:
            rgba.thumbnail((_MAX_SIDE, _MAX_SIDE), Image.Resampling.LANCZOS)
            logger.info("文字层长边超过 %s，已缩小到 %s", _MAX_SIDE, rgba.size)
        alpha_min, _alpha_max = rgba.getchannel("A").getextrema()
        if alpha_min == 255:
            logger.warning("文字层没有任何透明像素，背景会被完全盖住：%s", library_id)
        path = text_layer_file(project_root, config, library_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".png.tmp")
        rgba.save(temporary, format="PNG")
        temporary.replace(path)
        return path
    finally:
        rgba.close()


def delete_text_layer(project_root: Path, config: AppConfig, library_id: str) -> bool:
    path = text_layer_file(project_root, config, library_id)
    if not path.is_file():
        return False
    path.unlink()
    return True


def find_library_title(config: AppConfig, library_id: str) -> LibraryTitleConfig | None:
    library_id = str(library_id or "")
    for item in config.library_titles:
        if item.library_id == library_id:
            return item
    return None


def fixed_item_id_for(config: AppConfig, library_id: str) -> str:
    item = find_library_title(config, library_id)
    if item and item.text_overlay and item.text_overlay.fixed_item_id:
        return item.text_overlay.fixed_item_id.strip()
    return ""


def resolve_text_overlay_settings(config: AppConfig, library_id: str) -> TextOverlaySettings:
    base = config.cover.text_overlay
    item = find_library_title(config, library_id)
    override = item.text_overlay if item else None
    if override is None or not override.enabled:
        return base
    data = base.model_dump()
    for key in _OVERRIDE_FIELDS:
        value = getattr(override, key)
        if value is not None:
            data[key] = value
    return TextOverlaySettings.model_validate(data)


def apply_preview_overrides(config: AppConfig, library_id: str, request: PreviewRequest) -> AppConfig:
    """把页面上还没保存的滑块套到一份临时配置上，只用于预览。"""
    cover_updates: dict = {"style": "text_overlay"}
    if request.text_overlay is not None:
        cover_updates["text_overlay"] = request.text_overlay
    if request.resolution:
        cover_updates["resolution"] = request.resolution
    if request.custom_width:
        cover_updates["custom_width"] = max(320, min(3840, int(request.custom_width)))
    if request.custom_height:
        cover_updates["custom_height"] = max(180, min(3840, int(request.custom_height)))
    cover = config.cover.model_copy(update=cover_updates)

    library_titles = list(config.library_titles)
    if request.library_text_overlay is not None:
        updated: list[LibraryTitleConfig] = []
        found = False
        for item in library_titles:
            if item.library_id == library_id:
                updated.append(item.model_copy(update={"text_overlay": request.library_text_overlay}))
                found = True
            else:
                updated.append(item)
        if not found:
            updated.append(
                LibraryTitleConfig(
                    library_id=library_id,
                    text_overlay=request.library_text_overlay,
                )
            )
        library_titles = updated
    return config.model_copy(update={"cover": cover, "library_titles": library_titles})


def resolve_text_layer(
    project_root: Path,
    config: AppConfig,
    library_id: str,
    library_name: str,
    *,
    builtin_dir: Path | None = None,
    explicit_builtin: str | None = None,
) -> TextLayerResolution | None:
    """自定义上传优先，其次是页面指定的内置层，再按库名自动匹配。都没有就返回 None。"""
    custom = text_layer_file(project_root, config, library_id)
    if custom.is_file():
        return TextLayerResolution(path=custom, source="custom", name="")

    explicit = selected_builtin_name(config, library_id) if explicit_builtin is None else str(explicit_builtin or "").strip()
    if explicit:
        chosen = builtin_layer_file(explicit, builtin_dir)
        if chosen is not None:
            return TextLayerResolution(path=chosen, source="builtin", name=explicit)
        logger.warning("媒体库「%s」指定的内置文字层「%s」不存在，改为按库名自动匹配。", library_name, explicit)

    matched = match_builtin_name(library_name, list_builtin_layers(builtin_dir))
    if matched:
        path = builtin_layer_file(matched, builtin_dir)
        if path is not None:
            return TextLayerResolution(path=path, source="auto", name=matched)
    return None


def require_text_layer(
    project_root: Path,
    config: AppConfig,
    library_id: str,
    library_name: str,
) -> TextLayerResolution:
    found = resolve_text_layer(project_root, config, library_id, library_name)
    if found is None:
        message = missing_text_layer_message(library_name, library_id)
        logger.warning(message)
        raise TextLayerMissingError(message)
    logger.info("媒体库「%s」文字层来源：%s", library_name, found.label)
    return found


def missing_text_layer_message(library_name: str, library_id: str) -> str:
    return (
        f"媒体库「{library_name}」没有可用的文字层，已跳过。"
        f"可以上传自定义 PNG（data/text_layers/{library_id}.png），"
        "或在页面选择内置文字层；库名能对上时也会自动匹配内置图层。"
    )
