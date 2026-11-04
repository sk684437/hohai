#!/usr/bin/env python3
"""HOHAI (tv.hohai.eu.org) 每日签到。

流程
    POST /api/auth/login               拿 auth_token（登录不需要人机验证）
    GET  /api/checkin/status           查今日是否已签
    /dashboard 点「立即签到」           前端弹出「签到验证」对话框
    点 Turnstile 复选框                 拿 verificationToken
    POST /api/checkin                  前端自动提交，或脚本补提交
    GET  /api/checkin/status           复核，防止把 status 响应误判成成功

关键约束（都是实测踩出来的）
    * 必须用真实 Chrome + CDP 接管。patchright.launch() 带自动化标记，Turnstile 直接报验证失败。
    * 不能用系统代理。Windows 注册表里的 127.0.0.1:10808 会把
      brunhild.challenges.cloudflare.com 的 TLS 掐断，盾永远过不去。

用法
    python hohai_checkin.py               正常签到
    python hohai_checkin.py --status      只查状态，不开浏览器
    python hohai_checkin.py --force       已签到也走一遍（调试）
    python hohai_checkin.py --smoke       只测浏览器能否启动

依赖
    pip install -r requirements.txt
"""

from __future__ import annotations

import argparse
import configparser
import json
import os
import random
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

try:
    import requests
except ImportError:  # pragma: no cover
    sys.exit("缺少依赖：pip install requests")

try:
    from patchright.sync_api import sync_playwright

    ENGINE = "patchright"
except ImportError:  # pragma: no cover
    try:
        from playwright.sync_api import sync_playwright

        ENGINE = "playwright"
    except ImportError:
        sync_playwright = None  # type: ignore[assignment]
        ENGINE = ""

ROOT = Path(__file__).resolve().parent
LOG_DIR = ROOT / "logs"
SHOT_DIR = ROOT / "screenshots"

# Windows 控制台默认 GBK，日志里有 ¥ 这类字符会炸
for stream in (sys.stdout, sys.stderr):
    with suppress(Exception):
        stream.reconfigure(encoding="utf-8", errors="replace")

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36"
)

SEL_CARD = '[data-checkin-card="dashboard"]'
SEL_CF_FRAME = 'iframe[src*="challenges.cloudflare.com"]'
SEL_DIALOG = '[role="dialog"]'
CF_HOST = "challenges.cloudflare.com"

TEXT_CHECKED = "今日已签到"
TEXT_FAILED = "验证失败"

# 这些失败原因说明盾没过，换出口（摘代理直连）有意义
RETRY_DIRECT_HINTS = ("验证失败", "超时", "未通过")

CHROME_PATHS = (
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/usr/bin/google-chrome",
    "/usr/bin/google-chrome-stable",
    "/opt/google/chrome/chrome",
    "/usr/bin/chromium",
    "/usr/bin/chromium-browser",
)

# 从页面取 Turnstile token：hook 缓存的 > 组件 API > 隐藏 input
JS_READ_TOKEN = """
() => {
  try {
    if (window.__vtToken && window.__vtToken.length > 30) return window.__vtToken;
  } catch (e) {}
  try {
    if (window.turnstile && typeof window.turnstile.getResponse === 'function') {
      const direct = window.turnstile.getResponse();
      if (direct && direct.length > 30) return direct;
      for (let i = 0; i < 6; i++) {
        const v = window.turnstile.getResponse(String(i));
        if (v && v.length > 30) return v;
      }
    }
  } catch (e) {}
  const inputs = document.querySelectorAll('input[name="cf-turnstile-response"], input[id$="_response"]');
  for (const el of inputs) if (el.value && el.value.length > 30) return el.value;
  return null;
}
"""

# 包裹 turnstile.render 的回调，把 token 缓存下来（仅 fallback context 注入）
JS_HOOK_TURNSTILE = """
(() => {
  window.__vtToken = null;
  window.__vtFailed = 0;
  const wrap = () => {
    const T = window.turnstile;
    if (!T || T.__hooked) return false;
    const render = T.render.bind(T);
    T.render = (el, opts) => {
      const o = Object.assign({}, opts || {});
      const ok = o.callback;
      const bad = o['error-callback'];
      o.callback = (tok) => {
        window.__vtToken = tok;
        try { ok && ok(tok); } catch (e) {}
      };
      o['error-callback'] = () => {
        window.__vtFailed = (window.__vtFailed || 0) + 1;
        try { bad && bad(); } catch (e) {}
      };
      return render(el, o);
    };
    T.__hooked = true;
    return true;
  };
  const timer = setInterval(() => { if (wrap()) clearInterval(timer); }, 100);
  setTimeout(() => clearInterval(timer), 60000);
})();
"""

