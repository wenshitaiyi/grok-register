from DrissionPage import Chromium, ChromiumOptions
from DrissionPage.errors import PageDisconnectedError
import argparse
import shutil
import tempfile
import datetime
import logging
import time
import os
import secrets
import sys
import json
import urllib3
import requests

from email_register import get_email_and_token, get_oai_code

# ---------- 自定义异常 ----------
class DomainRejectedError(Exception):
    """邮箱域名被网页明确拒绝"""
    pass

class NoVerificationCodeError(Exception):
    """未获取到验证码（超时或邮件未到达）"""
    pass

# ---------- 日志初始化 ----------
def setup_run_logger() -> logging.Logger:
    log_dir = os.path.join(os.path.dirname(__file__), "logs")
    os.makedirs(log_dir, exist_ok=True)
    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    log_path = os.path.join(log_dir, f"run_{ts}.log")

    logger = logging.getLogger("grok_register")
    logger.setLevel(logging.INFO)
    logger.handlers.clear()

    fmt = logging.Formatter("%(asctime)s | %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
    fh = logging.FileHandler(log_path, encoding="utf-8")
    fh.setFormatter(fmt)
    logger.addHandler(fh)
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    logger.addHandler(sh)

    logger.info("日志文件: %s", log_path)
    return logger

run_logger: logging.Logger = None

# ---------- 独立邮箱尝试日志 ----------
def setup_email_attempt_logger() -> logging.Logger:
    log_dir = os.path.join(os.path.dirname(__file__), "logs")
    os.makedirs(log_dir, exist_ok=True)
    log_path = os.path.join(log_dir, "email_attempts.log")
    
    logger = logging.getLogger("email_attempts")
    logger.setLevel(logging.INFO)
    logger.handlers.clear()
    
    fmt = logging.Formatter("%(message)s")   # 只写消息内容，时间戳在记录内
    fh = logging.FileHandler(log_path, encoding="utf-8")
    fh.setFormatter(fmt)
    logger.addHandler(fh)
    return logger

email_attempt_logger = None

def log_email_attempt(email, dev_token, status, error=None, profile=None, sso=None, code=None):
    """将一次尝试的结果写入独立日志文件（JSON Lines）"""
    global email_attempt_logger
    if email_attempt_logger is None:
        email_attempt_logger = setup_email_attempt_logger()
    
    record = {
        "timestamp": datetime.datetime.now().isoformat(),
        "email": email,
        "dev_token": dev_token,
        "status": status,
    }
    if error:
        record["error"] = str(error)
    if profile:
        record.update(profile)   # given_name, family_name, password
    if sso:
        record["sso"] = sso
    if code:
        record["verification_code"] = code
    
    email_attempt_logger.info(json.dumps(record, ensure_ascii=False))

# ---------- 运行时环境适配 ----------
def ensure_stable_python_runtime():
    if sys.version_info < (3, 14) or os.environ.get("DPE_REEXEC_DONE") == "1":
        return

    local_app_data = os.environ.get("LOCALAPPDATA", "")
    candidates = [
        os.path.join(local_app_data, "Programs", "Python", "Python312", "python.exe"),
        os.path.join(local_app_data, "Programs", "Python", "Python313", "python.exe"),
    ]

    current_python = os.path.normcase(os.path.abspath(sys.executable))
    for candidate in candidates:
        if not os.path.isfile(candidate):
            continue
        if os.path.normcase(os.path.abspath(candidate)) == current_python:
            return

        print(f"[*] 检测到 Python {sys.version.split()[0]}，自动切换到更稳定的解释器: {candidate}")
        env = os.environ.copy()
        env["DPE_REEXEC_DONE"] = "1"
        os.execve(candidate, [candidate, os.path.abspath(__file__), *sys.argv[1:]], env)

def warn_runtime_compatibility():
    if sys.version_info >= (3, 14):
        print("[提示] 当前 Python 为 3.14+；若出现 Mail.tm TLS 异常，建议改用 Python 3.12 或 3.13。")

ensure_stable_python_runtime()
warn_runtime_compatibility()

# 无头服务器自动启用 Xvfb 虚拟显示器
_virtual_display = None
if not os.environ.get("DISPLAY") or os.environ.get("USE_XVFB") == "1":
    try:
        from pyvirtualdisplay import Display
        _virtual_display = Display(visible=0, size=(1920, 1080))
        _virtual_display.start()
        print(f"[*] Xvfb 虚拟显示器已启动: {os.environ.get('DISPLAY')}")
    except Exception as e:
        print(f"[Warn] Xvfb 启动失败: {e}，将尝试直接运行")

co = ChromiumOptions()
co.auto_port()
co.set_argument("--no-sandbox")
co.set_argument("--disable-gpu")
co.set_argument("--disable-dev-shm-usage")
co.set_argument("--disable-software-rasterizer")

# 从 config.json 读取代理配置给浏览器
_browser_proxy = ""
try:
    import json as _json_mod
    _cfg_path = os.path.join(os.path.dirname(__file__), "config.json")
    if os.path.isfile(_cfg_path):
        with open(_cfg_path, "r") as _f:
            _cfg = _json_mod.load(_f)
        _browser_proxy = str(_cfg.get("browser_proxy", "") or _cfg.get("proxy", "") or "")
except Exception:
    pass
if _browser_proxy:
    co.set_proxy(_browser_proxy)
    print(f"[*] 浏览器代理: {_browser_proxy}")

# Linux 服务器自动检测 chromium 路径
import platform
import glob as _glob_mod
if platform.system() == "Linux":
    _pw_chromes = _glob_mod.glob(os.path.expanduser("~/.cache/ms-playwright/chromium-*/chrome-linux*/chrome"))
    if _pw_chromes:
        co.set_browser_path(_pw_chromes[0])
    else:
        for _candidate in ["/usr/bin/chromium-browser", "/usr/bin/chromium", "/usr/bin/google-chrome"]:
            if os.path.isfile(_candidate):
                co.set_browser_path(_candidate)
                break

co.set_timeouts(base=1)

# 加载修复 MouseEvent.screenX / screenY 的扩展
EXTENSION_PATH = os.path.abspath(os.path.join(os.path.dirname(__file__), "turnstilePatch"))
co.add_extension(EXTENSION_PATH)

_chrome_temp_dir: str = ""
browser = None
page = None

SIGNUP_URL = "https://accounts.x.ai/sign-up?redirect=grok-com"

_sso_dir = os.path.join(os.path.dirname(__file__), "sso")
_sso_ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
DEFAULT_SSO_FILE = os.path.join(_sso_dir, f"sso_{_sso_ts}.txt")

# ---------- 浏览器控制函数 ----------
def start_browser():
    global browser, page, _chrome_temp_dir
    _chrome_temp_dir = tempfile.mkdtemp(prefix="chrome_run_")
    co.set_user_data_path(_chrome_temp_dir)
    browser = Chromium(co)
    tabs = browser.get_tabs()
    page = tabs[-1] if tabs else browser.new_tab()
    return browser, page

def stop_browser():
    global browser, page, _chrome_temp_dir
    if browser is not None:
        try:
            browser.quit()
        except Exception:
            pass
    browser = None
    page = None
    if _chrome_temp_dir and os.path.isdir(_chrome_temp_dir):
        shutil.rmtree(_chrome_temp_dir, ignore_errors=True)
    _chrome_temp_dir = ""

def restart_browser():
    global browser, page
    if browser is None:
        start_browser()
        return
    try:
        tabs = browser.get_tabs()
        page = tabs[-1] if tabs else browser.new_tab()
        page.run_js("window.localStorage.clear(); window.sessionStorage.clear();")
        page.clear_cache(session_storage=True, cookies=True)
    except Exception:
        stop_browser()
        start_browser()

def refresh_active_page():
    global browser, page
    if browser is None:
        start_browser()
    try:
        tabs = browser.get_tabs()
        if tabs:
            page = tabs[-1]
        else:
            page = browser.new_tab()
    except Exception:
        restart_browser()
    return page

def open_signup_page():
    global page
    refresh_active_page()
    try:
        page.get(SIGNUP_URL)
    except Exception:
        refresh_active_page()
        page = browser.new_tab(SIGNUP_URL)
    click_email_signup_button()

def close_current_page():
    restart_browser()

def has_profile_form():
    refresh_active_page()
    try:
        return bool(page.run_js(
            """
const givenInput = document.querySelector('input[data-testid="givenName"], input[name="givenName"], input[autocomplete="given-name"]');
const familyInput = document.querySelector('input[data-testid="familyName"], input[name="familyName"], input[autocomplete="family-name"]');
const passwordInput = document.querySelector('input[data-testid="password"], input[name="password"], input[type="password"]');
return !!(givenInput && familyInput && passwordInput);
            """
        ))
    except Exception:
        return False

# ---------- 注册步骤函数（已修改） ----------
def click_email_signup_button(timeout=10):
    deadline = time.time() + timeout
    while time.time() < deadline:
        clicked = page.run_js(r"""
function isVisible(node) {
    if (!node) {
        return false;
    }
    const style = window.getComputedStyle(node);
    if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') {
        return false;
    }
    const rect = node.getBoundingClientRect();
    return rect.width > 0 && rect.height > 0;
}

function normalize(text) {
    return String(text || '').replace(/\s+/g, ' ').trim().toLowerCase();
}

function looksLikeEmailSignup(node) {
    if (!node || node.disabled || node.getAttribute?.('aria-disabled') === 'true') {
        return false;
    }

    const text = normalize(node.innerText || node.textContent || '');
    const compactText = text.replace(/\s+/g, '');
    const aria = normalize(node.getAttribute('aria-label') || '');
    const href = normalize(node.getAttribute('href') || '');
    const testid = normalize(node.getAttribute('data-testid') || '');
    const cls = normalize(node.className || '');

    return compactText.includes('使用邮箱注册')
        || compactText.includes('邮箱注册')
        || compactText.includes('sign up with email')
        || compactText.includes('continue with email')
        || compactText.includes('signupwithemail')
        || compactText.includes('signupemail')
        || text === 'email'
        || text.includes('email')
        || aria.includes('email')
        || aria.includes('邮箱')
        || href.includes('email')
        || testid.includes('email')
        || cls.includes('email');
}

function clickNode(node) {
    if (!node) {
        return false;
    }
    if (typeof node.scrollIntoView === 'function') {
        node.scrollIntoView({ block: 'center', inline: 'center' });
    }
    node.focus?.();
    node.click?.();
    return true;
}

const pageText = normalize(document.body ? document.body.innerText : '');
if (pageText.includes('blocked due to abusive traffic patterns')) {
    return 'traffic-blocked';
}

const visibleCandidates = Array.from(document.querySelectorAll('button, a, [role="button"], input[type="button"], input[type="submit"]'))
    .filter(isVisible);
const target = visibleCandidates.find(looksLikeEmailSignup) || null;
if (target) {
    return clickNode(target) ? 'clicked' : 'not-found';
}

const bodyText = normalize(document.body ? document.body.innerText : '');
if (bodyText.includes('使用邮箱注册') || bodyText.includes('邮箱注册') || bodyText.includes('sign up with email') || bodyText.includes('continue with email')) {
    const all = Array.from(document.querySelectorAll('*')).filter((node) => isVisible(node) && looksLikeEmailSignup(node));
    const fallback = all.find((node) => ['A', 'BUTTON', 'INPUT'].includes(node.tagName)) || all[0] || null;
    if (fallback) {
        return clickNode(fallback) ? 'clicked' : 'not-found';
    }
}

return 'not-found';
        """)

        if clicked == 'clicked':
            return True

        if clicked == 'traffic-blocked':
            raise Exception('x.ai 返回 Blocked due to abusive traffic patterns，当前网络/环境被风控拦截')

        if clicked == 'not-found':
            snapshot = page.run_js(r"""
function isVisible(node) {
    if (!node) {
        return false;
    }
    const style = window.getComputedStyle(node);
    if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') {
        return false;
    }
    const rect = node.getBoundingClientRect();
    return rect.width > 0 && rect.height > 0;
}

function collectText(text) {
    return String(text || '').replace(/\s+/g, ' ').trim();
}

return {
    url: location.href,
    title: document.title,
    bodyText: collectText(document.body ? document.body.innerText : '').slice(0, 1500),
    buttons: Array.from(document.querySelectorAll('button, a, [role="button"], input[type="button"], input[type="submit"]'))
        .filter(isVisible)
        .slice(0, 30)
        .map((node) => ({
            tag: node.tagName,
            text: collectText(node.innerText || node.textContent || ''),
            ariaLabel: node.getAttribute('aria-label') || '',
            href: node.getAttribute('href') || '',
            dataTestId: node.getAttribute('data-testid') || '',
            type: node.getAttribute('type') || '',
            className: String(node.className || ''),
        })),
};
            """)
            print(f"[Debug] 邮箱注册页摘要: {snapshot}")

        time.sleep(0.5)

    raise Exception('未找到“使用邮箱注册”按钮')

def fill_email_and_submit(email, dev_token, timeout=15):
    """
    使用给定的邮箱和 token 填写邮箱输入框并点击注册按钮。
    若检测到域名拒绝提示，抛出 DomainRejectedError。
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        filled = page.run_js(
            r"""
const email = arguments[0];

function isVisible(node) {
    if (!node) {
        return false;
    }
    const style = window.getComputedStyle(node);
    if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') {
        return false;
    }
    const rect = node.getBoundingClientRect();
    return rect.width > 0 && rect.height > 0;
}

