#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Вывод ETH с Hyperliquid обратно в сеть Ethereum через мост Unit (hyperunit.xyz).
Обратная сторона hyperunit_deposit.py.

Как это работает (то же, что делает сайт https://app.hyperunit.xyz/ → Withdraw):
  1. У Unit запрашивается «адрес вывода» на самой Hyperliquid, привязанный к вашему
     адресу назначения в сети Ethereum:
        GET https://api.hyperunit.xyz/gen/hyperliquid/ethereum/eth/<ETH-адрес>
  2. Ответ содержит сам HL-адрес и подписи гардианов (3 ноды). Скрипт ПРОВЕРЯЕТ
     эти подписи (ECDSA P-256) по захардкоженным публичным ключам — чтобы быть
     уверенным, что адрес настоящий, а не подменён по дороге. Нужно >= 2 из 3.
  3. На этот проверенный HL-адрес отправляется ваш UETH («Unit Ethereum» —
     представление ETH на спот-балансе Hyperliquid) обычным спот-переводом
     (действие spotSend, подписывается по схеме Hyperliquid; газа на L1 нет).
  4. После финализации на Hyperliquid (Unit ждёт ~5000 блоков, ~7 минут) мост
     отправляет ETH на ваш Ethereum-адрес, удержав небольшую комиссию (см.
     /v2/estimate-fees → ethereum.withdrawal-fee-in-units, сейчас ~0.0000633 ETH).

Возможности:
  * Один кошелёк (--key или PRIVATE_KEY в .env) или пачка (--wallets файл).
  * По умолчанию выводит ВЕСЬ доступный ETH (UETH) с каждого кошелька. Можно задать
    фиксированную сумму (0.05), процент (50%) или случайный процент (50-70%).
  * Перед отправкой — проверка подписей гардианов и сводная таблица с подтверждением.
  * Случайные паузы между кошельками в пачке (--delay).
  * Режим --dry-run: всё посчитать, проверить и подписать, но НЕ отправлять.
  * --wait: дождаться зачисления на Ethereum (живой статус операции Unit).

Запуск (один кошелёк, вывести ВЕСЬ ETH на свой же Ethereum-адрес):
    python hyperliquid_withdrawal.py

Запуск (пачка, по 100% с каждого на свои адреса):
    python hyperliquid_withdrawal.py --wallets wallets.txt --yes

ВАЖНО:
  * RPC Ethereum НЕ нужен — перевод происходит внутри Hyperliquid (без газа на L1).
  * Минимум: у Unit для ETH это ~0.007 (тянется live с сайта). Меньше минимума мост
    может не зачислить — скрипт по умолчанию не отправляет такую сумму (--min-eth 0
    чтобы снять порог на свой риск).
  * Сначала прогоните --dry-run.
  * Приватные ключи нигде не логируются; .env и wallets.txt держите вне git.
"""

import os
import sys
import csv
import json
import time
import random
import argparse
from datetime import datetime, timezone
from decimal import Decimal, ROUND_DOWN, InvalidOperation

try:
    import requests
except ImportError:
    sys.exit("Нужен модуль requests:  pip install requests")

try:
    from eth_account import Account
    from eth_account.messages import encode_typed_data
    from eth_utils import to_hex, to_checksum_address
except ImportError:
    sys.exit("Нужен модуль eth-account:  pip install eth-account")

try:
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature
    from cryptography.hazmat.primitives import hashes
    from cryptography.exceptions import InvalidSignature
except ImportError:
    sys.exit("Нужен модуль cryptography:  pip install cryptography")

import base64
import re

# Корректный вывод кириллицы в консоли Windows
try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass


# --------------------------------------------------------------------------- #
#  Константы Unit                                                             #
# --------------------------------------------------------------------------- #
API_BASE = "https://api.hyperunit.xyz"
API_BASE_TESTNET = "https://api.hyperunit-testnet.xyz"

# Hyperliquid API (info + exchange)
HL_API = "https://api.hyperliquid.xyz"
HL_API_TESTNET = "https://api.hyperliquid-testnet.xyz"

# Публичные ключи гардианов (uncompressed EC point, hex). Источник:
# https://docs.hyperunit.xyz/developers/key-addresses/mainnet
MAINNET_GUARDIANS = [
    {"nodeId": "unit-node",  "publicKey": "04dc6f89f921dc816aa69b687be1fcc3cc1d48912629abc2c9964e807422e1047e0435cb5ba0fa53cb9a57a9c610b4e872a0a2caedda78c4f85ebafcca93524061"},
    {"nodeId": "hl-node",    "publicKey": "048633ea6ab7e40cdacf37d1340057e84bb9810de0687af78d031e9b07b65ad4ab379180ab55075f5c2ebb96dab30d2c2fab49d5635845327b6a3c27d20ba4755b"},
    {"nodeId": "field-node", "publicKey": "04ae2ab20787f816ea5d13f36c4c4f7e196e29e867086f3ce818abb73077a237f841b33ada5be71b83f4af29f333dedc5411ca4016bd52ab657db2896ef374ce99"},
]
TESTNET_GUARDIANS = [
    {"nodeId": "node-1",          "publicKey": "04bab844e8620c4a1ec304df6284cd6fdffcde79b3330a7bffb1e4cecfee72d02a7c1f3a4415b253dc8d6ca2146db170e1617605cc8a4160f539890b8a24712152"},
    {"nodeId": "hl-node-testnet", "publicKey": "04502d20a0d8d8aaea9395eb46d50ad2d8278c1b3a3bcdc200d531253612be23f5f2e9709bf3a3a50d1447281fa81aca0bf2ac2a6a3cb8a12978381d73c24bb2d9"},
    {"nodeId": "field-node",      "publicKey": "04e674a796ff01d6b74f4ee4079640729797538cdb4926ec333ce1bd18414ef7f22c1a142fd76dca120614045273f30338cd07d79bc99872c76151756aaec0f8e8"},
]
GUARDIAN_THRESHOLD = 2  # сколько подписей из 3 должно сойтись

# Hyperliquid spot-токен для ETH через Unit (имя на бирже). tokenId тянем live.
HL_TOKEN_NAME = "UETH"

# Минимум вывода у Unit отдельно не публикуется; берём минимум ассета ETH с сайта.
APP_BUNDLE_URL = "https://app.hyperunit.xyz/app.bundle.js"
MIN_CACHE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".min_cache.json")
FALLBACK_MIN = Decimal("0.007")  # запасной минимум ETH, если live недоступен

# Журнал выводов: объём прогнанных средств + комиссии. CSV рядом со скриптом.
LEDGER_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "withdrawals.csv")
LEDGER_COLUMNS = [
    "timestamp_utc", "network", "sender", "eth_dest", "method",
    "amount_eth", "eth_price_usd", "amount_usd",
    "unit_fee_eth", "net_eth", "activation_gas_usdc",
    "status", "ethereum_tx", "nonce",
]


class HyperunitError(Exception):
    pass


class HyperliquidError(Exception):
    pass


# --------------------------------------------------------------------------- #
#  .env рядом со скриптом (минимальный парсер, без зависимостей)              #
# --------------------------------------------------------------------------- #
def _load_dotenv():
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    if not os.path.isfile(path):
        return
    with open(path, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, val = line.split("=", 1)
            os.environ.setdefault(key.strip(), val.strip().strip('"').strip("'"))


# --------------------------------------------------------------------------- #
#  HTTP к Unit API                                                            #
# --------------------------------------------------------------------------- #
def api_get(path, testnet=False, timeout=25):
    base = API_BASE_TESTNET if testnet else API_BASE
    resp = requests.get(base + path, headers={"Accept": "application/json"}, timeout=timeout)
    try:
        data = resp.json()
    except ValueError:
        raise HyperunitError(f"Unit вернул не JSON (HTTP {resp.status_code}): {resp.text[:200]}")
    if resp.status_code >= 400:
        raise HyperunitError(f"Unit API HTTP {resp.status_code}: {data}")
    return data


def estimate_fees(testnet=False):
    """Текущие комиссии/ETA по сетям. Возвращает блок 'ethereum' или {}."""
    try:
        data = api_get("/v2/estimate-fees", testnet)
        return data.get("ethereum", {}) if isinstance(data, dict) else {}
    except Exception:
        return {}


def gen_withdraw_address(eth_dest, testnet=False):
    """Запросить HL-адрес вывода для адреса назначения в сети Ethereum."""
    data = api_get(f"/gen/hyperliquid/ethereum/eth/{eth_dest}", testnet)
    if not isinstance(data, dict) or "address" not in data:
        raise HyperunitError(f"Unit не вернул адрес вывода: {data}")
    return data


# --------------------------------------------------------------------------- #
#  Минимум вывода (тянем live из конфига сайта — поле ассета ETH)             #
# --------------------------------------------------------------------------- #
def _parse_min_from_bundle(js, asset_symbol):
    i = js.find(f'unitAssetSymbol:"{asset_symbol}"')
    if i < 0:
        return None
    m = re.search(r"minDepositAmountToken:([0-9.eE+\-]+)", js[i:i + 2000])
    if not m:
        return None
    try:
        return Decimal(m.group(1))
    except (InvalidOperation, ValueError):
        return None


def fetch_min(asset="ETH", max_age=43200):
    """
    Минимальная сумма ассета (в токене) с сайта Unit. У Unit нет отдельного API
    под минимумы — фронт хранит их в app.bundle.js. Кэш ~12 ч в .min_cache.json.
    Возвращает (value: Decimal|None, source: str).
    """
    asset = asset.upper()
    cache = {"mins": {}}
    try:
        with open(MIN_CACHE_FILE, "r", encoding="utf-8") as fh:
            cache = json.load(fh)
        if time.time() - cache.get("ts", 0) < max_age and asset in cache.get("mins", {}):
            return Decimal(str(cache["mins"][asset])), "кэш"
    except Exception:
        pass
    try:
        js = requests.get(APP_BUNDLE_URL, timeout=40, headers={"User-Agent": "Mozilla/5.0"}).text
        val = _parse_min_from_bundle(js, asset)
        if val is not None:
            mins = cache.get("mins", {}) if isinstance(cache, dict) else {}
            mins[asset] = float(val)
            try:
                with open(MIN_CACHE_FILE, "w", encoding="utf-8") as fh:
                    json.dump({"ts": time.time(), "mins": mins}, fh)
            except Exception:
                pass
            return val, "live с app.hyperunit.xyz"
    except Exception:
        pass
    if asset == "ETH":
        return FALLBACK_MIN, "запасное значение (live недоступен)"
    return None, "не найдено"


# --------------------------------------------------------------------------- #
#  Проверка подписей гардианов (ECDSA P-256 / secp256r1, SHA-256)            #
# --------------------------------------------------------------------------- #
def p256_verify(pubkey_hex, message_bytes, sig_b64):
    """True, если подпись (base64, 64 байта r||s) верна для сообщения."""
    try:
        pub = ec.EllipticCurvePublicKey.from_encoded_point(
            ec.SECP256R1(), bytes.fromhex(pubkey_hex)
        )
        raw = base64.b64decode(sig_b64)
        if len(raw) != 64:
            return False
        r = int.from_bytes(raw[:32], "big")
        s = int.from_bytes(raw[32:], "big")
        pub.verify(encode_dss_signature(r, s), message_bytes, ec.ECDSA(hashes.SHA256()))
        return True
    except (InvalidSignature, ValueError, Exception):
        return False


def _addr_variants(s):
    """Адрес мог быть нормализован нодой (lower/checksum) — пробуем варианты."""
    out = []
    cand = [s, s.lower()]
    try:
        cand.append(to_checksum_address(s))
    except Exception:
        pass
    for v in cand:
        if v and v not in out:
            out.append(v)
    return out


def _messages_for(node_id, eth_dest, hl_addr):
    """
    Варианты подписываемой строки для вывода hyperliquid→ethereum (ETH).
    Подтверждено на живом mainnet-API (3/3): новый формат, coinType=ethereum,
    dstChain=ethereum:  "{nodeId}:user-ethereum-ethereum-{eth_dest}-{hl_addr}".
    Остальные — запасные на случай смены формата нодами.
    """
    yield f"{node_id}:user-ethereum-ethereum-{eth_dest}-{hl_addr}".encode("utf-8")
    yield f"{node_id}:user-eth-ethereum-{eth_dest}-{hl_addr}".encode("utf-8")
    yield f"{node_id}:{eth_dest}-ethereum-eth-{hl_addr}-hyperliquid-withdraw".encode("utf-8")


def verify_signatures(eth_dest, hl_addr, signatures, guardians):
    """
    Проверка, что HL-адрес вывода подписан гардианами.
    Возвращает (ok: bool, verified_count: int, node_ids: list[str]).
    Перебираем варианты регистра адресов, т.к. нода могла их нормализовать.
    """
    if not isinstance(signatures, dict):
        return False, 0, []
    best_count, best_nodes = 0, []
    for d in _addr_variants(eth_dest):
        for a in _addr_variants(hl_addr):
            count, nodes = 0, []
            for g in guardians:
                sig = signatures.get(g["nodeId"])
                if not sig:
                    continue
                if any(p256_verify(g["publicKey"], msg, sig) for msg in _messages_for(g["nodeId"], d, a)):
                    count += 1
                    nodes.append(g["nodeId"])
            if count > best_count:
                best_count, best_nodes = count, nodes
            if count >= GUARDIAN_THRESHOLD:
                return True, count, nodes
    return best_count >= GUARDIAN_THRESHOLD, best_count, best_nodes


# --------------------------------------------------------------------------- #
#  Hyperliquid: info (баланс, метаданные токена) + exchange (spotSend)         #
# --------------------------------------------------------------------------- #
def hl_info(body, testnet=False, timeout=25):
    base = HL_API_TESTNET if testnet else HL_API
    resp = requests.post(base + "/info", json=body,
                         headers={"Content-Type": "application/json"}, timeout=timeout)
    try:
        data = resp.json()
    except ValueError:
        raise HyperliquidError(f"Hyperliquid вернул не JSON (HTTP {resp.status_code}): {resp.text[:200]}")
    if resp.status_code >= 400:
        raise HyperliquidError(f"Hyperliquid info HTTP {resp.status_code}: {data}")
    return data


def hl_find_token(testnet=False, name=HL_TOKEN_NAME):
    """
    Найти spot-токен по имени (UETH) в spotMeta. Возвращает dict:
    {"identifier": "UETH:0x..", "weiDecimals": int, "szDecimals": int, "index": int, "fullName": str}.
    """
    meta = hl_info({"type": "spotMeta"}, testnet)
    for t in (meta.get("tokens", []) if isinstance(meta, dict) else []):
        if str(t.get("name", "")).upper() == name.upper():
            return {
                "identifier": f'{t["name"]}:{t["tokenId"]}',
                "name": t["name"],
                "weiDecimals": int(t.get("weiDecimals", 9)),
                "szDecimals": int(t.get("szDecimals", 4)),
                "index": t.get("index"),
                "fullName": t.get("fullName", ""),
            }
    raise HyperliquidError(f"Токен {name} не найден в spotMeta Hyperliquid"
                           f"{' (testnet)' if testnet else ''}.")


def hl_spot_balance(addr, coin, testnet=False):
    """Доступный спот-баланс монеты на Hyperliquid. -> (total: Decimal, hold: Decimal)."""
    data = hl_info({"type": "spotClearinghouseState", "user": addr}, testnet)
    for b in (data.get("balances", []) if isinstance(data, dict) else []):
        if str(b.get("coin", "")).upper() == coin.upper():
            try:
                return Decimal(str(b.get("total", "0"))), Decimal(str(b.get("hold", "0")))
            except (InvalidOperation, ValueError):
                return Decimal(0), Decimal(0)
    return Decimal(0), Decimal(0)


# Котировочные стейблы Hyperliquid — ими платится activation gas за перевод.
QUOTE_TOKENS = {"USDC", "USDT0", "USDT", "USDH", "USDE", "USDD", "FEUSD"}
# Activation gas fee: первый перевод на НОВЫЙ (адрес, токен) на HyperCore стоит
# 1 котировочный токен (см. docs: «Activation gas fee»). Для вывода это критично —
# адрес вывода Unit уникален для каждого назначения и при первом выводе ещё «пустой».
ACTIVATION_FEE = Decimal("1")


def hl_quote_balance(addr, testnet=False):
    """Суммарный спот-баланс котировочных стейблов (USDC/USDT/…) — на activation gas."""
    data = hl_info({"type": "spotClearinghouseState", "user": addr}, testnet)
    total = Decimal(0)
    for b in (data.get("balances", []) if isinstance(data, dict) else []):
        if str(b.get("coin", "")).upper() in QUOTE_TOKENS:
            try:
                total += Decimal(str(b.get("total", "0")))
            except (InvalidOperation, ValueError):
                pass
    return total


def hl_user_exists(addr, testnet=False):
    """Есть ли у адреса присутствие на HyperCore (если нет — первый перевод требует activation gas)."""
    data = hl_info({"type": "preTransferCheck", "user": addr}, testnet)
    return bool(data.get("userExists", False)) if isinstance(data, dict) else False


def hl_eth_price(testnet=False):
    """Цена ETH/USD по mid перпа ETH на Hyperliquid (allMids["ETH"]). -> Decimal | None."""
    try:
        mids = hl_info({"type": "allMids"}, testnet)
        px = mids.get("ETH") if isinstance(mids, dict) else None
        return Decimal(str(px)) if px is not None else None
    except Exception:
        return None


# ---- подпись user-signed действий (EIP-712, схема Hyperliquid) ---- #
# Классический перевод спот-токена (на «обычных» аккаунтах):
SPOT_TRANSFER_SIGN_TYPES = [
    {"name": "hyperliquidChain", "type": "string"},
    {"name": "destination", "type": "string"},
    {"name": "token", "type": "string"},
    {"name": "amount", "type": "string"},
    {"name": "time", "type": "uint64"},
]
# Новый универсальный перевод (на unified-аккаунтах spotSend отключён → нужен этот):
SEND_ASSET_SIGN_TYPES = [
    {"name": "hyperliquidChain", "type": "string"},
    {"name": "destination", "type": "string"},
    {"name": "sourceDex", "type": "string"},
    {"name": "destinationDex", "type": "string"},
    {"name": "token", "type": "string"},
    {"name": "amount", "type": "string"},
    {"name": "fromSubAccount", "type": "string"},
    {"name": "nonce", "type": "uint64"},
]


def _user_signed_payload(primary_type, payload_types, action):
    chain_id = int(action["signatureChainId"], 16)
    return {
        "domain": {
            "name": "HyperliquidSignTransaction",
            "version": "1",
            "chainId": chain_id,
            "verifyingContract": "0x0000000000000000000000000000000000000000",
        },
        "types": {
            primary_type: payload_types,
            "EIP712Domain": [
                {"name": "name", "type": "string"},
                {"name": "version", "type": "string"},
                {"name": "chainId", "type": "uint256"},
                {"name": "verifyingContract", "type": "address"},
            ],
        },
        "primaryType": primary_type,
        "message": action,
    }


def sign_user_action(account, action, sign_types, primary_type):
    """Подписать user-signed действие по схеме Hyperliquid. -> {"r","s","v"}."""
    data = _user_signed_payload(primary_type, sign_types, action)
    encoded = encode_typed_data(full_message=data)
    signed = account.sign_message(encoded)
    return {"r": to_hex(signed.r), "s": to_hex(signed.s), "v": signed.v}


def build_spot_send_action(destination, token_identifier, amount_str, testnet=False):
    """Классический spotSend (поле time = nonce). Работает на «обычных» аккаунтах."""
    return {
        "type": "spotSend",
        "signatureChainId": "0x66eee",
        "hyperliquidChain": "Testnet" if testnet else "Mainnet",
        "destination": destination,
        "token": token_identifier,
        "amount": amount_str,
        "time": now_ms(),
    }


def build_send_asset_action(destination, token_identifier, amount_str, testnet=False):
    """Универсальный sendAsset (поле nonce). Нужен на unified-аккаунтах (там spotSend отключён).
    Перевод спот→спот на внешний адрес: sourceDex=destinationDex='spot', без сабаккаунта."""
    return {
        "type": "sendAsset",
        "signatureChainId": "0x66eee",
        "hyperliquidChain": "Testnet" if testnet else "Mainnet",
        "destination": destination,
        "sourceDex": "spot",
        "destinationDex": "spot",
        "token": token_identifier,
        "amount": amount_str,
        "fromSubAccount": "",
        "nonce": now_ms(),
    }


# Методы перевода UETH на адрес Unit, в порядке попытки. Сначала классический
# spotSend (его режим отказа на unified-аккаунте известен и безопасен — просто
# «Action disabled when unified account is active»), при этой ошибке — sendAsset.
# Билдеры ленивые: действие собирается прямо перед отправкой, чтобы каждый раз был
# свежий монотонный nonce (см. now_ms) — иначе повтор даст «duplicate nonce».
TRANSFER_METHODS = [
    ("spotSend",  build_spot_send_action,  SPOT_TRANSFER_SIGN_TYPES, "HyperliquidTransaction:SpotSend",  "time"),
    ("sendAsset", build_send_asset_action, SEND_ASSET_SIGN_TYPES,    "HyperliquidTransaction:SendAsset", "nonce"),
]


def hl_exchange_post(action, signature, nonce, testnet=False, timeout=25):
    base = HL_API_TESTNET if testnet else HL_API
    payload = {"action": action, "nonce": nonce, "signature": signature}
    resp = requests.post(base + "/exchange", json=payload,
                         headers={"Content-Type": "application/json"}, timeout=timeout)
    try:
        data = resp.json()
    except ValueError:
        raise HyperliquidError(f"Hyperliquid вернул не JSON (HTTP {resp.status_code}): {resp.text[:200]}")
    if resp.status_code >= 400:
        raise HyperliquidError(f"Hyperliquid exchange HTTP {resp.status_code}: {data}")
    return data


_last_nonce_ms = 0


def now_ms():
    """Монотонный таймстемп в мс: каждый вызов строго больше предыдущего.
    Hyperliquid отвергает повторяющиеся nonce — это гарантирует уникальность даже
    при двух вызовах в одну миллисекунду (например, spotSend → fallback sendAsset)."""
    global _last_nonce_ms
    t = int(time.time() * 1000)
    if t <= _last_nonce_ms:
        t = _last_nonce_ms + 1
    _last_nonce_ms = t
    return t


# --------------------------------------------------------------------------- #
#  Работа с суммой                                                            #
# --------------------------------------------------------------------------- #
def parse_amount_spec(s):
    """
    ''/'all'/'max' -> ('all',)              вывести весь доступный баланс
    '0.05'         -> ('eth', Decimal)       фиксированная сумма ETH
    '50%'          -> ('pct', 0.50, 0.50)    ровно 50% баланса
    '50-70%'       -> ('pct', 0.50, 0.70)    случайно 50..70% (на каждый кошелёк)
    """
    s = str(s).strip().lower().replace(" ", "")
    if s in ("", "all", "max", "всё", "все", "100%"):
        return ("all",) if s != "100%" else ("pct", 1.0, 1.0)
    if s.endswith("%"):
        body = s[:-1]
        if "-" in body:
            lo_s, hi_s = body.split("-", 1)
            lo, hi = float(lo_s), float(hi_s)
        else:
            lo = hi = float(body)
        if not (0 < lo <= hi <= 100):
            raise ValueError("процент должен быть в диапазоне 0..100 и lo<=hi")
        return ("pct", lo / 100.0, hi / 100.0)
    val = Decimal(s)
    if val <= 0:
        raise ValueError("сумма должна быть > 0")
    return ("eth", val)


def _floor(amount, wei_decimals):
    """Обрезать Decimal вниз до wei_decimals знаков (не отправить больше, чем есть)."""
    q = Decimal(1).scaleb(-int(wei_decimals))
    return amount.quantize(q, rounding=ROUND_DOWN)


def _amount_to_str(amount):
    """Decimal -> строка без хвостовых нулей и без экспоненты (для подписи/отправки)."""
    s = format(amount, "f")
    if "." in s:
        s = s.rstrip("0").rstrip(".")
    return s or "0"


def _usd(amount):
    """Decimal|None -> строка USD с 2 знаками ('' если нет)."""
    if amount is None:
        return ""
    try:
        return f"{Decimal(amount):.2f}"
    except (InvalidOperation, ValueError, TypeError):
        return ""


def compute_amount(spec, pct_draw, available, wei_decimals, min_amount):
    """
    Возвращает (amount_dec, amount_str, error|None). Сумма обрезается вниз до
    точности токена. Для процента — доля от доступного баланса; для 'all' — весь.
    """
    if spec[0] == "all":
        amount = available
    elif spec[0] == "eth":
        amount = spec[1]
        if amount > available:
            return None, None, (f"запрошено {fmt_eth(amount)} ETH, доступно только "
                                 f"{fmt_eth(available)} ETH")
    else:  # pct
        amount = available * Decimal(str(pct_draw))
    amount = _floor(amount, wei_decimals)
    if amount <= 0:
        return None, None, f"нечего выводить (доступно {fmt_eth(available)} ETH)"
    if min_amount and amount < min_amount:
        return None, None, (f"сумма {fmt_eth(amount)} ETH < минимума Unit {fmt_eth(min_amount)} ETH "
                            f"(меньше минимума мост может не зачислить; снять порог: --min-eth 0)")
    return amount, _amount_to_str(amount), None


# --------------------------------------------------------------------------- #
#  Загрузка списка кошельков                                                  #
# --------------------------------------------------------------------------- #
def load_wallets(args):
    """
    Возвращает список dict: {pk, dest|None, amount|None}.
    Источники: --key, PRIVATE_KEY (env), --wallets файл (.txt/.csv или .json).

    Формат текстового файла (строка на кошелёк, # — комментарий):
        PRIVATE_KEY
        PRIVATE_KEY,ETH_ADDRESS
        PRIVATE_KEY,ETH_ADDRESS,AMOUNT      (AMOUNT: all | 0.05 | 50% | 50-70%)
        PRIVATE_KEY,,50%                    (адрес назначения = свой же, 50%)
    JSON: [{"private_key": "...", "dest": "0x..", "amount": "all"}, ...]
    """
    if args.key:
        return [{"pk": args.key.strip(), "dest": args.dest, "amount": args.amount}]

    if not args.wallets:
        pk_env = os.environ.get("PRIVATE_KEY", "").strip()
        if pk_env:
            return [{"pk": pk_env, "dest": args.dest, "amount": args.amount}]
        sys.exit("Не задан кошелёк: укажите --key, либо PRIVATE_KEY в .env, либо --wallets <файл>.")

    if not os.path.isfile(args.wallets):
        sys.exit(f"Файл с кошельками не найден: {args.wallets}")

    wallets = []
    if args.wallets.lower().endswith(".json"):
        with open(args.wallets, "r", encoding="utf-8") as fh:
            for item in json.load(fh):
                pk = (item.get("private_key") or item.get("key") or item.get("pk") or "").strip()
                if not pk:
                    continue
                wallets.append({
                    "pk": pk,
                    "dest": (item.get("dest") or item.get("address") or item.get("eth") or args.dest),
                    "amount": (item.get("amount") or args.amount),
                })
    else:
        with open(args.wallets, "r", encoding="utf-8") as fh:
            for raw in fh:
                line = raw.strip()
                if not line or line.startswith("#"):
                    continue
                parts = [p.strip() for p in line.split(",")]
                pk = parts[0]
                dest = parts[1] if len(parts) > 1 and parts[1] else args.dest
                amount = parts[2] if len(parts) > 2 and parts[2] else args.amount
                wallets.append({"pk": pk, "dest": dest, "amount": amount})

    if not wallets:
        sys.exit("В файле не найдено ни одного кошелька.")
    return wallets


# --------------------------------------------------------------------------- #
#  Утилиты вывода                                                             #
# --------------------------------------------------------------------------- #
def fmt_eth(dec):
    try:
        s = format(Decimal(dec), "f")
        if "." in s:
            # показываем до 9 знаков, без хвостовых нулей
            whole, frac = s.split(".")
            frac = frac[:9].rstrip("0")
            s = whole if not frac else f"{whole}.{frac}"
        return s
    except Exception:
        return "?"


def short(addr):
    return f"{addr[:6]}…{addr[-4:]}" if addr and len(addr) > 12 else (addr or "—")


def parse_range(s, default):
    """'20-60' -> (20.0, 60.0); '5' -> (5.0, 5.0)."""
    try:
        s = str(s).strip()
        if "-" in s:
            a, b = s.split("-", 1)
            return float(a), float(b)
        v = float(s)
        return v, v
    except Exception:
        return default


# --------------------------------------------------------------------------- #
#  Планирование одного кошелька                                               #
# --------------------------------------------------------------------------- #
def plan_wallet(token, guardians, w, args):
    """Считает всё, что нужно для одного вывода, и проверяет адрес. -> dict(plan)."""
    plan = {"ok": False, "reason": ""}
    try:
        acct = Account.from_key(w["pk"])
    except Exception as e:
        plan["reason"] = f"плохой приватный ключ ({e})"
        plan["sender"] = "—"
        return plan

    sender = acct.address
    plan["pk"] = w["pk"]
    plan["sender"] = sender

    # Адрес назначения в Ethereum (по умолчанию — свой же EVM-адрес)
    try:
        eth_dest = to_checksum_address(w["dest"]) if w.get("dest") else sender
    except Exception:
        plan["reason"] = f"некорректный адрес назначения: {w.get('dest')}"
        return plan
    plan["dest"] = eth_dest

    # Спецификация суммы
    try:
        spec = parse_amount_spec(w["amount"]) if w.get("amount") is not None else ("all",)
    except Exception as e:
        plan["reason"] = f"некорректная сумма '{w.get('amount')}' ({e})"
        return plan
    plan["spec"] = spec
    plan["pct_draw"] = random.uniform(spec[1], spec[2]) if spec[0] == "pct" else None

    # Адрес вывода от Unit + проверка подписей
    try:
        res = gen_withdraw_address(eth_dest, args.testnet)
    except Exception as e:
        plan["reason"] = f"Unit не выдал адрес вывода ({e})"
        return plan
    hl_raw = res["address"]
    try:
        hl_addr = to_checksum_address(hl_raw)
    except Exception:
        plan["reason"] = f"Unit вернул некорректный адрес: {hl_raw}"
        return plan
    plan["protocol_addr"] = hl_addr

    ok, count, nodes = verify_signatures(eth_dest, hl_raw, res.get("signatures", {}), guardians)
    plan["verify_ok"], plan["verify_count"], plan["verify_nodes"] = ok, count, nodes
    if not ok and not args.no_verify:
        plan["reason"] = f"подписи гардианов НЕ прошли ({count}/{GUARDIAN_THRESHOLD}) — адрес не доверенный"
        return plan

    # Баланс UETH на Hyperliquid
    try:
        total, hold = hl_spot_balance(sender, token["name"], args.testnet)
    except Exception as e:
        plan["reason"] = f"не удалось получить спот-баланс Hyperliquid ({e})"
        return plan
    available = total - hold
    plan["total"], plan["hold"], plan["available"] = total, hold, available
    if available <= 0:
        plan["reason"] = (f"нет доступного {token['name']} на Hyperliquid "
                          f"(total {fmt_eth(total)}, hold {fmt_eth(hold)})")
        return plan

    # Сумма к выводу
    amount, amount_str, err = compute_amount(spec, plan["pct_draw"], available,
                                             token["weiDecimals"], args.min_amount)
    if err:
        plan["reason"] = err
        return plan
    plan["amount"] = amount
    plan["amount_str"] = amount_str

    # Activation gas: первый перевод на новый адрес Unit стоит ~1 USDC. Проверяем,
    # что есть чем заплатить, иначе Hyperliquid отклонит перевод уже при отправке.
    plan["needs_activation"] = False
    plan["quote_balance"] = None
    if not args.no_gas_check:
        try:
            new_dest = not hl_user_exists(hl_addr, args.testnet)
        except Exception:
            new_dest = True  # не смогли проверить — считаем, что газ нужен (безопаснее)
        if new_dest:
            plan["needs_activation"] = True
            try:
                quote = hl_quote_balance(sender, args.testnet)
            except Exception:
                quote = Decimal(0)
            plan["quote_balance"] = quote
            if quote < ACTIVATION_FEE:
                plan["reason"] = (
                    f"первый вывод на новый адрес Unit требует ~{fmt_eth(ACTIVATION_FEE)} USDC "
                    f"на активацию (activation gas Hyperliquid), а на споте только {fmt_eth(quote)} "
                    f"USDC. Пополни спот USDC до ≥{fmt_eth(ACTIVATION_FEE)} и повтори "
                    f"(или --no-gas-check, если уверен)."
                )
                return plan

    plan["ok"] = True
    return plan


# --------------------------------------------------------------------------- #
#  Отправка одного вывода (перевод UETH на HL-адрес Unit)                      #
# --------------------------------------------------------------------------- #
def _resp_ok(data):
    return isinstance(data, dict) and str(data.get("status", "")).lower() == "ok"


def _resp_err_text(data):
    if isinstance(data, dict):
        return str(data.get("response", data))
    return str(data)


def send_withdraw(token, plan, args):
    """
    Пересчитывает по свежему балансу и переводит UETH на адрес Unit.
    Пробует spotSend; если аккаунт unified (spotSend отключён) — автоматически
    переключается на sendAsset. -> result.
    """
    sender = plan["sender"]
    dest_hl = plan["protocol_addr"]

    # Свежий баланс на момент отправки
    total, hold = hl_spot_balance(sender, token["name"], args.testnet)
    available = total - hold
    amount, amount_str, err = compute_amount(plan["spec"], plan["pct_draw"], available,
                                             token["weiDecimals"], args.min_amount)
    if err:
        return {"ok": False, "error": err}

    acct = Account.from_key(plan["pk"])
    tid = token["identifier"]

    if args.dry_run:
        actions = [b(dest_hl, tid, amount_str, args.testnet) for _, b, _, _, _ in TRANSFER_METHODS]
        return {"ok": True, "dry_run": True, "amount": amount, "amount_str": amount_str,
                "actions": actions}

    last_err = None
    for method, builder, sign_types, primary_type, nonce_field in TRANSFER_METHODS:
        action = builder(dest_hl, tid, amount_str, args.testnet)   # свежий nonce на каждую попытку
        nonce = action[nonce_field]
        signature = sign_user_action(acct, action, sign_types, primary_type)
        data = hl_exchange_post(action, signature, nonce, args.testnet)
        if _resp_ok(data):
            return {"ok": True, "amount": amount, "amount_str": amount_str,
                    "nonce": nonce, "method": method, "response": data}
        last_err = _resp_err_text(data)
        # переключаемся на следующий метод только если дело в типе аккаунта (unified)
        if "unified" not in last_err.lower():
            break
        print(f"      (spotSend недоступен — unified-аккаунт; пробую sendAsset…)")
    return {"ok": False, "error": f"Hyperliquid отклонил перевод: {last_err}"}


# --------------------------------------------------------------------------- #
#  Сводки / таблицы                                                           #
# --------------------------------------------------------------------------- #
def print_plan_table(plans, fees_eth, token):
    print("\n" + "=" * 80)
    print("ПЛАН ВЫВОДОВ (Hyperliquid → Ethereum через Unit)")
    if token:
        print(f"Токен на Hyperliquid: {token['name']} ({token.get('fullName','')}), "
              f"id {token['identifier']}")
    if fees_eth:
        fee = fees_eth.get("withdrawal-fee-in-units")
        eta = fees_eth.get("withdrawalEta")
        if fee is not None:
            print(f"Комиссия Unit за вывод: ~{fmt_eth(Decimal(str(fee)))} ETH (удерживается из суммы)  |  ETA: ~{eta}")
    print("-" * 80)
    print(f"{'#':>2}  {'отправитель(HL)':<16} {'ETH-получатель':<16} {'адрес вывода':<16} "
          f"{'сумма ETH':>14}  подписи")
    for i, p in enumerate(plans, 1):
        if p.get("ok"):
            sig = f"{p.get('verify_count', 0)}/3"
            if not p.get("verify_ok"):
                sig += " (skip)"
            print(f"{i:>2}  {short(p['sender']):<16} {short(p['dest']):<16} "
                  f"{short(p['protocol_addr']):<16} {fmt_eth(p['amount']):>14}  {sig}")
        else:
            print(f"{i:>2}  {short(p.get('sender', '—')):<16} {'':<16} {'':<16} "
                  f"{'— ПРОПУСК':>14}  {p.get('reason', '')}")
    print("=" * 80)


CREDIT_NOTE = {
    "done":    "✅ ETH пришёл на адрес в сети Ethereum",
    "timeout": "⏳ пока не зачислено (вышел таймаут ожидания) — придёт само, проверьте позже",
    "failure": "❌ операция Unit завершилась ошибкой",
    "pending": "⌛ отправлено на Hyperliquid, вывод в обработке у Unit",
}


def print_summary(results, fees_eth, waited=False):
    print("\n" + "=" * 80)
    print("ИТОГ")
    print("-" * 80)
    fee = None
    if fees_eth:
        try:
            fee = Decimal(str(fees_eth.get("withdrawal-fee-in-units")))
        except Exception:
            fee = None
    ok = 0
    for r in results:
        res = r["result"]
        if res.get("ok") and not res.get("dry_run"):
            ok += 1
            net = ""
            if fee is not None:
                got = Decimal(res["amount_str"]) - fee
                net = f"  → на Ethereum ~{fmt_eth(got)} ETH (после комиссии Unit)"
            print(f"  ✓ {short(r['sender'])}  отправлено {res['amount_str']} ETH на Hyperliquid "
                  f"({res.get('method', '?')}){net}")
            if waited:
                note = CREDIT_NOTE.get(res.get("credit", "pending"))
                if note:
                    print(f"      {note}")
                if res.get("dst_tx"):
                    print(f"      Ethereum tx: {res['dst_tx']}")
        elif res.get("dry_run"):
            print(f"  • {short(r['sender'])}  {res['amount_str']} ETH  (dry-run, не отправлено)")
        else:
            print(f"  ✗ {short(r['sender'])}  ошибка: {res.get('error')}")
    print("-" * 80)
    print(f"Успешно отправлено: {ok} из {len(results)}")
    if not waited:
        print("Вывод на Ethereum произойдёт после финализации на Hyperliquid (~5000 блоков, обычно ~7 мин).")
    print("Статус: https://app.hyperunit.xyz/  или  /operations/<адрес> в API Unit.")
    print("=" * 80)


# --------------------------------------------------------------------------- #
#  Ожидание зачисления на Ethereum (опрос /operations)                        #
# --------------------------------------------------------------------------- #
STATE_LABELS = {
    "sourcetxdiscovered":       "Unit увидел вывод в блоке Hyperliquid",
    "readyforwithdrawqueue":    "поставлено в очередь на вывод",
    "queuedforwithdraw":        "в батче очереди вывода",
    "buildingdsttx":            "Unit готовит транзакцию в Ethereum",
    "additionalchecks":         "Unit проверяет операцию",
    "signtx":                   "Unit подписывает транзакцию",
    "broadcasttx":              "Unit отправляет ETH в Ethereum",
    "waitfordsttxfinalization": "транзакция отправлена, финализация в Ethereum",
    "done":                     "выведено",
    "failure":                  "ошибка операции",
}


def _op_state_label(op):
    state = str(op.get("state", ""))
    key = state.lower()
    if key == "waitforsrctxfinalization":
        return "ждём финализации в Hyperliquid (~5000 блоков, ~7 мин)"
    return STATE_LABELS.get(key, f"статус: {state or '—'}")


def _is_withdraw_op(op):
    return (str(op.get("sourceChain", "")).lower() == "hyperliquid" and
            str(op.get("destinationChain", "")).lower() == "ethereum")


def fetch_withdraw_ops(addresses, testnet):
    """Собрать withdrawal-операции (hyperliquid→ethereum) по набору адресов, дедуп по operationId."""
    ops, seen = [], set()
    for a in addresses:
        try:
            data = api_get(f"/operations/{a}", testnet)
        except Exception:
            continue
        for op in (data.get("operations", []) if isinstance(data, dict) else []):
            if not _is_withdraw_op(op):
                continue
            oid = op.get("operationId") or op.get("sourceTxHash") or json.dumps(op, sort_keys=True)
            if oid in seen:
                continue
            seen.add(oid)
            ops.append(op)
    return ops


def _match_new_op(ops, sender, dest, snapshot_ids):
    """Найти НАШУ операцию: новый operationId, наш sender и dest, самая свежая."""
    cand = []
    sl, dl = sender.lower(), dest.lower()
    for op in ops:
        oid = op.get("operationId") or op.get("sourceTxHash") or ""
        if oid and oid in snapshot_ids:
            continue
        src = str(op.get("sourceAddress", "")).lower()
        dst = str(op.get("destinationAddress", "")).lower()
        if dst and dst != dl:
            continue
        if src and src != sl:
            continue
        cand.append(op)
    if not cand:
        return None
    cand.sort(key=lambda o: str(o.get("opCreatedAt", "")), reverse=True)
    return cand[0]


def wait_for_all(sent, testnet, timeout_s, poll_s):
    """
    Опрашивает /operations пока каждый вывод не дойдёт до done/failure или таймаута.
    Матчим операцию по новому operationId (снимок сделан перед отправкой). Дописывает
    в result: 'credit' (done|failure|timeout) и 'dst_tx' (Ethereum-хэш зачисления).
    """
    remaining = list(sent)
    for r in remaining:
        r["result"].setdefault("credit", "pending")
    last_label = {}
    start = time.time()
    infinite = timeout_s is None or timeout_s <= 0
    while remaining and (infinite or (time.time() - start) < timeout_s):
        # одна выборка операций на уникальный набор адресов за цикл
        cache = {}
        for r in list(remaining):
            res = r["result"]
            sender, dest = r["sender"], res["dest"]
            key = (sender, dest)
            if key not in cache:
                cache[key] = fetch_withdraw_ops({sender, dest}, testnet)
            op = _match_new_op(cache[key], sender, dest, res.get("snapshot_ids", set()))
            label = _op_state_label(op) if op else "ждём, пока Unit увидит вывод на Hyperliquid"
            tag = res.get("nonce") or sender
            if last_label.get(tag) != label:
                last_label[tag] = label
                print(f"  [{int(time.time() - start):>4}с] {short(sender)}: {label}")
            if op:
                st = str(op.get("state", "")).lower()
                if st == "done":
                    res["credit"] = "done"
                    dst = str(op.get("destinationTxHash", "") or "")
                    res["dst_tx"] = dst.split(":")[0] if dst else ""
                    print(f"  ✅ {short(sender)}: ETH пришёл на {short(dest)}")
                    remaining.remove(r)
                elif st in ("failure", "failed"):
                    res["credit"] = "failure"
                    print(f"  ❌ {short(sender)}: операция завершилась ошибкой")
                    remaining.remove(r)
        if remaining:
            time.sleep(poll_s)
    for r in remaining:
        r["result"]["credit"] = "timeout"
    return sent


# --------------------------------------------------------------------------- #
#  Журнал выводов: объём прогнанных средств и комиссии                         #
# --------------------------------------------------------------------------- #
def _unit_fee_dec(fees_eth):
    try:
        return Decimal(str(fees_eth.get("withdrawal-fee-in-units")))
    except Exception:
        return Decimal(0)


def build_ledger_rows(results, fees_eth, testnet, waited):
    """Из результатов отправки собрать строки журнала (только реальные успешные выводы)."""
    unit_fee = _unit_fee_dec(fees_eth)
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    price = hl_eth_price(testnet)  # ETH/USD на момент записи (может быть None)
    rows = []
    for r in results:
        res = r["result"]
        if not res.get("ok") or res.get("dry_run"):
            continue
        try:
            amt = Decimal(res["amount_str"])
        except (InvalidOperation, KeyError, TypeError):
            continue
        gas = ACTIVATION_FEE if res.get("needs_activation") else Decimal(0)
        status = (res.get("credit") if waited else "sent") or "sent"
        amt_usd = (amt * price) if price is not None else None
        rows.append({
            "timestamp_utc": ts,
            "network": "testnet" if testnet else "mainnet",
            "sender": r["sender"],
            "eth_dest": res.get("dest", ""),
            "method": res.get("method", ""),
            "amount_eth": _amount_to_str(amt),
            "eth_price_usd": _usd(price),
            "amount_usd": _usd(amt_usd),
            "unit_fee_eth": _amount_to_str(unit_fee),
            "net_eth": _amount_to_str(amt - unit_fee),
            "activation_gas_usdc": _amount_to_str(gas),
            "status": status,
            "ethereum_tx": res.get("dst_tx", ""),
            "nonce": res.get("nonce", ""),
        })
    return rows


def append_ledger(rows, path):
    new = not os.path.isfile(path)
    with open(path, "a", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=LEDGER_COLUMNS)
        if new:
            w.writeheader()
        for r in rows:
            w.writerow(r)


def ledger_totals(path):
    """Суммарно по журналу: объём (ETH и USD), комиссия Unit (ETH), газ активации (USDC), кол-во."""
    if not os.path.isfile(path):
        return None
    sums = {"amount_eth": Decimal(0), "amount_usd": Decimal(0),
            "unit_fee_eth": Decimal(0), "activation_gas_usdc": Decimal(0)}
    n = 0
    try:
        with open(path, "r", newline="", encoding="utf-8") as fh:
            for row in csv.DictReader(fh):
                for key in sums:
                    try:
                        sums[key] += Decimal(row.get(key) or 0)
                    except (InvalidOperation, ValueError):
                        pass
                n += 1
    except Exception:
        return None
    return {"volume": sums["amount_eth"], "volume_usd": sums["amount_usd"],
            "unit_fee": sums["unit_fee_eth"], "gas": sums["activation_gas_usdc"], "count": n}


# --------------------------------------------------------------------------- #
#  main                                                                       #
# --------------------------------------------------------------------------- #
def main():
    _load_dotenv()

    p = argparse.ArgumentParser(
        description="Вывод ETH с Hyperliquid в Ethereum через Unit (hyperunit.xyz)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--wallets", help="Файл с кошельками (.txt/.csv построчно или .json)")
    p.add_argument("--key", help="Один приватный ключ (или возьмётся PRIVATE_KEY из .env)")
    p.add_argument("--dest", help="Ethereum-адрес назначения для всех (по умолчанию = адрес самого кошелька)")
    p.add_argument("--amount", default=None,
                   help="Сколько выводить: all (по умолчанию) | 0.05 | 50%% | 50-70%% (переопределяемо в файле)")
    p.add_argument("--min-eth", type=str, default=None,
                   help="Минимум вывода, ETH. По умолчанию тянется live с app.hyperunit.xyz (~0.007). 0 = без порога")
    p.add_argument("--delay", default="20-60",
                   help="Пауза между кошельками, сек: '20-60' или '5' (по умолчанию 20-60)")
    p.add_argument("--testnet", action="store_true",
                   help="Использовать тестнет Unit + Hyperliquid testnet")
    p.add_argument("--no-verify", action="store_true",
                   help="НЕ проверять подписи гардианов (НЕ рекомендуется)")
    p.add_argument("--no-gas-check", action="store_true",
                   help="НЕ проверять баланс USDC на activation gas (~1 USDC на первый вывод на новый адрес)")
    p.add_argument("--ledger", default=None,
                   help=f"Файл журнала выводов (объём+комиссии). По умолчанию {os.path.basename(LEDGER_FILE)} рядом со скриптом")
    p.add_argument("--no-ledger", action="store_true", help="НЕ вести журнал выводов")
    p.add_argument("--dry-run", action="store_true", help="Всё посчитать, проверить и подписать, но НЕ отправлять")
    p.add_argument("--yes", action="store_true", help="Не спрашивать подтверждение")
    p.add_argument("--wait", action="store_true",
                   help="После отправки ждать зачисления на Ethereum (опрос статуса до 'done')")
    p.add_argument("--wait-timeout", type=float, default=30.0,
                   help="Таймаут ожидания зачисления, мин (по умолчанию 30; 0 = ждать без таймаута)")
    p.add_argument("--poll", type=int, default=20,
                   help="Период проверки статуса, сек (по умолчанию 20)")
    args = p.parse_args()

    net = "тестнет" if args.testnet else "mainnet"
    print(f"=== Вывод ETH с Hyperliquid в Ethereum через Unit ({net}) ===")

    guardians = TESTNET_GUARDIANS if args.testnet else MAINNET_GUARDIANS
    fees_eth = estimate_fees(args.testnet)

    # Минимум вывода
    if args.min_eth is None:
        live_min, src = fetch_min("ETH")
        min_amount = live_min if live_min is not None else FALLBACK_MIN
        print(f"Минимум вывода ETH: {fmt_eth(min_amount)} ({src})")
    else:
        try:
            min_amount = Decimal(str(args.min_eth))
        except (InvalidOperation, ValueError):
            sys.exit(f"Некорректный --min-eth: {args.min_eth}")
        print(f"Минимум вывода ETH: {fmt_eth(min_amount)} (задано вручную)"
              + (" — порог снят" if min_amount == 0 else ""))
    args.min_amount = min_amount  # пробрасываем в plan_wallet/send_withdraw

    # Токен UETH на Hyperliquid (id, точность) — одна выборка
    try:
        token = hl_find_token(args.testnet)
    except HyperliquidError as e:
        sys.exit(f"Ошибка Hyperliquid: {e}")
    print(f"Токен на Hyperliquid: {token['name']} ({token['fullName']}), точность {token['weiDecimals']} зн., "
          f"id {token['identifier']}")

    wallets = load_wallets(args)
    print(f"Кошельков к обработке: {len(wallets)}")

    # --- Этап 1: планирование (адреса вывода + проверка подписей + баланс + сумма) ---
    print("\nГенерирую адреса вывода, проверяю подписи гардианов и баланс…")
    plans = []
    for i, w in enumerate(wallets, 1):
        plan = plan_wallet(token, guardians, w, args)
        mark = "ok" if plan.get("ok") else f"ПРОПУСК — {plan.get('reason')}"
        print(f"  [{i}/{len(wallets)}] {short(plan.get('sender', '—'))}: {mark}")
        plans.append(plan)

    print_plan_table(plans, fees_eth, token)

    valid = [p for p in plans if p.get("ok")]
    if not valid:
        sys.exit("Нет ни одного кошелька, готового к выводу. См. причины выше.")

    total = sum((p["amount"] for p in valid), Decimal(0))
    print(f"\nИтого к выводу: {fmt_eth(total)} ETH с {len(valid)} кошельк(а/ов).")

    if args.dry_run:
        print("\n[dry-run] Реальной отправки не будет. Покажу подписанные действия spotSend…")

    # --- Подтверждение ---
    if not args.yes and not args.dry_run:
        ans = input('Отправить выводы? Введите "yes" для подтверждения: ').strip().lower()
        if ans not in ("yes", "y"):
            sys.exit("Отменено.")

    # Снимок существующих операций ДО отправки (чтобы потом найти именно наши)
    if args.wait and not args.dry_run:
        for p in valid:
            try:
                ops = fetch_withdraw_ops({p["sender"], p["dest"]}, args.testnet)
                p["snapshot_ids"] = {op.get("operationId") or op.get("sourceTxHash") for op in ops}
            except Exception:
                p["snapshot_ids"] = set()

    # --- Этап 2: отправка ---
    lo, hi = parse_range(args.delay, (20.0, 60.0))
    results = []
    for i, plan in enumerate(valid, 1):
        print(f"\n[{i}/{len(valid)}] {short(plan['sender'])} → вывод {fmt_eth(plan['amount'])} ETH "
              f"на {short(plan['dest'])} (через HL-адрес {short(plan['protocol_addr'])}) …")
        try:
            res = send_withdraw(token, plan, args)
        except Exception as e:
            res = {"ok": False, "error": str(e)}
        res["dest"] = plan["dest"]
        res["snapshot_ids"] = plan.get("snapshot_ids", set())
        res["needs_activation"] = plan.get("needs_activation", False)

        if res.get("ok") and not res.get("dry_run"):
            print(f"      ✓ отправлено на Hyperliquid ({res.get('method')}, nonce {res.get('nonce')})")
        elif res.get("dry_run"):
            print(f"      • dry-run, действие НЕ отправлено (попытки по порядку):")
            for act in res.get("actions", []):
                print(f"        [{act.get('type')}] {json.dumps(act, ensure_ascii=False)}")
        else:
            print(f"      ✗ ошибка: {res.get('error')}")

        results.append({"sender": plan["sender"], "result": res})

        if i < len(valid) and not args.dry_run:
            pause = random.uniform(lo, hi)
            print(f"      …пауза {pause:.0f} сек перед следующим кошельком")
            time.sleep(pause)

    # --- Этап 3: ожидание зачисления (если --wait) ---
    sent = [r for r in results if r["result"].get("ok") and not r["result"].get("dry_run")]
    waited = False
    if args.wait and sent and not args.dry_run:
        waited = True
        to_s = args.wait_timeout * 60
        cap = "без таймаута" if to_s <= 0 else f"до {args.wait_timeout:g} мин"
        print(f"\nЖду зачисления на Ethereum ({cap}, проверка каждые {args.poll}с)…")
        print("Можно прервать (Ctrl+C) — выводы уже отправлены, статус увидите на app.hyperunit.xyz")
        try:
            wait_for_all(sent, args.testnet, to_s, args.poll)
        except KeyboardInterrupt:
            print("\n  …опрос прерван вручную.")

    print_summary(results, fees_eth, waited=waited)

    # --- Журнал: объём прогнанных средств и комиссии ---
    if not args.dry_run and not args.no_ledger:
        rows = build_ledger_rows(results, fees_eth, args.testnet, waited)
        if rows:
            path = args.ledger or LEDGER_FILE
            try:
                append_ledger(rows, path)
            except Exception as e:
                print(f"\n⚠  не удалось записать журнал {path}: {e}")
            else:
                sess_vol = sum((Decimal(x["amount_eth"]) for x in rows), Decimal(0))
                sess_usd = sum((Decimal(x["amount_usd"]) for x in rows if x["amount_usd"]), Decimal(0))
                sess_fee = sum((Decimal(x["unit_fee_eth"]) for x in rows), Decimal(0))
                sess_gas = sum((Decimal(x["activation_gas_usdc"]) for x in rows), Decimal(0))
                print(f"\nЖурнал: {path} (+{len(rows)} запис(ь/и))")
                print(f"  За сессию:  объём {fmt_eth(sess_vol)} ETH (~${_usd(sess_usd)}) | "
                      f"комиссия Unit {fmt_eth(sess_fee)} ETH | газ активации {fmt_eth(sess_gas)} USDC")
                tot = ledger_totals(path)
                if tot:
                    print(f"  Всего:      объём {fmt_eth(tot['volume'])} ETH (~${_usd(tot['volume_usd'])}) | "
                          f"комиссия Unit {fmt_eth(tot['unit_fee'])} ETH | газ активации {fmt_eth(tot['gas'])} USDC "
                          f"| выводов {tot['count']}")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit("\nОтменено.")
    except HyperunitError as e:
        sys.exit(f"Ошибка Unit API: {e}")
    except HyperliquidError as e:
        sys.exit(f"Ошибка Hyperliquid: {e}")
    except requests.RequestException as e:
        sys.exit(f"Сетевая ошибка: {e}")
