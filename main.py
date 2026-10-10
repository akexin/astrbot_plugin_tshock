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

v1.1.1 变更
-----------
对齐 AstrBot 官方插件开发文档：
  - 生命周期钩子由 ``@filter.on_astrbot_loaded()`` 改为官方模板约定的
    ``async def initialize(self)``。
  - 指令组 ``ts`` 改为文档推荐的无 ``self`` 形式。
  - 主动消息改用 ``MessageChain().message()`` 构建（官方推荐写法）。
  - 日志改用插件自带的 ``self.logger``，可在 WebUI 单独调节等级。
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
from typing import Any

import httpx

from astrbot.api import AstrBotConfig
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.star import Context, Star

__version__ = "1.1.1"

# TShock 默认每分钟 5 次 REST 请求，低于 12 秒的轮询必然触发限流并误判离线。
MIN_POLL_INTERVAL = 12
# TShock 默认 MaximumChatMessageLength，超长消息先截断再发送。
MAX_FORWARD_LENGTH = 500


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

    def __init__(self, host: str, token: str, timeout: int = 10):
        host = (host or "").strip().rstrip("/")
        # 容错：允许用户只填 "域名:端口" 而省略协议头
        if host and not host.startswith(("http://", "https://")):
            host = "http://" + host
        self.host = host
        self.token = (token or "").strip()
        self.timeout = timeout
        self._client: httpx.AsyncClient | None = None

    async def _client_get(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(timeout=self.timeout)
        return self._client

    async def _get(self, path: str, params: dict | None = None) -> Any:
        params = dict(params or {})
        if self.token:
            params["token"] = self.token

        client = await self._client_get()
        url = f"{self.host}{path}"

        try:
            resp = await client.get(url, params=params)
        except httpx.HTTPError as exc:
            raise TShockAPIError(f"无法连接 TShock 服务器: {exc}") from exc

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
                message += "，请检查 REST Token 是否正确"
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
        self.config = config
        self.host = str(config.get("host", "http://127.0.0.1:7878")).strip()
        self.token = str(config.get("token", "")).strip()
        self.timeout = int(config.get("timeout", 10) or 10)
        self.client = TShockClient(self.host, self.token, self.timeout)

        # 桥接相关状态
        self._bridge_task: asyncio.Task | None = None
        self._last_players: set[str] | None = None
        self._was_online: bool | None = None

    # ==================== 生命周期 ====================

    async def initialize(self) -> None:
        """实例化完成后由 AstrBot 自动调用（官方模板约定的异步初始化钩子）。

        参考：docs/dev/star/guides/simple —— "可选择实现异步的插件初始化方法"。
        """
        self.logger.info(
            "[tshock] 插件已加载 v%s，REST 地址: %s，桥接: %s",
            __version__,
            self.host,
            bool((self.config.get("bridge") or {}).get("enabled")),
        )
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

    # ==================== 指令组 ====================

    @filter.command_group("ts")
    def ts():
        """TShock 服务器管理指令组（指令组的函数体不需要实现任何逻辑）。

        注意：``filter.command_group`` 会把该函数替换为一个 ``RegisteringCommandable``
        对象，因此按官方文档写法不声明 ``self``。
        """

    @ts.command("help")
    async def ts_help(self, event: AstrMessageEvent):
        """显示帮助信息"""
        help_text = (
            "🌀 TShock 服务器管理指令：\n"
            "/ts status           查询服务器状态\n"
            "/ts players          查询在线玩家\n"
            "/ts help             显示本帮助\n"
            "—— 以下需管理员权限 ——\n"
            "/ts here             将当前会话设为桥接目标\n"
            "/ts users            查询用户账号列表\n"
            "/ts broadcast <消息>  向服务器广播消息\n"
            "/ts say <玩家> <消息> 向指定玩家私聊\n"
            "/ts cmd <指令>        执行服务器原始命令"
        )
        yield event.plain_result(help_text)

    @ts.command("status")
    async def ts_status(self, event: AstrMessageEvent):
        """查询服务器状态"""
        try:
            data = await self.client.status(players=False)
            yield event.plain_result(self._format_status(data))
        except TShockAPIError as exc:
            yield event.plain_result(f"❌ {exc}")

    @ts.command("players")
    async def ts_players(self, event: AstrMessageEvent):
        """查询在线玩家列表"""
        try:
            data = await self.client.status(players=True)
            players = data.get("players") if isinstance(data, dict) else None
            names = self._normalize_players(players or [])
            total = len(names)
            maxp = data.get("maxplayers", "?") if isinstance(data, dict) else "?"
            if not names:
                yield event.plain_result("🏝️ 当前没有玩家在线。")
                return
            yield event.plain_result(
                f"👥 在线玩家（{total}/{maxp}）：\n"
                + "\n".join(f"· {n}" for n in names)
            )
        except TShockAPIError as exc:
            yield event.plain_result(f"❌ {exc}")

    @ts.command("here")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def ts_here(self, event: AstrMessageEvent):
        """将当前会话设为桥接目标会话（管理员）"""
        deny = self._deny_if_not_admin(event)
        if deny:
            yield event.plain_result(deny)
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

        yield event.plain_result(
            f"✅ 已将当前会话设为桥接目标：\n{umo}\n"
            "服务器/玩家上下线通知将推送到此会话。"
        )

    @ts.command("users")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def ts_users(self, event: AstrMessageEvent):
        """查询用户账号列表（管理员）"""
        deny = self._deny_if_not_admin(event)
        if deny:
            yield event.plain_result(deny)
            return
        try:
            data = await self.client.users_list()
            users = data.get("users") if isinstance(data, dict) else None
            if not users:
                yield event.plain_result("没有查询到用户账号。")
                return
            lines = []
            for u in users:
                if isinstance(u, dict):
                    lines.append(
                        f"· {u.get('name', '?')} (#{u.get('id', '?')}) [{u.get('group', '?')}]"
                    )
                else:
                    lines.append(f"· {u}")
            yield event.plain_result("🗂️ 用户账号列表：\n" + "\n".join(lines))
        except TShockAPIError as exc:
            yield event.plain_result(f"❌ {exc}")

    @ts.command("broadcast")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def ts_broadcast(self, event: AstrMessageEvent):
        """向服务器广播消息（管理员）"""
        deny = self._deny_if_not_admin(event)
        if deny:
            yield event.plain_result(deny)
            return
        msg = self._text_after(event, "broadcast")
        if not msg:
            yield event.plain_result("用法：/ts broadcast <消息>")
            return
        msg = self._truncate(msg)
        try:
            await self.client.broadcast(msg)
            yield event.plain_result(f"📢 已广播：{msg}")
        except TShockAPIError as exc:
            yield event.plain_result(f"❌ {exc}")

    @ts.command("say")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def ts_say(self, event: AstrMessageEvent):
        """向指定玩家私聊（管理员）"""
        deny = self._deny_if_not_admin(event)
        if deny:
            yield event.plain_result(deny)
            return
        text = self._text_after(event, "say")
        parts = text.split(None, 1)
        if len(parts) < 2:
            yield event.plain_result("用法：/ts say <玩家名> <消息>")
            return
        player, msg = parts[0], self._truncate(parts[1])
        try:
            # 注意：不要给参数加引号。TShock 的命令解析器按空格切分参数且不剥离引号，
            # 加了引号会把玩家名变成 '"星"' 从而匹配不到玩家。
            await self.client.rawcmd(f"whisper {player} {msg}")
            yield event.plain_result(f"💬 已私聊 {player}：{msg}")
        except TShockAPIError as exc:
            yield event.plain_result(f"❌ {exc}")

    @ts.command("cmd")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def ts_cmd(self, event: AstrMessageEvent):
        """执行服务器原始命令（管理员）"""
        deny = self._deny_if_not_admin(event)
        if deny:
            yield event.plain_result(deny)
            return
        cmd = self._text_after(event, "cmd")
        if not cmd:
            yield event.plain_result("用法：/ts cmd <服务器指令>")
            return
        try:
            result = await self.client.rawcmd(cmd)
            yield event.plain_result(
                "🖥️ 命令执行结果：\n" + self._format_command_result(result)
            )
        except TShockAPIError as exc:
            yield event.plain_result(f"❌ {exc}")

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
            self.logger.debug(
                "[tshock] 非管理员消息，跳过转发：%s", event.get_sender_name()
            )
            return

        sender = event.get_sender_name() or "机器人"
        text = self._truncate(text)
        try:
            await self.client.broadcast(f"[{sender}] {text}")
        except TShockAPIError as exc:
            self.logger.warning("[tshock] 转发消息到服务器失败: %s", exc)

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
                self.logger.error("[tshock] 桥接轮询异常: %s", exc)

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

        使用官方文档推荐的 ``MessageChain().message()`` 构建链式消息。
        参考：docs/dev/star/guides/send-message —— 主动消息。
        """
        try:
            chain = MessageChain().message(text)
            await self.context.send_message(target, chain)
        except Exception as exc:  # noqa: BLE001
            self.logger.error("[tshock] 推送桥接消息失败: %s", exc)

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
        """格式化服务器状态信息。

        注意：status 返回的 ``port`` 是**游戏端口**，与 REST 端口不同，
        不能与 ``self.host`` 直接拼接。
        """
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
            f"运行时长：{uptime}\n"
            f"REST：{self.host}"
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
    def _text_after(event: AstrMessageEvent, subcommand: str) -> str:
        """从原始消息中提取指定子命令之后的文本（自动兼容前缀与参数空格）。"""
        parts = (event.message_str or "").strip().split()
        for i, token in enumerate(parts):
            if token == subcommand:
                return " ".join(parts[i + 1 :])
        return ""
