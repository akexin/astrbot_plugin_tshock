# -*- coding: utf-8 -*-
"""
astrbot_plugin_tshock
=====================
基于 TShock REST API 的 Terraria 服务器机器人管理插件。

功能：
  1. 查询：服务器状态、在线玩家、用户账号列表
  2. 命令：广播、私聊、执行服务器原始命令
  3. 桥接：聊天室 -> 游戏服务器 消息转发；服务器上下线 / 玩家进出 -> 聊天室推送（轮询）

依赖：httpx（异步 HTTP），见 requirements.txt
参考：https://tshock.readme.io/reference/rest-api-endpoints

v1.1.5 变更
-----------
按「指令按用途分组」重做菜单与回执：
  - 新增配置项 ``message_type``：``text``（默认，纯文本）或 ``image``
    （把指令回执渲染成图片发送）。渲染失败时自动回退为文本，不会丢回复。
  - 指令组与子指令增加中文别名（如 ``/ts 状态``、``/ts 在线``、``/ts 广播``），
    菜单按「查询 / 管理 / 桥接」分组重排。
  - ``/ts status`` 精简输出：去掉「鉴权」与「REST」两行，仅保留服务器自身信息。

v1.1.4 变更
-----------
新增两种免手工取 Token 的连接方式：
  - 新增配置项 ``username`` / ``password``：当 ``token`` 留空时，插件会用账号密码
    调用 ``/v2/token/create`` 自动换取 REST Token（仅内存缓存，不写入插件目录）。
  - 若在服务端 config.json 的 ``ApplicationRestTokens`` 中预置了 Token，
    直接把该 Token（JSON 的键名）填入插件配置的 ``token`` 即可。
    注意：TShock 的 TokenData 结构只有 ``Username`` 与 ``UserGroupName`` 两个字段，
    **没有 ``Password``**；且 ``UserGroupName`` 必须是服务器上真实存在的用户组，
    该组还需持有对应的 ``tshock.rest.*`` 权限，否则鉴权时会返回 403。
  - 401/403 的错误信息补充排障提示（Token / 用户组 / 权限），避免统一误报为“Token 错误”。
  - ``/ts status`` 增加鉴权状态一行，便于确认 Token 是否已生效。

v1.1.3 变更
-----------
兼容性修复（P0）：
  - 彻底移除对 ``self.logger`` 的依赖，全部改用模块级 ``astrbot.api.logger``。
    实测 AstrBot v4.26.7 的 ``Star`` 基类 ``__init__`` 仅设置 ``self.context``，
    并未注入 ``self.logger``（该属性是更晚版本才加入的），因此任何直接使用
    ``self.logger`` 的写法都会抛 ``AttributeError`` 导致插件加载失败。
    模块级 ``logger`` 在所有版本均可用，是 AstrBot 官方插件模板的通用写法。

v1.1.2 变更
-----------
  - 主动消息改为 ``MessageChain(chain=[Plain(text)])`` 构造式，该写法在
    AstrBot v4.17 上已验证可用（``MessageChain().message()`` 为较新 API）。

v1.1.1 变更
-----------
对齐 AstrBot 官方插件开发文档：
  - 生命周期钩子由 ``@filter.on_astrbot_loaded()`` 改为官方模板约定的
    ``async def initialize(self)``。
  - 指令组 ``ts`` 改为文档推荐的无 ``self`` 形式。
  - 主动消息改用 ``MessageChain().message()`` 构建（官方推荐写法）。
  - 日志改用插件自带 logger（v1.1.3 起回归模块级 ``logger`` 以兼容全部版本）。
  - 补全 metadata.yaml 的 ``short_desc`` / ``support_platforms`` / ``astrbot_version``。
  - 依官方指南说明：``@register`` 装饰器已废弃，本插件通过 ``metadata.yaml`` 注册元数据。

v1.1.0 变更
-----------
修复
  - P0  /ts say 使用 whisper 时对参数加了引号，TShock 命令解析器不剥离引号，
        导致玩家名匹配失败；改为不加引号的裸参数形式。
  - P2  状态输出把 REST 主机与游戏端口拼接成 ``http://host:7878:7777``，语义错误。
  - P2  插件重载后首轮轮询把服务器状态当作变化，误推一条“服务器已上线”。
  - P2  离线期间保留的玩家基线未清空，服务器恢复后会误报大量“玩家加入”。
安全
  - P1  /ts here 原为所有人可用，任何群成员都能把桥接目标改到自己的会话并开启转发；
        现限制为管理员，并在函数体内做显式权限校验（不依赖装饰器顺序）。
  - P1  _on_all_message 转发到游戏服务器时未做任何身份校验，群内任意成员都能向
        游戏内广播；现默认要求发送者为管理员（forward_require_admin）。
  - 管理员指令统一增加函数体内显式校验 ``_deny_if_not_admin``，作为装饰器的兜底。
稳定性
  - P1  TShock 默认限制每个 Token 每分钟 5 次 REST 请求（RESTMaximumRequestsPerInterval），
        原默认 10 秒轮询为 6 次/分钟，必然被限流并误判为离线；
        现默认 15 秒并强制不低于 ``MIN_POLL_INTERVAL``（12 秒）。
  - 转发消息按 TShock MaximumChatMessageLength 截断，避免超长广播被服务端拒绝。
"""