function getInputText(node) {
    const parts = [
        node.id,
        node.name,
        node.type,
        node.autocomplete,
        node.placeholder,
        node.getAttribute('aria-label'),
        node.getAttribute('data-testid'),
        node.getAttribute('data-test'),
        node.getAttribute('data-cy'),
    ];

    if (node.id) {
        const label = document.querySelector(`label[for="${CSS.escape(node.id)}"]`);
        if (label) {
            parts.push(label.innerText || label.textContent || '');
        }
    }

    const wrappingLabel = node.closest('label');
    if (wrappingLabel) {
        parts.push(wrappingLabel.innerText || wrappingLabel.textContent || '');
    }

    const container = node.closest('div, form, section');
    if (container) {
        parts.push(container.innerText || container.textContent || '');
    }

    return parts.filter(Boolean).join(' ').replace(/\s+/g, ' ').toLowerCase();
}

function looksLikeEmailInput(node) {
    if (!isVisible(node) || node.disabled || node.readOnly) {
        return false;
    }

    const type = String(node.type || '').toLowerCase();
    if (type && !['email', 'text', 'search', ''].includes(type)) {
        return false;
    }

    const text = getInputText(node);
    return type === 'email'
        || /\b(e-?mail|email address|mail)\b/.test(text)
        || text.includes('邮箱')
        || text.includes('电子邮件')
        || text.includes('邮件地址');
}

