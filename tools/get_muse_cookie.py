#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""muse2api Cookie 助手 —— 把 muse.ai 账号导入账号池，可导入多个。

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  最简单的用法（只敲这一条，地址和 Key 打错都没关系）：

      python get_muse_cookie.py

  它会干什么？
      1. 问你服务器地址和 管理员 Key（直接回车就用上次记住的）
      2. 弹出一个独立浏览器窗口，你登录 muse.ai
      3. 自动抓 cookie、自动上传，看到「✓ 导入成功」就好了
      4. 问你「还要再导入一个吗？」—— 想加就按 y，会再弹一个干净窗口，
         登录另一个 muse.ai 账号即可。一直加到你按 n 为止。

  首次运行会记住地址和 Key（存在 ~/.muse2api-import.json），
  第二次起直接回车两次就完事。

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  进阶用法（给脚本/批量用，全自动不交互）：

      python get_muse_cookie.py --base http://1.2.3.4:18610 --key m2a_xxx
      python get_muse_cookie.py --base ... --key ... --count 3   # 连续导 3 个
      python get_muse_cookie.py --list                           # 看账号池现状
      python get_muse_cookie.py --remove <账号ID或标签>            # 删一个账号
      python get_muse_cookie.py --from-clipboard                  # 剪贴板兜底

  也可以用环境变量，省得每次敲：
      set MUSE2API_BASE=http://1.2.3.4:18610     (Windows)
      set MUSE2API_KEY=m2a_xxx
      export MUSE2API_BASE=...                   (macOS / Linux)

