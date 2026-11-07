#!/usr/bin/env python3
# -*- coding: utf-8 -*-

from __future__ import annotations

import argparse
import configparser
import hashlib
import json
import os
import random
import re
import sys
import time
from datetime import datetime
from pathlib import Path

try:
    import requests
except ImportError:
    sys.exit("缺少依赖：pip install requests")

ROOT = Path(__file__).resolve().parent
LOG_DIR = ROOT / "logs"

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

# 每个账号允许覆盖的全局配置键
ACCOUNT_OVERRIDE_KEYS = ("tg_bot_token", "tg_chat_id", "tg_proxy")

STATE = {"logfile": None, "account": None}


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


def _mask_account(u: str) -> str:
    """账号打码：a***@q***#3f2a —— 同账号恒定、不同账号可区分、认不出是谁。"""
    u = (u or "").strip()
    if not u:
        return "***"
    h = hashlib.md5(u.encode("utf-8")).hexdigest()[:4]
    if "@" in u:
        local, _, dom = u.partition("@")
        lp = (local[:1] + "***") if local else "***"
        dp = (dom[:1] + "***") if dom else "***"
        return f"{lp}@{dp}#{h}"
    return f"{u[:1]}***#{h}"


def _display_label(label: str, username: str) -> str:
    label = (label or "").strip()
    if not label or label == username or "@" in label:
        return _mask_account(username)
    return label


# ---------------------------------------------------------------- 配置

def load_config() -> dict:
    cfg = {
        "base_url": "https://eoos.top",
        "username": "",
        "password": "",
        "timeout": "60",
        "tg_bot_token": "",
        "tg_chat_id": "",
        "tg_proxy": "",
        "account_delay": "",
    }
    ini = ROOT / "config.ini"
    hohai_cfg: dict = {}
    if ini.exists():
        cp = configparser.ConfigParser()
        cp.read(ini, encoding="utf-8")
        if cp.has_section("eoos"):
            for k, v in cp.items("eoos"):
                cfg[k.strip()] = v.strip()
        # TG 复用 hohai 的 bot：[eoos] 对应项留空时回落到 [hohai] 段同名值
        if cp.has_section("hohai"):
            for k, v in cp.items("hohai"):
                hohai_cfg[k.strip()] = v.strip()

    env_map = {
        "EOOS_BASE_URL": "base_url",
        "EOOS_USERNAME": "username",
        "EOOS_PASSWORD": "password",
        "EOOS_TIMEOUT": "timeout",
        "EOOS_TG_BOT_TOKEN": "tg_bot_token",
        "EOOS_TG_CHAT_ID": "tg_chat_id",
        "EOOS_TG_PROXY": "tg_proxy",
        "EOOS_ACCOUNT_DELAY": "account_delay",
        "EMAIL": "username",
        "PASSWORD": "password",
        "TG_BOT_TOKEN": "tg_bot_token",
        "TG_CHAT_ID": "tg_chat_id",
        "TG_PROXY": "tg_proxy",
    }
    for env, key in env_map.items():
        val = os.environ.get(env)
        if val:
            cfg[key] = val.strip()

    for key in ("tg_bot_token", "tg_chat_id", "tg_proxy"):
        if not cfg.get(key) and hohai_cfg.get(key):
            cfg[key] = hohai_cfg[key]
    return cfg


def _split_user_pass(raw: str) -> tuple[str, str]:
    raw = (raw or "").strip()
    if ":" in raw:
        u, p = raw.split(":", 1)
        return u.strip(), p.strip()
    return raw, ""


