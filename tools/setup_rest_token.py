#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
TShock REST Token 生成助手
==========================

通过 TShock 的 ``/v2/token/create`` 端点获取 REST Token，并立即验证其可用性。

用法::

    python tools/setup_rest_token.py
    python tools/setup_rest_token.py --host http://your-tshock-host:7878
    python tools/setup_rest_token.py --host http://127.0.0.1:7878 --username admin

设计要点：

* 仅使用标准库，无需安装任何依赖。
* 密码通过 ``getpass`` 读取，**不回显、不落盘**。
* 自动兼容服务端开启 ``EnableTokenEndpointAuthentication`` 的情况（改用 HTTP Basic）。

对应关系::

    GET /v2/token/create?username=<u>&password=<p>
        200 -> {"status":"200","token":"<token>","user":"<账号>"}
        401 -> 服务端开启了 EnableTokenEndpointAuthentication，需改用 Basic Auth
        403 -> 账号密码错误，或该账号所属用户组缺少 REST 权限
        404 -> RestApiEnabled 仍为 false（需重启服务端），或端口填写错误
"""

import argparse
import base64
import getpass
import json
import sys
import urllib.error
import urllib.parse
import urllib.request

DEFAULT_HOST = "http://127.0.0.1:7878"
TIMEOUT = 12


def normalize_host(host: str) -> str:
    """允许用户只填写 ``域名:端口``，自动补全协议头。"""
    host = (host or "").strip().rstrip("/")
    if host and not host.startswith(("http://", "https://")):
        host = "http://" + host
    return host


def request(host, path, params=None, basic_auth=None, timeout=TIMEOUT):
    """发一个 GET 请求，返回 (状态码, 响应文本)；连接层失败时状态码为 None。"""
    url = host + path
    if params:
        url += "?" + urllib.parse.urlencode(params)

    req = urllib.request.Request(url)
    if basic_auth:
        raw = ("%s:%s" % basic_auth).encode("utf-8")
        req.add_header(
            "Authorization", "Basic " + base64.b64encode(raw).decode("ascii")
        )

    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace")
    except Exception as exc:  # noqa: BLE001
        return None, str(exc)


def parse_json(body):
    try:
        return json.loads(body)
    except Exception:  # noqa: BLE001
        return None


def precheck(host):
    """访问公开端点 /v2/server/status，确认 REST 可达。"""
    code, body = request(host, "/v2/server/status")
    if code is None:
        print(f"[x] 无法连接：{body}")
        print("    请确认地址与端口正确、网络可达，且 TShock 的 RestApiEnabled 已开启。")
        return False

    info = parse_json(body) or {}
    if code == 200 and info.get("tshockversion"):
        print(
            "[+] REST 可达：TShock {ts} / Terraria {ter}，世界 {world}，在线 {pc}/{mp}".format(
                ts=info.get("tshockversion"),
                ter=info.get("serverversion"),
                world=info.get("world"),
                pc=info.get("playercount"),
                mp=info.get("maxplayers"),
            )
        )
        return True

    print(f"[!] 预检返回 HTTP {code}：{body[:200]}")
    return True


def verify(host, token):
    """用需要鉴权的 /v2/users/list 验证 token 是否真正可用。"""
    print("\n[*] 验证 token（请求需鉴权的 /v2/users/list）...")
    code, body = request(host, "/v2/users/list", {"token": token})
    if code == 200:
        users = (parse_json(body) or {}).get("users") or []
        print(f"[+] token 可用，读取到 {len(users)} 个账号。")
        for u in users[:10]:
            if isinstance(u, dict):
                print(f"      - {u.get('name')} [#{u.get('id')}] 组={u.get('group')}")
        return True
    print(f"[!] token 验证未通过 (HTTP {code})：{body[:200]}")
    print("    请确认该账号所属用户组具备 REST 权限。")
    return False


def main():
    ap = argparse.ArgumentParser(description="生成 TShock REST Token")
    ap.add_argument("--host", default=None, help="TShock REST 基础地址，如 http://1.2.3.4:7878")
    ap.add_argument("--username", default=None, help="管理员用户名（留空则交互输入）")
    ap.add_argument("--no-verify", action="store_true", help="跳过生成后的可用性验证")
    args = ap.parse_args()

    if args.host:
        host = normalize_host(args.host)
    else:
        entered = input(f"TShock REST 地址 [{DEFAULT_HOST}]: ").strip()
        host = normalize_host(entered or DEFAULT_HOST)
    print(f"[*] 目标：{host}")

    if not precheck(host):
        return 1

    username = args.username or input("管理员用户名: ").strip()
    if not username:
        print("[x] 用户名不能为空。")
        return 1

    password = getpass.getpass("管理员密码（输入不回显）: ")
    if not password:
        print("[x] 密码不能为空。")
        return 1

    print("[*] 正在请求 /v2/token/create ...")
    code, body = request(
        host, "/v2/token/create", {"username": username, "password": password}
    )
    data = parse_json(body) or {}

    if code == 401:
        print("[!] 服务端开启了 EnableTokenEndpointAuthentication，改用 HTTP Basic 认证重试 ...")
        code, body = request(
            host, "/v2/token/create", None, basic_auth=(username, password)
        )
        data = parse_json(body) or {}

    if code == 200 and data.get("token"):
        token = data["token"]
        print("\n[+] 生成成功")
        print(f"    token    = {token}")
        if data.get("user"):
            print(f"    绑定账号 = {data.get('user')}")
        print("\n下一步：把该 token 填入 AstrBot 插件配置的 token 字段。")
        print("也可以复制 astrbot_plugin_tshock_config.example.json 作为配置起点。")
        if not args.no_verify:
            verify(host, token)
        return 0

    print(f"\n[x] 生成失败 (HTTP {code})")
    print(f"    {data.get('error') or body[:300]}")
    if code == 403:
        print("    可能原因：账号密码错误；或该账号所属用户组缺少 REST 授权权限。")
    elif code == 404:
        print("    可能原因：RestApiEnabled 仍为 false（修改后需重启服务端），或端口填写错误。")
    return 1


if __name__ == "__main__":
    sys.exit(main())
