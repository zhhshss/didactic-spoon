#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
mambo-hachimi.biliblili.uk 自动签到脚本
========================================

技术栈：SeleniumBase UC Mode（Undetected Chrome）

为什么是 SeleniumBase 而不是普通 Playwright？
  目标站点的【登录】和【签到】都强制开启了 Cloudflare Turnstile v2 人机验证
  （/api/settings/verification/public 里 login 与 checkin 均 enabled=true）。
  普通自动化浏览器（含 Playwright headless）会被 Turnstile 识别并拒绝发放 token，
  实测返回 401 / failure_retry。SeleniumBase 的 UC Mode 用真实 Chrome + 反指纹，
  让浏览器表现得像真人，从而以正常方式通过 managed 模式的人机验证。

核心流程（全部基于对前端 bundle 的逆向）：
  1. 启动持久化 UC 浏览器（user_data_dir 保留登录态 + cf_clearance，降低重复验证）
  2. 检查登录态：localStorage.auth_token + GET /api/auth/me
  3. 未登录 → 打开 /login，填表单 → 过 Turnstile → 前端自动提交 POST /api/auth/login
  4. 查签到状态：GET /api/checkin/status，已签到则幂等退出
  5. 未签到 → 进入签到页点「立即签到」卡片 → 过 checkin 的 Turnstile → 前端 POST /api/checkin
  6. 校验结果并打印获得的奖励

凭据来源：环境变量或同目录 .env 文件（MAMBO_USERNAME / MAMBO_PASSWORD），
         绝不硬编码进代码，.env 已被 .gitignore 忽略。