def load_accounts(cfg: dict) -> list[dict]:
    entries: list[dict] = []
    seen: set[str] = set()

    def add(label: str, username: str, password: str, overrides: dict | None = None) -> None:
        username = (username or "").strip()
        if not username:
            return
        if username in seen:
            log(f"账号 {_mask_account(username)} 重复配置，忽略后出现的")
            return
        seen.add(username)
        entries.append({
            "label": _display_label(label, username),
            "username": username,
            "password": (password or "").strip(),
            "overrides": overrides or {},
        })

    ini = ROOT / "config.ini"
    cp = configparser.ConfigParser()
    if ini.exists():
        cp.read(ini, encoding="utf-8")

    # 1) 环境变量 EOOS_ACCOUNTS：`user:pass` 用 ; 或换行分隔（优先级最高）
    env_accounts = os.environ.get("EOOS_ACCOUNTS", "").strip()
    if env_accounts:
        for item in re.split(r"[;\r\n]+", env_accounts):
            item = item.strip()
            if not item:
                continue
            u, p = _split_user_pass(item)
            add(u, u, p)

    # 2) [eoos.accounts] 段：`标签 = 用户名:密码`
    if cp.has_section("eoos.accounts"):
        for key, val in cp.items("eoos.accounts"):
            u, p = _split_user_pass(val)
            add(key.strip(), u, p)

    for sec in cp.sections():
        m = re.match(r"^eoos\.account\.(.+)$", sec, re.IGNORECASE)
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
            target["label"] = _display_label(label, username)
            log(f"账号 {_mask_account(username)} 的覆盖配置已合并（标签 {target['label']}）")
        else:
            add(label, username, password, overrides)

    if not entries and cfg.get("username"):
        add("default", cfg["username"], cfg["password"])

    return entries


def truthy(val) -> bool:
    return str(val).strip().lower() in ("1", "true", "yes", "on")


# ---------------------------------------------------------------- HTTP

def make_session(cfg: dict) -> requests.Session:
    s = requests.Session()
    s.trust_env = False  # 无视系统代理，本机 127.0.0.1:10808 会干扰直连站点
    s.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                      "(KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36",
        "Accept": "application/json, text/plain, */*",
        "Origin": cfg["base_url"],
        "Referer": cfg["base_url"] + "/dashboard",
    })
    return s


def api_login(sess: requests.Session, cfg: dict) -> str:
    url = cfg["base_url"].rstrip("/") + "/api/auth/login"
    payload = {"userName": cfg["username"], "password": cfg["password"], "rememberMe": True}
    r = sess.post(url, json=payload, timeout=30)
    if r.status_code == 200:
        data = r.json()
        token = data.get("token")
        if token:
            log(f"登录成功：{_mask_account(data.get('user', {}).get('userName') or cfg['username'])}")
            return token
        raise RuntimeError(f"登录响应缺少 token: {json.dumps(data, ensure_ascii=False)[:200]}")
    # 登录接口要求验证码时先解题再重试
    detail = r.text[:200].lower()
    if "captcha" in detail or "verif" in detail or r.status_code in (400, 403):
        vt = cap_solve(sess, cfg)
        payload["verificationToken"] = vt
        r2 = sess.post(url, json=payload, timeout=30)
        if r2.status_code == 200:
            data = r2.json()
            token = data.get("token")
            if token:
                log(f"登录成功（带验证码）：{_mask_account(cfg['username'])}")
                return token
        raise RuntimeError(f"登录失败（带验证码重试）HTTP {r2.status_code}: {r2.text[:200]}")
    raise RuntimeError(f"登录失败 HTTP {r.status_code}: {r.text[:200]}")


def api_status(sess: requests.Session, cfg: dict, token: str) -> dict:
    url = cfg["base_url"].rstrip("/") + "/api/checkin/status"
    r = sess.get(url, headers={"Authorization": f"Bearer {token}"}, timeout=30)
    try:
        return r.json() if r.content else {}
    except Exception:
        return {"_status": r.status_code, "_raw": r.text[:200]}


