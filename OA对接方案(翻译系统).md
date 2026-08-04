# 文档翻译系统 ↔ 律智荟 OA 对接方案

> 版本：v2.1 | 日期：2026-08-04 | 状态：✅ 接口已实现，待生产配置与双方联调

---

## 一、整体架构

```
用户浏览器
┌─────────────────────────┐    ┌──────────────────────────────────────┐
│   律智荟 OA（必智）      │    │  文档翻译系统                            │
│   e.tylaw.com.cn        │    │  translation.tylaw.com.cn:8080 (HTTPS) │
│                         │    │                                        │
│  ┌─────────────────┐    │    │  ┌────────────────────┐               │
│  │  律智荟系统      │    │    │  │ FastAPI 后端        │               │
│  │                 │    │    │  │                    │               │
│  │  用户点击入口    │────┼────┼─>│ /api/sso/login     │               │
│  │                 │    │ ①  │  │ (SSO 单点登录)      │               │
│  │                 │<───┼────┼──│ 返回 redirect_url     │               │
│  │                 │    │    │  └────────────────────┘               │
│  │                 │    │    │                                        │
│  │                 │────┼────┼─>│ 浏览器打开 sso_code  │               │
│  │                 │    │    │  │ 前端调用 /exchange  │               │
│  │                 │    │    │  └────────────────────┘               │
│  │                 │    │    │                                        │
│  │                 │    │    │  ┌────────────────────┐               │
│  │  人员数据        │<───┼────┼──│ Celery Beat 定时任务 │               │
│  │                 │    │ ②  │  │ /api/getemployees   │               │
│  │                 │────┼────┼─>│ 9-18点每3小时同步   │               │
│  │                 │    │    │  └────────────────────┘               │
│  └─────────────────┘    │    │                                        │
└─────────────────────────┘    └────────────────────────────────────────┘

① SSO 登录：律智荟服务器 POST 调我们的接口；响应只返回带 60 秒一次性 code 的绝对 redirect_url，
   浏览器打开后由前端调用 /api/sso/exchange 兑换登录 token
② 用户同步：我们定时调律智荟 getemployees，同步全员资料及 OA 在职状态到本地 users 表；
   未设置管理员覆盖时，最终启停跟随 OA；管理员可强制启用或强制停用且后续同步不覆盖
```

---

## 二、域名与端口

| 项 | 值 |
|----|-----|
| 域名 | `translation.tylaw.com.cn` |
| 端口 | **8080**（唯一对外端口） |
| 协议 | **HTTPS**（SSL） |
| 80/443 | **不开放** |
| 完整访问地址 | `https://translation.tylaw.com.cn:8080` |

> 所有地址（前端页面、SSO 回调、API）统一使用 `https://translation.tylaw.com.cn:8080`，均带 8080 端口号。
> Docker Compose 默认配置在 8080 提供 HTTP，仅用于本机/内网联调；生产必须启用
> `frontend/nginx-ssl.conf.example` 并挂载证书。backend 8000、MinIO 9000/9001、PostgreSQL 5432、
> Redis 6379 仅绑定服务器 `127.0.0.1`，不对其他主机开放。`PUBLIC_BASE_URL` 必须精确设置为上述
> HTTPS 根地址。

---

## 三、SSO 单点登录（POST API 方式）

### 流程

```
1. 用户在律智荟点击「文档翻译」入口
2. 律智荟服务器 POST 调用我们的接口：
   POST https://translation.tylaw.com.cn:8080/api/sso/login
   Body: { "loginName": "zhangsan", "timestamp": 1722326494123, "sign": "xxx" }
3. 我们验证签名、防重放并检查本地 OA 用户 → 返回：
   { "success": true, "redirect_url": "https://translation.tylaw.com.cn:8080/?sso_code=xxx" }
4. 律智荟用用户浏览器打开 redirect_url
5. 前端先从地址栏移除 sso_code，再调用：
   POST https://translation.tylaw.com.cn:8080/api/sso/exchange
   Body: { "code": "xxx" }
6. 兑换成功后返回 7 天登录 token，用户无感知进入文档翻译系统
```

### 3.1 律智荟服务器调用登录接口

| 项 | 说明 |
|----|------|
| **接口地址** | `POST https://translation.tylaw.com.cn:8080/api/sso/login` |
| **Content-Type** | `application/json` |

