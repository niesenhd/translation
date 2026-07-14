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
- [x] Vue 3 前端（登录 / 多文件上传 / 任务列表 / 下载 / 反馈）
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

### 2026-07-14（支持多文件批量上传）
新建翻译对话框支持一次选择多个文件，共享同一组翻译参数（语种/模式/精译等），
并行提交后每个文件创建独立翻译任务。部分成功时提示「N个成功，M个失败」。

> 部署确认（commit `07b8593` + `ba78c22`）：代码审查通过（旧变量名 `selectedFile` 无残留、
> `Promise.allSettled` 错误处理完整、后端 UUID 任务 ID 并发安全），已 rsync 同步至
> `172.16.1.56` 并重建前端容器，全服务 HTTP 200 正常。

### 2026-07-10（外部工具 bug 报告核验 + 批量修复 14 项）
用其它工具生成的 `bug_report_20260710.xlsx`（15 条）逐条对照代码核验：14 条成立、1 条误报
（PPTX `font.language_id` —— 实测 python-pptx 1.0.2 该属性存在且赋值生效，非静默丢弃，未改）。
成立的 14 条全部修复：
- 🔴 **P0 PaddleOCR 递归死锁**（`paddle_ocr.py`）：`_engine_lock` 由 `Lock()` 改 `RLock()`。
  非中英语种模型初始化失败时会在持锁状态下递归回退 `_get_ocr_engine("zh")`，普通锁会自锁死
  锁使 worker 永久挂死。
- 🔴 **P0 PDF fitz 句柄泄漏**（`document_engine.translate_pdf_inplace`）：整函数用 `try/finally`
  包裹，确保任何路径（含提前 return / 异常）都 `doc.close()`，避免 fd 耗尽致后续 PDF 全失败。
- 🟡 **P1 孤儿任务对账竞态**（`celery_app.reconcile_stuck_tasks_task`）：标 FAILED 的 UPDATE 增加
  `status == RUNNING` 条件 + 校验 rowcount，避免与用户重试（RUNNING→QUEUED）竞态、误杀重试任务。
- 🟡 **P1 DOCX RTL 遍历不全**（`_apply_docx_rtl`）：改为 XML 级遍历全部 `<w:p>`，覆盖嵌套表格、
  文本框（txbxContent）、页眉页脚（含首页/偶数页变体）、脚注尾注（旧实现仅正文段落 + 顶层表格）。
- 🟡 **P1 PPTX 合并单元格重复翻译**（`translate_pptx._walk_shape`）：表格 cell 收集加 `seen_cells`
  按底层 `_tc` 去重（`row.cells` 对合并区域会返回同一 cell 多次）。
- 🟡 **P1 PPTX 对照模式不换行**（新增 `_set_pptx_paragraph_text`）：PowerPoint 不解释 `\n`，改用
  `<a:br/>` 软换行元素 + 克隆首 run 的 `rPr` 承载后续行，原文/译文正常分行且保留字体。
- 🟠 **P2 OCR 深底浅字不可见**（`ocr._sample_text_color`）：按背景亮度判定前景——深底取最亮像素
  （浅色文字）、浅底取最暗像素，无有效前景时用背景反色兜底，不再一律返回黑色。
- 🟠 **P2 RGBA 图片存 JPEG 失败**（`ocr` 就地替换保存）：JPEG 保存前把 RGBA/LA/带透明 P 模式合成
  到白底转 RGB，避免 `cannot write mode RGBA as JPEG` 被吞后返回未翻译原图。
- 🟠 **P2 PDF 阿语字体**（新增 `_pdf_font_for_lang`）：按目标语种选字体——日/韩用内置 CJK 字体、
  繁中 china-t、阿语尝试系统 Noto Naskh/Sans Arabic（`insert_textbox` 传 fontfile），找不到回退
  china-s 并告警。已在 `backend/Dockerfile` 加入 `fonts-noto-core`（含 NotoNaskhArabic/NotoSansArabic），
  重建镜像后阿语字体随镜像内置，无需手动装。
