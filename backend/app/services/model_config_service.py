"""模型运行时配置读取；服务层不依赖 API 路由模块。"""
from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select

from app.core.config import Settings, get_settings
from app.core.crypto import decrypt_secret
from app.core.database import SessionLocal
from app.models.model_config import ModelConfig, ModelType
from app.models.system_config import SystemConfig

KEY_TRANSLATION_MODEL = "translation_model"
KEY_VL_MODEL = "vl_model"
KEY_API_BASE_URL = "api_base_url"
KEY_API_KEY = "api_key"


@dataclass(frozen=True)
class RuntimeModelConfig:
    model_id: str
    api_base_url: str
    api_key: str


def get_runtime_model_config(model_type: ModelType, settings: Settings | None = None) -> RuntimeModelConfig:
    cfg = settings or get_settings()
    with SessionLocal() as db:
        active = db.scalar(
            select(ModelConfig).where(
                ModelConfig.model_type == model_type,
                ModelConfig.is_active.is_(True),
            )
        )
        if active is not None:
            return RuntimeModelConfig(
                model_id=active.model_id,
                api_base_url=active.api_base_url,
                api_key=decrypt_secret(active.api_key, cfg),
            )
        legacy_rows = list(
            db.scalars(
                select(SystemConfig).where(
                    SystemConfig.key.in_(
                        [KEY_TRANSLATION_MODEL, KEY_VL_MODEL, KEY_API_BASE_URL, KEY_API_KEY]
                    )
                )
            )
        )
    legacy = {row.key: row.value for row in legacy_rows}
    encrypted_or_plain = legacy.get(KEY_API_KEY, cfg.dashscope_api_key)
    model_key = KEY_VL_MODEL if model_type == ModelType.VL else KEY_TRANSLATION_MODEL
    default_model = cfg.dashscope_vl_model if model_type == ModelType.VL else cfg.dashscope_model
    return RuntimeModelConfig(
        model_id=legacy.get(model_key, default_model),
        api_base_url=legacy.get(KEY_API_BASE_URL, cfg.dashscope_base_url),
        api_key=decrypt_secret(encrypted_or_plain, cfg),
    )