# 真实 Chrome 下只补 webdriver，覆盖 window.chrome 反而更像机器人
JS_STEALTH = "try { Object.defineProperty(navigator, 'webdriver', { get: () => undefined }); } catch (e) {}"

Config = dict[str, str]


# ------------------------------------------------------------------ 基础设施

_logfile: Path | None = None


def now_str() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def log(message: str) -> None:
    line = f"[{now_str()}] {message}"
    print(line, flush=True)
    if _logfile is None:
        return
    try:
        with _logfile.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except OSError:
        pass


def truthy(value: Any) -> bool:
    return str(value).strip().lower() in ("1", "true", "yes", "on")


def js(page: Any, script: str, *args: Any) -> Any:
    """读页面数据用；页面被关掉时返回 None 而不是炸掉整个流程。"""
    try:
        return page.evaluate(script, *args) if args else page.evaluate(script)
    except Exception:
        return None


def screenshot(page: Any, name: str) -> Path | None:
    try:
        SHOT_DIR.mkdir(parents=True, exist_ok=True)
        path = SHOT_DIR / f"{name}_{datetime.now():%Y%m%d_%H%M%S}.png"
        page.screenshot(path=str(path))
        return path
    except Exception:
        return None


# ------------------------------------------------------------------ 配置

DEFAULTS: Config = {
    "base_url": "https://tv.hohai.eu.org",
    "username": "",
    "password": "",
    "headless": "false",
    "timeout": "180",
    "proxy": "",
    "tg_bot_token": "",
    "tg_chat_id": "",
    "tg_proxy": "",
    "use_cdp": "true",
    "keep_open": "false",
}

# 环境变量 -> 配置键。前面是规范名，后面几个是兼容别名。
ENV_KEYS: dict[str, str] = {
    "HOHAI_BASE_URL": "base_url",
    "HOHAI_USERNAME": "username",
    "HOHAI_PASSWORD": "password",
    "HOHAI_HEADLESS": "headless",
    "HOHAI_TIMEOUT": "timeout",
    "HOHAI_PROXY": "proxy",
    "HOHAI_TG_BOT_TOKEN": "tg_bot_token",
    "HOHAI_TG_CHAT_ID": "tg_chat_id",
    "HOHAI_TG_PROXY": "tg_proxy",
    "HOHAI_USE_CDP": "use_cdp",
    "HOHAI_KEEP_OPEN": "keep_open",
    "EMAIL": "username",
    "PASSWORD": "password",
    "TG_BOT_TOKEN": "tg_bot_token",
    "TG_CHAT_ID": "tg_chat_id",
    "TG_PROXY": "tg_proxy",
    "PROXY_SERVER": "proxy",
}


def load_config() -> Config:
    """config.ini 打底，环境变量覆盖（GitHub Actions 走这条路）。"""
    cfg: Config = dict(DEFAULTS)

    ini = ROOT / "config.ini"
    if ini.exists():
        parser = configparser.ConfigParser()
        parser.read(ini, encoding="utf-8")
        if parser.has_section("hohai"):
            cfg.update({k.strip(): v.strip() for k, v in parser.items("hohai")})

    for env, key in ENV_KEYS.items():
        value = os.environ.get(env)
        if value:
            cfg[key] = value.strip()

    return cfg


# ------------------------------------------------------------------ HTTP


