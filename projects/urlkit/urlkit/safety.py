"""URL 安全检查。

agent 拿到用户提供的 URL 时，访问前应当过一遍这个模块——这是 prompt injection
与 SSRF 的第一道防线。检查项都是无依赖的纯字符串/解析逻辑。
"""

from __future__ import annotations

import ipaddress
from typing import Any

from .core import parse_url

# 允许的协议白名单。javascript:/data:/file: 等一律拒绝。
ALLOWED_SCHEMES = {"http", "https"}

# 云厂商的实例元数据地址，取到即意味着 SSRF 风险
METADATA_HOSTS = {
    "169.254.169.254",  # AWS / Azure / GCP / 阿里云 / 腾讯云
    "metadata.google.internal",
    "metadata.goog",
    "100.100.100.200",  # 阿里云
}

# 视为「内网」的 host 名
LOCAL_HOSTNAMES = {
    "localhost",
    "localhost.localdomain",
    "ip6-localhost",
    "ip6-loopback",
    "0.0.0.0",  # noqa: S104 - 用于检测，不是绑定地址
}


def _is_private_ip(host: str) -> tuple[bool, str | None]:
    """判断 host 是否为内网/保留地址。

    返回 (是否内网, 原因)
    """
    candidate = host.strip("[]")  # IPv6 在 URL 中通常带方括号
    try:
        ip = ipaddress.ip_address(candidate)
    except ValueError:
        return False, None

    if ip.is_loopback:
        return True, "回环地址"
    if ip.is_private:
        return True, "私有网段地址"
    if ip.is_link_local:
        return True, "链路本地地址"
    if ip.is_reserved or ip.is_multicast or ip.is_unspecified:
        return True, "保留或未指定地址"
    return False, None


def check_url_safety(url: str) -> dict[str, Any]:
    """检查 URL 是否存在安全隐患。

    返回
    ----
    {
        "safe": bool,          # 是否通过全部检查
        "scheme": str,
        "host": str,
        "issues": [ {"level": "error"|"warning", "code": str, "message": str} ],
    }

    >>> check_url_safety("https://example.com/a")["safe"]
    True
    >>> check_url_safety("javascript:alert(1)")["safe"]
    False
    >>> check_url_safety("http://127.0.0.1:8080/")["safe"]
    False
    """
    issues: list[dict[str, str]] = []

    try:
        parts = parse_url(url, accept_space=True)
    except Exception as exc:  # noqa: BLE001 - 解析失败本身就是风险信号
        return {
            "safe": False,
            "scheme": "",
            "host": "",
            "issues": [
                {
                    "level": "error",
                    "code": "parse_failed",
                    "message": f"URL 无法解析：{exc}",
                }
            ],
        }

    scheme = (parts.scheme or "").lower()
    host = (parts.host or "").lower()

    # 1) 协议白名单
    if not scheme:
        issues.append({
            "level": "error",
            "code": "missing_scheme",
            "message": "缺少协议头，无法判断安全性",
        })
    elif scheme not in ALLOWED_SCHEMES:
        issues.append({
            "level": "error",
            "code": "scheme_not_allowed",
            "message": f"协议 {scheme!r} 不在白名单 {sorted(ALLOWED_SCHEMES)} 内，"
                       f"可能用于脚本注入或本地文件读取",
        })

    # 2) 缺少 host —— `http://` 这种输入解析后 host 为空，
    #    若不拦下会被当作「无问题」，等于放过了一个无法定位目标的 URL。
    if not host:
        issues.append({
            "level": "error",
            "code": "missing_host",
            "message": "缺少主机名，无法确定请求目标",
        })

    # 2) 元数据地址
    if host in METADATA_HOSTS:
        issues.append({
            "level": "error",
            "code": "cloud_metadata",
            "message": f"{host} 是云厂商实例元数据地址，访问可导致凭据泄露（SSRF）",
        })

    # 3) 内网 / 回环地址
    if host in LOCAL_HOSTNAMES:
        issues.append({
            "level": "error",
            "code": "localhost",
            "message": f"{host} 指向本机，外部输入访问它属于 SSRF",
        })
    else:
        private, reason = _is_private_ip(host)
        if private:
            issues.append({
                "level": "error",
                "code": "private_ip",
                "message": f"{host} 属于{reason}，外部输入访问它属于 SSRF",
            })

    # 4) 明文凭据
    if parts.user is not None:
        issues.append({
            "level": "warning",
            "code": "inline_credentials",
            "message": "URL 中携带明文 userinfo（用户名/密码），"
                       "记录日志时会泄露凭据",
        })

    # 5) 非标准端口（非 https 且端口异常）
    if parts.port is not None and scheme == "https" and parts.port not in (443, 8443):
        issues.append({
            "level": "warning",
            "code": "unusual_port",
            "message": f"https 使用非标准端口 {parts.port}，请确认目标可信",
        })

    return {
        "safe": not any(i["level"] == "error" for i in issues),
        "scheme": scheme,
        "host": host,
        "issues": issues,
    }
