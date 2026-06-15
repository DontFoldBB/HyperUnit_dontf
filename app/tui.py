# -*- coding: utf-8 -*-
"""
Цветное меню со стрелками (Windows / Linux / macOS, без сторонних библиотек).

  ↑/↓ (или k/j) — выбор строки
  Enter / Пробел — переключить модуль / режим, либо «запустить»
  q / Esc        — выход

Если терминал не интерактивный (ввод перенаправлен) — main использует текстовое
меню (menu_loop) как запасной вариант.
"""

import os
import sys

import wallets_xlsx

# --- ANSI-цвета ---
RESET = "\033[0m"
BOLD = "\033[1m"
DIM = "\033[2m"
REV = "\033[7m"
CYAN = "\033[96m"
GREEN = "\033[92m"
RED = "\033[91m"
YELLOW = "\033[93m"
GREY = "\033[90m"
WHITE = "\033[97m"
MAGENTA = "\033[95m"

CYCLE_ORDER = ["bitget", "deposit", "trade", "withdraw", "bitget_return"]
MOD_NAME = {
    "bitget": "Вывод ETH с Bitget",
    "deposit": "Депозит на Hyperliquid (Unit)",
    "trade": "Торговля / набивка объёма",
    "withdraw": "Вывод ETH с Hyperliquid (Unit)",
    "bitget_return": "Возврат ETH на Bitget",
}


def supported():
    """Можно ли рисовать стрелочное меню (интерактивный терминал: Windows, Linux или macOS)."""
    try:
        if not (sys.stdin.isatty() and sys.stdout.isatty()):
            return False
        if os.name == "nt":
            import msvcrt  # noqa: F401
        else:
            import termios  # noqa: F401  (Linux/macOS: посимвольное чтение клавиш)
    except Exception:
        return False
    return True


def _enable_ansi():
    # На Unix (Linux/macOS) ANSI работает из коробки — включать ничего не нужно.
    if os.name != "nt":
        return
    try:
        import ctypes
        k = ctypes.windll.kernel32
        # ENABLE_PROCESSED_OUTPUT | ENABLE_WRAP_AT_EOL_OUTPUT | ENABLE_VIRTUAL_TERMINAL_PROCESSING
        k.SetConsoleMode(k.GetStdHandle(-11), 7)
    except Exception:
        pass


def _clear():
    sys.stdout.write("\033[2J\033[H")
    sys.stdout.flush()


def _enter_fullscreen():
    # Альт-экран (как у vim/htop): меню рисуется в отдельном буфере и НЕ копится в истории.
    sys.stdout.write("\033[?1049h\033[?25l\033[H\033[2J")   # альт-буфер + спрятать курсор + очистить
    sys.stdout.flush()


def _exit_fullscreen():
    sys.stdout.write("\033[?25h\033[?1049l")                # вернуть курсор и основной экран
    sys.stdout.flush()


# ANSI-коды стрелок (Unix): после ESC идёт "[A"/"[B"/… или "OA"/"OB"/… -> наши имена
_ARROW_SEQ = {"[A": "up", "[B": "down", "[C": "right", "[D": "left",
              "OA": "up", "OB": "down", "OC": "right", "OD": "left"}


def _getkey():
    """Одно нажатие -> 'up'/'down'/'left'/'right'/'enter'/'space'/'esc'/буква."""
    return _getkey_nt() if os.name == "nt" else _getkey_posix()


def _getkey_nt():
    import msvcrt
    ch = msvcrt.getwch()
    if ch in ("\x00", "\xe0"):                     # спец-клавиша: стрелки и т.п.
        code = msvcrt.getwch()
        return {"H": "up", "P": "down", "K": "left", "M": "right"}.get(code, "")
    if ch in ("\r", "\n"):
        return "enter"
    if ch == " ":
        return "space"
    if ch == "\x1b":
        return "esc"
    if ch == "\x03":
        raise KeyboardInterrupt
    return ch.lower()


def _getkey_posix():
    import termios
    import tty
    import select
    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    try:
        tty.setcbreak(fd)                          # посимвольно, без эха; Ctrl+C остаётся сигналом
        ch = os.read(fd, 1).decode("utf-8", "ignore")
        if ch in ("\r", "\n"):
            return "enter"
        if ch == " ":
            return "space"
        if ch == "\x03":                           # подстраховка (обычно ловится как SIGINT)
            raise KeyboardInterrupt
        if ch == "\x1b":                           # ESC: либо сама Esc, либо начало стрелки
            r, _, _ = select.select([fd], [], [], 0.05)
            if not r:                              # больше байт нет — это была Esc
                return "esc"
            seq = os.read(fd, 2).decode("utf-8", "ignore")
            return _ARROW_SEQ.get(seq, "")
        return ch.lower()
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)


def _cycle_str(cfg):
    nums = [str(i + 1) for i, k in enumerate(CYCLE_ORDER) if cfg.enabled.get(k, True)]
    return " → ".join(nums) or "—"


