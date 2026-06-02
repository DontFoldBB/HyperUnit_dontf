# -*- coding: utf-8 -*-
"""
Стадия 4: вывод UETH с Hyperliquid обратно в сеть Ethereum через мост Unit.
Переиспользует hyperliquid_withdrawal/hyperliquid_withdrawal.py (план, проверка
подписей, spotSend/sendAsset с авто-fallback, ожидание зачисления).
Адрес назначения по умолчанию = наш же EVM-адрес (из приватника).
"""

from types import SimpleNamespace
from decimal import Decimal

import paths  # noqa: F401
import colors as C
import hyperliquid_withdrawal as hw


def run(cfg, live):
    cfg.require_private_key()
    wcfg = cfg.withdraw_cfg

    args = SimpleNamespace(
        testnet=False,
        no_verify=False,
        no_gas_check=False,
        dry_run=(not live),
        min_amount=None,
        dest=None,                       # назначение = свой EVM-адрес
        amount=str(wcfg.get("amount", "all")),
        wait=bool(wcfg.get("wait", True)),
        wait_timeout=float(wcfg.get("wait_timeout_min", 30.0)),
        poll=int(wcfg.get("poll_sec", 20)),
    )

    print(C.step("\n=== [4] Вывод ETH с Hyperliquid → Ethereum через Unit ==="))

    guardians = hw.MAINNET_GUARDIANS
    fees_eth = hw.estimate_fees(False)
    live_min, src = hw.fetch_min("ETH")
    args.min_amount = live_min if live_min is not None else hw.FALLBACK_MIN
    print(f"  Минимум вывода: {hw.fmt_eth(args.min_amount)} ETH ({src})")

    try:
        token = hw.hl_find_token(False)
    except Exception as e:
        return {"stage": "withdraw", "ok": False,
                "summary": f"Hyperliquid: {e}", "spent": {}, "raw": {}}

    w = {"pk": cfg.private_key, "dest": None, "amount": args.amount}
    plan = hw.plan_wallet(token, guardians, w, args)
    if not plan.get("ok"):
        return {"stage": "withdraw", "ok": False,
                "summary": f"пропуск ({plan.get('reason')})", "spent": {}, "raw": {}}

    print(f"  Кошелёк: {plan['sender']}")
    print(f"  Вывод {hw.fmt_eth(plan['amount'])} ETH → {plan['dest']} "
          f"(через HL-адрес {hw.short(plan['protocol_addr'])}, подписи {plan.get('verify_count')}/3)")

    # Снимок существующих операций ДО отправки (чтобы найти именно нашу).
    if args.wait and not args.dry_run:
        try:
            ops = hw.fetch_withdraw_ops({plan["sender"], plan["dest"]}, args.testnet)
            plan["snapshot_ids"] = {op.get("operationId") or op.get("sourceTxHash") for op in ops}
        except Exception:
            plan["snapshot_ids"] = set()

    res = hw.send_withdraw(token, plan, args)
    res["dest"] = plan["dest"]
    res["snapshot_ids"] = plan.get("snapshot_ids", set())
    res["needs_activation"] = plan.get("needs_activation", False)

    unit_fee = hw._unit_fee_dec(fees_eth)
    gas = hw.ACTIVATION_FEE if res.get("needs_activation") else Decimal(0)

    if res.get("dry_run"):
        amt = res.get("amount_str", "?")
        return {"stage": "withdraw", "ok": True, "planned": True,
                "summary": f"[ТЕСТ] вывел бы {amt} ETH (на Ethereum ~{hw.fmt_eth(Decimal(amt) - unit_fee)} после комиссии Unit)",
                "spent": {"eth": unit_fee, "usdc": gas, "label": "комиссия Unit (+газ активации)"},
                "raw": {"amount_eth": amt}}

    if not res.get("ok"):
        return {"stage": "withdraw", "ok": False,
                "summary": f"ошибка: {res.get('error')}", "spent": {}, "raw": res}

    print(f"  ✓ Отправлено на Hyperliquid ({res.get('method')}, nonce {res.get('nonce')})")

    # Ожидание зачисления на Ethereum.
    if args.wait:
        sent = [{"sender": plan["sender"], "result": res}]
        to_s = args.wait_timeout * 60
        cap = "без таймаута" if to_s <= 0 else f"до {args.wait_timeout:g} мин"
        print(f"  Жду зачисления на Ethereum ({cap}, проверка каждые {args.poll}с; Ctrl+C — прервать)…")
        try:
            hw.wait_for_all(sent, args.testnet, to_s, args.poll)
        except KeyboardInterrupt:
            print("\n  …опрос прерван (вывод уже отправлен).")

    amt = res.get("amount_str", "?")
    net = hw.fmt_eth(Decimal(amt) - unit_fee)
    credit = res.get("credit", "sent")
    note = {"done": "ETH пришёл на Ethereum ✅", "timeout": "ещё в обработке ⏳",
            "failure": "ошибка операции ❌"}.get(credit, "отправлено")
    summary = f"выведено {amt} ETH (на Ethereum ~{net} после комиссии Unit) — {note}"
    print(f"  --- {summary}")
    return {
        "stage": "withdraw", "ok": (credit != "failure"),
        "summary": summary,
        "spent": {"eth": unit_fee, "usdc": gas, "label": "комиссия Unit (+газ активации)"},
        "raw": {"amount_eth": amt, "net_eth": net, "method": res.get("method"),
                "credit": credit, "dst_tx": res.get("dst_tx", "")},
    }
