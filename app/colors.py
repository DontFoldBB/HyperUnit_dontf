# -*- coding: utf-8 -*-
"""
Маленький помощник ANSI-цветов. Красит ТОЛЬКО если вывод идёт в терминал (tty);
при перенаправлении/в файл — возвращает чистый текст без кодов.
"""

import os
import sys

_CODES = {
    "reset": "\033[0m", "bold": "\033[1m", "dim": "\033[2m",
    "red": "\033[91m", "green": "\033[92m", "yellow": "\033[93m",
    "blue": "\033[94m", "magenta": "\033[95m", "cyan": "\033[96m",
    "white": "\033[97m", "grey": "\033[90m",
}


def _detect():
    try:
        if not sys.stdout.isatty():
            return False
    except Exception:
        return False
    if os.name == "nt":
        try:
            import ctypes
            k = ctypes.windll.kernel32
            k.SetConsoleMode(k.GetStdHandle(-11), 7)  # включить ANSI в консоли Windows
        except Exception:
            return False
    return True


ENABLED = _detect()


def paint(text, *styles):
    if not ENABLED or not styles:
        return text
    pre = "".join(_CODES.get(s, "") for s in styles)
    return f"{pre}{text}{_CODES['reset']}"


# готовые роли
def header(t):  return paint(t, "cyan", "bold")
def step(t):    return paint(t, "green", "bold")    # заголовок модуля/стадии [1]..[5]
def title(t):   return paint(t, "magenta", "bold")  # баннер аккаунта (####)
def cycle(t):   return paint(t, "blue", "bold")     # разделитель шага i/N (───)
def ok(t):      return paint(t, "green")
def err(t):     return paint(t, "red", "bold")
def warn(t):    return paint(t, "yellow")
def dim(t):     return paint(t, "grey")
def money(t):   return paint(t, "green", "bold")
def bold(t):    return paint(t, "bold")
