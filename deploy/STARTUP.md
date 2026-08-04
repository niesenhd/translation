# 文档翻译系统启动、升级与迁移指南

## 1. 准备环境变量

首次部署：

```bash
cd deploy
cp .env.example .env
chmod 600 .env
```

升级现有环境时不要用 `.env.example` 覆盖现有 `.env`，应逐项补充新变量。生产环境至少核对：

| 字段 | 要求 |
|------|------|
| `APP_ENV` | 生产设为 `production` |
| `APP_SECRET_KEY` | 登录 token 签名密钥，使用随机强值 |
| `APP_ADMIN_TOKEN` | 仅供紧急直连 API 的超级管理员 Bearer token，不是前端登录密码 |
| `APP_DATA_ENCRYPTION_KEY` | Fernet 32 字节 urlsafe-base64 密钥；用于数据库敏感字段加密，必须备份并限制访问 |
| `POSTGRES_PASSWORD` / `REDIS_PASSWORD` | 使用独立随机强密码 |
| `MINIO_ACCESS_KEY` / `MINIO_SECRET_KEY` | 使用独立生产凭据 |
| `PUBLIC_BASE_URL` | 浏览器实际访问的绝对根地址；生产为 `https://translation.tylaw.com.cn:8080` |
| `CORS_ORIGINS` | 生产浏览器来源白名单，不得使用 `*` |
| 模型配置 | 可从管理后台/迁移数据恢复；模型 ID、API 地址、API Key 和启用状态必须完整保留 |
| `OA_APP_KEY` / `OA_APP_SECRET` / `OA_SSO_SECRET` | 使用现有真实值，永久保留，**不轮换、不改值** |

仓库、文档和 `.env.example` 只允许占位符。OA 三项真实值仅从既有受控 `.env` 或部署密钥系统
注入，不得粘贴到文档、工单、日志或代码仓库。

## 2. 升级或清库迁移前保留数据

普通原地升级应保留现有 Docker 数据卷，并先做 PostgreSQL 和 MinIO 备份。不要执行
`docker compose down -v`，该命令会删除 PostgreSQL、MinIO、Redis 和 OCR 模型数据卷。

迁移到全新空库时，必须使用 `backend/scripts/preserved_data.py` 导出、校验并恢复保留数据。保留范围包括：

- 允许保留的本地/管理员账号及密码哈希；普通 OA 人员由 `getemployees` 重新同步；
- 全部法律术语库和翻译记忆库；
- 全部 `model_configs`，包括模型 ID、API 地址、API Key、模型类型、启用状态；
- 旧版 `system_config` 中的 `translation_model`、`vl_model`、`api_base_url`、`api_key`。

该最小迁移包不包含翻译任务、源文件/译文文件和质量反馈；普通 OA 账号通过同步重建。若这些数据也需
保留，应改用经过审批的 PostgreSQL + MinIO 完整备份恢复流程，不能假定最小迁移包已经覆盖。

导出命令格式如下；数据库 URL 和 `APP_DATA_ENCRYPTION_KEY` 必须通过受控终端或密钥系统注入，
不要把真实值写进命令示例或 shell 历史：

```bash
cd backend
uv sync --frozen
uv run python scripts/preserved_data.py export \
  --database-url 'postgresql+psycopg://<user>:<url-encoded-password>@127.0.0.1:5432/<database>' \
  --output '<new-empty-secure-directory>'
```

导出包不是加密备份，包含密码哈希和模型 API Key 明文；目录权限必须为 700、文件权限为 600，
传输和长期保存还需使用受控加密介质。检查 `manifest.json` 的行数、SHA256、账号清单、语对分布和
`conflicts.csv` 后再继续。

新库先执行 Alembic 到最新版本，再在**所有应用写入停止且业务表为空**时恢复：

```bash
cd deploy
docker compose up -d postgres
docker compose run --rm migrate

cd ../backend
uv run python scripts/preserved_data.py import \
  --database-url 'postgresql+psycopg://<user>:<url-encoded-password>@127.0.0.1:5432/<database>' \
  --input '<verified-secure-directory>'
```

导入会校验 manifest 和 SHA256，并拒绝非空业务库。若新环境更换
`APP_DATA_ENCRYPTION_KEY`，导出时需提供旧密钥以解密现有模型 Key，导入时提供新密钥重新加密；两端
都不得缺失。恢复后逐项核对模型名称、模型 ID、API 地址、`api_key_set`、启用状态，并做一次最小翻译。

## 3. 启动服务

```bash
cd deploy
docker compose up -d --build
docker compose ps
```

Compose 会先运行 `alembic upgrade head`，迁移成功后才启动 backend、worker 和 beat。

