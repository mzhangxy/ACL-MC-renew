#!/usr/bin/env python3
"""
ACLClouds MC账号 专用续期脚本 (优化版)
- 支持 Cookie (remember_web_...) 优先登录，失效退回账密登录
- 代理按需自动挂载
"""

import os
import re
import sys
import json
import time
import traceback
from urllib.request import Request, urlopen

# ── 环境变量配置 ──────────────────────────────────────────
PROXY_SERVER = os.environ.get("MC_PROXY", "").strip()
EMAIL        = os.environ.get("MC_EMAIL", "").strip()
PASSWORD     = os.environ.get("MC_PASSWORD", "").strip()
MC_COOKIE    = os.environ.get("MC_COOKIE", "").strip()

TG_BOT_TOKEN = os.environ.get("TG_BOT_TOKEN", "").strip()
TG_CHAT_ID   = os.environ.get("TG_CHAT_ID", "").strip()

RENEW_THRESHOLD_DAYS = 48 / 24   # 剩余 < 2天 续期

BASE_URL  = "https://dash.aclclouds.com"
LOGIN_URL = f"{BASE_URL}/auth/login"

# ── 日志与脱敏 ───────────────────────────────────────────
def log(msg):       print(f"[INFO] {msg}", flush=True)
def log_warn(msg):  print(f"[WARN] {msg}", flush=True)
def log_error(msg): print(f"[ERROR] {msg}", flush=True)

def mask_email(email: str) -> str:
    if not email or "@" not in email: return "***"
    local, domain = email.split("@", 1)
    return f"{local[0]}**@{domain[0]}***" if len(local)>1 else f"**@{domain[0]}***"

def mask_ip(ip: str) -> str:
    parts = ip.strip().split(".")
    return f"{parts[0]}.{parts[1]}.*.*" if len(parts) == 4 else "***"

def get_current_ip(proxy: str) -> str:
    try:
        import subprocess
        cmd = ["curl", "-s", "--max-time", "5", "ifconfig.me"]
        if proxy:
            cmd = ["curl", "-s", "--max-time", "5", "--proxy", proxy, "ifconfig.me"]
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
        return mask_ip(r.stdout.strip()) if r.returncode == 0 else "获取失败"
    except Exception as e:
        return f"获取异常({e})"

# ── 推送 (仅 TG) ──────────────────────────────────────────
def send_tg(text: str):
    if not TG_BOT_TOKEN or not TG_CHAT_ID: return
    try:
        body = json.dumps({"chat_id": TG_CHAT_ID, "text": text, "parse_mode": "HTML"}).encode()
        req  = Request(f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendMessage",
                       data=body, headers={"Content-Type": "application/json"})
        urlopen(req, timeout=15)
        log("TG 推送成功")
    except Exception as e:
        log_warn(f"TG 推送失败: {e}")

# ── 解析时间 ──────────────────────────────────────────────
def parse_expires(text):
    if text is None: return None
    s = str(text).strip()
    if re.search(r'\d{4}-\d{2}-\d{2}', s):
        try:
            from datetime import datetime, timezone
            dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
            return (dt - datetime.now(timezone.utc)).total_seconds() / 86400
        except: pass
    try: return float(s) / 86400
    except: pass
    
    sl, days, hours, minutes = s.lower(), 0.0, 0.0, 0.0
    m = re.search(r'(\d+(?:\.\d+)?)\s*[dj]', sl)
    if m: days = float(m.group(1))
    m = re.search(r'(\d+(?:\.\d+)?)\s*h', sl)
    if m: hours = float(m.group(1))
    m = re.search(r'(\d+(?:\.\d+)?)\s*m(?!o)', sl)
    if m: minutes = float(m.group(1))
    
    total = days + hours / 24 + minutes / 1440
    return total if total > 0 else None

def fmt_remaining(days: float) -> str:
    h, m = divmod(int(days * 24 * 60), 60)
    return f"{h}h {m}min" if m else f"{h}h"

# ── 面板接口 ──────────────────────────────────────────────
def fetch_api(page, endpoint: str, method="GET", body=None):
    """通用接口调用封装，处理 XSRF 逻辑"""
    script = f"""async () => {{
        const xsrf = decodeURIComponent(
            document.cookie.split('; ').find(c => c.startsWith('XSRF-TOKEN='))?.split('=')[1] || ''
        );
        const opts = {{
            method: '{method}',
            headers: {{'Accept': 'application/json', 'X-XSRF-TOKEN': xsrf}}
        }};
        if ({'true' if body else 'false'}) {{
            opts.headers['Content-Type'] = 'application/json';
            opts.body = JSON.stringify({json.dumps(body) if body else 'null'});
        }}
        const r = await fetch('{endpoint}', opts);
        return {{status: r.status, body: await r.text()}};
    }}"""
    return page.evaluate(script)

