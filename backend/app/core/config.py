"""统一配置（基于 pydantic-settings 读取环境变量）。"""
import base64
import logging
from functools import lru_cache
from urllib.parse import urlsplit

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

logger = logging.getLogger(__name__)

# 已知的不安全默认/占位值：这些值一旦出现在生产就等于没有密钥保护
_INSECURE_SECRET_KEYS = {"", "change-me", "change-me-to-a-random-secret"}
_INSECURE_ADMIN_TOKENS = {"", "admin-dev-token"}
_INSECURE_DATA_KEYS = {"", "change-me", "change-me-to-a-random-secret"}
_INSECURE_PASSWORDS = {"", "change-me", "translation", "postgres", "redis", "minioadmin"}


def _is_valid_fernet_key(value: str) -> bool:
    """Fernet 密钥必须是 URL-safe base64 编码的 32 字节随机值。"""
    try:
        decoded = base64.b64decode(value.encode("ascii"), altchars=b"-_", validate=True)
    except (ValueError, UnicodeEncodeError):
        return False
    return len(decoded) == 32


def _is_absolute_http_url(value: str) -> bool:
    try:
        parsed = urlsplit(value)
    except ValueError:
        return False
    return (
        parsed.scheme in ("http", "https")
        and bool(parsed.netloc)
        and parsed.username is None
        and parsed.password is None
        and not parsed.query
        and not parsed.fragment
    )


def _is_http_origin(value: str) -> bool:
    if not _is_absolute_http_url(value):
        return False
    parsed = urlsplit(value)
    return parsed.path in ("", "/")


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # 应用
    app_env: str = "development"
    app_secret_key: str = "change-me"
    app_admin_token: str = "admin-dev-token"
    # 浏览器可访问的绝对根地址（SSO 跳转使用），例如 https://translation.example.com
    public_base_url: str = ""
    # 允许跨域访问 API 的浏览器来源，逗号分隔；生产环境禁止通配符。
    cors_origins: str = "http://localhost:8080,http://localhost:5173"
    # 应用数据字段加密密钥：Fernet.generate_key() 生成的 32 字节 URL-safe base64
    app_data_encryption_key: str = Field(default="", repr=False)

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

    # 律智荟 OA 对接（P3）
    oa_base_url: str = "https://e.tylaw.com.cn"
    oa_app_key: str = ""
    oa_app_secret: str = ""
    # SSO 签名密钥（与律智荟约定的专用密钥）
    oa_sso_secret: str = ""

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

    @property
    def cors_origin_list(self) -> list[str]:
        return [origin.strip().rstrip("/") for origin in self.cors_origins.split(",") if origin.strip()]

    @model_validator(mode="after")
    def _guard_insecure_secrets(self):
        """生产环境拒绝以默认/占位密钥启动，避免可伪造任意用户的签名 token。

        - app_secret_key 是 HMAC 登录 token 的签名密钥（见 core/security.py）；
          若为默认值，攻击者可离线伪造任意 admin token 绕过认证。
        - 判定生产：app_env in {production, prod}。开发环境仅告警不阻断。
        """
        is_prod = self.app_env.strip().lower() in ("production", "prod")
        secret_insecure = self.app_secret_key.strip() in _INSECURE_SECRET_KEYS
        admin_insecure = self.app_admin_token.strip() in _INSECURE_ADMIN_TOKENS
        data_key = self.app_data_encryption_key.strip()
        data_key_insecure = data_key in _INSECURE_DATA_KEYS
        public_base_url = self.public_base_url.strip().rstrip("/")
        cors_origins = self.cors_origin_list

        if data_key and not _is_valid_fernet_key(data_key):
            raise ValueError(
                "APP_DATA_ENCRYPTION_KEY 格式无效；请使用 "
                "`python -c \"from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())\"` 生成"
            )
        if public_base_url and not _is_absolute_http_url(public_base_url):
            raise ValueError("PUBLIC_BASE_URL 必须是不含认证信息、query 或 fragment 的绝对 http(s) URL")
        invalid_origins = [origin for origin in cors_origins if origin != "*" and not _is_http_origin(origin)]
        if invalid_origins:
            raise ValueError("CORS_ORIGINS 只能包含逗号分隔的绝对 http(s) 来源（不得含路径）")

        self.public_base_url = public_base_url
        self.app_data_encryption_key = data_key
        self.cors_origins = ",".join(cors_origins)

        if is_prod:
            problems = []
            if secret_insecure:
                problems.append("APP_SECRET_KEY 仍为默认/占位值")
            if admin_insecure:
                problems.append("APP_ADMIN_TOKEN 仍为默认值")
            if data_key_insecure:
                problems.append("APP_DATA_ENCRYPTION_KEY 未设置或仍为占位值")
            if not public_base_url:
                problems.append("PUBLIC_BASE_URL 未设置")
            if not cors_origins or "*" in cors_origins:
                problems.append("CORS_ORIGINS 必须是非空且不含通配符的来源白名单")
            if len(self.app_secret_key.strip()) < 32:
                problems.append("APP_SECRET_KEY 长度必须至少 32 字符")
            if len(self.app_admin_token.strip()) < 24:
                problems.append("APP_ADMIN_TOKEN 长度必须至少 24 字符")
            if self.postgres_password.strip().lower() in _INSECURE_PASSWORDS:
                problems.append("POSTGRES_PASSWORD 未设置或仍为默认弱口令")
            if self.redis_password.strip().lower() in _INSECURE_PASSWORDS:
                problems.append("REDIS_PASSWORD 未设置或仍为默认弱口令")
            if self.minio_access_key.strip().lower() in _INSECURE_PASSWORDS:
                problems.append("MINIO_ACCESS_KEY 未设置或仍为默认值")
            if self.minio_secret_key.strip().lower() in _INSECURE_PASSWORDS or len(self.minio_secret_key.strip()) < 16:
                problems.append("MINIO_SECRET_KEY 未设置、过短或仍为默认弱口令")
            if problems:
                raise ValueError(
                    "检测到不安全配置（APP_ENV=production）：" + "；".join(problems)
                    + "。请在 deploy/.env 改为随机强值（如 `openssl rand -hex 32`）后重启。"
                )
        else:
            if secret_insecure:
                logger.warning(
                    "APP_SECRET_KEY 使用默认/占位值，仅限开发测试；生产部署（APP_ENV=production）"
                    "前务必改为随机强值，否则登录 token 可被伪造。"
                )
            if data_key_insecure:
                logger.warning(
                    "APP_DATA_ENCRYPTION_KEY 未设置；涉及敏感字段加解密的功能将不可用。"
                )
            if self.oa_sso_secret and not public_base_url:
                logger.warning("已配置 OA_SSO_SECRET，但 PUBLIC_BASE_URL 未设置，SSO 跳转将不可用。")
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
