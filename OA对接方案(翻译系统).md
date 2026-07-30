# 翻译系统 ↔ 律智荟 OA 对接方案

> 版本：v2.0 | 日期：2026-07-22 | 状态：✅ 双方已确认，准备开发

---

## 一、整体架构

```
用户浏览器
┌─────────────────────────┐    ┌──────────────────────────────────────┐
│   律智荟 OA（必智）      │    │  翻译系统                                │
│   e.tylaw.com.cn        │    │  translation.tylaw.com.cn:8080 (HTTPS) │
│                         │    │                                        │
│  ┌─────────────────┐    │    │  ┌────────────────────┐               │
│  │  律智荟系统      │    │    │  │ FastAPI 后端        │               │
│  │                 │    │    │  │                    │               │
│  │  用户点击入口    │────┼────┼─>│ /api/sso/login     │               │
│  │                 │    │ ①  │  │ (SSO 单点登录)      │               │
│  │                 │<───┼────┼──│ 返回登录 token       │               │
│  │                 │    │    │  └────────────────────┘               │
│  │                 │    │    │                                        │
│  │                 │    │    │  ┌────────────────────┐               │
│  │  人员数据        │<───┼────┼──│ Celery Beat 定时任务 │               │
│  │                 │    │ ②  │  │ /api/getemployees   │               │
│  │                 │────┼────┼─>│ 9-18点每3小时同步   │               │
│  │                 │    │    │  └────────────────────┘               │
│  └─────────────────┘    │    │                                        │
└─────────────────────────┘    └────────────────────────────────────────┘

① SSO 登录：律智荟服务器 POST 调我们的接口，传 loginName + 签名，我们返回登录 token
② 用户同步：我们定时调律智荟 getemployees，同步在职人员到本地 users 表
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

---

## 三、SSO 单点登录（POST API 方式）

### 流程

```
1. 用户在律智荟点击「文档翻译」入口
2. 律智荟服务器 POST 调用我们的接口：
   POST https://translation.tylaw.com.cn:8080/api/sso/login
   Body: { "loginName": "zhangsan", "timestamp": 1722326494123, "sign": "xxx" }
3. 我们验证签名 → 查本地 users 表 → 返回登录 token
4. 律智荟拿到 token，带用户浏览器跳转到：
   https://translation.tylaw.com.cn:8080/?token=xxx
5. 用户无感知进入翻译系统
```

### 我们提供给律智荟的接口

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
| token | string | 登录 token（有效期 **7 天**，过期后需重新走 SSO 流程） |
| redirect_url | string | 带上 token 的前端跳转地址，直接用浏览器打开即可登录 |

```json
{
  "success": true,
  "token": "eyJ1Ijoi...",
  "redirect_url": "https://translation.tylaw.com.cn:8080/?token=eyJ1Ijoi..."
}
```

**错误响应（统一 JSON 格式）：**

```json
{
  "success": false,
  "detail": "签名校验失败"
}
```

| HTTP 状态码 | detail 内容 | 触发条件 |
|------------|------------|---------|
| 400 | `缺少必填参数：loginName / timestamp / sign` | 请求体中任一必填字段缺失 |
| 400 | `时间戳格式错误，需为 13 位毫秒级` | timestamp 不是合法的 13 位数字 |
| 401 | `签名校验失败` | HMAC-SHA256 验证不通过 |
| 401 | `时间戳已过期` | timestamp 与服务器时间差超过 5 分钟（300 秒） |
| 404 | `用户不存在或已离职：{loginName}` | 本地 users 表无此 loginName 或 is_active=False |
| 500 | `服务器内部错误` | 意外异常 |

> timestamp 使用 **UTC 时间**（非北京时间），与必智接口文档中 generateToken 的时间戳标准一致（毫秒级 Unix 时间戳）。

### 签名规则

| 项 | 说明 |
|----|------|
| **签名密钥（SSO 专用）** | `u2DWyb5vm0Dby1ndNeeHJNE57uutJYOoAoN74sP5nXfGFT4AyI7JSc6AQLDe8NEZ` |
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
import hmac, hashlib, base64
secret = "u2DWyb5vm0Dby1ndNeeHJNE57uutJYOoAoN74sP5nXfGFT4AyI7JSc6AQLDe8NEZ"
sign_str = f"{'zhangsan'}{1722326494123}"
sign = base64.b64encode(hmac.new(secret.encode(), sign_str.encode(), hashlib.sha256).digest()).decode()
```