def check_server_online(page, identifier: str):
    res = fetch_api(page, f"/api/client/servers/{identifier}/resources")
    if res['status'] != 200:
        res2 = fetch_api(page, f"/api/client/servers/{identifier}")
        if res2['status'] != 200: return None
        return False if json.loads(res2['body']).get('attributes', {}).get('suspended', False) else None
    
    attrs = json.loads(res['body']).get('attributes', {})
    state = attrs.get('current_state', 'unknown')
    if attrs.get('is_suspended', False): return False
    if state in ('running', 'starting'): return True
    if state in ('offline', 'stopping', 'stopped'): return False
    return None

def start_server(page, identifier: str) -> bool:
    res = fetch_api(page, f"/api/client/servers/{identifier}/power", "POST", {"signal": "start"})
    return res['status'] in (200, 204)

def wait_until_running(page, identifier: str, max_wait: int = 120, interval: int = 10) -> bool:
    for elapsed in range(0, max_wait, interval):
        time.sleep(interval)
        res = fetch_api(page, f"/api/client/servers/{identifier}/resources")
        state = json.loads(res['body']).get('attributes', {}).get('current_state', 'unknown') if res['status'] == 200 else 'unknown'
        log(f"  等待启动中... {elapsed+interval}s / {max_wait}s，当前状态: {state!r}")
        if state == 'running': return True
    return False