function findEmailInput() {
    const preferredSelector = [
        'input[data-testid="email"]',
        'input[name="email"]',
        'input[type="email"]',
        'input[autocomplete="email"]',
        'input[id*="email" i]',
        'input[name*="email" i]',
        'input[placeholder*="email" i]',
        'input[aria-label*="email" i]',
        'input[placeholder*="邮箱" i]',
        'input[aria-label*="邮箱" i]',
    ].join(',');

    const preferred = Array.from(document.querySelectorAll(preferredSelector)).find(looksLikeEmailInput);
    if (preferred) {
        return preferred;
    }

    const form = Array.from(document.querySelectorAll('form')).find((node) => {
        const text = (node.innerText || node.textContent || '').toLowerCase();
        return text.includes('email') || text.includes('邮箱') || text.includes('邮件');
    });
    const scope = form || document;
    return Array.from(scope.querySelectorAll('input')).find(looksLikeEmailInput) || null;
}

const input = findEmailInput();

if (!input) {
    return 'not-ready';
}

input.focus();
input.click();

const valueSetter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')?.set;
const tracker = input._valueTracker;
if (tracker) {
    tracker.setValue('');
}
if (valueSetter) {
    valueSetter.call(input, email);
} else {
    input.value = email;
}

input.dispatchEvent(new InputEvent('beforeinput', {
    bubbles: true,
    data: email,
    inputType: 'insertText',
}));
input.dispatchEvent(new InputEvent('input', {
    bubbles: true,
    data: email,
    inputType: 'insertText',
}));
input.dispatchEvent(new Event('change', { bubbles: true }));

if ((input.value || '').trim() !== email || !input.checkValidity()) {
    return false;
}

input.blur();
return 'filled';
            """,
            email,
        )

        if filled == 'not-ready':
            time.sleep(0.5)
            continue

        if filled != 'filled':
            print(f"[Debug] 邮箱输入框已出现，但写入失败: {filled}")
            time.sleep(0.5)
            continue

        if filled == 'filled':
            time.sleep(0.8)
            clicked = page.run_js(
                r"""
function isVisible(node) {
    if (!node) {
        return false;
    }
    const style = window.getComputedStyle(node);
    if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') {
        return false;
    }
    const rect = node.getBoundingClientRect();
    return rect.width > 0 && rect.height > 0;
}

function getInputText(node) {
    const parts = [
        node.id,
        node.name,
        node.type,
        node.autocomplete,
        node.placeholder,
        node.getAttribute('aria-label'),
        node.getAttribute('data-testid'),
        node.getAttribute('data-test'),
        node.getAttribute('data-cy'),
    ];

    if (node.id) {
        const label = document.querySelector(`label[for="${CSS.escape(node.id)}"]`);
        if (label) {
            parts.push(label.innerText || label.textContent || '');
        }
    }

    const wrappingLabel = node.closest('label');
    if (wrappingLabel) {
        parts.push(wrappingLabel.innerText || wrappingLabel.textContent || '');
    }

    const container = node.closest('div, form, section');
    if (container) {
        parts.push(container.innerText || container.textContent || '');
    }

    return parts.filter(Boolean).join(' ').replace(/\s+/g, ' ').toLowerCase();
}

function looksLikeEmailInput(node) {
    if (!isVisible(node) || node.disabled || node.readOnly) {
        return false;
    }

    const type = String(node.type || '').toLowerCase();
    if (type && !['email', 'text', 'search', ''].includes(type)) {
        return false;
    }

    const text = getInputText(node);
    return type === 'email'
        || /\b(e-?mail|email address|mail)\b/.test(text)
        || text.includes('邮箱')
        || text.includes('电子邮件')
        || text.includes('邮件地址');
}

