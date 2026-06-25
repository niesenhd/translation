# P0 启动指南

## 1. 准备环境变量

```bash
cd deploy
cp .env.example .env
```

编辑 [.env](file:///Users/niesen/Documents/trae_projects/translation/deploy/.env)，**至少修改以下两项**：

| 字段 | 说明 |
|------|------|
| `APP_ADMIN_TOKEN` | 管理员登录 Token，前端登录页填这个值。默认 `admin-dev-token` 仅用于开发，生产请换长随机字符串 |
| `DASHSCOPE_API_KEY` | 阿里云 DashScope API Key（[控制台获取](https://bailian.console.aliyun.com/?apiKey=1)）|

可选：`DASHSCOPE_MODEL` 可选 `qwen-plus` / `qwen-max` / `qwen-turbo` 等，默认 `qwen3.6-27b`。

## 2. 启动

```bash
cd deploy
docker compose up -d --build
```

服务监听：
- 前端：http://localhost:8080
- 后端 API + Swagger：http://localhost:8000/docs
- MinIO Console：http://localhost:9001（账号 `minioadmin` / `minioadmin`）
- PostgreSQL：localhost:5432
- Redis：localhost:6379

## 3. 端到端验证

1. 浏览器访问 http://localhost:8080
2. 登录页：用户名填 `admin`，Token 填 `.env` 里 `APP_ADMIN_TOKEN` 的值
3. 点击「+ 新建翻译」，上传一份 `.txt` 或 `.docx`，选择目标语种 + 输出模式
4. 任务列表会自动每 3 秒刷新一次，状态从 `排队中 → 翻译中 → 完成`
5. 完成后点击「下载」获取译文文件

## 4. 直接调 API（不走前端）

```bash
# 上传翻译
curl -X POST http://localhost:8000/api/tasks/upload \
  -H "Authorization: Bearer admin-dev-token" \
  -F "file=@/path/to/sample.docx" \
  -F "target_lang=en" \
  -F "output_mode=plain"

# 查列表
curl http://localhost:8000/api/tasks \
  -H "Authorization: Bearer admin-dev-token"

# 下载结果
curl -OJ http://localhost:8000/api/tasks/<task_id>/download \
  -H "Authorization: Bearer admin-dev-token"
```

## 5. 常见问题

| 现象 | 解决 |
|------|------|
| 任务一直 `running` 不动 | `docker logs translation-worker-1` 看 worker 日志，多半是 API Key 错或网络不通 |
| 上传 401 | Authorization 头格式必须是 `Bearer <token>` |
| PDF 翻译失败 | 检查是否扫描件（P0 仅支持电子版 PDF，扫描件 OCR 在 P1） |
| 上传文件大小受限 | nginx.conf 已设 200m，如需更大改 `client_max_body_size` |

## 6. 已实现功能（对照需求 v1.3）

| 章节 | 需求 | P0 状态 |
|------|------|---------|
| 2.1 | OA 对接 | ⏸️ 代码占位（P3 启用） |
| 2.1 | admin 登录 | ✅ 静态 Token |
| 2.2 | DOCX/PDF(电子)/TXT/MD | ✅ |
| 2.2 | PPT/Excel/PDF 扫描件 | ⏸️ P1 |
| 2.2 | 大文件分片 | ⏸️ P1 |
| 2.3 | 纯译文 / 对照模式 | ✅ |
| 2.4 | 9 语种支持 | ✅（前端选择） |
| 2.5 | 模型管理 | ⏸️ P2（已抽象 Translator 适配器） |
| 2.6 | 术语库 | ⏸️ P2 |
| 2.7 | 并发 + FIFO | ✅（Celery worker_prefetch=1） |
| 2.8 | 统计看板 | ⏸️ P2 |
| 2.9 | 文件保留策略 | ⏸️ P2 |
| 2.9 | 用户手动删除 | ✅ |
| 2.10 | 日志系统 | ⏸️ P4 |
| 2.11 | 反馈机制 | ⏸️ P2 |
| 2.12 | 纯内网 | ⏸️ P5 |