C#:
```csharp
using System.Security.Cryptography;
var secret = "u2DWyb5vm0Dby1ndNeeHJNE57uutJYOoAoN74sP5nXfGFT4AyI7JSc6AQLDe8NEZ";
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
String secret = "u2DWyb5vm0Dby1ndNeeHJNE57uutJYOoAoN74sP5nXfGFT4AyI7JSc6AQLDe8NEZ";
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
| 签名密钥 | `u2DWyb5vm0Dby1ndNeeHJNE57uutJYOoAoN74sP5nXfGFT4AyI7JSc6AQLDe8NEZ` |
| 签名方式 | HMAC-SHA256，签名串 = `loginName + timestamp`，Base64 输出 |
| 返回值 | success、token、redirect_url（用此 URL 跳转浏览器） |

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
  ├── 3. 过滤 Status="A" 且 InServiceStatus="在职" 的人员
  │
  ├── 4. 与本地 users 表比对：
  │      - 新人员 → 自动创建账号（初始随机密码）
  │      - 已有人员 → 更新姓名/邮箱/部门
  │      - 离职人员 → is_active=False（不删除，保留历史数据）
  │
  └── 5. 记录同步日志
```

### 凭证信息

| 项 | 值 |
|----|-----|
| **律智荟 API baseUrl** | `https://e.tylaw.com.cn` |
| **AppKey** | `app_d5ac8f3b0f0b4bb59a9495c15856bebc` |
| **SecretKey** | `ZXvcmYMMPYUVqTatM5akv5QrwXiR7B5b12x+EzTjKpU=` |

> 此凭证由必智提供，用于我方调用律智荟的 generateToken / getemployees 接口。

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
| Category | role | 角色（律师/助理等） |
| Status="A" + InServiceStatus="在职" | is_active=True | 在职状态 |

---

## 五、完整调用流程示例

### 场景：用户张三从律智荟进入翻译系统

```
步骤 1：律智荟准备签名
  loginName  = "zhangsan"
  timestamp  = 1722326494123（当前毫秒时间戳）
  secret     = "u2DWyb5vm0Dby1ndNeeHJNE57uutJYOoAoN74sP5nXfGFT4AyI7JSc6AQLDe8NEZ"
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
    "token": "eyJ1IjoiemhhbmdzYW4iLCJleHA...",
    "redirect_url": "https://translation.tylaw.com.cn:8080/?token=eyJ1IjoiemhhbmdzYW4iLCJleHA..."
  }

步骤 4：律智荟跳转浏览器
  用户浏览器打开 redirect_url → 自动登录翻译系统 → 进入任务页面
```

---

## 六、双方确认事项汇总

### ✅ 已确认（双方无异议）

| # | 事项 | 确认结果 |
|----|------|---------|
| 1 | SSO 用户标识字段 | **loginName**（唯一登录名） |
| 2 | SSO 签名密钥 | **SSO 专用密钥**（我方提供，见第三节） |
| 3 | 签名算法 | **HMAC-SHA256**，签名串 = `loginName + timestamp`，Base64 输出 |
| 4 | 跳转方式 | `https://translation.tylaw.com.cn:8080/?token=xxx` |
| 5 | 接口地址 | `https://translation.tylaw.com.cn:8080/api/sso/login` |
| 6 | 用户同步频率 | 工作时段 9:00-18:00 每 3 小时（共 4 次/天） |
| 7 | 数据同步方向 | 我方主动调律智荟 getemployees |
| 8 | SSO 方式 | POST API 调用（律智荟服务器调我方接口） |

### ⚠️ 域名生效前注意

域名 `translation.tylaw.com.cn` 解析和 SSL 证书配置完成后，以上地址才可访问。域名生效前可使用内网 IP `http://172.16.1.56:8080` 进行联调测试。
