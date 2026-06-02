# -*- coding: utf-8 -*-
"""
Стадия 2: депозит % ETH с нашего кошелька на Hyperliquid через мост Unit.
Переиспользует хелперы hyperunit_deposit/hyperunit_deposit.py (генерация адреса,
проверка подписей гардианов, газ, ожидание зачисления). Приходит как спот UETH.
"""

import time
from decimal import Decimal

import paths  # noqa: F401
import colors as C
from web3 import Web3
import hyperunit_deposit as ud

# Публичные RPC Ethereum (используются по умолчанию, если в .env не задан свой ETH_RPC_URL).
PUBLIC_RPCS = [
    "https://ethereum-rpc.publicnode.com",
    "https://eth.drpc.org",
    "https://1rpc.io/eth",
    "https://rpc.ankr.com/eth",
]

# После этого времени (сек) считаем, что зачисление "задерживается" — но НЕ бросаем ждать.
SOFT_DELAY_S = 480


def _wait_credit(sender, tx_hash, poll_s=30, hard_timeout_s=0):
    """Ждёт зачисления на Hyperliquid ПОКА не придёт (done) или не упадёт (failure).
    hard_timeout_s=0 → без жёсткого лимита (только Ctrl+C). После SOFT_DELAY_S один раз
    сообщает, что задерживается, и продолжает ждать. -> 'done'|'failure'|'timeout'."""
    start = time.time()
    last_label = None
    warned = False
    while True:
        try:
            op = ud.find_op(ud.get_operations(sender), tx_hash)
        except Exception:
            op = None
        el = int(time.time() - start)
        label = ud.state_label(op) if op else "ждём, пока Unit увидит транзакцию"
        if label != last_label:
            last_label = label
            print(f"    [{el:>4}с] {label}")
        if op:
            st = str(op.get("state", "")).lower()
            if st == "done":
                return "done"
            if st in ("failure", "failed"):
                return "failure"
        if not warned and el >= SOFT_DELAY_S:
            warned = True
            print(C.warn("  ⏳ Зачисление задерживается (обычно ~3–8 мин). Продолжаю ждать — "
                         "деньги дойдут сами. Ctrl+C — перестать ждать (статус: app.hyperunit.xyz)."))
        if hard_timeout_s and el >= hard_timeout_s:
            return "timeout"
        time.sleep(poll_s)


def _connect(cfg):
    """Подключиться к ETH RPC: свой из .env, иначе перебрать публичные. -> (w3|None, url|None)."""
    candidates = [cfg.eth_rpc_url] if cfg.eth_rpc_url else list(PUBLIC_RPCS)
    for url in candidates:
        try:
            w3 = Web3(Web3.HTTPProvider(url, request_kwargs={"timeout": 30}))
            if w3.is_connected():
                return w3, url
        except Exception:
            continue
    return None, None


