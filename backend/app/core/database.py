"""SQLAlchemy 数据库连接与会话管理。"""
from collections.abc import Iterator

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.core.config import get_settings

_settings = get_settings()

# 连接池显式调大：翻译任务在进度回调里会用独立 Session 写库/查存活，
# 叠加段落级并发 + 多任务并发时，默认池(5+10)极易耗尽导致 QueuePool timeout。
# pool_recycle 防止长连接被 PG/中间件断开后复用报错。
engine = create_engine(
    _settings.database_url,
    pool_pre_ping=True,
    pool_size=20,
    max_overflow=20,
    pool_recycle=1800,
    pool_timeout=30,
    future=True,
)
SessionLocal = sessionmaker(bind=engine, autocommit=False, autoflush=False, future=True)


class Base(DeclarativeBase):
    """所有 ORM 模型的基类。"""


def get_db() -> Iterator[Session]:
    """FastAPI 依赖注入：每个请求一个 Session。"""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