import asyncio
import hashlib
import os
import re
import tempfile
from typing import Any

import httpx

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.message_components import Plain
from astrbot.api.star import Context, Star

__version__ = "1.1.5"

# TShock 默认每分钟 5 次 REST 请求，低于 12 秒的轮询必然触发限流并误判离线。
MIN_POLL_INTERVAL = 12
# TShock 默认 MaximumChatMessageLength，超长消息先截断再发送。
MAX_FORWARD_LENGTH = 500

# message_type 的取值
MESSAGE_TYPE_TEXT = "text"
MESSAGE_TYPE_IMAGE = "image"
# 图片渲染参数（像素）
IMAGE_WIDTH = 760
IMAGE_PADDING = 28
IMAGE_FONT_SIZE = 24
IMAGE_TITLE_SIZE = 30
IMAGE_LINE_GAP = 12

# 中文/等宽字体候选路径（按优先级），跨平台兜底。
# 中文字体通常不含 emoji 字形，渲染图片时需先剥离，否则会显示成方块。
# 文本回执不受影响，仍会保留 emoji。
_EMOJI_RE = re.compile(
    "["
    "\U0001f000-\U0001faff"  # 各类 emoji / 符号
    "\U00002600-\U000027bf"  # 杂项符号与装饰符号（✅❌⛔ 等）
    "\U00002190-\U000021ff"  # 箭头
    "\U0000fe0f"             # 变体选择符
    "\U0000200d"             # 零宽连接符
    "]+"
)

FONT_CANDIDATES = (
    "C:/Windows/Fonts/msyh.ttc",
    "C:/Windows/Fonts/simhei.ttf",
    "C:/Windows/Fonts/simsun.ttc",
    "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
    "/System/Library/Fonts/PingFang.ttc",
    "/System/Library/Fonts/STHeiti Medium.ttc",
)


def _pick_font(size: int):
    """加载一个可用的字体，全部不可用时返回 None（调用方回退为文本）。"""
    from PIL import ImageFont

    for path in FONT_CANDIDATES:
        if os.path.exists(path):
            try:
                return ImageFont.truetype(path, size)
            except Exception:  # noqa: BLE001
                continue
    return None


