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

### 2026-07-03（第二批：数据安全 / 第三批：翻译覆盖，commit 407cc13）
**批2 数据安全：**
- **任务状态机条件更新**：worker 抢占（QUEUED→RUNNING）、收尾（RUNNING→SUCCEEDED/FAILED）
  全部改为带 WHERE 条件的原子 UPDATE。修复两个竞态：① 用户删除任务后 worker 无条件覆写
  状态导致"已安全删除的译文复活重新上传 MinIO 且永久残留"；② 并发 retry 重复入队导致
  同一任务被两个 worker 双份翻译（双倍 LLM 消耗）。收尾发现任务已删除时自动清理刚上传的
  译文对象；上传前增加存活复查。retry 接口同样条件抢占（冲突返回 409）；broker 不可用时
  上传/重试任务标 FAILED 而非永久卡"排队中"。
- **端口暴露收敛**：postgres/redis/minio/backend 全部改绑 127.0.0.1（此前 0.0.0.0 内网
  任意主机可直连数据库/队列/对象存储）。对外仅保留前端 8080。运维需要时在服务器本机
  localhost 访问或 SSH 隧道。
- **Redis 加固**：加 requirepass（密码在服务器 deploy/.env 的 REDIS_PASSWORD，不入 git）+
  appendonly 持久化 + 数据卷。修复"Redis 重启后已入队消息全丢、任务永卡排队中"。
**批3 翻译覆盖（法律文书常用结构此前整体漏翻）：**
- **DOCX**：嵌套表格递归收集；合并单元格按底层 tc 去重（不再重复翻译计费）；文本框
  （w:txbxContent，含 mc:Fallback 副本）与块级内容控件（w:sdtContent）段落纳入翻译；
  超链接/内联控件文字纳入统一"自有 run"提取+写回——修复"译文与超链接原文并存"；
  写回绕开 python-docx run.text= 的 clear_content，换行/制表符正确转 w:br/w:tab。
  NVCA 回归：脚注引用 17→17，页脚正常翻译。
- **PPTX SmartArt**：文字实际存于独立 diagram part（data/drawing.xml），此前扫幻灯片 XML
  永远扫不到，SmartArt 整体漏翻；现逐 part 解析 a:t 翻译并序列化写回。
- **TXT/MD 编码**：GBK/GB18030 自动探测（与 CSV 同链），修复"GBK 文件整篇变 � 再被翻译"。
- **图片 OCR 语种**：source_lang 真正传入 PaddleOCR（此前恒用中英模型，俄/阿语图片出乱码）；
  引擎按语种缓存；语种模型初始化失败自动回退中英模型。
- 新增 `scripts/test_batch23.py` 离线回归（16 项断言，假翻译器零 API 消耗）。

### 2026-07-02（译文正确性修复,第一批）
- **TM 高相似盲替换修复**：原逻辑相似度 ≥95% 直接套用历史译文。bigram Dice 对长段落
  不敏感——仅一个日期/金额/当事人名不同时相似度仍 >0.95，会把旧文书的事实性内容写进
  当前译文。改为 `lookup_tm` 返回 `exact`（归一化后逐字相等）字段，**仅全等才直接复用**；
  高相似非全等（≥80%）一律降级为参考注入 prompt。正文路径（document_engine）与图片
  OCR 批量翻译路径（ocr.py）同步修复。
- **TM auto 语对失效修复**：上传默认 `source_lang=auto` 时，TM 查询拼出 `auto→zh` 这类
  不存在的语对，精确等值查询恒为空——TM 在默认流程中形同虚设。改为 auto 时按
  `%→{目标语}` 模糊匹配语对（相似度阈值本身足以排除跨源语言误匹配）。
- **DOCX 空译文抹掉段落修复**：API 返回空 content（安全过滤/截断）时，docx 写回路径
  会把整段原文清空（pptx/xlsx/csv/pdf 均有防护，唯独 docx 没有）。在 `_translate_text`
  统一加空译文保留原文兜底 + docx 写回循环二次防护。
- **回收 6-29 服务器侧改进进 git**（用户用另一工具在服务器直接修改，本次回收恢复三方
  一致）：法律译者专用 system prompt（正式法律文体、保留条款编号/交叉引用/定义术语）；
  `_match_glossary` 术语匹配重写（英文词边界防 "act" 命中 "contract"、最长优先去重、
  strict 优先、单段 40 条上限）；术语库构建/去重脚本入库。11MB 术语备份 SQL 与源 xlsx
  移至服务器 `~/backups/`（不入 git）。

### 2026-06-26（线上故障修复：前端 502 / 后台空白）
- **nginx 缓存后端旧 IP 导致全站 502**：前端 nginx `proxy_pass http://backend:8000`
  仅在启动时解析一次 `backend` 主机名并永久缓存 IP。后端容器单独重启后换了新 IP
  （172.18.0.7→172.18.0.6），而前端容器未重启，nginx 仍打旧 IP → 连接被拒 → 所有
  `/api/` 请求 502。表现为：前端术语库 / 模型配置空白、文件提交失败，但后端直连 :8000
  完全正常（数据无损：术语 7.2 万条、模型配置均在）。**根治**：改用 Docker 内置 DNS
  `resolver 127.0.0.11 valid=10s` + 变量 + `$request_uri` 透传，强制 nginx 运行时按需
  重新解析，后端重启后自动跟随新 IP，不再复发。

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
- **DOCX 脚注/尾注引用丢失修复**：写回译文用 `run.text=` 会触发 python-docx 的
  `clear_content()`，删掉 run 内的 `w:footnoteReference` 等子元素，导致脚注虽已翻译但
  正文失去引用、Word 不再显示（脚注占比大的文书表现为"译文只有一部分"）。新增
  `_run_is_preservable`，把脚注/尾注引用、字段（页码/目录/交叉引用 fldChar·instrText）、
  图片/公式一并视为不可覆盖，写回时跳过。实测 NVCA 法律意见书正文引用 17→17 保留。
- **DOCX 页眉/页脚翻译补全**：`doc.paragraphs` 不含页眉页脚，此前完全未翻译（实测 NVCA
  文档 footer 仍为英文）。新增 `_collect_docx_header_footer_paragraphs`，逐 section 收集
  header/footer（含首页/奇偶页变体及其中表格），跳过 `is_linked_to_previous` 避免重复，
  走与正文同一套写回。PDF（整页 span）与 PPTX（占位符 shape）路径本就覆盖，无需改。

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
