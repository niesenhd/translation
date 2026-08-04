"""FastAPI 入口。"""
import logging

from fastapi import Depends, FastAPI
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.api.admin import router as admin_router
from app.api.auth import router as auth_router
from app.api.tasks import router as tasks_router
from app.api.terms import router as terms_router
from app.api.tm import router as tm_router
from app.api.feedback import router as feedback_router
from app.api.model_configs import router as model_configs_router
from app.api.sso import router as sso_router
from app.core.config import get_settings
from app.core.database import get_db

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

app = FastAPI(title="文档翻译系统", version="0.1.0")
settings = get_settings()

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origin_list,
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
app.include_router(auth_router, prefix="/api")
app.include_router(admin_router, prefix="/api")
app.include_router(terms_router, prefix="/api")
app.include_router(tm_router, prefix="/api")
app.include_router(feedback_router, prefix="/api")
app.include_router(model_configs_router, prefix="/api")
app.include_router(sso_router, prefix="/api")