def render_text_image(text: str) -> str | None:
    """把纯文本渲染为一张 PNG，返回本地文件路径；失败返回 None。

    设计目标：不依赖任何外部服务，离线可用；渲染失败由调用方回退为文本，
    因此这里所有异常都被吞掉并返回 None。
    """
    try:
        from PIL import Image, ImageDraw
    except Exception:  # noqa: BLE001  Pillow 未安装
        logger.warning("[tshock] 未安装 Pillow，无法渲染图片，已回退为文本")
        return None

    lines = (text or "").splitlines() or [""]
    # 剥离 emoji（中文字体无对应字形会渲染成方块），并清掉剥离后残留的行首空白
    cleaned: list[str] = []
    for line in lines:
        m = _EMOJI_RE.match(line)
        if m:
            line = line[m.end():].lstrip()
        else:
            line = _EMOJI_RE.sub("", line)
        cleaned.append(line.rstrip())
    lines = cleaned

    body_font = _pick_font(IMAGE_FONT_SIZE)
    title_font = _pick_font(IMAGE_TITLE_SIZE)
    if body_font is None:
        logger.warning("[tshock] 未找到可用字体，无法渲染图片，已回退为文本")
        return None

    # 先按可用宽度做一次折行（按字符宽度估算，CJK 计为等宽）
    max_width = IMAGE_WIDTH - IMAGE_PADDING * 2
    wrapped: list[str] = []
    for line in lines:
        if not line:
            wrapped.append("")
            continue
        cur = ""
        for ch in line:
            if body_font.getlength(cur + ch) > max_width:
                wrapped.append(cur)
                cur = ch
            else:
                cur += ch
        wrapped.append(cur)

    line_h = IMAGE_FONT_SIZE + IMAGE_LINE_GAP
    height = IMAGE_PADDING * 2 + line_h * len(wrapped)
    img = Image.new("RGB", (IMAGE_WIDTH, max(height, 120)), (255, 255, 255))
    draw = ImageDraw.Draw(img)

    # 顶部标题色条
    accent = (32, 122, 224)
    draw.rectangle([0, 0, IMAGE_WIDTH, 6], fill=accent)

    y = IMAGE_PADDING
    for line in wrapped:
        font = title_font or body_font
        color = (24, 28, 36)
        # 形如 "【查询】" 的行做标题样式
        if line.startswith("【") and line.endswith("】"):
            color = accent
            font = title_font or body_font
        draw.text((IMAGE_PADDING, y), line, font=font, fill=color)
        y += line_h

    out_dir = os.path.join(tempfile.gettempdir(), "astrbot_plugin_tshock")
    os.makedirs(out_dir, exist_ok=True)
    name = hashlib.md5((text or "").encode("utf-8")).hexdigest()[:16] + ".png"
    path = os.path.join(out_dir, name)
    img.save(path, "PNG")
    return path


