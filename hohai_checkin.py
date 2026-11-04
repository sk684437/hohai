#!/usr/bin/env python3
# -*- coding: utf-8 -*-

from __future__ import annotations

import argparse
import configparser
import json
import os
import random
import re
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from datetime import datetime
from pathlib import Path

try:
    import requests
except ImportError:
    sys.exit("缺少依赖：pip install requests")

ROOT = Path(__file__).resolve().parent
LOG_DIR = ROOT / "logs"
SHOT_DIR = ROOT / "screenshots"

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

try:
    from patchright.sync_api import sync_playwright  # 反检测内核，过 CF 成功率更高
    ENGINE = "patchright"
except ImportError:
    try:
        from playwright.sync_api import sync_playwright
        ENGINE = "playwright"
    except ImportError:
        sync_playwright = None
        ENGINE = None

DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36"
)

CF_IFRAME = 'iframe[src*="challenges.cloudflare.com"]'
CARD = '[data-checkin-card="dashboard"]'

# 每个账号允许覆盖的全局配置键
ACCOUNT_OVERRIDE_KEYS = (
    "proxy", "tg_bot_token", "tg_chat_id", "tg_proxy",
    "headless", "timeout", "use_cdp", "keep_open",
)

# 读取 Turnstile 通过后生成的 token（兼容自定义 container 与隐藏 input）
READ_TOKEN_JS = """
() => {
  try { if (window.__vtLast && window.__vtLast.length > 30) return window.__vtLast; } catch (e) {}
  try {
    if (window.turnstile && typeof window.turnstile.getResponse === 'function') {
      let t = null;
      try { t = window.turnstile.getResponse(); } catch (e) {}
      if (t && t.length > 30) return t;
      for (let i = 0; i < 6; i++) {
        try { const v = window.turnstile.getResponse(String(i)); if (v && v.length > 30) return v; } catch (e) {}
      }
    }
  } catch (e) {}
  const inputs = document.querySelectorAll('input[name="cf-turnstile-response"], input[id$="_response"]');
  for (const el of inputs) { if (el.value && el.value.length > 30) return el.value; }
  return null;
}
"""

# 真实 Chrome 下只补 navigator.webdriver，不覆盖 window.chrome（覆盖反而更像机器人）
STEALTH_JS = """
try { Object.defineProperty(navigator, 'webdriver', { get: () => undefined }); } catch (e) {}
"""

# 在页面脚本之前挂上 turnstile.render 的钩子，把成功回调里的 token 存进 window.__vtLast
HOOK_JS = """
(() => {
  window.__vtLast = null;
  window.__vtEvents = [];
  const wrap = () => {
    const T = window.turnstile;
    if (!T || T.__hooked) return false;
    try {
      const orig = T.render.bind(T);
      T.render = function (el, opts) {
        const o = Object.assign({}, opts || {});
        const cb = o.callback;
        o.callback = function (tok) {
          window.__vtLast = tok;
          try { window.__vtEvents.push('callback'); } catch (e) {}
          try { cb && cb(tok); } catch (e) {}
        };
        const ec = o['error-callback'];
        o['error-callback'] = function () {
          try { window.__vtEvents.push('error'); } catch (e) {}
          try { ec && ec(); } catch (e) {}
        };
        return orig(el, o);
      };
      T.__hooked = true;
      return true;
    } catch (e) { return false; }
  };
  const iv = setInterval(() => { if (wrap()) clearInterval(iv); }, 100);
  setTimeout(() => clearInterval(iv), 60000);
})();
"""

STATE = {"logfile": None, "last_shot": None, "account": None}


def _acct_tag() -> str:
    a = STATE.get("account")
    return f"[{a}] " if a else ""


def log(msg: str) -> None:
    line = f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {_acct_tag()}{msg}"
    print(line, flush=True)
    if STATE["logfile"]:
        try:
            with open(STATE["logfile"], "a", encoding="utf-8") as fh:
                fh.write(line + "\n")
        except Exception:
            pass


def _safe_name(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9_\-\u4e00-\u9fff]", "_", str(s))[:40] or "acct"


# ---------------------------------------------------------------- 配置

