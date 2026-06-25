FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple/ \
    PIP_TRUSTED_HOST=pypi.tuna.tsinghua.edu.cn

WORKDIR /app

# 系统依赖：python-docx 不需要额外编译，pdf2docx / PyMuPDF 需要 libgl 等
# LibreOffice：用于 .doc → .docx / .ppt → .pptx 转换
# fonts-wqy-zenhei：CJK 字体，图片 OCR 文字替换时使用
RUN sed -i 's|http://deb.debian.org|https://mirrors.tuna.tsinghua.edu.cn|g; s|http://security.debian.org|https://mirrors.tuna.tsinghua.edu.cn/debian-security|g' /etc/apt/sources.list.d/debian.sources \
    && apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
        libgl1 \
        libglib2.0-0 \
        curl \
        libreoffice-writer \
        libreoffice-impress \
        libreoffice-common \
        fonts-wqy-zenhei \
        fonts-noto-cjk \
    && rm -rf /var/lib/apt/lists/*

# 复制全部源码后再安装（非 editable 模式需要源码存在）
COPY . /app/
RUN pip install --upgrade pip && pip install .

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