class HohaiAPI:
    """登录 / 查状态 / 提交签到。token 内部持有，避免到处传参。"""

    def __init__(self, cfg: Config) -> None:
        self.base = cfg["base_url"].rstrip("/")
        self.cfg = cfg
        self.token = ""
        self.session = requests.Session()
        # Windows 的 urllib 会从注册表读 Internet Settings 代理，本机那个
        # 127.0.0.1:10808 会掐断 Turnstile 域名，所以默认完全无视系统代理。
        self.session.trust_env = False
        self.session.headers.update(
            {
                "User-Agent": USER_AGENT,
                "Accept": "application/json, text/plain, */*",
                "Origin": self.base,
                "Referer": f"{self.base}/login",
            }
        )
        if cfg.get("proxy"):
            self.session.proxies = {"http": cfg["proxy"], "https": cfg["proxy"]}

    def login(self) -> None:
        resp = self.session.post(
            f"{self.base}/api/auth/login",
            json={
                "userName": self.cfg["username"],
                "password": self.cfg["password"],
                "rememberMe": True,
            },
            timeout=30,
        )
        if resp.status_code != 200:
            raise RuntimeError(f"登录失败 HTTP {resp.status_code}: {resp.text[:300]}")
        data = resp.json()
        token = data.get("token")
        if not token:
            raise RuntimeError(f"登录响应缺少 token: {json.dumps(data, ensure_ascii=False)[:300]}")
        self.token = token
        user = (data.get("user") or {}).get("userName") or self.cfg["username"]
        log(f"登录成功：{user}")

    def status(self) -> dict:
        resp = self.session.get(
            f"{self.base}/api/checkin/status",
            headers={"Authorization": f"Bearer {self.token}"},
            timeout=30,
        )
        try:
            return resp.json() if resp.content else {}
        except ValueError:
            return {"_status": resp.status_code, "_raw": resp.text[:300]}

    def checkin(self, turnstile_token: str) -> tuple[int, dict]:
        resp = self.session.post(
            f"{self.base}/api/checkin",
            json={"verificationToken": turnstile_token},
            headers={
                "Authorization": f"Bearer {self.token}",
                "Content-Type": "application/json",
            },
            timeout=30,
        )
        try:
            return resp.status_code, resp.json()
        except ValueError:
            return resp.status_code, {"_raw": resp.text[:300]}


class Notifier:
    """推 Telegram：先发文字，再发截图。

    tg_proxy 与 proxy 分开：国内直连 api.telegram.org 不通，但签到站点反而必须直连，
    两条链路不能共用一个代理。
    """

    def __init__(self, cfg: Config) -> None:
        self.bot_token = cfg.get("tg_bot_token", "")
        self.chat_id = cfg.get("tg_chat_id", "")
        self.proxy = (cfg.get("tg_proxy") or cfg.get("proxy") or "").strip()

    @property
    def enabled(self) -> bool:
        return bool(self.bot_token and self.chat_id)

    def send(self, text: str, photo: Path | None = None) -> None:
        if not self.enabled:
            return
        session = requests.Session()
        session.trust_env = False
        if self.proxy:
            session.proxies = {"http": self.proxy, "https": self.proxy}
        self._send_message(session, text)
        if photo and photo.is_file():
            self._send_photo(session, photo)

    def _send_message(self, session: requests.Session, text: str) -> None:
        try:
            resp = session.post(
                f"https://api.telegram.org/bot{self.bot_token}/sendMessage",
                json={"chat_id": self.chat_id, "text": text, "disable_web_page_preview": True},
                timeout=20,
            )
            if resp.status_code == 200:
                log("Telegram 文字已发送")
            else:
                log(f"Telegram 文字发送失败：{resp.text[:150]}")
        except Exception as exc:
            log(f"Telegram 文字异常：{exc}")

    def _send_photo(self, session: requests.Session, photo: Path) -> None:
        try:
            with photo.open("rb") as fh:
                resp = session.post(
                    f"https://api.telegram.org/bot{self.bot_token}/sendPhoto",
                    data={"chat_id": self.chat_id, "caption": photo.name},
                    files={"photo": fh},
                    timeout=90,
                )
            if resp.status_code == 200:
                log("Telegram 截图已发送")
            else:
                log(f"Telegram 截图发送失败：{resp.text[:150]}")
        except Exception as exc:
            log(f"Telegram 截图异常：{exc}")


# ------------------------------------------------------------------ 浏览器


@dataclass
class CheckinResult:
    ok: bool = False
    already: bool = False
    amount: Any = None
    currency: str = ""
    reason: str = ""
    turnstile_token: str = ""
    screenshot: Path | None = None


@dataclass
class BrowserSession:
    browser: Any
    page: Any
    proc: subprocess.Popen | None = None
    profile: Path | None = None

    def close(self) -> None:
        with suppress(Exception):
            self.browser.close()
        if self.proc is not None:
            try:
                self.proc.terminate()
                self.proc.wait(timeout=10)
            except Exception:
                with suppress(Exception):
                    self.proc.kill()
            log("真实 Chrome 已退出")
        if self.profile is not None:
            shutil.rmtree(self.profile, ignore_errors=True)


def find_chrome() -> str | None:
    return next((p for p in CHROME_PATHS if p and os.path.isfile(p)), None)


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def wait_cdp(port: int, timeout_s: float) -> bool:
    """等 CDP 端口起来。显式禁用代理，避免被系统代理（本机 127.0.0.1:10808）拦掉。"""
    url = f"http://127.0.0.1:{port}/json/version"
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            with opener.open(url, timeout=1.5) as resp:
                if resp.status == 200:
                    return True
        except Exception:
            time.sleep(0.3)
    return False