def load_config() -> dict:
    cfg = {
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
        "account_delay": "",
    }
    ini = ROOT / "config.ini"
    if ini.exists():
        cp = configparser.ConfigParser()
        cp.read(ini, encoding="utf-8")
        if cp.has_section("hohai"):
            for k, v in cp.items("hohai"):
                cfg[k.strip()] = v.strip()

    env_map = {
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
        "HOHAI_ACCOUNT_DELAY": "account_delay",
        # 兼容常见别名
        "EMAIL": "username",
        "PASSWORD": "password",
        "TG_BOT_TOKEN": "tg_bot_token",
        "TG_CHAT_ID": "tg_chat_id",
        "TG_PROXY": "tg_proxy",
        "PROXY_SERVER": "proxy",
    }
    for env, key in env_map.items():
        val = os.environ.get(env)
        if val:
            cfg[key] = val.strip()
    return cfg


def _split_user_pass(raw: str) -> tuple[str, str]:
    """`user:pass` 按第一个冒号切，密码里可以再带冒号。"""
    raw = (raw or "").strip()
    if ":" in raw:
        u, p = raw.split(":", 1)
        return u.strip(), p.strip()
    return raw, ""


def load_accounts(cfg: dict) -> list[dict]:
    """合并各来源账号，按用户名去重（先出现的优先）。

    返回 [{label, username, password, overrides{...}}]，overrides 只含 ACCOUNT_OVERRIDE_KEYS。
    """
    entries: list[dict] = []
    seen: set[str] = set()

    def add(label: str, username: str, password: str, overrides: dict | None = None) -> None:
        username = (username or "").strip()
        if not username:
            return
        if username in seen:
            log(f"账号 {username} 重复配置，忽略后出现的（标签 {label}）")
            return
        seen.add(username)
        entries.append({
            "label": (label or username).strip(),
            "username": username,
            "password": (password or "").strip(),
            "overrides": overrides or {},
        })

    ini = ROOT / "config.ini"
    cp = configparser.ConfigParser()
    if ini.exists():
        cp.read(ini, encoding="utf-8")

    # 1) [accounts] 段：`标签 = 用户名:密码`
    if cp.has_section("accounts"):
        for key, val in cp.items("accounts"):
            u, p = _split_user_pass(val)
            add(key.strip(), u, p)

    # 2) 环境变量 HOHAI_ACCOUNTS：`user:pass` 用 ; 或换行分隔（CI 用）
    env_accounts = os.environ.get("HOHAI_ACCOUNTS", "").strip()
    if env_accounts:
        for item in re.split(r"[;\r\n]+", env_accounts):
            item = item.strip()
            if not item:
                continue
            u, p = _split_user_pass(item)
            add(u, u, p)

    # 3) [account.标签] 段：直写 username/password，可覆盖全局键
    for sec in cp.sections():
        m = re.match(r"^account\.(.+)$", sec, re.IGNORECASE)
        if not m:
            continue
        label = m.group(1).strip()
        username = cp.get(sec, "username", fallback="").strip()
        password = cp.get(sec, "password", fallback="").strip()
        overrides = {k: cp.get(sec, k, fallback="").strip()
                     for k in ACCOUNT_OVERRIDE_KEYS if cp.get(sec, k, fallback="").strip()}
        target = next((e for e in entries if e["username"] == username and username), None)
        if target:
            target["overrides"].update(overrides)
            target["label"] = label
            log(f"账号 {username} 的覆盖配置已合并（标签 {label}）")
        else:
            add(label, username, password, overrides)

    # 4) 兼容旧版单账号（[hohai] username/password 或 HOHAI_USERNAME/HOHAI_PASSWORD）
    if not entries and cfg.get("username"):
        add("default", cfg["username"], cfg["password"])

    return entries


def truthy(val) -> bool:
    return str(val).strip().lower() in ("1", "true", "yes", "on")


# ---------------------------------------------------------------- HTTP

def make_session(cfg: dict) -> requests.Session:
    s = requests.Session()
    # Windows 上 urllib 会从注册表读 Internet Settings 的 ProxyServer，
    # 本机那个 127.0.0.1:10808 会把 Turnstile / 登录接口掐断，所以默认完全无视系统代理。
    s.trust_env = False
    s.headers.update({
        "User-Agent": DEFAULT_UA,
        "Accept": "application/json, text/plain, */*",
        "Origin": cfg["base_url"],
        "Referer": cfg["base_url"] + "/login",
    })
    if cfg.get("proxy"):
        s.proxies = {"http": cfg["proxy"], "https": cfg["proxy"]}
    return s


