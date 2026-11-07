from __future__ import annotations

import hashlib
import shutil
import tempfile
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import urlparse

from mediacovergenerator.emby import EmbyClient
from mediacovergenerator.logging import logger
from mediacovergenerator.models import AppConfig, HistoryRecord, LibraryInfo, PreviewResponse
from mediacovergenerator.storage import HistoryRepository
from mediacovergenerator.text_layers import (
    TextLayerMissingError,
    fixed_item_id_for,
    missing_text_layer_message,
    resolve_text_overlay_settings,
    text_layer_file,
)
from mediacovergenerator.titles import TitleConfigResolver

if TYPE_CHECKING:
    from mediacovergenerator.fonts import FontResolver
    from mediacovergenerator.generator import PosterGenerator


class LibraryUpdateService:
    def __init__(self, project_root: Path, history_repository: HistoryRepository):
        self.project_root = project_root
        self.history_repository = history_repository
        self._font_resolver: FontResolver | None = None
        self._generator: PosterGenerator | None = None

    def _get_font_resolver(self) -> "FontResolver":
        if self._font_resolver is None:
            from mediacovergenerator.fonts import FontResolver

            self._font_resolver = FontResolver(self.project_root)
        return self._font_resolver

    def _get_generator(self) -> "PosterGenerator":
        if self._generator is None:
            from mediacovergenerator.generator import PosterGenerator

            self._generator = PosterGenerator(self.project_root)
        return self._generator

    def list_libraries(self, config: AppConfig) -> list[LibraryInfo]:
        return EmbyClient(config.emby).list_libraries()

    def generate_for_library(
        self,
        config: AppConfig,
        library_id: str,
        stop_event,
    ) -> HistoryRecord:
        client = EmbyClient(config.emby)
        libraries = client.get_library_map()
        library = libraries.get(str(library_id))
        if not library:
            raise KeyError(f"Library not found: {library_id}")

        generator = self._get_generator()
        library_name = library["Name"]
        if config.cover.style == "text_overlay":
            self._ensure_text_layer(config, library_name, str(library_id))
        title = TitleConfigResolver.from_config(config).resolve(str(library_id), library_name)
        if config.cover.style == "text_overlay":
            font_paths = (Path("."), Path("."))
        else:
            font_paths = self._get_font_resolver().resolve(config)
        source_item_ids = self._prepare_library_images(client, config, library, stop_event)

        encoded_cover = generator.render(
            config=config,
            library_name=library_name,
            title=title,
            font_paths=font_paths,
            stop_event=stop_event,
            library_id=str(library_id),
        )
        image_bytes, content_type, extension = generator.decode_image(encoded_cover)
        saved_path = generator.save_recent_cover(
            config=config,
            library_name=library_name,
            server_name=config.emby.name,
            image_bytes=image_bytes,
            extension=extension,
        )
        client.set_library_image(str(library_id), encoded_cover, content_type)

        record = HistoryRecord(
            id=hashlib.md5(f"{library_id}-{datetime.now(timezone.utc).isoformat()}".encode("utf-8")).hexdigest(),
            server=config.emby.name,
            library_id=str(library_id),
            library_name=library_name,
            source_item_ids=source_item_ids,
            saved_path=str(saved_path) if saved_path else None,
            style=config.cover.style,
            created_at=datetime.now(timezone.utc),
        )
        self.history_repository.append(record)
        return record

    def preview_text_overlay(self, config: AppConfig, library_id: str) -> PreviewResponse:
        """按当前配置画一张文字层封面，不上传、不写历史。"""
        client = EmbyClient(config.emby)
        if not client.is_configured():
            raise ValueError("请先填写 Emby 服务地址和 API Key")
        libraries = client.get_library_map()
        library = libraries.get(str(library_id))
        if not library:
            raise KeyError(f"Library not found: {library_id}")
        library_name = library["Name"]
        self._ensure_text_layer(config, library_name, str(library_id))
        layer_path = text_layer_file(self.project_root, config, str(library_id))
        settings = resolve_text_overlay_settings(config, str(library_id))
        stop_event = threading.Event()
        with tempfile.TemporaryDirectory(prefix="mcg-preview-") as temporary:
            background = Path(temporary) / "background.jpg"
            source = self._acquire_text_overlay_background(client, config, library, stop_event, background)
            from mediacovergenerator.style.style_text_overlay import create_style_text_overlay

            encoded = create_style_text_overlay(
                str(background),
                str(layer_path),
                resolution_config=self._get_generator()._resolution_config(config),
                darken=settings.darken,
                blur_radius=settings.blur_radius,
                saturation=settings.saturation,
                vignette=settings.vignette,
                text_scale=settings.text_scale,
                text_offset_x=settings.text_offset_x,
                text_offset_y=settings.text_offset_y,
            )
        image_bytes, content_type, _extension = self._get_generator().decode_image(encoded)
        del image_bytes
        resolution = self._get_generator()._resolution_config(config)
        return PreviewResponse(
            image_base64=encoded,
            content_type=content_type,
            source_item_id=source.get("source_item_id", ""),
            source_item_name=source.get("source_item_name", ""),
            source_kind=source.get("source_kind", ""),
            width=resolution.width,
            height=resolution.height,
        )

    def _ensure_text_layer(self, config: AppConfig, library_name: str, library_id: str) -> None:
        if text_layer_file(self.project_root, config, library_id).is_file():
            return
        message = missing_text_layer_message(library_name, library_id)
        logger.warning(message)
        raise TextLayerMissingError(message)

    def _prepare_library_images(
        self,
        client: EmbyClient,
        config: AppConfig,
        library: dict,
        stop_event,
    ) -> list[str]:
        generator = self._get_generator()
        library_name = library["Name"]
        cache_dir = generator.library_cache_dir(config, library_name)
        cache_dir.mkdir(parents=True, exist_ok=True)
        for file in cache_dir.glob("*"):
            if file.is_file():
                file.unlink()

        input_dir = generator.library_input_dir(config, library_name)
        if input_dir.exists():
            copied = 0
            for source in sorted(input_dir.iterdir()):
                if not source.is_file() or source.suffix.lower() not in {".jpg", ".jpeg", ".png", ".webp"}:
                    continue
                copied += 1
                shutil.copy(source, cache_dir / source.name)
            if copied:
                generator.prepare_library_images(cache_dir, generator.get_required_items(config))
                if config.cover.style == "text_overlay":
                    logger.info("媒体库「%s」使用本地素材作为文字层背景：%s", library_name, input_dir)
                return []

        sort_by = self._text_overlay_sort(config, library)
        if self._uses_fixed_background(config, library):
            saved = self._download_fixed_background(client, config, library, cache_dir / "1.jpg")
            if saved:
                source_id = saved.get("source_item_id") or ""
                return [source_id] if source_id else []

        items = self._collect_items(client, config, library, stop_event, sort_by=sort_by)
        if not items:
            raise RuntimeError(f"No valid media items found for library {library_name}")

        source_ids: list[str] = []
        for index, item in enumerate(items, start=1):
            if stop_event.is_set():
                raise RuntimeError("Job cancelled")
            image_url = self._get_image_url(client, config, item)
            if not image_url:
                continue
            content = client.download_image(image_url)
            if not content:
                continue
            target = cache_dir / f"{index}.jpg"
            target.write_bytes(content)
            item_id = self._get_item_id(config, item)
            if item_id:
                source_ids.append(str(item_id))
        if not any(cache_dir.iterdir()):
            raise RuntimeError(f"Unable to download images for library {library_name}")
        generator.prepare_library_images(cache_dir, generator.get_required_items(config))
        return source_ids

    def _collect_items(
        self,
        client: EmbyClient,
        config: AppConfig,
        library: dict,
        stop_event,
        sort_by: str | None = None,
        required_items: int | None = None,
    ) -> list[dict]:
        generator = self._get_generator()
        if required_items is None:
            required_items = generator.get_required_items(config)
        effective_sort = sort_by or config.cover.sort_by
        collection_type = library.get("CollectionType")
        if collection_type == "boxsets":
            include_types = "BoxSet,Movie"
        elif collection_type == "playlists":
            include_types = "Playlist,Movie,Series,Episode,Audio"
        elif collection_type == "music":
            include_types = "MusicAlbum,Audio"
        else:
            include_types = self._include_types(config, effective_sort)

        valid_items: list[dict] = []
        seen_keys: set[str] = set()
        offset = 0
        for _ in range(20):
            if stop_event.is_set():
                raise RuntimeError("Job cancelled")
            batch = client.list_items(
                parent_id=str(library["Id"]),
                include_types=include_types,
                sort_by=effective_sort,
                start_index=offset,
                limit=50,
            )
            if not batch:
                break
            for item in batch:
                image_url = self._get_image_url(client, config, item)
                if not image_url:
                    continue
                content_key = self._build_content_key(item)
                image_key = self._build_image_key(image_url)
                if (content_key and content_key in seen_keys) or (image_key and image_key in seen_keys):
                    continue
                if content_key:
                    seen_keys.add(content_key)
                if image_key:
                    seen_keys.add(image_key)
                valid_items.append(item)
                if len(valid_items) >= required_items:
                    return valid_items[:required_items]
            offset += 50
        return valid_items[:required_items]

    def _include_types(self, config: AppConfig, sort_by: str | None = None) -> str:
        effective_sort = sort_by or config.cover.sort_by
        if self._get_generator().is_single_image_style(config):
            return {
                "PremiereDate": "Movie,Series",
                "DateCreated": "Movie,Episode",
                "Random": "Movie,Series",
            }.get(effective_sort, "Movie,Series")
        if effective_sort == "DateCreated":
            return "Movie,Episode"
        return "Movie,Series"

    def _get_image_url(self, client: EmbyClient, config: AppConfig, item: dict) -> str | None:
        if config.cover.style == "text_overlay":
            return self._backdrop_first_image_url(client, item)
        if item.get("Type") in {"MusicAlbum", "Audio"}:
            if item.get("ParentBackdropImageTags"):
                return client.build_image_url(item.get("ParentBackdropItemId"), "Backdrop", item["ParentBackdropImageTags"][0], 0)
            if item.get("PrimaryImageTag"):
                return client.build_image_url(item.get("PrimaryImageItemId"), "Primary", item.get("PrimaryImageTag"))
            if item.get("AlbumPrimaryImageTag"):
                return client.build_image_url(item.get("AlbumId"), "Primary", item.get("AlbumPrimaryImageTag"))

        prefer_primary = config.cover.use_primary
        single_style = self._get_generator().is_single_image_style(config)
        if item.get("Type") == "Episode":
            if not prefer_primary and item.get("ParentBackdropImageTags"):
                return client.build_image_url(item.get("ParentBackdropItemId"), "Backdrop", item["ParentBackdropImageTags"][0], 0)
            if item.get("SeriesPrimaryImageTag"):
                return client.build_image_url(item.get("SeriesId"), "Primary", item.get("SeriesPrimaryImageTag"))
            if item.get("ParentBackdropImageTags"):
                return client.build_image_url(item.get("ParentBackdropItemId"), "Backdrop", item["ParentBackdropImageTags"][0], 0)

        if prefer_primary:
            if item.get("ImageTags", {}).get("Primary"):
                return client.build_image_url(item.get("Id"), "Primary", item["ImageTags"]["Primary"])
            if item.get("ParentBackdropImageTags"):
                return client.build_image_url(item.get("ParentBackdropItemId"), "Backdrop", item["ParentBackdropImageTags"][0], 0)
            if item.get("BackdropImageTags"):
                return client.build_image_url(item.get("Id"), "Backdrop", item["BackdropImageTags"][0], 0)
        else:
            if item.get("ParentBackdropImageTags"):
                return client.build_image_url(item.get("ParentBackdropItemId"), "Backdrop", item["ParentBackdropImageTags"][0], 0)
            if item.get("BackdropImageTags"):
                return client.build_image_url(item.get("Id"), "Backdrop", item["BackdropImageTags"][0], 0)
            if item.get("ImageTags", {}).get("Primary"):
                return client.build_image_url(item.get("Id"), "Primary", item["ImageTags"]["Primary"])
        if not single_style and item.get("ImageTags", {}).get("Primary"):
            return client.build_image_url(item.get("Id"), "Primary", item["ImageTags"]["Primary"])
        return None

    def _get_item_id(self, config: AppConfig, item: dict) -> str | None:
        if config.cover.style == "text_overlay":
            if item.get("BackdropImageTags"):
                return item.get("Id")
            if item.get("ParentBackdropImageTags"):
                return item.get("ParentBackdropItemId")
            return item.get("Id")
        if item.get("Type") in {"MusicAlbum", "Audio"}:
            if item.get("ParentBackdropImageTags"):
                return item.get("ParentBackdropItemId")
            if item.get("PrimaryImageTag"):
                return item.get("PrimaryImageItemId")
            if item.get("AlbumPrimaryImageTag"):
                return item.get("AlbumId")

        if item.get("ParentBackdropImageTags") and not config.cover.use_primary:
            return item.get("ParentBackdropItemId")
        if item.get("BackdropImageTags") or item.get("ImageTags", {}).get("Primary"):
            return item.get("Id")
        if item.get("ParentBackdropImageTags"):
            return item.get("ParentBackdropItemId")
        return None

    def _text_overlay_sort(self, config: AppConfig, library: dict) -> str | None:
        if config.cover.style != "text_overlay":
            return None
        settings = resolve_text_overlay_settings(config, str(library.get("Id") or ""))
        if settings.background_pick == "random":
            return "Random"
        return "DateCreated"

    def _uses_fixed_background(self, config: AppConfig, library: dict) -> bool:
        if config.cover.style != "text_overlay":
            return False
        settings = resolve_text_overlay_settings(config, str(library.get("Id") or ""))
        return settings.background_pick == "fixed"

    def _download_fixed_background(self, client: EmbyClient, config: AppConfig, library: dict, target: Path) -> dict | None:
        library_id = str(library.get("Id") or "")
        library_name = library.get("Name") or library_id
        fixed_id = fixed_item_id_for(config, library_id)
        if not fixed_id:
            logger.warning("媒体库「%s」选择了固定背景，但没有填写条目 ID，改为最近入库。", library_name)
            return None
        saved = self._download_item_image(client, fixed_id, target)
        if saved:
            return saved
        logger.warning("媒体库「%s」的固定背景条目 %s 不可用，改为最近入库。", library_name, fixed_id)
        return None

    def _download_item_image(self, client: EmbyClient, item_id: str, target: Path) -> dict | None:
        try:
            item = client.get_item(item_id)
        except Exception as exc:
            logger.warning("读取背景条目 %s 失败: %s", item_id, exc)
            return None
        image_url = self._backdrop_first_image_url(client, item)
        if not image_url:
            return None
        content = client.download_image(image_url)
        if not content:
            return None
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
        return {
            "source_item_id": str(item.get("Id") or item_id),
            "source_item_name": str(item.get("Name") or ""),
            "source_kind": "backdrop" if "/Images/Backdrop" in image_url else "primary",
        }

    def _acquire_text_overlay_background(
        self,
        client: EmbyClient,
        config: AppConfig,
        library: dict,
        stop_event,
        target: Path,
    ) -> dict:
        library_name = library.get("Name") or ""
        input_dir = self._get_generator().library_input_dir(config, library_name)
        local_image = self._pick_local_image(input_dir)
        if local_image is not None:
            shutil.copy(local_image, target)
            logger.info("预览使用本地素材：%s", local_image)
            return {"source_item_id": "", "source_item_name": local_image.name, "source_kind": "local"}

        if self._uses_fixed_background(config, library):
            saved = self._download_fixed_background(client, config, library, target)
            if saved:
                return saved

        sort_by = self._text_overlay_sort(config, library) or "Random"
        items = self._collect_items(client, config, library, stop_event, sort_by=sort_by, required_items=1)
        for item in items:
            if stop_event.is_set():
                raise RuntimeError("Job cancelled")
            image_url = self._backdrop_first_image_url(client, item)
            if not image_url:
                continue
            content = client.download_image(image_url)
            if not content:
                continue
            target.write_bytes(content)
            return {
                "source_item_id": str(self._get_item_id(config, item) or item.get("Id") or ""),
                "source_item_name": str(item.get("Name") or ""),
                "source_kind": "backdrop" if "/Images/Backdrop" in image_url else "primary",
            }
        raise RuntimeError(f"媒体库「{library_name}」没有可用的背景图")

    @staticmethod
    def _pick_local_image(input_dir: Path) -> Path | None:
        if not input_dir.is_dir():
            return None
        images = [
            path
            for path in sorted(input_dir.iterdir())
            if path.is_file() and path.suffix.lower() in {".jpg", ".jpeg", ".png", ".webp"}
        ]
        return images[0] if images else None

    def _backdrop_first_image_url(self, client: EmbyClient, item: dict) -> str | None:
        if item.get("BackdropImageTags"):
            return client.build_image_url(item.get("Id"), "Backdrop", item["BackdropImageTags"][0], 0)
        if item.get("ParentBackdropImageTags"):
            return client.build_image_url(
                item.get("ParentBackdropItemId"),
                "Backdrop",
                item["ParentBackdropImageTags"][0],
                0,
            )
        if item.get("Type") == "Episode" and item.get("SeriesPrimaryImageTag"):
            return client.build_image_url(item.get("SeriesId"), "Primary", item.get("SeriesPrimaryImageTag"))
        if item.get("ImageTags", {}).get("Primary"):
            return client.build_image_url(item.get("Id"), "Primary", item["ImageTags"]["Primary"])
        if item.get("PrimaryImageTag"):
            return client.build_image_url(item.get("PrimaryImageItemId"), "Primary", item.get("PrimaryImageTag"))
        if item.get("AlbumPrimaryImageTag"):
            return client.build_image_url(item.get("AlbumId"), "Primary", item.get("AlbumPrimaryImageTag"))
        return None

    @staticmethod
    def _build_content_key(item: dict) -> str | None:
        item_type = item.get("Type")
        if item_type == "Episode":
            if item.get("SeriesId"):
                return f"series:{item['SeriesId']}"
            if item.get("ParentBackdropItemId"):
                return f"parent:{item['ParentBackdropItemId']}"
        if item_type in {"MusicAlbum", "Audio"}:
            if item.get("AlbumId"):
                return f"album:{item['AlbumId']}"
            if item.get("ParentBackdropItemId"):
                return f"parent:{item['ParentBackdropItemId']}"
        if item.get("Id"):
            return f"item:{item['Id']}"
        return None

    @staticmethod
    def _build_image_key(image_url: str) -> str:
        parsed = urlparse(image_url)
        normalized_query = "&".join(
            chunk for chunk in parsed.query.split("&") if chunk and not chunk.startswith("api_key=")
        )
        return f"img:{parsed.path}?{normalized_query}"