def start_cdp_chrome(playwright: Any, cfg: Config) -> BrowserSession | None:
    """起一个没有自动化标记的真实 Chrome，再用 CDP 接管。"""
    exe = find_chrome()
    if exe is None:
        return None

    port = free_port()
    profile = Path(tempfile.mkdtemp(prefix="hohai_profile_"))
    args = [
        exe,
        f"--remote-debugging-port={port}",
        f"--user-data-dir={profile}",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-infobars",
        "--hide-crash-restore-bubble",
        "--disable-dev-shm-usage",
        "--disable-features=Translate",
        "--lang=zh-CN",
        "--window-size=1920,1080",
        "--start-maximized",
        # 走代理时不暴露真实 IP，不然 Turnstile 能看到两条出路
        "--force-webrtc-ip-handling-policy=disable_non_proxied_udp",
    ]
    if truthy(os.environ.get("HOHAI_NO_SANDBOX")):
        args.append("--no-sandbox")  # 容器里 root 跑才需要
    args.append(f"--proxy-server={cfg['proxy']}" if cfg.get("proxy") else "--no-proxy-server")
    args.append("about:blank")

    proc = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if not wait_cdp(port, 40):
        with suppress(Exception):
            proc.terminate()
        shutil.rmtree(profile, ignore_errors=True)
        log("真实 Chrome 未能启动，回退到内置 Chromium")
        return None

    browser = playwright.chromium.connect_over_cdp(f"http://127.0.0.1:{port}")
    ctx = browser.contexts[0] if browser.contexts else browser.new_context()
    page = ctx.pages[0] if ctx.pages else ctx.new_page()
    page.set_default_timeout(30000)
    route = f"走代理 {cfg['proxy']}" if cfg.get("proxy") else "直连（不走系统代理）"
    log(f"已接管真实 Chrome：CDP {port}，无自动化标记，{route}")
    return BrowserSession(browser=browser, page=page, proc=proc, profile=profile)


def start_fallback_browser(playwright: Any, cfg: Config, headless: bool) -> BrowserSession:
    """CDP 不可用时的退路：patchright/playwright 自带浏览器，注入反检测脚本。"""
    launch_kwargs: dict = {"headless": headless, "args": ["--disable-infobars"]}
    try:
        browser = playwright.chromium.launch(channel="chrome", **launch_kwargs)
    except Exception:
        log("未找到系统 Chrome，回退到内置 Chromium")
        browser = playwright.chromium.launch(**launch_kwargs)

    ctx = browser.new_context(
        viewport=None,
        locale="zh-CN",
        timezone_id="Asia/Shanghai",
        proxy={"server": cfg["proxy"]} if cfg.get("proxy") else None,
    )
    ctx.add_init_script(JS_STEALTH)
    ctx.add_init_script(JS_HOOK_TURNSTILE)
    page = ctx.new_page()
    page.set_default_timeout(30000)
    return BrowserSession(browser=browser, page=page)


@contextmanager
def browser_page(cfg: Config, headless: bool) -> Iterator[BrowserSession]:
    if sync_playwright is None:
        raise RuntimeError("未安装 patchright / playwright")
    with sync_playwright() as playwright:
        use_cdp = truthy(cfg.get("use_cdp", "true")) and not headless
        session = start_cdp_chrome(playwright, cfg) if use_cdp else None
        if session is None:
            session = start_fallback_browser(playwright, cfg, headless)
        page = session.page
        page.on("console", _on_console)
        try:
            yield session
        finally:
            if truthy(cfg.get("keep_open")):
                log("keep_open=true，浏览器保持打开 60 秒")
                time.sleep(60)
            session.close()


def _on_console(message: Any) -> None:
    try:
        if message.type not in ("error", "warning"):
            return
        text = message.text or ""
        if any(word in text.lower() for word in ("turnstile", "cloudflare", "challenge")):
            log(f"[console] {text[:180]}")
    except Exception:
        pass


# ------------------------------------------------------------------ 页面操作


def is_login_page(page: Any) -> bool:
    try:
        return bool(page.query_selector('input[type="password"]')) and bool(
            page.query_selector('button[type="submit"]')
        )
    except Exception:
        return False


def wait_card(page: Any, timeout_s: float) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            if page.query_selector(SEL_CARD):
                return True
        except Exception:
            pass
        time.sleep(0.4)
    return False


def card_text(page: Any) -> str:
    try:
        return page.locator(SEL_CARD).inner_text()
    except Exception:
        return ""


def dialog_text(page: Any) -> str:
    try:
        box = page.locator(SEL_DIALOG)
        if not box.count():
            return ""
        return box.first.inner_text().replace("\n", " / ")[:200]
    except Exception:
        return ""


