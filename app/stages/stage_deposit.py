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


MAX_DEPOSIT_ATTEMPTS = 5       # сетевой сбой ДО отправки tx → повторяем столько раз
DEPOSIT_RETRY_WAIT_S = 20      # пауза между попытками депозита, сек


def _norm_hash(h):
    """tx-хэш в вид 0x... lower (принимает str или HexBytes)."""
    h = h if isinstance(h, str) else h.hex()
    if not h.startswith("0x"):
        h = "0x" + h
    return h.lower()


def run(cfg, live):
    """Депозит с РЕТРАЯМИ при сетевых сбоях ДО отправки транзакции (инет пропал/RPC/Unit).
    После того как tx ушла в сеть, повтор НЕ делается — чтобы не задвоить депозит."""
    cfg.require_private_key()
    pct = float(cfg.deposit_cfg["percent"])
    if not (0 < pct <= 100):
        return {"stage": "deposit", "ok": False,
                "summary": f"deposit_percent должен быть 0..100, а не {pct}", "spent": {}, "raw": {}}

    print(C.step("\n=== [2] Депозит ETH → Hyperliquid через Unit ==="))
    last = None
    for attempt in range(1, MAX_DEPOSIT_ATTEMPTS + 1):
        if attempt > 1:
            print(C.warn(f"  ↻ Повтор депозита {attempt}/{MAX_DEPOSIT_ATTEMPTS}…"))
        res = _attempt_deposit(cfg, live)
        if res.get("ok") or not res.get("retryable"):
            res.pop("retryable", None)
            return res
        last = res
        print(C.warn(f"  ⏳ депозит не удался (попытка {attempt}/{MAX_DEPOSIT_ATTEMPTS}): {res.get('summary')}"))
        if attempt < MAX_DEPOSIT_ATTEMPTS:
            time.sleep(DEPOSIT_RETRY_WAIT_S)
    print(C.err(f"  ✗ депозит не удался после {MAX_DEPOSIT_ATTEMPTS} попыток."))
    (last or {}).pop("retryable", None)
    return last or {"stage": "deposit", "ok": False, "summary": "депозит не удался", "spent": {}, "raw": {}}


