# 法律文档翻译系统

面向律所内部的多格式文档翻译系统（私有化部署 / 纯内网）。

> 详见 [需求总结](./法律文档翻译软件%20—%20需求总结.md)

## 目录结构

```
translation/
├── backend/              # FastAPI 后端
│   ├── app/
│   │   ├── api/          # 路由
│   │   ├── core/         # 配置、鉴权、数据库
│   │   ├── models/       # ORM 模型
│   │   ├── schemas/      # Pydantic schema
│   │   ├── services/     # 业务逻辑（翻译引擎、文件处理、OA 对接）
│   │   ├── tasks/        # Celery 任务
│   │   └── main.py
│   ├── tests/
│   ├── pyproject.toml
│   └── Dockerfile
├── frontend/             # Vue 3 + Element Plus
│   ├── src/
│   ├── package.json
│   └── Dockerfile
├── deploy/
│   ├── docker-compose.yml
│   ├── nginx.conf
│   └── .env.example
└── README.md
```

## 当前阶段：P2（核心功能已完成）

- [x] 项目骨架 + docker-compose（PostgreSQL / Redis / MinIO）
- [x] FastAPI 后端骨架（admin Token 鉴权）
- [x] 多格式翻译：DOCX / PDF / TXT / MD / XLSX / XLS / CSV / PPTX / PPT
- [x] Word 脚注 (footnotes) + 尾注 (endnotes) 翻译（绕过 python-docx 限制，直接操作 OOXML）
- [x] Qwen（DashScope）官方 API 接入 + 管理后台模型配置
- [x] Vue 3 前端（登录 / 上传 / 任务列表 / 下载 / 反馈）
- [x] 术语库管理（手动录入 / Excel·CSV·SDLTB 导入 / 双向匹配 / 优先级 / **按段落动态过滤**）
- [x] 翻译记忆库（TM）模糊匹配
- [x] 图片 OCR + 就地替换文字（PaddleOCR 3.x + PP-Structure，VL 降级）
- [x] PDF 扫描件整页 OCR（PaddleOCR 优先，VL 降级）
- [x] 超大文件分片调度 + 段落级并发（默认 3 路，避免 API 限流）
- [x] 管理员后台（统计看板 / 模型配置 / 并发控制 / 文件保留策略）
- [x] 阿拉伯语 RTL 排版
- [x] 并发闸门安全（Worker 启动自动重置计数器，防泄漏）
- [x] API 限流防护（SDK 重试禁用 + 应用层指数退避 5-60s）
- [x] 400 拒绝预检（纯数字/符号段落跳过，不浪费 API 调用）

## 快速启动

```bash
# 1. 复制环境变量
cp deploy/.env.example deploy/.env
# 编辑 deploy/.env，填入 DASHSCOPE_API_KEY

# 2. 启动基础设施 + 服务
cd deploy
docker compose up -d

# 3. 访问
# 前端: http://localhost:8080
# 后端 API: http://localhost:8000/docs
# MinIO Console: http://localhost:9001
```