class TShockAPIError(Exception):
    """TShock REST API 请求错误。"""

    def __init__(self, message: str, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code


class TShockClient:
    """极简的 TShock REST API 异步客户端。

    TShock 的 REST API 通过在 query string 中附加 token 参数完成鉴权。
    参考：https://tshock.readme.io/reference/rest-api-endpoints

    端点可用性（以 TShock 6.2.1.0 实测为准）：
      - ``/v2/server/status``   公开，无需 token
      - ``/v2/server/broadcast`` 需 token
      - ``/v2/users/list``       需 token
      - ``/v3/server/rawcmd``    需 token
      - ``/v2/server/rawcmd``    已在 6.x 移除，返回 404
      - ``/v2/server/motd`` / ``/v2/server/rules`` 已在 6.x 移除，返回 404
    """

    def __init__(
        self,
        host: str,
        token: str,
        timeout: int = 10,
        username: str = "",
        password: str = "",
    ):
        host = (host or "").strip().rstrip("/")
        # 容错：允许用户只填 "域名:端口" 而省略协议头
        if host and not host.startswith(("http://", "https://")):
            host = "http://" + host
        self.host = host
        self.token = (token or "").strip()
        self.timeout = timeout
        # 可选：账号密码。仅在 token 为空时用于自动换取 REST Token。
        self.username = (username or "").strip()
        self.password = password or ""
        self._client: httpx.AsyncClient | None = None

    async def _client_get(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(timeout=self.timeout)
        return self._client

    async def ensure_token(self) -> bool:
        """在未配置 token 时，用账号密码向 ``/v2/token/create`` 换取 REST Token。

        TShock 的 ``/v2/token/create`` 需要账号密码，且该账号所在用户组必须具备
        ``tshock.rest.useapi`` 权限（或组名恰为 ``superadmin``）。
        换到的 Token 仅缓存在内存中，不写入配置文件。

        参考：TShockAPI/Rest/SecureRest.cs -> NewTokenInternal
        """
        if self.token:
            return True
        if not (self.username and self.password):
            return False
        try:
            data = await self._get(
                "/v2/token/create",
                {"username": self.username, "password": self.password},
                with_token=False,
                _retry=False,
            )
        except TShockAPIError as exc:
            logger.error("[tshock] 用账号密码换取 REST Token 失败：%s", exc)
            return False

        token = ""
        if isinstance(data, dict):
            token = str(data.get("token") or "").strip()
        if not token:
            logger.error("[tshock] /v2/token/create 未返回 token，响应：%s", data)
            return False

        self.token = token
        logger.info("[tshock] 已通过账号 %s 自动获取 REST Token", self.username)
        return True

    async def _get(
        self,
        path: str,
        params: dict | None = None,
        *,
        with_token: bool = True,
        _retry: bool = True,
    ) -> Any:
        base_params = dict(params or {})
        query = dict(base_params)
        if with_token and self.token:
            query["token"] = self.token

        client = await self._client_get()
        url = f"{self.host}{path}"

        try:
            resp = await client.get(url, params=query)
        except httpx.HTTPError as exc:
            raise TShockAPIError(f"无法连接 TShock 服务器: {exc}") from exc

        # 鉴权失败且配置了账号密码时，自动重新登录并重试一次。
        if (
            resp.status_code in (401, 403)
            and with_token
            and _retry
            and self.username
            and self.password
        ):
            self.token = ""
            if await self.ensure_token():
                return await self._get(
                    path, base_params, with_token=True, _retry=False
                )

        if resp.status_code >= 400:
            detail = ""
            try:
                data = resp.json()
                if isinstance(data, dict):
                    detail = str(
                        data.get("error")
                        or data.get("response")
                        or data.get("status")
                        or ""
                    )
                else:
                    detail = str(data)
            except Exception:
                detail = resp.text[:200]

            message = f"TShock 返回错误 (HTTP {resp.status_code})"
            if resp.status_code in (401, 403):
                message += (
                    "：鉴权失败。请检查插件配置的 Token；"
                    "若使用服务端 config.json 中 ApplicationRestTokens 预置的 Token，"
                    "请确认其 UserGroupName 是服务器上真实存在的用户组，且该组持有对应的 "
                    "tshock.rest.* 权限（该结构没有 Password 字段，勿填密码）"
                )
            if detail:
                message += f": {detail}"
            raise TShockAPIError(message, status_code=resp.status_code)

        try:
            return resp.json()
        except Exception:
            return resp.text

    async def aclose(self) -> None:
        if self._client is not None and not self._client.is_closed:
            await self._client.aclose()

    # ---------------- 服务器 ----------------
    async def status(self, players: bool = False) -> Any:
        return await self._get(
            "/v2/server/status",
            {"players": "true" if players else "false"},
        )

    async def broadcast(self, msg: str) -> Any:
        return await self._get("/v2/server/broadcast", {"msg": msg})

    async def rawcmd(self, cmd: str) -> Any:
        """执行服务器原始命令。

        优先使用 v3（会返回命令输出）。v2 版本端点在 TShock 6.x 已被移除，
        保留回退分支仅用于兼容 4.x/5.x 的旧服务端。
        """
        try:
            return await self._get("/v3/server/rawcmd", {"cmd": cmd})
        except TShockAPIError as exc:
            if exc.status_code == 404:
                return await self._get("/v2/server/rawcmd", {"cmd": cmd})
            raise

    async def users_list(self) -> Any:
        return await self._get("/v2/users/list")


class TShockPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        # 注意：``self.logger``（插件专属日志器）仅在较新版本 AstrBot 的 Star 基类中
        # 由 ``__init__`` 注入；实测 AstrBot v4.26.7 的基类只设置 ``self.context``，
        # 并无该属性。为兼容所有版本，本插件统一使用模块级 ``astrbot.api.logger``，
        # 不再依赖 ``self.logger``（这是 AstrBot 官方插件模板的通用写法）。
        self.config = config
        self.host = str(config.get("host", "http://127.0.0.1:7878")).strip()
        self.token = str(config.get("token", "")).strip()
        # 可选：账号密码（token 为空时由插件自动换取 Token）
        self.username = str(config.get("username", "")).strip()
        self.password = str(config.get("password", ""))
        self.timeout = int(config.get("timeout", 10) or 10)
        # 回执形式：text（默认）或 image（渲染成图片发送）
        self.message_type = str(config.get("message_type", MESSAGE_TYPE_TEXT)).strip().lower()
        if self.message_type not in (MESSAGE_TYPE_TEXT, MESSAGE_TYPE_IMAGE):
            self.message_type = MESSAGE_TYPE_TEXT
        self.client = TShockClient(
            self.host,
            self.token,
            self.timeout,
            username=self.username,
            password=self.password,
        )

        # 桥接相关状态
        self._bridge_task: asyncio.Task | None = None
        self._last_players: set[str] | None = None
        self._was_online: bool | None = None

    # ==================== 生命周期 ====================

    async def initialize(self) -> None:
        """实例化完成后由 AstrBot 自动调用（官方模板约定的异步初始化钩子）。

        参考：docs/dev/star/guides/simple —— "可选择实现异步的插件初始化方法"。
        """
        logger.info(
            "[tshock] 插件已加载 v%s，REST 地址: %s，桥接: %s",
            __version__,
            self.host,
            bool((self.config.get("bridge") or {}).get("enabled")),
        )
        # 未配置 token 但提供了账号密码时，启动阶段自动换取 Token（失败仅告警，不阻断加载）。
        if not self.token and self.username and self.password:
            await self.client.ensure_token()
        self._sync_bridge_task()

    async def terminate(self) -> None:
        """插件被卸载/停用时调用，做资源清理。"""
        if self._bridge_task is not None and not self._bridge_task.done():
            self._bridge_task.cancel()
            self._bridge_task = None
        await self.client.aclose()

    def _sync_bridge_task(self) -> None:
        """根据当前配置启动/停止桥接轮询任务。"""
        bridge = self.config.get("bridge") or {}
        enabled = bool(bridge.get("enabled"))
        if enabled:
            if self._bridge_task is None or self._bridge_task.done():
                self._bridge_task = asyncio.create_task(self._bridge_loop())
        else:
            if self._bridge_task is not None and not self._bridge_task.done():
                self._bridge_task.cancel()
                self._bridge_task = None

    # ==================== 权限工具 ====================

    @staticmethod
    def _is_admin(event: AstrMessageEvent) -> bool:
        """判断消息发送者是否为管理员，兼容不同 AstrBot 版本的事件对象。"""
        try:
            if event.is_admin():
                return True
        except Exception:  # noqa: BLE001
            pass
        try:
            return str(getattr(event, "role", "")).lower() == "admin"
        except Exception:  # noqa: BLE001
            return False

    def _deny_if_not_admin(self, event: AstrMessageEvent) -> str | None:
        """管理员指令的统一兜底校验：通过返回 None，否则返回拒绝提示。"""
        if self._is_admin(event):
            return None
        return "⛔ 该指令仅限管理员使用。"

    # ==================== 回执（文本 / 图片） ====================

    def _result(self, event: AstrMessageEvent, text: str):
        """按 ``message_type`` 生成指令回执。

        - ``text`` ：普通文本结果。
        - ``image``：把文本渲染成 PNG 后作为图片发送；渲染失败（缺 Pillow、
          缺字体等）会**自动回退为文本**，保证任何情况下都有回复。
        """
        if self.message_type == MESSAGE_TYPE_IMAGE:
            path = render_text_image(text)
            if path:
                try:
                    return event.image_result(path)
                except Exception as exc:  # noqa: BLE001
                    logger.warning("[tshock] 发送图片失败，回退为文本：%s", exc)
        return event.plain_result(text)

    # ==================== 指令组 ====================

    @filter.command_group("ts", alias={"TSHOCK"})
    def ts():
        """TShock 服务器管理指令组（指令组的函数体不需要实现任何逻辑）。

        注意：``filter.command_group`` 会把该函数替换为一个 ``RegisteringCommandable``
        对象，因此按官方文档写法不声明 ``self``。
        """

    @ts.command("help", alias={"帮助", "菜单", "h"})
    async def ts_help(self, event: AstrMessageEvent):
        """显示指令菜单"""
        yield self._result(event, self._help_text())

    @ts.command("status", alias={"状态", "信息"})
    async def ts_status(self, event: AstrMessageEvent):
        """查询服务器状态"""
        try:
            data = await self.client.status(players=False)
            yield self._result(event, self._format_status(data))
        except TShockAPIError as exc:
            yield self._result(event, f"❌ {exc}")

    @ts.command("players", alias={"在线", "玩家"})
    async def ts_players(self, event: AstrMessageEvent):
        """查询在线玩家列表"""
        try:
            data = await self.client.status(players=True)
            players = data.get("players") if isinstance(data, dict) else None
            names = self._normalize_players(players or [])
            total = len(names)
            maxp = data.get("maxplayers", "?") if isinstance(data, dict) else "?"
            if not names:
                yield self._result(event, "🏝️ 当前没有玩家在线。")
                return
            yield self._result(
                event,
                f"👥 在线玩家（{total}/{maxp}）：\n"
                + "\n".join(f"· {n}" for n in names),
            )
        except TShockAPIError as exc:
            yield self._result(event, f"❌ {exc}")

    @ts.command("here", alias={"这里"})
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def ts_here(self, event: AstrMessageEvent):
        """将当前会话设为桥接目标会话（管理员）"""
        deny = self._deny_if_not_admin(event)
        if deny:
            yield self._result(event, deny)
            return

        umo = event.unified_msg_origin
        bridge = dict(self.config.get("bridge") or {})
        bridge["target_umo"] = umo
        bridge["enabled"] = True
        self.config["bridge"] = bridge
        self.config.save_config()
        self._sync_bridge_task()

        # 切换目标后重置基线，避免把历史状态当成变化推送
        self._last_players = None
        self._was_online = None

        yield self._result(
            event,
            f"✅ 已将当前会话设为桥接目标：\n{umo}\n"
            "服务器/玩家上下线通知将推送到此会话。",
        )

    @ts.command("users", alias={"账号", "用户"})
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def ts_users(self, event: AstrMessageEvent):
        """查询用户账号列表（管理员）"""
        deny = self._deny_if_not_admin(event)
        if deny:
            yield self._result(event, deny)
            return
        try:
            data = await self.client.users_list()
            users = data.get("users") if isinstance(data, dict) else None
            if not users:
                yield self._result(event, "没有查询到用户账号。")
                return
            lines = []
            for u in users:
                if isinstance(u, dict):
                    lines.append(
                        f"· {u.get('name', '?')} (#{u.get('id', '?')}) [{u.get('group', '?')}]"
                    )
                else:
                    lines.append(f"· {u}")
            yield self._result(event, "🗂️ 用户账号列表：\n" + "\n".join(lines))
        except TShockAPIError as exc:
            yield self._result(event, f"❌ {exc}")

    @ts.command("broadcast", alias={"广播"})
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def ts_broadcast(self, event: AstrMessageEvent):
        """向服务器广播消息（管理员）"""
        deny = self._deny_if_not_admin(event)
        if deny:
            yield self._result(event, deny)
            return
        msg = self._text_after(event, ("broadcast", "广播"))
        if not msg:
            yield self._result(event, "用法：/ts broadcast <消息>")
            return
        msg = self._truncate(msg)
        try:
            await self.client.broadcast(msg)
            yield self._result(event, f"📢 已广播：{msg}")
        except TShockAPIError as exc:
            yield self._result(event, f"❌ {exc}")

    @ts.command("say", alias={"私聊"})
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def ts_say(self, event: AstrMessageEvent):
        """向指定玩家私聊（管理员）"""
        deny = self._deny_if_not_admin(event)
        if deny:
            yield self._result(event, deny)
            return
        text = self._text_after(event, ("say", "私聊"))
        parts = text.split(None, 1)
        if len(parts) < 2:
            yield self._result(event, "用法：/ts say <玩家名> <消息>")
            return
        player, msg = parts[0], self._truncate(parts[1])
        try:
            # 注意：不要给参数加引号。TShock 的命令解析器按空格切分参数且不剥离引号，
            # 加了引号会把玩家名变成 '"星"' 从而匹配不到玩家。
            await self.client.rawcmd(f"whisper {player} {msg}")
            yield self._result(event, f"💬 已私聊 {player}：{msg}")
        except TShockAPIError as exc:
            yield self._result(event, f"❌ {exc}")

    @ts.command("cmd", alias={"执行", "命令"})
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def ts_cmd(self, event: AstrMessageEvent):
        """执行服务器原始命令（管理员）"""
        deny = self._deny_if_not_admin(event)
        if deny:
            yield self._result(event, deny)
            return
        cmd = self._text_after(event, ("cmd", "执行", "命令"))
        if not cmd:
            yield self._result(event, "用法：/ts cmd <服务器指令>")
            return
        try:
            result = await self.client.rawcmd(cmd)
            yield self._result(
                event,
                "🖥️ 命令执行结果：\n" + self._format_command_result(result),
            )
        except TShockAPIError as exc:
            yield self._result(event, f"❌ {exc}")

    # ==================== 消息监听（机器人 -> 服务器 转发） ====================

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def _on_all_message(self, event: AstrMessageEvent) -> None:
        """把桥接目标会话中的消息转发到游戏服务器。"""
        bridge = self.config.get("bridge") or {}
        if not bridge.get("enabled") or not bridge.get("forward_to_server"):
            return

        target = str(bridge.get("target_umo", "")).strip()
        if not target or event.unified_msg_origin != target:
            return

        text = (event.message_str or "").strip()
        if not text:
            return
        # 跳过指令，避免重复/误转发
        if text.startswith("/") or text.startswith("ts "):
            return

        # 安全：默认仅转发管理员的消息，避免任意群成员向游戏内广播。
        if bridge.get("forward_require_admin", True) and not self._is_admin(event):
            logger.debug(
                "[tshock] 非管理员消息，跳过转发：%s", event.get_sender_name()
            )
            return

        sender = event.get_sender_name() or "机器人"
        text = self._truncate(text)
        try:
            await self.client.broadcast(f"[{sender}] {text}")
        except TShockAPIError as exc:
            logger.warning("[tshock] 转发消息到服务器失败: %s", exc)

    # ==================== 桥接轮询（服务器 -> 机器人 推送） ====================

    async def _bridge_loop(self) -> None:
        """周期性轮询服务器状态，推送上线/下线与玩家进出。"""
        while True:
            bridge = self.config.get("bridge") or {}
            try:
                interval = int(bridge.get("poll_interval", 15) or 15)
            except (TypeError, ValueError):
                interval = 15
            if interval < MIN_POLL_INTERVAL:
                interval = MIN_POLL_INTERVAL

            try:
                await self._poll_once()
            except asyncio.CancelledError:
                break
            except Exception as exc:  # noqa: BLE001
                logger.error("[tshock] 桥接轮询异常: %s", exc)

            await asyncio.sleep(interval)

    async def _poll_once(self) -> None:
        bridge = self.config.get("bridge") or {}
        target = str(bridge.get("target_umo", "")).strip()
        if not bridge.get("enabled") or not target:
            return

        try:
            data = await self.client.status(players=True)
            online = True
        except TShockAPIError:
            online = False
            data = None

        # 首次轮询只建立基线，不推送，避免插件重载时误报“服务器已上线”。
        first_run = self._was_online is None
        if not first_run and online != self._was_online:
            if online:
                await self._notify(target, "🟢 TShock 服务器已上线")
            else:
                await self._notify(target, "🔴 TShock 服务器连接异常/离线")
        self._was_online = online

        if not online or not isinstance(data, dict):
            # 离线/异常时清空玩家基线，恢复后不会误报大量“玩家加入”。
            self._last_players = None
            return

        # 玩家加入/离开
        if bridge.get("notify_join_leave", True):
            names = set(self._normalize_players(data.get("players") or []))
            if self._last_players is not None:
                for name in sorted(names - self._last_players):
                    await self._notify(target, f"✅ 玩家 {name} 加入服务器")
                for name in sorted(self._last_players - names):
                    await self._notify(target, f"🚪 玩家 {name} 离开服务器")
            self._last_players = names

    async def _notify(self, target: str, text: str) -> None:
        """向桥接目标会话主动发送一条文本消息。

        使用 ``MessageChain(chain=[Plain(text)])`` 构造消息链，该构造式在
        AstrBot v4.17 与更新版本上均可用。
        参考：docs/dev/star/guides/send-message —— 主动消息。
        """
        try:
            chain = MessageChain(chain=[Plain(text)])
            await self.context.send_message(target, chain)
        except Exception as exc:  # noqa: BLE001
            logger.error("[tshock] 推送桥接消息失败: %s", exc)

    # ==================== 工具方法 ====================

    @staticmethod
    def _truncate(text: str) -> str:
        """按 TShock 的 MaximumChatMessageLength 截断过长消息。"""
        text = (text or "").strip()
        if len(text) > MAX_FORWARD_LENGTH:
            return text[:MAX_FORWARD_LENGTH] + "…"
        return text

    @staticmethod
    def _normalize_players(players: list) -> list[str]:
        """兼容不同的玩家数据结构（字符串列表或字典列表）。"""
        names: list[str] = []
        for p in players:
            if isinstance(p, dict):
                names.append(
                    str(
                        p.get("nickname")
                        or p.get("username")
                        or p.get("name")
                        or p
                    )
                )
            else:
                names.append(str(p))
        return names

    def _format_status(self, data: Any) -> str:
        """格式化服务器状态信息（只输出服务器自身信息）。"""
        if not isinstance(data, dict):
            return f"服务器返回：\n{data}"
        name = data.get("name") or "未设置"
        port = data.get("port", "?")
        world = data.get("world", "?")
        pc = data.get("playercount", data.get("players_count", "?"))
        mp = data.get("maxplayers", "?")
        tsv = data.get("tshockversion", "?")
        ter = data.get("serverversion", "?")
        uptime = data.get("uptime", "?")
        return (
            "🌀 TShock 服务器状态\n"
            f"名称：{name}\n"
            f"世界：{world}\n"
            f"玩家：{pc}/{mp}\n"
            f"游戏端口：{port}\n"
            f"版本：TShock {tsv} / Terraria {ter}\n"
            f"运行时长：{uptime}"
        )

    # ==================== 菜单 ====================

    @staticmethod
    def _help_text() -> str:
        """按用途分组的指令菜单。

        以 ``【】`` 包裹的行会被图片渲染器识别为小标题。
        """
        return (
            "🌀 TShock 服务器管理\n"
            "\n"
            "【查询】所有人可用\n"
            "  /ts status   | /ts 状态     服务器状态\n"
            "  /ts players  | /ts 在线     在线玩家\n"
            "  /ts help     | /ts 菜单     显示本菜单\n"
            "\n"
            "【管理】仅管理员\n"
            "  /ts users      | /ts 账号     用户账号列表\n"
            "  /ts broadcast  | /ts 广播     向服务器广播消息\n"
            "  /ts say        | /ts 私聊     向指定玩家私聊\n"
            "  /ts cmd        | /ts 执行     执行服务器原始命令\n"
            "\n"
            "【桥接】聊天室与游戏服务器互通\n"
            "  /ts here  | /ts 这里   把当前会话设为桥接目标\n"
            "  启用后服务器与玩家上下线会推送到该会话"
        )

    @staticmethod
    def _format_command_result(result: Any) -> str:
        """格式化原始命令的执行结果。"""
        if isinstance(result, dict):
            value = result.get("response") or result.get("output") or result.get("error")
            if value is None:
                return str(result)
        else:
            value = result
        if isinstance(value, list):
            return "\n".join(str(x) for x in value) or "(无输出)"
        text = str(value).strip()
        return text or "(无输出)"

    @staticmethod
    def _text_after(event: AstrMessageEvent, subcommands: str | tuple | set) -> str:
        """从原始消息中提取子命令之后的文本。

        ``subcommands`` 可以是单个名称或名称集合（含别名），命中任意一个即可。
        """
        if isinstance(subcommands, str):
            accepted = {subcommands}
        else:
            accepted = set(subcommands)
        parts = (event.message_str or "").strip().split()
        for i, token in enumerate(parts):
            if token in accepted:
                return " ".join(parts[i + 1 :])
        return ""
