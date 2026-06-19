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

# 持久化浏览器数据目录：保留 localStorage(auth_token) 与 cf_clearance，
# 让第二次以后的运行尽量免登录、免验证。
USER_DATA_DIR = os.getenv("MAMBO_PROFILE_DIR", str(Path(__file__).resolve().parent / ".profile"))

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


def do_login(sb):
    """打开 /login，填表单 → 过 Turnstile → 等前端自动提交并写入 auth_token。"""
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
    if not USERNAME or not PASSWORD:
        sys.exit("未配置凭据：请在 .env 中设置 MAMBO_USERNAME / MAMBO_PASSWORD")

    log.info(f"目标站点：{BASE_URL}")
    log.info(f"持久化目录：{USER_DATA_DIR}")

    with SB(**build_sb_kwargs()) as sb:
        # 先打开首页，建立站点上下文（同源 + 可读 localStorage）
        sb.uc_open_with_reconnect(f"{BASE_URL}/", reconnect_time=4)
        sb.sleep(1.5)

        # 1) 登录态
        if not is_logged_in(sb):
            do_login(sb)
        else:
            log.info("复用已保存的登录态，跳过登录")

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


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit("\n已手动中断")
    except Exception as exc:
        log.error(f"签到失败：{exc}")
        sys.exit(1)