def api_login(sess: requests.Session, cfg: dict) -> str:
    url = cfg["base_url"].rstrip("/") + "/api/auth/login"
    payload = {
        "userName": cfg["username"],
        "password": cfg["password"],
        "rememberMe": True,
    }
    r = sess.post(url, json=payload, timeout=30)
    if r.status_code != 200:
        detail = r.text[:300]
        raise RuntimeError(f"登录失败 HTTP {r.status_code}: {detail}")
    data = r.json()
    token = data.get("token")
    if not token:
        raise RuntimeError(f"登录响应缺少 token: {json.dumps(data, ensure_ascii=False)[:300]}")
    user = data.get("user") or {}
    log(f"登录成功：{user.get('userName') or cfg['username']}")
    return token


def api_status(sess: requests.Session, cfg: dict, token: str) -> dict:
    url = cfg["base_url"].rstrip("/") + "/api/checkin/status"
    r = sess.get(url, headers={"Authorization": f"Bearer {token}"}, timeout=30)
    try:
        return r.json() if r.content else {}
    except Exception:
        return {"_status": r.status_code, "_raw": r.text[:300]}


def api_checkin(sess: requests.Session, cfg: dict, token: str, vt: str) -> tuple[int, dict]:
    url = cfg["base_url"].rstrip("/") + "/api/checkin"
    r = sess.post(
        url,
        json={"verificationToken": vt},
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        timeout=30,
    )
    try:
        body = r.json()
    except Exception:
        body = {"_raw": r.text[:300]}
    return r.status_code, body


def notify(cfg: dict, text: str, photo: str | None = None) -> None:
    """推 Telegram：先发文字，再把截图当照片发过去（同一会话里更直观）。

    tg_proxy 单独配置：国内直连 api.telegram.org 不通，但签到站点反而必须直连，
    两者走不同链路，所以不能共用一个 proxy。
    """
    tok, chat = cfg.get("tg_bot_token"), cfg.get("tg_chat_id")
    if not tok or not chat:
        return
    proxy = (cfg.get("tg_proxy") or cfg.get("proxy") or "").strip()
    sess = requests.Session()
    sess.trust_env = False
    if proxy:
        sess.proxies = {"http": proxy, "https": proxy}
    try:
        r = sess.post(
            f"https://api.telegram.org/bot{tok}/sendMessage",
            json={"chat_id": chat, "text": text, "disable_web_page_preview": True},
            timeout=20,
        )
        log("Telegram 文字已发送" if r.status_code == 200 else f"Telegram 文字发送失败：{r.text[:150]}")
    except Exception as exc:
        log(f"Telegram 文字异常：{exc}")

    if photo and os.path.isfile(photo):
        try:
            with open(photo, "rb") as fh:
                r = sess.post(
                    f"https://api.telegram.org/bot{tok}/sendPhoto",
                    data={"chat_id": chat, "caption": os.path.basename(photo)},
                    files={"photo": fh},
                    timeout=90,
                )
            log("Telegram 截图已发送" if r.status_code == 200 else f"Telegram 截图发送失败：{r.text[:150]}")
        except Exception as exc:
            log(f"Telegram 截图异常：{exc}")


# ---------------------------------------------------------------- 浏览器


def _is_login_page(page) -> bool:
    try:
        return bool(page.query_selector('input[type="password"]')) and bool(page.query_selector('button[type="submit"]'))
    except Exception:
        return False


def _wait_card(page, timeout_s: float) -> bool:
    end = time.time() + timeout_s
    while time.time() < end:
        try:
            if page.query_selector(CARD):
                return True
        except Exception:
            pass
        time.sleep(0.4)
    return False


def _ui_login(page, cfg: dict) -> bool:
    """在登录页表单里填账号密码并点「登录」（token 注入失效时的兜底）。"""
    try:
        if not _is_login_page(page):
            return False
        log("改用表单登录 ...")
        page.fill('input[type="text"]', cfg["username"], timeout=15000)
        page.fill('input[type="password"]', cfg["password"], timeout=15000)
        page.click('button[type="submit"]', timeout=15000)
        return True
    except Exception as exc:
        log(f"表单登录失败：{exc}")
        return False


def _find_visible_cf_frames(page):
    """可见且有实际尺寸的 Cloudflare iframe（弹窗内的通常排在最后）。"""
    out = []
    try:
        for handle in page.query_selector_all(CF_IFRAME):
            try:
                box = handle.bounding_box()
                if box and box["width"] > 10 and box["height"] > 10:
                    out.append(handle)
            except Exception:
                continue
    except Exception:
        pass
    return out


