#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
HyperUnit: Bitget → Unit-депозит → торговля Hyperliquid → Unit-вывод → возврат на Bitget.

Прогон идёт по ВСЕМ кошелькам из config/wallets.xlsx (1 строка = 1 аккаунт).
Параметры — в config/config.json, ключи Bitget — в config/.env.
Запуск ВСЕГДА реальный (тест-режима нет).

Запуск:
    python app/main.py                # меню: выбрать модули и «Запустить»
    python app/main.py --stage cycle  # весь цикл по всем кошелькам, без меню
    python app/main.py --stage 1      # только стадию 1 по всем кошелькам
    python app/main.py --stage trade  # стадию можно и по имени

Стадии: 1=bitget  2=deposit  3=trade  4=withdraw  5=bitget_return  |  cycle=весь цикл.
"""

import os
import csv
import sys
import time
import random
import argparse
import traceback
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation

import paths  # noqa: F401  (sys.path для соседних папок)
import config_loader
import colors as C
import tui
import stage_bitget
import stage_deposit
import stage_trade
import stage_withdraw
import stage_bitget_return
import wallets_xlsx
import bitget_api
import net_proxy

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

# ключ -> (человекочитаемое имя, функция стадии)
STAGES = {
    "bitget":        ("Вывод с Bitget",         stage_bitget.run),
    "deposit":       ("Депозит на Hyperliquid",  stage_deposit.run),
    "trade":         ("Торговля Hyperliquid",   stage_trade.run),
    "withdraw":      ("Вывод с Hyperliquid",    stage_withdraw.run),
    "bitget_return": ("Возврат на Bitget",      stage_bitget_return.run),
}
CYCLE_ORDER = ["bitget", "deposit", "trade", "withdraw", "bitget_return"]
NUM_TO_KEY = {str(i): k for i, k in enumerate(CYCLE_ORDER, 1)}   # "1".."5" -> стадия

# Зависимости стадий: шаг имеет смысл, только если его «поставщик средств» отработал.
#   deposit  ← bitget   (нет прихода ETH на кошелёк — депать нечего)
#   trade    ← deposit  (нет средств на Hyperliquid — торговать нечем)
#   withdraw ← deposit  (нет средств на Hyperliquid — выводить нечего)
# bitget_return НИ ОТ ЧЕГО не зависит: возвращает остаток с кошелька (в т.ч. ETH после
# неудавшегося депозита — страховка от застревания средств). Зависимость применяется,
# только если шаг-поставщик включён в этот прогон (частичные запуски не ломаем).
STAGE_DEPENDS = {"deposit": "bitget", "trade": "deposit", "withdraw": "deposit"}


def enabled_cycle(cfg):
    """Стадии для режима «весь цикл», только включённые (enabled=true)."""
    return [k for k in CYCLE_ORDER if cfg.enabled.get(k, True)]


# --------------------------------------------------------------------------- #
#  Траты                                                                      #
# --------------------------------------------------------------------------- #
def _dec(x):
    if x is None or x == "":
        return None
    try:
        return Decimal(str(x))
    except (InvalidOperation, ValueError, TypeError):
        return None


def _eth_price_usd():
    """Цена ETH/USD: Hyperliquid -> Coinbase -> None."""
    import requests
    try:
        p = requests.post("https://api.hyperliquid.xyz/info", json={"type": "allMids"},
                          timeout=10).json().get("ETH")
        if p:
            return Decimal(str(p))
    except Exception:
        pass
    try:
        return Decimal(str(requests.get("https://api.coinbase.com/v2/prices/ETH-USD/spot",
                                        timeout=10).json()["data"]["amount"]))
    except Exception:
        return None


def _raw(bs, stage):
    return (bs.get(stage) or {}).get("raw", {}) or {}


def _g(d, key, default=""):
    v = (d or {}).get(key)
    return default if v is None else v


def _eth_usd(eth, price):
    e = _dec(eth)
    return (e * price) if (e is not None and price) else None


def _fee_usd(spent, price):
    """Все комиссии одной стадии -> $ (ETH×цена + $ + USDC≈$1)."""
    total = Decimal(0)
    e, u, uc = _dec(spent.get("eth")), _dec(spent.get("usd")), _dec(spent.get("usdc"))
    if e is not None and price:
        total += e * price
    if u is not None:
        total += u
    if uc is not None:
        total += uc
    return total


NARR_NAMES = {
    "bitget": "Bitget → кошелёк", "deposit": "Депозит на HL", "trade": "Торговля",
    "withdraw": "Вывод с HL", "bitget_return": "Возврат на Bitget", "return": "Возврат на Bitget",
}


def _usd_str(eth, price):
    u = _eth_usd(eth, price)
    return f" (~${u:.2f})" if u is not None else ""


def _eths(x):
    """ETH-сумму в аккуратный вид (до 6 знаков, без хвостовых нулей)."""
    d = _dec(x)
    if d is None:
        return str(x) if x not in (None, "") else "?"
    s = f"{d:.6f}".rstrip("0").rstrip(".")
    return s or "0"


def _stage_lines(results, price):
    """Понятные строки по каждой стадии (суммы в $). -> [(name, detail, ok)].
    Неуспешная стадия показывает причину (summary), успешная — цифры."""
    rows = []
    for r in results:
        k = r.get("stage")
        raw = r.get("raw", {}) or {}
        ok = bool(r.get("ok"))
        name = NARR_NAMES.get(k) or STAGES.get(k, (k or "?",))[0]
        if not ok:
            d = r.get("summary", "") or "—"
        elif k == "bitget":
            d = f"вывел {_eths(raw.get('amount'))} ETH{_usd_str(raw.get('amount'), price)}"
            if raw.get("received"):
                d += f", пришло {_eths(raw.get('received'))} ETH"
        elif k == "deposit":
            d = f"{_eths(raw.get('amount_eth'))} ETH{_usd_str(raw.get('amount_eth'), price)}"
        elif k == "trade":
            d = (f"UETH {_g(raw, 'ueth_before', '?')} → {_g(raw, 'ueth_after', '?')}  "
                 f"(объём ${_g(raw, 'volume_total', 0)})")
        elif k == "withdraw":
            d = f"{_eths(raw.get('amount_eth'))} ETH{_usd_str(raw.get('amount_eth'), price)}"
            nu = _eth_usd(raw.get("net_eth"), price)
            if nu is not None:
                d += f" → на Ethereum ~${nu:.2f}"
        elif k in ("bitget_return", "return"):
            amt = raw.get("amount_eth") or raw.get("amount")
            d = f"{_eths(amt)} ETH{_usd_str(amt, price)}"
        else:
            d = r.get("summary", "")
        rows.append((name, d, ok))
    return rows


def _totals(results, price):
    fees = sum((_fee_usd(r.get("spent") or {}, price) for r in results), Decimal(0))
    bs = {r.get("stage"): r for r in results}
    vol = _dec(_raw(bs, "trade").get("volume_total")) or Decimal(0)
    return fees, vol


def _volumes(results, price):
    """Объёмы по площадкам в $: Unit (депозит+вывод), HIP-3, perps, спот."""
    bs = {r.get("stage"): r for r in results}
    d, w, t = _raw(bs, "deposit"), _raw(bs, "withdraw"), _raw(bs, "trade")
    unit = (_eth_usd(d.get("amount_eth"), price) or Decimal(0)) \
        + (_eth_usd(w.get("amount_eth"), price) or Decimal(0))
    return {
        "unit": unit,
        "hip3": _dec(t.get("volume_hip3")) or Decimal(0),
        "perp": _dec(t.get("volume_perp")) or Decimal(0),
        "spot": _dec(t.get("volume_spot")) or Decimal(0),
    }


def print_spent_report(results, price=None):
    if price is None:
        price = _eth_price_usd()
    rows = _stage_lines(results, price)
    fees, _vol = _totals(results, price)
    v = _volumes(results, price)
    print()
    print(C.header("═" * 64))
    print(C.header("  ИТОГ"))
    print(C.dim("─" * 64))
    for name, detail, ok in rows:
        icon = C.ok("✓") if ok else C.err("✗")
        print(f"  {icon} {C.bold((name + ':').ljust(20))} {detail}")
    print(C.dim("─" * 64))
    print("  " + C.bold("ОБЪЁМЫ:"))
    print(f"    Unit (деп+вывод):  {C.money('$' + format(v['unit'], '.2f'))}")
    print(f"    HIP-3:             {C.money('$' + format(v['hip3'], '.2f'))}")
    print(f"    Perps:             {C.money('$' + format(v['perp'], '.2f'))}")
    print(f"    Спот (UETH):       {C.money('$' + format(v['spot'], '.2f'))}")
    note = "" if price else C.warn("  (ETH в $ не переведён — нет цены)")
    print("  " + C.bold("КОМИССИИ ВСЕГО:") + "  " + C.money("~$" + format(fees, ".2f")) + note)
    print(C.header("═" * 64))


# --------------------------------------------------------------------------- #
#  Сохранение итога: runs_log.txt (читаемый) + runs.csv (Excel) — всё в $      #
# --------------------------------------------------------------------------- #
RUN_CSV = os.path.join(paths.OUTPUT_DIR, "runs.csv")
RUN_TXT = os.path.join(paths.OUTPUT_DIR, "runs_log.txt")
RUN_COLUMNS = [
    "timestamp_utc", "mode", "address",
    "bitget_out_eth", "bitget_out_usd", "bitget_received_eth",
    "deposit_eth", "deposit_usd",
    "unit_volume_usd", "trade_volume_usd", "trade_hip3_usd", "trade_perp_usd", "trade_spot_usd",
    "ueth_before", "ueth_after",
    "withdraw_eth", "withdraw_usd", "withdraw_net_usd", "withdraw_credit",
    "fees_usd", "volume_usd",
]


# --------------------------------------------------------------------------- #
#  Прогресс: done (все ВКЛЮЧЁННЫЕ стадии прошли) / failed (упал) — резюме+ретрай  #
# --------------------------------------------------------------------------- #
DONE_FILE = os.path.join(paths.OUTPUT_DIR, "done_accounts.txt")
FAILED_FILE = os.path.join(paths.OUTPUT_DIR, "failed_accounts.txt")


def _addr_of(private_key):
    """EVM-адрес из приватника (в нижнем регистре) или None."""
    try:
        from eth_account import Account
        return Account.from_key((private_key or "").strip()).address.lower()
    except Exception:
        return None


def load_done_accounts():
    """Множество адресов (lower) уже прогнанных аккаунтов из output/done_accounts.txt."""
    done = set()
    try:
        with open(DONE_FILE, "r", encoding="utf-8") as fh:
            for line in fh:
                t = line.strip()
                if not t or t.startswith("#"):
                    continue
                a = t.split()[0].lower()
                if a.startswith("0x"):
                    done.add(a)
    except FileNotFoundError:
        pass
    except Exception:
        pass
    return done


def mark_account_done(address):
    """Дописать адрес аккаунта в output/done_accounts.txt (с меткой времени UTC)."""
    if not address:
        return
    try:
        new = not os.path.isfile(DONE_FILE)
        with open(DONE_FILE, "a", encoding="utf-8") as fh:
            if new:
                fh.write("# Уже сделанные аккаунты (адрес + время UTC). "
                         "Удали этот файл, чтобы пройти всех заново.\n")
            fh.write(f"{address.lower()}  {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M')}\n")
    except Exception as e:
        print(f"  ⚠ не записал прогресс ({os.path.basename(DONE_FILE)}): {e}")


def mark_account_failed(address, results):
    """Записать упавший аккаунт в output/failed_accounts.txt (адрес + время + упавшие стадии).
    В done он НЕ попадает → на следующем запуске пройдёт заново (авто-ретрай)."""
    if not address:
        return
    bad = [str(r.get("stage")) for r in (results or []) if not r.get("ok")]
    try:
        new = not os.path.isfile(FAILED_FILE)
        with open(FAILED_FILE, "a", encoding="utf-8") as fh:
            if new:
                fh.write("# Упавшие аккаунты (адрес + время UTC + упавшие стадии). "
                         "В done НЕ попадают — пройдут заново при следующем запуске.\n")
            fh.write(f"{address.lower()}  {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M')}  "
                     f"стадии: {', '.join(bad) or '?'}\n")
    except Exception as e:
        print(f"  ⚠ не записал {os.path.basename(FAILED_FILE)}: {e}")


def _usd_num(eth, price):
    u = _eth_usd(eth, price)
    return f"{u:.2f}" if u is not None else ""


def save_run_report(cfg, results, live, price=None):
    """Дописать итог в runs_log.txt (читаемый) и runs.csv (Excel). Всё сведено в $."""
    if not results:
        return
    if price is None:
        price = _eth_price_usd()
    bs = {r.get("stage"): r for r in results}
    braw, draw, traw, wraw = (_raw(bs, "bitget"), _raw(bs, "deposit"),
                              _raw(bs, "trade"), _raw(bs, "withdraw"))
    fees, vol = _totals(results, price)
    v = _volumes(results, price)
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")
    mode = "РЕАЛ" if live else "ТЕСТ"
    rows = _stage_lines(results, price)

    # --- читаемый отчёт ---
    try:
        with open(RUN_TXT, "a", encoding="utf-8") as fh:
            fh.write("\n" + "═" * 64 + "\n")
            fh.write(f"ИТОГ · {ts} UTC · {mode} · {cfg.address or '—'}\n")
            fh.write("─" * 64 + "\n")
            for name, detail, ok in rows:
                fh.write(f"  {'OK' if ok else '— '} {(name + ':'):<20} {detail}\n")
            fh.write("─" * 64 + "\n")
            fh.write("  ОБЪЁМЫ:\n")
            fh.write(f"    Unit (деп+вывод):  ${v['unit']:.2f}\n")
            fh.write(f"    HIP-3:             ${v['hip3']:.2f}\n")
            fh.write(f"    Perps:             ${v['perp']:.2f}\n")
            fh.write(f"    Спот (UETH):       ${v['spot']:.2f}\n")
            fh.write(f"  КОМИССИИ ВСЕГО:  ~${fees:.2f}\n")
    except Exception as e:
        print(f"  ⚠ не удалось записать {RUN_TXT}: {e}")

    # --- CSV для Excel (всё в $) ---
    row = {
        "timestamp_utc": ts, "mode": mode, "address": cfg.address or "",
        "bitget_out_eth": _g(braw, "amount"), "bitget_out_usd": _usd_num(braw.get("amount"), price),
        "bitget_received_eth": (_eths(braw["received"]) if braw.get("received") else ""),
        "deposit_eth": (_eths(draw["amount_eth"]) if draw.get("amount_eth") is not None else ""),
        "deposit_usd": _usd_num(draw.get("amount_eth"), price),
        "unit_volume_usd": f"{v['unit']:.2f}",
        "trade_volume_usd": _g(traw, "volume_total"), "trade_hip3_usd": _g(traw, "volume_hip3"),
        "trade_perp_usd": _g(traw, "volume_perp"), "trade_spot_usd": _g(traw, "volume_spot"),
        "ueth_before": _g(traw, "ueth_before"), "ueth_after": _g(traw, "ueth_after"),
        "withdraw_eth": (_eths(wraw["amount_eth"]) if wraw.get("amount_eth") is not None else ""),
        "withdraw_usd": _usd_num(wraw.get("amount_eth"), price),
        "withdraw_net_usd": _usd_num(wraw.get("net_eth"), price), "withdraw_credit": _g(wraw, "credit"),
        "fees_usd": f"{fees:.2f}", "volume_usd": f"{vol:.2f}",
    }
    try:
        new = not os.path.isfile(RUN_CSV)
        with open(RUN_CSV, "a", newline="", encoding="utf-8-sig") as fh:
            w = csv.DictWriter(fh, fieldnames=RUN_COLUMNS, delimiter=";")
            if new:
                w.writeheader()
            w.writerow(row)
    except Exception as e:
        print(f"  ⚠ не удалось записать {RUN_CSV}: {e}")

    print("  " + C.dim("💾 Итог: runs_log.txt (читаемый) + runs.csv (Excel)"))


# --------------------------------------------------------------------------- #
#  Запуск стадий                                                              #
# --------------------------------------------------------------------------- #
def run_one(key, cfg, live):
    name, fn = STAGES[key]
    try:
        return fn(cfg, live)
    except config_loader.ConfigError as e:
        return {"stage": key, "ok": False, "summary": f"конфиг: {e}", "spent": {}}
    except KeyboardInterrupt:
        return {"stage": key, "ok": False, "summary": "прервано (Ctrl+C)", "spent": {}}
    except Exception as e:
        traceback.print_exc()
        return {"stage": key, "ok": False, "summary": f"исключение: {e}", "spent": {}}


def print_stage_result(r):
    if r.get("ok"):
        print(f"\n  {C.ok('[✓]')} {r.get('summary', '')}")
    else:
        print(f"\n  {C.err('[✗]')} {C.warn(r.get('summary', ''))}")


def _pick_delay(spec):
    """Пауза в секундах: число (фикс.) или [мин,макс] (случайно). Иначе 0."""
    try:
        if isinstance(spec, (list, tuple)) and len(spec) == 2:
            return max(0.0, random.uniform(float(spec[0]), float(spec[1])))
        return max(0.0, float(spec))
    except Exception:
        return 0.0


def run_stage_list(keys, cfg, live, is_cycle, assume_yes):
    results = []
    ok_by_stage = {}   # стадия -> успех (для проверки зависимостей в этом прогоне)
    for i, key in enumerate(keys, 1):
        if is_cycle:
            print(C.cycle(f"\n  ─────── Шаг {i}/{len(keys)}: {STAGES[key][0]} ───────"))
        # Зависимость: если нужный предыдущий шаг был в прогоне, но НЕ удался — пропускаем
        # (нельзя торговать/выводить без средств на HL, нельзя депать без прихода с Bitget).
        dep = STAGE_DEPENDS.get(key)
        if dep and dep in keys and not ok_by_stage.get(dep, False):
            r = {"stage": key, "ok": False,
                 "summary": f"пропущено — предыдущий шаг «{STAGES[dep][0]}» не выполнен",
                 "spent": {}}
            results.append(r)
            ok_by_stage[key] = False
            print_stage_result(r)
            continue
        r = run_one(key, cfg, live)
        ok_by_stage[key] = bool(r.get("ok"))
        results.append(r)
        print_stage_result(r)
        if is_cycle and not r.get("ok") and i < len(keys):
            print(C.warn("  ⚠ Стадия не удалась — зависимые шаги будут пропущены."))
        # человеческая пауза перед следующим модулем
        if is_cycle and i < len(keys):
            d = _pick_delay(cfg.module_gap_sec)
            if d > 0:
                print(C.dim(f"  ⏳ пауза {d:.0f}с перед следующим модулем…"))
                time.sleep(d)
    return results


# --------------------------------------------------------------------------- #
#  Шапка / меню                                                               #
# --------------------------------------------------------------------------- #
def print_header(cfg):
    print(C.header("=" * 64))
    print(C.header("  HyperUnit  ·  Bitget → Unit → Hyperliquid → Bitget"))
    print(C.header("=" * 64))
    print(C.paint("  TG-канал: https://t.me/thatcryptofriend", "magenta"))
    wn = wallets_xlsx.count_wallets(cfg.wallets_file or None)
    print(f"  Кошельки: {C.bold('wallets.xlsx')} — {wn} шт. (1 строка = 1 аккаунт)")
    rpc = "свой (из .env)" if cfg.eth_rpc_url else "публичный по умолчанию"
    bg = "заданы" if all(cfg.bitget.values()) else "НЕ заданы (нужны для Bitget)"
    print(f"  ETH RPC:  {rpc}   |   ключи Bitget: {bg}")
    print(f"  Конфиг:   Bitget={cfg.bitget_cfg.get('amount_eth')} | "
          f"депозит {cfg.deposit_cfg.get('percent')}% | вывод {cfg.withdraw_cfg.get('amount')}")
    print("=" * 64)


def _persist(cfg):
    """Сохранить вкл/выкл и режим в config.json (с сохранением комментариев)."""
    try:
        config_loader.save_toggles(cfg)
    except Exception as e:
        print(f"  ⚠ не удалось сохранить config.json: {e}")


def _run_wallets(cfg, args, keys):
    """Прогнать стадии keys по КАЖДОМУ кошельку из wallets.xlsx (всегда реально, без вопросов)."""
    if not keys:
        print(C.warn("  Все модули выключены — включи хотя бы один (цифры в меню)."))
        return
    try:
        wallets = wallets_xlsx.read_wallets(cfg.wallets_file or None)
    except Exception as e:
        print(C.err(f"Список кошельков не прочитан: {e}"))
        print(C.dim(f"  Скопируй wallets.example.xlsx → {wallets_xlsx.DEFAULT_FILE} и заполни: "
                    "A — приватник, B — адрес депозита Bitget."))
        return
    wallets = [w for w in wallets if w.get("private_key")]
    if not wallets:
        print(C.err("В wallets.xlsx нет кошельков (A — приватник, B — адрес депозита Bitget)."))
        return
    # Резюме после обрыва: пропустить аккаунты, которые уже прогнаны (записаны в done_accounts.txt).
    if getattr(cfg, "skip_done_accounts", True):
        done = load_done_accounts()
        if done:
            before = len(wallets)
            wallets = [w for w in wallets if _addr_of(w.get("private_key")) not in done]
            skipped = before - len(wallets)
            if skipped:
                print(C.dim(f"  ⏭ пропускаю {skipped} уже сделанных (output/done_accounts.txt); осталось {len(wallets)}"))
            if not wallets:
                print(C.ok("  Все аккаунты из wallets.xlsx уже сделаны. "
                           "Удали output/done_accounts.txt, чтобы пройти всех заново."))
                return
    if getattr(cfg, "randomize_wallets", False) and len(wallets) > 1:
        random.shuffle(wallets)
        print(C.dim("  🔀 Порядок кошельков: случайный (этот запуск)"))
    print(C.header(f"\n▶ ЗАПУСК: {len(wallets)} аккаунт(ов) | стадии: "
                   + " → ".join(str(CYCLE_ORDER.index(k) + 1) for k in keys)))
    # Перед стартом: свести остатки с субаккаунтов Bitget на мейн (вдруг застряли с прошлого прогона).
    if all(cfg.bitget.values()):
        print(C.header("\nПроверяю субаккаунты Bitget перед стартом…"))
        bitget_api.use_keys(cfg)
        try:
            moved = bitget_api.sweep_all_subs_to_main("ETH", log=lambda m: print(C.dim(m)))
            print(C.ok(f"  Переведено на мейн: {moved} ETH") if moved > 0 else C.dim("  на субаккаунтах пусто"))
        except Exception as e:
            print(C.warn(f"  ⚠ проверка субакков не удалась: {e}"))
    accounts = []
    for idx, wallet in enumerate(wallets, 1):
        proxy = net_proxy.normalize_proxy(wallet.get("proxy"))
        wcfg = config_loader.clone_for_wallet(cfg, wallet["private_key"],
                                              wallet["bitget_address"], proxy)
        print(C.title("\n" + "#" * 64))
        print(C.title(f"#  АККАУНТ {idx}/{len(wallets)}: {wcfg.address or '??? плохой приватник'}"))
        print(C.title(f"#  возврат на Bitget: {wallet['bitget_address'] or '— не задан'}"))
        print(C.title(f"#  прокси: {net_proxy.mask_proxy(proxy)}"))
        print(C.title("#" * 64))
        if not wcfg.address:
            print(C.err("  ✗ плохой приватник — пропускаю."))
            continue
        # Прокси аккаунта: проверим живость и покажем exit-IP. Не блокирует — трафик всё
        # равно пойдёт через прокси (на основной IP аккаунт не выпускаем).
        if proxy:
            try:
                ip = net_proxy.proxy_exit_ip(proxy)
                print(C.dim(f"  🌐 прокси ОК | exit IP: {ip}"))
            except Exception as e:
                print(C.warn(f"  ⚠ прокси не ответил на проверке ({e}); всё равно иду через него"))
        else:
            print(C.warn("  ⚠ прокси не задан (столбец C в wallets.xlsx) — аккаунт идёт на ОСНОВНОМ IP"))
        # Весь HTTP-трафик аккаунта — через его прокси (Bitget исключён, если proxy_bitget=false).
        with net_proxy.account_proxy(proxy, bitget_through_proxy=getattr(cfg, "proxy_bitget", False)):
            results = run_stage_list(keys, wcfg, True, is_cycle=(len(keys) > 1), assume_yes=True)
            price = _eth_price_usd()
            print_spent_report(results, price)
            save_run_report(wcfg, results, True, price)
        accounts.append({"address": wcfg.address, "results": results, "price": price})
        # done — ТОЛЬКО если все ВКЛЮЧЁННЫЕ стадии прошли (results = только включённые); иначе failed + повтор
        if results and all(r.get("ok") for r in results):
            mark_account_done(wcfg.address)
        else:
            mark_account_failed(wcfg.address, results)
            print(C.warn("  ⚠ были ошибки → аккаунт записан в failed_accounts.txt, повторю при следующем запуске"))
        if idx < len(wallets):
            d = _pick_delay(cfg.module_gap_sec)
            if d > 0:
                print(C.dim(f"\n  ⏳ пауза {d:.0f}с перед следующим аккаунтом…"))
                time.sleep(d)
    # финал: свести остатки со всех субаккаунтов Bitget на мейн
    if all(cfg.bitget.values()):
        print(C.header("\nПеревожу остатки с субаккаунтов Bitget на мейн…"))
        bitget_api.use_keys(cfg)
        try:
            moved = bitget_api.sweep_all_subs_to_main("ETH", log=lambda m: print(C.dim(m)))
            print(C.ok(f"  Переведено на мейн: {moved} ETH") if moved > 0 else C.dim("  на субакках пусто"))
        except Exception as e:
            print(C.warn(f"  ⚠ перевод с субакков не удался: {e}"))
    print_accounts_summary(accounts)


def _do_run(cfg, args):
    """Запуск из меню: включённые модули по всем кошелькам из wallets.xlsx."""
    _run_wallets(cfg, args, enabled_cycle(cfg))


# --------------------------------------------------------------------------- #
#  Сводка по аккаунтам: объёмы по каждому + ОБЩИЕ траты (комиссии)             #
# --------------------------------------------------------------------------- #
ACCOUNTS_CSV = os.path.join(paths.OUTPUT_DIR, "accounts.csv")
ACCOUNTS_TXT = os.path.join(paths.OUTPUT_DIR, "accounts_summary.txt")


def print_accounts_summary(accounts):
    if not accounts:
        return
    rows, total_fees = [], Decimal(0)
    for a in accounts:
        fees, _ = _totals(a["results"], a["price"])
        v = _volumes(a["results"], a["price"])
        total_fees += fees
        rows.append({"address": a["address"], "fees": fees, "v": v})

    print()
    print(C.header("═" * 64))
    print(C.header(f"  СВОДКА ПО АККАУНТАМ ({len(rows)})  —  объёмы и комиссии"))
    print(C.dim("─" * 64))
    for r in rows:
        s = r["address"][:8] + "…" + r["address"][-4:]
        v = r["v"]
        vols = f"Unit ${v['unit']:.0f} · HIP-3 ${v['hip3']:.0f} · perps ${v['perp']:.0f}"
        print(f"  {C.bold(s)}  {vols}  |  " + C.money("~$" + format(r["fees"], ".2f")))
    print(C.dim("─" * 64))
    print("  " + C.bold("ОБЩИЕ ТРАТЫ (комиссии) по всем:") + "  " + C.money("~$" + format(total_fees, ".2f")))
    print(C.header("═" * 64))
    _save_accounts_summary(rows, total_fees)


def _save_accounts_summary(rows, total_fees):
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")
    try:
        with open(ACCOUNTS_TXT, "a", encoding="utf-8") as fh:
            fh.write("\n" + "═" * 70 + "\n")
            fh.write(f"СВОДКА · {ts} UTC · аккаунтов: {len(rows)}\n")
            fh.write("─" * 70 + "\n")
            for r in rows:
                v = r["v"]
                fh.write(f"  {r['address']}\n")
                fh.write(f"      объёмы: Unit ${v['unit']:.2f}  HIP-3 ${v['hip3']:.2f}  "
                         f"perps ${v['perp']:.2f}  спот ${v['spot']:.2f}   |   "
                         f"комиссии ~${r['fees']:.2f}\n")
            fh.write("─" * 70 + "\n")
            fh.write(f"  ОБЩИЕ ТРАТЫ (комиссии) по всем: ~${total_fees:.2f}\n")
    except Exception as e:
        print(f"  ⚠ не записал {os.path.basename(ACCOUNTS_TXT)}: {e}")
    try:
        new = not os.path.isfile(ACCOUNTS_CSV)
        with open(ACCOUNTS_CSV, "a", newline="", encoding="utf-8-sig") as fh:
            w = csv.writer(fh, delimiter=";")
            if new:
                w.writerow(["timestamp_utc", "address", "fees_usd",
                            "unit_usd", "hip3_usd", "perp_usd", "spot_usd"])
            for r in rows:
                v = r["v"]
                w.writerow([ts, r["address"], f"{r['fees']:.2f}", f"{v['unit']:.2f}",
                            f"{v['hip3']:.2f}", f"{v['perp']:.2f}", f"{v['spot']:.2f}"])
    except Exception as e:
        print(f"  ⚠ не записал {os.path.basename(ACCOUNTS_CSV)}: {e}")
    print("  " + C.dim(f"💾 Сводка по аккаунтам: {os.path.basename(ACCOUNTS_TXT)} + {os.path.basename(ACCOUNTS_CSV)}"))


def menu(cfg, args):
    """Точка входа меню: стрелочное (если терминал поддерживает) или текстовое."""
    if tui.supported():
        tui.run_menu(cfg, args, _do_run, _persist)
    else:
        menu_loop(cfg, args)


def menu_loop(cfg, args):
    def badge(key):
        return "[✓ ВКЛ ]" if cfg.enabled.get(key, True) else "[✗ выкл]"

    names = {
        "bitget": "Вывод ETH с Bitget", "deposit": "Депозит ETH на Hyperliquid (Unit)",
        "trade": "Торговля / набивка объёма", "withdraw": "Вывод ETH с Hyperliquid (Unit)",
        "bitget_return": "Возврат ETH на Bitget",
    }
    while True:
        print_header(cfg)
        cyc = enabled_cycle(cfg)
        cyc_str = " → ".join(str(CYCLE_ORDER.index(k) + 1) for k in cyc) or "ничего не выбрано"
        print("  Модули (цифра — включить/выключить, сохраняется сразу):")
        for i, k in enumerate(CYCLE_ORDER, 1):
            print(f"    {i}) {badge(k)}  {names.get(k, STAGES[k][0])}")
        rnd = "[✓ ВКЛ ]" if getattr(cfg, "randomize_wallets", False) else "[✗ выкл]"
        print(f"    r) {rnd}  Случайный порядок кошельков")
        skp = "[✓ ВКЛ ]" if getattr(cfg, "skip_done_accounts", True) else "[✗ выкл]"
        print(f"    d) {skp}  Пропускать уже сделанные (из output/done_accounts.txt)")
        print(f"    s) ▶ ЗАПУСТИТЬ по wallets.xlsx:  {cyc_str}")
        print("    0) Выход")
        choice = input("  Выбор: ").strip().lower()

        if choice in ("0", "q", "exit", "выход"):
            print("Выход. (настройки сохранены в config.json)")
            return
        if choice in NUM_TO_KEY:                       # вкл/выкл модуль
            key = NUM_TO_KEY[choice]
            cfg.enabled[key] = not cfg.enabled.get(key, True)
            _persist(cfg)
            continue
        if choice == "r":                              # вкл/выкл случайный порядок
            cfg.randomize_wallets = not getattr(cfg, "randomize_wallets", False)
            _persist(cfg)
            continue
        if choice == "d":                              # вкл/выкл пропуск прогнанных (резюме)
            cfg.skip_done_accounts = not getattr(cfg, "skip_done_accounts", True)
            _persist(cfg)
            continue
        if choice in ("s", "go", "run", "запуск", "старт"):
            _do_run(cfg, args)
            input("\n  Enter — вернуться в меню… ")
            continue
        print("  Не понял. Цифры 1-5 — вкл/выкл, r — случайный порядок, d — пропуск сделанных, s — запуск, 0 — выход.")


# --------------------------------------------------------------------------- #
#  main                                                                       #
# --------------------------------------------------------------------------- #
def main():
    p = argparse.ArgumentParser(description="HyperUnit: Bitget/Unit/Hyperliquid по wallets.xlsx")
    p.add_argument("--stage", help="1..5 | bitget|deposit|trade|withdraw|bitget_return | cycle — "
                                    "прогнать эту стадию/цикл по ВСЕМ кошелькам из wallets.xlsx")
    args = p.parse_args()

    try:
        cfg = config_loader.load()
    except config_loader.ConfigError as e:
        sys.exit(f"Ошибка конфигурации: {e}")

    if not args.stage:
        try:
            menu(cfg, args)
        except KeyboardInterrupt:
            print("\nВыход.")
        return 0

    # неинтерактивный запуск — стадия/цикл по всем кошелькам
    stage = args.stage.strip().lower()
    print_header(cfg)
    try:
        if stage in ("cycle", "цикл", "all", "все"):
            _run_wallets(cfg, args, enabled_cycle(cfg))
        elif stage in NUM_TO_KEY:
            _run_wallets(cfg, args, [NUM_TO_KEY[stage]])
        elif stage in STAGES:
            _run_wallets(cfg, args, [stage])
        else:
            sys.exit(f"Неизвестная стадия: {args.stage}")
    except KeyboardInterrupt:
        print("\nПрервано.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
