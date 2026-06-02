#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Кросс-платформенная точка входа.

    python run.py                 # меню
    python run.py --stage cycle   # без меню, по всем кошелькам

Добавляет app/ в sys.path и выполняет app/main.py как __main__ — не зависит от
текущей рабочей директории.
"""

import os
import runpy
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
APP = os.path.join(ROOT, "app")
if APP not in sys.path:
    sys.path.insert(0, APP)

runpy.run_path(os.path.join(APP, "main.py"), run_name="__main__")
