#!/usr/bin/env python3
"""
deposit_eth.py — перевод ETH в сети Ethereum (mainnet) с приватного ключа
на адрес назначения (например, депозитный адрес Bitget).

Принимает:
  * приватный ключ отправителя,
  * адрес назначения,
  * процент от баланса, который нужно отправить (0, 100].

Газ резервируется автоматически. При 100% кошелёк опустошается за вычетом
резерва на газ (сам газ нельзя отправить получателю). При проценте < 100
получателю уходит ровно указанная доля баланса, а газ оплачивается из остатка.

Запуск:
  python deposit_eth.py                       # спросит всё интерактивно
  python deposit_eth.py -t 0x... -p 100       # ключ спросит через getpass
  python deposit_eth.py -k 0x... -t 0x... -p 50 -y -w
"""
import argparse
import getpass
import json
import os
import sys
from decimal import Decimal
from pathlib import Path

try:
    from web3 import Web3
    from eth_account import Account
except ImportError:
    sys.exit("Не установлен web3. Установите зависимости: pip install -r requirements.txt")

HERE = Path(__file__).resolve().parent
DEFAULT_RPC = "https://ethereum-rpc.publicnode.com"
CHAIN_ID = 1
GAS_LIMIT_FALLBACK = 21000          # обычный перевод ETH между EOA
ETHERSCAN_TX = "https://etherscan.io/tx/0x{}"


def load_config() -> dict:
    """Читает config.json и подмешивает .env в окружение (если есть)."""
    cfg = {}
    cfg_path = HERE / "config.json"
    if cfg_path.exists():
        try:
            cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
        except Exception as e:  # noqa: BLE001
            print(f"[!] Не удалось прочитать config.json: {e}")

    env_path = HERE / ".env"
    if env_path.exists():
        for line in env_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, val = line.split("=", 1)
            val = val.strip().strip('"').strip("'")
            os.environ.setdefault(key.strip(), val)
    return cfg


def resolve_rpc(args, cfg) -> str:
    return (
        args.rpc
        or os.environ.get("ETH_RPC_URL")
        or os.environ.get("RPC_URL")
        or cfg.get("rpc_url")
        or DEFAULT_RPC
    )


def normalize_pk(pk: str) -> str:
    pk = pk.strip()
    if pk.startswith(("0x", "0X")):
        pk = pk[2:]
    if len(pk) != 64:
        raise ValueError("Приватный ключ должен быть 64 hex-символа (32 байта).")
    int(pk, 16)  # проверка, что это hex
    return "0x" + pk