def api_checkin(sess: requests.Session, cfg: dict, token: str, vt: str) -> tuple[int, dict]:
    url = cfg["base_url"].rstrip("/") + "/api/checkin"
    r = sess.post(url, json={"verificationToken": vt},
                  headers={"Authorization": f"Bearer {token}"}, timeout=30)
    try:
        body = r.json()
    except Exception:
        body = {"_raw": r.text[:200]}
    return r.status_code, body


# ---------------------------------------------------------------- Cap PoW 求解器
# 对齐官方 @cap.js/widget：盐/目标由 challenge token 派生（FNV-1a 种子 + xorshift32）

def _cap_prng_string(seed: str, length: int) -> str:
    t = 2166136261  # FNV-1a offset basis
    for ch in seed:
        t = ((t ^ ord(ch)) * 16777619) & 0xFFFFFFFF  # FNV prime
    s = ""
    while len(s) < length:
        t ^= (t << 13) & 0xFFFFFFFF
        t ^= t >> 17
        t ^= (t << 5) & 0xFFFFFFFF
        t &= 0xFFFFFFFF
        s += format(t, "08x")
    return s[:length]


def cap_solve(sess: requests.Session, cfg: dict, retries: int = 3) -> str:
    """解 Cap PoW 拿 verificationToken。实测 18 题 x 难度4 约一秒内解完。"""
    base = cfg["base_url"].rstrip("/") + "/api/cap/"
    last_err = "未知错误"
    for attempt in range(1, retries + 1):
        try:
            r = sess.post(base + "challenge", json={}, timeout=30)
            r.raise_for_status()
            ch = r.json()
            meta = ch.get("challenge") or {}
            count, salt_len, diff = int(meta.get("c", 18)), int(meta.get("s", 32)), int(meta.get("d", 4))
            ctok = ch["token"]

            solutions = []
            for i in range(1, count + 1):
                salt = _cap_prng_string(f"{ctok}{i}", salt_len)
                target = _cap_prng_string(f"{ctok}{i}d", diff)
                n = 0
                while True:
                    if hashlib.sha256((salt + str(n)).encode()).hexdigest().startswith(target):
                        solutions.append(n)
                        break
                    n += 1

            r2 = sess.post(base + "redeem",
                           json={"token": ctok, "solutions": solutions}, timeout=30)
            r2.raise_for_status()
            red = r2.json()
            if red.get("success") and red.get("token"):
                if attempt > 1:
                    log(f"Cap 解题成功（第 {attempt} 次）：{count} 题")
                return red["token"]
            last_err = red.get("error") or "redeem 未返回 token"
        except Exception as exc:
            last_err = str(exc)
        log(f"Cap 第 {attempt}/{retries} 次求解未果：{last_err}，重试 …")
        time.sleep(1.5)
    raise RuntimeError(f"Cap 验证码求解失败：{last_err}")


# ---------------------------------------------------------------- 通知

def notify(cfg: dict, text: str) -> None:
    tok, chat = cfg.get("tg_bot_token"), cfg.get("tg_chat_id")
    if not tok or not chat:
        return
    proxy = (cfg.get("tg_proxy") or "").strip()
    sess = requests.Session()
    sess.trust_env = False
    if proxy:
        sess.proxies = {"http": proxy, "https": proxy}
    try:
        r = sess.post(f"https://api.telegram.org/bot{tok}/sendMessage",
                      json={"chat_id": chat, "text": text, "disable_web_page_preview": True},
                      timeout=20)
        log("Telegram 已发送" if r.status_code == 200 else f"Telegram 发送失败：{r.text[:150]}")
    except Exception as exc:
        log(f"Telegram 异常：{exc}")


# ---------------------------------------------------------------- 当日结果缓存
# 奖励/余额只在签到 POST 响应里带一次，/api/checkin/status 已签状态下不返回这些值。
# 当天实签成功就把数值落盘，同日重跑时「今日已签到」的账号从缓存回显完整通知。

def _state_file() -> Path:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    return LOG_DIR / f"state_eoos_{datetime.now():%Y%m%d}.json"