def form_login(page: Any, cfg: Config) -> bool:
    """token 注入失效时，退回站点自己那张登录表单。"""
    if not is_login_page(page):
        return False
    try:
        log("改用表单登录 ...")
        page.fill('input[type="text"]', cfg["username"], timeout=15000)
        page.fill('input[type="password"]', cfg["password"], timeout=15000)
        page.click('button[type="submit"]', timeout=15000)
        return True
    except Exception as exc:
        log(f"表单登录失败：{exc}")
        return False


def goto_dashboard(page: Any, cfg: Config, token: str) -> bool:
    base = cfg["base_url"].rstrip("/")
    log("打开登录页并注入 token ...")
    page.goto(f"{base}/login", wait_until="domcontentloaded", timeout=60000)
    js(page, "(t) => { localStorage.setItem('auth_token', t); }", token)
    page.goto(f"{base}/dashboard", wait_until="domcontentloaded", timeout=60000)

    if wait_card(page, 40):
        return True
    if is_login_page(page):
        log("token 未被接受，回退到表单登录")
        if form_login(page, cfg) and wait_card(page, 60):
            return True
    return False


def click_checkin_button(page: Any) -> str | None:
    """点卡片上的按钮，返回被点掉的按钮文案；已签到时那个按钮是「查看奖励」。"""
    for selector in (f'{SEL_CARD} button:has-text("立即签到")', f"{SEL_CARD} button"):
        with suppress(Exception):
            button = page.locator(selector).first
            button.wait_for(state="visible", timeout=15000)
            button.scroll_into_view_if_needed()
            label = (button.inner_text(timeout=2000) or "").strip()
            button.click(timeout=15000)
            log(f"已点击按钮「{label or '?'}」")
            return label or "?"
    return None


def turnstile_frames(page: Any) -> list:
    """弹窗里那个有实际尺寸的 Cloudflare iframe。"""
    frames = []
    try:
        for handle in page.query_selector_all(SEL_CF_FRAME):
            try:
                box = handle.bounding_box()
                if box and box["width"] > 10 and box["height"] > 10:
                    frames.append(handle)
            except Exception:
                continue
    except Exception:
        pass
    return frames


def checkbox_visible(page: Any) -> bool:
    """验证进行中复选框会变成转圈，这时再点会打断挑战。"""
    for frame in page.frames:
        if CF_HOST not in (frame.url or ""):
            continue
        try:
            if frame.locator('input[type="checkbox"]:visible').count():
                return True
        except Exception:
            continue
    return False


def click_turnstile(page: Any, frame_handle: Any = None) -> bool:
    """先点 iframe 内的复选框元素，失败再退化成真实鼠标坐标点击。"""
    for frame in page.frames:
        if CF_HOST not in (frame.url or ""):
            continue
        try:
            frame.locator('input[type="checkbox"]').first.click(timeout=2500)
            log("已点击 Turnstile 复选框（元素）")
            return True
        except Exception:
            continue

    if frame_handle is None:
        return False
    try:
        with suppress(Exception):
            frame_handle.scroll_into_view_if_needed(timeout=3000)
        box = frame_handle.bounding_box()
        if not box:
            return False
        x = box["x"] + random.uniform(18, 34)
        y = box["y"] + box["height"] / 2 + random.uniform(-4, 4)
        page.mouse.move(x - random.uniform(30, 60), y + random.uniform(-8, 8))
        time.sleep(random.uniform(0.15, 0.4))
        page.mouse.move(x, y)
        time.sleep(random.uniform(0.05, 0.2))
        page.mouse.down()
        time.sleep(random.uniform(0.04, 0.1))
        page.mouse.up()
        log("已点击 Turnstile 复选框（坐标）")
        return True
    except Exception:
        return False


def read_turnstile_token(page: Any) -> str:
    token = js(page, JS_READ_TOKEN)
    return token if isinstance(token, str) else ""


def reset_turnstile(page: Any) -> bool:
    result = js(
        page,
        """
        () => {
          if (!window.turnstile) return false;
          for (const arg of [undefined, 'turnstile-container']) {
            try { window.turnstile.reset(arg); return true; } catch (e) {}
          }
          return false;
        }
    """,
    )
    return bool(result)


def reopen_dialog(page: Any) -> None:
    try:
        page.locator(f'{SEL_DIALOG} button:has-text("取消")').first.click(timeout=5000)
    except Exception:
        with suppress(Exception):
            page.keyboard.press("Escape")
    time.sleep(2)
    with suppress(Exception):
        page.locator(f"{SEL_CARD} button").first.click(timeout=10000)


