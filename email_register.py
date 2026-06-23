from __future__ import annotations

import json
import logging
import re
import time
import urllib.parse
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

try:
    from curl_cffi import requests as curl_requests
except ImportError:
    curl_requests = None

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# ============================================================
# 基础配置
# ============================================================
_config_path = Path(__file__).parent / "config.json"
_conf: Dict[str, Any] = {}
if _config_path.exists():
    with _config_path.open("r", encoding="utf-8") as _f:
        _conf = json.load(_f)

PROXY = str(_conf.get("proxy", ""))
_temp_email_cache: Dict[str, str] = {}

_global_session = None
_global_use_cffi = False

def _get_session():
    global _global_session, _global_use_cffi
    if _global_session is not None:
        return _global_session, _global_use_cffi

    if curl_requests:
        _global_session = curl_requests.Session()
        _global_session.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
            "Accept": "application/json, text/plain, */*",
            "Origin": "https://smailpro.com",
            "Referer": "https://smailpro.com/",
        })
        if PROXY:
            _global_session.proxies = {"http": PROXY, "https": PROXY}
        _global_use_cffi = True
    else:
        import urllib3
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
        _global_session = requests.Session()
        retry = Retry(total=3, backoff_factor=1, status_forcelist=[429, 500, 502, 503, 504])
        adapter = HTTPAdapter(max_retries=retry)
        _global_session.mount("https://", adapter)
        _global_session.mount("http://", adapter)
        _global_session.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Accept": "application/json, text/plain, */*",
        })
        if PROXY:
            _global_session.proxies = {"http": PROXY, "https": PROXY}
        _global_use_cffi = False

    return _global_session, _global_use_cffi


def _do_request(session, use_cffi, method, url, **kwargs):
    if use_cffi:
        kwargs.setdefault("impersonate", "chrome131")
    return getattr(session, method)(url, **kwargs)


# ============================================================
# Smailpro 核心流：获取 Payload 与创建
# ============================================================

def get_smailpro_payload(target_url: str) -> str:
    """去 smailpro 换取签名的 JWT Payload"""
    session, use_cffi = _get_session()
    encoded_url = urllib.parse.quote(target_url, safe="")
    payload_api = f"https://smailpro.com/app/payload?url={encoded_url}"
    
    res = _do_request(session, use_cffi, "get", payload_api, timeout=15)
    if res.status_code == 200:
        val = res.text.strip()
        if val:
            return val
    raise Exception(f"无法获取 Smailpro 签名 Payload: HTTP {res.status_code} - {res.text[:100]}")


def create_temp_email() -> Tuple[str, str, str]:
    """
    创建邮箱，并将关键的原始有效 payload 传出，作为后续轮询的身份令牌
    """
    create_url = "https://api.sonjj.com/v1/temp_email/create"
    try:
        # 1. 这一轮仅获取一次基础 Payload
        payload = get_smailpro_payload(create_url)
        
        # 2. 带着这个 Payload 去请求真实创建
        api_url = f"{create_url}?payload={payload}"
        session, use_cffi = _get_session()
        res = _do_request(session, use_cffi, "get", api_url, timeout=15)
        
        if res.status_code == 200:
            data = res.json()
            email = data.get("email")
            if email:
                print(f"[*] Smailpro 临时邮箱创建成功: {email}")
                # ?? 关键修改：把第一步换到的 payload 直接当成 mail_token 返回给主脚本
                return email, "smailpro_no_pass", payload
                
        raise Exception(f"创建请求未返回有效邮箱: HTTP {res.status_code} - {res.text[:100]}")
    except Exception as e:
        raise Exception(f"Smailpro 创建邮箱失败: {e}")


# ============================================================
# 适配层：对外导出标准接口
# ============================================================

def get_email_and_token() -> Tuple[Optional[str], Optional[str]]:
    """供 DrissionPage_example.py 调用"""
    try:
        email, _password, mail_token = create_temp_email()
        if email and mail_token:
            _temp_email_cache[email] = mail_token
            return email, mail_token
    except Exception as e:
        print(f"[-] 获取邮箱遇到阻碍: {e}")
    return None, None


def get_oai_code(dev_token: str, email: str, timeout: int = 15) -> Optional[str]:
    """
    高效轮询收件箱
    dev_token: 主脚本传回来的，也就是创建邮箱时锁定的那个合法 payload
    """
    start = time.time()
    seen_ids = set()
    
    print(f"[*] 开始轮询 Smailpro 收件箱 ({email}) ...")
    session, use_cffi = _get_session()
    
    # ?? 完美闭环：轮询期间只请求 api.sonjj.com，完全不碰 smailpro.com 官网
    inbox_api = f"https://api.sonjj.com/v1/temp_email/inbox?payload={dev_token}"
    
    while time.time() - start < timeout:
        try:
            res = _do_request(session, use_cffi, "get", inbox_api, timeout=15)
            
            if res.status_code == 200:
                data = res.json()
                print(f"data is {data}")
                messages = data.get("messages", [])
                
                for msg in messages:
                    # 识别唯一邮件，防止重复处理（增加 Smailpro 专用的 mid 字段）
                    msg_id = msg.get("mid") or msg.get("id") or msg.get("uid") or str(msg.get("textDate", ""))
                    if not msg_id or msg_id in seen_ids:
                        continue
                    seen_ids.add(msg_id)
                    
                    # 提取验证码
                    content = json.dumps(msg, ensure_ascii=False)
                    code = extract_verification_code(content)
                    if code:
                        print(f"[*] ?? 成功从 Smailpro 提取到验证码: {code}")
                        return code.replace("-", "")
            else:
                # 暴露出真实的非 200 错误代码，便于定位网络或节点问题
                print(f"[Warn] 轮询期间接口返回异常状态码: HTTP {res.status_code}")
                        
        except Exception as e:
            print(f"[Debug] 轮询期间发生网络抖动: {e}")
            
        time.sleep(3)
        
    print("[-] 轮询 Smailpro 收件箱超时")
    return None


# ============================================================
# 正则提取器
# ============================================================
def extract_verification_code(content: str) -> Optional[str]:
    if not content:
        return None
    m = re.search(r"(?<![A-Z0-9-])([A-Z0-9]{3}-[A-Z0-9]{3})(?![A-Z0-9-])", content)
    if m:
        return m.group(1)
    m = re.search(r"(?:verification code|验证码|your code)[:\s]*[<>\s]*([A-Z0-9]{3}-[A-Z0-9]{3})\b", content, re.IGNORECASE)
    if m:
        return m.group(1)
    m = re.search(r"background-color:\s*#F3F3F3[^>]*>[\s\S]*?([A-Z0-9]{3}-[A-Z0-9]{3})[\s\S]*?</p>", content)
    if m:
        return m.group(1)
    m = re.search(r"Subject:.*?(\d{6})", content)
    if m and m.group(1) != "177010":
        return m.group(1)
    for code in re.findall(r">\s*(\d{6})\s*<", content):
        if code != "177010":
            return code
    for code in re.findall(r"(?<![&#\d])(\d{6})(?![&#\d])", content):
        if code != "177010":
            return code
    return None