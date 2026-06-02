# -*- coding: utf-8 -*-
"""
Структурный хаб прод-сборки: задаёт все каталоги проекта и регистрирует в
sys.path папки с кодом, чтобы плоские импорты (``import colors``, ``import
circle``, ``import stage_bitget`` …) работали из любой точки запуска.

Раскладка проекта:
    HyperUnit_script_dontfoldbb/        <- ROOT_DIR
        app/                            <- APP_DIR (мы здесь: оркестратор combined)
            stages/                     <- STAGES_DIR (5 стадий)
        lib/                            <- LIB_DIR (вендоренные оригиналы)
            withdraw_eth.py             (Bitget вывод)
            deposit_eth.py              (Bitget возврат)
            hyperunit_deposit.py        (депозит на HL через Unit)
            hyperliquid_withdrawal.py   (вывод с HL через Unit)
            circle.py                   (торговля/набивка объёма)
        config/                         <- CONFIG_DIR (.env, config.json, wallets.xlsx)
        output/                         <- OUTPUT_DIR (runs.*, accounts.* — рантайм)

Папка самодостаточна: модули из lib/ переиспользуются ИМПОРТОМ (без копирования
логики), секреты/параметры combined прокидывает в них сам.
"""

import os
import sys

APP_DIR = os.path.dirname(os.path.abspath(__file__))
STAGES_DIR = os.path.join(APP_DIR, "stages")
ROOT_DIR = os.path.dirname(APP_DIR)
LIB_DIR = os.path.join(ROOT_DIR, "lib")
CONFIG_DIR = os.path.join(ROOT_DIR, "config")
OUTPUT_DIR = os.path.join(ROOT_DIR, "output")

# Обратная совместимость со старым кодом combined (раньше всё лежало в combined/).
COMBINED_DIR = APP_DIR

# Папки с импортируемым кодом — их добавляем в sys.path.
CODE_DIRS = [APP_DIR, STAGES_DIR, LIB_DIR]


def setup():
    """Добавить папки с кодом в sys.path (идемпотентно) и создать output/."""
    for p in CODE_DIRS:
        if p not in sys.path:
            sys.path.insert(0, p)
    try:
        os.makedirs(OUTPUT_DIR, exist_ok=True)
    except Exception:
        pass


setup()
