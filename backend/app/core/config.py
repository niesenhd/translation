"""统一配置（基于 pydantic-settings 读取环境变量）。"""
from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # 应用
    app_env: str = "development"
    app_secret_key: str = "change-me"
    app_admin_token: str = "admin-dev-token"

    # PostgreSQL
    postgres_host: str = "localhost"
    postgres_port: int = 5432
    postgres_db: str = "translation"
    postgres_user: str = "translation"
    postgres_password: str = "translation"

    # Redis
    redis_host: str = "localhost"
    redis_port: int = 6379
    # Redis 密码（生产必须设置——broker 里能注入任意 Celery 任务）
    redis_password: str = ""

    # MinIO
    minio_endpoint: str = "localhost:9000"
    minio_access_key: str = "minioadmin"
    minio_secret_key: str = "minioadmin"
    minio_bucket: str = "translation"
    minio_secure: bool = False

    # Qwen / DashScope
    dashscope_api_key: str = ""
    dashscope_base_url: str = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    dashscope_model: str = "qwen3.6-27b"
    # 多模态 OCR 模型：用于图片 OCR + 翻译一体化（默认 qwen-vl-plus，可换 qwen-vl-max）
    dashscope_vl_model: str = "qwen-vl-plus"
    # 是否启用图片 OCR：关闭后所有 ocr 调用直接返回空（节省 token）
    enable_image_ocr: bool = True
    # 图片 OCR 最小边长（px）：过小图片视为装饰直接跳过
    ocr_min_image_size: int = 64

    # 翻译
    translation_max_concurrency: int = 4
    # 文件总字符数超过此阈值则触发"分片调度"
    # （单段独立调用模型不变，分片只是组织调度的最小单元）
    translation_large_file_threshold: int = 30_000
    # 单文件内段落级并发数（线程池），用于提升单文件翻译速度
    # 注意：过高会导致 DashScope API 限流（429），建议 2-3
    translation_paragraph_concurrency: int = 3
    # 分片相关：单分片最大字符数 / 末分片合并下限
    translation_chunk_max_chars: int = 6_000
    translation_chunk_min_chars: int = 1_500
    # 文档级术语抽取（功能B）：翻译前一次调用抽取全文关键术语，保证定义术语全文统一译法
    translation_doc_term_extraction: bool = True
    # 文件保留天数：默认 180 天，0 表示永不过期；管理员可通过 system_config 表动态调整
    file_retention_days: int = 180

    @property
    def database_url(self) -> str:
        return (
            f"postgresql+psycopg://{self.postgres_user}:{self.postgres_password}"
            f"@{self.postgres_host}:{self.postgres_port}/{self.postgres_db}"
        )

    @property
    def redis_url(self) -> str:
        if self.redis_password:
            return f"redis://:{self.redis_password}@{self.redis_host}:{self.redis_port}/0"
        return f"redis://{self.redis_host}:{self.redis_port}/0"


@lru_cache
def get_settings() -> Settings:
    return Settings()