# ------------------------------------------------------------------ 签到主逻辑


def capture_responses(page: Any) -> list[dict]:
    """只认真实的签到提交：POST /api/checkin，排除 GET /api/checkin/status。"""
    hits: list[dict] = []

    def on_response(resp: Any) -> None:
        try:
            if resp.request.method.upper() != "POST":
                return
            if not resp.url.split("?")[0].rstrip("/").endswith("/api/checkin"):
                return
        except Exception:
            return
        try:
            body = resp.json()
        except Exception:
            body = {"_raw": resp.text()[:300]}
        hits.append({"status": resp.status, "body": body})
        log(f"捕获签到响应 POST HTTP {resp.status}: {json.dumps(body, ensure_ascii=False)[:300]}")

    page.on("response", on_response)
    return hits


def settle_from_response(hits: list[dict], result: CheckinResult) -> bool:
    """前端自己提交成功 / 重复签到，直接从响应判定。"""
    for hit in hits:
        body = hit["body"]
        if hit["status"] == 200 and body.get("success") and "hasCheckedInToday" not in body:
            result.ok = True
            result.amount = body.get("amount")
            result.currency = body.get("currencyUnit", "")
            result.reason = "前端提交成功"
            log(f"签到成功，奖励 {result.amount} {result.currency}")
            return True
        if hit["status"] == 400 and body.get("hasCheckedIn"):
            result.ok = True
            result.already = True
            result.amount = body.get("amount")
            result.reason = "重复签到"
            log("重复签到：今日已签到")
            return True
    return False


@dataclass
class SolveState:
    """等盾过程中的节流状态：什么时候可以再点一次、再重置一次。"""

    deadline: float
    next_click: float
    next_reset: float = 0.0
    next_note: float = 0.0
    clicks: int = 0
    reopened: int = 0


def success_from_card(page: Any, hits: list[dict], result: CheckinResult) -> bool:
    """卡片已经显示「今日已签到」，本轮算过。"""
    if TEXT_CHECKED not in card_text(page):
        return False
    settled = [hit for hit in hits if hit["status"] == 200]
    if settled:
        result.amount = settled[-1]["body"].get("amount")
    else:
        result.already = True
    result.ok = True
    result.screenshot = screenshot(page, "checkin_success")
    log("卡片显示「今日已签到」，已截图")
    return True


def take_turnstile_token(page: Any, result: CheckinResult) -> bool:
    """盾过了：把 token 记进结果，前端不提交时由 API 兜底。"""
    token = read_turnstile_token(page)
    if not token:
        return False
    log(f"已取得 Turnstile token（{len(token)} 字符）")
    result.turnstile_token = token
    return True


def note_dialog(page: Any, state: SolveState) -> None:
    """每 10 秒把弹窗文案打到日志，方便回看卡在哪一步。"""
    if time.monotonic() - state.next_note <= 10:
        return
    state.next_note = time.monotonic()
    text = dialog_text(page)
    if text:
        log(f"弹窗：{text}")


def click_checkbox(page: Any, state: SolveState, result: CheckinResult) -> bool:
    """到点了就把 Turnstile 复选框点一下（元素点击，失败退坐标点击）。"""
    if time.monotonic() < state.next_click:
        return False
    frames = turnstile_frames(page)
    if not frames or not checkbox_visible(page) or not click_turnstile(page, frames[-1]):
        return False
    state.clicks += 1
    state.next_click = time.monotonic() + 6
    if state.clicks == 1:
        result.screenshot = screenshot(page, "after_first_click")
    time.sleep(random.uniform(3, 5))
    return True


def reset_if_failed(page: Any, state: SolveState) -> None:
    """弹窗出现「验证失败」就重置组件，重置不动就把弹窗关了重开。"""
    if time.monotonic() < state.next_reset or TEXT_FAILED not in dialog_text(page):
        return
    state.next_reset = time.monotonic() + 8
    log("Turnstile 返回验证失败，重置组件重试")
    if not reset_turnstile(page) and state.reopened < 3:
        state.reopened += 1
        log(f"重置无效，关闭弹窗重新打开（第 {state.reopened} 次）")
        reopen_dialog(page)
    time.sleep(2)


