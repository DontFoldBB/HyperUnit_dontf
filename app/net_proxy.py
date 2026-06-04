# -*- coding: utf-8 -*-
"""
Пер-аккаунт прокси: весь HTTP-трафик аккаунта (Unit, Hyperliquid, EVM RPC, цены)
идёт через прокси этого аккаунта из wallets.xlsx (столбец C).

Реализовано через переменные окружения HTTP(S)_PROXY — их уважают requests, web3 и
SDK hyperliquid (trust_env=True по умолчанию), поэтому прокидывать прокси в каждый
HTTP-клиент по отдельности не нужно.

Bitget по умолчанию ИСКЛЮЧЁН из прокси (NO_PROXY=api.bitget.com): его API завязан на
IP-whitelist (один аккаунт на все кошельки), поэтому он должен ходить с основного
(вайтлистнутого) IP. Пустить Bitget тоже через прокси: proxy_bitget=true в config.json.
"""

import os
from contextlib import contextmanager

BITGET_HOST = "api.bitget.com"
_PROXY_KEYS = ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY",
               "http_proxy", "https_proxy", "all_proxy", "no_proxy")


def normalize_proxy(s):
    """Привести строку прокси к URL scheme://[user:pass@]host:port.
    Принимает: готовый URL (http/https/socks5); host:port; user:pass@host:port;
    host:port:user:pass (частый формат провайдеров). Пусто -> ''."""
    s = (s or "").strip()
    if not s:
        return ""
    if "://" in s:
        return s
    if "@" in s:                              # user:pass@host:port без схемы
        return "http://" + s
    parts = s.split(":")
    if len(parts) == 4:                       # host:port:user:pass
        host, port, user, pwd = parts
        return f"http://{user}:{pwd}@{host}:{port}"
    return "http://" + s                      # host:port (без авторизации)


def mask_proxy(proxy):
    """Спрятать пароль прокси для логов: http://user:***@host:port."""
    if not proxy:
        return "—"
    try:
        if "@" in proxy:
            head, tail = proxy.rsplit("@", 1)
            scheme, sep, creds = head.partition("://")
            if sep:
                user = creds.split(":", 1)[0]
                return f"{scheme}://{user}:***@{tail}"
            user = head.split(":", 1)[0]
            return f"{user}:***@{tail}"
    except Exception:
        pass
    return proxy


def proxy_exit_ip(proxy, timeout=15):
    """Exit-IP через прокси (проверка живости). -> str; бросает при сбое."""
    import requests
    r = requests.get("https://api.ipify.org?format=json",
                     proxies={"http": proxy, "https": proxy}, timeout=timeout)
    return r.json().get("ip")


@contextmanager
def account_proxy(proxy, bitget_through_proxy=False):
    """На время блока направить весь HTTP-трафик через proxy (через env-переменные).
    Bitget исключается (NO_PROXY), если bitget_through_proxy=False.
    Пустой proxy -> ничего не меняем (идём с основного IP). yield: bool (включён ли прокси)."""
    if not proxy:
        yield False
        return
    saved = {k: os.environ.get(k) for k in _PROXY_KEYS}
    try:
        for k in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY",
                  "http_proxy", "https_proxy", "all_proxy"):
            os.environ[k] = proxy
        if bitget_through_proxy:
            os.environ.pop("NO_PROXY", None)
            os.environ.pop("no_proxy", None)
        else:
            os.environ["NO_PROXY"] = BITGET_HOST
            os.environ["no_proxy"] = BITGET_HOST
        yield True
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