def state_record(username: str, res: dict) -> None:
    try:
        if res.get("amount") is None:
            return
        f = _state_file()
        try:
            store = json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            store = {}
        store[hashlib.md5(username.encode("utf-8")).hexdigest()] = {
            "label": _mask_account(username),
            "amount": res.get("amount"), "currency": res.get("currency"),
            "balance": res.get("balance"),
        }
        f.write_text(json.dumps(store, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass


def state_fill(username: str, res: dict) -> None:
    """已签到且本次没拿到奖励数值时，用当日缓存补全 amount/currency/balance。"""
    if not (res.get("ok") and res.get("already") and res.get("amount") is None):
        return
    try:
        store = json.loads(_state_file().read_text(encoding="utf-8"))
        rec = store.get(hashlib.md5(username.encode("utf-8")).hexdigest()) or {}
        for k in ("amount", "currency", "balance"):
            if rec.get(k) is not None:
                res[k] = rec[k]
    except Exception:
        pass


# ---------------------------------------------------------------- 单账号流程

def run_account(cfg: dict, args) -> dict:
    res = {"code": 1, "ok": False, "already": False, "amount": None, "currency": None,
           "balance": None, "reason": ""}

    sess = make_session(cfg)
    if not cfg["username"] or not cfg["password"]:
        res["reason"] = "缺少账号或密码（config.ini [eoos.accounts] / EOOS_ACCOUNTS / EOOS_USERNAME）"
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

    if st.get("hasCheckedInToday"):
        res.update(code=0, ok=True, already=True,
                   amount=st.get("amount"), currency=st.get("currencyUnit"))
        state_fill(cfg["username"], res)
        log("今日已签到")
        return res
    if args.status:
        res.update(code=0, ok=st.get("hasCheckedInToday"), reason="仅查询模式")
        log("仅查询模式，结束")
        return res

    log("解题领 verificationToken ...")
    try:
        vt = cap_solve(sess, cfg)
    except Exception as exc:
        res["reason"] = f"验证码求解失败：{exc}"
        log(res["reason"])
        return res

    code, body = api_checkin(sess, cfg, token, vt)
    log(f"签到提交 HTTP {code}: {json.dumps(body, ensure_ascii=False)[:300]}")
    if code == 200 and body.get("success"):
        res.update(code=0, ok=True, amount=body.get("amount"), currency=body.get("currencyUnit"))
        if body.get("balance") is not None:
            res["balance"] = body.get("balance")
    elif code == 400 and body.get("hasCheckedIn"):
        res.update(code=0, ok=True, already=True, amount=body.get("amount"))
        if body.get("balance") is not None:
            res["balance"] = body.get("balance")
    else:
        res["reason"] = f"签到被拒 HTTP {code}: {body.get('message') or body.get('error') or str(body)[:120]}"
        log(res["reason"])
        return res

    # 复核：确认签到真的生效
    if not res.get("already"):
        time.sleep(2)
        st2 = api_status(sess, cfg, token)
        log(f"复核签到状态：{json.dumps(st2, ensure_ascii=False)[:300]}")
        if not st2.get("hasCheckedInToday"):
            res.update(code=1, ok=False, reason="响应看似成功，但复核仍是未签到")
        else:
            res["amount"] = st2.get("amount", res.get("amount"))
            res["currency"] = st2.get("currencyUnit", res.get("currency"))
    if res.get("ok"):
        # 当日缓存：实签成功的直接记录；已签但带出数值的（400 响应）也记录，供后续重跑回显
        state_record(cfg["username"], res)
        state_fill(cfg["username"], res)
    return res


def _acc_text(res: dict) -> str:
    if res.get("ok") and res.get("already"):
        parts = ["今日已签到"]
        if res.get("amount") is not None:
            parts.append(f"今日奖励 +{res.get('amount')} {res.get('currency') or ''}".rstrip())
        return "，".join(parts)
    if res.get("ok"):
        line = f"签到成功，奖励 +{res.get('amount')} {res.get('currency') or ''}".rstrip()
        if res.get("balance") is not None:
            line += f"，当前余额 {res['balance']} {res.get('currency') or ''}".rstrip()
        return line
    return f"失败：{res.get('reason') or '未知原因'}"


# ---------------------------------------------------------------- 主流程

def main() -> int:
    ap = argparse.ArgumentParser(description="EOOS 每日签到（纯 API 版，多账号）")
    ap.add_argument("--status", action="store_true", help="只查询签到状态，不签到")
    ap.add_argument("--token", default="", help="直接使用已有 auth_token（跳过登录）")
    ap.add_argument("--account", default="", help="只跑指定账号：标签、用户名或序号（1 起）")
    ap.add_argument("--list", action="store_true", help="列出已配置的账号后退出")
    args = ap.parse_args()

    cfg = load_config()
    accounts = load_accounts(cfg)

    if args.list:
        log(f"共配置 {len(accounts)} 个账号：")
        for i, acc in enumerate(accounts):
            extra = f"（覆盖：{', '.join(sorted(acc['overrides']))}）" if acc["overrides"] else ""
            log(f"  {i + 1}. [{acc['label']}] {_mask_account(acc['username'])}{extra}")
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
    STATE["logfile"] = LOG_DIR / f"eoos_checkin_{datetime.now():%Y%m%d}.log"

    log(f"=== EOOS 签到开始（纯 API 模式，账号 {len(accounts)} 个）===")

    if not accounts:
        log("未配置任何账号：请在 config.ini [eoos.accounts] 段、[eoos.account.*] 段或环境变量 EOOS_ACCOUNTS 中配置")
        return 2

    delay_raw = str(cfg.get("account_delay") or "").strip()
    summary: list[tuple[str, dict]] = []
    t0 = time.time()

    for i, acc in enumerate(accounts):
        cfg_i = dict(cfg)
        cfg_i.update(acc["overrides"])
        cfg_i["username"] = acc["username"]
        cfg_i["password"] = acc["password"]
        STATE["account"] = acc["label"]

        log(f"─── 账号 {i + 1}/{len(accounts)}：{acc['label']} ───")
        res = run_account(cfg_i, args)
        summary.append((acc["label"], res))

        if res.get("ok") and res.get("already") and truthy(os.environ.get("EOOS_QUIET_ALREADY", "")):
            log("已签到且设置了 EOOS_QUIET_ALREADY，跳过该账号通知")
        else:
            notify(cfg_i, f"[{acc['label']}] EOOS {_acc_text(res)}\n时间：{datetime.now():%Y-%m-%d %H:%M:%S}")

        if i < len(accounts) - 1:
            try:
                delay = float(delay_raw) if delay_raw else random.uniform(8, 20)
            except ValueError:
                delay = random.uniform(8, 20)
            log(f"等待 {delay:.0f} 秒后处理下一个账号 ...")
            time.sleep(delay)

    STATE["account"] = None

    ok_n = sum(1 for _, r in summary if r.get("code") == 0)
    lines = [f"{'✓' if r.get('code') == 0 else '✗'} {label}：{_acc_text(r)}" for label, r in summary]
    head = f"EOOS 多账号签到汇总：{ok_n}/{len(summary)} 成功（用时 {time.time() - t0:.0f} 秒）"
    log(head)
    for line in lines:
        log("  " + line)
    # 汇总消息：多账号才发（单账号以逐账号通知为准；已签静音由 QUIET_ALREADY 控制）
    if len(summary) > 1:
        notify(cfg, head + "\n" + "\n".join(lines) + f"\n时间：{datetime.now():%Y-%m-%d %H:%M:%S}")

    return 0 if ok_n == len(summary) else 1


if __name__ == "__main__":
    sys.exit(main())