def _cf_checkbox_visible(page) -> bool:
    """任意 Cloudflare 框架内当前是否有可见复选框（验证进行中会变成转圈，此时不该再点）。"""
    for frame in page.frames:
        if "challenges.cloudflare.com" not in (frame.url or ""):
            continue
        try:
            if frame.locator('input[type="checkbox"]:visible').count() > 0:
                return True
        except Exception:
            continue
    return False


def _click_cf(page, frame_el=None) -> bool:
    """先点框架内复选框元素，失败退化为真实鼠标坐标点击（复选框在 iframe 左侧居中）。"""
    for frame in page.frames:
        if "challenges.cloudflare.com" not in (frame.url or ""):
            continue
        try:
            frame.locator('input[type="checkbox"]').first.click(timeout=2500)
            log("已点击 Turnstile 复选框（元素）")
            return True
        except Exception:
            continue
    if frame_el is None:
        return False
    try:
        try:
            frame_el.scroll_into_view_if_needed(timeout=3000)
        except Exception:
            pass
        box = frame_el.bounding_box()
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


def _vt_events(page):
    try:
        return page.evaluate("() => (window.__vtEvents || []).slice()")
    except Exception:
        return []


def _reset_turnstile(page) -> bool:
    """组件进入「验证失败」后重置它，重新发一次挑战。"""
    try:
        return bool(page.evaluate("""
            () => {
              if (!window.turnstile) return false;
              try { window.turnstile.reset(); return true; } catch (e) {}
              try { window.turnstile.reset('turnstile-container'); return true; } catch (e) {}
              return false;
            }
        """))
    except Exception:
        return False


def _shoot(page, name: str) -> None:
    try:
        SHOT_DIR.mkdir(parents=True, exist_ok=True)
        path = SHOT_DIR / f"{_safe_name(STATE.get('account') or 'acct')}_{name}_{datetime.now():%Y%m%d_%H%M%S}.png"
        page.screenshot(path=str(path))
        STATE["last_shot"] = str(path)
    except Exception:
        pass


CHROME_CANDIDATES = [
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/usr/bin/google-chrome",
    "/opt/google/chrome/chrome",
    "/usr/bin/google-chrome-stable",
    "/usr/bin/chromium",
    "/usr/bin/chromium-browser",
]


def _find_chrome() -> str | None:
    for path in CHROME_CANDIDATES:
        if path and os.path.isfile(path):
            return path
    return None


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait_cdp(port: int, timeout_s: float) -> bool:
    """等 Chrome 的 CDP 端口起来（真实 Chrome 从启动到可连通常 1~3 秒）。"""
    url = f"http://127.0.0.1:{port}/json/version"
    end = time.time() + timeout_s
    while time.time() < end:
        try:
            with urllib.request.urlopen(url, timeout=1.5) as r:
                if r.status == 200:
                    return True
        except Exception:
            time.sleep(0.3)
    return False


def _launch_cdp_chrome(p, cfg: dict, proxy_arg):
    """用真实 Chrome + 独立 profile 起一个**没有 --enable-automation**的窗口，再用 CDP 接管。

    这是实测唯一能让 HOHAI 的 Turnstile 自动放行的方式：
      - patchright.launch() 会带自动化标记，Turnstile 直接报「验证失败」；
      - 而且不能走系统代理（本机 127.0.0.1:10808 会把 brunhild.challenges.cloudflare.com 的 TLS 掐断）。
    返回 (browser, ctx, page, proc)。
    """
    exe = _find_chrome()
    if not exe:
        return None, None, None, None
    port = _free_port()
    profile = tempfile.mkdtemp(prefix="hohai_profile_")
    args = [
        exe,
        f"--remote-debugging-port={port}",
        f"--user-data-dir={profile}",
        "--no-first-run",
        "--no-default-browser-check",
        "--start-maximized",
        "--window-size=1920,1080",
        "--disable-infobars",
        "--hide-crash-restore-bubble",
        "--disable-dev-shm-usage",
        "--lang=zh-CN",
        "--disable-features=Translate",
    ]
    if truthy(os.environ.get("HOHAI_NO_SANDBOX", "")):
        # 容器里 root 跑 Chrome 才需要；会略微改变渲染进程模型，仅 CI 容器用
        args.append("--no-sandbox")
    args.append(f"--proxy-server={proxy_arg['server']}" if proxy_arg else "--no-proxy-server")
    args.append("about:blank")
    proc = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if not _wait_cdp(port, 40):
        try:
            proc.terminate()
        except Exception:
            pass
        return None, None, None, None
    browser = p.chromium.connect_over_cdp(f"http://127.0.0.1:{port}")
    ctx = browser.contexts[0] if browser.contexts else browser.new_context()
    page = ctx.pages[0] if ctx.pages else ctx.new_page()
    page.set_default_timeout(30000)
    log(f"已接管真实 Chrome（CDP {port}，无自动化标记，{'走代理 ' + proxy_arg['server'] if proxy_arg else '直连不走系统代理'}）")
    return browser, ctx, page, proc


