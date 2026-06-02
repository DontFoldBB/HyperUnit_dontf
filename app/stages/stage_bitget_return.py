# -*- coding: utf-8 -*-
"""
Стадия 5: возврат ETH с кошелька обратно на Bitget (завершает круг).
Шлёт ETH (сеть Ethereum) на депозит-адрес Bitget. Адрес берётся из wallets.xlsx
(столбец B, рядом с приватником) — кладётся в cfg.return_cfg['address'] через clone_for_wallet.
Переиспользует bitget_deposit/deposit_eth.py (fmt_eth, лимиты) и stage_deposit._connect.
"""

from decimal import Decimal

import paths  # noqa: F401
import colors as C
from web3 import Web3
import deposit_eth as bd          # bitget_deposit/deposit_eth.py
import bitget_api                 # ожидание зачисления на Bitget + свод субакк→мейн
from stage_deposit import _connect  # подключение к ETH RPC (свой/публичный)


def _bitget_deposit_address(cfg):
    """Адрес депозита Bitget — из wallets.xlsx (столбец B), кладётся в cfg.return_cfg['address'].
    -> (адрес|None, источник)."""
    explicit = str(cfg.return_cfg.get("address") or "").strip()
    if explicit:
        return explicit, "из wallets.xlsx"
    return None, "адрес не задан (столбец B в wallets.xlsx)"