function findEmailInput() {
    const preferredSelector = [
        'input[data-testid="email"]',
        'input[name="email"]',
        'input[type="email"]',
        'input[autocomplete="email"]',
        'input[id*="email" i]',
        'input[name*="email" i]',
        'input[placeholder*="email" i]',
        'input[aria-label*="email" i]',
        'input[placeholder*="邮箱" i]',
        'input[aria-label*="邮箱" i]',
    ].join(',');

    const preferred = Array.from(document.querySelectorAll(preferredSelector)).find(looksLikeEmailInput);
    if (preferred) {
        return preferred;
    }

    const form = Array.from(document.querySelectorAll('form')).find((node) => {
        const text = (node.innerText || node.textContent || '').toLowerCase();
        return text.includes('email') || text.includes('邮箱') || text.includes('邮件');
    });
    const scope = form || document;
    return Array.from(scope.querySelectorAll('input')).find(looksLikeEmailInput) || null;
}

const input = findEmailInput();

if (!input || !input.checkValidity() || !(input.value || '').trim()) {
    return false;
}

const buttons = Array.from(document.querySelectorAll('button[type="submit"], button')).filter((node) => {
    return isVisible(node) && !node.disabled && node.getAttribute('aria-disabled') !== 'true';
});
const submitButton = buttons.find((node) => {
    const text = (node.innerText || node.textContent || '').replace(/\s+/g, '');
    const t = text.toLowerCase(); return text === '注册' || text.includes('注册') || text === '继续' || text.includes('继续') || text === '下一步' || text.includes('下一步') || t === 'signup' || t === 'sign up' || t.includes('sign up') || t.includes('continue') || t.includes('next');
});

if (!submitButton || submitButton.disabled) {
    return false;
}

submitButton.click();
return true;
                """
            )

            if clicked:
                print(f"[*] 已填写邮箱并点击注册: {email}")
                
                # 检测域名拒绝提示
                time.sleep(1.5)
                error_msg = page.run_js("""
                    const errNodes = document.querySelectorAll('p[class*="danger"], span[class*="danger"], div[class*="danger"]');
                    for (let node of errNodes) {
                        const text = (node.innerText || node.textContent || '').toLowerCase();
                        if (text.includes('拒绝') || text.includes('使用其他') || text.includes('rejected') || text.includes('support@x.ai')) {
                            return text.trim();
                        }
                    }
                    return null;
                """)
                
                if error_msg:
                    raise DomainRejectedError(f"DOMAIN_REJECTED: {error_msg}")

                return True   # 成功

        time.sleep(0.5)

    debug_snapshot = page.run_js(
        r"""
function isVisible(node) {
    if (!node) {
        return false;
    }
    const style = window.getComputedStyle(node);
    if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') {
        return false;
    }
    const rect = node.getBoundingClientRect();
    return rect.width > 0 && rect.height > 0;
}

const inputs = Array.from(document.querySelectorAll('input')).filter(isVisible).map((node) => ({
    type: node.type || '',
    name: node.name || '',
    id: node.id || '',
    testid: node.getAttribute('data-testid') || '',
    autocomplete: node.autocomplete || '',
    placeholder: node.placeholder || '',
    ariaLabel: node.getAttribute('aria-label') || '',
    valueLength: String(node.value || '').length,
}));

const buttons = Array.from(document.querySelectorAll('button, a, [role="button"]')).filter(isVisible).map((node) => ({
    text: String(node.innerText || node.textContent || '').replace(/\s+/g, ' ').trim(),
    disabled: !!node.disabled,
    ariaDisabled: node.getAttribute('aria-disabled') || '',
}));

return { url: location.href, inputs, buttons };
        """
    )
    print(f"[Debug] 邮箱页 DOM 摘要: {debug_snapshot}")
    raise Exception("未找到邮箱输入框或注册按钮")

def fill_code_and_submit(email, dev_token, timeout=60):
    """
    获取验证码并填写提交。
    若未获取到验证码，抛出 NoVerificationCodeError。
    """
    code = get_oai_code(dev_token, email)
    if not code:
        raise NoVerificationCodeError("获取验证码失败（超时或邮件未到达）")

    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            filled = page.run_js(
                """
const code = String(arguments[0] || '').trim();

function isVisible(node) {
    if (!node) {
        return false;
    }
    const style = window.getComputedStyle(node);
    if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') {
        return false;
    }
    const rect = node.getBoundingClientRect();
    return rect.width > 0 && rect.height > 0;
}

function setNativeValue(input, value) {
    const nativeInputValueSetter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value')?.set;
    const tracker = input._valueTracker;
    if (tracker) {
        tracker.setValue('');
    }
    if (nativeInputValueSetter) {
        nativeInputValueSetter.call(input, '');
        nativeInputValueSetter.call(input, value);
    } else {
        input.value = '';
        input.value = value;
    }
}

function dispatchInputEvents(input, value) {
    input.dispatchEvent(new InputEvent('beforeinput', {
        bubbles: true,
        cancelable: true,
        data: value,
        inputType: 'insertText',
    }));
    input.dispatchEvent(new InputEvent('input', {
        bubbles: true,
        cancelable: true,
        data: value,
        inputType: 'insertText',
    }));
    input.dispatchEvent(new Event('change', { bubbles: true }));
}

const input = Array.from(document.querySelectorAll('input[data-input-otp="true"], input[name="code"], input[autocomplete="one-time-code"], input[inputmode="numeric"], input[inputmode="text"]')).find((node) => {
    return isVisible(node) && !node.disabled && !node.readOnly && Number(node.maxLength || code.length || 6) > 1;
}) || null;

const otpBoxes = Array.from(document.querySelectorAll('input')).filter((node) => {
    if (!isVisible(node) || node.disabled || node.readOnly) {
        return false;
    }
    const maxLength = Number(node.maxLength || 0);
    const autocomplete = String(node.autocomplete || '').toLowerCase();
    return maxLength === 1 || autocomplete === 'one-time-code';
});

if (!input && otpBoxes.length < code.length) {
    return 'not-ready';
}