- 🟠 **P2 XLSX 工作表名边界**（`translate_xlsx` 写回）：补充去首尾单引号、空名/保留名（History）回退
  原名、重名去重改为大小写不敏感（Excel 表名不区分大小写）。
- ⚪ **P3 translate_images 默认值不一致**（`TranslationContext`）：默认 `yes`→`no`，与 DB/API 对齐
  （无实际行为变化，celery_app 构造时总显式传 DB 值）。
- ⚪ **P3 删除死代码**（`document_engine`）：移除无调用方的 `_run_is_preservable` /`_translate_paragraph`。
- ⚪ **P3 密钥默认值守卫**（`config.Settings`）：`APP_ENV=production` 且 `APP_SECRET_KEY`/`APP_ADMIN_TOKEN`
  仍为默认/占位值时启动即报错拒绝运行；开发环境仅告警。**（不改动服务器现有 .env，仅代码级防护，
  生产密钥硬化仍按既定暂缓。）**
- ⚪ **P3 登录防爆破**（`api/auth.login`）：新增基于 Redis 的 IP 级失败限速——5 分钟内失败 5 次锁定
  15 分钟；成功登录清零；Redis 不可用时 fail-open 不阻断登录。取真实 IP 优先用 `X-Forwarded-For`。

### 2026-07-10（翻译 prompt 加强：保留时态/情态/法律双连词，方案1+2）
律师反馈 "do not and will not" 被译成 "不会"，丢失"现在 vs 将来"时态区分（法律上重要）。
根因：通用 prompt 没显式要求保留时态/情态/双连词；且精译评审用同一模型、同盲区，笼统
"检查准确性"不足以纠偏。实测：当前 review 对丢义初译仍输出"并未且不会"（错），加强版输出
"现在不会且将来也不会"（对）。
- **方案1 SYSTEM_PROMPT（所有翻译默认生效）**：新增 FAITHFULNESS 段——时态（do not and will not→
  现在不会且将来也不会，不合并）、情态（shall=应/may=可/may not=不得/will=将）、法律双连词
  （null and void 等）、否定范围；强调不得为流畅牺牲忠实。
- **方案2 REVIEW_SYSTEM_PROMPT（精译模式生效）**：复核改为显式检查清单（时态合并/情态/双连词/
  否定范围）带示例，能纠正初译的丢义。
- 改善一整类"流畅但丢义"问题，不止这一句。
- 另：新增孤儿任务对账（commit 8a435b4）——worker 重启/异常退出导致卡死在 RUNNING 的任务，
  由 Celery Beat 每 5 分钟自动标 FAILED，不再永久卡住。

### 2026-07-09（翻译质量四阶段提升 + 本地账密登录 + 测试账号）
基于律师反馈，分四阶段系统性提升翻译水平，并新增账号体系供律师测试。

- **阶段1 术语库方案改造**：domain="法学" 的 6.8 万条法律词典退出翻译注入（模型裸翻更优，
  词典释义只会带偏），保留带具体领域标签的精选术语（约 494 条）并恢复双向匹配（满足需求
  line 257）。en→zh 注入量 14286→494，全部为高质量 strict/preferred 法律术语。
- **阶段2 文档级术语一致性**：新增 `doc_term_extractor.py`，翻译前一次模型调用从全文抽取
  关键术语/定义术语/专有名词并锁定译文，合并进 glossary（STRICT）逐段注入，保证同一术语
  全文统一译法（解决"Transaction Documents 前后译法不一"）。开关 `TRANSLATION_DOC_TERM_EXTRACTION`
  默认开，仅对 >1500 字符文档触发。
- **阶段3 两遍法精译**：新增 per-task `refine_mode`（none/double_pass）。开启后每段翻译 +
  法律译审复核两遍（术语一致性、法律文体、漏译），成本约 2 倍，重要文书用。前端上传表单
  增"精译模式"开关（默认关）。translator 新增 `review()` + `REVIEW_SYSTEM_PROMPT`。
- **阶段4 充实 TM + 反馈闭环**：TM 创建/导入去重（同 lang_pair + 归一化 source_text）；
  新增 `POST /admin/tm/import-from-task/{id}` 从已完成任务对齐段落导入 TM；TM 增 task_id
  追溯字段；反馈新增 `POST /admin/feedback/{id}/adopt` 采纳时录入术语库(strict)/TM(source=feedback)
  （补需求 2.11 缺口）；Admin 后台 TM 页增"从任务导入"按钮。
