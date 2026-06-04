# -*- coding: utf-8 -*-
"""
HTTP-GET для Cloudflare-защищённых эндпоинтов Unit (api.hyperunit.xyz).

Unit включил бот-защиту Cloudflare: обычный requests палится по TLS/JA3-фингерпринту
и получает 403 (HTML-заглушка). Здесь запрос идёт через curl_cffi с подменой
фингерпринта браузера (impersonate=chrome) — TLS как у настоящего Chrome.

Прокси берётся из переменных окружения (их на время обработки аккаунта ставит
app/net_proxy.account_proxy). Если curl_cffi не установлен — фоллбэк на обычный
requests (тогда, скорее всего, снова 403 — поставь curl_cffi: pip install curl_cffi).

Объект ответа совместим с requests: .status_code / .text / .json() / .headers.
GET идемпотентен — при обрыве прокси/сети повторяем (см. tries в get()).
"""

import os
import time

IMPERSONATE = "chrome"          # последний доступный профиль Chrome в curl_cffi
_PROXY_ENV = ("HTTPS_PROXY", "https_proxy", "ALL_PROXY", "all_proxy", "HTTP_PROXY", "http_proxy")


def _env_proxy():
    for k in _PROXY_ENV:
        v = os.environ.get(k)
        if v:
            return v
    return None


def available():
    """Стоит ли curl_cffi (браузерный TLS). False -> работаем обычным requests."""
    try:
        import curl_cffi  # noqa: F401
        return True
    except Exception:
        return False


def _get_once(url, headers, timeout):
    proxy = _env_proxy()
    try:
        from curl_cffi import requests as creq
        kw = {"headers": headers or {}, "timeout": timeout, "impersonate": IMPERSONATE}
        if proxy:
            kw["proxies"] = {"http": proxy, "https": proxy}
        return creq.get(url, **kw)
    except ImportError:
        import requests
        # requests сам подхватит прокси из переменных окружения
        return requests.get(url, headers=headers or {}, timeout=timeout)


def get(url, headers=None, timeout=25, tries=4, backoff=0.7):
    """GET с браузерным TLS-фингерпринтом (curl_cffi), через прокси из env.
    Фоллбэк на requests, если curl_cffi недоступен. -> requests-совместимый ответ.
    Сетевые/прокси-обрывы повторяет до tries раз (GET идемпотентен — без дублей)."""
    last = None
    for i in range(tries):
        try:
            return _get_once(url, headers, timeout)
        except Exception as e:        # обрыв прокси/сети — повторяем с нарастающей паузой
            last = e
            if i < tries - 1:
                time.sleep(backoff * (2 ** i))
    raise last