if (input) {
    input.focus();
    input.click();
    setNativeValue(input, code);
    dispatchInputEvents(input, code);

    const normalizedValue = String(input.value || '').trim();
    const expectedLength = Number(input.maxLength || code.length || 6);
    const slots = Array.from(document.querySelectorAll('[data-input-otp-slot="true"]'));
    const filledSlots = slots.filter((slot) => (slot.textContent || '').trim()).length;

    if (normalizedValue !== code) {
        return 'aggregate-mismatch';
    }

    if (expectedLength > 0 && normalizedValue.length !== expectedLength) {
        return 'aggregate-length-mismatch';
    }

    if (slots.length && filledSlots && filledSlots !== normalizedValue.length) {
        return 'aggregate-slot-mismatch';
    }

    input.blur();
    return 'filled';
}

const orderedBoxes = otpBoxes.slice(0, code.length);
for (let i = 0; i < orderedBoxes.length; i += 1) {
    const box = orderedBoxes[i];
    const char = code[i] || '';
    box.focus();
    box.click();
    setNativeValue(box, char);
    dispatchInputEvents(box, char);
    box.dispatchEvent(new KeyboardEvent('keydown', { bubbles: true, key: char }));
    box.dispatchEvent(new KeyboardEvent('keyup', { bubbles: true, key: char }));
    box.blur();
}

const merged = orderedBoxes.map((node) => String(node.value || '').trim()).join('');
return merged === code ? 'filled' : 'box-mismatch';
                """,
                code,
            )
        except PageDisconnectedError:
            refresh_active_page()
            if has_profile_form():
                print("[*] 验证码提交后已跳转到最终注册页。")
                return code
            time.sleep(1)
            continue

        if filled == 'not-ready':
            if has_profile_form():
                print("[*] 已直接进入最终注册页，跳过验证码按钮确认。")
                return code
            time.sleep(0.5)
            continue

        if filled != 'filled':
            print(f"[Debug] 验证码输入框已出现，但写入失败: {filled}")
            time.sleep(0.5)
            continue

        if filled == 'filled':
            time.sleep(1.2)
            try:
                clicked = page.run_js(
                    r"""
function isVisible(node) {
    if (!node) {
        return false;
    }
    const style = window.getComputedStyle(node);
    if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') {
        return false;
    }
    const rect = node.getBoundingClientRect();
    return rect.width > 0 && rect.height > 0;
}

const aggregateInput = Array.from(document.querySelectorAll('input[data-input-otp="true"], input[name="code"], input[autocomplete="one-time-code"], input[inputmode="numeric"], input[inputmode="text"]')).find((node) => {
    return isVisible(node) && !node.disabled && !node.readOnly && Number(node.maxLength || 0) > 1;
}) || null;

let value = '';
if (aggregateInput) {
    value = String(aggregateInput.value || '').trim();
    const expectedLength = Number(aggregateInput.maxLength || value.length || 6);
    if (!value || (expectedLength > 0 && value.length !== expectedLength)) {
        return false;
    }

    const slots = Array.from(document.querySelectorAll('[data-input-otp-slot="true"]'));
    if (slots.length) {
        const filledSlots = slots.filter((slot) => (slot.textContent || '').trim()).length;
        if (filledSlots && filledSlots !== value.length) {
            return false;
        }
    }
} else {
    const otpBoxes = Array.from(document.querySelectorAll('input')).filter((node) => {
        if (!isVisible(node) || node.disabled || node.readOnly) {
            return false;
        }
        const maxLength = Number(node.maxLength || 0);
        const autocomplete = String(node.autocomplete || '').toLowerCase();
        return maxLength === 1 || autocomplete === 'one-time-code';
    });
    value = otpBoxes.map((node) => String(node.value || '').trim()).join('');
    if (!value || value.length < 6) {
        return false;
    }
}

const buttons = Array.from(document.querySelectorAll('button[type="submit"], button')).filter((node) => {
    return isVisible(node) && !node.disabled && node.getAttribute('aria-disabled') !== 'true';
});
const confirmButton = buttons.find((node) => {
    const text = (node.innerText || node.textContent || '').replace(/\s+/g, '');
    const t = text.toLowerCase(); return text === '确认邮箱' || text.includes('确认邮箱') || text === '继续' || text.includes('继续') || text === '下一步' || text.includes('下一步') || t.includes('confirm') || t.includes('continue') || t.includes('next') || t.includes('verify');
});

if (!confirmButton) {
    return 'no-button';
}

confirmButton.focus();
confirmButton.click();
return 'clicked';
                    """
                )
            except PageDisconnectedError:
                refresh_active_page()
                if has_profile_form():
                    print("[*] 确认邮箱后页面跳转成功，已进入最终注册页。")
                    return code
                clicked = 'disconnected'

            if clicked == 'clicked':
                print(f"[*] 已填写验证码并点击确认邮箱: {code}")
                time.sleep(2)
                refresh_active_page()
                if has_profile_form():
                    print("[*] 验证码确认完成，最终注册页已就绪。")
                return code

            if clicked == 'no-button':
                current_url = page.url
                if 'sign-up' in current_url or 'signup' in current_url:
                    print(f"[*] 已填写验证码，页面已自动跳转到下一步: {current_url}")
                    return code

            if clicked == 'disconnected':
                time.sleep(1)
                continue

        time.sleep(0.5)

    debug_snapshot = page.run_js(
        r"""
function isVisible(node) {
    if (!node) {
        return false;
    }
    const style = window.getComputedStyle(node);
    if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') {
        return false;
    }
    const rect = node.getBoundingClientRect();
    return rect.width > 0 && rect.height > 0;
}

const inputs = Array.from(document.querySelectorAll('input')).filter(isVisible).map((node) => ({
    type: node.type || '',
    name: node.name || '',
    testid: node.getAttribute('data-testid') || '',
    autocomplete: node.autocomplete || '',
    maxLength: Number(node.maxLength || 0),
    value: String(node.value || ''),
}));

const buttons = Array.from(document.querySelectorAll('button')).filter(isVisible).map((node) => ({
    text: String(node.innerText || node.textContent || '').replace(/\s+/g, ' ').trim(),
    disabled: !!node.disabled,
    ariaDisabled: node.getAttribute('aria-disabled') || '',
}));

