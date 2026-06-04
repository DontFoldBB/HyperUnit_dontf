#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Простой депозит ETH из сети Ethereum на Hyperliquid через мост Unit (hyperunit.xyz).

Логика проще некуда:
  1. Берёт кошелёк из .env (PRIVATE_KEY + ETH_RPC_URL).
  2. Спрашивает, СКОЛЬКО ПРОЦЕНТОВ от баланса ETH задепнуть на Hyperliquid.
  3. Проверяет, что хватает на газ и на минимум Unit — иначе ПРЕДУПРЕЖДАЕТ и не шлёт.
  4. Проверяет подписи гардианов Unit (защита от подмены депозит-адреса).
  5. Отправляет ETH и ждёт зачисления на Hyperliquid.
  6. Пишет траты на комиссию (газ + комиссия Unit) в deposits_log.csv.

Депозит идёт на Hyperliquid-аккаунт = адрес самого кошелька. ETH приходит как спот-токен UETH.

Запуск:
  python hyperunit_deposit.py            # спросит процент
  python hyperunit_deposit.py 80         # задепнуть 80% баланса
  python hyperunit_deposit.py 80 --yes   # без подтверждения
  python hyperunit_deposit.py 80 --dry-run   # всё посчитать/проверить, но НЕ отправлять

.env рядом со скриптом:
  ETH_RPC_URL=https://eth-mainnet.g.alchemy.com/v2/ВАШ_КЛЮЧ
  PRIVATE_KEY=0x...
