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
- [x] 并发闸门安全（Redis ZSET + TTL 自愈，强杀不泄漏、beat/多 worker 不误清）
- [x] API 限流防护（SDK 重试禁用 + 应用层指数退避 5-60s）
- [x] 400 拒绝预检（纯数字/符号段落跳过，不浪费 API 调用）

## 维护变更记录

### 2026-06-25（稳定性 / 一致性修复）
- **并发闸门重构**：由单纯 Redis INCR 计数器改为 **ZSET + TTL 自愈**。解决两类隐患：
  worker 子进程被 OOM/SIGKILL 强杀导致的"槽位永久泄漏"；以及 beat / 多 worker 进程
  启动时把计数器清零造成的"超额放行"。僵尸条目按 600s TTL 自动回收，任务执行中通过
  进度回调心跳续期。
- **模型切换跨进程生效**：每个翻译任务开始前重置翻译器 / VL 客户端单例，管理员在后台
  切换激活模型后，worker 进程无需重启即可使用新模型。
- **数据库连接池调大**：`pool_size=20 / max_overflow=20 / pool_recycle=1800`，并把任务
  存活检查节流到每 2s 一次，避免段落级并发 + 多任务并发下连接池耗尽（QueuePool timeout）。
- **对照模式 PDF 输出修正**：中外对照模式下 PDF 源文件强制输出为 Word（.docx）。就地替换
  会让翻倍的对照文本溢出原 bbox / 字号骤缩，转 Word 后段落可自由换行，保证可读。
- **图片翻译默认项对齐**：上传接口与建表默认值统一为「仅翻译文档文字，图片保持原样」（NO），
  与需求 2.2 默认项及 ORM 模型默认一致。

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
