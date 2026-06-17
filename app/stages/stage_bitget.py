# -*- coding: utf-8 -*-
"""
Стадия 1: вывод ETH (сеть Ethereum) с Bitget на наш EVM-кошелёк.
Переиспользует логику bitget_withdrawal/withdraw_eth.py. Адрес получателя
вычисляется из общего приватника (PRIVATE_KEY). Сумма берётся из config.json.

amount_eth можно задать числом ("0.01"), "all" (весь баланс) или "NN%" — для "all"/"%"
ключу Bitget нужно право чтения спота (иначе подскажет задать число).
"""

import time
import random
from decimal import Decimal, ROUND_DOWN

import paths  # noqa: F401
import colors as C
import withdraw_eth as bg


def _apply_keys(cfg):
    bg.API_KEY = cfg.bitget["api_key"]
    bg.API_SECRET = cfg.bitget["api_secret"]
    bg.API_PASSPHRASE = cfg.bitget["api_passphrase"]


def _spot_balance(coin="ETH"):
    """Доступный баланс монеты на споте Bitget. -> Decimal | None (None = не прочитать,
    напр. у ключа нет права чтения спота)."""
    try:
        data = bg._request("GET", "/api/v2/spot/account/assets", params={"coin": coin}, auth=True)
    except Exception:
        return None
    for a in (data or []):
        if str(a.get("coin", "")).upper() == coin.upper():
            try:
                return Decimal(str(a.get("available", "0")))
            except Exception:
                return None
    return Decimal(0)            # монета не найдена на споте → 0


_ALL = ("all", "max", "всё", "все", "100%")


def _need_balance_err(spec):
    return {"stage": "bitget", "ok": False,
            "summary": f"для amount '{spec}' нужен баланс Bitget, но прочитать не вышло — "
                       f"дай ключу право Spot (чтение) или укажи число ETH вместо '{spec}'",
            "spent": {}, "raw": {}}


def _submit_with_retry(address, amount, unlock_wait_min, on_other_error,
                       sleep_fn=time.sleep, now_fn=time.time):
    """Отправить вывод с Bitget, переживая лок свежего депозита.
      • 13008 «withdrawable amount: 0» — деньги ещё НЕ разморожены для вывода (возврат с прошлого
        кошелька только зачислился): ждём и повторяем САМИ до unlock_wait_min минут (это не вайтлист).
      • прочие ошибки Bitget — отдаём on_other_error(e) -> 'retry' | 'skip'.
    Возвращает res (dict) или None (пропустить кошелёк). sleep_fn/now_fn вынесены для тестов."""
    deadline = now_fn() + max(0.0, float(unlock_wait_min)) * 60
    last_note = 0.0
    while True:
        try:
            return bg.submit_withdrawal(address, amount)
        except bg.BitgetError as e:
            msg = str(e)
            if ("13008" in msg or "withdrawable amount" in msg.lower()) and now_fn() < deadline:
                t = now_fn()
                if t - last_note >= 60:                 # не спамим: заметка раз в ~минуту
                    left = int(deadline - t)
                    print(C.warn(f"  ⏳ Bitget: средства ещё залочены для вывода (свежий депозит с прошлого "
                                 f"кошелька зачислился, но не разморожен) — жду и повторяю… осталось ~{max(1, left // 60)} мин"))
                    last_note = t
                sleep_fn(30)
                continue
            if on_other_error(e) == "skip":
                return None
            # 'retry' — повторить отправку