def run(cfg, live):
    cfg.require_private_key()

    key = cfg.private_key
    pct = float(cfg.deposit_cfg["percent"])
    if not (0 < pct <= 100):
        return {"stage": "deposit", "ok": False,
                "summary": f"unit_deposit.percent должен быть 0..100, а не {pct}",
                "spent": {}, "raw": {}}

    print(C.step("\n=== [2] Депозит ETH → Hyperliquid через Unit ==="))

    w3, rpc = _connect(cfg)
    if w3 is None:
        return {"stage": "deposit", "ok": False,
                "summary": "не удалось подключиться к ETH RPC (ни свой, ни публичные)",
                "spent": {}, "raw": {}}
    print(f"  RPC: {rpc}" + ("" if cfg.eth_rpc_url else " (публичный)"))
    chain_id = w3.eth.chain_id
    if chain_id != 1:
        print(f"  ⚠ chainId={chain_id} — это не Ethereum mainnet (Unit работает с mainnet=1).")

    acct = w3.eth.account.from_key(key)
    sender = acct.address               # он же HL-аккаунт назначения
    balance = w3.eth.get_balance(sender)
    print(f"  Кошелёк: {sender}")
    print(f"  Баланс:  {ud.eth(balance)} ETH | вношу {pct:g}%")

    # Газ и проверка «хватает ли на комиссию».
    fees = ud.get_fees(w3)
    try:
        gas_limit = int(w3.eth.estimate_gas({"from": sender, "to": sender, "value": 1}) * 1.2)
    except Exception:
        gas_limit = 21000
    gas_reserve = gas_limit * fees["per_gas"]
    min_eth, min_src = ud.fetch_min_deposit()
    min_wei = Web3.to_wei(min_eth, "ether")

    amount = int(balance * pct / 100)
    capped = False
    if amount + gas_reserve > balance:
        amount = balance - gas_reserve
        capped = True

    if amount <= 0:
        return {"stage": "deposit", "ok": False,
                "summary": f"не хватает на газ: баланс {ud.eth(balance)} ETH, газ ~{ud.eth(gas_reserve, 8)} ETH",
                "spent": {}, "raw": {}}
    if amount < min_wei:
        return {"stage": "deposit", "ok": False,
                "summary": f"сумма {ud.eth(amount)} ETH ниже минимума Unit ({min_eth} ETH, {min_src}) — не зачислится",
                "spent": {}, "raw": {}}
    if capped:
        print(f"  ⚠ Урезал сумму до {ud.eth(amount)} ETH, чтобы осталось на газ (~{ud.eth(gas_reserve, 8)} ETH).")

    # Депозит-адрес Unit + проверка подписей гардианов.
    res = ud.gen_deposit_address(sender)
    deposit_raw = res["address"]
    deposit = Web3.to_checksum_address(deposit_raw)
    ok, count = ud.verify_signatures(sender, deposit_raw, res.get("signatures", {}))
    if not ok:
        return {"stage": "deposit", "ok": False,
                "summary": f"подписи гардианов Unit НЕ прошли ({count}/{ud.GUARDIAN_THRESHOLD}) — адрес мог быть подменён",
                "spent": {}, "raw": {}}

    fee_unit, eta = ud.estimate_deposit_fee()
    price = ud.eth_price_usd()
    usd_str = f", ~${float(Web3.from_wei(amount, 'ether')) * price:.2f}" if price else ""
    print(f"  Вношу:          {ud.eth(amount)} ETH ({pct:g}%{usd_str})")
    print(f"  На Hyperliquid: {sender} (придёт как спот UETH)")
    print(f"  Депозит-адрес:  {deposit} | подписи Unit: {count}/3 ✓")
    print(f"  Газ (оценка):   ~{ud.eth(gas_reserve, 8)} ETH"
          + (f" | комиссия Unit ~{fee_unit} ETH (ETA ~{eta})" if fee_unit is not None else ""))

    deposit_eth_f = float(Web3.from_wei(amount, "ether"))
    unit_fee_eth = float(fee_unit) if fee_unit is not None else 0.0

    if not live:
        total_fee = float(Web3.from_wei(int(gas_reserve), "ether")) + unit_fee_eth
        return {"stage": "deposit", "ok": True, "planned": True,
                "summary": f"[ТЕСТ] внёс бы {ud.eth(amount)} ETH (придёт ~{deposit_eth_f - unit_fee_eth:.6f} UETH)",
                "spent": {"eth": Decimal(str(round(total_fee, 9))), "label": "газ+комиссия Unit (план)"},
                "raw": {"amount_eth": deposit_eth_f, "unit_fee_eth": unit_fee_eth}}

    # Отправка ETH-транзакции на депозит-адрес.
    nonce = w3.eth.get_transaction_count(sender)
    tx = ud.build_tx(chain_id, fees, nonce, deposit, amount, gas_limit)
    signed = w3.eth.account.sign_transaction(tx, key)
    raw = getattr(signed, "raw_transaction", None) or getattr(signed, "rawTransaction")
    txh = w3.eth.send_raw_transaction(raw)
    tx_hash = txh.hex()
    if not tx_hash.startswith("0x"):
        tx_hash = "0x" + tx_hash
    tx_hash = tx_hash.lower()
    print(f"  ✓ Отправлено: {ud.eth(amount)} ETH | tx: {tx_hash}")

    # Фактический газ из квитанции.
    gas_eth_actual = None
    try:
        rcpt = w3.eth.wait_for_transaction_receipt(txh, timeout=300, poll_latency=5)
        eff = rcpt.get("effectiveGasPrice") or fees["per_gas"]
        gas_eth_actual = rcpt["gasUsed"] * eff
    except Exception as e:
        print(f"  (не дождался квитанции для точного газа: {e})")

    # Ожидание зачисления на Hyperliquid: ждём, ПОКА не придёт (или ошибка / Ctrl+C).
    status = "sent"
    if cfg.deposit_cfg.get("wait_credit", True):
        hard_min = float(cfg.deposit_cfg.get("wait_timeout_min", 0) or 0)
        cap = "без лимита, пока не придёт" if hard_min <= 0 else f"до {hard_min:g} мин"
        print(f"  Жду зачисления на Hyperliquid (опрос каждые 30с, {cap}; Ctrl+C — перестать ждать)…")
        try:
            status = _wait_credit(sender, tx_hash, poll_s=30, hard_timeout_s=int(hard_min * 60))
        except KeyboardInterrupt:
            status = "interrupted"
            print(C.warn("\n  ⏸ Ожидание прервано — депозит уже отправлен, придёт сам "
                         "(проверь app.hyperliquid.xyz / app.hyperunit.xyz)."))
        if status == "done":
            print(C.ok("  ✅ Деньги на Hyperliquid (Spot, токен UETH)."))
        elif status == "failure":
            print(C.err("  ❌ Операция Unit завершилась ошибкой — проверь app.hyperunit.xyz."))
        elif status == "timeout":
            print(C.warn("  ⏳ Истёк заданный лимит ожидания — депозит дойдёт сам, проверь позже."))

    gas_for_log = gas_eth_actual if gas_eth_actual is not None else gas_reserve
    total_fee = float(Web3.from_wei(int(gas_for_log), "ether")) + unit_fee_eth
    received = deposit_eth_f - unit_fee_eth
    summary = (f"внесено {deposit_eth_f:.6f} ETH (статус {status}); "
               f"придёт ~{received:.6f} UETH; газ {Web3.from_wei(int(gas_for_log), 'ether'):.8f} + "
               f"Unit {unit_fee_eth:.8f} ETH")
    return {
        "stage": "deposit", "ok": (status != "failure"),
        "summary": summary,
        "spent": {"eth": Decimal(str(round(total_fee, 9))), "label": "газ + комиссия Unit"},
        "raw": {"amount_eth": deposit_eth_f, "received_ueth": received,
                "gas_eth": float(Web3.from_wei(int(gas_for_log), "ether")),
                "unit_fee_eth": unit_fee_eth, "tx_hash": tx_hash, "status": status},
    }