def browser_checkin(cfg: dict, token: str) -> dict:
    """打开浏览器 -> 点「立即签到」-> 过 Turnstile -> 等 /api/checkin 响应。"""
    result = {"ok": False, "already": False, "amount": None, "currency": None, "reason": ""}
    if sync_playwright is None:
        result["reason"] = "未安装 patchright/playwright"
        return result

    base = cfg["base_url"].rstrip("/")
    headless = truthy(cfg.get("headless"))
    budget = max(60, int(cfg.get("timeout") or 180))

    proxy_arg = {"server": cfg["proxy"]} if cfg.get("proxy") else None
    use_cdp = truthy(cfg.get("use_cdp", "true")) and not headless

    with sync_playwright() as p:
        chrome_proc = None
        browser = ctx = page = None
        if use_cdp:
            browser, ctx, page, chrome_proc = _launch_cdp_chrome(p, cfg, proxy_arg)
        if browser is None:
            launch_kw = dict(headless=headless, args=["--disable-infobars"])
            try:
                browser = p.chromium.launch(channel="chrome", **launch_kw)
            except Exception:
                log("未找到系统 Chrome，回退到内置 Chromium")
                browser = p.chromium.launch(**launch_kw)

        if page is None:
            ctx = browser.new_context(
                viewport=None,
                locale="zh-CN",
                timezone_id="Asia/Shanghai",
                proxy=proxy_arg,
            )
            ctx.add_init_script(STEALTH_JS)
            ctx.add_init_script(HOOK_JS)
            page = ctx.new_page()
            page.set_default_timeout(30000)

        def on_console(msg):
            try:
                text = (msg.text or "").lower()
                if ("turnstile" in text or "cloudflare" in text or "challenge" in text) and msg.type in ("error", "warning"):
                    log(f"[console] {msg.text[:180]}")
            except Exception:
                pass

        page.on("console", on_console)

        hits: list[dict] = []

        def on_response(resp):
            # 只认真实的签到提交：POST /api/checkin（排除 GET /api/checkin/status）
            try:
                if resp.request.method.upper() != "POST":
                    return
                path = resp.url.split("?")[0].rstrip("/")
                if not path.endswith("/api/checkin"):
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

        try:
            log("打开登录页并注入 token ...")
            page.goto(base + "/login", wait_until="domcontentloaded", timeout=60000)
            page.evaluate("(t) => { localStorage.setItem('auth_token', t); }", token)
            page.goto(base + "/dashboard", wait_until="domcontentloaded", timeout=60000)

            entered = _wait_card(page, 40)
            if not entered and _is_login_page(page):
                log("token 未被接受，回退到表单登录")
                if _ui_login(page, cfg):
                    entered = _wait_card(page, 60)
            if not entered:
                _shoot(page, "no_card")
                result["reason"] = "未进入面板：token 失效或账号密码错误"
                return result
            log("已进入面板")
            card_text = page.locator(CARD).inner_text()
            if "今日已签到" in card_text:
                result.update(ok=True, already=True)
                _shoot(page, "already_done")
                log("今日已签到（已截图，随后关闭浏览器）")
                return result

            clicked = False
            for sel in (f'{CARD} button:has-text("立即签到")', f"{CARD} button"):
                try:
                    btn = page.locator(sel).first
                    btn.wait_for(state="visible", timeout=15000)
                    btn.scroll_into_view_if_needed()
                    btn.click(timeout=15000)
                    clicked = True
                    log("已点击「立即签到」")
                    break
                except Exception:
                    continue
            if not clicked:
                _shoot(page, "no_button")
                result["reason"] = "未找到「立即签到」按钮"
                return result

            deadline = time.time() + budget
            vt = None
            clicks = 0
            errs_handled = 0
            next_click = time.time() + 1.5
            last_note = 0.0
            reopened = 0

            while time.time() < deadline:
                try:
                    # 1) 前端已经提交
                    for h in hits:
                        body = h["body"]
                        if h["status"] == 200 and body.get("success") and "hasCheckedInToday" not in body:
                            result.update(
                                ok=True,
                                amount=body.get("amount"),
                                currency=body.get("currencyUnit"),
                                reason="前端提交成功",
                            )
                            log(f"签到成功，奖励 {body.get('amount')} {body.get('currencyUnit')}")
                            time.sleep(1.5)
                            _shoot(page, "checkin_success")
                            log("已截图，随后关闭浏览器")
                            return result
                        if h["status"] == 400 and body.get("hasCheckedIn"):
                            result.update(ok=True, already=True, amount=body.get("amount"))
                            _shoot(page, "already_done")
                            log("重复签到：今日已签到")
                            return result

                    # 2) 卡片文字判定
                    if "今日已签到" in page.locator(CARD).inner_text():
                        plain = [h for h in hits if h["status"] == 200]
                        if plain:
                            result.update(ok=True, amount=plain[-1]["body"].get("amount"))
                        else:
                            result.update(ok=True, already=True)
                        _shoot(page, "checkin_success")
                        log("卡片显示「今日已签到」，已截图")
                        return result

                    # 3) 读取 token（含 hook 到的回调 token）
                    vt = page.evaluate(READ_TOKEN_JS)
                    if vt:
                        log(f"已取得 Turnstile token（{len(vt)} 字节）")
                        time.sleep(5)
                        if hits:
                            continue
                        log("前端未自动提交，脚本直接调用 /api/checkin")
                        result["turnstile_token"] = vt
                        return result

                    # 4) 弹窗状态诊断
                    if time.time() - last_note > 10:
                        last_note = time.time()
                        try:
                            dlg = page.locator('[role="dialog"]')
                            if dlg.count():
                                txt = dlg.first.inner_text().replace("\n", " / ")[:140]
                                log(f"弹窗：{txt}")
                        except Exception:
                            pass

                    # 5) 只在复选框可见时点击（验证中会变转圈，此时再点会打断挑战）
                    frames = _find_visible_cf_frames(page)
                    if frames and _cf_checkbox_visible(page) and time.time() >= next_click:
                        if _click_cf(page, frames[-1]):
                            clicks += 1
                            next_click = time.time() + 6
                            if clicks == 1:
                                _shoot(page, "after_first_click")
                            time.sleep(random.uniform(3, 5))
                            continue

                    # 6) 组件报「验证失败」→ 重置重来；连续失败则关弹窗重新打开
                    errs = _vt_events(page).count("error")
                    if errs > errs_handled:
                        errs_handled = errs
                        log("Turnstile 返回验证失败，重置组件重试")
                        if not _reset_turnstile(page) and reopened < 3:
                            reopened += 1
                            log(f"重置无效，关闭弹窗重新打开（第 {reopened} 次）")
                            try:
                                page.locator('[role="dialog"] button:has-text("取消")').first.click(timeout=5000)
                            except Exception:
                                page.keyboard.press("Escape")
                            time.sleep(2)
                            try:
                                page.locator(f'{CARD} button').first.click(timeout=10000)
                            except Exception:
                                pass
                        next_click = time.time() + 3
                        time.sleep(2)

                    time.sleep(1)

                except Exception as exc:
                    msg = str(exc)
                    if "has been closed" in msg or "Target closed" in msg or "Target page" in msg:
                        result["reason"] = "浏览器窗口被关闭，流程中断"
                        return result
                    raise

            _shoot(page, "timeout")
            bad = [h for h in hits if not (h["status"] == 200 and h["body"].get("success"))]
            if bad:
                b = bad[-1]
                detail = b["body"].get("message") or b["body"].get("error") or str(b["body"])[:120]
                result["reason"] = f"签到请求被拒 HTTP {b['status']}: {detail}"
            else:
                result["reason"] = "等待签到结果超时（Turnstile 未通过）"
            result["turnstile_token"] = vt
            return result

        except Exception as exc:
            _shoot(page, "exception")
            result["reason"] = f"浏览器流程异常：{exc}"
            return result
        finally:
            if truthy(cfg.get("keep_open")):
                log("keep_open=true，浏览器保持打开 60 秒")
                time.sleep(60)
            try:
                if browser is not None:
                    browser.close()
            except Exception:
                pass
            if chrome_proc is not None:
                try:
                    chrome_proc.terminate()
                    chrome_proc.wait(timeout=10)
                except Exception:
                    try:
                        chrome_proc.kill()
                    except Exception:
                        pass
                log("真实 Chrome 已退出")