"""

import os
import re
import sys
import csv
import json
import time
import base64
import argparse
from datetime import datetime

try:
    import requests
except ImportError:
    sys.exit("Нужен requests:  pip install requests")
import cf_http  # GET к Unit с браузерным TLS (curl_cffi) против Cloudflare; фоллбэк на requests
try:
    from web3 import Web3
except ImportError:
    sys.exit("Нужен web3:  pip install web3")
try:
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature
    from cryptography.hazmat.primitives import hashes
    from cryptography.exceptions import InvalidSignature
except ImportError:
    sys.exit("Нужен cryptography:  pip install cryptography")

# Корректный вывод кириллицы в консоли Windows
try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass


# --------------------------------------------------------------------------- #
#  Константы                                                                  #
# --------------------------------------------------------------------------- #
API_BASE = "https://api.hyperunit.xyz"
APP_BUNDLE_URL = "https://app.hyperunit.xyz/app.bundle.js"
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
MIN_CACHE_FILE = os.path.join(SCRIPT_DIR, ".min_cache.json")
LOG_FILE = os.path.join(SCRIPT_DIR, "deposits_log.csv")
VOLUME_FILE = os.path.join(SCRIPT_DIR, "volume.txt")
FALLBACK_MIN_ETH = 0.007

# Публичные ключи гардианов mainnet (для проверки подлинности депозит-адреса).
MAINNET_GUARDIANS = [
    {"nodeId": "unit-node",  "publicKey": "04dc6f89f921dc816aa69b687be1fcc3cc1d48912629abc2c9964e807422e1047e0435cb5ba0fa53cb9a57a9c610b4e872a0a2caedda78c4f85ebafcca93524061"},
    {"nodeId": "hl-node",    "publicKey": "048633ea6ab7e40cdacf37d1340057e84bb9810de0687af78d031e9b07b65ad4ab379180ab55075f5c2ebb96dab30d2c2fab49d5635845327b6a3c27d20ba4755b"},
    {"nodeId": "field-node", "publicKey": "04ae2ab20787f816ea5d13f36c4c4f7e196e29e867086f3ce818abb73077a237f841b33ada5be71b83f4af29f333dedc5411ca4016bd52ab657db2896ef374ce99"},
]
GUARDIAN_THRESHOLD = 2

STATE_LABELS = {
    "sourcetxdiscovered":       "Unit увидел транзакцию",
    "buildingdsttx":            "Unit готовит зачисление на Hyperliquid",
    "additionalchecks":         "Unit проверяет операцию",
    "signtx":                   "Unit подписывает зачисление",
    "broadcasttx":              "Unit отправляет зачисление в Hyperliquid",
    "waitfordsttxfinalization": "зачисление отправлено, финализация на Hyperliquid",
    "done":                     "зачислено",
    "failure":                  "ошибка операции",
}


class HyperunitError(Exception):
    pass


# --------------------------------------------------------------------------- #
#  .env                                                                       #
# --------------------------------------------------------------------------- #
def load_dotenv():
    path = os.path.join(SCRIPT_DIR, ".env")
    if not os.path.isfile(path):
        return
    with open(path, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


# --------------------------------------------------------------------------- #
#  Unit API                                                                   #
# --------------------------------------------------------------------------- #
def api_get(path, timeout=25):
    # curl_cffi с браузерным TLS — иначе Cloudflare у Unit отдаёт 403. Только Accept в
    # заголовках (свой User-Agent сломал бы impersonate). Прокси — из env (net_proxy).
    resp = cf_http.get(API_BASE + path, headers={"Accept": "application/json"}, timeout=timeout)
    try:
        data = resp.json()
    except ValueError:
        raise HyperunitError(f"Unit вернул не JSON (HTTP {resp.status_code}): {resp.text[:200]}")
    if resp.status_code >= 400:
        raise HyperunitError(f"Unit API HTTP {resp.status_code}: {data}")
    return data


def estimate_deposit_fee():
    """(комиссия Unit за депозит в ETH, ETA-строка) или (None, None)."""
    try:
        e = api_get("/v2/estimate-fees").get("ethereum", {})
        return e.get("deposit-fee-in-units"), e.get("depositEta")
    except Exception:
        return None, None


def gen_deposit_address(dest):
    data = api_get(f"/gen/ethereum/hyperliquid/eth/{dest}")
    if not isinstance(data, dict) or "address" not in data:
        raise HyperunitError(f"Unit не вернул адрес депозита: {data}")
    return data


def get_operations(dest):
    data = api_get(f"/operations/{dest}")
    return data.get("operations", []) if isinstance(data, dict) else []


def eth_price_usd():
    """Цена ETH в USD: Hyperliquid (куда бриджим) → Coinbase фоллбэк → None."""
    try:
        p = requests.post("https://api.hyperliquid.xyz/info",
                          json={"type": "allMids"}, timeout=15).json().get("ETH")
        if p:
            return float(p)
    except Exception:
        pass
    try:
        return float(requests.get("https://api.coinbase.com/v2/prices/ETH-USD/spot",
                                  timeout=15).json()["data"]["amount"])
    except Exception:
        return None


# --------------------------------------------------------------------------- #
#  Минимальный депозит (live из конфига сайта, кэш ~12 ч)                      #
# --------------------------------------------------------------------------- #
def fetch_min_deposit(max_age=43200):
    """Минимум ETH с app.hyperunit.xyz (отдельного API нет). -> (float, источник)."""
    cache = {"mins": {}}
    try:
        with open(MIN_CACHE_FILE, "r", encoding="utf-8") as fh:
            cache = json.load(fh)
        if time.time() - cache.get("ts", 0) < max_age and "ETH" in cache.get("mins", {}):
            return cache["mins"]["ETH"], "кэш"
    except Exception:
        pass
    try:
        js = requests.get(APP_BUNDLE_URL, timeout=40, headers={"User-Agent": "Mozilla/5.0"}).text
        i = js.find('unitAssetSymbol:"ETH"')
        m = re.search(r"minDepositAmountToken:([0-9.eE+\-]+)", js[i:i + 2000]) if i >= 0 else None
        if m:
            val = float(m.group(1))
            mins = cache.get("mins", {}) if isinstance(cache, dict) else {}
            mins["ETH"] = val
            try:
                with open(MIN_CACHE_FILE, "w", encoding="utf-8") as fh:
                    json.dump({"ts": time.time(), "mins": mins}, fh)
            except Exception:
                pass
            return val, "live с сайта"
    except Exception:
        pass
    return FALLBACK_MIN_ETH, "запасное значение"


# --------------------------------------------------------------------------- #
#  Проверка подписей гардианов (ECDSA P-256 / SHA-256)                        #
# --------------------------------------------------------------------------- #
def _p256_verify(pubkey_hex, message_bytes, sig_b64):
    try:
        pub = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), bytes.fromhex(pubkey_hex))
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
    out = []
    cand = [s, s.lower()]
    try:
        cand.append(Web3.to_checksum_address(s))
    except Exception:
        pass
    for v in cand:
        if v and v not in out:
            out.append(v)
    return out


def _messages(node_id, dest, addr):
    yield f"{node_id}:user-ethereum-hyperliquid-{dest}-{addr}".encode("utf-8")          # новый формат
    yield f"{node_id}:{dest}-hyperliquid-eth-{addr}-ethereum-deposit".encode("utf-8")   # легаси


def verify_signatures(dest, addr, signatures):
    """-> (ok: bool, count: int). Нужно >= 2 из 3 гардианов."""
    if not isinstance(signatures, dict):
        return False, 0
    best = 0
    for d in _addr_variants(dest):
        for a in _addr_variants(addr):
            count = 0
            for g in MAINNET_GUARDIANS:
                sig = signatures.get(g["nodeId"])
                if sig and any(_p256_verify(g["publicKey"], m, sig) for m in _messages(g["nodeId"], d, a)):
                    count += 1
            best = max(best, count)
            if count >= GUARDIAN_THRESHOLD:
                return True, count
    return best >= GUARDIAN_THRESHOLD, best


# --------------------------------------------------------------------------- #
#  Газ / транзакция                                                           #
# --------------------------------------------------------------------------- #
def get_fees(w3):
    blk = w3.eth.get_block("latest")
    base = blk.get("baseFeePerGas")
    if base is None:
        gp = w3.eth.gas_price
        return {"eip1559": False, "gas_price": gp, "per_gas": gp}
    try:
        prio = w3.eth.max_priority_fee
    except Exception:
        prio = w3.to_wei(1, "gwei")
    max_fee = base * 2 + prio
    return {"eip1559": True, "prio": prio, "max_fee": max_fee, "per_gas": max_fee}


def build_tx(chain_id, fees, nonce, to, value, gas):
    tx = {"chainId": chain_id, "nonce": nonce, "to": to, "value": int(value), "gas": int(gas)}
    if fees["eip1559"]:
        tx.update({"type": 2, "maxFeePerGas": fees["max_fee"], "maxPriorityFeePerGas": fees["prio"]})
    else:
        tx["gasPrice"] = fees["gas_price"]
    return tx


# --------------------------------------------------------------------------- #
#  Утилиты                                                                    #
# --------------------------------------------------------------------------- #
def eth(wei, n=6):
    return f"{Web3.from_wei(int(wei), 'ether'):.{n}f}"


def short(a):
    return f"{a[:6]}…{a[-4:]}" if a and len(a) > 12 else (a or "—")


def ask_percent():
    while True:
        raw = input("Сколько % от баланса ETH задепнуть на Hyperliquid? ").strip().replace("%", "").replace(",", ".")
        try:
            v = float(raw)
            if 0 < v <= 100:
                return v
        except ValueError:
            pass
        print("  Введите число от 0 до 100 (например 80).")


def state_label(op):
    s = str(op.get("state", "")).lower()
    if s == "waitforsrctxfinalization":
        return f"финализация в Ethereum (~{op.get('sourceTxConfirmations')}/14 подтв.)"
    return STATE_LABELS.get(s, f"статус: {op.get('state') or '—'}")


def find_op(ops, target_hash):
    t = target_hash.lower().replace("0x", "", 1)
    for op in ops:
        if t in str(op.get("sourceTxHash", "")).lower():
            return op
    return None


def wait_for_credit(dest, tx_hash, timeout_s=480, poll_s=30):
    """Опрашивает /operations пока не done/failure/timeout. -> (status, op)."""
    start = time.time()
    last = None
    op = None
    while time.time() - start < timeout_s:
        try:
            op = find_op(get_operations(dest), tx_hash)
        except Exception:
            op = op  # временная ошибка API — пробуем дальше
        label = state_label(op) if op else "ждём, пока Unit увидит транзакцию"
        if label != last:
            last = label
            print(f"  [{int(time.time() - start):>4}с] {label}")
        if op:
            st = str(op.get("state", "")).lower()
            if st == "done":
                return "done", op
            if st in ("failure", "failed"):
                return "failure", op
        time.sleep(poll_s)
    return "timeout", op


# --------------------------------------------------------------------------- #
#  Лог трат на комиссию                                                       #
# --------------------------------------------------------------------------- #
LOG_COLUMNS = ["datetime", "wallet", "hl_account", "percent", "deposit_eth",
               "eth_price_usd", "deposit_usd", "gas_eth", "unit_fee_eth",
               "total_fee_eth", "received_eth", "tx_hash", "hl_tx", "status"]


def log_deposit(row):
    """Дописать строку в deposits_log.csv (с заголовком при первом запуске)."""
    new_file = not os.path.isfile(LOG_FILE)
    try:
        with open(LOG_FILE, "a", newline="", encoding="utf-8-sig") as fh:
            w = csv.DictWriter(fh, fieldnames=LOG_COLUMNS)
            if new_file:
                w.writeheader()
            w.writerow(row)
        print(f"\n📝 Запись добавлена в {LOG_FILE}")
    except Exception as e:
        print(f"\n⚠ Не удалось записать лог: {e}")


def update_volume():
    """
    Накопительный объём, прогнанный через Unit (по deposits_log.csv, кроме failure).
    Пишет volume.txt (ETH + USD + кол-во). -> (n, eth_sum, usd_sum) | None.
    """
    n, eth_sum, usd_sum = 0, 0.0, 0.0
    try:
        with open(LOG_FILE, newline="", encoding="utf-8-sig") as fh:
            for r in csv.DictReader(fh):
                if str(r.get("status", "")).lower() == "failure":
                    continue
                try:
                    eth_sum += float(r.get("deposit_eth") or 0)
                except ValueError:
                    pass
                try:
                    usd_sum += float(r.get("deposit_usd") or 0)
                except ValueError:
                    pass
                n += 1
    except FileNotFoundError:
        return None
    try:
        with open(VOLUME_FILE, "w", encoding="utf-8") as fh:
            fh.write(
                "Прогнано через Unit (Ethereum → Hyperliquid)\n"
                f"Депозитов: {n}\n"
                f"Объём ETH: {eth_sum:.9f}\n"
                f"Объём USD: ${usd_sum:.2f}\n"
                f"Обновлено:  {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
            )
    except Exception as e:
        print(f"⚠ Не удалось записать volume.txt: {e}")
    return n, eth_sum, usd_sum


# --------------------------------------------------------------------------- #
#  main                                                                       #
# --------------------------------------------------------------------------- #
def main():
    load_dotenv()

    p = argparse.ArgumentParser(description="Простой депозит ETH на Hyperliquid через Unit")
    p.add_argument("percent", nargs="?", type=float,
                   help="Процент от баланса ETH (напр. 80). Если не указать — спросит.")
    p.add_argument("--yes", action="store_true", help="Не спрашивать подтверждение")
    p.add_argument("--dry-run", action="store_true", help="Посчитать и проверить, но НЕ отправлять")
    p.add_argument("--rpc", help="RPC Ethereum (иначе ETH_RPC_URL из .env)")
    p.add_argument("--key", help="Приватный ключ (иначе PRIVATE_KEY из .env)")
    args = p.parse_args()

    print("=== Депозит ETH → Hyperliquid (Unit) ===")

    rpc = args.rpc or os.environ.get("ETH_RPC_URL", "").strip()
    key = args.key or os.environ.get("PRIVATE_KEY", "").strip()
    if not rpc:
        sys.exit("Не задан RPC. Впишите ETH_RPC_URL в .env (Alchemy/Infura) или передайте --rpc.")
    if not key:
        sys.exit("Не задан ключ. Впишите PRIVATE_KEY в .env или передайте --key.")

    w3 = Web3(Web3.HTTPProvider(rpc, request_kwargs={"timeout": 30}))
    if not w3.is_connected():
        sys.exit(f"Нет связи с RPC: {rpc}")
    chain_id = w3.eth.chain_id
    if chain_id != 1:
        print(f"⚠ chainId={chain_id} — это не Ethereum mainnet. Депозит ETH на Unit работает с mainnet (1).")

    acct = w3.eth.account.from_key(key)
    sender = acct.address          # он же HL-аккаунт назначения
    balance = w3.eth.get_balance(sender)
    print(f"Кошелёк:  {sender}")
    print(f"Баланс:   {eth(balance)} ETH")

    # --- процент ---
    pct = args.percent if args.percent is not None else ask_percent()
    if not (0 < pct <= 100):
        sys.exit("Процент должен быть в диапазоне 0..100.")

    # --- газ и проверка «хватает ли на комиссию» ---
    fees = get_fees(w3)
    try:
        gas_limit = int(w3.eth.estimate_gas({"from": sender, "to": sender, "value": 1}) * 1.2)
    except Exception:
        gas_limit = 21000
    gas_reserve = gas_limit * fees["per_gas"]               # на ЭТО уйдёт газ (макс. оценка)
    min_eth, min_src = fetch_min_deposit()
    min_wei = Web3.to_wei(min_eth, "ether")

    amount = int(balance * pct / 100)
    capped = False
    if amount + gas_reserve > balance:                       # не оставили на газ — режем
        amount = balance - gas_reserve
        capped = True

    # ВАРНЫ: не хватает на комиссию сети / ниже минимума Unit
    if amount <= 0:
        sys.exit(f"⚠ НЕ ХВАТАЕТ НА ГАЗ. Баланс {eth(balance)} ETH, газ нужен ~{eth(gas_reserve, 8)} ETH. "
                 f"Пополни кошелёк.")
    if amount < min_wei:
        sys.exit(f"⚠ Сумма {eth(amount)} ETH НИЖЕ МИНИМУМА Unit ({min_eth} ETH, {min_src}) — не зачислится. "
                 f"Увеличь процент или пополни кошелёк.")
    if capped:
        print(f"⚠ Урезал сумму до {eth(amount)} ETH, чтобы осталось на газ (~{eth(gas_reserve, 8)} ETH).")

    # --- депозит-адрес + проверка подписей ---
    res = gen_deposit_address(sender)
    deposit_raw = res["address"]
    deposit = Web3.to_checksum_address(deposit_raw)
    ok, count = verify_signatures(sender, deposit_raw, res.get("signatures", {}))
    if not ok:
        sys.exit(f"⚠ Подписи гардианов Unit НЕ прошли ({count}/{GUARDIAN_THRESHOLD}). "
                 f"Адрес мог быть подменён — отправка отменена ради безопасности.")

    fee_unit, eta = estimate_deposit_fee()
    price = eth_price_usd()
    usd_str = f", ~${float(Web3.from_wei(amount, 'ether')) * price:.2f}" if price else ""

    # --- сводка ---
    print("\n" + "-" * 56)
    print(f"  Депнуть:        {eth(amount)} ETH  ({pct:g}% от баланса{usd_str})")
    print(f"  На Hyperliquid: {sender}  (придёт как спот UETH)")
    print(f"  Депозит-адрес:  {deposit}")
    print(f"  Подписи Unit:   {count}/3 ✓")
    print(f"  Газ (оценка):   ~{eth(gas_reserve, 8)} ETH")
    if fee_unit is not None:
        print(f"  Комиссия Unit:  ~{fee_unit} ETH   (придёт ~{eth(amount - Web3.to_wei(fee_unit, 'ether'))} UETH, ETA ~{eta})")
    print("-" * 56)

    if args.dry_run:
        print("[dry-run] Ничего не отправлено.")
        return
    if not args.yes:
        if input('Отправить? Введите "yes": ').strip().lower() not in ("yes", "y"):
            sys.exit("Отменено.")

    # --- отправка ---
    nonce = w3.eth.get_transaction_count(sender)
    tx = build_tx(chain_id, fees, nonce, deposit, amount, gas_limit)
    signed = w3.eth.account.sign_transaction(tx, key)
    raw = getattr(signed, "raw_transaction", None) or getattr(signed, "rawTransaction")
    txh = w3.eth.send_raw_transaction(raw)
    tx_hash = txh.hex()
    if not tx_hash.startswith("0x"):
        tx_hash = "0x" + tx_hash
    tx_hash = tx_hash.lower()
    print(f"\n✓ Отправлено: {eth(amount)} ETH")
    print(f"  tx: {tx_hash}")
    print(f"  https://etherscan.io/tx/{tx_hash}")

    # --- фактический газ из квитанции ---
    gas_eth_actual = None
    try:
        rcpt = w3.eth.wait_for_transaction_receipt(txh, timeout=300, poll_latency=5)
        eff = rcpt.get("effectiveGasPrice") or fees["per_gas"]
        gas_eth_actual = rcpt["gasUsed"] * eff
    except Exception as e:
        print(f"  (не дождался квитанции для точного газа: {e})")

    # --- ждём зачисления на Hyperliquid ---
    print("\nЖду зачисления на Hyperliquid (опрос каждые 30с, до 8 мин)…")
    status, op = wait_for_credit(sender, tx_hash, timeout_s=480, poll_s=30)
    hl_tx = (op or {}).get("destinationTxHash", "") if op else ""
    if status == "done":
        print("  ✅ Деньги на Hyperliquid (раздел Spot, токен UETH).")
    elif status == "failure":
        print("  ❌ Операция Unit завершилась ошибкой — проверь на app.hyperunit.xyz.")
    else:
        print("  ⏳ Пока не зачислено (вышел таймаут опроса). Деньги дойдут сами — "
              "проверь позже на app.hyperliquid.xyz (Spot) / app.hyperunit.xyz.")

    # --- лог трат на комиссию ---
    gas_for_log = gas_eth_actual if gas_eth_actual is not None else gas_reserve
    unit_fee_eth = float(fee_unit) if fee_unit is not None else 0.0
    deposit_eth_f = float(Web3.from_wei(amount, "ether"))
    total_fee = float(Web3.from_wei(int(gas_for_log), "ether")) + unit_fee_eth
    received = deposit_eth_f - unit_fee_eth
    log_deposit({
        "datetime": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "wallet": sender,
        "hl_account": sender,
        "percent": f"{pct:g}",
        "deposit_eth": f"{deposit_eth_f:.9f}",
        "eth_price_usd": f"{price:.2f}" if price else "",
        "deposit_usd": f"{deposit_eth_f * price:.2f}" if price else "",
        "gas_eth": f"{Web3.from_wei(int(gas_for_log), 'ether'):.9f}"
                   + ("" if gas_eth_actual is not None else " (оценка)"),
        "unit_fee_eth": f"{unit_fee_eth:.9f}",
        "total_fee_eth": f"{total_fee:.9f}",
        "received_eth": f"{received:.9f}",
        "tx_hash": tx_hash,
        "hl_tx": hl_tx,
        "status": status,
    })

    vol = update_volume()
    if vol:
        n, te, tu = vol
        print(f"📊 Всего прогнано через Unit: {te:.6f} ETH / ${tu:.2f}  ({n} деп.)  → {VOLUME_FILE}")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit("\nОтменено.")
    except HyperunitError as e:
        sys.exit(f"Ошибка Unit API: {e}")
    except requests.RequestException as e:
        sys.exit(f"Сетевая ошибка: {e}")
