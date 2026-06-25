"""FastAPI 入口。"""
import logging

from fastapi import Depends, FastAPI
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.api.admin import router as admin_router
from app.api.tasks import router as tasks_router
from app.api.terms import router as terms_router
from app.api.tm import router as tm_router
from app.api.feedback import router as feedback_router
from app.api.model_configs import router as model_configs_router
from app.core.database import Base, SessionLocal, engine, get_db
# 显式 import 让 Base.metadata 包含所有 model 表
from app.models import system_config as _system_config  # noqa: F401
from app.models import task as _task  # noqa: F401
from app.models import term as _term  # noqa: F401
from app.models import translation_memory as _translation_memory  # noqa: F401
from app.models import feedback as _feedback  # noqa: F401
from app.models import model_config as _model_config  # noqa: F401

# 统一日志配置：所有模块共享同一格式和级别
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
# 第三方库降噪
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("openai").setLevel(logging.WARNING)
logging.getLogger("celery").setLevel(logging.INFO)
logging.getLogger("urllib3").setLevel(logging.WARNING)

# P0 阶段：直接 create_all；后续接入 Alembic 迁移
Base.metadata.create_all(bind=engine)


def _ensure_columns() -> None:
    """对已存在的表做幂等的列补齐（Alembic 上线前的过渡方案）。

    create_all 不会在已有表上加新列，必须手动 ALTER。
    """
    statements = [
        # 2026-06-13: 新增 pdf_output_format（PDF 输出格式选择）
        "ALTER TABLE translation_tasks "
        "ADD COLUMN IF NOT EXISTS pdf_output_format VARCHAR(8) NOT NULL DEFAULT 'pdf'",
        # 2026-06-14: 新增 translate_images（是否翻译图片中的文字）
        # 默认 'no'：与需求 2.2 默认项「仅翻译文档文字」及模型 server_default 一致
        "ALTER TABLE translation_tasks "
        "ADD COLUMN IF NOT EXISTS translate_images VARCHAR(8) NOT NULL DEFAULT 'no'",
    ]
    with SessionLocal() as session:
        for sql in statements:
            try:
                session.execute(text(sql))
                session.commit()
            except Exception:
                session.rollback()


_ensure_columns()

app = FastAPI(title="法律文档翻译系统", version="0.1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/health")
def health(db: Session = Depends(get_db)) -> dict:
    """健康检查：验证数据库连通性。"""
    checks: dict = {"status": "ok"}
    try:
        db.execute(text("SELECT 1"))
        checks["database"] = "ok"
    except Exception as e:
        checks["status"] = "degraded"
        checks["database"] = f"error: {str(e)[:100]}"
    return checks


app.include_router(tasks_router, prefix="/api")
app.include_router(admin_router, prefix="/api")
app.include_router(terms_router, prefix="/api")
app.include_router(tm_router, prefix="/api")
app.include_router(feedback_router, prefix="/api")
app.include_router(model_configs_router, prefix="/api")
