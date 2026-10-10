# astrbot_plugin_tshock

基于 [TShock](https://github.com/Pryaxis/TShock) 服务器 **REST API** 的 AstrBot 插件，让你可以直接在聊天窗口里管理 Terraria 服务器。

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

- 作者：akexin
- 仓库：https://github.com/akexin/astrbot_plugin_tshock
- 许可：MIT

## 功能

- 🔍 **查询**：服务器状态、在线玩家列表、用户账号列表
- ⚙️ **命令**：向服务器广播消息、私聊指定玩家、直接执行服务器原始指令
- 🔗 **桥接**：目标会话消息 → 游戏内广播；服务器上下线、玩家进出 → 目标会话通知

## 环境要求

| 项目 | 要求 |
| --- | --- |
| AstrBot | **v4.0.0 及以上**（`metadata.yaml` 中的 `astrbot_version` 字段会在版本不符时阻止加载） |
| TShock | 6.x（已在 **TShock 6.2.1.0 / Terraria 1.4.5.8** 实测） |
| Python | 3.10+（插件使用了 `X \| None` 类型语法） |
| 依赖 | `httpx>=0.24`（由 `requirements.txt` 自动安装） |

## 遵循的 AstrBot 开发规范

本插件的实现严格对齐 [AstrBot 官方插件开发指南](https://docs.astrbot.app/dev/star/plugin-new.html)：

| 规范 | 本插件的做法 |
| --- | --- |
| 插件类继承 `Star`，入口文件为 `main.py` | ✅ `class TShockPlugin(Star)` 位于 `main.py` |
| 构造函数接收 `Context` 与 `AstrBotConfig` | ✅ `def __init__(self, context: Context, config: AstrBotConfig)` |
| 生命周期钩子 | ✅ `async def initialize(self)`（初始化）+ `async def terminate(self)`（清理） |
| 不需要 `@register` 装饰器（v3.5.19 起已废弃） | ✅ 元数据全部由 `metadata.yaml` 提供 |
| 指令组用 `@filter.command_group` + 组内 `@group.command` | ✅ 无 `self` 的 `def ts():` 写法 |
| 管理员指令用 `@filter.permission_type(PermissionType.ADMIN)` | ✅ 同时做函数体内显式校验兜底 |
| 主动消息用 `self.context.send_message(umo, MessageChain().message(...))` | ✅ 桥接推送 |
| 使用异步 HTTP 库（禁止 `requests`） | ✅ 使用 `httpx.AsyncClient` |
| 日志使用插件自带的 `self.logger` | ✅ 优先用 `self.logger`，旧版本无该属性时自动回退到模块级 `logger` |
| 持久化数据不放插件自身目录 | ✅ 配置存于 AstrBot 的 `data/config/`（由 `AstrBotConfig` 管理） |
| 声明 `metadata.yaml` 元数据字段 | ✅ 含 `display_name` / `short_desc` / `astrbot_version` / `support_platforms` |


## 安装

把整个 `astrbot_plugin_tshock` 目录放到 AstrBot 的插件目录中：

```
AstrBot/
└── data/
    └── plugins/
        └── astrbot_plugin_tshock/
            ├── main.py
            ├── metadata.yaml
            ├── _conf_schema.json
            ├── requirements.txt
            ├── LICENSE
            ├── README.md
            └── tools/
                └── setup_rest_token.py
```

然后在 AstrBot WebUI 的「插件」页找到「TShock 服务器管理」，点击卡片上的刷新图标（重载插件）即可。

### 安装 / 更新排障

- **从 GitHub 仓库地址安装**时，AstrBot 会访问 `raw.githubusercontent.com` 校验 `metadata.yaml`。若该域名被网络阻断，安装会在校验阶段失败——可在安装对话框的「加速 / 代理」框中填入一个 GitHub 代理前缀（例如 `https://gh-proxy.com`），AstrBot 会以 `代理前缀/https://raw.githubusercontent.com/...` 的形式请求。
- **最稳妥的方式是直接上传 zip**：把本仓库打包成 zip（根目录即插件文件，不要再套一层目录），在 WebUI「插件 → 安装插件 → 上传文件」中选择该 zip，由 AstrBot 解压安装。
- 若插件页提示「插件已存在」，请先**卸载**旧版再重新安装，否则可能仍加载缓存中的旧代码。
- 确认已生效：加载后日志应出现 `[tshock] 插件已加载 v1.1.3 ...`。

## 快速开始

### 第 1 步：开启 TShock REST API

编辑服务器的 `config.json`（TShock 6.x 中位于数据目录，形如 `<服务端>/tshock/config.json`，内容包裹在 `"Settings"` 键下）：

```json
"RestApiEnabled": true,
"RestApiPort": 7878
```

> 修改后**必须重启服务器**才会生效。TShock 在关闭时会把内存中的配置回写到磁盘，因此请先停服再改文件，否则改动会被覆盖。

### 第 2 步：让插件拿到 REST Token

TShock 的 REST 鉴权是「Token → 用户名 + 用户组」的映射。以下三种方式**任选其一**。

**方式一：在服务端 `config.json` 里预置 Token（免密，推荐自建服）**

在服务端 `config.json` 中加入 `ApplicationRestTokens`，**JSON 的键名就是你的 Token**：

```json
"ApplicationRestTokens": {
  "my-astrbot-token": {
    "Username": "astrbot",
    "UserGroupName": "owner"
  }
}
```

重启服务器后，把键名 `my-astrbot-token` 原样填入插件的 `token` 配置即可。

> ⚠️ 两个硬性要求（写错必然 403）：
>
> 1. `ApplicationRestTokens` 的值**只有 `Username` 与 `UserGroupName` 两个字段，没有 `Password`**（依据源码 `TShockAPI/Rest/SecureRest.cs` 的 `TokenData` 结构）。填 `Password` 不生效，服务端回写配置时也会把它丢弃。
> 2. `UserGroupName` 必须是服务器上**真实存在的用户组名**，且该组需持有对应的 `tshock.rest.*` 权限。组名拼错时该 Token 每次请求都返回 403。

**方式二：用账号密码换 Token**

```bash
python tools/setup_rest_token.py --host http://<主机>:<REST端口>
```

脚本会交互式询问账号密码（不回显、不写入任何文件），随后调用 `/v2/token/create` 并验证结果；把返回的 `token` 填入插件配置。

也可手动调用：

```bash
curl "http://<主机>:<REST端口>/v2/token/create?username=<账号>&password=<密码>"
```

**方式三：把账号密码直接交给插件**

插件的 `username` / `password` 配置项即为此设计：当 `token` 留空而这两项已填时，插件会在启动时自动调用 `/v2/token/create` 换取 Token（**仅内存缓存，不写入任何文件**）。

> 方式二、三都要求所用账号所属用户组具备 `tshock.rest.useapi` 权限（或组名恰为 `superadmin`）。若返回 `403 用户名或密码错误，也可能是该用户没有足够的权限`，即账号密码有误或该组缺权限。

### 第 3 步：配置插件

在 AstrBot WebUI 的插件配置面板里填写，或直接编辑 `data/config/astrbot_plugin_tshock_config.json`。也可以复制本仓库的 `astrbot_plugin_tshock_config.example.json` 作为起点。

## 配置项

| 配置项 | 说明 | 默认值 |
| --- | --- | --- |
| `host` | TShock REST API 基础地址 | `http://127.0.0.1:7878` |
| `token` | REST API Token（预置 Token 时填该 Token 的键名） | 空 |
| `username` | TShock 账号用户名；`token` 留空时用于自动换取 Token | 空 |
| `password` | TShock 账号密码；仅保存在 AstrBot 配置中 | 空 |
| `timeout` | HTTP 请求超时（秒） | `10` |
| `message_type` | 指令回执形式：`text` 纯文本 / `image` 图片 | `text` |
| `bridge.enabled` | 是否启用消息桥接 | `false` |
| `bridge.target_umo` | 接收服务器推送的会话（UMO） | 空 |
| `bridge.poll_interval` | 轮询服务器状态间隔（秒，下限 12） | `15` |
| `bridge.forward_to_server` | 目标会话消息转发到游戏内 | `false` |
| `bridge.forward_require_admin` | 转发消息时要求发送者为管理员 | `true` |
| `bridge.notify_join_leave` | 玩家上/下线通知 | `true` |

> `host` 可以省略 `http://` 前缀，插件会自动补全。

## 指令

指令组为 `/ts`（别名 `/TSHOCK`），每个子指令都有中文别名，可任选一种写法。

### 查询（所有人）

| 指令 | 中文别名 | 功能 |
| --- | --- | --- |
| `/ts status` | `/ts 状态`、`/ts 信息` | 查询服务器状态 |
| `/ts players` | `/ts 在线`、`/ts 玩家` | 查询在线玩家 |
| `/ts help` | `/ts 菜单`、`/ts 帮助`、`/ts h` | 显示指令菜单 |

### 管理（仅管理员）

| 指令 | 中文别名 | 功能 |
| --- | --- | --- |
| `/ts users` | `/ts 账号`、`/ts 用户` | 查询用户账号列表 |
| `/ts broadcast <消息>` | `/ts 广播 <消息>` | 向服务器广播 |
| `/ts say <玩家> <消息>` | `/ts 私聊 <玩家> <消息>` | 私聊指定玩家 |
| `/ts cmd <指令>` | `/ts 执行 <指令>`、`/ts 命令 <指令>` | 执行服务器原始命令 |

### 桥接

| 指令 | 中文别名 | 功能 |
| --- | --- | --- |
| `/ts here` | `/ts 这里` | 把当前会话设为桥接目标 |

管理员指令除了装饰器声明外，还在函数体内做了独立校验，以规避不同 AstrBot 版本对装饰器顺序处理的差异。

### 回执形式（文本 / 图片）

配置项 `message_type` 控制所有指令回执的呈现方式：

- `text`（默认）：直接回复纯文本。
- `image`：把回执渲染成图片发送。渲染依赖 `Pillow`（已列入 `requirements.txt`，AstrBot 会自动安装）与一个中文字体；插件内置跨平台字体候选列表（Windows `msyh.ttc`/`simhei.ttf`、Linux Noto CJK/文泉驿、macOS PingFang）。**任一环节失败都会自动回退为文本**，不会出现"没有回复"的情况。

## 桥接配置速成

1. 在你想接收服务器通知的群/私聊里发送 `/ts here`，插件会把该会话写入 `bridge.target_umo` 并开启桥接。
2. （可选）把 `bridge.forward_to_server` 打开，该会话的**管理员**消息就会以 `[昵称] 消息` 的形式广播到游戏内。
3. 之后玩家上下线、服务器上下线都会推送通知到该会话。

## 已知限制

1. **游戏内聊天内容无法回传**。TShock 官方 REST API 没有提供游戏内聊天记录的推送接口，因此无法做到「游戏聊天实时同步到机器人」。如需真正的聊天内容双向同步，需要在 TShock 侧配合额外插件，把聊天事件转发到 HTTP webhook。
2. **轮询受限流约束**。TShock 默认限制**每个 Token 每分钟 5 次** REST 请求（`RESTMaximumRequestsPerInterval`，恢复间隔 `RESTRequestBucketDecreaseIntervalMinutes`）。轮询间隔低于 12 秒必定触发限流，进而被误判为服务器离线。插件已强制下限为 12 秒；若需要更频繁的检测，请在服务端调大该配置。
3. **REST 端点是逐权限授权的**。除 `/v2/server/status` 公开外，其余端点要求 token 且绑定账号所属用户组必须具备对应权限（TShock 6.x 的权限名为 `tshock.rest.*`）：

   | 插件功能 | 端点 | 所需权限（推断） |
   | --- | --- | --- |
   | `/ts users` | `/v2/users/list` | `tshock.rest.users.view` |
   | `/ts broadcast` | `/v2/server/broadcast` | `tshock.rest.broadcast` |
   | `/ts cmd`、`/ts say` | `/v3/server/rawcmd` | `tshock.rest.command` |

   若账号缺少对应权限，端点会返回 403。特别注意：使用了新版用户组体系（`guest/default/vip/newadmin/admin/trustedadmin/owner`）的服务端**没有 `superadmin` 组**，且 `owner` 组默认不含 `tshock.rest.command`，此时需要手动为该组补权限。
4. `/ts say` 依赖 TShock 的 `whisper` 命令。命令参数按空格切分且**不解析引号**，因此玩家名中不能包含空格。
5. TShock 6.x 已移除 `/v2/server/rawcmd`、`/v2/server/motd`、`/v2/server/rules` 等旧端点，插件使用 `/v3/server/rawcmd`。

## 变更日志

### v1.1.5

**菜单重排 + 回执可选文本/图片**

- 新增 `message_type` 配置项：`text`（默认）/ `image`（把指令回执渲染成图片）。渲染失败自动回退为文本。
- 指令组别名 `/TSHOCK`；所有子指令增加中文别名（`/ts 状态`、`/ts 在线`、`/ts 广播`、`/ts 私聊`、`/ts 执行` 等）。
- 菜单按「查询 / 管理 / 桥接」三组重排。
- `/ts status` 精简：去掉「鉴权」与「REST」两行，只保留服务器自身信息。

### v1.1.4

**新增两种免手工取 Token 的连接方式**

- 新增 `username` / `password` 配置项：`token` 留空时，插件启动阶段自动调用 `/v2/token/create` 换取 Token（仅内存缓存，不落盘）；请求遇到 401/403 时也会自动重新登录并重试一次。
- 完善对服务端 `ApplicationRestTokens` 预置 Token 方式的说明：明确 `TokenData` **没有 `Password` 字段**，且 `UserGroupName` 必须是真实存在的用户组并持有对应 `tshock.rest.*` 权限。
- 401/403 错误信息补充排障提示，不再统一提示为「Token 错误」。
- `/ts status` 增加「鉴权」一行，便于确认 Token 是否已生效。

### v1.1.3

**兼容性修复（P0，彻底修复插件加载失败）**

- 彻底移除对 `self.logger` 的依赖，全部改用模块级 `astrbot.api.logger`。实测 **AstrBot v4.26.7** 的 `Star` 基类 `__init__` 只设置 `self.context`，并不注入 `self.logger`（该属性是更晚版本才加入的），因此任何使用 `self.logger` 的写法都会抛 `AttributeError: 'TShockPlugin' object has no attribute 'logger'` 并使插件加载失败。模块级 `logger` 在所有版本均可用。

### v1.1.2

**兼容性修复（P0，修复插件无法加载）**

- 主动消息由 `MessageChain().message()` 改为 `MessageChain(chain=[Plain(text)])` 构造式——后者在 AstrBot v4.17 上已验证可用，前者为较新 API。
- （v1.1.2 采用的 `hasattr` 回退方案，已在 v1.1.3 被更彻底的模块级 `logger` 方案取代。）

### v1.1.1

- 对齐 AstrBot 官方插件开发文档：
  - 生命周期钩子由 `@filter.on_astrbot_loaded()` 改为官方模板约定的 `async def initialize(self)`
  - 指令组 `ts` 改为文档推荐的无 `self` 写法
  - 主动消息改用 `MessageChain().message()` 构建
  - 日志改用插件自带的 `self.logger`，可在 WebUI 单独调节等级
  - 移除未使用的 `Comp` / 全局 `logger` 导入
- 元数据补全：`display_name`、`short_desc`、`astrbot_version`、`support_platforms`，版本号改为 `v1.1.1`

### v1.1.0

- 修复：`/ts say` 对参数加引号导致 `whisper` 找不到玩家（TShock 不剥离引号）
- 安全：`/ts here` 由「所有人」收紧为「管理员」
- 安全：转发到游戏服务器的消息默认要求发送者为管理员
- 安全：管理员指令增加函数体内显式权限校验作为兜底
- 修复：轮询间隔增加 12 秒下限，避免触发 REST 限流导致误判离线
- 修复：状态输出中 REST 地址与游戏端口的拼接错误
- 修复：插件重载后首轮轮询误报「服务器已上线」
- 修复：离线期间未清空玩家基线，恢复后会误报大量「玩家加入」
- 增强：`/ts status` 补充版本、运行时长信息；`host` 支持省略协议头；超长消息按 `MaximumChatMessageLength` 截断

### v1.0.0

- 首个版本：查询、命令、消息桥接

## 参考与致谢

- [AstrBot 插件开发指南](https://docs.astrbot.app/dev/star/plugin-new.html) —— 本插件的目录结构、生命周期、指令注册与配置 schema 均依此实现
- [TShock REST API 文档](https://tshock.readme.io/reference/rest-api-endpoints) —— 所有端点的调用依据
- 插件由 akexin 独立编写，未移植第三方代码

## 许可证

本项目采用 [MIT 许可证](LICENSE)，版权归 akexin 所有。
