# -*- coding: utf-8 -*-
"""
Доп. функции Bitget для батч-режима: баланс ETH мейна, UID, субаккаунты и их балансы,
перевод суб→мейн, детект прихода депозита (в т.ч. на субаккаунт).
Переиспользует подписанный запрос withdraw_eth._request (ключи берём из общего конфига).
"""

import time
import uuid
from decimal import Decimal

import paths  # noqa: F401
import withdraw_eth as bg

# Сколько ждать разморозки свежего депозита при своде субакк→мейн, сек (Bitget лочит
# поступившие монеты на время). 0 = без лимита. Ctrl+C прерывает ожидание.
SWEEP_TIMEOUT_S = 1200          # ~20 мин на аккаунт; финальный свод — короче


def use_keys(cfg):
    """Прокинуть ключи Bitget из конфига в клиент withdraw_eth."""
    bg.API_KEY = cfg.bitget["api_key"]
    bg.API_SECRET = cfg.bitget["api_secret"]
    bg.API_PASSPHRASE = cfg.bitget["api_passphrase"]


def _dec(x):
    try:
        return Decimal(str(x))
    except Exception:
        return Decimal(0)


def _amt(d):
    s = format(Decimal(d), "f")
    return (s.rstrip("0").rstrip(".") or "0") if "." in s else s


def main_eth_balance(coin="ETH"):
    """Доступный баланс монеты на споте МАЙН-аккаунта. -> Decimal."""
    data = bg._request("GET", "/api/v2/spot/account/assets", params={"coin": coin}, auth=True)
    for a in (data or []):
        if str(a.get("coin", "")).upper() == coin.upper():
            return _dec(a.get("available", "0"))
    return Decimal(0)


def main_uid():
    """UID мейн-аккаунта (для переводов суб→мейн)."""
    data = bg._request("GET", "/api/v2/spot/account/info", auth=True)
    return str((data or {}).get("userId") or "")


def sub_eth_balances(coin="ETH"):
    """Баланс монеты по ВСЕМ субаккаунтам. -> {uid(str): Decimal}."""
    res = {}
    data = bg._request("GET", "/api/v2/spot/account/subaccount-assets", auth=True)
    for entry in (data or []):
        uid = str(entry.get("userId") or entry.get("id") or "")
        bal = Decimal(0)
        for a in (entry.get("assetsList") or []):
            if str(a.get("coin", "")).upper() == coin.upper():
                bal = _dec(a.get("available", "0"))
        if uid:
            res[uid] = bal
    return res


def subaccount_transfer(coin, amount, from_uid, to_uid):
    """Перевод coin со спота субаккаунта from_uid на спот to_uid (обычно мейн)."""
    body = {
        "fromType": "spot", "toType": "spot", "amount": _amt(amount), "coin": coin,
        "fromUserId": str(from_uid), "toUserId": str(to_uid), "clientOid": uuid.uuid4().hex,
    }
    return bg._request("POST", "/api/v2/spot/wallet/subaccount-transfer", body_obj=body, auth=True)


def sweep_sub_to_main(coin, uid, muid=None, first_delay=0, poll_s=30, timeout_s=SWEEP_TIMEOUT_S, log=print):
    """Перевести доступный coin с субакка uid на мейн, ДОЖИДАЯСЬ разморозки.
    Bitget держит свежий депозит залоченным какое-то время. Первая попытка — через
    first_delay сек (для свежего депозита ставим ~2 мин), далее каждые poll_s, пока не
    пройдёт или не истечёт timeout_s (0 = без лимита; Ctrl+C прервёт). Лишнего не пишем:
    одна строка «жду разморозки» + редкий пульс + результат.
    -> сколько переведено (Decimal); 0, если так и не удалось."""
    if not muid:
        try:
            muid = main_uid()
        except Exception:
            muid = ""
    log(f"  жду разморозки депозита на Bitget (обычно ~10 минут), затем переведу субаккаунт {uid} → мейн…")
    start = time.time()
    if first_delay > 0:
        time.sleep(min(first_delay, timeout_s) if timeout_s else first_delay)
    while True:
        el = int(time.time() - start)
        if not muid:
            try:
                muid = main_uid()
            except Exception:
                muid = ""
        try:
            avail = sub_eth_balances(coin).get(uid, Decimal(0))
        except Exception:
            avail = Decimal(0)
        if muid and avail > 0:
            try:
                subaccount_transfer(coin, avail, uid, muid)
                log(f"  ✅ разморозилось — перевёл на мейн {_amt(avail)} {coin} (субакк {uid})")
                return avail
            except Exception:
                pass        # ещё заморожено — тихо ждём дальше
        if timeout_s and el >= timeout_s:
            log(f"  ⚠ за ~{max(1, el // 60)} мин разморозки не дождался — оставляю на субакке {uid} (подберётся при следующем запуске).")
            return Decimal(0)
        time.sleep(poll_s)


def sweep_all_subs_to_main(coin="ETH", poll_s=20, timeout_s=300, log=print):
    """Свести весь ETH со всех субаккаунтов на мейн (с ожиданием разморозки, но
    финальный свод ждёт меньше — это уборка в конце). -> сколько всего переведено (Decimal)."""
    try:
        muid = main_uid()
        subs = sub_eth_balances(coin)
    except Exception as e:
        log(f"  ⚠ не смог прочитать субаккаунты: {e}")
        return Decimal(0)
    moved = Decimal(0)
    for uid, bal in subs.items():
        if bal > 0:
            moved += sweep_sub_to_main(coin, uid, muid, poll_s=poll_s, timeout_s=timeout_s, log=log)
    return moved


def wait_credit_and_sweep(coin, expected_eth, poll_s=20, timeout_s=0, log=print):
    """
    Ждёт прихода ~expected_eth на Bitget (мейн ИЛИ субакк), детект по росту баланса.
    Если пришло на субакк → сразу переводит на мейн. Ctrl+C прерывает.
    -> ('main' | 'sub:<uid>' | 'timeout', пришло Decimal).
    """
    try:
        muid = main_uid()
    except Exception:
        muid = ""
    main0 = main_eth_balance(coin)
    subs0 = sub_eth_balances(coin)
    target = _dec(expected_eth) * Decimal("0.5")        # порог детекта (с запасом)
    start = time.time()
    warned = False
    while True:
        el = int(time.time() - start)
        try:
            main1 = main_eth_balance(coin)
            subs1 = sub_eth_balances(coin)
        except Exception:
            main1, subs1 = main0, subs0
        if main1 - main0 >= target:
            log(f"  ✅ пришло на мейн Bitget: +{_amt(main1 - main0)} {coin}")
            return "main", main1 - main0
        for uid, b1 in subs1.items():
            inc = b1 - subs0.get(uid, Decimal(0))
            if inc >= target:
                log(f"  ↪ пришло на субакк {uid}: +{_amt(inc)} {coin} — перевожу на мейн (жду разморозки депозита)…")
                moved = sweep_sub_to_main(coin, uid, muid, first_delay=120, poll_s=30, log=log)
                return ("main" if moved > 0 else f"sub:{uid}"), (moved if moved > 0 else inc)
        if timeout_s and el >= timeout_s:
            return "timeout", Decimal(0)
        if not warned and el >= 600:
            warned = True
            log("  ⏳ депозит на Bitget задерживается — продолжаю ждать (Ctrl+C прервёт).")
        time.sleep(poll_s)