return { url: location.href, inputs, buttons };
        """
    )
    print(f"[Debug] 验证码页 DOM 摘要: {debug_snapshot}")
    raise Exception("未找到验证码输入框或确认邮箱按钮")

def getTurnstileToken():
    page.run_js("try { turnstile.reset() } catch(e) { }")

    turnstileResponse = None

    for i in range(0, 15):
        try:
            turnstileResponse = page.run_js("try { return turnstile.getResponse() } catch(e) { return null }")
            if turnstileResponse:
                return turnstileResponse

            challengeSolution = page.ele("@name=cf-turnstile-response")
            challengeWrapper = challengeSolution.parent()
            challengeIframe = challengeWrapper.shadow_root.ele("tag:iframe")

            challengeIframe.run_js("""
window.dtp = 1
function getRandomInt(min, max) {
    return Math.floor(Math.random() * (max - min + 1)) + min;
}

let screenX = getRandomInt(800, 1200);
let screenY = getRandomInt(400, 600);

Object.defineProperty(MouseEvent.prototype, 'screenX', { value: screenX });
Object.defineProperty(MouseEvent.prototype, 'screenY', { value: screenY });
                        """)

            challengeIframeBody = challengeIframe.ele("tag:body").shadow_root
            challengeButton = challengeIframeBody.ele("tag:input")
            challengeButton.click()
        except:
            pass
        time.sleep(1)
    raise Exception("failed to solve turnstile")

def build_profile():
    given_name = "Neo"
    family_name = "Lin"
    password = "N" + secrets.token_hex(4) + "!a7#" + secrets.token_urlsafe(6)
    return given_name, family_name, password

def fill_profile_and_submit(timeout=30):
    given_name, family_name, password = build_profile()
    deadline = time.time() + timeout
    turnstile_token = ""

    while time.time() < deadline:
        filled = page.run_js(
            """
const givenName = arguments[0];
const familyName = arguments[1];
const password = arguments[2];

function isVisible(node) {
    if (!node) {
        return false;
    }
    const style = window.getComputedStyle(node);
    if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') {
        return false;
    }
    const rect = node.getBoundingClientRect();
    return rect.width > 0 && rect.height > 0;
}

function pickInput(selector) {
    return Array.from(document.querySelectorAll(selector)).find((node) => {
        return isVisible(node) && !node.disabled && !node.readOnly;
    }) || null;
}

function setInputValue(input, value) {
    if (!input) {
        return false;
    }
    input.focus();
    input.click();

    const nativeSetter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value')?.set;
    const tracker = input._valueTracker;
    if (tracker) {
        tracker.setValue('');
    }

    if (nativeSetter) {
        nativeSetter.call(input, '');
        nativeSetter.call(input, value);
    } else {
        input.value = '';
        input.value = value;
    }

    input.dispatchEvent(new InputEvent('beforeinput', {
        bubbles: true,
        cancelable: true,
        data: value,
        inputType: 'insertText',
    }));
    input.dispatchEvent(new InputEvent('input', {
        bubbles: true,
        cancelable: true,
        data: value,
        inputType: 'insertText',
    }));
    input.dispatchEvent(new Event('change', { bubbles: true }));
    input.dispatchEvent(new Event('blur', { bubbles: true }));

    return String(input.value || '') === String(value || '');
}

const givenInput = pickInput('input[data-testid="givenName"], input[name="givenName"], input[autocomplete="given-name"]');
const familyInput = pickInput('input[data-testid="familyName"], input[name="familyName"], input[autocomplete="family-name"]');
const passwordInput = pickInput('input[data-testid="password"], input[name="password"], input[type="password"]');

if (!givenInput || !familyInput || !passwordInput) {
    return 'not-ready';
}

const givenOk = setInputValue(givenInput, givenName);
const familyOk = setInputValue(familyInput, familyName);
const passwordOk = setInputValue(passwordInput, password);

if (!givenOk || !familyOk || !passwordOk) {
    return 'filled-failed';
}

return [
    String(givenInput.value || '').trim() === String(givenName || '').trim(),
    String(familyInput.value || '').trim() === String(familyName || '').trim(),
    String(passwordInput.value || '') === String(password || ''),
].every(Boolean) ? 'filled' : 'verify-failed';
            """,
            given_name,
            family_name,
            password,
        )

        if filled == 'not-ready':
            time.sleep(0.5)
            continue

        if filled != 'filled':
            print(f"[Debug] 最终注册页输入框已出现，但姓名/密码写入失败: {filled}")
            time.sleep(0.5)
            continue

        values_ok = page.run_js(
            """
const expectedGiven = arguments[0];
const expectedFamily = arguments[1];
const expectedPassword = arguments[2];

function isVisible(node) {
    if (!node) {
        return false;
    }
    const style = window.getComputedStyle(node);
    if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') {
        return false;
    }
    const rect = node.getBoundingClientRect();
    return rect.width > 0 && rect.height > 0;
}

function pickInput(selector) {
    return Array.from(document.querySelectorAll(selector)).find((node) => {
        return isVisible(node) && !node.disabled && !node.readOnly;
    }) || null;
}

const givenInput = pickInput('input[data-testid="givenName"], input[name="givenName"], input[autocomplete="given-name"]');
const familyInput = pickInput('input[data-testid="familyName"], input[name="familyName"], input[autocomplete="family-name"]');
const passwordInput = pickInput('input[data-testid="password"], input[name="password"], input[type="password"]');

if (!givenInput || !familyInput || !passwordInput) {
    return false;
}

return String(givenInput.value || '').trim() === String(expectedGiven || '').trim()
    && String(familyInput.value || '').trim() === String(expectedFamily || '').trim()
    && String(passwordInput.value || '') === String(expectedPassword || '');
            """,
            given_name,
            family_name,
            password,
        )
        if not values_ok:
            print("[Debug] 最终注册页字段值校验失败，继续重试填写。")
            time.sleep(0.5)
            continue

        turnstile_state = page.run_js(
            """
const challengeInput = document.querySelector('input[name="cf-turnstile-response"]');
if (!challengeInput) {
    return 'not-found';
}
const value = String(challengeInput.value || '').trim();
return value ? 'ready' : 'pending';
            """
        )

        if turnstile_state == "pending" and not turnstile_token:
            print("[*] 检测到最终注册页存在 Turnstile，开始使用现有真人化点击逻辑。")
            turnstile_token = getTurnstileToken()
            if turnstile_token:
                synced = page.run_js(
                    """