只依赖 Python 标准库（Python 3.8+），不需要 pip install 任何东西。
"""

from __future__ import annotations

import argparse
import base64
import glob
import json
import os
import re
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import urllib.parse
import getpass

SITE = "https://muse.ai/"
LOGIN_URL = "https://muse.ai/login"
CDP_PORT = 9333
ESSENTIAL = ("hatch_sess", "hatch_gw", "hatch_vml", "hatch_native_auth_device")
DOMAIN_HINT = "muse.ai"

# 记住上次填过的地址 / Key，第二次起只要回车
CONF_PATH = os.path.join(os.path.expanduser("~"), ".muse2api-import.json")


# ---------------------------------------------------------------- 输出
#   ⚠️ 中文 Windows 的 cmd 默认是 GBK(936)，而 ✓ ✗ ⚠ 这类符号**不在 GBK 里**。
#   直接 print 会抛 UnicodeEncodeError（或被替换成 ?），小白看到一堆问号。
#   策略：启动时探一次当前输出编码，能编码就用好看的符号，不能就降级成 ASCII。
_OK_MARK = "✓"
_BAD_MARK = "✗"
_WARN_MARK = "⚠"


def _probe_marks():
    """看当前 stdout 编码支不支持那几个漂亮符号，不支持就换 ASCII。

    ⚠️ 要测的是「reconfigure 之后实际生效的编码」，不是原始编码。
    另外还要考虑一种更隐蔽的情况：encoding 属性说 utf-8，但底层是 GBK 控制台
    （某些 Windows 终端的包装层就是这么撒谎的）。所以除了 encode()，还要看
    输出是否被替换成 '?' —— 那是终端层替换的迹象。
    """
    global _OK_MARK, _BAD_MARK, _WARN_MARK
    enc = (getattr(sys.stdout, "encoding", "") or "utf-8")
    pairs = (("\u2713", "[OK]", "_OK_MARK"),
             ("\u2717", "[X]", "_BAD_MARK"),
             ("\u26a0", "[!]", "_WARN_MARK"))
    for ch, fallback, name in pairs:
        keep = True
        try:
            ch.encode(enc)
        except (UnicodeEncodeError, LookupError):
            keep = False
        if not keep:
            if name == "_OK_MARK":
                _OK_MARK = fallback
            elif name == "_BAD_MARK":
                _BAD_MARK = fallback
            else:
                _WARN_MARK = fallback


def say(msg=""):
    try:
        print(msg, flush=True)
    except UnicodeEncodeError:                     # 老 Windows 控制台
        enc = (getattr(sys.stdout, "encoding", "") or "utf-8")
        # 兜底：把所有「不在目标编码里的」字符换成 '?'
        try:
            safe = msg.encode(enc, "replace").decode(enc, "replace")
            print(safe, flush=True)
        except Exception:                          # noqa: BLE001
            print(msg.encode("ascii", "replace").decode("ascii"), flush=True)


def init_console():
    #   reconfigure 到 utf-8 是首选，但**不是万能的**：
    #   有些环境（IDE 内置终端、被重定向到文件、PYTHONIOENCODING 已被设死）
    #   reconfigure 会失败或无效，所以后面 say() 里还有一层兜底。
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")   # Python 3.7+
        except Exception:                          # noqa: BLE001
            pass
    _probe_marks()


def hr(ch="─", n=66):
    say(ch * n)


def banner(title):
    say()
    hr("━")
    say(f"  {title}")
    hr("━")


# ---------------------------------------------------------------- 记住上次
def load_conf() -> dict:
    try:
        with open(CONF_PATH, "r", encoding="utf-8") as fh:
            d = json.load(fh)
            return d if isinstance(d, dict) else {}
    except Exception:                              # noqa: BLE001
        return {}


def save_conf(**kv):
    d = load_conf()
    d.pop("key", None)
    d.update({k: v for k, v in kv.items() if v and k != "key"})
    try:
        with open(CONF_PATH, "w", encoding="utf-8") as fh:
            json.dump(d, fh, ensure_ascii=False, indent=2)
        try:
            os.chmod(CONF_PATH, 0o600)
        except Exception:                          # noqa: BLE001
            pass
    except Exception:                              # noqa: BLE001
        pass


# ---------------------------------------------------------------- 极简 WebSocket
class WS:
    """只为 CDP 用的最小 WebSocket 客户端（RFC6455，文本帧）。"""

    def __init__(self, url: str, timeout: float = 15.0):
        assert url.startswith("ws://"), url
        rest = url[5:]
        hostport, _, path = rest.partition("/")
        path = "/" + path
        host, _, port = hostport.partition(":")
        port = int(port or 80)

        self.sock = socket.create_connection((host, port), timeout=timeout)

        key = base64.b64encode(os.urandom(16)).decode()
        req = (f"GET {path} HTTP/1.1\r\n"
               f"Host: {host}:{port}\r\n"
               "Upgrade: websocket\r\n"
               "Connection: Upgrade\r\n"
               f"Sec-WebSocket-Key: {key}\r\n"
               "Sec-WebSocket-Version: 13\r\n\r\n")
        self.sock.sendall(req.encode())

        buf = b""
        while b"\r\n\r\n" not in buf:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise ConnectionError("WebSocket 握手失败")
            buf += chunk
        if b" 101 " not in buf.split(b"\r\n")[0]:
            raise ConnectionError("WebSocket 握手被拒绝："
                                  + buf.split(b"\r\n")[0].decode("utf-8", "replace"))
        self._id = 0

    # --- 发送 ---
    def _frame(self, opcode: int, payload: bytes):
        head = bytearray([0x80 | opcode])
        n = len(payload)
        if n < 126:
            head.append(0x80 | n)
        elif n < 65536:
            head.append(0x80 | 126)
            head += struct.pack(">H", n)
        else:
            head.append(0x80 | 127)
            head += struct.pack(">Q", n)
        mask = os.urandom(4)
        head += mask
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        self.sock.sendall(bytes(head) + masked)

    def send_text(self, text: str):
        self._frame(0x1, text.encode("utf-8"))

    # --- 接收 ---
    def _read(self, n: int) -> bytes:
        out = b""
        while len(out) < n:
            chunk = self.sock.recv(n - len(out))
            if not chunk:
                raise ConnectionError("连接已关闭")
            out += chunk
        return out

    def recv_text(self) -> str:
        while True:
            h = self._read(2)
            opcode = h[0] & 0x0F
            masked = h[1] & 0x80
            ln = h[1] & 0x7F
            if ln == 126:
                ln = struct.unpack(">H", self._read(2))[0]
            elif ln == 127:
                ln = struct.unpack(">Q", self._read(8))[0]
            mk = self._read(4) if masked else None
            data = self._read(ln) if ln else b""
            if mk:
                data = bytes(b ^ mk[i % 4] for i, b in enumerate(data))
            if opcode == 0x1:
                return data.decode("utf-8", "replace")
            if opcode == 0x9:                       # ping -> pong
                self._frame(0xA, data)
            elif opcode == 0x8:
                raise ConnectionError("服务端关闭了连接")
            # 其它（pong / 二进制 / 分片）直接忽略

    def call(self, method: str, params: dict | None = None,
             timeout: float = 20.0) -> dict:
        self._id += 1
        mid = self._id
        self.send_text(json.dumps({"id": mid, "method": method,
                                   "params": params or {}}))
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                msg = json.loads(self.recv_text())
            except socket.timeout:
                continue
            except ConnectionError:
                raise
            if msg.get("id") == mid:
                if "error" in msg:
                    raise RuntimeError(f"{method}: {msg['error']}")
                return msg.get("result", {})
        raise TimeoutError(f"CDP {method} 超时")

    def close(self):
        try:
            self.sock.close()
        except Exception:                              # noqa: BLE001
            pass


# ---------------------------------------------------------------- 找浏览器
#   浏览器要求：Chromium 内核 + 支持 --remote-debugging-port。
#   Chrome / Edge / Brave / Chromium / Vivaldi / Opera 都行。
#   国产浏览器（360 / QQ / 搜狗）多数阉割了调试接口，故意不收录。
BROWSER_SPECS = [
    # (注册表 App Paths 里的 exe 名, 常见安装相对路径)
    # Chrome 排最前：调试协议支持最稳定
    ("chrome.exe", r"Google\Chrome\Application\chrome.exe",
     r"Google\Chrome Beta\Application\chrome.exe"),
    ("msedge.exe", r"Microsoft\Edge\Application\msedge.exe"),
    ("brave.exe", r"BraveSoftware\Brave-Browser\Application\brave.exe"),
    ("vivaldi.exe", r"Vivaldi\Application\vivaldi.exe"),
    ("chromium.exe", r"Chromium\Application\chrome.exe"),
    ("launcher.exe", r"Opera\launcher.exe"),
]
CHROME_EXES = tuple(s[0] for s in BROWSER_SPECS)


def _win_registry_paths() -> list[str]:
    """从注册表 App Paths 找浏览器。

    不能靠 %ProgramFiles% 环境变量 —— 它在某些 shell / 精简环境下并不存在
    （本机实测 ProgramFiles 和 PROGRAMFILES 都是 None）。
    """
    out: list[str] = []
    try:
        import winreg
    except ImportError:
        return out
    sub = (r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths")
    for root in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
        for exe in CHROME_EXES:
            for view in (winreg.KEY_WOW64_64KEY, winreg.KEY_WOW64_32KEY):
                try:
                    with winreg.OpenKey(root, sub + "\\" + exe, 0,
                                        winreg.KEY_READ | view) as k:
                        val = winreg.QueryValueEx(k, "")[0]
                        if val:
                            out.append(os.path.expandvars(val))
                except OSError:
                    continue
    return out


def _win_common_paths() -> list[str]:
    """常见安装位置。

    ⚠️ 这里只放「浏览器主程序确实叫这个 exe 名」的路径。
    踩过的坑：Edge 没有 64 位版，只装在 Program Files (x86)；
    早期代码把 msedge.exe 也拼到 Program Files 下，产生一堆不存在的候选路径
    （不致命，但让「没找到浏览器」的提示里夹杂噪音）。
    """
    roots = [os.environ.get("ProgramFiles"), os.environ.get("ProgramFiles(x86)"),
             os.environ.get("LOCALAPPDATA"), r"C:\Program Files",
             r"C:\Program Files (x86)"]
    out: list[str] = []
    # Chrome：64 位在 Program Files，32 位在 (x86)
    for r in roots:
        if r:
            out.append(os.path.join(r, r"Google\Chrome\Application\chrome.exe"))
    # Edge：只有 32 位安装包，主目录固定在 Program Files (x86)
    for r in ([os.environ.get("ProgramFiles(x86)"), r"C:\Program Files (x86)",
               os.environ.get("LOCALAPPDATA"), r"C:\Program Files"]):
        if r:
            out.append(os.path.join(r, r"Microsoft\Edge\Application\msedge.exe"))
    # 其余 Chromium 系
    for spec_exe, *rels in BROWSER_SPECS:
        if spec_exe in ("chrome.exe", "msedge.exe"):
            continue                                # 上面已单独处理
        for r in roots:
            if r:
                for rel in rels:
                    out.append(os.path.join(r, rel))
    return out


def find_browser(explicit: str = "") -> str | None:
    if explicit:
        return explicit if os.path.isfile(explicit) else None
    if sys.platform == "win32":
        cands = _win_registry_paths() + _win_common_paths()
    elif sys.platform == "darwin":
        cands = [
            "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
            "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
            "/Applications/Chromium.app/Contents/MacOS/Chromium",
            "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser",
            "/Applications/Vivaldi.app/Contents/MacOS/Vivaldi",
        ]
    else:
        cands = ["google-chrome", "google-chrome-stable", "chromium",
                 "chromium-browser", "microsoft-edge", "microsoft-edge-stable",
                 "brave-browser", "vivaldi"]
    seen = set()
    for c in cands:
        if not c or c in seen:
            continue
        seen.add(c)
        if os.path.isfile(c):
            return c
        low = os.path.basename(c).lower()
        # 排除掉我们顺手扫到的、其实不是浏览器的同名文件
        if not any(low.startswith(x.split(".")[0]) for x in CHROME_EXES):
            continue
        if not os.path.isabs(c):
            w = shutil.which(c)
            if w:
                return w
    return None


def wait_cdp(port: int, timeout: float = 30.0) -> str:
    """等 DevTools 端口起来，返回浏览器级 WebSocket 地址。"""
    deadline = time.time() + timeout
    url = f"http://127.0.0.1:{port}/json/version"
    last = None
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=2) as r:
                return json.load(r)["webSocketDebuggerUrl"]
        except Exception as exc:                       # noqa: BLE001
            last = exc
            time.sleep(0.5)
    raise RuntimeError(f"浏览器调试端口没起来（{port}）：{last}")


def kill_process_tree(proc) -> None:
    """把浏览器及其全部子进程杀掉。

    ⚠️ 为什么不能只 proc.terminate()：
        Chrome / Edge 主进程会拉一堆 helper 子进程。杀掉主进程后，
        子进程仍然活着并占着 profile 目录与调试端口 —— 用户下次重跑
        （或想再导一个账号）就会看到「浏览器调试端口没起来」，
        完全不知道该怎么办。必须整棵树清理。
    """
    if proc is None:
        return
    try:
        if sys.platform == "win32":
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                           stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL, timeout=20)
        else:
            try:
                os.killpg(os.getpgid(proc.pid), 15)     # SIGTERM 整组
            except Exception:                           # noqa: BLE001
                proc.terminate()
            time.sleep(0.5)
            try:
                os.killpg(os.getpgid(proc.pid), 9)      # 还不走就 SIGKILL
            except Exception:                           # noqa: BLE001
                proc.kill()
    except Exception:                                   # noqa: BLE001
        try:
            proc.terminate()
        except Exception:                               # noqa: BLE001
            pass
    try:
        proc.wait(timeout=10)
    except Exception:                                   # noqa: BLE001
        pass


def port_in_use(port: int) -> bool:
    """本地这个端口有没有人在监听（用来区分「端口被占」和「浏览器没起来」）。"""
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=1):
            return True
    except OSError:
        return False


def browser_running(browser: str = "") -> bool:
    """用户自己的浏览器是不是正开着。

    Chrome 系浏览器（含 Edge/Brave）有个单例机制：如果同一个「用户配置」
    已经有实例在跑，新起的进程会把参数转交给它然后自己退出 —— 于是
    --remote-debugging-port 就永远起不来。这是小白最常撞的墙，必须提前识别。
    """
    if sys.platform == "win32":
        exe = os.path.basename(browser).lower() if browser else ""
        names = [exe] if exe else ["chrome.exe", "msedge.exe", "brave.exe"]
        try:
            out = subprocess.run(
                ["tasklist", "/FI", "STATUS eq RUNNING", "/FO", "CSV", "/NH"],
                capture_output=True, timeout=10)
            listing = out.stdout.decode("utf-8", "replace").lower()
        except Exception:                              # noqa: BLE001
            return False
        return any(n and n in listing for n in names)
    try:
        out = subprocess.run(["ps", "-A"], capture_output=True, timeout=10)
        listing = out.stdout.decode("utf-8", "replace").lower()
    except Exception:                                  # noqa: BLE001
        return False
    key = os.path.basename(browser).lower() if browser else ""
    if key:
        return key in listing
    return any(k in listing for k in
               ("chrome", "chromium", "msedge", "brave", "vivaldi"))


# ---------------------------------------------------------------- 读 cookie
def read_cookies(ws: WS) -> dict[str, dict]:
    """用浏览器级 Storage.getCookies 拿全部 cookie，再筛 muse.ai。

    这是关键：CDP 能读到 httpOnly 的 cookie，网页 JS 读不到。
    """
    res = ws.call("Storage.getCookies", {}, timeout=20)
    out: dict[str, dict] = {}
    for c in res.get("cookies", []):
        domain = (c.get("domain") or "").lstrip(".")
        if domain != DOMAIN_HINT and not domain.endswith("." + DOMAIN_HINT):
            continue
        name = c.get("name")
        if not name:
            continue
        try:
            exp = int(float(c.get("expires", -1)))
        except (TypeError, ValueError):
            exp = -1
        out[name] = {"value": c.get("value", ""), "expires": exp,
                     "httpOnly": bool(c.get("httpOnly"))}
    return out


def read_login_state(ws: WS) -> tuple[bool, str]:
    """看当前页面是不是已经登录了（顺便拿邮箱，给账号起个自动标签）。"""
    try:
        r = ws.call("Runtime.evaluate", {
            "expression": (
                "(function(){try{"
                "var m=document.querySelector('meta[name=user-email]');"
                "if(m&&m.content)return m.content;"
                "var t=document.body?document.body.innerText:'';"
                "var x=t.match(/[\\\\w.+-]+@[\\\\w-]+\\\\.[\\\\w.]+/);"
                "return x?x[0]:'';}catch(e){return '';}})()"
            ),
            "returnByValue": True,
        }, timeout=10)
        val = (r.get("result") or {}).get("value") or ""
        val = str(val).strip()
        if re.fullmatch(r"[\w.+-]+@[\w-]+\.[\w.]+", val):
            return True, val
        return bool(val), val
    except Exception:                              # noqa: BLE001
        return False, ""


# ---------------------------------------------------------------- 账号池 API
class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise RuntimeError("服务器重定向被拒绝：请直接填写最终 HTTPS 地址")


def validate_transport(base):
    u = urllib.parse.urlsplit(base)
    if (u.scheme not in ("http", "https") or not u.hostname or u.username is not None
            or u.password is not None or u.query or u.fragment or u.path not in ("", "/")):
        raise RuntimeError("服务器地址无效")
    if u.scheme == "http" and u.hostname not in ("127.0.0.1", "::1"):
        raise RuntimeError("Cookie 只能通过 HTTPS 或本机 SSH 隧道上传（127.0.0.1 / ::1）")



def _api(base: str, key: str, path: str, method: str = "GET",
         payload: dict | None = None, timeout: int = 30) -> dict:
    validate_transport(base)
    url = base.rstrip("/") + path
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    headers = {"Authorization": f"Bearer {key}"}
    if data is not None:
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect()).open(req, timeout=timeout) as r:
            body = r.read().decode("utf-8", "replace")
            return json.loads(body) if body.strip() else {}
    except urllib.error.HTTPError as e:
        detail = "服务器拒绝请求"
        hint = ""
        if e.code in (401, 403):
            hint = ("\n  → Key 不对或已失效。跑 `--status` 能看回正确的 Key，"
                    "或去管理页面复制。")
        elif e.code == 404:
            hint = "\n  → 地址不对，或这个服务不是 muse2api。检查端口是否正确。"
        raise RuntimeError(f"HTTP {e.code}（{path}）：{detail}{hint}") from None
    except urllib.error.URLError as e:
        reason = str(getattr(e, "reason", e))
        raise RuntimeError(
            f"连不上 {base}：{reason}\n"
            f"  → 检查：① 地址和端口写对了吗？② 服务器防火墙/安全组放行了这个端口吗？"
            f"③ 服务在跑吗（sudo bash install.sh --credentials）？") from None


def upload(base: str, key: str, label: str, cookies: dict[str, dict]) -> dict:
    return _api(base, key, "/admin/accounts", "POST", {
        "label": label,
        "cookies": {k: v["value"] for k, v in cookies.items()},
        "expires": {k: v["expires"] for k, v in cookies.items() if v["expires"] > 0},
    })


def list_accounts(base: str, key: str) -> list[dict]:
    """拉账号池。兼容 /admin/accounts 与 /admin/accounts/list 两种路径。"""
    last_err = None
    for path in ("/admin/accounts", "/admin/accounts/list"):
        try:
            r = _api(base, key, path)
        except RuntimeError as exc:
            last_err = exc
            continue
        if isinstance(r, list):
            return r
        for k in ("accounts", "items", "data", "list"):
            v = r.get(k)
            if isinstance(v, list):
                return v
    if last_err:
        raise last_err
    return []


def delete_account(base: str, key: str, ident: str) -> bool:
    """按 ID 或标签删账号；返回是否删掉了。"""
    ok = False
    for path in (f"/admin/accounts/{ident}", f"/admin/accounts?id={ident}",
                 f"/admin/accounts?label={ident}"):
        for method in ("DELETE", "POST"):
            try:
                _api(base, key, path, method)
                return True
            except RuntimeError:
                continue
    return ok


def probe(base: str, key: str) -> tuple[bool, str]:
    """连通性自检：返回 (是否可用, 说明)。"""
    for path, desc in (("/admin/accounts", "账号池接口"),):
        try:
            _api(base, key, path, timeout=10)
            return True, f"{desc}正常"
        except RuntimeError as exc:
            msg = str(exc)
            if "HTTP 401" in msg or "HTTP 403" in msg:
                return False, "管理员 Key 不对（被服务器拒绝了）"
            if "HTTP 404" in msg:
                return False, "地址不对（这个端口上没有 muse2api）"
            if "连不上" in msg:
                return False, "连不上服务器（地址/端口/防火墙）"
            return False, msg.splitlines()[0][:80]
    return False, "未知错误"


def fmt_ts(ts: int) -> str:
    if ts <= 0:
        return "会话级"
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(ts))


def fmt_left(ts: int) -> str:
    if ts <= 0:
        return ""
    left = ts - time.time()
    if left <= 0:
        return "（已过期）"
    h = int(left // 3600)
    if h >= 24:
        return f"（剩 {h // 24} 天）"
    if h >= 1:
        return f"（剩 {h} 小时）"
    return f"（剩 {int(left // 60)} 分钟）"


# ---------------------------------------------------------------- 剪贴板
def read_clipboard() -> str:
    """从剪贴板读 cookie 字符串。支持 Windows / macOS / Linux。"""
    cmds = []
    if sys.platform == "win32":
        cmds = [["powershell", "-NoProfile", "-Command", "Get-Clipboard"]]
    elif sys.platform == "darwin":
        cmds = [["pbpaste"]]
    else:
        cmds = [["xclip", "-selection", "clipboard", "-o"],
                ["xsel", "--clipboard", "--output"],
                ["wl-paste"]]
    for c in cmds:
        try:
            out = subprocess.run(c, capture_output=True, timeout=10)
            if out.returncode == 0 and out.stdout.strip():
                return out.stdout.decode("utf-8", "replace")
        except Exception:                          # noqa: BLE001
            continue
    return ""


def parse_cookie_text(text: str) -> dict[str, str]:
    """把用户从 DevTools 复制的 cookie 文本解析成 {名: 值}。

    能识别这些形态：
        hatch_sess=abc; hatch_gw=def
        hatch_sess    abc
        hatch_sess=abc
        Cookie: hatch_sess=abc; ...
    """
    out: dict[str, str] = {}
    if not text:
        return out
    text = text.strip()
    if text.lower().startswith("cookie:"):
        text = text.split(":", 1)[1]
    # 先按 ; 或换行切
    for chunk in re.split(r"[;\n\r]+", text):
        chunk = chunk.strip().strip(",")
        if not chunk:
            continue
        if "=" in chunk:
            k, _, v = chunk.partition("=")
        else:
            parts = re.split(r"\s{2,}|\t", chunk, maxsplit=1)
            if len(parts) != 2:
                continue
            k, v = parts
        k = k.strip()
        v = v.strip().strip('"').strip("'")
        if k in ESSENTIAL and v:
            out[k] = v
    return out


# ---------------------------------------------------------------- 交互小工具
def _prompt_line(tip: str) -> None:
    """把「问题」按当前环境用合适的方式呈现出来。

    ⚠️ 为什么要分两种情况（实测踩过）：
        input(prompt) 的提示默认写到 stdout 且**不带换行**，光标停在提示符后面等输入。
        交互终端里这很正常；但在**非交互**环境（管道、`< file`、IDE 内置终端）
        输入会立刻被消费/EOF，于是下一行输出直接接在提示符后面，挤成一坨：

            服务器地址（形如 …）：  这个 Key 我还记着：m2a_xxx
                                   ^^^^^^^^^^^^^^^^^^^^ 两段提示粘在一起，小白看懵

        所以：非交互时先把提示**独立打印一行**，再读输入；
              交互时保持原来的「提示后跟光标」不变。
    """
    if sys.stdin.isatty():
        print(tip, end="", flush=True)
    else:
        say(tip)


def ask(prompt: str, default: str = "") -> str:
    """问一个问题；直接回车就用默认值。"""
    if default:
        tip = f"{prompt} [{default}]："
    else:
        tip = f"{prompt}："
    _prompt_line(tip)
    try:
        ans = input().strip() if not sys.stdin.isatty() else input(tip).strip()
    except EOFError:
        return default
    return ans or default


def ask_secret(prompt: str) -> str:
    """问一个不想显示在屏幕上的值（管理员 Key）。

    ⚠️ 为什么不用 getpass.getpass()：
        它只在「真控制台」下工作。一旦 stdin 是管道 / 重定向 / IDE 内置终端 /
        CI，getpass 会挂住或直接抛异常 —— 小白把命令贴进各种终端时很容易撞上。
        而且 getpass 读不到已回显的输入，兼容性反而差。
        这里用普通 input()：输入会回显，但对小白更友好（能看见自己粘了什么），
        并且 --key 参数 / 环境变量 / 配置记忆三条路都不依赖它。
    """
    _prompt_line(prompt)
    try:
        return (input() if not sys.stdin.isatty() else getpass.getpass(prompt)).strip()
    except EOFError:
        return ""


def ask_yes(prompt: str, default_no: bool = True) -> bool:
    tip = "[y/N]" if default_no else "[Y/n]"
    try:
        ans = input(f"{prompt} {tip} ").strip().lower()
    except EOFError:
        return not default_no
    if not ans:
        return not default_no
    return ans in ("y", "yes", "是", "1", "true")


def normalize_base(s: str) -> str:
    """把用户填的地址整理成规范的 http(s)://主机[:端口] 形式。

    ⚠️ 必须做全的三件事（早期版本只做了最后一件，导致实测翻车）：
      1. 协议头统一成小写：`HTTP://1.2.3.4` 要变 `http://1.2.3.4`。
         大写协议在有些 HTTP 客户端/代理上会被拒，而且显示出来很怪。
      2. 去掉**所有**尾部斜杠与路径：`http://1.2.3.4:18610///` 要变
         `http://1.2.3.4:18610`。早期版本只 rstrip 一次，于是
         `...///` 变成一个带路径的地址，拿去做 `...///admin/accounts`
         请求会 404 或返回空 —— 但自检还显示「连接正常」（因为状态码不是
         401/403/404 里被识别的那几个），小白完全查不出来。
      3. 没有协议头时补 `http://`。
    """
    s = (s or "").strip()
    if not s:
        return s
    # 协议头统一小写
    m = re.match(r"^(https?)://", s, re.I)
    if m:
        scheme = m.group(1).lower()
        rest = s[m.end():]
    else:
        scheme = "http"
        rest = s
    # 只保留主机[:端口] 部分，路径/查询/锚点一律丢掉
    rest = rest.split("/", 1)[0].split("?", 1)[0].split("#", 1)[0]
    rest = rest.strip().rstrip("/")
    if not rest:
        return ""
    return f"{scheme}://{rest}"


def looks_like_address(s: str) -> bool:
    """粗略判断用户填的是不是「主机[:端口][/路径]」这类地址。

    ⚠️ 为什么需要这个：
        小白经常把地址填成「1.2.3.4 18610」（漏了冒号）或直接粘了一段
        中文/别的文字。早期版本遇到这种输入会**静默沿用上次记住的地址**，
        于是连通自检通过、小白以为自己的输入生效了，其实连的还是旧服务器 ——
        导号导到了错误的机器上。这是必须拦住的。
    """
    if not s:
        return False
    # 去掉可能的协议头
    body = re.sub(r"^https?://", "", s, flags=re.I)
    host = body.split("/", 1)[0]
    # 形如 主机:端口，主机可以是 IP 或域名
    m = re.match(r"^([A-Za-z0-9_.\-\[\]:]+)$", host)
    if not m:
        return False
    # 至少要有「字母数字」组成的非空主机部分
    hostpart = host.split(":", 1)[0]
    if not hostpart or not re.search(r"[A-Za-z0-9]", hostpart):
        return False
    # 端口部分如果存在，必须是数字
    if ":" in host:
        _, _, port = host.partition(":")
        port = port.split(":", 1)[0]
        if port and not port.isdigit():
            return False
    return True


# ---------------------------------------------------------------- 抓一次
def grab_one(browser: str, port: int, base: str, key: str,
             label: str, timeout: int, keep_open: bool,
             fresh: bool, user_data_dir: str = "") -> tuple[str, str]:
    """弹浏览器 → 等登录 → 上传。返回 (状态, 说明)。
    状态 ∈ ok / timeout / noupload / nocookies / failed
    """
    # ⚠️ 必须先用「不可能跑起来」的方式确认 browser 是真实可用的可执行文件。
    #    踩过的坑：主流程里 --no-browser-check 会让 browser=None，如果不在这里
    #    拦住，就会走到 subprocess.Popen(None, ...) 报 TypeError，
    #    或者在奇怪的情况下卡满整个 timeout。宁可这里就明确报错。
    if not browser or not os.path.isfile(browser):
        return "failed", (
            f"浏览器路径不可用：{browser or '(没检测到)'}\n"
            "  三个选择：\n"
            "    1) 装个 Google Chrome：https://www.google.cn/chrome/\n"
            '    2) 手动指定：python get_muse_cookie.py --chrome "D:\\某处\\chrome.exe"\n'
            "    3) 不想装浏览器：python get_muse_cookie.py --from-clipboard")

    #   --user-data-dir 的用途：让用户复用自己日常浏览器的登录态。
    #   有些站点的登录会二次校验「设备指纹」，用全新 profile 反而不好登；
    #   直接借用自己的配置就顺畅得多（缺点：那个浏览器得先完全关掉）。
    if user_data_dir:
        profile = os.path.abspath(os.path.expanduser(user_data_dir))
        try:
            os.makedirs(profile, exist_ok=True)
        except OSError as exc:
            return "failed", f"指定的浏览器配置目录用不了：{profile}（{exc}）"
    else:
        profile = os.path.join(tempfile.gettempdir(),
                               f"muse2api-cookie-profile-{os.getpid()}")
        shutil.rmtree(profile, ignore_errors=True)
        os.makedirs(profile, exist_ok=True)

    args_cmd = [
        browser,
        f"--remote-debugging-port={port}",
        "--remote-debugging-address=127.0.0.1",
        f"--user-data-dir={profile}",
        "--no-first-run", "--no-default-browser-check",
        "--disable-features=Translate,MediaRouter",
        "--new-window", SITE,
    ]
    say("  正在打开一个独立的浏览器窗口（不会动你日常用的浏览器）…")
    #  Windows 上把新进程放进独立进程组，方便最后一锅端掉它派生的所有子进程
    #  （Chrome 会拉一串 helper 进程；只 terminate 主进程的话，
    #   profile 会被子进程继续占着，下次启动就报「端口没起来」）。
    popen_kw = {}
    if sys.platform == "win32":
        popen_kw["creationflags"] = 0x00000200      # CREATE_NEW_PROCESS_GROUP
    else:
        popen_kw["start_new_session"] = True
    proc = subprocess.Popen(args_cmd, stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL, **popen_kw)

    ws = None
    got: dict[str, dict] = {}
    try:
        try:
            ws_url = wait_cdp(port, 40)
        except RuntimeError as exc:
            # 三种最常见的原因，各自给具体可操作的指引 ——
            # 小白看到「端口没起来」是一头雾水的，必须告诉他去做什么。
            hint = ""
            if port_in_use(port):
                hint = (f"  → 端口 {port} 上已经有东西在监听了。"
                        f"多半是上一次的浏览器没退干净：\n"
                        f"     ① 先试着把浏览器窗口全部关掉，然后重跑本命令；\n"
                        f"     ② 还是不行就换个端口：--port {port + 1}")
            elif browser_running(browser):
                hint = ("  → 你自己日常用的浏览器正在运行，Chrome 系浏览器会因此\n"
                        "     拒绝启动第二个带调试端口的实例。两个办法：\n"
                        "     ① 把你的浏览器**完全退出**（连托盘图标也退掉），再重跑；\n"
                        "     ② 或者指定另一个浏览器，比如 Edge：\n"
                        '        python get_muse_cookie.py --chrome "C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe"')
            else:
                hint = ("  → 浏览器没能起来。可以手动验证一下它到底报什么错，\n"
                        "     在命令行里敲（把路径换成你的）：\n"
                        '     "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe"'
                        f" --remote-debugging-port={port}\n"
                        "     如果它弹窗口报错，照提示处理即可。")
            return "failed", f"{exc}\n{hint}"
        ws = WS(ws_url)

        say()
        say("  ┌" + "─" * 62 + "┐")
        say("  │  请在刚弹出的窗口里登录 muse.ai                        │")
        say("  │  登录成功、能看到聊天界面后，脚本会自动抓取，无需操作。  │")
        say("  └" + "─" * 62 + "┘")

        deadline = time.time() + timeout
        last_note = 0.0
        last_email = ""
        while time.time() < deadline:
            try:
                got = read_cookies(ws)
            except Exception as exc:                    # noqa: BLE001
                say(f"  读取 cookie 出错，重试中…（{exc}）")
                time.sleep(2)
                continue

            have = [n for n in ESSENTIAL if n in got]
            if len(have) == len(ESSENTIAL):
                break

            now = time.time()
            if now - last_note > 8:
                last_note = now
                left = int(deadline - now)
                _, em = read_login_state(ws)
                last_email = em or last_email
                who = f"（当前登录：{em}）" if em else ""
                say(f"  等登录中… 已拿到 {len(have)}/{len(ESSENTIAL)} 条核心 cookie{who}"
                    f"，还剩 {left} 秒")
            time.sleep(2)
        else:
            return "timeout", "等待超时，没检测到登录完成。"

        say()
        say(f"  {_OK_MARK} 已抓到完整会话 cookie：")
        say()
        say(f"    {'cookie 名':<32}{'httpOnly':<10}{'有效期'}")
        say("    " + "-" * 60)
        for name in ESSENTIAL:
            c = got.get(name, {})
            exp = c.get("expires", -1)
            say(f"    {name:<32}{('是' if c.get('httpOnly') else '否'):<10}"
                f"{fmt_ts(exp)}{fmt_left(exp)}")
        say()

        if not label:
            _, em = read_login_state(ws)
            label = em or ("muse-" + time.strftime("%m%d-%H%M"))

        say(f"  正在上传到服务器（账号标签：{label}）…")
        try:
            r = upload(base, key, label, got)
        except RuntimeError as exc:
            say(f"  {_BAD_MARK} {exc}")
            say()
            say("  cookie 已经抓到了，只是没能自动上传。"
                "你可以把下面这行复制到管理页面的「导入账号」框里：")
            say()
            say("  " + "; ".join(f"{k}={v['value']}" for k, v in got.items()))
            return "noupload", "抓到了但上传失败"

        added = (r.get("added") or [{}])[0]
        say()
        hr("═")
        say(f"  {_OK_MARK} 导入成功！")
        say(f"      账号标签：{added.get('label', label)}")
        say(f"      账号 ID： {added.get('id', '(未知)')}")
        say(f"      cookie： {added.get('cookie_count', len(got))} 条")
        if r.get("warning"):
            say(f"      {_WARN_MARK} {r['warning']}")
        hr("═")
        return "ok", str(added.get("label", label))

    finally:
        if ws:
            ws.close()
        if not keep_open:
            kill_process_tree(proc)
        # ⚠️ 只删「我们自己创建的临时 profile」。
        #    用户用 --user-data-dir 指定的目录是他自己的浏览器配置，
        #    里面有书签、密码、登录态 —— 删了就是灾难。
        if not user_data_dir:
            for _ in range(10):
                try:
                    shutil.rmtree(profile, ignore_errors=True)
                    if not os.path.isdir(profile):
                        break
                except Exception:                       # noqa: BLE001
                    pass
                time.sleep(0.3)


# ---------------------------------------------------------------- 主流程
def main() -> int:
    init_console()
    conf = load_conf()

    ap = argparse.ArgumentParser(
        description="把 muse.ai 账号导入 muse2api 账号池（可导入多个）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "常用示例：\n"
            "  python get_muse_cookie.py                      只敲这一条，剩下的它来问\n"
            "  python get_muse_cookie.py --count 3            连续导入 3 个账号\n"
            "  python get_muse_cookie.py --list               看账号池里现在有哪些账号\n"
            "  python get_muse_cookie.py --remove acc-02      删掉某个账号\n"
            "  python get_muse_cookie.py --from-clipboard     从剪贴板导入（浏览器打不开时）\n"
        ))
    ap.add_argument("--base", default="", help="muse2api 地址，如 http://1.2.3.4:18610")
    ap.add_argument("--key", default="", help="管理员 Key（m2a_ 开头）")
    ap.add_argument("--label", default="", help="账号标签，如 acc-01（不给就自动取邮箱）")
    ap.add_argument("--count", type=int, default=0,
                    help="连续导入几个账号；给了就进全自动模式不再追问")
    ap.add_argument("--timeout", type=int, default=300,
                    help="每个账号等待登录的最长秒数，默认 300")
    ap.add_argument("--keep-open", action="store_true",
                    help="上传成功后不关闭浏览器窗口")
    ap.add_argument("--port", type=int, default=CDP_PORT,
                    help=f"调试端口，默认 {CDP_PORT}")
    ap.add_argument("--chrome", default="", help="手动指定浏览器可执行文件路径")
    ap.add_argument("--no-browser-check", action="store_true",
                    help="跳过浏览器检测（自己知道路径时用）")
    ap.add_argument("--user-data-dir", default="", metavar="目录",
                    help="借用你自己浏览器的配置目录（复用已登录状态，"
                         "但需先把那个浏览器完全关掉）")
    ap.add_argument("--list", action="store_true", help="列出账号池里的账号后退出")
    ap.add_argument("--remove", default="", metavar="ID或标签",
                    help="删除指定账号后退出")
    ap.add_argument("--from-clipboard", action="store_true",
                    help="不用浏览器：从剪贴板读 cookie 文本导入")
    args = ap.parse_args()

    banner("muse.ai 账号导入助手")

    # ---- 先做参数体检，把小白填错的地方当场说清楚
    #
    # ⚠️ 为什么要在最前面单独做一遍：
    #    早期版本对 --port / --count 的非法值是"默默用默认值"，然后走到
    #    后面的流程里报一个**牛头不对马嘴**的错。实测出现的两个例子：
    #      · `--count 0` / `--count -1` → 循环条件不成立，什么都没干，
    #        界面上却显示"没找到可用的浏览器"，小白以为是自己没装浏览器；
    #      · `--port 0` / `99999` / `-1` → 直接拿去当调试端口，浏览器一定起不来。
    #    这里的判据很简单：值不合法就明确告诉他"你给的数字不对"，并给出合法范围。
    if args.port < 1024 or args.port > 65535:
        say(f"  {_BAD_MARK} --port 给的 {args.port} 不是合法端口。")
        say("    要一个 1024~65535 之间的数字（默认 9432，一般不用改）。")
        return 2
    if args.count < 0:
        say(f"  {_BAD_MARK} --count 不能是负数（你给的是 {args.count}）。")
        say("    想连导 3 个就写：python get_muse_cookie.py --count 3")
        return 2
    if args.timeout < 30:
        say(f"  {_BAD_MARK} --timeout 给的 {args.timeout} 秒太短了。")
        say("    登录 muse.ai 需要时间，建议至少 120 秒（默认 300）。")
        return 2
    # ⚠️ `--remove ""` 曾经是个很坑的坑：args.remove 为空字符串时
    #    `if args.list or args.remove` 为假，脚本直接掉进"导入账号"的分支，
    #    于是小白想删账号，结果被弹出一个浏览器要求登录 —— 完全不知所措。
    #    这里用 `--remove` 是否**出现过**来判断，而不是看它的值空不空。
    remove_given = any(a == "--remove" or a.startswith("--remove=")
                       for a in sys.argv[1:])
    if remove_given and not args.remove.strip():
        say(f"  {_BAD_MARK} --remove 后面要跟账号的 id 或标签，但你没给。")
        say("    先看一眼有哪些账号：python get_muse_cookie.py --list")
        say("    再删：              python get_muse_cookie.py --remove acc-02")
        return 2
    args.remove = args.remove.strip()

    # ---- 地址 / Key：命令行 > 环境变量 > 上次记住的 > 现场问
    #
    # ⚠️ 三个「去空格」不能省：
    #    小白从网页/聊天窗口复制 Key 时，前后经常带上空格或换行。
    #    早期版本只在发请求时去空格，**显示用的是原始值**，于是界面打印出
    #    `管理员 Key： m2a_c9c2682…`（开头有空格）。小白照这行字去填客户端配置
    #    就会认证失败，而且完全看不出问题在哪。统一在这里 trim 一次。
    base = normalize_base((args.base
                           or os.environ.get("MUSE2API_BASE", "")
                           or conf.get("base", "")).strip())
    key = (args.key
           or os.environ.get("MUSE2API_KEY", "")
           or "").strip().strip('"').strip("'")

    # ⚠️ Key 里夹了换行/空格时，光 strip 两端不够 —— 中间的空格一定是粘贴事故，
    #    要去掉后再用（m2a_ 后面是纯十六进制，不该有任何空白）。
    key = re.sub(r"\s+", "", key)

    interactive = not (args.base and args.key)

    if interactive:
        say("  先确认两件事：服务器地址、管理员 Key。")
        say("  （忘了的话：在你的服务器上跑 `bash install.sh --credentials` 就能看回来）")
        say()
        # ⚠️ 地址要重问，不能「填了别的就用上次的」——
        #    那会让小白误以为自己的输入生效了，实际连的是旧服务器。
        #
        #    ⚠️ 重试次数在「非交互」环境里必须是 1：那里根本没人能回答，
        #       重试 5 次只会把同一段报错刷 5 遍（实测见过的画面），
        #       小白只会更慌。非交互时第一次拿不到就停下，直接教他怎么用。
        hint = base or "1.2.3.4:18610"
        _maxtry = 5 if sys.stdin.isatty() else 1
        for _try in range(_maxtry):
            raw = ask(f"  服务器地址（形如 {hint}）", base)
            if looks_like_address(raw):
                base = normalize_base(raw)
                break
            say(f"  {_BAD_MARK} 「{raw}」看着不像一个地址。")
            say("    正确样子（任选其一）：")
            say("      1.2.3.4:18610")
            say("      video.example.com")
            say("      https://my-domain.com")
            say("    （IP 和端口之间的冒号别漏了；端口一般是 18610）")
            base = ""          # 清掉，逼着重填，别拿旧的顶上
            hint = "1.2.3.4:18610"
        else:
            if sys.stdin.isatty():
                say(f"  {_BAD_MARK} 试了 5 次都不是地址，先退出了。重跑一次慢慢来：")
                say("      python get_muse_cookie.py")
            else:
                # 非交互：说明白原因 + 给出「不用问答」的正确用法
                say()
                say(f"  {_BAD_MARK} 当前不是交互终端，读不到你输入的地址。")
                say("    想让它问你，请在自己的电脑上直接双击运行，或在这个窗口敲：")
                say("        python get_muse_cookie.py")
                say("    想一条命令跑完，把地址和 Key 直接写在命令里：")
                say("        python get_muse_cookie.py --base http://1.2.3.4:18610 --key m2a_xxx")
            return 2

        # ⚠️ Key 这一段的写法很讲究，早期版本有个很坑的显示 bug：
        #       say("管理员 Key 上次记的是 xxx（直接回车沿用）")
        #       key = ask_secret("  管理员 Key：") or key
        #    ask_secret 的提示是 input() 打的、**不换行**，于是「上次记的是 xxx」
        #    和「管理员 Key：」两行挤在一起；而在日志/管道场景下 input 读不到东西，
        #    界面上就出现一个**空的 `管理员 Key：`**，小白根本不知道自己到底在用哪个
        #    Key —— 和之前修的「地址静默沿用」是同一类错误。
        #    正解：把「沿用 / 重填」讲成一句话，让人一眼看清用的哪个 Key。
        if key:
            say(f"  这个 Key 我还记着：{key[:12]}…")
            say(f"  {_OK_MARK} 直接回车就用它；想换一个就现在粘贴新的。")
            typed = ask_secret("  管理员 Key（回车沿用）：")
            if typed:
                key = re.sub(r"\s+", "", typed.strip().strip('"').strip("'"))
            else:
                say(f"  {_OK_MARK} 沿用 {key[:12]}…")
        else:
            say("  管理员 Key 是 m2a_ 开头的一长串，直接粘贴回来即可。")
            say("  （在你的服务器上跑 `bash install.sh --credentials` 能看到）")
            key = re.sub(
                r"\s+", "",
                ask_secret("  管理员 Key：").strip().strip('"').strip("'"))
            if not key:
                say(f"  {_BAD_MARK} 没读到 Key。要么粘贴一个，要么用参数指定：")
                say("      python get_muse_cookie.py --key m2a_xxx")
                return 2
    else:
        say(f"  服务器：{base}")
        say(f"  管理员 Key：{key[:12]}…")

    if not base or not key:
        say(f"  {_BAD_MARK} 地址或 Key 是空的，没法继续。")
        say("    手动指定：python get_muse_cookie.py --base http://1.2.3.4:18610 --key m2a_xxx")
        return 2

    # ---- 连通性自检（小白最容易在这里翻车，提前拦住并说清楚）
    say()
    say("  正在检查能不能连上服务器…")
    good, why = probe(base, key)
    if not good:
        say(f"  {_BAD_MARK} 连不上：{why}")
        say("    改地址/Key 后重跑即可。参数有误不会记住。")
        return 7
    say(f"  {_OK_MARK} 连接正常（{why}）")
    save_conf(base=base, key=key)

    # ---- 只列表 / 只删除，做完就走
    if args.list or args.remove:
        # 两个一起给时 --remove 优先，但必须说一声，别让 --list 被静默忽略。
        if args.list and args.remove:
            say(f"  {_WARN_MARK} 你同时给了 --list 和 --remove，先执行删除。")
        try:
            accts = list_accounts(base, key)
        except RuntimeError as exc:
            say(f"  {_BAD_MARK} {exc}")
            return 7
        if args.remove:
            hit = [a for a in accts
                   if str(a.get("id")) == args.remove
                   or a.get("label") == args.remove]
            if not hit:
                say(f"  {_BAD_MARK} 没找到 id 或标签为 {args.remove} 的账号。"
                    "跑 --list 看看有哪些。")
                return 8
            # ⚠️ 删除是不可逆的，先把要删的那个账号打出来让小白核对一眼。
            t = hit[0]
            say(f"  即将删除：{t.get('label') or '(无名)'}  "
                f"（id {t.get('id')}）")
            if delete_account(base, key, args.remove):
                say(f"  {_OK_MARK} 已删除：{args.remove}")
            else:
                say(f"  {_BAD_MARK} 删除失败（服务端可能没开放这个接口）。"
                    f"可以到管理页面手动删。")
                return 8
            return 0
        print_accounts(accts, base)
        return 0

    # ---- 剪贴板模式：完全不碰浏览器
    if args.from_clipboard:
        text = read_clipboard()
        ck = parse_cookie_text(text)
        missing = [n for n in ESSENTIAL if n not in ck]
        if missing:
            say(f"  {_BAD_MARK} 剪贴板里没找到完整的 cookie。")
            say(f"    缺：{', '.join(missing)}")
            say("    正确做法：在已登录 muse.ai 的浏览器里按 F12 → Application →")
            say("    Cookies → https://muse.ai → 依次复制下面 4 个的 Value：")
            for n in ESSENTIAL:
                say(f"      {n}")
            return 9
        label = args.label or ("muse-" + time.strftime("%m%d-%H%M"))
        try:
            r = upload(base, key, label, {k: {"value": v, "expires": -1}
                                          for k, v in ck.items()})
        except RuntimeError as exc:
            say(f"  {_BAD_MARK} {exc}")
            return 6
        added = (r.get("added") or [{}])[0]
        say(f"  {_OK_MARK} 导入成功！标签 {added.get('label', label)}"
            f"，ID {added.get('id', '(未知)')}")
        return 0

    # ---- 浏览器：检测
    # ---- 浏览器：检测
    #    ⚠️ --no-browser-check 只是「跳过自动检测」，不等于「没有浏览器」。
    #    所以在跳过检测时，必须要求 --chrome 明确给出路径；否则就退回自动检测。
    #    踩过的坑：早期版本这里让 browser=None 直接往下走，结果 subprocess 崩
    #    或者白等一个完整 timeout —— 对小白来说就是「卡住了」。
    browser = None
    if args.chrome:
        browser = find_browser(args.chrome)
        if not browser:
            say(f"  {_BAD_MARK} 你指定的浏览器路径不存在：{args.chrome}")
            say("    检查一下路径（注意 Windows 路径里的反斜杠要用 \\\\ 或整个用引号包起来）。")
            return 3
    elif args.no_browser_check:
        say(f"  {_BAD_MARK} --no-browser-check 需要同时用 --chrome 指定浏览器路径，")
        say("    否则我不知道该启动哪一个。例如：")
        say("      python get_muse_cookie.py --chrome \"C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe\"")
        return 3
    else:
        browser = find_browser()
        if not browser:
            say(f"  {_BAD_MARK} 没找到可用的浏览器（需要 Chrome / Edge / Brave / Vivaldi 之类）。")
            say()
            say("    三个选择，随便挑一个：")
            say("      1) 装一个 Google Chrome：https://www.google.cn/chrome/")
            say("      2) 已经有浏览器但装在别处 → 手动指定路径：")
            say("         python get_muse_cookie.py --chrome \"D:\\某处\\chrome.exe\"")
            say("      3) 不想装浏览器 → 用剪贴板模式（按 F12 手动复制 4 条 cookie）：")
            say("         python get_muse_cookie.py --from-clipboard")
            return 3
    say(f"  浏览器：{browser}")

    # ---- 决定导几个
    if args.count > 0:
        total = args.count
        say()
        say(f"  准备连续导入 {total} 个账号（全自动，不再追问）。")
    else:
        total = 1

    ok_cnt = 0
    round_no = 0
    while round_no < total:
        round_no += 1
        if total > 1:
            banner(f"第 {round_no} / {total} 个账号")
        elif round_no > 1:
            banner(f"再来一个（第 {round_no} 个）")

        if round_no > 1:
            say("  马上会弹出新的浏览器窗口，请在里面登录【另一个】muse.ai 账号。")
            say("  想收手就现在按 Ctrl+C。")
            time.sleep(3)

        status, info = grab_one(
            browser=browser, port=args.port, base=base, key=key,
            label=(args.label if total == 1 else ""),
            timeout=args.timeout, keep_open=args.keep_open,
            fresh=(round_no > 1), user_data_dir=args.user_data_dir)

        if status == "ok":
            ok_cnt += 1
        elif status == "timeout":
            say(f"  {_BAD_MARK} 没等到登录完成。")
        else:
            say(f"  {_BAD_MARK} 这一个没成功（{info}）。")

        # 命令行给了 --count 就严格按数量走，不问
        if args.count > 0:
            continue
        # 交互模式：问要不要再来一个
        say()
        if not ask_yes("  还要再导入一个 muse.ai 账号吗？", default_no=True):
            break
        total = round_no + 1

    # ---- 收尾：把账号池现状打出来
    say()
    try:
        accts = list_accounts(base, key)
        if accts:
            say(f"  账号池现在有 {len(accts)} 个账号：")
            print_accounts(accts, base)
        else:
            say(f"  {_WARN_MARK} 账号池还是空的 —— 没导入成功。")
            say("    重跑一次：python get_muse_cookie.py")
    except RuntimeError:
        pass

    if ok_cnt == 0:
        say(f"  {_BAD_MARK} 这轮一个都没导入成功。")
        return 5
    say(f"  {_OK_MARK} 完成，本轮成功导入 {ok_cnt} 个账号。")
    say()
    say("  接下来：打开网页 http://<你的地址>:8090/ 写一句话就能生成视频了。")
    return 0


def print_accounts(accts: list[dict], base: str) -> None:
    say()
    say(f"    {'标签':<20}{'账号 ID':<18}{'cookie':<8}{'状态'}")
    say("    " + "-" * 62)
    for a in accts:
        label = str(a.get("label") or a.get("name") or "(无名)")
        aid = str(a.get("id") or "-")
        n = a.get("cookie_count", a.get("cookies_count", "-"))
        st = a.get("status") or a.get("state") or "ok"
        say(f"    {label:<20}{aid:<18}{str(n):<8}{st}")
    say()
    # ⚠️ 空账号池不能只甩一个「（空）」就完事。
    #    小白跑 --list 就是为了确认"我到底导进去没有"，看到空表会懵在当场、
    #    不知道该干什么。必须把"下一步跑什么"直接写在脸上。
    if not accts:
        say(f"    {_WARN_MARK} 账号池是空的 —— 一个账号都还没导入。")
        say()
        say("    加一个账号很简单，敲这一条就行（会自动弹浏览器让你登录）：")
        say("        python get_muse_cookie.py")
        say()
        say("    想一口气加几个：python get_muse_cookie.py --count 3")
        return
    say(f"  共 {len(accts)} 个账号。管理页面：{base}/admin?key=<你的Key>")


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        say()
        say("  已取消（账号池不会受影响，已经导入的都在）。")
        sys.exit(130)
    except Exception as exc:                        # noqa: BLE001
        say()
        say(f"  {_BAD_MARK} 出错了：{exc}")
        say("    如果看不懂这句，把整段输出截图发出来即可。")
        sys.exit(1)