def run(cfg, live):
    cfg.require_private_key()
    rcfg = cfg.return_cfg
    try:
        pct = float(rcfg.get("percent", 100))
    except Exception:
        pct = 100.0
    if not (0 < pct <= 100):
        return {"stage": "bitget_return", "ok": False,
                "summary": f"bitget_return_percent должен быть 0..100, а не {pct}", "spent": {}, "raw": {}}

    print(C.step("\n=== [5] Возврат ETH на Bitget ==="))

    w3, rpc = _connect(cfg)
    if w3 is None:
        return {"stage": "bitget_return", "ok": False,
                "summary": "не удалось подключиться к ETH RPC (ни свой, ни публичные)",
                "spent": {}, "raw": {}}
    print(f"  RPC: {rpc}" + ("" if cfg.eth_rpc_url else " (публичный)"))

    sender = w3.eth.account.from_key(cfg.private_key).address

    dest, src = _bitget_deposit_address(cfg)
    if not dest or not Web3.is_address(dest):
        return {"stage": "bitget_return", "ok": False,
                "summary": f"нет/некорректный адрес депозита Bitget ({src}). "
                           f"Впиши адрес депозита ETH с Bitget в wallets.xlsx (столбец B рядом с приватником).",
                "spent": {}, "raw": {}}
    dest = Web3.to_checksum_address(dest)

    balance = w3.eth.get_balance(sender)
    if balance == 0:
        return {"stage": "bitget_return", "ok": False,
                "summary": "баланс кошелька 0 ETH — нечего возвращать", "spent": {}, "raw": {}}

    # Газ (EIP-1559, как в bitget_deposit/deposit_eth.py).
    latest = w3.eth.get_block("latest")
    base_fee = latest.get("baseFeePerGas")
    try:
        priority = w3.eth.max_priority_fee
    except Exception:
        priority = w3.to_wei(1.5, "gwei")
    if base_fee is None:
        eip1559, max_fee = False, w3.eth.gas_price
    else:
        eip1559, max_fee = True, base_fee * 2 + priority
    try:
        est = w3.eth.estimate_gas({"from": sender, "to": dest, "value": 1})
    except Exception:
        est = bd.GAS_LIMIT_FALLBACK
    gas_limit = max(est, bd.GAS_LIMIT_FALLBACK) if est <= bd.GAS_LIMIT_FALLBACK * 1.05 else int(est * 1.2)
    gas_reserve = gas_limit * max_fee

    capped = False
    if pct >= 100:
        value = balance - gas_reserve                       # опустошаем кошелёк минус газ
    else:
        value = int(Decimal(balance) * Decimal(str(pct)) / Decimal(100))
        if value + gas_reserve > balance:
            value = balance - gas_reserve
            capped = True
    if value <= 0:
        return {"stage": "bitget_return", "ok": False,
                "summary": f"не хватает на газ: баланс {bd.fmt_eth(balance)} ETH, "
                           f"газ ~{bd.fmt_eth(gas_reserve)} ETH", "spent": {}, "raw": {}}

    val_eth = float(Web3.from_wei(value, "ether"))
    print(f"  Кошелёк:    {sender}")
    print(f"  Баланс:     {bd.fmt_eth(balance)} ETH | отправляю {pct:g}%")
    print(f"  На Bitget:  {dest}  ({src})")
    print(f"  К отправке: {bd.fmt_eth(value)} ETH" + ("  (урезано под газ)" if capped else ""))
    print(f"  Газ:        ~{bd.fmt_eth(gas_reserve)} ETH (резерв; по факту меньше)")

    gas_plan = float(Web3.from_wei(int(gas_reserve), "ether"))
    if not live:
        return {"stage": "bitget_return", "ok": True, "planned": True,
                "summary": f"[ТЕСТ] отправил бы {bd.fmt_eth(value)} ETH на Bitget ({dest})",
                "spent": {"eth": Decimal(str(round(gas_plan, 9))), "label": "газ (план)"},
                "raw": {"amount_eth": val_eth, "address": dest}}

    # Отправка.
    tx = {"chainId": 1, "nonce": w3.eth.get_transaction_count(sender), "to": dest,
          "value": int(value), "gas": int(gas_limit)}
    if eip1559:
        tx.update({"type": 2, "maxFeePerGas": max_fee, "maxPriorityFeePerGas": priority})
    else:
        tx["gasPrice"] = max_fee
    signed = w3.eth.account.sign_transaction(tx, cfg.private_key)
    raw = getattr(signed, "raw_transaction", None) or getattr(signed, "rawTransaction")
    txh = w3.eth.send_raw_transaction(raw)
    tx_hash = txh.hex()
    if not tx_hash.startswith("0x"):
        tx_hash = "0x" + tx_hash
    print(f"  ✓ Отправлено: {bd.fmt_eth(value)} ETH | tx: {tx_hash}")

    gas_eth_actual, status = None, "sent"
    if rcfg.get("wait", True):
        try:
            rcpt = w3.eth.wait_for_transaction_receipt(txh, timeout=300, poll_latency=5)
            eff = rcpt.get("effectiveGasPrice") or max_fee
            gas_eth_actual = rcpt["gasUsed"] * eff
            status = "done" if rcpt.get("status") == 1 else "failed"
            print(C.ok("  ✅ Подтверждено на Ethereum (ETH ушёл на Bitget).") if status == "done"
                  else C.err("  ❌ Транзакция завершилась ошибкой."))
        except Exception as e:
            print(C.warn(f"  ⏳ Не дождался квитанции: {e} (транзакция уже отправлена)."))

    # Дождаться зачисления на Bitget (мейн или субакк) и свести субакк → мейн.
    credit = ""
    if status != "failed" and rcfg.get("wait_credit", True) and all(cfg.bitget.values()):
        bitget_api.use_keys(cfg)
        print("  Жду зачисления на Bitget (мейн/субакк; Ctrl+C — перестать ждать)…")
        try:
            credit, _got = bitget_api.wait_credit_and_sweep(
                "ETH", val_eth, poll_s=20, timeout_s=0, log=lambda m: print(C.dim(m)))
        except KeyboardInterrupt:
            credit = "interrupted"
            print(C.warn("\n  ⏸ Ожидание Bitget прервано — депозит отправлен, придёт сам."))
        except Exception as e:
            print(C.warn(f"  ⚠ не смог отследить зачисление на Bitget: {e}"))

    gas_for_log = gas_eth_actual if gas_eth_actual is not None else gas_reserve
    gas_eth = float(Web3.from_wei(int(gas_for_log), "ether"))
    summary = (f"возвращено {bd.fmt_eth(value)} ETH на Bitget ({dest}); газ {gas_eth:.8f} ETH; статус {status}"
               + (f"; на Bitget: {credit}" if credit else ""))
    print(f"  --- {summary}")
    return {
        "stage": "bitget_return", "ok": (status != "failed"),
        "summary": summary,
        "spent": {"eth": Decimal(str(round(gas_eth, 9))), "label": "газ возврата на Bitget"},
        "raw": {"amount_eth": val_eth, "address": dest, "tx_hash": tx_hash,
                "status": status, "bitget_credit": credit},
    }