const token = arguments[0];
const challengeInput = document.querySelector('input[name="cf-turnstile-response"]');
if (!challengeInput) {
    return false;
}
const nativeSetter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value')?.set;
if (nativeSetter) {
    nativeSetter.call(challengeInput, token);
} else {
    challengeInput.value = token;
}
challengeInput.dispatchEvent(new Event('input', { bubbles: true }));
challengeInput.dispatchEvent(new Event('change', { bubbles: true }));
return String(challengeInput.value || '').trim() === String(token || '').trim();
                    """,
                    turnstile_token,
                )
                if synced:
                    print("[*] Turnstile 响应已同步到最终注册表单。")

        time.sleep(1.2)

        try:
            submit_button = page.ele('tag:button@@text()=完成注册') or page.ele('tag:button@@text():Create Account') or page.ele('tag:button@@text():Sign up')
        except Exception:
            submit_button = None

        if not submit_button:
            clicked = page.run_js(
                r"""
const challengeInput = document.querySelector('input[name="cf-turnstile-response"]');
if (challengeInput && !String(challengeInput.value || '').trim()) {
    return false;
}
const buttons = Array.from(document.querySelectorAll('button[type="submit"], button'));
const submitButton = buttons.find((node) => {
    const text = (node.innerText || node.textContent || '').replace(/\s+/g, '');
    const t = text.toLowerCase(); return text === '完成注册' || text.includes('完成注册') || t.includes('create account') || t.includes('sign up') || t.includes('complete');
});
if (!submitButton || submitButton.disabled || submitButton.getAttribute('aria-disabled') === 'true') {
    return false;
}
submitButton.focus();
submitButton.click();
return true;
                """
            )
        else:
            challenge_value = page.run_js(
                """
const challengeInput = document.querySelector('input[name="cf-turnstile-response"]');
return challengeInput ? String(challengeInput.value || '').trim() : 'not-found';
                """
            )
            if challenge_value not in ('not-found', ''):
                submit_button.click()
                clicked = True
            else:
                clicked = False

        if clicked:
            print(f"[*] 已填写注册资料并点击完成注册: {given_name} {family_name} / {password}")
            return {
                "given_name": given_name,
                "family_name": family_name,
                "password": password,
            }

        time.sleep(0.5)

    raise Exception("未找到最终注册表单或完成注册按钮")

def extract_visible_numbers(timeout=60):
    deadline = time.time() + timeout
    while time.time() < deadline:
        result = page.run_js(
            r"""
function isVisible(el) {
    if (!el) {
        return false;
    }
    const style = window.getComputedStyle(el);
    if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') {
        return false;
    }
    const rect = el.getBoundingClientRect();
    return rect.width > 0 && rect.height > 0;
}

const selector = [
    'h1', 'h2', 'h3', 'h4', 'h5', 'h6',
    'div', 'span', 'p', 'strong', 'b', 'small',
    '[data-testid]', '[class]', '[role="heading"]'
].join(',');

const seen = new Set();
const matches = [];
for (const node of document.querySelectorAll(selector)) {
    if (!isVisible(node)) {
        continue;
    }
    const text = String(node.innerText || node.textContent || '').replace(/\s+/g, ' ').trim();
    if (!text) {
        continue;
    }
    const found = text.match(/\d+(?:\.\d+)?/g);
    if (!found) {
        continue;
    }
    for (const value of found) {
        const key = `${value}@@${text}`;
        if (seen.has(key)) {
            continue;
        }
        seen.add(key);
        matches.push({ value, text });
    }
}