def timeout_result(page: Any, hits: list[dict], result: CheckinResult) -> CheckinResult:
    """等超时了：优先报「请求被拒」的真实原因，否则报盾没过。"""
    result.screenshot = screenshot(page, "timeout")
    rejected = [hit for hit in hits if not (hit["status"] == 200 and hit["body"].get("success"))]
    if not rejected:
        result.reason = "等待签到结果超时（Turnstile 未通过）"
        return result
    last = rejected[-1]
    body = last["body"]
    detail = body.get("message") or body.get("error") or str(body)[:120]
    result.reason = f"签到请求被拒 HTTP {last['status']}: {detail}"
    return result


def solve_turnstile(page: Any, hits: list[dict], budget: int) -> CheckinResult:
    """等 Turnstile 放行：能自动过就等响应，过不去就点复选框，失败了重置重来。"""
    result = CheckinResult()
    now = time.monotonic()
    state = SolveState(deadline=now + budget, next_click=now + 1.5)

    while time.monotonic() < state.deadline:
        if settle_from_response(hits, result):
            time.sleep(1.5)
            result.screenshot = screenshot(page, "checkin_success")
            log("已截图，随后关闭浏览器")
            return result

        if page.is_closed():
            result.reason = "浏览器窗口被关闭，流程中断"
            return result

        if success_from_card(page, hits, result):
            return result

        if take_turnstile_token(page, result):
            time.sleep(5)
            if not hits:
                log("前端未自动提交，脚本直接调用 /api/checkin")
                return result
            continue

        note_dialog(page, state)
        if click_checkbox(page, state, result):
            continue
        reset_if_failed(page, state)
        time.sleep(1)

    return timeout_result(page, hits, result)


def already_result(page: Any, text: str = "今日已签到") -> CheckinResult:
    result = CheckinResult(ok=True, already=True, reason=text)
    result.screenshot = screenshot(page, "already_done")
    log(f"{text}（已截图，随后关闭浏览器）")
    return result


def browser_checkin(cfg: Config, token: str) -> CheckinResult:
    headless = truthy(cfg.get("headless"))
    budget = max(60, int(cfg.get("timeout") or 180))

    try:
        with browser_page(cfg, headless) as session:
            page = session.page
            hits = capture_responses(page)
            if not goto_dashboard(page, cfg, token):
                result = CheckinResult(reason="未进入面板：token 失效或账号密码错误")
                result.screenshot = screenshot(page, "no_card")
                return result
            log("已进入面板")

            if TEXT_CHECKED in card_text(page):
                return already_result(page)

            label = click_checkin_button(page)
            if label is None:
                result = CheckinResult(reason="未找到「立即签到」按钮")
                result.screenshot = screenshot(page, "no_button")
                return result

            # 卡片可能只是没渲染完，点开才发现今天已经签过了（此时按钮是「查看奖励」）
            time.sleep(2)
            if TEXT_CHECKED in card_text(page) or TEXT_CHECKED in dialog_text(page):
                return already_result(page)

            return solve_turnstile(page, hits, budget)

    except Exception as exc:
        return CheckinResult(reason=f"浏览器流程异常：{exc}")


# ------------------------------------------------------------------ 主流程


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="HOHAI 每日签到（模拟点击）")
    parser.add_argument("--status", action="store_true", help="只查询签到状态")
    parser.add_argument("--headed", action="store_true", help="强制显示浏览器窗口")
    parser.add_argument("--smoke", action="store_true", help="只测试浏览器能否启动")
    parser.add_argument("--token", default="", help="直接使用已有 auth_token（跳过登录）")
    parser.add_argument("--force", action="store_true", help="已签到也走一遍（调试用）")
    return parser.parse_args(argv)


def run_smoke(cfg: Config) -> int:
    if sync_playwright is None:
        log("未安装 patchright / playwright")
        return 1
    with sync_playwright() as playwright:
        try:
            browser = playwright.chromium.launch(channel="chrome", headless=False)
        except Exception as exc:
            log(f"浏览器启动失败：{exc}")
            return 1
        try:
            page = browser.new_context().new_page()
            page.goto(f"{cfg['base_url'].rstrip('/')}/login", wait_until="domcontentloaded", timeout=60000)
            log(f"浏览器可用，页面标题：{page.title()}")
        finally:
            browser.close()
    return 0


def describe(result: CheckinResult, status: dict) -> str:
    """通知文案。"""
    currency = result.currency or status.get("currencyUnit") or ""
    if result.already:
        return f"HOHAI 今日已签到（重复执行）\n时间：{now_str()}"
    lines = [
        "HOHAI 签到成功",
        f"奖励：+{result.amount} {currency}".rstrip(),
        f"时间：{now_str()}",
    ]
    balance = status.get("balance")
    if balance is not None:
        lines.append(f"余额：{balance} {currency}".rstrip())
    return "\n".join(lines)


