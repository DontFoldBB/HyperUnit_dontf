# -*- coding: utf-8 -*-
"""
Стадия 3: торговля / набивка объёма на Hyperliquid.
Переиспользует готовый circle.run_circle() из hyperliquid_trade/circle.py.
Приватник берётся из общего конфига (не спрашивается).
"""

import paths  # noqa: F401  (sys.path)
import colors as C
import circle


def run(cfg, live):
    """cfg — Config; live — реальная торговля (True) или тест без сделок (False).
    Возвращает нормализованный результат стадии."""
    cfg.require_private_key()
    t = cfg.trade_cfg

    hip3 = t.get("hip3_assets") or []
    perp = (t.get("perp") or "none").lower()
    single = t.get("single_coin") or None
    if not hip3 and perp == "none":
        return {"stage": "trade", "ok": False,
                "summary": "нечего торговать: в config.json trade.hip3_assets пуст и trade.perp = none",
                "spent": {}, "raw": {}}

    print(C.step("\n=== [3] Торговля на Hyperliquid ==="))
    single_lbl = ", ".join(single) if isinstance(single, (list, tuple)) else (single or "")
    print(f"  HIP-3: {hip3 or '—'} | perp: {perp}"
          + (f" ({single_lbl})" if perp == "single" and single_lbl else "")
          + f" | маржа {t['pct']}% | плечо {t['leverage']}x")
    if (t.get("target_hip3") or 0) or (t.get("target_perp") or 0):
        print(f"  Цель объёма (на этот кошелёк): HIP-3 ${t.get('target_hip3') or 0} | "
              f"perps ${t.get('target_perp') or 0}")

    res = circle.run_circle(
        private_key=cfg.private_key,
        hip3_assets=hip3,
        perp=perp,
        single_coin=single,
        pct=float(t["pct"]),
        leverage=t["leverage"],
        target_hip3=float(t["target_hip3"]),
        target_perp=float(t["target_perp"]),
        hold_minutes=t["hold_minutes"],
        gap_minutes=t["gap_minutes"],
        live=bool(live),
        log_csv=True,
        reserve_usdc=float(t.get("reserve_usdc", 1.0)),
        random_pick=True,                       # HIP-3 — случайный актив из пула, а не обход списка
        size_jitter=float(t.get("size_jitter", 0.35)),  # размеры позиций разные (±%)
        # builder-комиссия (монетизация): тумблер в config; адрес/ставка вшиты в circle.py. выкл -> ордера как раньше
        builder_enabled=bool(getattr(cfg, "builder", {}).get("enabled")),
        # выключать DEX abstraction перед торговлей (по умолчанию выкл — режим аккаунта не трогаем)
        disable_abstraction=bool(getattr(cfg, "disable_dex_abstraction", False)),
    )

    summary = (f"UETH {res.get('ueth_before')} → {res.get('ueth_after')} | "
               f"объём всего ${res.get('volume_total')} "
               f"(HIP-3 ${res.get('volume_hip3')}, perps ${res.get('volume_perp')}, "
               f"спот ${res.get('volume_spot')}) | открытых позиций: {res.get('positions_left')}")
    return {
        "stage": "trade",
        "ok": bool(res.get("ok")),
        "summary": summary,
        "spent": {"usd": res.get("spent_usd"), "ueth": res.get("spent_ueth")},
        "raw": res,
    }
