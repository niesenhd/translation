FROM python:3.11.9-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    HOME=/home/app \
    PIP_NO_CACHE_DIR=1 \
    PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple/ \
    PIP_TRUSTED_HOST=pypi.tuna.tsinghua.edu.cn

WORKDIR /app

# 系统依赖：python-docx 不需要额外编译，pdf2docx / PyMuPDF 需要 libgl 等
# LibreOffice：用于 .doc → .docx / .ppt → .pptx 转换
# fonts-wqy-zenhei / fonts-noto-cjk：CJK 字体，图片 OCR 文字替换时使用
# fonts-noto-core：含 NotoNaskhArabic / NotoSansArabic 等，PDF 阿语等 RTL 脚本写回需要
RUN sed -i 's|http://deb.debian.org|https://mirrors.tuna.tsinghua.edu.cn|g; s|http://security.debian.org|https://mirrors.tuna.tsinghua.edu.cn/debian-security|g' /etc/apt/sources.list.d/debian.sources \
    && apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
        libgl1 \
        libglib2.0-0 \
        curl \
        libreoffice-writer \
        libreoffice-impress \
        libreoffice-calc \
        libreoffice-common \
        fonts-wqy-zenhei \
        fonts-noto-cjk \
        fonts-noto-core \
    && rm -rf /var/lib/apt/lists/*

RUN groupadd --gid 10001 app \
    && useradd --uid 10001 --gid 10001 --create-home --shell /usr/sbin/nologin app

# 锁文件是唯一依赖来源；--frozen 保证构建不会静默重新解析版本。
RUN pip install --upgrade pip && pip install uv==0.11.8
COPY pyproject.toml uv.lock /app/
RUN uv sync --frozen --no-install-project

COPY app /app/app
COPY alembic /app/alembic
COPY alembic.ini /app/alembic.ini
COPY scripts /app/scripts
RUN uv sync --frozen

ENV PATH="/app/.venv/bin:${PATH}"

RUN mkdir -p /home/app/.paddlex /home/app/.cache /tmp/libreoffice \
    && chown -R app:app /home/app /tmp/libreoffice

USER app

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]

# 仅在服务器显式 `--target test` 时构建；生产默认 runtime 不包含测试源码。
FROM base AS test
USER root
COPY tests /app/tests
RUN uv sync --frozen --extra dev
USER app
CMD ["python", "-m", "pytest", "-q", "-p", "no:cacheprovider"]

FROM base AS runtime