def fmt_eth(wei: int) -> str:
    """Красивый вывод суммы в ETH без хвостовых нулей."""
    s = f"{Web3.from_wei(wei, 'ether'):.18f}".rstrip("0").rstrip(".")
    return s or "0"


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Перевод ETH (Ethereum mainnet) по проценту от баланса кошелька."
    )
    parser.add_argument("-k", "--private-key", help="Приватный ключ отправителя (иначе спросит безопасно).")
    parser.add_argument("-t", "--to", help="Адрес назначения (0x...).")
    parser.add_argument("-p", "--percent", type=float, help="Процент баланса для отправки, (0, 100].")
    parser.add_argument("--rpc", help="RPC URL Ethereum (иначе из .env/config.json/по умолчанию).")
    parser.add_argument("-y", "--yes", action="store_true", help="Не запрашивать подтверждение.")
    parser.add_argument("-w", "--wait", action="store_true", help="Дождаться подтверждения транзакции.")
    args = parser.parse_args()

    cfg = load_config()
    rpc = resolve_rpc(args, cfg)

    w3 = Web3(Web3.HTTPProvider(rpc, request_kwargs={"timeout": 30}))
    if not w3.is_connected():
        sys.exit(f"[x] Нет соединения с RPC: {rpc}")

    # ---------- ввод ----------
    pk_raw = args.private_key or getpass.getpass("Приватный ключ отправителя: ")
    try:
        pk = normalize_pk(pk_raw)
    except ValueError as e:
        sys.exit(f"[x] {e}")
    sender = Account.from_key(pk).address

    to_raw = (args.to or input("Адрес назначения (0x...): ")).strip()
    if not Web3.is_address(to_raw):
        sys.exit(f"[x] Некорректный адрес назначения: {to_raw}")
    to_addr = Web3.to_checksum_address(to_raw)
    if to_addr.lower() == sender.lower():
        print("[!] Внимание: адрес назначения совпадает с отправителем.")

    if args.percent is not None:
        percent = args.percent
    else:
        try:
            percent = float(input("Сколько вывести, % от баланса (например 100): ").strip())
        except ValueError:
            sys.exit("[x] Процент должен быть числом.")
    if not (0 < percent <= 100):
        sys.exit("[x] Процент должен быть в диапазоне (0, 100].")

    # ---------- баланс и комиссии ----------
    balance = w3.eth.get_balance(sender)
    if balance == 0:
        sys.exit(f"[x] Баланс {sender} = 0 ETH. Нечего отправлять.")

    latest = w3.eth.get_block("latest")
    base_fee = latest.get("baseFeePerGas")
    try:
        priority = w3.eth.max_priority_fee
    except Exception:  # noqa: BLE001 — не все RPC поддерживают eth_maxPriorityFeePerGas
        priority = w3.to_wei(1.5, "gwei")

    if base_fee is None:
        # сеть без EIP-1559 — legacy gasPrice
        eip1559 = False
        max_fee = w3.eth.gas_price
    else:
        eip1559 = True
        max_fee = base_fee * 2 + priority   # запас на рост base fee в следующем блоке

    # gas limit: 21000 для перевода на EOA, больше — если адресат контракт.
    try:
        est = w3.eth.estimate_gas({"from": sender, "to": to_addr, "value": 1})
    except Exception:  # noqa: BLE001
        est = GAS_LIMIT_FALLBACK
    # обычный перевод (± паддинг ноды) берём как есть; контракту-получателю даём запас 20%
    if est <= GAS_LIMIT_FALLBACK * 1.05:
        gas_limit = max(est, GAS_LIMIT_FALLBACK)
    else:
        gas_limit = int(est * 1.2)

    gas_reserve = gas_limit * max_fee

    target = int(Decimal(balance) * Decimal(str(percent)) / Decimal(100))
    capped = False
    if percent >= 100:
        value = balance - gas_reserve            # опустошаем кошелёк минус газ
    else:
        value = target
        if value + gas_reserve > balance:        # на газ не хватает из остатка — урезаем
            value = balance - gas_reserve
            capped = True

    if value <= 0:
        sys.exit(
            f"[x] Недостаточно средств на газ. Баланс {fmt_eth(balance)} ETH, "
            f"резерв газа до {fmt_eth(gas_reserve)} ETH."
        )

    # ---------- сводка ----------
    print("\n=== Перевод ETH (Ethereum mainnet) ===")
    print(f"RPC:            {rpc}")
    print(f"Откуда:         {sender}")
    print(f"Куда:           {to_addr}")
    print(f"Баланс:         {fmt_eth(balance)} ETH")
    print(f"Процент:        {percent}%")
    print(f"К отправке:     {fmt_eth(value)} ETH")
    if capped:
        print("   (!) сумма уменьшена, чтобы покрыть газ")
    print(f"Gas limit:      {gas_limit}")
    if eip1559:
        print(f"maxFeePerGas:   {w3.from_wei(max_fee, 'gwei'):.3f} gwei "
              f"(priority {w3.from_wei(priority, 'gwei'):.3f} gwei)")
    else:
        print(f"gasPrice:       {w3.from_wei(max_fee, 'gwei'):.3f} gwei")
    print(f"Макс. комиссия: {fmt_eth(gas_reserve)} ETH (резерв; по факту меньше)")
    print("=" * 40)

    if not args.yes:
        ans = input("Отправить? [y/N]: ").strip().lower()
        if ans not in ("y", "yes", "д", "да"):
            sys.exit("Отменено.")

    # ---------- сборка, подпись, отправка ----------
    tx = {
        "chainId": cfg.get("chain_id", CHAIN_ID),
        "nonce": w3.eth.get_transaction_count(sender),
        "to": to_addr,
        "value": value,
        "gas": gas_limit,
    }
    if eip1559:
        tx["type"] = 2
        tx["maxFeePerGas"] = max_fee
        tx["maxPriorityFeePerGas"] = priority
    else:
        tx["gasPrice"] = max_fee

    signed = Account.from_key(pk).sign_transaction(tx)
    raw = getattr(signed, "raw_transaction", None)
    if raw is None:
        raw = signed.rawTransaction  # совместимость со старыми версиями eth-account
    tx_hash = w3.eth.send_raw_transaction(raw)

    h = tx_hash.hex()
    h = h[2:] if h.startswith("0x") else h
    print(f"\n[OK] Транзакция отправлена: 0x{h}")
    print(f"     {ETHERSCAN_TX.format(h)}")

    if args.wait:
        print("Ожидание подтверждения...")
        receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=300)
        status = "успех" if receipt.status == 1 else "ОШИБКА"
        print(f"[OK] Блок {receipt.blockNumber}, статус: {status}, газ использован: {receipt.gasUsed}")


if __name__ == "__main__":
    main()