**请求参数（律智荟发给我们）：**

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| loginName | string | 是 | 用户的登录名（与 getemployees 返回的 LoginName 一致） |
| timestamp | long | 是 | 13 位毫秒时间戳（如 `1722326494123`） |
| sign | string | 是 | 签名值（见下方签名规则） |

**请求示例：**

```json
{
  "loginName": "zhangsan",
  "timestamp": 1722326494123,
  "sign": "k3Jk2xN9..."
}
```

**响应（成功，HTTP 200）：**

| 字段 | 类型 | 说明 |
|------|------|------|
| success | bool | 固定为 true |
| redirect_url | string | 带 60 秒一次性 `sso_code` 的**绝对**前端地址，律智荟直接用浏览器打开 |

```json
{
  "success": true,
  "redirect_url": "https://translation.tylaw.com.cn:8080/?sso_code=one-time-code"
}
```

> `/api/sso/login` 的成功响应严格只有 `success`、`redirect_url`；不得增加或返回登录 token。

**错误响应（FastAPI 标准格式）：**

```json
{
  "detail": "签名校验失败"
}
```

| HTTP 状态码 | detail 内容 | 触发条件 |
|------------|------------|---------|
| 422 | 参数校验详情 | 请求体缺字段、字段类型或长度不符合 schema |
| 400 | `时间戳格式错误，需为 13 位毫秒级` | timestamp 不是合法的 13 位数字 |
| 401 | `签名校验失败` | HMAC-SHA256 验证不通过 |
| 401 | `时间戳已过期` | timestamp 与服务器时间差超过 5 分钟（300 秒） |
| 401 | `SSO 请求已使用或正在处理` | 同一签名请求在防重放窗口内再次提交 |
| 404 | `用户不存在或已停用：{loginName}` | 用户不存在、非 OA 身份，或最终启用状态为停用；管理员强制启用可作为离职账号的明确例外 |
| 500 | `SSO 密钥未配置` / `PUBLIC_BASE_URL 未配置` | 生产配置缺失 |
| 503 | `SSO 服务暂不可用，请稍后重试` | Redis 防重放或一次性 code 存储不可用（安全失败） |

> timestamp 是 13 位毫秒级 Unix 时间戳，表示绝对时刻，本身没有时区。这不是“使用 UTC 而不是北京时间”：双方直接取当前 Unix epoch 毫秒值，不做 UTC+8 或任何额外偏移。

### 3.2 浏览器前端调用 code 兑换接口

此接口由文档翻译系统前端调用，不需要律智荟服务器代为调用。

| 项 | 说明 |
|----|------|
| **接口地址** | `POST https://translation.tylaw.com.cn:8080/api/sso/exchange` |
| **Content-Type** | `application/json` |
| **请求体** | `{ "code": "从 redirect_url 取得的一次性 code" }` |
| **code 有效期** | 60 秒 |
| **使用次数** | 最多成功一次；服务端原子读取并删除 |

**成功响应（HTTP 200）：**

```json
{
  "token": "7-day-login-token",
  "username": "zhangsan",
  "is_admin": false,
  "display_name": "张三"
}
```

`display_name` 可为 `null`。code 无效、已使用、已过期，或兑换时用户已失去登录资格，均返回
HTTP 401：`{"detail":"SSO code 无效、已使用或已过期"}`。Redis 不可用时返回 HTTP 503。

> 前端必须在兑换请求前用 `history.replaceState` 从地址栏移除 `sso_code`，避免一次性 code 长时间留在
> 浏览器历史。真正的 7 天登录 token 只出现在兑换响应中，不进入 URL。

### 签名规则

| 项 | 说明 |
|----|------|
| **签名密钥（SSO 专用）** | 使用双方现有 SSO Secret，永久保留、不轮换；只通过受控环境变量注入，不写入文档或代码仓库 |
| 签名算法 | HMAC-SHA256 |
| 签名串拼接 | `loginName + timestamp`（无分隔符，直接拼接） |
| 签名结果 | Base64 编码 |
| 时间窗口 | timestamp 与服务器时间差不超过 5 分钟（300 秒） |

**签名示例：**

```
loginName  = "zhangsan"
timestamp  = 1722326494123

签名串 = "zhangsan" + "1722326494123" = "zhangsan1722326494123"

sign = Base64( HMAC-SHA256(secretKey, "zhangsan1722326494123") )
```

**各语言签名代码示例：**

