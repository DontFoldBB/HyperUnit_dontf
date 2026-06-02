# HyperUnit — Bitget → Unit → Hyperliquid → возврат на Bitget

Прод-сборка объединённого скрипта. Один прогон делает весь цикл вокруг **одного
EVM-кошелька** (он же аккаунт Hyperliquid), по списку кошельков из `config/wallets.xlsx`:

1. **Вывод ETH с Bitget** → на кошелёк (сеть Ethereum).
2. **Депозит** % ETH **на Hyperliquid** через мост Unit (приходит как спот-токен UETH).
3. **Торговля / набивка объёма** на Hyperliquid (HIP-3 trade.xyz и/или перпы).
4. **Вывод** ETH **с Hyperliquid** обратно в сеть Ethereum через Unit.
5. **Возврат ETH на Bitget** — на депозит-адрес из `wallets.xlsx` (замыкает круг,
   ждёт зачисления и сводит субаккаунт → мейн).

> ⚠ **Запуск ВСЕГДА реальный** — двигаются настоящие деньги. Тест-режима нет.

---

## Структура папки

```
HyperUnit_script_dontfoldbb/
├─ ЗАПУСТИТЬ.bat        ← двойной клик для запуска (Windows)
├─ run.py               ← кросс-платформенный запуск: python run.py
├─ requirements.txt     ← зависимости (если собирать свой venv)
├─ config/              ← НАСТРОЙКИ (заполнить тут)
│   ├─ .env             ← СЕКРЕТЫ: ключи Bitget, RPC (из .env.example)
│   ├─ config.json      ← параметры всех стадий (из config.example.json)
│   └─ wallets.xlsx     ← список кошельков (из wallets.example.xlsx)
├─ app/                 ← код оркестратора
│   ├─ main.py          ← точка входа
│   ├─ paths.py         ← раскладка каталогов + sys.path
│   ├─ config_loader.py, wallets_xlsx.py, tui.py, colors.py, bitget_api.py
│   └─ stages/          ← 5 стадий (stage_bitget … stage_bitget_return)
├─ lib/                 ← вендоренные оригиналы (логика не меняется)
│   ├─ withdraw_eth.py            (вывод с Bitget)
│   ├─ deposit_eth.py             (возврат на Bitget)
│   ├─ hyperunit_deposit.py       (депозит на HL через Unit)
│   ├─ hyperliquid_withdrawal.py  (вывод с HL через Unit)
│   └─ circle.py                  (торговля/набивка объёма)
└─ output/              ← отчёты прогонов (создаётся автоматически)
```

Папка **самодостаточна**: модули из `lib/` переиспользуются импортом, соседние
проекты для работы не нужны.

---

## Настройка (один раз)

Всё — в папке **`config/`**. Скопируй образцы и заполни:

| Скопировать                         | → в                    | Что внутри                                          |
|-------------------------------------|------------------------|-----------------------------------------------------|
| `config/.env.example`               | `config/.env`          | СЕКРЕТЫ: ключи Bitget, (опц.) RPC Ethereum           |
| `config/config.example.json`        | `config/config.json`   | параметры всех стадий                                |
| `config/wallets.example.xlsx`       | `config/wallets.xlsx`  | список кошельков (A=приватник, B=адрес Bitget)       |

1. **`.env`** — один аккаунт Bitget на все кошельки:
   - `BITGET_API_KEY` / `BITGET_API_SECRET` / `BITGET_API_PASSPHRASE` — право **Spot** + **Withdraw** + IP-whitelist.
   - `ETH_RPC_URL` — **необязательно** (пусто = публичные RPC; свой — если публичные тормозят).
   - `PRIVATE_KEY` не нужен: приватники берутся из `wallets.xlsx`.

2. **`wallets.xlsx`** — по строке на аккаунт: **A — приватник**, **B — адрес депозита ETH с Bitget**
   (куда вернуть). 1 строка = 1 аккаунт, N строк = N.