- **本地账密登录系统**（OA 律智荟对接前的过渡）：新增 User 表 + `/api/auth/login` + `/api/auth/me`，
  HMAC 签名 token（7 天，app_secret_key 签名），pbkdf2 密码哈希。前端改为用户名/密码登录，
  启动时校验 token 刷新权限，admin 后台按 is_admin 守卫。原静态 admin token 仍作紧急超管入口。
- **测试账号**：`scripts/seed_users.py` 幂等建号。已创建 admin + lawyer01~05（随机密码，仅哈希入库）。

### 2026-07-08（翻译质量治理：定位并修复译文劣化主因）

针对律师反馈"翻译水平低"，逐层定位到三个独立问题并全部修复。**根因是术语库加载逻辑**，
而非模型能力（qwen3.6-27b 裸翻法律文本质量良好）。

- **🔴 根因修复（glossary.py，commit b688cec）**：`get_glossary_for_lang_pair` 此前做 en→zh
  翻译时，会把**整本 5.4 万条 zh→en 词典 source/target 互换后当作 en→zh 术语注入**，塞进数万条
  词典式反向释义（某 zh→en 条目反向成 `transaction→和息`、`terms→开庭期`、`valid→作准`、
  `ITS→进口报表制度`），系统性把模型带偏，产出"和息文件 / 开庭期 / 交货"等劣化译文。改为
  **只按真实方向正向加载**（auto 时按 `→{tgt}` 结尾正向），不做反向匹配、不做跨语种通配。
  auto→zh 加载量 68498→14286。实测原劣化段译文现已正确（"交易文件…根据其条款…签署并交付"）。
- **术语库治理（DB，commit bd59c7e）**：72537 → 68498，删除 4039 条高频误配源——en→zh 单个英文
  头词 3767（court→议会、execute→履行、qualified→附条件的、enforceable→可执行、demonstrate→表演、
  delivery→交出）、单字中文源 244、词典式词性标注 13、乱码 18。保留多词法律术语 + 494 条精选。
  全量备份：服务器 `~/backups/term_entries_pre_cleanup_202607081457.sql`。
- **译文截断修复（translator.py，commit bd59c7e，模型无关）**：显式 max_tokens（按输入长度给足，
  封顶 8192）——不设走 DashScope 偏小默认值，长段落译文被中途截断（NVCA 多段砍到半句）；
  finish_reason=length 检测放大重试；空译文重试兜底。该修复对自部署模型同样适用。

> **关于私有化部署 / 数据保密**：当前翻译走 DashScope 云端 API。model_configs 本就支持任意
> OpenAI 兼容端点（api_base_url + api_key + model_id）。正式应用若要求数据不出内网，可在律所
> 内网 GPU 服务器上用 vLLM 部署开源翻译模型 **Hunyuan-MT-7B / HY-MT1.5-7B**（腾讯开源，WMT25 冠军，
> 支持术语干预），在管理后台新增一条模型配置指向本地 vLLM 端点即可，无需改代码。Qwen-MT 翻译
> 质量好但属 DashScope 纯云端服务、不开源，仅适合用免费额度做 A/B 基准，**不能**作为保密场景生产模型。

> **关于私有化部署 / 数据保密**：当前翻译走 DashScope 云端 API。模型配置（model_configs）
> 本就支持任意 OpenAI 兼容端点（api_base_url + api_key + model_id）。正式应用若要求
> 数据不出内网，可在律所内网 GPU 服务器上用 vLLM 部署开源翻译模型 **Hunyuan-MT-7B /
> HY-MT1.5-7B**（腾讯开源，WMT25 冠军，支持术语干预），在管理后台新增一条模型配置指向
> 本地 vLLM 端点即可，无需改代码。Qwen-MT 虽翻译质量好但是 DashScope 纯云端服务、不开源，
> 仅适合用免费额度做 A/B 基准，**不能**作为保密场景的生产模型。

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