Python:
```python
import base64
import hashlib
import hmac
import os

secret = os.environ["OA_SSO_SECRET"]
sign_str = f"{'zhangsan'}{1722326494123}"
sign = base64.b64encode(hmac.new(secret.encode(), sign_str.encode(), hashlib.sha256).digest()).decode()
```

C#:
```csharp
using System.Security.Cryptography;
var secret = Environment.GetEnvironmentVariable("OA_SSO_SECRET");
var signStr = "zhangsan" + "1722326494123";
using var hmac = new HMACSHA256(Encoding.UTF8.GetBytes(secret));
var hash = hmac.ComputeHash(Encoding.UTF8.GetBytes(signStr));
var sign = Convert.ToBase64String(hash);
```

Java:
```java
import javax.crypto.Mac;
import javax.crypto.spec.SecretKeySpec;
import java.util.Base64;
String secret = System.getenv("OA_SSO_SECRET");
String signStr = "zhangsan" + "1722326494123";
Mac mac = Mac.getInstance("HmacSHA256");
mac.init(new SecretKeySpec(secret.getBytes("UTF-8"), "HmacSHA256"));
String sign = Base64.getEncoder().encodeToString(mac.doFinal(signStr.getBytes("UTF-8")));
```

### 律智荟配置入口所需信息

必智在律智荟后台配置「文档翻译」入口时，填入以下信息即可：

| 配置项 | 值 |
|--------|-----|
| 接口地址 | `https://translation.tylaw.com.cn:8080/api/sso/login` |
| 请求方式 | POST |
| Content-Type | application/json |
| 请求参数 | loginName（登录名）、timestamp（13位毫秒时间戳）、sign（签名） |
| 签名密钥 | 使用现有 `OA_SSO_SECRET`，通过受控环境变量配置，禁止写入仓库 |
| 签名方式 | HMAC-SHA256，签名串 = `loginName + timestamp`，Base64 输出 |
| 返回值 | 仅 `success`、`redirect_url`（用此 URL 跳转浏览器） |

---

## 四、用户数据同步（我方定时拉取）

### 流程

```
Celery Beat 定时任务（工作时段 9:00-18:00，每 3 小时同步一次）
  │
  ├── 0. 执行时间点：9:00, 12:00, 15:00, 18:00（共 4 次/天）
  │
  ├── 1. 调 generateToken 获取 Saury token
  │      POST https://e.tylaw.com.cn/api/generateToken
  │      Body: { AppKey, AppSecret, Timestamp }
  │
  ├── 2. 调 getemployees 拉取全员列表（分页）
  │      POST https://e.tylaw.com.cn/api/getemployees
  │      Headers: S-App-Key, S-Auth-Token, S-Timestamp
  │      Body: { PageNumber: 1, PageSize: 1000 }
  │
  ├── 3. 根据 Status="A" 且 InServiceStatus="在职" 计算 oa_employed
  │
  ├── 4. 与本地 users 表比对：
  │      - 新人员 → 自动创建 OA 专用账号（不创建本地密码，只能走 SSO）
  │      - loginName 精确为 admin → 无条件跳过，不创建、不绑定、不更新
  │      - 已有人员 → 更新姓名/邮箱/部门
  │      - 离职人员 → oa_employed=False（不删除）；无管理员覆盖时最终状态自动停用
  │      - 管理员强制启用/停用 → active_override=True/False，后续 OA 同步不得覆盖
  │
  └── 5. 记录同步日志
```

### 凭证信息

| 项 | 值 |
|----|-----|
| **律智荟 API baseUrl** | `https://e.tylaw.com.cn` |
| **AppKey** | 使用现有值，部署时从受控 `OA_APP_KEY` 注入；文档仅写占位符 |
| **SecretKey** | 使用现有值，部署时从受控 `OA_APP_SECRET` 注入；文档仅写占位符 |
| **SSO Secret** | 使用现有值，部署时从受控 `OA_SSO_SECRET` 注入；文档仅写占位符 |

> 现有 AppKey、AppSecret、SSO Secret 已确定**永久保留，不轮换、不改值**。真实值只能存在于权限受控的
> `deploy/.env` 或部署密钥系统中，不得出现在本文、示例、日志、截图或代码仓库。AppKey/AppSecret 用于
> generateToken / getemployees；SSO Secret 用于验证 `/api/sso/login` 签名。

### 律智荟接口注意事项