3. **`config.json`** (комментарии `//` и `#` разрешены):
   - `modules` — `true/false` каждый модуль (или галочки 1-5 в меню).
   - `bitget_amount_eth` — сколько вывести: `"0.01"` / `"all"` / `"50%"` /
     **`"30-50%"`** (случайный % ETH-баланса, свой на каждый кошелёк) / `"0.01-0.03"`.
   - `deposit_percent`, `trade_hip3` / `trade_perp` / `trade_margin_pct` / `trade_leverage` /
     `trade_hold_minutes` / `trade_gap_minutes`, `withdraw_amount` — параметры стадий.
   - `bitget_return_percent` — % ETH назад на Bitget (100 = весь минус газ). Адрес — из `wallets.xlsx` (B).
   - `advanced` — ожидания/таймауты + `module_gap_sec` (пауза между шагами), обычно не трогаешь.

---

## Запуск

Двойной клик по **`ЗАПУСТИТЬ.bat`** — или из терминала:

```bat
python run.py                  REM кросс-платформенно
REM либо напрямую:
python app\main.py
```

Лаунчер `ЗАПУСТИТЬ.bat` сам ищет Python: сначала локальный `.venv\`, затем общий
`..\first_try\hyperliquid_trade\.venv\`, затем системный `python`. Если нужен свой venv —
`python -m venv .venv` в корне и `.\.venv\Scripts\pip install -r requirements.txt`.

Появится **цветное меню со стрелками**: отмечаешь модули и запускаешь — прогон идёт
**по всем кошелькам из `config/wallets.xlsx`**:

```
  ⚠ РЕАЛЬНЫЙ запуск · кошельки из wallets.xlsx
  ► [✓] Вывод ETH с Bitget
    [✓] Депозит на Hyperliquid (Unit)
    [✓] Торговля / набивка объёма
    [✓] Вывод ETH с Hyperliquid (Unit)
    [✓] Возврат ETH на Bitget
        ▶ ЗАПУСТИТЬ по wallets.xlsx  (1 → 2 → 3 → 4 → 5)
        Выход
```

- **↑/↓** — выбор, **Enter/Пробел** — вкл/выкл модуль либо «Запустить», **q** — выход.
- Галочки **сразу сохраняются** в `config/config.json`. Выключенный модуль пропускается.
- Если терминал без стрелок — простое текстовое меню (цифры 1-5 вкл/выкл, `s` — запуск).

### Без меню (автоматизация)

```bat
python app\main.py --stage cycle    REM все включённые стадии по всем кошелькам
python app\main.py --stage trade    REM только торговля
python app\main.py --stage 1        REM только Bitget
```

`--stage`: `1..5` | `bitget|deposit|trade|withdraw|bitget_return` | `cycle`.

---

## Важные нюансы

- **Кошельки — из `config/wallets.xlsx`** (приватник + адрес возврата Bitget на каждый). Один аккаунт Bitget на всех.
- **Чтение баланса Bitget** требует право **Spot** у ключа (для `"all"`/`"%"`/`"30-50%"`).
- **Минимум Unit** ~0.007 ETH (тянется live с сайта). Меньше — мост может не зачислить.
- **Первый вывод на новый адрес Unit** стоит ~1 USDC (activation gas) — торговля оставляет резерв `reserve_usdc`.
- В полном цикле стадии читают **живые балансы**, поэтому между ними есть ожидания зачисления.

## Отчёты

В конце каждого прогона combined сохраняет в **`output/`** (все суммы в долларах):

- **`runs_log.txt`** — читаемый блок на каждый прогон (что вывел/задепнул, объёмы, **КОМИССИИ ВСЕГО**).
- **`runs.csv`** — то же для Excel (`;`-разделитель).
- **`accounts_summary.txt`** / **`accounts.csv`** — сводка по аккаунтам в конце прогона по списку.

Модули из `lib/` дополнительно пишут свои детальные журналы рядом с собой
(`lib/withdrawals_log.csv`, `lib/deposits_log.csv`, `lib/circle_log.csv` и т.п.).
