**English** · [Русский](README.ru.md)

# HyperUnit — auto-cycle Bitget → Hyperliquid → Bitget

For each wallet in your list the script runs the full loop and builds volume on Hyperliquid:

1. withdraws ETH from **Bitget** to your wallet;
2. bridges part of it to **Hyperliquid** via the **Unit** bridge;
3. **trades / builds volume** (HIP-3 on trade.xyz and/or perps);
4. withdraws back to Ethereum via Unit;
5. **returns ETH to Bitget** and sweeps to the main account — then moves on to the next wallet in the list.

---

## What you need first

- **Windows, Linux or macOS** and **Python 3.10+**. On Windows, when installing Python tick *“Add Python to PATH”*.
  Check: open a terminal and run `python --version` (on Linux/macOS — `python3 --version`).
- A **Bitget account** with an API key: **Spot** + **Withdraw** permissions, and your IP added to the key's **whitelist**.
- **Private keys** of your wallets.

---

## Setup — step by step

> The `config\` folder ships with **`*.example`** templates. For each one, make a copy **without** `.example` and fill it in — your real `config\.env`, `config\config.json`, `config\wallets.xlsx` stay on your machine (git ignores them, so your keys are never committed).

### Step 1. Install dependencies
Open a terminal (PowerShell) **in the project folder** and run:
```powershell
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
```
This creates a local `.venv` — `ЗАПУСТИТЬ.bat` will find it automatically.

A venv is **optional**: you can instead run `pip install -r requirements.txt` with your system Python and launch directly — `ЗАПУСТИТЬ.bat` falls back to the system `python`. The venv just keeps these dependencies isolated from other projects.

### Step 2. Bitget keys — file `config\.env`
Copy **`config\.env.example`** → **`config\.env`**, then open `config\.env` in a text editor and fill in your keys (no quotes, no spaces around `=`):
```
BITGET_API_KEY=your_key
BITGET_API_SECRET=your_secret
BITGET_API_PASSPHRASE=your_passphrase
ETH_RPC_URL=            ← can be left empty (public nodes will be used)
```

### Step 3. Wallets — file `config\wallets.xlsx`
Copy **`config\wallets.example.xlsx`** → **`config\wallets.xlsx`**, then open it in Excel and fill in:
- **column A** — wallet private key (`0x...`);
- **column B** — the ETH deposit address on Bitget (where funds return at the end of the loop);
- **column C** *(optional)* — a per-wallet proxy (`http://user:pass@host:port`). All of that wallet's traffic (Unit / Hyperliquid / RPC) goes through it; Bitget stays on your main IP unless `proxy_bitget` is on. Leave it empty to use your main IP.
- **One row = one account.** As many rows as accounts to run, one after another. The header row can stay — it's skipped.

> ⚠️ **Don't forget to add every wallet address to the *withdrawal whitelist* on Bitget** — step 1 withdraws ETH from Bitget to these addresses, so without whitelisting the withdrawal will fail.

### Step 4. Parameters — file `config\config.json` (the most important)
Copy **`config\config.example.json`** → **`config\config.json`**, then open it and tune — this is where you set **what and how much to do**. Every line is documented inside the file; `//` and `#` comments are allowed.

**Ranges.** Many numbers can be given as a “range” — the value is picked randomly (a separate one per wallet/trade) so amounts aren't round: `"90-95"` = random between 90 and 95; a plain number (`90`) = fixed.

**How much money to move:**
- `bitget_amount_eth` — how much to withdraw from Bitget per wallet: `"0.01"` (that much ETH), `"all"` (full balance) or `"80-100%"` (random % of balance);
- `deposit_percent` — what % to bridge to Hyperliquid via Unit (e.g. `"90-95"`);
- `withdraw_amount` — how much to then withdraw from Hyperliquid: `"all"` or a range like `"50-70%"`;
- `bitget_return_percent` — how much to return to Bitget (`100` = everything minus gas).

**What to trade:**
- `trade_hip3` — list of HIP-3 assets (trade.xyz), e.g. `["NVDA","TSLA","GOLD"]`. `[]` = don't trade HIP-3;
- `trade_perp` — perps: `"none"` (off), `"pair"` (BTC+ETH together) or `"single"` (cycles a pool of perps one by one);
- `trade_single_coin` — pool of perps for `"single"` mode, e.g. `["BTC","SOL","XRP"]`.

**How much volume to build (in $):**
- `trade_target_hip3` — target $ volume on HIP-3 assets;
- `trade_target_perp` — target $ volume on perps.
- The script opens and closes positions **until it reaches this amount**. Set **`0` — one pass** (just opens and immediately closes one position, no volume building).

**Exactly how to trade:**
- `trade_margin_pct` — what share of funds to use as margin per trade, % (e.g. `"50-95"`);
- `trade_leverage` — leverage (e.g. `"5-7"`; rounded to an integer for perps);
- `trade_hold_minutes` — how long to **hold each position, in minutes** (`[0.5, 2]` = random 0.5–2 min);
- `trade_gap_minutes` — **pause between trades, in minutes** (`[0.2, 1]` = random 0.2–1 min).