"""

import os
import sys
import json
import base64
import time
import logging
from pathlib import Path

# ------------------------------------------------------------
# 第三方依赖
# ------------------------------------------------------------
try:
    from seleniumbase import SB
except ImportError:
    sys.exit("缺少依赖 seleniumbase，请先执行：pip install -r requirements.txt")

# .env 加载（可选依赖，缺失则仅用系统环境变量）
try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).resolve().parent / ".env")
except ImportError:
    pass


# ============================================================
# 配置区（均可被环境变量覆盖）
# ============================================================
BASE_URL = os.getenv("MAMBO_BASE_URL", "https://mambo-hachimi.biliblili.uk").rstrip("/")
USERNAME = os.getenv("MAMBO_USERNAME", "")
PASSWORD = os.getenv("MAMBO_PASSWORD", "")

# 可选：直接注入的登录令牌（JWT）。优先级高于 .profile 与本地 token 文件，
# 适合首次 bootstrap 或跨机器复用：本机用 `python checkin.py --export-token`
# 导出后配进 GitHub Secret（MAMBO_AUTH_TOKEN），云端首跑即可免登录。
AUTH_TOKEN = os.getenv("MAMBO_AUTH_TOKEN", "").strip()

# 持久化浏览器数据目录：保留 localStorage(auth_token) 与 cf_clearance，
# 让第二次以后的运行尽量免登录、免验证。
USER_DATA_DIR = os.getenv("MAMBO_PROFILE_DIR", str(Path(__file__).resolve().parent / ".profile"))

# 登录令牌的独立落盘文件（放在持久化目录内，随 .profile 一起被 actions/cache 复用）。
# 为什么需要它：Chromium 的 localStorage(leveldb) 是【异步批量落盘】的，UC 浏览器
# 在签到完成后快速退出时，auth_token 常来不及 flush 进磁盘 —— 于是缓存恢复后读不到，
# 被迫每次重新登录（还会触发登录提醒邮件）。故由脚本在浏览器【仍存活】时主动把 token
# 读出写入此文件，落盘确定、不依赖浏览器退出 flush，下次启动注入即可跳过登录。
TOKEN_FILE = Path(USER_DATA_DIR) / ".mambo_auth_token"

# 显示模式：
#   有桌面环境 → 设 MAMBO_DISPLAY=headed（弹出真实窗口，最稳）
#   无桌面服务器 → 设 MAMBO_DISPLAY=xvfb（虚拟显示，UC 仍可做 GUI 点击）
#   auto（默认）→ 有 $DISPLAY 用 headed，否则用 xvfb
DISPLAY_MODE = os.getenv("MAMBO_DISPLAY", "auto").lower()

# 登录后签到卡片可能所在的路由（站点未提供独立 /checkin 路由，按可能性排序探测）
CHECKIN_ROUTES = ["/dashboard", "/account", "/"]

# 登录成功的最长等待（秒）
LOGIN_TIMEOUT = int(os.getenv("MAMBO_LOGIN_TIMEOUT", "60"))

# ------------------------------------------------------------
# 日志
# ------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("checkin")


# ============================================================
# 浏览器内辅助：同源同步 XHR 调用后端 API（自动带上 Bearer token）
# ============================================================
def api_call(sb, method, path, body=None):
    """在当前站点页面内用同步 XHR 调后端 API，复用登录态。返回解析后的 dict。

    必须在浏览器已停留于本站页面时调用（保证同源 + 能读到 localStorage）。
    """
    js = r"""
        var method = arguments[0], path = arguments[1], body = arguments[2];
        var t = localStorage.getItem('auth_token') || sessionStorage.getItem('auth_token');
        var xhr = new XMLHttpRequest();
        xhr.open(method, path, false);            // 同步请求，简单可靠
        xhr.setRequestHeader('Content-Type', 'application/json');
        if (t) xhr.setRequestHeader('Authorization', 'Bearer ' + t);
        xhr.withCredentials = true;
        try { xhr.send(body ? JSON.stringify(body) : null); }
        catch (e) { return JSON.stringify({__error: String(e)}); }
        return JSON.stringify({__status: xhr.status, __text: xhr.responseText});
    """
    raw = sb.execute_script(js, method, path, body)
    try:
        wrapper = json.loads(raw)
    except Exception:
        return {"__error": "无法解析响应", "__raw": raw}
    if "__error" in wrapper:
        return wrapper
    text = wrapper.get("__text") or ""
    try:
        data = json.loads(text) if text else {}
    except Exception:
        data = {"__text": text}
    data["__status"] = wrapper.get("__status")
    return data


def get_turnstile_token(sb):
    """读取页面上 Turnstile 隐藏域的 token（非空表示验证已通过）。"""
    js = (
        "var e=document.querySelector('input[name=\"cf-turnstile-response\"]');"
        "return e ? e.value : '';"
    )
    try:
        return sb.execute_script(js) or ""
    except Exception:
        return ""


# ============================================================
# Turnstile 处理
# ============================================================
def solve_turnstile(sb, max_clicks=3):
    """尽力通过（弹窗内的）Cloudflare Turnstile。

    重要：本站 Turnstile 在 UC Mode + 持久化 profile 下经常【无感通过】，
    且通过后 token 不一定回填到标准隐藏域。因此本函数只“尽力点击”，
    最终是否成功一律交由调用方的【业务判据】（登录态 / 签到状态）确认，
    不以 token 作为唯一成功标志，避免误判阻断流程。
    """
    if get_turnstile_token(sb):
        log.info("Turnstile 已无感通过（拿到 token）")
        return True

    has_widget = sb.execute_script(
        'return !!document.querySelector(\'[class*="turnstile"]\');'
    )
    if not has_widget:
        log.info("未出现 Turnstile 组件，本次可能无需验证")
        return True

    log.info("出现 Turnstile，尝试通过（无感模式下可能已自动放行）…")
    for i in range(max_clicks):
        # UC Mode 的系统级点击（PyAutoGUI）；需交互的 Turnstile 才用得上。
        # 无感放行时此处通常无目标可点（异常被忽略），成功由业务判据确认。
        try:
            sb.uc_gui_click_captcha()
            log.info(f"已执行 GUI 点击（第 {i + 1}/{max_clicks} 次）")
        except Exception as e:
            log.debug(f"uc_gui_click_captcha：{e}")
        for _ in range(4):
            if get_turnstile_token(sb):
                log.info("已获取 Turnstile token ✓")
                return True
            sb.sleep(1)

    log.info("未显式获取 token；改由业务结果判定是否成功")
    return False


# ============================================================
# 登录态
# ============================================================
def is_logged_in(sb):
    """通过 /api/auth/me 校验当前登录态是否有效。"""
    me = api_call(sb, "GET", "/api/auth/me")
    ok = bool(me) and me.get("__status") == 200 and not me.get("__error")
    if ok:
        # 兼容不同返回结构，尽量取出用户名
        user = me.get("user") or me
        name = user.get("userName") or user.get("username") or user.get("name") or "已登录用户"
        log.info(f"登录态有效：{name}")
    return ok


# ============================================================
# 登录令牌的本地持久化（绕开 leveldb 异步落盘的不确定性）
# ============================================================
def load_token():
    """从独立文件读取上次保存的 auth_token（不存在或失败则返回空串）。"""
    try:
        return TOKEN_FILE.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return ""
    except Exception as e:
        log.debug(f"读取 token 文件失败（忽略）：{e}")
        return ""


def save_token(sb):
    """把当前 localStorage 里的 auth_token 主动落盘到独立文件。

    必须在浏览器仍停留于本站、且已登录时调用。此举不依赖浏览器退出 flush，
    确保下次运行（缓存恢复后）能稳定读回 token，免去反复登录。内容未变则不写盘。
    """
    try:
        token = sb.execute_script(
            "return localStorage.getItem('auth_token') || sessionStorage.getItem('auth_token');"
        )
    except Exception as e:
        log.debug(f"读取 localStorage token 失败（忽略）：{e}")
        return
    if not token or token == load_token():
        return
    try:
        TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
        TOKEN_FILE.write_text(token, encoding="utf-8")
        log.info("已保存登录令牌到本地（供下次免登录复用）")
    except Exception as e:
        log.debug(f"写入 token 文件失败（忽略）：{e}")


def token_expired(token):
    """纯本地解码 JWT 的 exp 判断是否已过期（不验签，仅用于省去一次无效注入）。

    解析失败一律返回 False —— 交由实际注入 + 业务校验兜底，绝不阻断流程。
    """
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        exp = json.loads(base64.urlsafe_b64decode(payload)).get("exp")
        return bool(exp) and exp <= time.time()
    except Exception:
        return False


def inject_token(sb, token):
    """把 token 写进 localStorage 绕过登录，并以登录态校验是否生效。

    需已停留在本站同源页面。校验走 /api/auth/me（直接读 localStorage 发请求），
    故注入后无需刷新页面即可判定。
    """
    if not token or token_expired(token):
        if token:
            log.info("待注入的令牌已过期，跳过注入")
        return False
    try:
        sb.execute_script(
            "localStorage.setItem('auth_token', arguments[0]);"
            "try{sessionStorage.setItem('auth_token', arguments[0]);}catch(e){}",
            token,
        )
    except Exception as e:
        log.debug(f"注入 token 失败：{e}")
        return False
    return is_logged_in(sb)


def do_login(sb):
    """打开 /login，填表单 → 过 Turnstile → 等前端自动提交并写入 auth_token。"""
    if not (USERNAME and PASSWORD):
        raise RuntimeError(
            "无法登录：未配置账号密码。请设置 MAMBO_USERNAME / MAMBO_PASSWORD，"
            "或提供有效的 MAMBO_AUTH_TOKEN / 本地 token 文件以免登录复用。"
        )
    log.info("开始登录流程…")
    sb.uc_open_with_reconnect(f"{BASE_URL}/login", reconnect_time=4)
    sb.sleep(1.5)

    # 表单字段没有 name/id，用类型选择器（页面唯一）
    sb.type('input[type="text"]', USERNAME)
    sb.type('input[type="password"]', PASSWORD)
    log.info("已填入用户名与密码")

    # 勾选「记住密码」→ token 写入 localStorage，便于持久化复用
    try:
        if sb.is_element_present('button[role="checkbox"]'):
            sb.click('button[role="checkbox"]')
            log.info("已勾选『记住密码』")
    except Exception as e:
        log.debug(f"勾选记住密码失败（忽略）：{e}")

    # 点击登录 → 触发 Turnstile 验证弹窗
    sb.click('button[type="submit"]')
    log.info("已点击登录，等待人机验证弹窗…")
    sb.sleep(2)

    # 尽力通过 Turnstile（无感模式自动放行；需交互时本函数会点击。
    # 若你的环境必须人工点选，请用有头模式 MAMBO_DISPLAY=headed 手动完成）
    solve_turnstile(sb)

    # 以【登录态】为最终判据（不依赖 Turnstile token 检测，更可靠）
    deadline = time.time() + LOGIN_TIMEOUT
    while time.time() < deadline:
        token = sb.execute_script(
            "return localStorage.getItem('auth_token') || sessionStorage.getItem('auth_token');"
        )
        if token and is_logged_in(sb):
            log.info("登录成功 ✓")
            return True
        sb.sleep(2)
    raise RuntimeError(
        "登录未成功：无头/数据中心环境可能无法通过人机验证；"
        "请改用有头模式（MAMBO_DISPLAY=headed）在弹窗中手动完成验证，或检查账号密码"
    )


# ============================================================
# 签到
# ============================================================
def get_checkin_status(sb):
    """查询签到状态。返回 (已签到?, 原始数据)。"""
    data = api_call(sb, "GET", "/api/checkin/status")
    checked = bool(data.get("hasCheckedInToday"))
    return checked, data


def find_checkin_page(sb):
    """在候选路由中找到带 [data-checkin-card] 的签到页，返回该路由或 None。"""
    for route in CHECKIN_ROUTES:
        log.info(f"探测签到页：{route}")
        sb.uc_open_with_reconnect(f"{BASE_URL}{route}", reconnect_time=3)
        sb.sleep(2)
        if sb.is_element_present("[data-checkin-card]"):
            log.info(f"在 {route} 找到签到卡片 ✓")
            return route
    return None


def do_checkin(sb):
    """执行签到：定位签到卡片/按钮 → 点击 → 过 Turnstile → 校验结果。"""
    route = find_checkin_page(sb)
    if not route:
        raise RuntimeError("未能在任何候选页面找到签到卡片 [data-checkin-card]")

    # 优先点「立即签到」按钮，退而点整张签到卡片
    clicked = False
    for selector in [
        '//button[contains(., "立即签到")]',
        '//*[contains(text(), "立即签到")]',
        "[data-checkin-card] button",
        "[data-checkin-card]",
    ]:
        try:
            by = "xpath" if selector.startswith("//") else "css selector"
            if sb.is_element_present(selector, by=by):
                sb.click(selector, by=by)
                log.info(f"已点击签到入口：{selector}")
                clicked = True
                break
        except Exception as e:
            log.debug(f"点击 {selector} 失败：{e}")
    if not clicked:
        raise RuntimeError("找到了签到卡片，但点击签到入口失败")

    sb.sleep(2)
    # 签到同样可能弹 Turnstile（action=checkin）；若无则 solve 会直接成功返回
    solve_turnstile(sb)

    # 等待并校验结果（轮询 /api/checkin/status）
    deadline = time.time() + 30
    while time.time() < deadline:
        checked, data = get_checkin_status(sb)
        if checked:
            unit = data.get("currencyUnit") or "奖励"
            log.info(f"签到成功 ✓ 今日已签到（货币单位：{unit}）")
            return True
        sb.sleep(2)

    # 兜底：直接看页面是否出现成功文案
    if sb.is_text_visible("签到成功") or sb.is_text_visible("今日已签到"):
        log.info("签到成功 ✓（依据页面提示）")
        return True
    raise RuntimeError("点击签到后未确认到成功状态")


# ============================================================
# 主入口
# ============================================================
def build_sb_kwargs():
    """根据显示模式组织 SB() 参数。"""
    kwargs = dict(
        uc=True,                     # Undetected Chrome
        user_data_dir=USER_DATA_DIR, # 持久化 profile
        locale_code="zh-CN",
        ad_block=True,
    )
    has_display = bool(os.environ.get("DISPLAY"))
    mode = DISPLAY_MODE
    if mode == "auto":
        mode = "headed" if has_display else "xvfb"
    if mode == "headed":
        kwargs["headed"] = True
    elif mode == "xvfb":
        kwargs["xvfb"] = True          # 无桌面环境用虚拟显示，保证 GUI 点击可用
    elif mode == "headless":
        kwargs["headless"] = True      # 注意：纯 headless 多半过不了 Turnstile
    log.info(f"显示模式：{mode}（DISPLAY={'有' if has_display else '无'}）")
    return kwargs


def main():
    if not AUTH_TOKEN and not load_token() and not (USERNAME and PASSWORD):
        sys.exit(
            "未配置凭据：请设置 MAMBO_AUTH_TOKEN（推荐云端），"
            "或 MAMBO_USERNAME / MAMBO_PASSWORD"
        )

    log.info(f"目标站点：{BASE_URL}")
    log.info(f"持久化目录：{USER_DATA_DIR}")

    with SB(**build_sb_kwargs()) as sb:
        # 先打开首页，建立站点上下文（同源 + 可读 localStorage）
        sb.uc_open_with_reconnect(f"{BASE_URL}/", reconnect_time=4)
        sb.sleep(1.5)

        # 1) 登录态：三层兜底，尽量免登录
        #    ① .profile 里的登录态仍有效 → 直接复用（最理想）
        #    ② 注入令牌（Secret/env 优先，否则本地 token 文件）→ 免登录跳过
        #    ③ 都不行 → 账密登录（会触发登录提醒邮件）
        if is_logged_in(sb):
            log.info("复用已保存的登录态，跳过登录")
        elif inject_token(sb, AUTH_TOKEN or load_token()):
            log.info("已注入持久化登录令牌，跳过登录 ✓")
        else:
            do_login(sb)

        # 登录态已确定有效 → 主动把最新令牌落盘，供下次免登录复用
        # （关键：不依赖浏览器退出时的 leveldb flush，从根上消除反复登录）
        save_token(sb)

        # 2) 是否已签到（幂等）
        checked, status = get_checkin_status(sb)
        if status.get("__error") or status.get("__status") not in (200, None):
            log.warning(f"查询签到状态异常：{status}")
        if checked:
            log.info("今日已签到，无需重复操作 ✓")
            return

        # 3) 执行签到
        do_checkin(sb)
        log.info("全部完成 ✓ 喵～")


def export_token():
    """读取当前 .profile 的 auth_token 并打印，便于配置到 GitHub Secret（MAMBO_AUTH_TOKEN）。

    用于首次 bootstrap 或令牌过期后刷新：在【有效登录态】的本机执行，
    把令牌喂给云端，云端首跑即可免登录。
    """
    log.info(f"目标站点：{BASE_URL}")
    log.info(f"持久化目录：{USER_DATA_DIR}")
    with SB(**build_sb_kwargs()) as sb:
        sb.uc_open_with_reconnect(f"{BASE_URL}/", reconnect_time=4)
        sb.sleep(1.5)
        if not is_logged_in(sb):
            sys.exit(
                "当前 .profile 未处于登录态，无法导出令牌。\n"
                "请先在有桌面环境登录一次（MAMBO_DISPLAY=headed python checkin.py），成功后再导出。"
            )
        token = sb.execute_script(
            "return localStorage.getItem('auth_token') || sessionStorage.getItem('auth_token');"
        )
        if not token:
            sys.exit("登录态有效但未读到 auth_token（站点可能改用 cookie 鉴权），无法导出。")
        save_token(sb)  # 顺便落盘，与正常运行保持一致

    print("\n" + "=" * 64)
    print("登录令牌 auth_token（请妥善保管，等同账号密码，切勿外泄）：")
    print("=" * 64)
    print(token)
    print("=" * 64)
    # 解码过期时间，提示何时需要重新导出刷新 Secret
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        exp = json.loads(base64.urlsafe_b64decode(payload)).get("exp")
        if exp:
            exp_dt = time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime(exp))
            left = (exp - time.time()) / 86400
            print(f"过期时间：{exp_dt}（约 {left:.1f} 天后；过期后需重新导出并更新 Secret）")
    except Exception:
        pass
    print("\n配置方法：仓库 Settings → Secrets and variables → Actions → New repository secret")
    print("  名称：MAMBO_AUTH_TOKEN     值：上面那串令牌\n")


if __name__ == "__main__":
    try:
        if len(sys.argv) > 1 and sys.argv[1] in ("--export-token", "export-token"):
            export_token()
        else:
            main()
    except KeyboardInterrupt:
        sys.exit("\n已手动中断")
    except Exception as exc:
        log.error(f"操作失败：{exc}")
        sys.exit(1)