def _rows(cfg):
    """Список строк меню: (kind, цветной_текст, plain_текст)."""
    rows = []
    for k in CYCLE_ORDER:
        on = cfg.enabled.get(k, True)
        box_c = f"{GREEN}[✓]{RESET}" if on else f"{GREY}[ ]{RESET}"
        box_p = "[✓]" if on else "[ ]"
        name = MOD_NAME[k]
        rows.append((f"mod:{k}", f"{box_c} {name}", f"{box_p} {name}"))

    rnd = getattr(cfg, "randomize_wallets", False)
    rbox_c = f"{GREEN}[✓]{RESET}" if rnd else f"{GREY}[ ]{RESET}"
    rbox_p = "[✓]" if rnd else "[ ]"
    rname = "Случайный порядок кошельков"
    rows.append(("opt:randomize", f"{rbox_c} {CYAN}{rname}{RESET}", f"{rbox_p} {rname}"))

    skp = getattr(cfg, "skip_done_accounts", True)
    sbox_c = f"{GREEN}[✓]{RESET}" if skp else f"{GREY}[ ]{RESET}"
    sbox_p = "[✓]" if skp else "[ ]"
    sname = "Пропускать уже сделанные (из output/done_accounts.txt)"
    rows.append(("opt:skipdone", f"{sbox_c} {CYAN}{sname}{RESET}", f"{sbox_p} {sname}"))

    lim = getattr(cfg, "limit_orders", False)
    lbox_c = f"{GREEN}[✓]{RESET}" if lim else f"{GREY}[ ]{RESET}"
    lbox_p = "[✓]" if lim else "[ ]"
    lname = "Лимитки (перпы/HIP-3 лимит-ордерами — дешевле маркета)"
    rows.append(("opt:limit", f"{lbox_c} {CYAN}{lname}{RESET}", f"{lbox_p} {lname}"))

    rec = getattr(cfg, "recover_tails", False)
    cbox_c = f"{GREEN}[✓]{RESET}" if rec else f"{GREY}[ ]{RESET}"
    cbox_p = "[✓]" if rec else "[ ]"
    cname = "Продолжить прошлый прогон (подобрать позиции/USDC)"
    rows.append(("opt:recover", f"{cbox_c} {CYAN}{cname}{RESET}", f"{cbox_p} {cname}"))

    cyc = _cycle_str(cfg)
    rows.append(("run", f"    {GREEN}{BOLD}▶ ЗАПУСТИТЬ по wallets.xlsx{RESET}  ({cyc})",
                 f"    ▶ ЗАПУСТИТЬ по wallets.xlsx  ({cyc})"))
    rows.append(("exit", "    Выход", "    Выход"))
    return rows


def _render(cfg, rows, cursor):
    _clear()
    bar = "═" * 60
    print(f"{CYAN}{BOLD}{bar}{RESET}")
    print(f"{CYAN}{BOLD}   Bitget → Unit → Hyperliquid → вывод{RESET}")
    print(f"{CYAN}{BOLD}{bar}{RESET}")
    print(f"  {MAGENTA}TG-канал: https://t.me/thatcryptofriend{RESET}")
    n = wallets_xlsx.count_wallets(getattr(cfg, "wallets_file", "") or None)
    cnt = f"{GREEN}{n}{RESET}" if n else f"{RED}0 — заполни файл{RESET}"
    print(f"  кошельки из {WHITE}wallets.xlsx{RESET}: {cnt}")
    rpc = "свой (.env)" if cfg.eth_rpc_url else "публичный"
    bg = f"{GREEN}есть{RESET}" if all(cfg.bitget.values()) else f"{RED}нет{RESET}"
    print(f"  RPC: {rpc}    ключи Bitget: {bg}")
    print(f"  {DIM}Bitget {cfg.bitget_cfg.get('amount_eth')} · депозит {cfg.deposit_cfg.get('percent')}%"
          f" · вывод {cfg.withdraw_cfg.get('amount')}{RESET}")
    print()
    print(f"  {DIM}↑/↓ — выбор   Enter/Пробел — вкл-выкл / запуск   q — выход{RESET}")
    print()
    for i, (_kind, colored, plain) in enumerate(rows):
        if i == cursor:
            print(f"  {YELLOW}{BOLD}►{RESET} {REV}{plain} {RESET}")
        else:
            print(f"    {colored}")
    sys.stdout.flush()


def run_menu(cfg, args, do_run, persist):
    """Главный цикл стрелочного меню.
    do_run(cfg, args) — запустить по wallets.xlsx; persist(cfg) — сохранить вкл/выкл в config.json."""
    _enable_ansi()
    _enter_fullscreen()
    try:
        cursor = 0
        while True:
            rows = _rows(cfg)
            _render(cfg, rows, cursor)
            try:
                key = _getkey()
            except KeyboardInterrupt:
                return
            n = len(rows)
            if key in ("up", "k"):
                cursor = (cursor - 1) % n
            elif key in ("down", "j"):
                cursor = (cursor + 1) % n
            elif key in ("q", "esc"):
                return
            elif key in ("enter", "space"):
                kind = rows[cursor][0]
                if kind.startswith("mod:"):
                    k = kind[4:]
                    cfg.enabled[k] = not cfg.enabled.get(k, True)
                    persist(cfg)
                elif kind == "opt:randomize":
                    cfg.randomize_wallets = not getattr(cfg, "randomize_wallets", False)
                    persist(cfg)
                elif kind == "opt:skipdone":
                    cfg.skip_done_accounts = not getattr(cfg, "skip_done_accounts", True)
                    persist(cfg)
                elif kind == "opt:limit":
                    cfg.limit_orders = not getattr(cfg, "limit_orders", False)
                    persist(cfg)
                elif kind == "opt:recover":
                    cfg.recover_tails = not getattr(cfg, "recover_tails", False)
                    persist(cfg)
                elif kind == "run":
                    _exit_fullscreen()          # вывод прогона — на основном экране (останется в истории)
                    do_run(cfg, args)
                    try:
                        input("\n  Enter — назад в меню… ")
                    except EOFError:
                        return
                    _enter_fullscreen()         # назад в чистое меню
                elif kind == "exit":
                    return
    finally:
        _exit_fullscreen()