**The rest:**
- `modules` (top of the file) — which of the 5 steps are enabled (`true/false`); can also be toggled in the menu at launch;
- `randomize_wallets` — `true` shuffles the wallet order on each run (`false` = order from the file); can also be toggled in the menu;
- `skip_done_accounts` — `true` skips accounts where **all enabled stages passed** (logged to `output/done_accounts.txt`) so you can resume after an interruption. If any enabled stage failed, the account goes to `output/failed_accounts.txt` and is retried on the next run. Delete `done_accounts.txt` to run everyone from scratch;
- `resume_failed` — `true` (also a menu toggle, key `f`): accounts that failed/were interrupted before (from `output/failed_accounts.txt`) go **first** in the queue on the next run, and if their funds are already on Hyperliquid (deposit went through, then something failed) the bot **skips Bitget+deposit and does withdraw+return only** — so stuck money is recovered fast, without an extra round-trip. An account is removed from `failed_accounts.txt` once it completes successfully;
- **network resilience** (automatic, base behaviour — no toggle): if Hyperliquid drops mid-cycle the bot does **not** abandon the account — it waits for the network to come back (pinging it) and retries the failed read-step (safe — it never repeats a send/deposit). If the network doesn't recover within `network_wait_max_min` (advanced, default 30 min, `0` = no limit) the run stops and the stuck account is retried first next time;
- `limit_orders` — `true` places perps/HIP-3 as **limit orders** (cheaper than market: posts at the mid, repegs, falls back to market only if it can't fill); spot is always limit. Also a menu toggle. `false` = market;
- `disable_dex_abstraction` — **default `true`** (needed for HIP-3): the bot switches the account to standard margin mode and funds HIP-3 directly (spot→DEX). Set `false` to leave the account's margin mode untouched — but then HIP-3 can fail with `Insufficient margin`;
- `proxy_bitget` — `false` (default) keeps the Bitget API on your main IP (its API uses an IP whitelist); `true` routes Bitget through the wallet's proxy too;
- `builder_codes` — **builder fee**: a small fixed fee (~0.01%) per Hyperliquid order goes to the project's builder address. **Enabled in the shipped config** — set `builder_codes: false` to turn it off. Only the on/off toggle is in the config; the address and rate are fixed in the code;
- **market-hours guard** (automatic, no config key): before opening a position the bot checks the L2 order book; if it's empty or the spread is too wide — a HIP-3 stock/commodity whose market is closed at night/on weekends — the asset is skipped for that round, so a market fallback never fills at a terrible price;
- `advanced` — wait timeouts and pauses between steps/wallets.

---

## Run

**Double-click `ЗАПУСТИТЬ.bat`** (or in a terminal: `python run.py`).

An arrow-key menu opens (the in-app menu is in Russian):
```
  wallets from wallets.xlsx: 5
  ► [✓] Withdraw ETH from Bitget
    [✓] Deposit to Hyperliquid (Unit)
    [✓] Trade / build volume
    [✓] Withdraw ETH from Hyperliquid (Unit)
    [✓] Return ETH to Bitget
    [✓] Shuffle wallet order
    [✓] Skip already-done (from output/done_accounts.txt)
    [ ] Limit orders (perps/HIP-3 as limit — cheaper than market)
    [✓] Resume failed accounts first (+ withdraw/return only)
        ▶ RUN over wallets.xlsx
        Exit
```
- **↑ / ↓** — move through the list.
- **Enter / Space** — toggle a module or press “Run”.
- **q** — exit. Checkboxes are saved automatically.
- “Run” executes the enabled steps **over all wallets** from `wallets.xlsx`, one by one.

### Linux / macOS
`ЗАПУСТИТЬ.bat` is Windows-only. On Linux/macOS set up and run with Python directly (file paths use `/` instead of `\`):
```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python run.py            # menu — arrow keys work here too
# or without the menu (handy on a server):
.venv/bin/python run.py --stage cycle
```
Over SSH the arrow-key menu works; in a non-interactive/piped run it automatically falls back to the text menu.

---

## Where to see results

The **`output\`** folder (created automatically):
- `runs_log.txt` — human-readable: what happened on each account (withdraw, deposit, volumes, fees);
- `runs.csv` / `accounts.csv` — the same for Excel;
- the key figure — **volume in Unit** (deposit + withdrawal) and total fees.

---

## Good to know

- **One Bitget account for all wallets** (keys in `.env`), while the EVM wallets come from `wallets.xlsx`.
- Each wallet address must be added to the **withdrawal whitelist** on Bitget. If an address isn't added — at the withdrawal step the script stops, tells you, and waits: add the address to the whitelist and press Enter to continue (or skip that wallet).
- The Unit bridge minimum is ~0.007 ETH. The first withdrawal to a new address costs ~1 USDC — the script keeps a reserve for it.
- Between steps the script waits for real credits (Bitget → wallet, Unit bridge) — that's normal, it can take a few minutes.

---

## What's in the folder (for reference)

- `config\` — settings. The repo ships **`*.example`** templates; copy each without `.example` and fill it in. Your real `.env` / `config.json` / `wallets.xlsx` stay local (git-ignored).
- `app\` — launch code and menu; `lib\` — operation modules (Bitget / Unit / Hyperliquid); `output\` — reports.
