#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Вывод ETH в сети Ethereum с Bitget. Самодостаточный скрипт (без внешних модулей проекта).

  * Монета и сеть ЗАХАРДКОЖЕНЫ: ETH / Ethereum (у Bitget сеть называется "ETH").
  * Спрашивает ТОЛЬКО сумму. Адрес получателя берётся из приватника в .env.
  * Показывает «выводим X» -> ждёт зачисления on-chain -> «поступило Y».
  * Комиссию Bitget пишет в журнал трат (withdrawals_log.csv) и считает суммарно.

.env (рядом со скриптом):
    BITGET_API_KEY=...
    BITGET_API_SECRET=...
    BITGET_API_PASSPHRASE=...
    WITHDRAW_PRIVATE_KEY=0x...     # приватник кошелька-получателя (адрес вычислится сам)
    # ETH_RPC_URL=...              # опционально свой RPC

Запуск:
    python withdraw_eth.py --amount 0.006
    python withdraw_eth.py                       # спросит сумму
    python withdraw_eth.py --amount 0.006 --yes
    python withdraw_eth.py --amount 0.006 --no-wait
"""

import os
import sys
import csv
import time
import json
import uuid
import hmac
import base64
import hashlib
import argparse
import urllib.parse
from decimal import Decimal, ROUND_DOWN
from datetime import datetime, timezone

try:
    import requests
except ImportError:
    sys.exit("Нужен requests:  pip install requests")

try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

# --------------------------- захардкожено --------------------------- #
COIN = "ETH"
CHAIN = "ETH"                 # Ethereum mainnet (имя сети у Bitget — "ETH")
BASE_URL = "https://api.bitget.com"
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
LEDGER = os.path.join(SCRIPT_DIR, "withdrawals_log.csv")
DEFAULT_RPCS = [
    "https://ethereum-rpc.publicnode.com",
    "https://1rpc.io/eth",
    "https://eth.drpc.org",
]


# ------------------------------- .env ------------------------------- #
def _load_dotenv():
    path = os.path.join(SCRIPT_DIR, ".env")
    if not os.path.isfile(path):
        return
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


_load_dotenv()
API_KEY = os.environ.get("BITGET_API_KEY", "").strip()
API_SECRET = os.environ.get("BITGET_API_SECRET", "").strip()
API_PASSPHRASE = os.environ.get("BITGET_API_PASSPHRASE", "").strip()


class BitgetError(Exception):
    pass


# -------------------------- Bitget client --------------------------- #
def _sign(prehash):
    return base64.b64encode(
        hmac.new(API_SECRET.encode(), prehash.encode(), hashlib.sha256).digest()
    ).decode()


def _request(method, path, params=None, body_obj=None, auth=False, timeout=20):
    query = "?" + urllib.parse.urlencode(params) if params else ""
    request_path = path + query
    body = json.dumps(body_obj) if body_obj is not None else ""
    headers = {"Content-Type": "application/json", "locale": "en-US"}
    if auth:
        if not (API_KEY and API_SECRET and API_PASSPHRASE):
            raise BitgetError("Не заданы ключи Bitget в .env")
        ts = str(int(time.time() * 1000))
        headers.update({
            "ACCESS-KEY": API_KEY,
            "ACCESS-SIGN": _sign(ts + method.upper() + request_path + body),
            "ACCESS-TIMESTAMP": ts,
            "ACCESS-PASSPHRASE": API_PASSPHRASE,
        })
    resp = requests.request(method, BASE_URL + request_path, headers=headers,
                            data=body.encode() if body else None, timeout=timeout)
    try:
        data = resp.json()
    except ValueError:
        raise BitgetError(f"Не JSON (HTTP {resp.status_code}): {resp.text[:200]}")
    if str(data.get("code")) != "00000":
        raise BitgetError(f"{data.get('msg')} (code {data.get('code')})")
    return data.get("data")


def _truthy(v):
    return str(v).lower() in ("true", "1", "yes")


def eth_chain_info():
    """Параметры сети ETH (комиссия/минимум/точность) — публичный справочник, без авторизации."""
    data = _request("GET", "/api/v2/spot/public/coins", params={"coin": COIN})
    for item in data or []:
        if item.get("coin", "").upper() == COIN:
            for c in item.get("chains", []):
                if c.get("chain", "").upper() == CHAIN and _truthy(c.get("withdrawable")):
                    return c
    raise BitgetError(f"сеть {CHAIN} для {COIN} недоступна для вывода")


def submit_withdrawal(address, size):
    payload = {
        "coin": COIN, "transferType": "on_chain", "address": address,
        "chain": CHAIN, "size": str(size), "clientOid": uuid.uuid4().hex,
    }
    return _request("POST", "/api/v2/spot/wallet/withdrawal", body_obj=payload, auth=True)


def get_withdrawal_record(order_id):
    end = int(time.time() * 1000)
    start = end - 3 * 24 * 3600 * 1000
    data = _request("GET", "/api/v2/spot/wallet/withdrawal-records",
                    params={"coin": COIN, "startTime": str(start), "endTime": str(end), "limit": "50"},
                    auth=True)
    return next((r for r in (data or []) if r.get("orderId") == str(order_id)), None)


# --------------------- приватный ключ -> адрес (EVM) ---------------- #
_HEX = set("0123456789abcdefABCDEF")


def looks_like_privkey(s):
    if not s:
        return False
    t = s.strip()
    if t[:2].lower() == "0x":
        t = t[2:]
    return len(t) == 64 and all(c in _HEX for c in t)


def _hex_pk(pk):
    t = pk.strip()
    if t[:2].lower() == "0x":
        t = t[2:]
    if len(t) != 64 or any(c not in _HEX for c in t):
        raise BitgetError("приватный ключ должен быть 64 hex-символа (32 байта)")
    return t.lower()


def evm_address(pk):
    try:
        from eth_account import Account
    except ImportError:
        raise BitgetError("нужен пакет:  pip install eth-account")
    try:
        return Account.from_key(_hex_pk(pk)).address  # checksummed 0x...
    except BitgetError:
        raise
    except Exception as e:
        raise BitgetError(f"некорректный приватный ключ: {e}")


def resolve_destination(address, privkey):
    """Вернуть (адрес, derived?). Адрес может быть и приватником. По умолчанию — из .env."""
    if privkey or looks_like_privkey(address):
        key = privkey or address
        derived = evm_address(key)
        if privkey and address and not looks_like_privkey(address) and address.lower() != derived.lower():
            raise BitgetError("указанный адрес не совпадает с адресом из приватника")
        return derived, True
    if not address:
        raise BitgetError("не задан получатель: укажите WITHDRAW_PRIVATE_KEY в .env (или --address/--privkey)")
    return address, False


# ------------------------------ on-chain ---------------------------- #
def eth_balance(address, rpc_urls):
    """Баланс адреса в ETH (Decimal) через публичный RPC, или None если все недоступны."""
    payload = {"jsonrpc": "2.0", "method": "eth_getBalance", "params": [address, "latest"], "id": 1}
    for url in rpc_urls:
        try:
            res = requests.post(url, json=payload, timeout=12).json().get("result")
            if res is not None:
                return Decimal(int(res, 16)) / Decimal(10 ** 18)
        except Exception:
            continue
    return None


# --------------------------- журнал трат ---------------------------- #
LEDGER_COLS = ["timestamp_utc", "coin", "chain", "amount_withdrawn", "bitget_fee",
               "received_onchain", "address", "orderId", "txId", "status"]


def ledger_append(row):
    is_new = not os.path.isfile(LEDGER)
    with open(LEDGER, "a", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=LEDGER_COLS)
        if is_new:
            w.writeheader()
        w.writerow(row)


def ledger_total_fee():
    if not os.path.isfile(LEDGER):
        return Decimal(0), 0
    total, n = Decimal(0), 0
    with open(LEDGER, newline="", encoding="utf-8") as fh:
        for r in csv.DictReader(fh):
            try:
                total += Decimal(r.get("bitget_fee") or 0)
                n += 1
            except Exception:
                pass
    return total, n


# ------------------------------- утилиты ---------------------------- #
def ask(prompt):
    while True:
        v = input(prompt + ": ").strip()
        if v:
            return v


def main():
    p = argparse.ArgumentParser(
        description="Вывод ETH (сеть Ethereum) с Bitget — спрашивает только сумму")
    p.add_argument("--amount", help="Сумма ETH к выводу. Если не задана — спросит.")
    p.add_argument("--address", help="Адрес/приватник получателя (по умолч. из .env WITHDRAW_PRIVATE_KEY)")
    p.add_argument("--privkey", help="Приватник получателя (или .env WITHDRAW_PRIVATE_KEY)")
    p.add_argument("--yes", action="store_true", help="Не спрашивать подтверждение")
    p.add_argument("--no-wait", action="store_true", help="Не ждать зачисления on-chain")
    p.add_argument("--wait-timeout", type=int, default=900,
                   help="Сколько ждать зачисления, сек (по умолч. 900)")
    p.add_argument("--rpc", help="Свой ETH RPC (или .env ETH_RPC_URL)")
    args = p.parse_args()

    rpc_urls = [u for u in [args.rpc, os.environ.get("ETH_RPC_URL")] if u] + DEFAULT_RPCS

    try:
        chain = eth_chain_info()
        fee = Decimal(chain["withdrawFee"])
        mn = Decimal(chain["minWithdrawAmount"])
        scale = int(chain.get("withdrawMinScale") or 8)

        privkey = args.privkey or os.environ.get("WITHDRAW_PRIVATE_KEY") or None
        address, derived = resolve_destination(args.address, privkey)

        # единственный вопрос — сумма
        amount_in = args.amount or ask(f"Сумма ETH к выводу (мин {mn}, комиссия {fee})")
        try:
            amount = Decimal(str(amount_in))
        except Exception:
            sys.exit("Сумма должна быть числом.")
        if -amount.as_tuple().exponent > scale:            # обрезать лишнюю точность
            amount = amount.quantize(Decimal(1).scaleb(-scale), rounding=ROUND_DOWN)
        if amount < mn:
            sys.exit(f"Сумма меньше минимальной ({mn} ETH).")

        recv_expected = amount - fee
        print("\n=== ВЫВОД ETH (сеть Ethereum) ===")
        print(f"Выводим:              {amount:f} ETH")
        print(f"Адрес:                {address}" + ("  (из приватника)" if derived else ""))
        print(f"Комиссия Bitget:      {fee:f} ETH")
        print(f"Ожидается на кошелёк: ~{recv_expected:f} ETH")

        if not args.yes:
            if input('Отправить? Введите "yes" для подтверждения: ').strip().lower() not in ("yes", "y"):
                sys.exit("Отменено.")

        # фиксируем баланс кошелька ДО отправки
        b0 = eth_balance(address, rpc_urls)
        if b0 is None:
            print("(!) RPC недоступен — зачисление не отслежу, но вывод отправлю.")

        res = submit_withdrawal(address, amount)
        order_id = res.get("orderId")
        print(f"\n✓ Заявка отправлена. orderId: {order_id}")

        received, status, txid, real_fee = None, "submitted", None, fee
        if not args.no_wait and b0 is not None:
            print(f"Жду зачисления на кошелёк (до {args.wait_timeout}s, Ctrl+C — прервать)...")
            start = time.time()
            try:
                while time.time() - start < args.wait_timeout:
                    time.sleep(15)
                    try:
                        rec = get_withdrawal_record(order_id)
                    except Exception:
                        rec = None
                    if rec:
                        status = rec.get("status") or status
                        txid = rec.get("txId") or txid
                        if rec.get("fee") not in (None, ""):
                            try:
                                real_fee = abs(Decimal(str(rec.get("fee"))))
                            except Exception:
                                pass
                    b1 = eth_balance(address, rpc_urls)
                    delta = (b1 - b0) if b1 is not None else None
                    shown = ("+" + format(delta, "f")) if (delta and delta > 0) else "ещё нет"
                    print(f"  [{int(time.time() - start)}s] Bitget: {status} | на кошельке: {shown}")
                    if delta is not None and delta > 0:
                        received = delta
                        break
            except KeyboardInterrupt:
                print("\nОжидание прервано (вывод уже отправлен — проверь позже).")

        # итог
        print("\n--- ИТОГ ---")
        print(f"Выведено:  {amount:f} ETH")
        if received is not None:
            print(f"Поступило: {received:f} ETH (подтверждено on-chain)")
        elif args.no_wait:
            print(f"Поступит:  ~{recv_expected:f} ETH (ожидание отключено)")
        else:
            print(f"Поступило: за {args.wait_timeout}s не задетектил; ожидается ~{recv_expected:f} ETH "
                  f"(статус Bitget: {status})")
        print(f"Комиссия Bitget: {real_fee:f} ETH")

        ledger_append({
            "timestamp_utc": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
            "coin": COIN, "chain": CHAIN,
            "amount_withdrawn": format(amount, "f"),
            "bitget_fee": format(real_fee, "f"),
            "received_onchain": format(received, "f") if received is not None else "",
            "address": address, "orderId": order_id, "txId": txid or "", "status": status,
        })
        total_fee, n = ledger_total_fee()
        print(f"\nЗаписано в журнал трат: {os.path.basename(LEDGER)}")
        print(f"Итого комиссий Bitget за всё время: {total_fee:f} ETH ({n} вывод(ов))")

    except BitgetError as e:
        sys.exit(f"Ошибка Bitget: {e}")
    except requests.RequestException as e:
        sys.exit(f"Сетевая ошибка: {e}")
    except KeyboardInterrupt:
        sys.exit("\nОтменено.")


if __name__ == "__main__":
    main()
