# -*- coding: utf-8 -*-
"""
Единая загрузка настроек HyperUnit.

Источники (оба лежат в config/):
  * .env         — СЕКРЕТЫ: приватник, ключи Bitget, RPC Ethereum.
  * config.json  — ПАРАМЕТРЫ всех 4 стадий (можно с комментариями // # /* */).

Приоритет значений: .env  >  переменные окружения  >  пусто.
config.json парсится «мягко»: разрешены комментарии и висячие запятые, чтобы
файл было удобно заполнять руками (под будущий UI это просто JSON).
"""

import os
import re
import json
import random

import paths  # noqa: F401  (настраивает sys.path; нужен для деривации адреса)

CONFIG_DIR = paths.CONFIG_DIR
ENV_PATH = os.path.join(CONFIG_DIR, ".env")
CONFIG_PATH = os.path.join(CONFIG_DIR, "config.json")


class ConfigError(Exception):
    pass


# --------------------------------------------------------------------------- #
#  .env                                                                       #
# --------------------------------------------------------------------------- #
def load_env(path=ENV_PATH):
    """Прочитать .env в словарь (KEY=VALUE, # — комментарий). Без побочных эффектов."""
    data = {}
    if not os.path.isfile(path):
        return data
    with open(path, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            data[k.strip()] = v.strip().strip('"').strip("'")
    return data


# --------------------------------------------------------------------------- #
#  config.json с поддержкой комментариев / висячих запятых (JSONC)            #
# --------------------------------------------------------------------------- #
def _strip_jsonc(text):
    """Убрать // # /* */ комментарии (не трогая строки) и висячие запятые."""
    out = []
    i, n = 0, len(text)
    in_str, quote = False, ""
    while i < n:
        c = text[i]
        if in_str:
            out.append(c)
            if c == "\\" and i + 1 < n:       # экранированный символ внутри строки
                out.append(text[i + 1]); i += 2; continue
            if c == quote:
                in_str = False
            i += 1; continue
        if c in ('"', "'"):
            in_str, quote = True, c
            out.append(c); i += 1; continue
        if c == "/" and i + 1 < n and text[i + 1] == "/":   # // ...
            i += 2
            while i < n and text[i] not in "\r\n":
                i += 1
            continue
        if c == "#":                                        # # ...
            i += 1
            while i < n and text[i] not in "\r\n":
                i += 1
            continue
        if c == "/" and i + 1 < n and text[i + 1] == "*":   # /* ... */
            i += 2
            while i + 1 < n and not (text[i] == "*" and text[i + 1] == "/"):
                i += 1
            i += 2
            continue
        out.append(c); i += 1
    s = "".join(out)
    s = re.sub(r",(\s*[}\]])", r"\1", s)      # висячие запятые перед } или ]
    return s


def load_json(path=CONFIG_PATH):
    if not os.path.isfile(path):
        raise ConfigError(
            f"Не найден config.json: {path}\n"
            f"Скопируй config.example.json -> config.json и заполни."
        )
    with open(path, "r", encoding="utf-8") as fh:
        raw = fh.read()
    try:
        return json.loads(_strip_jsonc(raw))
    except json.JSONDecodeError as e:
        raise ConfigError(f"Ошибка в config.json (после удаления комментариев): {e}")


# --------------------------------------------------------------------------- #
#  Единый объект конфигурации                                                 #
# --------------------------------------------------------------------------- #
def _looks_like_pk(s):
    t = (s or "").strip()
    if t[:2].lower() == "0x":
        t = t[2:]
    return len(t) == 64 and all(c in "0123456789abcdefABCDEF" for c in t)


def _pick_num(spec, integer=False):
    """Вилка -> конкретное число.
    Принимает: число (5) | "min-max" (можно с %, напр. "90-95") | [min,max].
    Диапазон -> случайное значение в нём; integer=True -> целое (для плеча),
    иначе округление до 2 знаков (чтобы сумма была неровной). Одиночное число — как
    есть. Непонятный ввод возвращается без изменений (стадия выдаст свою ошибку)."""
    try:
        if isinstance(spec, (list, tuple)) and len(spec) == 2:
            lo, hi, rng = float(spec[0]), float(spec[1]), True
        elif isinstance(spec, str):
            s = spec.strip().rstrip("%").strip()
            if "-" in s[1:]:                      # диапазон "a-b" (а не одиночное число)
                lo, hi = (float(x) for x in s.split("-", 1))
                rng = True
            else:
                lo = hi = float(s); rng = False
        else:
            lo = hi = float(spec); rng = False
    except Exception:
        return spec
    if integer:
        a, b = int(round(min(lo, hi))), int(round(max(lo, hi)))
        return random.randint(a, b) if rng else int(round(lo))
    return round(random.uniform(lo, hi), 2) if rng else round(lo, 2)


class Config:
    """Готовая к использованию конфигурация всех стадий."""

    def __init__(self, env, js):
        self.env = env
        self.raw = js

        # --- секреты из .env ---
        self.private_key = (env.get("PRIVATE_KEY") or os.environ.get("PRIVATE_KEY") or "").strip()
        self.eth_rpc_url = (env.get("ETH_RPC_URL") or os.environ.get("ETH_RPC_URL") or "").strip()
        self.bitget = {
            "api_key": (env.get("BITGET_API_KEY") or os.environ.get("BITGET_API_KEY") or "").strip(),
            "api_secret": (env.get("BITGET_API_SECRET") or os.environ.get("BITGET_API_SECRET") or "").strip(),
            "api_passphrase": (env.get("BITGET_API_PASSPHRASE") or os.environ.get("BITGET_API_PASSPHRASE") or "").strip(),
        }

        # --- режим ---
        # Плоский формат: главное — на верхнем уровне, редкое — в "advanced".
        adv = js.get("advanced", {}) or {}
        mods = js.get("modules", {}) or {}

        self.live = True            # тест-режим убран — запуск ВСЕГДА реальный
        # пауза между модулями (как человек переключает вкладки): число или [мин,макс] сек
        self.module_gap_sec = adv.get("module_gap_sec", [5, 15])
        # батч: путь к Excel со списком кошельков ("" = combined/wallets.xlsx)
        self.wallets_file = (js.get("batch_wallets_file") or "").strip()
        # перемешивать ли порядок кошельков из wallets.xlsx на каждом запуске
        self.randomize_wallets = bool(js.get("randomize_wallets", False))
        # лимитные ордера для перпов/HIP-3 (спот всегда лимиткой). По умолчанию выкл = маркет.
        self.limit_orders = bool(js.get("limit_orders", False))
        # резюме после обрыва: пропускать аккаунты, уже записанные в output/done_accounts.txt
        self.skip_done_accounts = bool(js.get("skip_done_accounts", True))
        # прокси аккаунта (из wallets.xlsx, столбец C) — на каждый кошелёк свой; "" = основной IP
        self.proxy = ""
        # пускать ли Bitget тоже через прокси. По умолчанию НЕТ (его API на IP-whitelist — основной IP)
        self.proxy_bitget = bool(js.get("proxy_bitget", False))
        # выключать DEX abstraction перед торговлей (стандартный режим маржи). ПО УМОЛЧАНИЮ true —
        # нужно для HIP-3 (бот фондит площадку напрямую spot→DEX). false = режим аккаунта не трогать.
        self.disable_dex_abstraction = bool(js.get("disable_dex_abstraction", True))
        # Builder-комиссия Hyperliquid (монетизация): в конфиге только тумблер;
        # адрес и ставка вшиты в lib/circle.py (BUILDER_ADDRESS/BUILDER_FEE).
        self.builder = {"enabled": bool(js.get("builder_codes", False))}

        # --- вкл/выкл модулей ---
        self.enabled = {
            "bitget": bool(mods.get("bitget", True)),
            "deposit": bool(mods.get("deposit", True)),
            "trade": bool(mods.get("trade", True)),
            "withdraw": bool(mods.get("withdraw", True)),
            "bitget_return": bool(mods.get("bitget_return", True)),
        }

        # --- параметры стадий (внутренний вид прежний; читаем из плоских ключей) ---
        self.bitget_cfg = {
            "amount_eth": js.get("bitget_amount_eth", "0.01"),
            "wait_arrival": bool(adv.get("bitget_wait_arrival", True)),
            "wait_timeout_sec": adv.get("bitget_wait_timeout_sec", 900),
            # вывод с Bitget: ждать разморозки свежего депозита для вывода, мин (деньги с прошлого
            # кошелька зачислились, но залочены) — авто-повтор при code 13008 «withdrawable amount: 0».
            "withdraw_unlock_wait_min": adv.get("bitget_withdraw_unlock_wait_min", 30),
        }
        self.deposit_cfg = {
            "percent": js.get("deposit_percent", 80),
            "wait_credit": bool(adv.get("deposit_wait_credit", True)),
            "wait_timeout_min": adv.get("deposit_wait_timeout_min", 0),
        }
        self.trade_cfg = {
            "hip3_assets": js.get("trade_hip3", []),
            "perp": js.get("trade_perp", "none"),
            "single_coin": js.get("trade_single_coin", ""),
            "pct": js.get("trade_margin_pct", 50),
            "leverage": js.get("trade_leverage", 2),
            "target_hip3": js.get("trade_target_hip3", 0),
            "target_perp": js.get("trade_target_perp", 0),
            "hold_minutes": js.get("trade_hold_minutes", 0.2),
            "gap_minutes": js.get("trade_gap_minutes", 0.1),
            "reserve_usdc": adv.get("trade_reserve_usdc", 1.0),
            "size_jitter": adv.get("trade_size_jitter", 0.35),
        }
        self.withdraw_cfg = {
            "amount": js.get("withdraw_amount", "all"),
            "wait": bool(adv.get("withdraw_wait", True)),
            "wait_timeout_min": adv.get("withdraw_wait_timeout_min", 30.0),
            "poll_sec": adv.get("withdraw_poll_sec", 20),
        }
        self.return_cfg = {
            "percent": js.get("bitget_return_percent", 100),
            "address": js.get("bitget_return_address", ""),
            "wait": bool(adv.get("bitget_return_wait", True)),
            "wait_credit": bool(adv.get("bitget_return_wait_credit", True)),
        }

        # --- адрес кошелька (для показа) ---
        self.address = None
        if _looks_like_pk(self.private_key):
            try:
                from eth_account import Account
                self.address = Account.from_key(self.private_key).address
            except Exception:
                self.address = None

    # --- проверки готовности для конкретной стадии ---
    def require_private_key(self):
        if not self.private_key:
            raise ConfigError("В .env не задан PRIVATE_KEY (нужен всем стадиям).")
        if not _looks_like_pk(self.private_key):
            raise ConfigError("PRIVATE_KEY в .env не похож на приватный ключ (64 hex / 0x...).")

    def require_bitget(self):
        miss = [k for k, v in self.bitget.items() if not v]
        if miss:
            raise ConfigError("В .env не заданы ключи Bitget: "
                              + ", ".join("BITGET_" + m.upper() for m in miss))

    def require_rpc(self):
        if not self.eth_rpc_url:
            raise ConfigError(
                "В .env не задан ETH_RPC_URL — депозит шлёт ETH-транзакцию и без RPC "
                "(Alchemy/Infura/свой нод) работать не может."
            )


def load(env_path=ENV_PATH, config_path=CONFIG_PATH):
    return Config(load_env(env_path), load_json(config_path))


def clone_for_wallet(cfg, private_key, bitget_address, proxy=""):
    """Копия конфига под конкретный кошелёк батча: свой приватник, адрес возврата на Bitget и прокси."""
    import copy
    w = copy.copy(cfg)
    w.proxy = (proxy or "").strip()
    w.bitget_cfg = dict(cfg.bitget_cfg)
    w.deposit_cfg = dict(cfg.deposit_cfg)
    w.trade_cfg = dict(cfg.trade_cfg)
    w.withdraw_cfg = dict(cfg.withdraw_cfg)
    w.return_cfg = dict(cfg.return_cfg)
    w.builder = dict(getattr(cfg, "builder", {}))
    # Вилки ("90-95", [50,95]) -> своё число НА КАЖДЫЙ кошелёк: неровные и разные суммы.
    w.deposit_cfg["percent"] = _pick_num(cfg.deposit_cfg.get("percent"))
    w.trade_cfg["pct"] = _pick_num(cfg.trade_cfg.get("pct"))
    w.trade_cfg["target_hip3"] = _pick_num(cfg.trade_cfg.get("target_hip3"))
    w.trade_cfg["target_perp"] = _pick_num(cfg.trade_cfg.get("target_perp"))
    w.trade_cfg["leverage"] = _pick_num(cfg.trade_cfg.get("leverage"), integer=True)
    w.return_cfg["percent"] = _pick_num(cfg.return_cfg.get("percent"))
    w.private_key = (private_key or "").strip()
    w.return_cfg["address"] = (bitget_address or "").strip()
    w.address = None
    if _looks_like_pk(w.private_key):
        try:
            from eth_account import Account
            w.address = Account.from_key(w.private_key).address
        except Exception:
            w.address = None
    return w


# --------------------------------------------------------------------------- #
#  Сохранение переключателей обратно в config.json (БЕЗ потери комментариев)  #
# --------------------------------------------------------------------------- #
def _set_top_bool(text, key, value):
    """Заменить первое "key": true/false на верхнем уровне (для "live")."""
    val = "true" if value else "false"
    new, n = re.subn(r'("%s"\s*:\s*)(?:true|false)' % re.escape(key),
                     r"\g<1>" + val, text, count=1)
    return new if n else text


def _set_module_bool(text, key, value):
    """Заменить "key": true/false внутри блока "modules" (без вложенных скобок)."""
    val = "true" if value else "false"
    m = re.search(r'"modules"\s*:\s*\{', text)
    if not m:
        return text
    start = m.end()
    end = text.find("}", start)
    if end == -1:
        end = len(text)
    region = text[start:end]
    new_region, n = re.subn(r'("%s"\s*:\s*)(?:true|false)' % re.escape(key),
                            r"\g<1>" + val, region, count=1)
    if n == 0:
        new_region = '\n    "%s": %s,' % (key, val) + region
    return text[:start] + new_region + text[end:]


def _set_or_add_top_bool(text, key, value):
    """Заменить top-level "key": true/false; если ключа нет — добавить сразу после '{'."""
    val = "true" if value else "false"
    new, n = re.subn(r'("%s"\s*:\s*)(?:true|false)' % re.escape(key),
                     r"\g<1>" + val, text, count=1)
    if n:
        return new
    idx = text.find("{")
    if idx == -1:
        return text
    return text[:idx + 1] + ('\n  "%s": %s,' % (key, val)) + text[idx + 1:]


def save_toggles(cfg, path=CONFIG_PATH):
    """Сохранить modules.* и randomize_wallets в config.json, сохранив комментарии и остальное."""
    with open(path, "r", encoding="utf-8") as fh:
        text = fh.read()
    for key in ("bitget", "deposit", "trade", "withdraw", "bitget_return"):
        text = _set_module_bool(text, key, cfg.enabled.get(key, True))
    text = _set_or_add_top_bool(text, "randomize_wallets",
                                getattr(cfg, "randomize_wallets", False))
    text = _set_or_add_top_bool(text, "skip_done_accounts",
                                getattr(cfg, "skip_done_accounts", True))
    text = _set_or_add_top_bool(text, "limit_orders",
                                getattr(cfg, "limit_orders", False))
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)