# ── 主流程 ────────────────────────────────────────────────
def run():
    from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

    log(f"账号: {mask_email(EMAIL)}")
    log(f"当前 IP: {get_current_ip(PROXY_SERVER)}")
    log(f"续期阈值: < {RENEW_THRESHOLD_DAYS*24:.1f} 小时")

    renewed_list, offline_list, skipped_list, failed_list = [], [], [], []

    with sync_playwright() as p:
        launch_args = {"args": ["--no-sandbox", "--disable-setuid-sandbox"]}
        if PROXY_SERVER:
            launch_args["proxy"] = {"server": PROXY_SERVER}
            log(f"已启用代理: {PROXY_SERVER}")
            
        browser = p.chromium.launch(**launch_args)
        ctx = browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36",
            locale="zh-CN"
        )

        # ── 注入 Cookie ──────────────────────────────────────
        if MC_COOKIE:
            try:
                cookies = json.loads(MC_COOKIE)
                if isinstance(cookies, dict): cookies = [cookies]
                ctx.add_cookies(cookies)
                log("✅ 成功加载 Cookie (JSON 格式)")
            except json.JSONDecodeError:
                # 兼容原生请求头 Cookie 字符串复制
                cookie_list = []
                for pair in MC_COOKIE.split(';'):
                    if '=' in pair:
                        k, v = pair.split('=', 1)
                        cookie_list.append({"name": k.strip(), "value": v.strip(), "domain": "dash.aclclouds.com", "path": "/"})
                if cookie_list:
                    ctx.add_cookies(cookie_list)
                    log("✅ 成功解析并加载 Cookie 字符串")

        page = ctx.new_page()

        try:
            # ── 1. 尝试 Cookie 直接登录 ────────────────────────
            login_success = False
            if MC_COOKIE:
                log("尝试使用 Cookie 访问仪表盘...")
                page.goto(BASE_URL, timeout=60000)
                page.wait_for_load_state("networkidle", timeout=30000)
                if "login" not in page.url:
                    log(f"✅ Cookie 登录成功! URL: {page.url}")
                    login_success = True
                else:
                    log_warn("⚠️ Cookie 已失效或不完整，降级为账密登录...")

            # ── 2. 账密登录兜底 ────────────────────────────────
            if not login_success:
                log(f"账密登录: {LOGIN_URL}")
                page.goto(LOGIN_URL, timeout=60000)
                page.wait_for_load_state("networkidle", timeout=30000)

                page.fill("input[type='email'], input[name='email'], input:first-of-type", EMAIL)
                page.fill("input[type='password'], input[name='password']", PASSWORD)

                # Captcha
                CAPTCHA_SEL = "div.auth-captcha-box.verified, div.auth-captcha-inner[aria-checked='true'], :text('Verified'), :text('verified')"
                captcha_ok = False
                for attempt in range(1, 4):
                    try: page.click("div.auth-captcha-inner", timeout=5000)
                    except: pass
                    try:
                        page.wait_for_selector(CAPTCHA_SEL, timeout=15000)
                        time.sleep(0.5)
                        log("captcha 验证通过 ✅")
                        captcha_ok = True
                        break
                    except:
                        log_warn(f"captcha 第 {attempt} 次等待超时")
                
                if not captcha_ok:
                    raise RuntimeError("captcha 验证失败，放弃登录")

                # 提交
                page.click("button[type='submit'], button:has-text('Login'), button:has-text('登录')")
                try:
                    page.wait_for_url(lambda url: "login" not in url, timeout=20000)
                    log(f"账密登录成功 ✅")
                except PWTimeout:
                    raise RuntimeError("登录提交超时")

            # ── 3. 等待 Dashboard 渲染 ────────────────────────
            try:
                page.wait_for_selector("a:has-text('My services'), nav a[href*='server'], .sidebar", timeout=30000)
            except:
                time.sleep(5)

            # ── 4. 获取项目 ──────────────────────────────────
            res = None
            for _ in range(3):
                res = fetch_api(page, "/api/client")
                if res['status'] == 200: break
                time.sleep(5)

            if res['status'] != 200:
                log_warn("面板接口异常，视为无项目")
                send_tg(f"⚠️ <b>ACLClouds</b>\n\n/api/client 异常 (HTTP {res['status']})")
                return renewed_list, offline_list, skipped_list, failed_list

            projects = [i['attributes'] for i in json.loads(res['body']).get('data', []) if i.get('attributes')]
            log(f"找到 {len(projects)} 个项目")

            # ── 5. 遍历处理 ──────────────────────────────────
            for project in projects:
                name, identifier = project.get("name", "未知"), project.get("identifier", "")
                remaining = parse_expires(project.get("expires_at"))
                log(f"\n── 项目: {name} ──")

                # 离线处理
                online = check_server_online(page, identifier)
                if online is False:
                    log_warn("  ❌ 服务离线，尝试启动...")
                    if start_server(page, identifier):
                        if wait_until_running(page, identifier): log("  ✅ 成功启动！")
                        else: offline_list.append(name)
                    else: offline_list.append(name)
                elif online is True: log("  ✅ 服务在线")
                
                if remaining is None:
                    failed_list.append(f"{name} (解析时间失败)")
                    continue

                remaining_str = fmt_remaining(remaining)
                log(f"  剩余: {remaining_str}")

                if remaining >= RENEW_THRESHOLD_DAYS:
                    skipped_list.append(f"{name} ({remaining_str})")
                    continue

                # 续期逻辑
                log("  尝试续期...")
                renew_res = fetch_api(page, f"/api/client/servers/{identifier}/upgrade/renew", "POST")
                if renew_res['status'] == 200:
                    renewed_list.append(f"{name} (续期前: {remaining_str})")
                    log("  续期成功 ✅")
                else:
                    err = json.loads(renew_res['body']).get('error', renew_res['body'][:80])
                    failed_list.append(f"{name} ({err})")

        except Exception as e:
            send_tg(f"❌ <b>ACLClouds 脚本异常</b>\n\n<code>{str(e)[:200]}</code>")
            raise
        finally:
            ctx.close()
            browser.close()

    return renewed_list, offline_list, skipped_list, failed_list

if __name__ == "__main__":
    if not EMAIL or not PASSWORD:
        log_error("缺少 MC_EMAIL 或 MC_PASSWORD")
        sys.exit(1)

    try:
        renewed_list, offline_list, skipped_list, failed_list = run()
    except Exception:
        traceback.print_exc()
        sys.exit(1)

    # 结果推送
    if offline_list:
        send_tg("🚨 <b>ACLClouds 服务离线</b>\n" + "\n".join(f"• {n}" for n in offline_list))

    if renewed_list or failed_list:
        lines = []
        if renewed_list: lines += ["✅ <b>ACLClouds 续期成功</b>"] + [f"• {i}" for i in renewed_list]
        if failed_list: lines += ["\n❌ <b>ACLClouds 失败项目</b>"] + [f"• {i}" for i in failed_list]
        if skipped_list: lines += ["\n⏳ <b>ACLClouds 未到窗口</b>"] + [f"• {i}" for i in skipped_list]
        send_tg("\n".join(lines))
