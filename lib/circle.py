#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
circle.py - ПРОСТОЙ круг на Hyperliquid / app.trade.xyz с набивкой объёма.

Запустил -> вставил приватник -> выбрал что крутить и сколько объёма -> погнал.

Что делает:
  1) продаёт UETH (спот) за USDC
  2) крутит выбранное (по очереди, купить -> подержать -> продать):
       - HIP-3 активы trade.xyz (NVDA, GOLD, CL, SP500 ...), и/или
       - пара BTC+ETH (открываются ВМЕСТЕ: лонг один + шорт другой), или
       - один перп (SOL, HYPE, BTC, ETH ...)
     Если задан целевой объём - повторяет круги, пока не наберёт его, и останавливается.
  3) закрывает все позиции, возвращает USDC на спот, покупает обратно UETH.
Пишет лог в circle_log.csv и итог (траты+объём) в circle_summary.csv (Excel).

Размер: % от проданного эфира = МАРЖА на позицию. Объём позиции = маржа * плечо.
Объём набивки считает покупку И продажу (т.е. ~2 * объём позиции за круг), с учётом плеча.

Запуск:  двойной клик по ЗАПУСТИТЬ.bat  (или  python circle.py)
"""

import csv
import os
import random
import sys
import time
from datetime import datetime
from typing import Any, Dict, List, Tuple

import eth_account
from hyperliquid.exchange import Exchange
from hyperliquid.utils import constants

try:                       # цвета берём из combined/colors.py, если запущено через combined
    import colors as _C
    _paint = _C.paint
except Exception:          # отдельный запуск circle.py — без цветов (заглушка)
    def _paint(t, *_a):
        return t

DEX = "xyz"
ETH_TOKEN = "UETH"
SPOT_SLIPPAGE = 0.01
PERP_SLIPPAGE = 0.05
MIN_ORDER = 10.0
MAX_TRADES = 5000            # предохранитель от бесконечного цикла
MAX_EMPTY = 6                # стоп, если подряд столько сделок не открылось (мало средств/нет цены)
RESERVE_USDC = 1.0          # оставлять на споте при финальной покупке (нужно ~1 USDC
                            #   на аккаунте для вывода ETH через Hyperunit)
# Builder-комиссия (монетизация): адрес и ставка ВШИТЫ. В конфиге — только тумблер builder_codes.
BUILDER_ADDRESS = "0x7dE5Db5d19bB0bfFa8c1fD0c725556D66fBad8a0"
BUILDER_FEE = "0.01%"       # 0.01% = 1 б.п.; макс 0.1% перпы / 1% спот
# Перпы по умолчанию для perp="single" (ликвидные) — крутятся по одному случайно до target_perp.
DEFAULT_PERP_POOL = ["BTC", "ETH", "SOL", "BNB", "XRP", "HYPE", "NEAR", "TON", "WLD"]
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
LOG_CSV = os.path.join(SCRIPT_DIR, "circle_log.csv")
SUMMARY_CSV = os.path.join(SCRIPT_DIR, "circle_summary.csv")

POPULAR = [
    ("NVDA", "Nvidia"), ("TSLA", "Tesla"), ("AAPL", "Apple"), ("MSFT", "Microsoft"),
    ("META", "Meta"), ("AMZN", "Amazon"), ("GOOGL", "Google"), ("COIN", "Coinbase"),
    ("MSTR", "MicroStrategy"), ("PLTR", "Palantir"), ("HOOD", "Robinhood"), ("NFLX", "Netflix"),
    ("GOLD", "Золото"), ("SILVER", "Серебро"), ("PLATINUM", "Платина"), ("COPPER", "Медь"),
    ("CL", "Нефть WTI"), ("BRENTOIL", "Нефть Brent"), ("NATGAS", "Газ"), ("URANIUM", "Уран"),
    ("SP500", "S&P 500"), ("XYZ100", "Индекс XYZ100"), ("NIFTY", "Nifty Индия"), ("VIX", "VIX"),
    ("EUR", "Евро"), ("JPY", "Иена"),
]

for _s in (sys.stdout, sys.stderr, sys.stdin):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


# ----------------------------- ввод ----------------------------- #
def ask(prompt: str, default: str = "") -> str:
    hint = f" [{default}]" if default else ""
    return (input(f"{prompt}{hint}: ").strip() or default)


def ask_float(prompt: str, default: float) -> float:
    while True:
        try:
            return float(ask(prompt, str(default)).replace(",", "."))
        except ValueError:
            print("   нужно число")


def ask_int(prompt: str, default: int) -> int:
    while True:
        try:
            return int(float(ask(prompt, str(default))))
        except ValueError:
            print("   нужно целое число")


def fmt(x: float) -> str:
    return f"${x:,.2f}"


# ----------------------------- биржа ----------------------------- #
def is_ok(resp: Any) -> bool:
    try:
        if not isinstance(resp, dict) or resp.get("status") != "ok":
            return False
        for s in resp.get("response", {}).get("data", {}).get("statuses", []):
            if isinstance(s, dict) and "error" in s:
                return False
        return True
    except Exception:
        return isinstance(resp, dict) and resp.get("status") == "ok"


def fill_info(resp: Any) -> Tuple[float, float]:
    """(объём_usd, цена) из ответа ордера."""
    try:
        for s in resp["response"]["data"]["statuses"]:
            if "filled" in s:
                sz = float(s["filled"]["totalSz"]); px = float(s["filled"]["avgPx"])
                return sz * px, px
    except Exception:
        pass
    return 0.0, 0.0


def spot_free(info, addr: str, coin: str) -> float:
    try:
        for b in info.spot_user_state(addr).get("balances", []):
            if b["coin"] == coin:
                return float(b["total"]) - float(b.get("hold", 0) or 0)
    except Exception:
        pass
    return 0.0


def acct_usdc(info, addr: str, account: str) -> float:
    """Свободный USDC: account 'spot' | '' (осн.перп) | 'xyz' (HIP-3)."""
    if account == "spot":
        return spot_free(info, addr, "USDC")
    try:
        return float(info.user_state(addr, account).get("withdrawable", 0) or 0)
    except Exception:
        return 0.0


def open_positions(info, addr: str, dex: str) -> List[Dict[str, Any]]:
    out = []
    try:
        for p in info.user_state(addr, dex).get("assetPositions", []):
            pos = p.get("position", {})
            if float(pos.get("szi", 0) or 0) != 0:
                out.append(pos)
    except Exception:
        pass
    return out


def get_mid(info, coin: str) -> float:
    dex = coin.split(":")[0] if ":" in coin else ""
    try:
        return float(info.all_mids(dex)[coin])
    except Exception:
        return 0.0


def resolve_spot_coin(info, token: str) -> Tuple[str, int]:
    sm = info.spot_meta()
    by_idx = {t["index"]: t for t in sm["tokens"]}
    base = next((t for t in sm["tokens"] if t["name"] == token), None)
    usdc = next((t for t in sm["tokens"] if t["name"] == "USDC"), None)
    for u in sm["universe"]:
        if base and usdc and u["tokens"] == [base["index"], usdc["index"]]:
            return u["name"], int(by_idx[base["index"]]["szDecimals"])
    raise RuntimeError(f"Спот-пара {token}/USDC не найдена")


# ----------------------------- переводы ----------------------------- #
def move_usdc(ex, info, addr, src, dst, amount, live, rows):
    amount = int(amount * 100) / 100
    if amount < 0.5:
        return True
    if not live:
        log_event(rows, "Перевод", "USDC", f"{loc(src)}->{loc(dst)}", amount, 1.0, "ТЕСТ")
        return True
    try:
        if src == "spot" and dst == "":
            r = ex.usd_class_transfer(amount, True)
        elif src == "" and dst == "spot":
            r = ex.usd_class_transfer(amount, False)
        else:
            r = ex.send_asset(addr, src, dst, "USDC", amount)
        ok = is_ok(r)
        log_event(rows, "Перевод", "USDC", f"{loc(src)}->{loc(dst)}", amount, 1.0,
                  "ок" if ok else f"ОШИБКА {r}")
        return ok
    except Exception as e:
        print(f"     X перевод не удался: {e}")
        return False


def loc(a: str) -> str:
    return {"spot": "spot", "": "perp"}.get(a, a)


def ensure_margin(ex, info, addr, account, need, live, rows) -> bool:
    """Долить USDC со спота, чтобы на account было >= need."""
    if not live:
        return True
    cur = acct_usdc(info, addr, account)
    if cur >= need:
        return True
    short = int((need - cur + 0.05) * 100) / 100
    avail = spot_free(info, addr, "USDC")
    if short > avail:
        short = int(avail * 100) / 100
    if short < 0.5:
        return cur >= need * 0.9
    move_usdc(ex, info, addr, "spot", account, short, live, rows)
    for _ in range(12):
        if acct_usdc(info, addr, account) >= need * 0.95:
            return True
        time.sleep(1.2)
    return acct_usdc(info, addr, account) >= need * 0.9


# ----------------------------- лог ----------------------------- #
def log_event(rows, action, asset, side, size, price, status):
    usd = size * price
    rows.append([datetime.now().strftime("%Y-%m-%d %H:%M:%S"), action, asset, side,
                 f"{size:.6f}", f"{price:.4f}", f"{usd:.2f}", status])
    st = str(status)
    col = "green" if st == "ок" else ("red" if "ОШИБКА" in st else "grey")
    print(f"    -> {_paint(action, 'cyan')} {asset} {side} {size} @ {price} "
          f"(~{fmt(usd)}) [{_paint(st, col)}]")


def write_logs(addr, rows, summary):
    new = not os.path.exists(LOG_CSV)
    with open(LOG_CSV, "a", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f, delimiter=";")
        if new:
            w.writerow(["Время", "Действие", "Актив", "Сторона", "Размер", "Цена", "Объём_USD", "Статус"])
        w.writerows(rows)
    new = not os.path.exists(SUMMARY_CSV)
    with open(SUMMARY_CSV, "a", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f, delimiter=";")
        if new:
            w.writerow(["Время", "Адрес", "Комиссии_USD", "Резерв_USDC",
                        "Объём_HIP3", "Объём_перпы", "Объём_спот", "Объём_всего",
                        "Цель_HIP3", "Цель_перпы", "Сделок"])
        w.writerow(summary)
    print(f"\n  Лог: {LOG_CSV}\n  Итог: {SUMMARY_CSV}")


# ----------------------------- выбор ----------------------------- #
def choose_hip3(available: set) -> List[str]:
    print("\nHIP-3 активы trade.xyz (тикеры/номера через запятую, Enter = не нужны):")
    for i, (tk, name) in enumerate(POPULAR, 1):
        mark = "" if f"{DEX}:{tk}" in available else " (нет)"
        print(f"  {i:>2}. {tk:<10} {name}{mark}")
    print("  (можно любой тикер из полного списка trade.xyz)")
    raw = ask("\nКакие HIP-3 активы (напр: NVDA, GOLD  или  1,13  | Enter = пропустить)")
    chosen, seen = [], set()
    for tok in raw.replace(";", ",").split(","):
        tok = tok.strip()
        if not tok:
            continue
        tk = POPULAR[int(tok) - 1][0] if (tok.isdigit() and 1 <= int(tok) <= len(POPULAR)) \
            else tok.upper().replace("XYZ:", "")
        coin = f"{DEX}:{tk}"
        if coin not in available:
            print(f"   X {tk} не найден - пропуск")
        elif coin not in seen:
            seen.add(coin); chosen.append(coin)
    return chosen


def choose_perp(main_specs: dict):
    print("\nОсновные перпы Hyperliquid:")
    print("  1 - пара BTC + ETH (открываются вместе: лонг один + шорт другой)")
    print("  2 - один перп (SOL, HYPE, BTC, ETH, XRP ...)")
    print("  Enter - ничего (только HIP-3)")
    raw = ask("Что крутить из перпов? (1 / 2 / Enter)")
    if raw == "1":
        return ("pair", ("ETH", "BTC"))
    if raw == "2":
        while True:
            t = ask("Какой перп (тикер, напр SOL)").upper()
            if t in main_specs:
                return ("single", t)
            print(f"   X {t} не найден среди перпов")
    return (None, None)


# ----------------------------- торговые операции ----------------------------- #
def do_open(ex, info, addr, coin, side, per_margin, lev, cross, specs, live, rows) -> float:
    spec = specs.get(coin, {})
    eff = max(1, min(int(lev), int(spec.get("maxLeverage", lev))))
    px = get_mid(info, coin)
    notional = per_margin * eff
    sz = round(notional / px, int(spec.get("szDecimals", 2))) if px else 0
    if px == 0 or notional < MIN_ORDER or sz <= 0:
        print(f"    X {coin}: позиция мала (<{fmt(MIN_ORDER)}) или нет цены - пропуск")
        return 0.0
    if not live:
        log_event(rows, "Открытие", coin, side, sz, px, "ТЕСТ")
        return notional
    try:
        ex.update_leverage(eff, coin, is_cross=cross)
    except Exception as e:
        print(f"    ! плечо {coin}: {e}")
    r = ex.market_open(coin, side == "long", sz, px=px, slippage=PERP_SLIPPAGE)
    vol, fpx = fill_info(r)
    log_event(rows, "Открытие", coin, side, sz, fpx or px, "ок" if is_ok(r) else f"ОШИБКА {r}")
    return vol


def do_close(ex, info, addr, coin, specs, live, rows, plan_notional) -> float:
    if not live:
        px = get_mid(info, coin)
        log_event(rows, "Закрытие", coin, "close", (plan_notional / px if px else 0), px, "ТЕСТ")
        return plan_notional
    r = ex.market_close(coin, slippage=PERP_SLIPPAGE)
    vol, fpx = fill_info(r)
    log_event(rows, "Закрытие", coin, "close", (vol / fpx if fpx else 0), fpx,
              "ок" if (r is None or is_ok(r)) else f"ОШИБКА {r}")
    return vol


def _jit_margin(per_margin, jitter):
    """Случайный размер позиции: маржа * uniform(1-jitter, 1). jitter=0 -> без изменений."""
    if jitter and jitter > 0:
        return per_margin * random.uniform(max(0.0, 1.0 - float(jitter)), 1.0)
    return per_margin


def trade_hip3(ex, info, addr, coin, per_margin, lev, hold, specs, live, rows, jitter=0.0) -> float:
    m = _jit_margin(per_margin, jitter)
    ensure_margin(ex, info, addr, DEX, m * 1.15, live, rows)
    vol = do_open(ex, info, addr, coin, "long", m, lev, False, specs, live, rows)
    if vol > 0:
        wait(hold, f"держим {coin}", live)
        vol += do_close(ex, info, addr, coin, specs, live, rows, vol)
    return vol


def trade_pair(ex, info, addr, coins, per_margin, lev, hold, specs, live, rows, jitter=0.0) -> float:
    a, b = coins
    lc, sc = random.choice([(a, b), (b, a)])
    m = _jit_margin(per_margin, jitter)            # размер пары варьируется от цикла к циклу
    print(f"    пара: ЛОНГ {lc} / ШОРТ {sc}")
    ensure_margin(ex, info, addr, "", m * 2 * 1.15, live, rows)
    vol = do_open(ex, info, addr, lc, "long", m, lev, True, specs, live, rows)
    vol += do_open(ex, info, addr, sc, "short", m, lev, True, specs, live, rows)
    if vol > 0:
        wait(hold, "держим пару", live)
        vol += do_close(ex, info, addr, lc, specs, live, rows, m * lev)
        vol += do_close(ex, info, addr, sc, specs, live, rows, m * lev)
    return vol


def trade_single(ex, info, addr, coin, per_margin, lev, hold, specs, live, rows, jitter=0.0) -> float:
    m = _jit_margin(per_margin, jitter)
    ensure_margin(ex, info, addr, "", m * 1.15, live, rows)
    vol = do_open(ex, info, addr, coin, "long", m, lev, True, specs, live, rows)
    if vol > 0:
        wait(hold, f"держим {coin}", live)
        vol += do_close(ex, info, addr, coin, specs, live, rows, vol)
    return vol


def wait(mins, what, live):
    sec = max(0.0, float(mins) * 60.0)
    print(f"    ждём {mins} мин ({sec:.0f} сек) - {what}")
    if live and sec > 0:
        time.sleep(sec)


# --------------------- лимитные спот-ордера (с переустановкой) --------------------- #
def _round_px(ex, coin, is_buy, px):
    """Округлить цену под правила HL (5 знач. цифр / тики). Без слиппеджа."""
    try:
        return ex._slippage_price(coin, is_buy, 0.0, px)
    except Exception:
        return float(f"{px:.5g}")


def _resting_oid(r):
    """oid висящего лимит-ордера из ответа; None если исполнился сразу/ошибка."""
    try:
        st = r["response"]["data"]["statuses"][0]
        return st["resting"]["oid"] if "resting" in st else None
    except Exception:
        return None


def _order_done(info, addr, oid):
    """True, если ордер oid больше НЕ 'open' (исполнен/снят/неизвестен)."""
    try:
        r = info.query_order_by_oid(addr, oid)
        inner = r.get("order") if isinstance(r, dict) else None
        return ((inner or {}).get("status") or "done") != "open"
    except Exception:
        return False


def _cancel_coin_orders(ex, info, addr, coin):
    """Снять все висящие ордера по coin (страховка от орфанов с залоченными средствами)."""
    try:
        for o in (info.open_orders(addr) or []):
            if o.get("coin") == coin and o.get("oid") is not None:
                try:
                    ex.cancel(coin, o["oid"])
                except Exception:
                    pass
    except Exception:
        pass


def spot_limit_fill(ex, info, addr, coin, is_buy, need_fn, szdec, rows, label, out_token,
                    wait_s=10.0, repegs=8, final_cross=0.004):
    """Исполнить нужный объём ЛИМИТКОЙ по текущему миду с переустановкой каждые wait_s сек.

    need_fn(mid) -> сколько ЕЩЁ монет надо (пересчёт по балансу каждую итерацию, поэтому
    частичные исполнения учитываются сами). Не исполнилось за wait_s — снимаем и ставим заново
    на свежий мид. Последняя попытка чуть крестит спред (final_cross), чтобы гарантированно
    добить. Лимитка по точной цене не требует запаса на слиппедж → меньше потерь/остатка, чем
    маркет. Возвращает исполненный объём в USD (по приросту out_token)."""
    vol = 0.0
    for attempt in range(int(repegs) + 1):
        mid = get_mid(info, coin)
        if not mid:
            break
        sz = int(max(0.0, need_fn(mid)) * 10 ** szdec) / 10 ** szdec
        if sz <= 0 or sz * mid < MIN_ORDER:
            break
        cross = final_cross if attempt == repegs else 0.0
        px = _round_px(ex, coin, is_buy, mid * (1 + cross) if is_buy else mid * (1 - cross))
        out_before = spot_free(info, addr, out_token)
        try:
            r = ex.order(coin, is_buy, sz, px, {"limit": {"tif": "Gtc"}})
        except Exception as e:
            print(f"    ! лимитка {label}: {e}")
            break
        if not is_ok(r):
            log_event(rows, label, ETH_TOKEN, "buy" if is_buy else "sell", sz, px, f"ОШИБКА {r}")
            break
        oid = _resting_oid(r)
        if oid is not None:
            waited, filled = 0.0, False
            while waited < wait_s:
                time.sleep(1.0)
                waited += 1.0
                if _order_done(info, addr, oid):
                    filled = True
                    break
            if not filled:
                try:
                    ex.cancel(coin, oid)
                except Exception:
                    pass
                print(f"    ↻ {label}: не исполнилось за {wait_s:.0f}с — переставляю на текущую цену")
        time.sleep(1.2)  # дать залоку/балансам обновиться
        got = max(0.0, spot_free(info, addr, out_token) - out_before)
        if got > 0:
            vol += got if out_token == "USDC" else got * px
            log_event(rows, label, ETH_TOKEN, "buy" if is_buy else "sell",
                      sz if out_token == "USDC" else got, px, "ок")
    _cancel_coin_orders(ex, info, addr, coin)  # подчистить возможные орфаны
    return vol


# ----------------------------- круг ----------------------------- #
def run(ex, info, addr, spot_coin, spot_szdec, specs, hip3_assets, perp_kind, perp_arg,
        pct, lev, target_hip3, target_perp, hold, gap, live, log_csv=True, reserve_usdc=RESERVE_USDC,
        jitter=0.0, random_pick=False, builder=None):
    rows: List[List[Any]] = []
    # builder-комиссия: подставить builder во ВСЕ ордера этого аккаунта (если включена).
    # Выключено (builder=None) -> обёрток нет, поведение 1-в-1 прежнее.
    if builder:
        _bo, _bc = ex.market_open, ex.market_close
        ex.market_open = lambda *a, **k: _bo(*a, **{"builder": builder, **k})
        ex.market_close = lambda *a, **k: _bc(*a, **{"builder": builder, **k})
    ueth_before = spot_free(info, addr, ETH_TOKEN)
    usdc_before = spot_free(info, addr, "USDC")
    spot_px = get_mid(info, spot_coin)
    print(f"\n  Старт: {ueth_before} {ETH_TOKEN} (~{fmt(ueth_before*spot_px)})")

    # 1) продать UETH -> USDC ЛИМИТКОЙ (переустановка каждые 10с; меньше слиппеджа, чем маркет)
    vol_spot = 0.0   # объём на споте (продажа + покупка UETH)
    print(_paint("\n  ▸ Продаю UETH -> USDC (лимиткой)", "cyan", "bold"))
    if ueth_before * spot_px >= MIN_ORDER:
        if live:
            vol_spot += spot_limit_fill(
                ex, info, addr, spot_coin, False,
                lambda _mid: spot_free(info, addr, ETH_TOKEN),
                spot_szdec, rows, "Продажа UETH", "USDC")
            # страховка: лимитка не добила и UETH ещё на >= минимум — добиваем маркетом
            rest = spot_free(info, addr, ETH_TOKEN)
            if rest * get_mid(info, spot_coin) >= MIN_ORDER:
                szr = int(rest * 10**spot_szdec) / 10**spot_szdec
                r = ex.market_open(spot_coin, False, szr, slippage=SPOT_SLIPPAGE)
                sv, fpx = fill_info(r)
                vol_spot += sv
                log_event(rows, "Продажа UETH (маркет-добор)", ETH_TOKEN, "sell", szr,
                          fpx or spot_px, "ок" if is_ok(r) else f"ОШИБКА {r}")
        else:
            sz = int(ueth_before * 10**spot_szdec) / 10**spot_szdec
            vol_spot += sz * spot_px
            log_event(rows, "Продажа UETH", ETH_TOKEN, "sell", sz, spot_px, "ТЕСТ")
    else:
        print("    X UETH мало (<$10)")
    if live:
        time.sleep(2)

    budget = spot_free(info, addr, "USDC") if live else ueth_before * spot_px + spot_free(info, addr, "USDC")
    requested = budget * pct / 100.0
    base_margin = min(requested, budget * 0.97)     # вся маржа на сделку; ≤97% бюджета — запас на комиссии
    cap_note = "  (огранич. 97% бюджета)" if base_margin < requested - 1e-9 else ""
    print(f"\n  Бюджет: {fmt(budget)} | маржа на сделку ({pct}%): {fmt(base_margin)}{cap_note} | плечо {lev}x")

    # Заданный % — это ВСЯ маржа на одну сделку. Фазы идут по очереди, маржа переиспользуется.
    # HIP-3 держит 1 позицию → берёт маржу целиком. Пара перпов держит 2 ноги сразу →
    # ДЕЛИТ ту же маржу пополам (НЕ удваивает площадку). Один перп → целиком.
    perp_conc = 2 if perp_kind == "pair" else 1
    per_margin_hip = base_margin if hip3_assets else 0.0
    per_margin_perp = (base_margin / perp_conc) if perp_kind else 0.0
    if hip3_assets:
        print(f"    HIP-3:  {fmt(per_margin_hip)} маржи на позицию")
    if perp_kind == "pair":
        print(f"    Perps:  {fmt(per_margin_perp)} × 2 ноги = {fmt(per_margin_perp * 2)} маржи (та же сумма, поделена)")
    elif perp_kind:
        print(f"    Perps:  {fmt(per_margin_perp)} маржи на позицию")

    # Если маржа × плечо не дотягивает до минимального ордера (~$10) — на этой площадке
    # ничего не открыть; фазу пропускаем (не крутим вхолостую).
    hip_ok = bool(hip3_assets) and (per_margin_hip * lev >= MIN_ORDER)
    perp_ok = bool(perp_kind) and (per_margin_perp * lev >= MIN_ORDER)
    if hip3_assets and not hip_ok:
        print(_paint(f"  ⚠ HIP-3 пропускаю: {fmt(per_margin_hip)} × {lev} = {fmt(per_margin_hip*lev)} "
                     f"< мин. ордера {fmt(MIN_ORDER)}.", "yellow"))
    if perp_kind and not perp_ok:
        print(_paint(f"  ⚠ Perps пропускаю: {fmt(per_margin_perp)} × {lev} = {fmt(per_margin_perp*lev)} "
                     f"< мин. ордера {fmt(MIN_ORDER)}.", "yellow"))
    if (hip3_assets or perp_kind) and not hip_ok and not perp_ok:
        print(_paint("     Бюджет мал. Нужно: пополнить счёт, поднять плечо, убрать одну "
                     "площадку или снизить цели объёма.", "yellow", "bold"))

    one_pass_hip3 = target_hip3 <= 0
    one_pass_perp = target_perp <= 0
    volume = vol_hip3 = vol_perp = 0.0
    trades = 0

    # ===== ФАЗА HIP-3: набиваем target_hip3 (0 = один проход) =====
    if hip3_assets and hip_ok:
        print(_paint(f"\n  ▸ HIP-3 ({'один проход' if one_pass_hip3 else 'цель '+fmt(target_hip3)}): "
                     f"{', '.join(hip3_assets)}", "cyan", "bold")
              + (_paint("  [случайный актив из пула, разные размеры]", "grey") if random_pick else ""))
        empty = 0
        if random_pick:
            # волумен-драйв: каждую сделку берём СЛУЧАЙНЫЙ актив из списка-пула
            while True:
                coin = random.choice(hip3_assets)
                print(_paint(f"\n   ~~ {coin}  (HIP-3: {fmt(vol_hip3)}"
                             f"{'' if one_pass_hip3 else '/'+fmt(target_hip3)}) ~~", "cyan"))
                before = vol_hip3
                vol_hip3 += trade_hip3(ex, info, addr, coin, per_margin_hip, lev, _pick(hold), specs, live, rows, jitter)
                trades += 1
                empty = 0 if vol_hip3 > before else empty + 1
                if empty >= MAX_EMPTY:
                    print(_paint("    ⚠ подряд ничего не открылось (мало средств / нет цены) — стоп HIP-3", "yellow"))
                    break
                if one_pass_hip3 or vol_hip3 >= target_hip3 or trades >= MAX_TRADES or not live:
                    break
                wait(_pick(gap), "пауза между сделками", live)
        else:
            done = False
            while not done:
                for coin in hip3_assets:
                    print(_paint(f"\n   ~~ {coin}  (HIP-3: {fmt(vol_hip3)}"
                                 f"{'' if one_pass_hip3 else '/'+fmt(target_hip3)}) ~~", "cyan"))
                    before = vol_hip3
                    vol_hip3 += trade_hip3(ex, info, addr, coin, per_margin_hip, lev, _pick(hold), specs, live, rows, jitter)
                    trades += 1
                    empty = 0 if vol_hip3 > before else empty + 1
                    if empty >= MAX_EMPTY:
                        print(_paint("    ⚠ подряд ничего не открылось (мало средств / нет цены) — стоп HIP-3", "yellow"))
                        done = True
                        break
                    if (not one_pass_hip3 and vol_hip3 >= target_hip3) or trades >= MAX_TRADES:
                        done = True
                        break
                    wait(_pick(gap), "пауза между сделками", live)
                if one_pass_hip3 or not live:
                    break

    # Вернуть USDC с площадки HIP-3 (dex) на спот, чтобы фаза перпов могла взять полную
    # маржу из спота (после HIP-3 маржа осталась на dex-аккаунте).
    if live and hip3_assets and hip_ok and perp_kind and perp_ok:
        move_usdc(ex, info, addr, DEX, "spot", acct_usdc(info, addr, DEX), live, rows)
        time.sleep(1)

    # ===== ФАЗА ПЕРПЫ: крутим пару/один перп до target_perp (0 = один проход) =====
    if perp_kind and perp_ok:
        # single = пул перпов: крутим по одному случайно (как HIP-3). pair = фикс. пара.
        perp_pool = list(perp_arg) if (perp_kind == "single" and isinstance(perp_arg, (list, tuple))) else None
        plabel = (f"{perp_arg[0]}+{perp_arg[1]}" if perp_kind == "pair"
                  else (", ".join(perp_pool) if perp_pool else str(perp_arg)))
        print(_paint(f"\n  ▸ Perps ({'один проход' if one_pass_perp else 'цель '+fmt(target_perp)}): {plabel}"
                     + ("  [случайный из пула]" if perp_pool and len(perp_pool) > 1 else ""), "cyan", "bold"))
        empty = 0
        while True:
            if perp_kind == "pair":
                coin, label = None, plabel
            else:
                coin = random.choice(perp_pool) if perp_pool else perp_arg
                label = coin
            print(_paint(f"\n   ~~ {label}  (perps: {fmt(vol_perp)}"
                         f"{'' if one_pass_perp else '/'+fmt(target_perp)}) ~~", "cyan"))
            before = vol_perp
            if perp_kind == "pair":
                vol_perp += trade_pair(ex, info, addr, perp_arg, per_margin_perp, lev, _pick(hold), specs, live, rows, jitter)
            else:
                vol_perp += trade_single(ex, info, addr, coin, per_margin_perp, lev, _pick(hold), specs, live, rows, jitter)
            trades += 1
            empty = 0 if vol_perp > before else empty + 1
            if empty >= MAX_EMPTY:
                print(_paint("    ⚠ подряд ничего не открылось (мало средств / нет цены) — стоп perps", "yellow"))
                break
            if one_pass_perp or vol_perp >= target_perp or trades >= MAX_TRADES or not live:
                break
            wait(_pick(gap), "пауза между сделками", live)

    volume = vol_hip3 + vol_perp

    # 3) добить остаточные позиции
    if live:
        for dex in (DEX, ""):
            for pos in open_positions(info, addr, dex):
                print(f"    ! закрываю остаток {pos['coin']}")
                ex.market_close(pos["coin"], slippage=0.08); time.sleep(1)
        time.sleep(2)

    # 4) USDC обратно на спот
    print(_paint("\n  ▸ Возвращаю USDC на спот", "cyan", "bold"))
    move_usdc(ex, info, addr, DEX, "spot", acct_usdc(info, addr, DEX) if live else 0, live, rows)
    move_usdc(ex, info, addr, "", "spot", acct_usdc(info, addr, "") if live else 0, live, rows)
    if live:
        time.sleep(2)

    # 5) купить UETH на USDC ЛИМИТКОЙ, ОСТАВИВ резерв (для вывода через Unit нужен ~1 USDC).
    # Лимитка по точной цене не требует запаса на слиппедж -> на споте останется ~резерв,
    # а не +1-3$ как при маркете (он резал 1.3% от суммы выкупа про запас).
    print(_paint(f"\n  ▸ Покупаю UETH лимиткой (оставляю резерв {fmt(reserve_usdc)} USDC на споте)", "cyan", "bold"))
    if live:
        before_buy = vol_spot
        # buf=0.0015 покрывает комиссию/округление; sz считается из остатка USDC каждую итерацию
        vol_spot += spot_limit_fill(
            ex, info, addr, spot_coin, True,
            lambda mid: max(0.0, spot_free(info, addr, "USDC") - reserve_usdc) / (mid * 1.0015),
            spot_szdec, rows, "Покупка UETH", ETH_TOKEN)
        # страховка: если лимитка оставила КРУПНЫЙ остаток (>= минимума ордера) — добить маркетом
        px = get_mid(info, spot_coin)
        spend_rest = max(0.0, spot_free(info, addr, "USDC") - reserve_usdc)
        szf = int(spend_rest / (px * (1 + SPOT_SLIPPAGE) * 1.003) * 10**spot_szdec) / 10**spot_szdec if px else 0
        if szf * px >= MIN_ORDER:
            r = ex.market_open(spot_coin, True, szf, px=px, slippage=SPOT_SLIPPAGE)
            sv, fpx = fill_info(r)
            vol_spot += sv
            log_event(rows, "Покупка UETH (маркет-добор)", ETH_TOKEN, "buy", szf, fpx or px,
                      "ок" if is_ok(r) else f"ОШИБКА {r}")
        elif vol_spot == before_buy:
            print("    X для покупки осталось мало - оставляю USDC как есть")
    else:
        px = get_mid(info, spot_coin)
        spendable = max(0.0, budget - reserve_usdc)
        sz = int(spendable / (px * 1.0015) * 10**spot_szdec) / 10**spot_szdec if px else 0
        if sz * px >= MIN_ORDER:
            vol_spot += sz * px
            log_event(rows, "Покупка UETH", ETH_TOKEN, "buy", sz, px, "ТЕСТ")
        else:
            print(f"    X для покупки осталось мало ({fmt(spendable)}) - оставляю USDC как есть")

    # итог
    if live:
        time.sleep(2)
    ueth_after = spot_free(info, addr, ETH_TOKEN)
    usdc_left = spot_free(info, addr, "USDC")
    px_end = get_mid(info, spot_coin)
    spent_ueth = ueth_before - ueth_after
    # реальные траты = изменение полной стоимости (UETH+USDC) в одной цене;
    # намеренный резерв USDC НЕ считается тратой (он остаётся на счету)
    cost_usd = max(0.0, (ueth_before * px_end + usdc_before) - (ueth_after * px_end + usdc_left))
    left = (len(open_positions(info, addr, DEX)) + len(open_positions(info, addr, ""))) if live else 0
    print(_paint("\n  ===== ИТОГ =====", "magenta", "bold"))
    if not live and (target_hip3 > 0 or target_perp > 0):
        print("    [ТЕСТ] показан один проход; в реале фазы крутятся до своих целей")
    print(f"    объём HIP-3:  {fmt(vol_hip3)}" + (f" / цель {fmt(target_hip3)}" if target_hip3 > 0 else ""))
    print(f"    объём perps:  {fmt(vol_perp)}" + (f" / цель {fmt(target_perp)}" if target_perp > 0 else ""))
    print(f"    объём спот:   {fmt(vol_spot)}")
    print(f"    объём ВСЕГО:  {fmt(vol_hip3 + vol_perp + vol_spot)}")
    print(f"    сделок (открытий+закрытий): {trades}")
    print(f"    было UETH:  {ueth_before}  ->  стало UETH: {ueth_after}")
    print(f"    потрачено комиссий/спреда: ~{fmt(cost_usd)}")
    print(f"    оставлено USDC на споте (резерв для Unit): {fmt(usdc_left)}")
    print(f"    открытых позиций: {left} {'(чисто)' if left == 0 else '(!) ПРОВЕРЬ'}")
    if log_csv:
        write_logs(addr, rows, [datetime.now().strftime("%Y-%m-%d %H:%M:%S"), addr,
                                f"{cost_usd:.2f}", f"{usdc_left:.2f}",
                                f"{vol_hip3:.2f}", f"{vol_perp:.2f}", f"{vol_spot:.2f}",
                                f"{(vol_hip3 + vol_perp + vol_spot):.2f}",
                                (f"{target_hip3:.0f}" if target_hip3 > 0 else "1x"),
                                (f"{target_perp:.0f}" if target_perp > 0 else "1x"), trades])
    # структурный результат - для проверки другим скриптом/агентом
    return {
        "ok": (left == 0),                 # True = не осталось открытых позиций
        "address": addr,
        "volume": round(volume, 2),         # набитый объём HIP-3+перпы $ (buy+sell, с плечом)
        "volume_hip3": round(vol_hip3, 2),  # объём на HIP-3 (trade.xyz)
        "volume_perp": round(vol_perp, 2),  # объём на обычных перпах (BTC/ETH/SOL...)
        "volume_spot": round(vol_spot, 2),  # объём на споте (продажа+покупка UETH)
        "volume_total": round(vol_hip3 + vol_perp + vol_spot, 2),
        "target_hip3": target_hip3,         # заданная цель объёма HIP-3
        "target_perp": target_perp,         # заданная цель объёма перпов
        "trades": trades,                   # единиц торговли (актив/пара) за прогон
        "ueth_before": ueth_before,
        "ueth_after": ueth_after,
        "spent_ueth": round(spent_ueth, 8),
        "spent_usd": round(cost_usd, 4),       # реальные траты (комиссии/спред), без резерва
        "usdc_left": round(usdc_left, 2),   # «пыль» USDC на споте после круга
        "positions_left": left,             # должно быть 0
        "live": live,
        "log": rows,                        # список событий [время,действие,актив,сторона,размер,цена,$,статус]
    }


# ===================================================================== #
#  ПУБЛИЧНЫЙ API для интеграции (вызов из другого скрипта/агента)
# ===================================================================== #
def _harden_retries(ex, tries=4, backoff=0.7):
    """Ретраи на обрывах прокси/сети для ВСЕХ вызовов SDK (ex и ex.info идут через API.post).

    Прокси периодически рвёт соединение (ProxyError/'Remote end closed connection') — без
    повтора падает плечо/ордер и дальше каскадом 'Insufficient margin'. Оборачиваем .post:
      • /exchange (плечо, ордера, переводы) — повтор ТОЛЬКО при ошибке соединения
        (ConnectionError/ProxyError): запрос не дошёл до сервера, повтор безопасен (без дублей).
        ReadTimeout (ответ мог уйти) НЕ повторяем — чтобы не сдублировать ордер.
      • /info (чтение) — повтор при любой сетевой ошибке (идемпотентно).
    Бизнес-ошибки (HTTP 200 + {'status':'err'}) не трогаем — это не сетевой сбой.
    """
    import requests as _rq

    def _wrap(api_obj):
        if api_obj is None or getattr(api_obj, "_retry_wrapped", False):
            return
        _orig = api_obj.post

        def _post_retry(url_path, payload=None):
            is_exchange = "exchange" in str(url_path)
            exc = _rq.exceptions.ConnectionError if is_exchange else _rq.exceptions.RequestException
            last = None
            for i in range(tries):
                try:
                    return _orig(url_path, payload)
                except exc as e:
                    last = e
                    if i < tries - 1:
                        time.sleep(backoff * (2 ** i))
            raise last

        api_obj.post = _post_retry
        api_obj._retry_wrapped = True

    _wrap(ex)
    _wrap(getattr(ex, "info", None))


def setup_account(private_key: str, account_address: str = None,
                  base_url: str = constants.MAINNET_API_URL):
    """Создаёт подключение. Возвращает (ex, info, addr, spot_coin, spot_szdec, specs)."""
    wallet = eth_account.Account.from_key(private_key.strip())
    addr = account_address or wallet.address
    ex = Exchange(wallet, base_url, account_address=addr, perp_dexs=["", DEX])
    info = ex.info
    _harden_retries(ex)              # ретраи на обрывах прокси/сети для всех вызовов SDK
    spot_coin, spot_szdec = resolve_spot_coin(info, ETH_TOKEN)
    specs = {}
    for dx in ("", DEX):
        for a in info.meta(dx)["universe"]:
            specs[a["name"]] = {"szDecimals": int(a["szDecimals"]),
                                "maxLeverage": int(a.get("maxLeverage", 1))}
    return ex, info, addr, spot_coin, spot_szdec, specs


def account_status(private_key: str, account_address: str = None,
                   base_url: str = constants.MAINNET_API_URL) -> dict:
    """Быстрый снимок аккаунта без сделок: балансы + открытые позиции."""
    ex, info, addr, spot_coin, _, _ = setup_account(private_key, account_address, base_url)
    return {
        "address": addr,
        "ueth": spot_free(info, addr, ETH_TOKEN),
        "ueth_usd": spot_free(info, addr, ETH_TOKEN) * get_mid(info, spot_coin),
        "usdc_spot": spot_free(info, addr, "USDC"),
        "usdc_perp": acct_usdc(info, addr, ""),
        "usdc_dex": acct_usdc(info, addr, DEX),
        "positions_perp": [p["coin"] for p in open_positions(info, addr, "")],
        "positions_dex": [p["coin"] for p in open_positions(info, addr, DEX)],
    }


def _pick(val, integer=False):
    """Если val - пара [min,max] -> случайное значение в диапазоне; иначе само val."""
    if isinstance(val, (list, tuple)) and len(val) == 2 and all(isinstance(x, (int, float)) for x in val):
        lo, hi = val
        return random.randint(int(lo), int(hi)) if integer else round(random.uniform(float(lo), float(hi)), 2)
    return val


def _builder_pct(fee, default=0.01):
    """'0.01%' | '0.01' | 0.01 -> процент как float (0.01). Мусор -> default."""
    try:
        return float(str(fee).strip().rstrip("%").strip())
    except Exception:
        return default


def run_circle(private_key: str, hip3_assets=None, perp: str = "none", single_coin: str = None,
               pct: float = 50, leverage=2, target_hip3: float = 0, target_perp: float = 0,
               hold_minutes=0.2, gap_minutes=0.1, live: bool = False,
               account_address: str = None, base_url: str = constants.MAINNET_API_URL,
               log_csv: bool = True, reserve_usdc: float = RESERVE_USDC,
               hip3_count: int = None, shuffle: bool = False,
               size_jitter: float = 0.0, random_pick: bool = False,
               builder_enabled: bool = False) -> dict:
    """
    Прогнать круг программно (без вопросов в консоли). Возвращает dict с результатом
    (см. ключи в конце run(): ok, volume, positions_left, spent_usd, ...).

    Параметры:
      private_key   - приватный ключ ОСНОВНОГО кошелька (переводы USDC делает только он)
      hip3_assets   - список HIP-3 тикеров, напр ["NVDA","GOLD"] или ["xyz:NVDA"]; [] = без HIP-3
      perp          - "none" | "pair" (BTC+ETH вместе) | "single"
      single_coin   - перп(ы) для perp="single": список-пул ["BTC","SOL",...] (крутит по
                      одному СЛУЧАЙНО до target_perp, как HIP-3) или один тикер; пусто -> дефолтный пул
      pct           - % от проданного UETH = маржа на позицию
      leverage      - плечо (капается лимитом ассета). Объём позиции = маржа*плечо
      target_hip3   - целевой объём $ набить в HIP-3 (0 = один проход по активам)
      target_perp   - целевой объём $ набить в перпах (0 = один проход)
      hold_minutes  - держать каждую позицию; gap_minutes - пауза между сделками
      live          - False = тест (без сделок), True = реальная торговля
      account_address - адрес осн. аккаунта, если ключ - agent (обычно не нужно)
      log_csv       - писать ли circle_log.csv / circle_summary.csv

    РАНДОМ ("от случая к случаю"):
      leverage - можно пару [min,max] (берётся одно значение на прогон).
      hold_minutes / gap_minutes - число (фикс.) ИЛИ пара [min,max]: тогда удержание
          каждой позиции и пауза между сделками выбираются СЛУЧАЙНО в диапазоне
          ОТДЕЛЬНО на каждой сделке. Напр. hold_minutes=[0.5,2], gap_minutes=[0.2,1].
      hip3_count - взять случайное подмножество такого размера из hip3_assets (пула).
      shuffle    - перемешать порядок активов.
      size_jitter - рандом размера позиции: маржа * uniform(1-jitter, 1) (0 = фикс. размер).
      random_pick - HIP-3 брать СЛУЧАЙНЫЙ актив из пула каждую сделку (вместо обхода списка),
                    набивая target_hip3; список = пул, из которого выбираем.
    """
    ex, info, addr, spot_coin, spot_szdec, specs = setup_account(private_key, account_address, base_url)

    # Builder-комиссия (монетизация): адрес/ставка ВШИТЫ (BUILDER_ADDRESS/BUILDER_FEE); в конфиге только тумблер.
    builder = None
    if builder_enabled and BUILDER_ADDRESS:
        b_addr = BUILDER_ADDRESS.strip().lower()
        pct_b = _builder_pct(BUILDER_FEE)
        f_int = max(1, int(round(pct_b * 1000)))      # десятые доли б.п.: 0.01% -> 10
        if live:
            try:
                ex.approve_builder_fee(b_addr, f"{pct_b:g}%")
                print(f"[run_circle] builder-fee: {b_addr} ~{pct_b:g}% (approve ок)")
            except Exception as e:
                print(f"[run_circle] approve_builder_fee не прошёл: {e} — продолжаю без builder-комиссии")
                b_addr = None
        if b_addr:
            builder = {"b": b_addr, "f": f_int}

    norm = []
    for a in (hip3_assets or []):
        a = str(a).strip()
        coin = a if a.startswith(DEX + ":") else f"{DEX}:{a.upper()}"
        if coin in specs:
            norm.append(coin)
        else:
            print(f"[run_circle] HIP-3 '{a}' не найден - пропуск")

    perp_kind, perp_arg = None, None
    if perp == "pair":
        perp_kind, perp_arg = "pair", ("ETH", "BTC")
    elif perp == "single":
        # single = ПУЛ перпов (как HIP-3): крутим по одному случайно до target_perp.
        # single_coin: список ["BTC","SOL",...] или один тикер строкой; пусто -> дефолтный пул.
        raw = single_coin if isinstance(single_coin, (list, tuple)) else [single_coin]
        want = [str(c).upper().strip() for c in raw if c and str(c).strip()]
        if not want:
            want = list(DEFAULT_PERP_POOL)
        pool = [c for c in want if c in specs and ":" not in c]
        if not pool:
            raise ValueError(f"перпы single: ни один тикер не найден среди основных перпов ({want})")
        perp_kind, perp_arg = "single", pool

    # рандом: случайное подмножество активов / перемешать порядок
    if hip3_count and norm:
        norm = random.sample(norm, min(int(hip3_count), len(norm)))
    elif shuffle and norm:
        random.shuffle(norm)

    if not norm and not perp_kind:
        raise ValueError("нечего торговать: задай hip3_assets и/или perp")

    # плечо — одно на прогон; удержание/пауза рандомятся на КАЖДОЙ позиции (в run() через _pick)
    lev_use = int(_pick(leverage, integer=True))

    def _rng(v):
        return f"{v[0]}–{v[1]}" if isinstance(v, (list, tuple)) and len(v) == 2 else f"{v}"
    print(f"[run_circle] активы={norm} перп={perp_kind or '-'} | плечо {lev_use}x | "
          f"держать {_rng(hold_minutes)} мин | пауза {_rng(gap_minutes)} мин"
          + (f" | рандом размера ±{int(float(size_jitter)*100)}%" if size_jitter else "")
          + (" | случайный выбор HIP-3" if random_pick else ""))

    return run(ex, info, addr, spot_coin, spot_szdec, specs, norm, perp_kind, perp_arg,
               float(pct), lev_use, float(target_hip3), float(target_perp), hold_minutes,
               gap_minutes, bool(live), log_csv=log_csv, reserve_usdc=float(reserve_usdc),
               jitter=float(size_jitter), random_pick=bool(random_pick), builder=builder)


# ----------------------------- main (интерактив) ----------------------------- #
def main() -> int:
    print("=" * 62)
    print("  КРУГ + НАБИВКА ОБЪЁМА:  UETH -> сделки -> UETH")
    print("=" * 62)

    key = ask("\nВставь приватный ключ").lstrip("﻿").strip()
    if not key:
        print("Ключ не введён."); return 1
    try:
        wallet = eth_account.Account.from_key(key)
    except Exception as e:
        print(f"Неверный ключ: {e}"); return 1
    addr = wallet.address

    print("\nПодключаюсь к Hyperliquid...")
    ex = Exchange(wallet, constants.MAINNET_API_URL, account_address=addr, perp_dexs=["", DEX])
    info = ex.info
    _harden_retries(ex)              # ретраи на обрывах прокси/сети для всех вызовов SDK
    try:
        spot_coin, spot_szdec = resolve_spot_coin(info, ETH_TOKEN)
        specs = {}
        for dx in ("", DEX):
            for a in info.meta(dx)["universe"]:
                specs[a["name"]] = {"szDecimals": int(a["szDecimals"]),
                                    "maxLeverage": int(a.get("maxLeverage", 1))}
        available = set(k for k in specs if k.startswith(DEX + ":"))
        main_specs = set(k for k in specs if ":" not in k)
    except Exception as e:
        print(f"Ошибка подключения: {e}"); return 1

    ueth = spot_free(info, addr, ETH_TOKEN)
    px = get_mid(info, spot_coin)
    print(f"\nАккаунт: {addr}")
    print(f"На счету: {ueth} {ETH_TOKEN} (~{fmt(ueth*px)}) | USDC {fmt(spot_free(info, addr, 'USDC'))}")

    hip3_assets = choose_hip3(available)
    perp_kind, perp_arg = choose_perp(main_specs)
    if not hip3_assets and not perp_kind:
        print("\nНичего не выбрано - выход."); return 0

    pct = ask_float("\n% от проданного эфира на КАЖДУЮ позицию (маржа, напр 50)", 50)
    lev = ask_int("Плечо для перпов (напр 2)", 2)
    target_hip3 = ask_float("Объём $ набить в HIP-3 (0 = один проход)", 0) if hip3_assets else 0
    target_perp = ask_float("Объём $ набить в перпах (0 = один проход)", 0) if perp_kind else 0
    hold = ask_float("Держать каждую позицию (минут, напр 0.2)", 0.2)
    gap = ask_float("Пауза между сделками (минут, напр 0.1)", 0.1)

    print("\n----- ПЛАН -----")
    if hip3_assets:
        print(f"  HIP-3: {', '.join(hip3_assets)}  (цель {'один проход' if target_hip3 <= 0 else fmt(target_hip3)})")
    if perp_kind == "pair":
        print(f"  Перпы: пара {perp_arg[0]}+{perp_arg[1]} (цель {'один проход' if target_perp <= 0 else fmt(target_perp)})")
    elif perp_kind == "single":
        print(f"  Перп: {perp_arg}  (цель {'один проход' if target_perp <= 0 else fmt(target_perp)})")
    print(f"  маржа на позицию: {pct}% | плечо: {lev}x | держать {hold} мин | пауза {gap} мин")
    ans = ask("\nЗапускаю РЕАЛЬНО? напиши 'да' (Enter = тест без сделок)").lower()
    live = ans in ("да", "yes", "y", "д")
    print(f"\nРежим: {'РЕАЛЬНАЯ ТОРГОВЛЯ' if live else 'ТЕСТ (без сделок)'}")

    try:
        run(ex, info, addr, spot_coin, spot_szdec, specs, hip3_assets, perp_kind, perp_arg,
            pct, lev, target_hip3, target_perp, hold, gap, live)
    except KeyboardInterrupt:
        print("\nПрервано. Проверь аккаунт вручную (могли остаться позиции/USDC).")
        return 130
    return 0


if __name__ == "__main__":
    sys.exit(main())
