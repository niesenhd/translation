"""多模型配置管理接口。"""
from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import select, update

from app.core.database import SessionLocal
from app.core.security import CurrentUser, require_admin
from app.models.model_config import ModelConfig, ModelType

router = APIRouter(prefix="/admin/models", tags=["model-config"], dependencies=[Depends(require_admin)])


# ── Schemas ──────────────────────────────────────────────────────────

class ModelConfigCreate(BaseModel):
    name: str = Field(max_length=128)
    model_type: ModelType
    model_id: str = Field(max_length=256)
    api_base_url: str = Field(max_length=512)
    api_key: str
    is_active: bool = False


class ModelConfigUpdate(BaseModel):
    name: str | None = Field(default=None, max_length=128)
    model_type: ModelType | None = None
    model_id: str | None = Field(default=None, max_length=256)
    api_base_url: str | None = Field(default=None, max_length=512)
    api_key: str | None = None
    is_active: bool | None = None


class ModelConfigRead(BaseModel):
    id: str
    name: str
    model_type: str
    model_id: str
    api_base_url: str
    api_key_set: bool  # 不返回实际 key
    is_active: bool
    created_at: str
    updated_at: str


# ── CRUD ─────────────────────────────────────────────────────────────

@router.get("", response_model=list[ModelConfigRead])
def list_models():
    db = SessionLocal()
    try:
        items = list(db.scalars(select(ModelConfig).order_by(ModelConfig.model_type, ModelConfig.name)))
        return [
            ModelConfigRead(
                id=m.id,
                name=m.name,
                model_type=m.model_type.value,
                model_id=m.model_id,
                api_base_url=m.api_base_url,
                api_key_set=bool(m.api_key),
                is_active=m.is_active,
                created_at=m.created_at.isoformat() if m.created_at else "",
                updated_at=m.updated_at.isoformat() if m.updated_at else "",
            )
            for m in items
        ]
    finally:
        db.close()


@router.post("", response_model=ModelConfigRead, status_code=status.HTTP_201_CREATED)
def create_model(payload: ModelConfigCreate, user: CurrentUser = Depends(require_admin)):
    db = SessionLocal()
    try:
        # 如果设为 active，先取消同类型的其他 active
        if payload.is_active:
            db.execute(
                update(ModelConfig)
                .where(ModelConfig.model_type == payload.model_type, ModelConfig.is_active == True)
                .values(is_active=False)
            )

        entry = ModelConfig(
            id=str(uuid.uuid4()),
            name=payload.name,
            model_type=payload.model_type,
            model_id=payload.model_id,
            api_base_url=payload.api_base_url,
            api_key=payload.api_key,
            is_active=payload.is_active,
            updated_by=user.username,
        )
        db.add(entry)
        db.commit()
        db.refresh(entry)

        # 如果激活了模型，重置翻译器
        if payload.is_active:
            _reset_translators()

        return ModelConfigRead(
            id=entry.id,
            name=entry.name,
            model_type=entry.model_type.value,
            model_id=entry.model_id,
            api_base_url=entry.api_base_url,
            api_key_set=bool(entry.api_key),
            is_active=entry.is_active,
            created_at=entry.created_at.isoformat() if entry.created_at else "",
            updated_at=entry.updated_at.isoformat() if entry.updated_at else "",
        )
    finally:
        db.close()


@router.put("/{model_id}", response_model=ModelConfigRead)
def update_model(model_id: str, payload: ModelConfigUpdate, user: CurrentUser = Depends(require_admin)):
    db = SessionLocal()
    try:
        entry = db.get(ModelConfig, model_id)
        if entry is None:
            raise HTTPException(status_code=404, detail="模型配置不存在")

        was_active = entry.is_active

        for field, value in payload.model_dump(exclude_unset=True).items():
            setattr(entry, field, value)
        entry.updated_by = user.username

        # 如果设为 active，取消同类型的其他 active
        if payload.is_active and not was_active:
            db.execute(
                update(ModelConfig)
                .where(
                    ModelConfig.model_type == entry.model_type,
                    ModelConfig.is_active == True,
                    ModelConfig.id != model_id,
                )
                .values(is_active=False)
            )

        db.commit()
        db.refresh(entry)

        # 如果激活状态变化，重置翻译器
        if payload.is_active is not None and payload.is_active != was_active:
            _reset_translators()

        return ModelConfigRead(
            id=entry.id,
            name=entry.name,
            model_type=entry.model_type.value,
            model_id=entry.model_id,
            api_base_url=entry.api_base_url,
            api_key_set=bool(entry.api_key),
            is_active=entry.is_active,
            created_at=entry.created_at.isoformat() if entry.created_at else "",
            updated_at=entry.updated_at.isoformat() if entry.updated_at else "",
        )
    finally:
        db.close()


@router.delete("/{model_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_model(model_id: str):
    db = SessionLocal()
    try:
        entry = db.get(ModelConfig, model_id)
        if entry is None:
            raise HTTPException(status_code=404, detail="模型配置不存在")
        was_active = entry.is_active
        db.delete(entry)
        db.commit()
        if was_active:
            _reset_translators()
    finally:
        db.close()


# ── 获取当前激活模型 ────────────────────────────────────────────────

class ActiveModelsRead(BaseModel):
    translation: ModelConfigRead | None = None
    vl: ModelConfigRead | None = None


@router.get("/active", response_model=ActiveModelsRead)
def get_active_models():
    db = SessionLocal()
    try:
        trans = db.scalar(
            select(ModelConfig).where(ModelConfig.model_type == ModelType.TRANSLATION, ModelConfig.is_active == True)
        )
        vl = db.scalar(
            select(ModelConfig).where(ModelConfig.model_type == ModelType.VL, ModelConfig.is_active == True)
        )
        return ActiveModelsRead(
            translation=_to_read(trans) if trans else None,
            vl=_to_read(vl) if vl else None,
        )
    finally:
        db.close()


# ── 辅助 ─────────────────────────────────────────────────────────────

def _to_read(m: ModelConfig) -> ModelConfigRead:
    return ModelConfigRead(
        id=m.id,
        name=m.name,
        model_type=m.model_type.value,
        model_id=m.model_id,
        api_base_url=m.api_base_url,
        api_key_set=bool(m.api_key),
        is_active=m.is_active,
        created_at=m.created_at.isoformat() if m.created_at else "",
        updated_at=m.updated_at.isoformat() if m.updated_at else "",
    )


def _reset_translators():
    """重置翻译器与 VL 模型客户端单例，使新配置生效。"""
    from app.services.translator import reset_translator
    from app.services.ocr import reset_ocr_client
    reset_translator()
    reset_ocr_client()


def get_active_translation_config() -> ModelConfig | None:
    """获取当前激活的翻译模型配置。"""
    db = SessionLocal()
    try:
        return db.scalar(
            select(ModelConfig).where(ModelConfig.model_type == ModelType.TRANSLATION, ModelConfig.is_active == True)
        )
    finally:
        db.close()


def get_active_vl_config() -> ModelConfig | None:
    """获取当前激活的 VL 模型配置。"""
    db = SessionLocal()
    try:
        return db.scalar(
            select(ModelConfig).where(ModelConfig.model_type == ModelType.VL, ModelConfig.is_active == True)
        )
    finally:
        db.close()