return matches.slice(0, 30);
            """
        )

        if result:
            print("[*] 页面可见数字文本提取结果:")
            for item in result:
                try:
                    print(f"    - 数字: {item['value']} | 上下文: {item['text']}")
                except Exception:
                    pass
            return result

        time.sleep(1)

    raise Exception("登录后未提取到可见数字文本")

def wait_for_sso_cookie(timeout=30):
    deadline = time.time() + timeout
    last_seen_names = set()

    while time.time() < deadline:
        try:
            refresh_active_page()
            if page is None:
                time.sleep(1)
                continue

            cookies = page.cookies(all_domains=True, all_info=True) or []
            for item in cookies:
                if isinstance(item, dict):
                    name = str(item.get("name", "")).strip()
                    value = str(item.get("value", "")).strip()
                else:
                    name = str(getattr(item, "name", "")).strip()
                    value = str(getattr(item, "value", "")).strip()

                if name:
                    last_seen_names.add(name)

                if name == "sso" and value:
                    print("[*] 注册完成后已获取到 sso cookie。")
                    return value

        except PageDisconnectedError:
            refresh_active_page()
        except Exception:
            pass

        time.sleep(1)

    raise Exception(f"注册完成后未获取到 sso cookie，当前已见 cookie: {sorted(last_seen_names)}")

def append_sso_to_txt(sso_value, output_path=DEFAULT_SSO_FILE):
    normalized = str(sso_value or "").strip()
    if not normalized:
        raise Exception("待写入的 sso 为空")

    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    with open(output_path, "a", encoding="utf-8") as file:
        file.write(normalized + "\n")

    print(f"[*] 已追加写入 sso 到文件: {output_path}")

def push_sso_to_api(new_tokens: list):
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

    config_path = os.path.join(os.path.dirname(__file__), "config.json")
    try:
        with open(config_path, "r", encoding="utf-8") as f:
            conf = json.load(f)
    except Exception as e:
        print(f"[Warn] 读取 config.json 失败，跳过推送: {e}")
        return

    api_conf = conf.get("api", {})
    endpoint = str(api_conf.get("endpoint", "")).strip()
    api_token = str(api_conf.get("token", "")).strip()
    append_mode = api_conf.get("append", True)

    if not endpoint or not api_token:
        return

    headers = {
        "Authorization": f"Bearer {api_token}",
        "Content-Type": "application/json",
    }

    tokens_to_push = [t for t in new_tokens if t]

    if append_mode:
        try:
            get_resp = requests.get(endpoint, headers=headers, timeout=15, verify=False)
            if get_resp.status_code == 200:
                data = get_resp.json()
                if isinstance(data, dict) and isinstance(data.get("tokens"), dict):
                    existing = data["tokens"].get("ssoBasic", [])
                else:
                    existing = data.get("ssoBasic", []) if isinstance(data, dict) else []
                existing_tokens = [
                    item["token"] if isinstance(item, dict) else str(item)
                    for item in existing if item
                ]
                seen = set()
                deduped = []
                for t in existing_tokens + tokens_to_push:
                    if t not in seen:
                        seen.add(t)
                        deduped.append(t)
                tokens_to_push = deduped
                print(f"[*] 查询到线上 {len(existing_tokens)} 个 token，合并本次 {len(new_tokens)} 个，共 {len(deduped)} 个")
            else:
                print(f"[Error] 查询线上 token 失败: HTTP {get_resp.status_code}，放弃推送以保护存量数据")
                return
        except Exception as e:
            print(f"[Error] 查询线上 token 异常: {e}，放弃推送以保护存量数据")
            return

    try:
        resp = requests.post(
            endpoint,
            json={"ssoBasic": tokens_to_push},
            headers=headers,
            timeout=60,
            verify=False,
        )
        if resp.status_code == 200:
            print(f"[*] SSO token 已推送到 API（共 {len(tokens_to_push)} 个）: {endpoint}")
        else:
            print(f"[Warn] 推送 API 返回异常: HTTP {resp.status_code} {resp.text[:200]}")
    except Exception as e:
        print(f"[Warn] 推送 API 失败: {e}")

# ---------- 单轮注册（含日志记录） ----------
def run_single_registration(output_path=DEFAULT_SSO_FILE, extract_numbers=False):
    """
    单轮注册流程，统一获取邮箱和 token，并在最后记录尝试结果。
    """
    #设定本轮注册的最大硬限时：180 秒（3 分钟）
    MAX_ROUND_DURATION = 80 
    round_start_time = time.time()

    def check_global_timeout(step_name):
        """辅助闭包：检查是否超过本轮总限时"""
        if time.time() - round_start_time > MAX_ROUND_DURATION:
            raise TimeoutError(f"【硬超时】{step_name} 阶段由于本轮总耗时超过 {MAX_ROUND_DURATION} 秒，强行终止")

    # 1. 获取临时邮箱和 token
    email, dev_token = get_email_and_token()
    if not email or not dev_token:
        log_email_attempt("", "", "failed", error="获取临时邮箱失败")
        raise Exception("获取邮箱失败")

    # 2. 执行注册流程
    try:
        open_signup_page()
        check_global_timeout("打开注册页")  #检查超时

        fill_email_and_submit(email, dev_token)   # 可能抛出 DomainRejectedError
        check_global_timeout("填写邮箱提交")  #检查超时

        # 注意：这里我们同时把等待验证码的默认超时传参缩短，双重保险
        code = fill_code_and_submit(email, dev_token, timeout=30)   # 可能抛出 NoVerificationCodeError
        check_global_timeout("填写验证码提交")  #检查超时

        profile = fill_profile_and_submit()
        check_global_timeout("填写资料提交")  #检查超时

        sso_value = wait_for_sso_cookie()
        append_sso_to_txt(sso_value, output_path)

        if extract_numbers:
            extract_visible_numbers()

        # 成功 -> 记录
        log_email_attempt(
            email=email,
            dev_token=dev_token,
            status="success",
            profile=profile,
            sso=sso_value,
            code=code
        )

        result = {
            "email": email,
            "sso": sso_value,
            **profile,
        }
        if run_logger:
            run_logger.info(
                "注册成功 | email=%s | password=%s | given=%s | family=%s",
                email,
                profile.get("password", ""),
                profile.get("given_name", ""),
                profile.get("family_name", ""),
            )
        print(f"[*] 本轮注册完成，邮箱: {email}")
        return result

    except DomainRejectedError as e:
        log_email_attempt(email=email, dev_token=dev_token, status="rejected", error=str(e))
        raise

    except NoVerificationCodeError as e:
        log_email_attempt(email=email, dev_token=dev_token, status="no_code", error=str(e))
        raise

    except TimeoutError as e:
        #捕获我们自己定义的硬超时，记录日志
        log_email_attempt(email=email, dev_token=dev_token, status="failed", error=str(e))
        raise

    except Exception as e:
        log_email_attempt(email=email, dev_token=dev_token, status="failed", error=str(e))
        raise

def load_run_count() -> int:
    config_path = os.path.join(os.path.dirname(__file__), "config.json")
    try:
        with open(config_path, "r", encoding="utf-8") as f:
            conf = json.load(f)
        v = conf.get("run", {}).get("count")
        if isinstance(v, int) and v >= 0:
            return v
    except Exception:
        pass
    return 10

def main():
    global run_logger
    run_logger = setup_run_logger()

    config_count = load_run_count()

    parser = argparse.ArgumentParser(description="xAI 自动注册并采集 sso")
    parser.add_argument("--count", type=int, default=config_count, help=f"执行轮数，0 表示无限循环（默认读取 config.json run.count，当前 {config_count}）")
    parser.add_argument("--output", default=DEFAULT_SSO_FILE, help="sso 输出 txt 路径")
    parser.add_argument("--extract-numbers", action="store_true", help="注册完成后额外提取页面数字文本")
    args = parser.parse_args()

    current_round = 0
    collected_sso: list = []
    try:
        start_browser()
        while True:
            if args.count > 0 and current_round >= args.count:
                break

            current_round += 1
            print(f"\n[*] 开始第 {current_round} 轮注册")
            round_succeeded = False

            try:
                result = run_single_registration(args.output, extract_numbers=args.extract_numbers)
                collected_sso.append(result["sso"])
                round_succeeded = True
            except KeyboardInterrupt:
                print("\n[Info] 收到中断信号，停止后续轮次。")
                break
            except Exception as error:
                print(f"[Error] 第 {current_round} 轮失败: {error}")
            finally:
                restart_browser()

            if args.count == 0 or current_round < args.count:
                time.sleep(2)

    finally:
        if collected_sso:
            print(f"\n[*] 注册完成，推送 {len(collected_sso)} 个 token 到 API...")
            push_sso_to_api(collected_sso)

        stop_browser()

if __name__ == "__main__":
    main()