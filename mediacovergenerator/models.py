from __future__ import annotations

from datetime import datetime, timezone
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


SortBy = Literal["Random", "PremiereDate", "DateCreated"]
CoverStyle = Literal[
    "static_1",
    "static_2",
    "static_3",
    "text_overlay",
]
BackgroundPick = Literal["random", "latest", "fixed"]


class EmbySettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = "emby"
    base_url: str = ""
    api_key: str = ""
    verify_ssl: bool = True
    timeout_seconds: int = 30


class PathsSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    data_dir: str = "data"
    cache_dir: str = "data/cache"
    covers_input_dir: str = "data/input"
    recent_covers_dir: str = "data/recent_covers"
    fonts_dir: str = "data/fonts"
    text_layers_dir: str = "data/text_layers"


class ScheduleSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    cron: str = "0 3 * * *"


class WebhookSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    token: str = ""
    delay_seconds: int = 60


class TextOverlaySettings(BaseModel):
    """文字层叠加的全局效果。各库可以只覆盖其中一部分。"""

    model_config = ConfigDict(extra="forbid")

    darken: float = 0.45
    blur_radius: int = 18
    saturation: float = 0.85
    vignette: float = 0.40
    text_scale: float = 1.0
    text_offset_x: float = 0.0
    text_offset_y: float = 0.0
    background_pick: BackgroundPick = "random"

    @field_validator("darken", "vignette")
    @classmethod
    def clamp_unit_interval(cls, value: float) -> float:
        return min(1.0, max(0.0, float(value)))

    @field_validator("saturation")
    @classmethod
    def clamp_saturation(cls, value: float) -> float:
        return min(2.0, max(0.0, float(value)))

    @field_validator("blur_radius")
    @classmethod
    def clamp_blur_radius(cls, value: int) -> int:
        return min(40, max(0, int(value)))

    @field_validator("text_scale")
    @classmethod
    def clamp_text_scale(cls, value: float) -> float:
        return min(1.5, max(0.2, float(value)))

    @field_validator("text_offset_x", "text_offset_y")
    @classmethod
    def clamp_text_offset(cls, value: float) -> float:
        return min(50.0, max(-50.0, float(value)))


class LibraryTextOverlayOverride(BaseModel):
    """单个媒体库对文字层叠加的覆盖项。enabled 为 false 时忽略数值，仍保留固定条目 ID。"""

    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    darken: float | None = None
    blur_radius: int | None = None
    saturation: float | None = None
    vignette: float | None = None
    text_scale: float | None = None
    text_offset_x: float | None = None
    text_offset_y: float | None = None
    background_pick: BackgroundPick | None = None
    fixed_item_id: str = ""
    builtin_layer: str = ""

    @field_validator(
        "darken",
        "blur_radius",
        "saturation",
        "vignette",
        "text_scale",
        "text_offset_x",
        "text_offset_y",
        "background_pick",
        mode="before",
    )
    @classmethod
    def empty_as_none(cls, value):
        if value is None or value == "":
            return None
        return value

    @field_validator("darken", "vignette")
    @classmethod
    def clamp_optional_unit(cls, value: float | None) -> float | None:
        if value is None:
            return None
        return min(1.0, max(0.0, float(value)))

    @field_validator("saturation")
    @classmethod
    def clamp_optional_saturation(cls, value: float | None) -> float | None:
        if value is None:
            return None
        return min(2.0, max(0.0, float(value)))

    @field_validator("blur_radius")
    @classmethod
    def clamp_optional_blur(cls, value: int | None) -> int | None:
        if value is None:
            return None
        return min(40, max(0, int(value)))

    @field_validator("text_scale")
    @classmethod
    def clamp_optional_scale(cls, value: float | None) -> float | None:
        if value is None:
            return None
        return min(1.5, max(0.2, float(value)))

    @field_validator("text_offset_x", "text_offset_y")
    @classmethod
    def clamp_optional_offset(cls, value: float | None) -> float | None:
        if value is None:
            return None
        return min(50.0, max(-50.0, float(value)))

    @field_validator("fixed_item_id")
    @classmethod
    def strip_item_id(cls, value: str) -> str:
        return str(value or "").strip()

    @field_validator("builtin_layer")
    @classmethod
    def strip_builtin_layer(cls, value: str) -> str:
        text = str(value or "").strip()
        if "/" in text or "\\" in text or ".." in text:
            return ""
        return text[:64]


class CoverSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    style: CoverStyle = "static_1"
    sort_by: SortBy = "Random"
    use_primary: bool = True
    resolution: str = "480p"
    custom_width: int = 1920
    custom_height: int = 1080
    zh_font_preset: str = "chaohei"
    en_font_preset: str = "EmblemaOne"
    zh_font_custom: str = ""
    en_font_custom: str = ""
    zh_font_size: float = 170
    en_font_size: float = 75
    zh_font_offset: float = 0
    title_spacing: float = 40
    en_line_spacing: float = 40
    title_scale: float = 1.0
    blur_size: int = 50
    color_ratio: float = 0.8
    multi_1_blur: bool = True
    bg_color_mode: Literal["auto", "custom", "config"] = "auto"
    custom_bg_color: str = ""
    save_recent_covers: bool = True
    covers_history_limit_per_library: int = 10
    text_overlay: TextOverlaySettings = Field(default_factory=TextOverlaySettings)


class LibraryTitleConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    library_id: str = ""
    library_name: str = ""
    zh_title: str = ""
    en_title: str = ""
    bg_color: str = ""
    text_overlay: LibraryTextOverlayOverride | None = None


class AppConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    emby: EmbySettings = Field(default_factory=EmbySettings)
    paths: PathsSettings = Field(default_factory=PathsSettings)
    schedule: ScheduleSettings = Field(default_factory=ScheduleSettings)
    webhook: WebhookSettings = Field(default_factory=WebhookSettings)
    cover: CoverSettings = Field(default_factory=CoverSettings)
    selected_library_ids: list[str] = Field(default_factory=list)
    library_titles: list[LibraryTitleConfig] = Field(default_factory=list)
    titles_yaml: str = Field(
        default=(
            "# 示例\n"
            "\"电影\": [\"电影\", \"MOVIES\"]\n"
            "\"电视剧\": [\"剧集\", \"SERIES\", \"#24415a\"]\n"
        )
    )


class LibraryInfo(BaseModel):
    id: str
    name: str
    collection_type: str = ""


class GenerateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    library_ids: list[str] = Field(default_factory=list)


class DeleteRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ids: list[str] = Field(default_factory=list)


class PreviewRequest(BaseModel):
    """用当前页面上的参数预览一张文字层封面，不写回 Emby。"""

    model_config = ConfigDict(extra="forbid")

    text_overlay: TextOverlaySettings | None = None
    library_text_overlay: LibraryTextOverlayOverride | None = None
    resolution: str | None = None
    custom_width: int | None = None
    custom_height: int | None = None


class PreviewResponse(BaseModel):
    image_base64: str
    content_type: str
    source_item_id: str = ""
    source_item_name: str = ""
    source_kind: str = ""
    width: int = 0
    height: int = 0
    text_layer_source: str = ""
    text_layer_name: str = ""
    text_layer_label: str = ""


class TextLayerResolveItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    library_id: str
    library_name: str = ""
    builtin_layer: str = ""


class TextLayerResolveRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    items: list[TextLayerResolveItem] = Field(default_factory=list)


class TextLayerStatus(BaseModel):
    library_id: str
    source: Literal["custom", "builtin", "auto", "none"]
    name: str = ""
    label: str = ""


class HistoryRecord(BaseModel):
    id: str
    server: str
    library_id: str
    library_name: str
    source_item_ids: list[str] = Field(default_factory=list)
    saved_path: str | None = None
    style: str
    created_at: datetime

    @field_validator("created_at", mode="after")
    @classmethod
    def ensure_created_at_utc(cls, value: datetime) -> datetime:
        return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


class JobSummary(BaseModel):
    id: str
    status: Literal["pending", "running", "completed", "failed", "cancelled"]
    created_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None
    title: str = ""
    message: str = ""
    library_ids: list[str] = Field(default_factory=list)
    library_names: list[str] = Field(default_factory=list)
    total_libraries: int = 0
    completed_libraries: int = 0
    failed_libraries: int = 0
    cancel_requested: bool = False
    errors: list[str] = Field(default_factory=list)

    @field_validator("created_at", "started_at", "finished_at", mode="after")
    @classmethod
    def ensure_timestamps_utc(cls, value: datetime | None) -> datetime | None:
        if value is None or value.tzinfo is not None:
            return value
        return value.replace(tzinfo=timezone.utc)


class HealthResponse(BaseModel):
    status: Literal["ok", "degraded"]
    configured: bool
    emby_reachable: bool
    active_jobs: int