# ---------------------------------------------------------------- 单账号流程

def run_account(cfg: dict, args) -> dict:
    """跑一个账号的完整流程，返回汇总 dict。"""
    res = {"code": 1, "ok": False, "already": False, "amount": None, "currency": None,
           "balance": None, "reason": ""}

    sess = make_session(cfg)
    token = (getattr(args, "token", "") or os.environ.get("HOHAI_TOKEN") or "").strip()
    if token:
        log("使用外部传入的 auth_token，跳过登录")
    else:
        if not cfg["username"] or not cfg["password"]:
            res["reason"] = "缺少账号或密码（config.ini [accounts] / HOHAI_ACCOUNTS / HOHAI_USERNAME）"
            log(res["reason"])
            return res
        try:
            token = api_login(sess, cfg)
        except Exception as exc:
            log(str(exc))
            res["reason"] = f"登录异常：{exc}"
            return res

    st = api_status(sess, cfg, token)
    log(f"签到状态：{json.dumps(st, ensure_ascii=False)[:300]}")

    if st.get("hasCheckedInToday") and not args.force:
        res.update(code=0, ok=True, already=True,
                   amount=st.get("amount"), currency=st.get("currencyUnit"),
                   balance=st.get("balance"))
        log("今日已签到")
        return res
    if args.status:
        res.update(code=0, ok=st.get("hasCheckedInToday"), reason="仅查询模式")
        log("仅查询模式，结束")
        return res

    result = browser_checkin(cfg, token)

    if not result.get("ok") and result.get("turnstile_token"):
        log("改用 API 直接提交 ...")
        code, body = api_checkin(sess, cfg, token, result["turnstile_token"])
        log(f"API 提交结果 HTTP {code}: {json.dumps(body, ensure_ascii=False)[:300]}")
        if code == 200 and body.get("success"):
            result.update(ok=True, amount=body.get("amount"), currency=body.get("currencyUnit"))
        elif code == 400 and body.get("hasCheckedIn"):
            result.update(ok=True, already=True, amount=body.get("amount"))

    st2: dict = {}
    if result.get("ok"):
        time.sleep(2)
        st2 = api_status(sess, cfg, token)
        log(f"复核签到状态：{json.dumps(st2, ensure_ascii=False)[:300]}")
        if result.get("already"):
            # 浏览器侧判定已签到：用 status 接口补全今日奖励与余额
            res.update(code=0, ok=True, already=True,
                       amount=result.get("amount") or st2.get("amount"),
                       currency=result.get("currency") or st2.get("currencyUnit"),
                       balance=st2.get("balance"))
            return res
        if not st2.get("hasCheckedInToday"):
            result.update(ok=False, reason="响应看似成功，但复核仍是未签到")
        else:
            result["amount"] = st2.get("amount", result.get("amount"))
            result["currency"] = st2.get("currencyUnit", result.get("currency"))

    if result.get("ok"):
        res.update(code=0, ok=True, already=bool(result.get("already")),
                   amount=result.get("amount"), currency=result.get("currency"),
                   balance=st2.get("balance") or result.get("balance"))
        if not result.get("already"):
            log(f"签到成功：+{result.get('amount')} {result.get('currency')}")
        return res

    res["reason"] = result.get("reason") or "未知原因"
    log(f"签到失败：{res['reason']}")
    return res