| 项 | 说明 |
|----|------|
| **ABP 包装格式** | 律智荟所有响应均被 ABP 框架包装，实际数据在 `result` 字段中。解析时取 `json["result"]`，而非直接解析整个 JSON |
| **响应结构** | generateToken：`result` 为字符串（token）；getemployees：`result.totalCount` + `result.items[]` |
| **success 判断** | `json["success"] == true` 表示成功，`json["error"]` 有值表示失败 |
| **日期时区** | 人员信息中的日期时间（SignDate / QuitDate / CreationTime 等）均为 **UTC+8 北京时间** |
| **Token 有效期** | 律智荟的 Saury token 有效期 **5 分钟**，建议每次同步前重新生成 |
| **S-Timestamp 一致性** | 调 getemployees 时 S-Timestamp 请求头的值必须与生成 token 时传入的 Timestamp **完全一致** |

### getemployees 返回字段映射

| OA 字段 | 本地 users 表字段 | 说明 |
|---------|-------------------|------|
| LoginName | username | 登录名（唯一，SSO 匹配用） |
| Name | display_name | 姓名 |
| Email | email | 邮箱 |
| Phone | phone | 手机号 |
| Department | department | 部门 |
| Category | — | 当前版本不落库；后续如启用 OA 角色映射需另行确认规则 |
| Status="A" + InServiceStatus="在职" | oa_employed=True | OA 原始在职状态；其他情况为 false。最终启用状态优先采用管理员覆盖，否则跟随该字段 |

> `admin` 是翻译系统保留的本地管理员用户名，只允许本地密码登录。律智荟中同名人员不参与同步或 SSO，
> 不得覆盖本地 `admin` 的密码哈希、资料、`auth_source`、管理员权限或启停状态。

---

## 五、完整调用流程示例

### 场景：用户张三从律智荟进入文档翻译系统

```
步骤 1：律智荟准备签名
  loginName  = "zhangsan"
  timestamp  = 1722326494123（当前毫秒时间戳）
  secret     = getenv("OA_SSO_SECRET")
  signString = "zhangsan1722326494123"
  sign       = Base64(HMAC-SHA256(secret, signString))

步骤 2：律智荟 POST 调用
  POST https://translation.tylaw.com.cn:8080/api/sso/login
  Content-Type: application/json

  {
    "loginName": "zhangsan",
    "timestamp": 1722326494123,
    "sign": "（上方计算出的签名）"
  }

步骤 3：我方返回
  {
    "success": true,
    "redirect_url": "https://translation.tylaw.com.cn:8080/?sso_code=one-time-code"
  }

步骤 4：律智荟跳转浏览器
  用户浏览器打开 redirect_url

步骤 5：文档翻译系统前端移除地址栏中的 sso_code，并兑换登录 token
  POST https://translation.tylaw.com.cn:8080/api/sso/exchange
  Content-Type: application/json

  { "code": "one-time-code" }

步骤 6：我方返回登录信息
  {
    "token": "7-day-login-token",
    "username": "zhangsan",
    "is_admin": false,
    "display_name": "张三"
  }

步骤 7：前端保存会话 → 进入任务页面
```

---

## 六、双方确认事项汇总

### ✅ 已确认（双方无异议）

| # | 事项 | 确认结果 |
|----|------|---------|
| 1 | SSO 用户标识字段 | **loginName**（唯一登录名） |
| 2 | SSO 签名密钥 | **SSO 专用密钥**（我方提供，见第三节） |
| 3 | 签名算法 | **HMAC-SHA256**，签名串 = `loginName + timestamp`，Base64 输出 |
| 4 | 跳转方式 | `https://translation.tylaw.com.cn:8080/?sso_code=xxx`；code 60 秒有效且单次使用 |
| 5 | 接口地址 | `https://translation.tylaw.com.cn:8080/api/sso/login` |
| 6 | 用户同步频率 | 工作时段 9:00-18:00 每 3 小时（共 4 次/天） |
| 7 | 数据同步方向 | 我方主动调律智荟 getemployees |
| 8 | SSO 方式 | POST API 调用（律智荟服务器调我方接口） |
| 9 | 登录响应 | `/api/sso/login` 仅返回 `success`、`redirect_url`；登录 token 由前端调用 `/api/sso/exchange` 获得 |
| 10 | OA 凭据 | 现有 AppKey、AppSecret、SSO Secret 永久保留、不轮换；文档只写占位符 |

### ⚠️ 域名生效前注意

域名 `translation.tylaw.com.cn` 解析和 SSL 证书配置完成后，以上地址才可访问。域名生效前可使用内网 IP `http://172.16.1.56:8080` 进行联调测试。