def notify_already(notifier: Notifier, status: dict) -> int:
    currency = status.get("currencyUnit") or ""
    text = f"HOHAI 今日已签到（{status.get('amount')} {currency}）\n时间：{now_str()}"
    log(text.replace("\n", " | "))
    if truthy(os.environ.get("HOHAI_QUIET_ALREADY")):
        log("已签到且设置了 HOHAI_QUIET_ALREADY，跳过通知")
        return 0
    if status.get("balance") is not None:
        text += f"\n余额：{status['balance']} {currency}".rstrip()
    notifier.send(text)
    return 0


def apply_api_fallback(api: HohaiAPI, result: CheckinResult) -> None:
    """拿到 token 但前端没提交时，用 requests 补一次。"""
    if result.ok or not result.turnstile_token:
        return
    log("改用 API 直接提交 ...")
    code, body = api.checkin(result.turnstile_token)
    log(f"API 提交结果 HTTP {code}: {json.dumps(body, ensure_ascii=False)[:300]}")
    if code == 200 and body.get("success"):
        result.ok = True
        result.amount = body.get("amount")
        result.currency = body.get("currencyUnit", "")
    elif code == 400 and body.get("hasCheckedIn"):
        result.ok = True
        result.already = True
        result.amount = body.get("amount")


def verify_result(api: HohaiAPI, result: CheckinResult) -> dict:
    """响应说成功不算数，回查 status 确认真的加上了。返回最新 status。"""
    if not result.ok or result.already:
        return {}
    time.sleep(2)
    status = api.status()
    log(f"复核签到状态：{json.dumps(status, ensure_ascii=False)[:300]}")
    if not status.get("hasCheckedInToday"):
        result.ok = False
        result.reason = "响应看似成功，但复核仍是未签到"
        return status
    result.amount = status.get("amount", result.amount)
    result.currency = status.get("currencyUnit", result.currency)
    return status


def should_retry_direct(result: CheckinResult, cfg: Config) -> bool:
    """走代理时 Turnstile 容易失败，值得摘掉代理再试一次。

    只对「盾没过」重试：进不去面板、找不到按钮这些跟出口无关，重试纯浪费时间。
    """
    if not cfg.get("proxy") or result.ok or result.already:
        return False
    reason = result.reason or ""
    return any(hint in reason for hint in RETRY_DIRECT_HINTS)


def run(args: argparse.Namespace) -> int:
    cfg = load_config()
    if args.headed:
        cfg["headless"] = "false"

    LOG_DIR.mkdir(parents=True, exist_ok=True)
    global _logfile
    _logfile = LOG_DIR / f"checkin_{datetime.now():%Y%m%d}.log"

    log(f"=== HOHAI 签到开始（引擎 {ENGINE or '无'}）===")

    if args.smoke:
        return run_smoke(cfg)

    api = HohaiAPI(cfg)
    notifier = Notifier(cfg)

    token = (args.token or os.environ.get("HOHAI_TOKEN") or "").strip()
    if token:
        log("使用外部传入的 auth_token，跳过登录")
        api.token = token
    else:
        if not cfg["username"] or not cfg["password"]:
            log("缺少账号或密码：请在 config.ini 或环境变量 HOHAI_USERNAME / HOHAI_PASSWORD 中配置")
            return 2
        try:
            api.login()
        except Exception as exc:
            log(str(exc))
            notifier.send(f"HOHAI 签到失败：登录异常\n{exc}\n时间：{now_str()}")
            return 3

    status = api.status()
    log(f"签到状态：{json.dumps(status, ensure_ascii=False)[:300]}")

    if status.get("hasCheckedInToday") and not args.force:
        return notify_already(notifier, status)
    if args.status:
        log("仅查询模式，结束")
        return 0

    result = browser_checkin(cfg, api.token)
    if should_retry_direct(result, cfg):
        log(f"代理 {cfg['proxy']} 下盾没过（{result.reason}），摘掉代理直连再试一次")
        result = browser_checkin({**cfg, "proxy": ""}, api.token)
    apply_api_fallback(api, result)
    final_status = verify_result(api, result)

    if result.ok:
        text = describe(result, final_status)
        log(text.replace("\n", " | "))
        notifier.send(text, photo=result.screenshot)
        return 0

    reason = result.reason or "未知原因"
    log(f"签到失败：{reason}")
    notifier.send(f"HOHAI 签到失败：{reason}\n时间：{now_str()}", photo=result.screenshot)
    return 1


def main(argv: list[str] | None = None) -> int:
    return run(parse_args(argv))


if __name__ == "__main__":
    sys.exit(main())