def _acc_text(res: dict) -> str:
    """把单账号结果拼成一行可读文本。"""
    if res.get("ok") and res.get("already"):
        parts = ["今日已签到"]
        if res.get("amount") is not None:
            parts.append(f"今日奖励 +{res.get('amount')} {res.get('currency') or ''}".rstrip())
        if res.get("balance") is not None:
            parts.append(f"当前余额 {res.get('balance')} {res.get('currency') or ''}".rstrip())
        return "，".join(parts)
    if res.get("ok"):
        line = f"签到成功，奖励 +{res.get('amount')} {res.get('currency') or ''}".rstrip()
        if res.get("balance") is not None:
            line += f"，当前余额 {res['balance']} {res.get('currency') or ''}".rstrip()
        return line
    return f"失败：{res.get('reason') or '未知原因'}"


# ---------------------------------------------------------------- 主流程

def main() -> int:
    ap = argparse.ArgumentParser(description="HOHAI 每日签到（模拟点击，多账号）")
    ap.add_argument("--status", action="store_true", help="只查询签到状态，不签到")
    ap.add_argument("--headed", action="store_true", help="强制显示浏览器窗口")
    ap.add_argument("--smoke", action="store_true", help="只测试浏览器能否启动")
    ap.add_argument("--token", default="", help="直接使用已有 auth_token（跳过账号密码登录，调试用）")
    ap.add_argument("--force", action="store_true", help="即使今日已签到也打开浏览器走一遍（调试用）")
    ap.add_argument("--account", default="", help="只跑指定账号：标签、用户名或序号（1 起）")
    ap.add_argument("--list", action="store_true", help="列出已配置的账号后退出")
    args = ap.parse_args()

    cfg = load_config()
    accounts = load_accounts(cfg)

    if args.list:
        log(f"共配置 {len(accounts)} 个账号：")
        for i, acc in enumerate(accounts):
            extra = f"（覆盖：{', '.join(sorted(acc['overrides']))}）" if acc["overrides"] else ""
            log(f"  {i + 1}. [{acc['label']}] {acc['username']}{extra}")
        return 0

    if args.account:
        key = args.account.strip()
        picked = None
        if key.isdigit() and 1 <= int(key) <= len(accounts):
            picked = [accounts[int(key) - 1]]
        if picked is None:
            picked = [a for a in accounts if a["label"] == key or a["username"] == key]
        if not picked:
            log(f"找不到账号：{key}（可用 --list 查看）")
            return 2
        accounts = picked

    LOG_DIR.mkdir(parents=True, exist_ok=True)
    STATE["logfile"] = LOG_DIR / f"checkin_{datetime.now():%Y%m%d}.log"

    log(f"=== HOHAI 签到开始（引擎 {ENGINE or '无'}，账号 {len(accounts)} 个）===")

    if args.smoke:
        if sync_playwright is None:
            log("未安装 patchright/playwright")
            return 1
        with sync_playwright() as p:
            try:
                b = p.chromium.launch(channel="chrome", headless=False)
                pg = b.new_context().new_page()
                pg.goto(cfg["base_url"] + "/login", wait_until="domcontentloaded", timeout=60000)
                log(f"浏览器可用，页面标题：{pg.title()}")
                b.close()
                return 0
            except Exception as exc:
                log(f"浏览器启动失败：{exc}")
                return 1

    if not accounts:
        log("未配置任何账号：请在 config.ini [accounts] 段、[account.*] 段或环境变量 HOHAI_ACCOUNTS 中配置")
        return 2

    delay_raw = str(cfg.get("account_delay") or "").strip()
    summary: list[tuple[str, dict]] = []
    t0 = time.time()

    for i, acc in enumerate(accounts):
        cfg_i = dict(cfg)
        cfg_i.update(acc["overrides"])
        cfg_i["username"] = acc["username"]
        cfg_i["password"] = acc["password"]
        if args.headed:
            cfg_i["headless"] = "false"
        STATE["account"] = acc["label"]
        STATE["last_shot"] = None

        log(f"─── 账号 {i + 1}/{len(accounts)}：{acc['label']} ───")
        res = run_account(cfg_i, args)
        summary.append((acc["label"], res))

        # 每个账号单独推一条（带账号前缀 + 各自截图）
        body = _acc_text(res)
        notify(cfg_i, f"[{acc['label']}] HOHAI {body}\n时间：{datetime.now():%Y-%m-%d %H:%M:%S}",
               photo=STATE.get("last_shot"))

        if i < len(accounts) - 1:
            try:
                delay = float(delay_raw) if delay_raw else random.uniform(8, 20)
            except ValueError:
                delay = random.uniform(8, 20)
            log(f"等待 {delay:.0f} 秒后处理下一个账号 ...")
            time.sleep(delay)

    STATE["account"] = None

    # 汇总日志 + TG 汇总消息
    ok_n = sum(1 for _, r in summary if r.get("code") == 0)
    lines = [f"{'✓' if r.get('code') == 0 else '✗'} {label}：{_acc_text(r)}" for label, r in summary]
    head = f"HOHAI 多账号签到汇总：{ok_n}/{len(summary)} 成功（用时 {time.time() - t0:.0f} 秒）"
    log(head)
    for line in lines:
        log("  " + line)
    if len(summary) > 1:
        notify(cfg, head + "\n" + "\n".join(lines) + f"\n时间：{datetime.now():%Y-%m-%d %H:%M:%S}")

    return 0 if ok_n == len(summary) else 1


if __name__ == "__main__":
    sys.exit(main())