def _attempt_deposit(cfg, live):
    """Одна попытка депозита. retryable=True → сетевой сбой ДО отправки tx (безопасно повторить)."""
    key = cfg.private_key
    pct = float(cfg.deposit_cfg["percent"])

    # --- подключение к RPC + чтение состояния (сетевое → повторяемо) ---
    try:
        w3, rpc = _connect(cfg)
        if w3 is None:
            return {"stage": "deposit", "ok": False, "retryable": True,
                    "summary": "не удалось подключиться к ETH RPC (ни свой, ни публичные)",
                    "spent": {}, "raw": {}}
        print(f"  RPC: {rpc}" + ("" if cfg.eth_rpc_url else " (публичный)"))
        chain_id = w3.eth.chain_id
        if chain_id != 1:
            return {"stage": "deposit", "ok": False, "retryable": False,
                    "summary": f"RPC не Ethereum mainnet (chainId={chain_id}, нужен 1) — проверь ETH_RPC_URL в .env.",
                    "spent": {}, "raw": {}}
        acct = w3.eth.account.from_key(key)
        sender = acct.address               # он же HL-аккаунт назначения
        balance = w3.eth.get_balance(sender)
        print(f"  Кошелёк: {sender}")
        print(f"  Баланс:  {ud.eth(balance)} ETH | вношу {pct:g}%")
        fees = ud.get_fees(w3)
        try:
            gas_limit = int(w3.eth.estimate_gas({"from": sender, "to": sender, "value": 1}) * 1.2)
        except Exception:
            gas_limit = 21000
        gas_reserve = gas_limit * fees["per_gas"]
        min_eth, min_src = ud.fetch_min_deposit()
        min_wei = Web3.to_wei(min_eth, "ether")
    except Exception as e:
        return {"stage": "deposit", "ok": False, "retryable": True,
                "summary": f"сетевой сбой при подготовке (RPC/чтение баланса): {e}", "spent": {}, "raw": {}}

    amount = int(balance * pct / 100)
    capped = False
    if amount + gas_reserve > balance:
        amount = balance - gas_reserve
        capped = True
    if amount <= 0:
        return {"stage": "deposit", "ok": False, "retryable": False,
                "summary": f"не хватает на газ: баланс {ud.eth(balance)} ETH, газ ~{ud.eth(gas_reserve, 8)} ETH",
                "spent": {}, "raw": {}}
    if amount < min_wei:
        return {"stage": "deposit", "ok": False, "retryable": False,
                "summary": f"сумма {ud.eth(amount)} ETH ниже минимума Unit ({min_eth} ETH, {min_src}) — не зачислится",
                "spent": {}, "raw": {}}
    if capped:
        print(f"  ⚠ Урезал сумму до {ud.eth(amount)} ETH, чтобы осталось на газ (~{ud.eth(gas_reserve, 8)} ETH).")

    # --- депозит-адрес Unit + подписи гардианов (сетевое → повторяемо) ---
    try:
        res = ud.gen_deposit_address(sender)
        deposit_raw = res["address"]
        deposit = Web3.to_checksum_address(deposit_raw)
        ok, count = ud.verify_signatures(sender, deposit_raw, res.get("signatures", {}))
    except Exception as e:
        return {"stage": "deposit", "ok": False, "retryable": True,
                "summary": f"сетевой сбой при получении депозит-адреса Unit: {e}", "spent": {}, "raw": {}}
    if not ok:
        return {"stage": "deposit", "ok": False, "retryable": True,
                "summary": f"подписи гардианов Unit НЕ прошли ({count}/{ud.GUARDIAN_THRESHOLD}) — повтор "
                           "(возможно, неполный ответ из-за сети)", "spent": {}, "raw": {}}

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

    # --- подготовка транзакции (сетевое чтение nonce → повторяемо) ---
    try:
        nonce = w3.eth.get_transaction_count(sender)
        tx = ud.build_tx(chain_id, fees, nonce, deposit, amount, gas_limit)
        signed = w3.eth.account.sign_transaction(tx, key)
        raw = getattr(signed, "raw_transaction", None) or getattr(signed, "rawTransaction")
        known_hash = _norm_hash(getattr(signed, "hash"))
    except Exception as e:
        return {"stage": "deposit", "ok": False, "retryable": True,
                "summary": f"сетевой сбой при подготовке транзакции: {e}", "spent": {}, "raw": {}}

    # --- ОТПРАВКА: повторяем отправку ОДНОГО И ТОГО ЖЕ подписанного raw (тот же nonce) —
    #     это идемпотентно, задвоить депозит нельзя. ---
    tx_hash, last_err = None, None
    for _ in range(4):
        try:
            txh = w3.eth.send_raw_transaction(raw)
            tx_hash = _norm_hash(txh.hex())
            break
        except Exception as e:
            last_err = e
            m = str(e).lower()
            if any(s in m for s in ("already known", "nonce too low", "already imported", "known transaction")):
                tx_hash = known_hash      # транзакция уже в сети
                break
            print(C.dim(f"    отправка tx не прошла ({e}) — повтор того же raw…"))
            time.sleep(5)

    if tx_hash is None:
        # Отправка не удалась. Если точно НЕ в сети (nonce не сдвинулся) — безопасно повторить
        # весь депозит. Если статус неизвестен — НЕ повторяем (риск двойного депозита).
        in_chain = "unknown"
        try:
            in_chain = w3.eth.get_transaction(known_hash)
        except Exception:
            in_chain = "unknown"
        if in_chain and in_chain != "unknown":
            tx_hash = known_hash          # всё-таки ушла
        elif in_chain == "unknown":
            return {"stage": "deposit", "ok": False, "retryable": False,
                    "summary": f"отправка депозита не подтверждена (сеть): {last_err}. Проверь tx "
                               f"{known_hash} на etherscan вручную, чтобы не задвоить депозит.",
                    "spent": {}, "raw": {"tx_hash": known_hash}}
        else:
            return {"stage": "deposit", "ok": False, "retryable": True,
                    "summary": f"транзакция депозита не ушла (сеть): {last_err} — повтор", "spent": {}, "raw": {}}

    print(f"  ✓ Отправлено: {ud.eth(amount)} ETH | tx: {tx_hash}")

    # --- tx уже в сети: квитанция + ожидание зачисления (повтор НЕ делаем) ---
    gas_eth_actual = None
    try:
        rcpt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=300, poll_latency=5)
        eff = rcpt.get("effectiveGasPrice") or fees["per_gas"]
        gas_eth_actual = rcpt["gasUsed"] * eff
    except Exception as e:
        print(f"  (не дождался квитанции для точного газа: {e})")

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