| 服务 | 地址 | 暴露范围 |
|------|------|----------|
| 前端与统一 API 入口 | `http://localhost:8080`（默认配置） | 8080 对外 |
| FastAPI / Swagger | `http://127.0.0.1:8000/docs` | 仅服务器本机 |
| MinIO API / Console | `127.0.0.1:9000` / `http://127.0.0.1:9001` | 仅服务器本机 |
| PostgreSQL | `127.0.0.1:5432` | 仅服务器本机 |
| Redis | `127.0.0.1:6379` | 仅服务器本机 |

外部请求一律经前端 Nginx 8080 反代；不要把 8000、9000/9001、5432、6379 开放给内网其他主机。

## 4. 首次创建管理员并登录

前端只接受用户名/密码。首次空库可在服务器本机使用紧急 `APP_ADMIN_TOKEN` 创建管理员：

```bash
curl -X POST http://127.0.0.1:8000/api/admin/users \
  -H 'Authorization: Bearer <APP_ADMIN_TOKEN>' \
  -H 'Content-Type: application/json' \
  -d '{"username":"admin","password":"<strong-password>","display_name":"系统管理员","is_admin":true}'
```

随后访问 `http://localhost:8080`，使用刚创建的用户名和密码登录。`APP_ADMIN_TOKEN` 只作紧急 API
兜底，不应填入前端登录页，也不应作为日常用户凭据。

## 5. 直接调用 API

先用本地账号登录：

```http
POST /api/auth/login
Content-Type: application/json

{"username":"admin","password":"<strong-password>"}
```

后续请求使用响应中的 token：`Authorization: Bearer <token>`。

任务列表分页契约：

```http
GET /api/tasks?page=1&page_size=20
Authorization: Bearer <token>
```

响应为 `{"items":[...],"total":0,"page":1,"page_size":20}`；`page >= 1`，
`1 <= page_size <= 100`。普通用户仅能看到自己的非删除任务，管理员可看到全部非删除任务。

## 6. 律智荟 SSO

- `POST /api/sso/login` 接收 `{loginName,timestamp,sign}`，成功响应严格只有
  `{success,redirect_url}`；`redirect_url` 使用绝对地址并携带 60 秒一次性 `sso_code`。
- 前端移除地址栏中的 code 后，调用 `POST /api/sso/exchange`，请求体为 `{code}`，兑换 7 天登录 token。
- `sso_code` 最多成功使用一次；同一签名请求在 5 分钟窗口内也只能受理一次。Redis 不可用时 SSO
  安全失败并返回 503。
- 生产 `PUBLIC_BASE_URL` 必须为 `https://translation.tylaw.com.cn:8080`。

## 7. 生产 HTTPS（8080）

默认 `frontend/nginx.conf` 是 HTTP。律智荟生产环境要求 HTTPS 8080、80/443 不开放：

1. 把证书链和私钥放入仅运维可读的目录；
2. 以 `frontend/nginx-ssl.conf.example` 作为生产 Nginx 配置；
3. 在 Compose 中挂载 Nginx 配置和证书，只保留宿主机 `8080:8080`；
4. 设置 `PUBLIC_BASE_URL=https://translation.tylaw.com.cn:8080` 和匹配的 `CORS_ORIGINS`；
5. 重建 frontend，并验证首页、`/api/auth/login`、SSO redirect_url 和证书链。

## 8. 验证策略

- 不在 GitHub Actions 中运行后端、前端、端到端测试或应用构建；现有 GitHub workflow 只做凭据安全扫描。
- 功能测试、迁移演练、镜像构建和端到端验收只在指定服务器执行，并使用服务器受控环境变量；不得把
  OA 凭据或模型 API Key 注入 GitHub runner。
- 文档/代码收尾阶段在本机只做静态文本与差异检查，不运行本机测试或构建。

## 9. 常见问题

| 现象 | 排查 |
|------|------|
| 服务因生产配置退出 | 检查 `APP_SECRET_KEY`、`APP_ADMIN_TOKEN`、`APP_DATA_ENCRYPTION_KEY`、`PUBLIC_BASE_URL`、`CORS_ORIGINS` 是否仍为缺省/占位值 |
| 模型配置存在但调用失败 | 核对迁移后的 API 地址、`api_key_set`、启用状态和 `APP_DATA_ENCRYPTION_KEY`；不要在日志中打印 Key |
| 任务一直排队/运行 | 查看 worker、Redis 和模型端点日志；确认 Redis 密码一致、模型 Key 有效且网络可达 |
| 上传 401 | `Authorization` 必须为 `Bearer <登录响应 token>`；紧急 token 仅供管理员直连 API |
| 上传 413 | 应用和 Nginx 的有效上限均为 200MB |
| 外部打不开 Swagger/MinIO | 这是预期安全边界；这些端口仅服务器本机可达，使用 SSH 隧道运维 |
| SSO 500/503 | 500 检查 SSO Secret/PUBLIC_BASE_URL；503 检查 Redis；不要通过关闭防重放绕过故障 |
