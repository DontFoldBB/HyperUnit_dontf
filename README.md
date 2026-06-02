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

- **Windows** and **Python 3.10+**. When installing Python, tick *“Add Python to PATH”*.
  Check: open a terminal (PowerShell) and run `python --version` — it should print a version.
- A **Bitget account** with an API key: **Spot** + **Withdraw** permissions, and your IP added to the key's **whitelist**.
- **Private keys** of your wallets.

---

## Setup — step by step

### Step 1. Install dependencies
Open a terminal (PowerShell) **in the project folder** and run:
```powershell
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
```
This creates a local `.venv` — `ЗАПУСТИТЬ.bat` will find it automatically.

### Step 2. Bitget keys — file `config\.env`
The **`config\.env`** file is already in the folder — open it in a text editor and fill in your keys (no quotes, no spaces around `=`):
```
BITGET_API_KEY=your_key
BITGET_API_SECRET=your_secret
BITGET_API_PASSPHRASE=your_passphrase
ETH_RPC_URL=            ← can be left empty (public nodes will be used)
```

### Step 3. Wallets — file `config\wallets.xlsx`
Open **`config\wallets.xlsx`** in Excel and fill in:
- **column A** — wallet private key (`0x...`);
- **column B** — the ETH deposit address on Bitget (where funds return at the end of the loop).
- **One row = one account.** As many rows as accounts to run, one after another. The header row can stay — it's skipped.

### Step 4. Parameters — file `config\config.json` (the most important)
Open **`config\config.json`** and tune it — this is where you set **what and how much to do**. Every line is documented inside the file; `//` and `#` comments are allowed.

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
- `advanced` — wait timeouts and pauses between steps/wallets.

---

## Run

**Double-click `ЗАПУСТИТЬ.bat`** (or in a terminal: `python run.py`).

An arrow-key menu opens (the in-app menu is in Russian):
```
  wallets from wallets.xlsx
  ► [✓] Withdraw ETH from Bitget
    [✓] Deposit to Hyperliquid (Unit)
    [✓] Trade / build volume
    [✓] Withdraw ETH from Hyperliquid (Unit)
    [✓] Return ETH to Bitget
        ▶ RUN over wallets.xlsx
        Exit
```
- **↑ / ↓** — move through the list.
- **Enter / Space** — toggle a module or press “Run”.
- **q** — exit. Checkboxes are saved automatically.
- “Run” executes the enabled steps **over all wallets** from `wallets.xlsx`, one by one.

---

## Where to see results

The **`output\`** folder (created automatically):
- `runs_log.txt` — human-readable: what happened on each account (withdraw, deposit, volumes, fees);
- `runs.csv` / `accounts.csv` — the same for Excel;
- the key figure — **volume run through Unit** (deposit + withdrawal) and total fees.

---

## Good to know

- **One Bitget account for all wallets** (keys in `.env`), while the EVM wallets come from `wallets.xlsx`.
- Each wallet address must be added to the **withdrawal whitelist** on Bitget. If an address isn't added — at the withdrawal step the script stops, tells you, and waits: add the address to the whitelist and press Enter to continue (or skip that wallet).
- The Unit bridge minimum is ~0.007 ETH. The first withdrawal to a new address costs ~1 USDC — the script keeps a reserve for it.
- Between steps the script waits for real credits (Bitget → wallet, Unit bridge) — that's normal, it can take a few minutes.

---

## What's in the folder (for reference)

- `config\` — settings (`.env`, `config.json`, `wallets.xlsx`) — fill them in right here. They're empty in the repository; your filled-in values don't go to git.
- `app\` — launch code and menu; `lib\` — operation modules (Bitget / Unit / Hyperliquid); `output\` — reports.