def run(cfg, live):
    cfg.require_private_key()
    cfg.require_bitget()
    _apply_keys(cfg)

    rpc_urls = ([cfg.eth_rpc_url] if cfg.eth_rpc_url else []) + bg.DEFAULT_RPCS
    bcfg = cfg.bitget_cfg

    print(C.step("\n=== [1] Вывод ETH с Bitget ==="))

    # Параметры сети ETH (комиссия/минимум/точность) — публичный справочник.
    chain = bg.eth_chain_info()
    fee = Decimal(chain["withdrawFee"])
    mn = Decimal(chain["minWithdrawAmount"])
    scale = int(chain.get("withdrawMinScale") or 8)

    # Адрес получателя из приватника.
    address, derived = bg.resolve_destination(None, cfg.private_key)

    # Баланс ETH на споте Bitget (если у ключа есть право чтения спота).
    balance = _spot_balance("ETH")
    if balance is not None:
        print(f"  Баланс на Bitget:     {balance:f} ETH")

    # Сумма: число ("0.01") | "all" | "NN%" | "FROM-TO%" (случ.% от ETH-баланса) | "FROM-TO" (случ. ETH)
    spec = str(bcfg["amount_eth"]).strip().lower()
    note = ""
    if spec in _ALL:
        if balance is None:
            return _need_balance_err(spec)
        amount, note = balance, "  (весь баланс)"
    elif spec.endswith("%"):
        if balance is None:
            return _need_balance_err(spec)
        body = spec[:-1]
        try:
            if "-" in body:
                lo, hi = (float(x) for x in body.split("-", 1))
                pct = random.uniform(lo, hi)
                note = f"  (случайно {pct:.1f}% из {body}%)"
            else:
                pct = float(body)
            amount = balance * (Decimal(str(pct)) / Decimal(100))
        except Exception:
            return {"stage": "bitget", "ok": False,
                    "summary": f"некорректный процент: {bcfg['amount_eth']}", "spent": {}, "raw": {}}
    elif "-" in spec and all(c in "0123456789.-" for c in spec):
        try:
            lo, hi = (float(x) for x in spec.split("-", 1))
            v = random.uniform(lo, hi)
            amount, note = Decimal(str(v)), f"  (случайно {v:.6f} ETH из {spec})"
        except Exception:
            return {"stage": "bitget", "ok": False,
                    "summary": f"некорректный диапазон ETH: {bcfg['amount_eth']}", "spent": {}, "raw": {}}
    else:
        try:
            amount = Decimal(str(bcfg["amount_eth"]))
        except Exception:
            return {"stage": "bitget", "ok": False,
                    "summary": f"bitget_amount_eth: число (\"0.01\"), \"all\", \"NN%\", \"30-50%\" или "
                               f"\"0.01-0.03\" — а не '{bcfg['amount_eth']}'", "spent": {}, "raw": {}}

    if -amount.as_tuple().exponent > scale:                 # обрезать лишнюю точность вниз
        amount = amount.quantize(Decimal(1).scaleb(-scale), rounding=ROUND_DOWN)
    if balance is not None and amount > balance:
        return {"stage": "bitget", "ok": False,
                "summary": f"запрошено {amount:f} ETH, а на споте Bitget только {balance:f} ETH",
                "spent": {}, "raw": {}}
    if amount < mn:
        return {"stage": "bitget", "ok": False,
                "summary": f"сумма {amount:f} ETH меньше минимума Bitget ({mn} ETH)"
                           + (f"; на споте {balance:f} ETH" if balance is not None else ""),
                "spent": {}, "raw": {}}

    recv_expected = amount - fee
    print(f"  Выводим:              {amount:f} ETH" + note)
    print(f"  Адрес:                {address}" + ("  (из приватника)" if derived else ""))
    print(f"  Комиссия Bitget:      {fee:f} ETH")
    print(f"  Ожидается на кошелёк: ~{recv_expected:f} ETH")

    if not live:
        return {"stage": "bitget", "ok": True, "planned": True,
                "summary": f"[ТЕСТ] вывел бы {amount:f} ETH на {address} "
                           f"(комиссия {fee:f}, на кошелёк ~{recv_expected:f})",
                "spent": {"eth": fee, "label": "комиссия Bitget (план)"},
                "raw": {"amount": str(amount), "fee": str(fee), "address": address}}

    # Баланс кошелька ДО отправки (для детекта зачисления on-chain).
    b0 = bg.eth_balance(address, rpc_urls)
    if b0 is None:
        print("  (!) RPC недоступен — зачисление не отслежу, но вывод отправлю.")

    # Отправка вывода. Свежий депозит (возврат с прошлого кошелька) Bitget держит ЗАЛОЧЕННЫМ для
    # вывода → code 13008 «withdrawable amount: 0»: это НЕ вайтлист, а «ещё не разморожено» —
    # _submit_with_retry сам ждёт и повторяет (до bitget_withdraw_unlock_wait_min мин). На ПРОЧИЕ
    # ошибки Bitget (реальный вайтлист и т.п.) спрашиваем: добавить и повторить, либо пропустить.
    last_err = {}

    def _on_other_error(e):
        last_err["e"] = e
        print(C.err(f"\n  ✗ Bitget отклонил вывод: {e}"))
        print(C.warn(f"    Частая причина: адрес {address} НЕ в вайтлисте вывода Bitget."))
        print(C.dim("    Добавь его: Bitget → Вывод → Управление адресами (whitelist), сеть ETH (ERC-20)."))
        try:
            ans = input(C.bold("    [Enter] — добавил, повторить   |   [s] — пропустить этот кошелёк: ")).strip().lower()
        except EOFError:
            ans = "s"
        if ans in ("s", "skip", "п", "пропуск", "пропустить"):
            return "skip"
        print(C.dim("    Повторяю вывод…"))
        return "retry"

    res = _submit_with_retry(address, amount,
                             float(bcfg.get("withdraw_unlock_wait_min", 30) or 0), _on_other_error)
    if res is None:
        return {"stage": "bitget", "ok": False,
                "summary": f"вывод пропущен (адрес не в вайтлисте Bitget?): {last_err.get('e', '')}",
                "spent": {}, "raw": {"address": address, "skipped": True}}
    order_id = res.get("orderId")
    print(f"  ✓ Заявка отправлена. orderId: {order_id}")

    received, status, txid, real_fee = None, "submitted", None, fee
    timeout = int(bcfg.get("wait_timeout_sec", 900) or 0)
    if bcfg.get("wait_arrival", True) and b0 is not None and timeout > 0:
        print(f"  Жду зачисления на кошелёк (первая проверка через ~60с, потом каждые 15с; до {timeout}s, Ctrl+C — прервать)...")
        start = time.time()
        first_check = True
        last_status = None
        try:
            while time.time() - start < timeout:
                time.sleep(min(60, timeout) if first_check else 15)   # Bitget начинает выводить ~через минуту
                first_check = False
                try:
                    rec = bg.get_withdrawal_record(order_id)
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
                b1 = bg.eth_balance(address, rpc_urls)
                delta = (b1 - b0) if b1 is not None else None
                el = int(time.time() - start)
                if status != last_status:                 # печатаем только смену статуса
                    last_status = status
                    print(f"    [{el}s] Bitget: {status}")
                if delta is not None and delta > 0:
                    print(f"    ✓ пришло на кошелёк: +{format(delta, 'f')} ETH")
                    received = delta
                    break
        except KeyboardInterrupt:
            print("\n  Ожидание прервано (вывод уже отправлен — проверь позже).")

    if received is not None:
        summary = f"выведено {amount:f} ETH, поступило {received:f} ETH (on-chain), комиссия {real_fee:f} ETH"
    else:
        summary = (f"выведено {amount:f} ETH (отправлено, статус {status}); "
                   f"ожидается ~{recv_expected:f} ETH, комиссия {real_fee:f} ETH")
    print(f"  --- {summary}")

    return {
        "stage": "bitget", "ok": True,
        "summary": summary,
        "spent": {"eth": real_fee, "label": "комиссия Bitget"},
        "raw": {"amount": str(amount), "fee": str(real_fee),
                "received": (str(received) if received is not None else ""),
                "address": address, "orderId": order_id, "txId": txid or "", "status": status},
    }
