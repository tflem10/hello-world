# Connecting `swing` to Schwab

You do **not** need this to use `swing`. Scans, backtests and alerts all run on
free yfinance data. Schwab gets you two extra things: real-time quotes, and the
ability to send orders with `swing execute --live`.

Budget about 20 minutes of work and then **several days of waiting** — Schwab
approves new developer apps by hand, and there is no way to hurry it.

---

## Before you start

You need a Schwab **brokerage** account that is open and funded. A Schwab
retirement-only login, a Schwab Bank login, or an account still in "pending
approval" will not work: the developer portal has nothing to attach the app to.

Everything below is done once. After that the only recurring chore is a weekly
`swing auth`, described in [Living with the seven-day token](#living-with-the-seven-day-token).

---

## Step 1 — Create a developer account

1. Go to <https://developer.schwab.com> and select **Register**.
2. Register as an **individual developer**. There is no fee and no company
   details are required. Use the same email as your brokerage login if you can;
   it makes the approval step less likely to stall.
3. Confirm the email Schwab sends you and sign in to the developer portal.

The developer account is separate from your Schwab.com brokerage login. Having
one does not give anything access to the other until you complete Step 4.

---

## Step 2 — Create an app

From the developer dashboard choose **Create App** and fill it in:

| Field | What to put |
| --- | --- |
| App name | Anything, e.g. `swing-personal`. Only you see it. |
| Description | Anything, e.g. `Personal swing-trade scanner`. |
| Order limit | Leave the default (120/minute is far more than this tool uses). |
| **API products** | Tick **Accounts and Trading Production** *and* **Market Data Production**. |
| **Callback URL** | `https://127.0.0.1:8182` |

Two of those rows decide whether this works at all.

**API products.** You need both. "Accounts and Trading" lets `swing` read
balances and place orders; "Market Data" is what serves quotes. If you only
tick the first, `swing auth --check` will report the account fine and then fail
on the SPY quote — that specific combination almost always means the market
data product is missing or still pending.

**Callback URL.** It must be exactly:

```
https://127.0.0.1:8182
```

Character for character. `https`, not `http`. `127.0.0.1`, not `localhost`. No
trailing slash. No path. Schwab compares this string to the one `swing` sends
during login, and a mismatch of a single character produces an unhelpful
generic error at the very end of the flow. If you want a different port, change
it in **both** places — here and `schwab.callback_url` in your `config.toml`.

---

## Step 3 — Wait for "Ready for use"

A new app starts in **Approved - Pending** and has to reach **Ready for use**
before it will authenticate anything.

This takes **days, not hours** — typically two to five business days, sometimes
longer. There is no queue position and no way to expedite it. Check the app's
status on the developer dashboard occasionally; Schwab does not always send an
email when it flips.

Trying to log in before then fails with an error that looks like a credential
problem, which sends people off changing keys that were fine all along. If
`swing auth` fails, check the app status **first**.

---

## Step 4 — Put the keys in your config

On the app's page in the developer portal, reveal the **App Key** and
**Secret**. Copy them into your `config.toml`:

```toml
[schwab]
api_key = "PASTE-YOUR-APP-KEY-HERE"
app_secret = "PASTE-YOUR-SECRET-HERE"
callback_url = "https://127.0.0.1:8182"
token_path = "~/.swing/schwab_token.json"
account_index = 0
```

If you do not have a `config.toml` yet:

```bash
cp config.example.toml config.toml
```

`config.toml` is in `.gitignore` and must stay there. Those two strings can
place trades in your account — treat them exactly like your brokerage password.
Do not paste them into an issue, a chat, or a commit.

`account_index` picks which linked account to trade when your login can see
several, counting from zero. Leave it at `0` unless `swing auth --check` shows
the wrong account.

---

## Step 5 — Log in

```bash
uv run swing auth
```

This opens a browser and hands you to Schwab's own login page. `swing` never
sees your Schwab username or password — they go to Schwab, and what comes back
is a token.

What happens, in order:

1. A browser window opens on Schwab's login page. Sign in normally.
2. Schwab asks which accounts to expose. Tick the one you intend to trade.
3. Schwab redirects to `https://127.0.0.1:8182`.
4. **Your browser will warn that the certificate is not trusted.** This is
   expected and it is not a man-in-the-middle. `swing` is running a tiny local
   web server to catch the redirect, with a self-signed certificate; nothing
   leaves your Mac. Choose **Advanced → Proceed**.
5. The page will look blank or broken. That is also fine — the token has
   already been captured by then.

On success the token is written to `~/.swing/schwab_token.json` and `chmod`ed to
`600` (readable only by you). That file is as sensitive as the app secret: it
*is* the ability to trade your account until it expires. It is covered by
`.gitignore`; keep it that way, and do not sync it to a shared folder.

The flow gives up after five minutes if you leave the browser sitting there.
Just run `swing auth` again.

---

## Step 6 — Check it

```bash
uv run swing auth --check
```

A healthy result looks like this:

```
Token file : /Users/you/.swing/schwab_token.json
Token age  : 0.0 days
Status     : The Schwab token is 0.0 days old; 7.0 days left before it must be renewed.
Account    : ****6789 (hash A1B2C3...)
SPY quote  : 512.34
```

Four facts, and each one tells you a different thing works: the token exists
and is young, the credentials load it, the account is visible, and market data
is flowing. `--check` exits non-zero if any of that fails, so it is safe to use
in a script.

---

## Living with the seven-day token

**Schwab refresh tokens expire seven days after they are created.** Not seven
days after last use — seven days after creation. Using the tool daily does not
extend it. This is a Schwab policy and no amount of clever code gets around it.

So the ritual is: **run `swing auth` once a week.** Sunday evening works well,
since it puts a fresh token in front of a full trading week.

The system nags rather than surprising you:

| Token age | What happens |
| --- | --- |
| 0–6 days | Everything works silently. |
| 6–7 days | The nightly scan and every guardrail print `re-auth soon: run swing auth`, but trading still works. |
| 7 days + | The `token_age` guardrail refuses. `swing execute` sends nothing until you log in again. |

Day six is deliberately a warning rather than a failure, so a token dying
mid-week never comes as a surprise on a morning you wanted to trade.

### When the token dies, nothing else breaks

A dead token degrades the system, it does not stop it:

- **Scans keep running.** The data layer falls back to yfinance, which is the
  default provider anyway. Prices are delayed rather than real-time, which
  matters not at all for a nightly scan of daily bars.
- **Backtests are unaffected.** They only ever use cached historical bars.
- **Alerts keep arriving.** ntfy, email and macOS notifications know nothing
  about Schwab.
- **Only live execution stops**, which is exactly the behaviour you want from
  an expired credential.

So a forgotten `swing auth` costs you automated order placement for a day. It
does not cost you the picks.

---

## Symbols with a class letter (`BRK-B`, `BF-B`)

Schwab writes class shares with a slash: `BRK/B`, not `BRK-B`. `swing` translates automatically —
`swing.universe.to_schwab_symbol` rewrites the exact `ROOT-LETTER` form to `ROOT/LETTER` on the way
into every Schwab request, and the results are mapped back, so nothing outside the Schwab provider
ever sees a slash. Everything else (plain tickers, and anything with a multi-character suffix) is
passed through unchanged.

**Verify it against a live quote the first time you use Schwab.** This mapping is derived from
Schwab's documented convention, not from a response we have replayed for every symbol in the
universe. There is no `swing quote` command, so the check is a one-liner (with
`data.provider = "schwab"` in your config and a fresh token):

```bash
uv run python -c "
from swing.config import load_config
from swing.data import get_provider
print(get_provider(load_config()).latest_quotes(['BRK-B', 'SPY']))
"
```

Two symbols, so you can tell "Schwab is not answering" from "Schwab does not know that symbol": a
symbol it cannot price is **omitted** from the result and logged, rather than coming back wrong. On
the bars side, a fetch where several symbols return nothing logs `N of M symbols returned no history
from Schwab.` — worth watching the first time you run a scan on the Schwab provider.

---

## What `swing execute` writes to the journal

Worth reading once before the first live run, because it decides what you will see if something goes
wrong mid-flight.

| Step | Journal state |
|------|---------------|
| Before the order is sent | a row is written with status **`pending`**, a `client_ref` (`swing-<timestamp>-<SYMBOL>-<8 hex>`), the quantity, limit, stop and the exact payload |
| Schwab accepted it | the same row flips to **`open`** and gains the broker's `order_id`; the pick becomes `ordered` |
| Schwab refused it | the run stops with a non-zero exit; nothing further is sent |
| Something failed *after* Schwab accepted it | the row is annotated **`unknown`** with the error, and the run prints a warning that begins `THE ORDER MAY BE LIVE` |

The order is journalled **before** it is sent on purpose: an order that nothing can record is an
order nothing can track, so if that first write fails the run refuses to send anything at all.

**The "may be live" warning is never "not sent".** It means Schwab took the order and then the id
could not be read, or the journal could not be updated. Check the Schwab app before doing anything
else, and cancel there if you did not want the order.

**Fills are synced from Schwab, not assumed.** At the start of every `swing execute --live` and every
`swing positions` with a working connection, `swing` fetches your account's orders and reconciles
them: `FILLED`, `CANCELED`, `REJECTED`, `EXPIRED` and `REPLACED` move the journal row to its terminal
state, and a fill additionally moves the pick to `filled`. Anything still working at the broker is
left alone. A **dry run does none of this** — it makes no broker connection at all.

Two behaviours inside the placement loop are worth knowing:

- **The kill switch and the trading-hours window are re-checked before every single order**, not
  once at the start. Creating `~/.swing/KILL` (or running `swing kill` from another terminal) part
  way through a batch stops the orders that have not been sent yet; the run says which symbol it
  stopped at and that the rest were not sent.
- **The duplicate guardrail does not block the scan you are executing.** It refuses a symbol that
  already has an `open` order or a filled position in the journal, or that was picked again inside
  the five-day cooling-off window — but every pick dated with *this* bundle's scan date is exempt by
  identity, so executing tonight's scan is never mistaken for re-entering a name you just picked
  (audit BUG-006).

### A stuck `unknown` row, or an `open` row Schwab has never heard of

This is the one place the system currently needs your hands. Symptom: `swing positions` keeps
reporting a reconciliation difference such as *"working in the journal but not at Schwab: SYM"*, and
it does not clear.

It happens because fill sync matches journal rows to broker orders **by `order_id`**, and a `pending`
or `unknown` row never got one. There is no `swing` command that edits an order's status — `swing
kill` only moves rows that are already `open` and that Schwab confirmed cancelled.

The honest procedure, in order:

1. **Look in the Schwab app first.** Is there a working order for that symbol, or a fill? Schwab is
   the authority; the journal is only a record.
2. If the order **is live** and you do not want it, cancel it in the Schwab app (or run
   `swing kill`, which cancels working orders and engages the kill switch).
3. Once Schwab and reality agree, **edit `~/.swing/journal.json` by hand.** Quit any running `swing`
   process first — the journal is written under an advisory lock and hand-editing behind a live
   process can lose the edit. Find the object in `"orders"` whose `client_ref` matches the one
   printed at placement time and set its `"status"` to what actually happened: `"cancelled"`,
   `"rejected"`, or `"filled"`. If it filled, also set that symbol's pick `"status"` to `"filled"` so
   `swing positions` agrees.
4. Re-run `swing positions` and confirm the reconciliation line is clean.

Keep a copy of the file before editing it. A malformed journal is moved aside as
`journal.corrupt.<pid>-<timestamp>.json` and replaced with an empty one, and until you restore it
`swing` believes it holds no positions and has sent no orders.

---

## What the errors look like

| What you see | What it actually means |
| --- | --- |
| `Schwab api_key and app_secret are empty…` | Step 4 not done, or `swing` is reading a different `config.toml` than you edited. Run `swing --config ./config.toml auth` to be sure. |
| Login fails immediately, generic error | The app is still **Approved - Pending**. Check the dashboard; wait it out. |
| Login completes but ends in an error page | The callback URL does not match byte-for-byte. Compare the portal against `schwab.callback_url`. |
| Browser warns about the certificate | Expected. Advanced → Proceed. See Step 5. |
| `The Schwab token … could not be loaded` | The token file is corrupt or was written by a much older schwab-py. Delete `~/.swing/schwab_token.json` and run `swing auth`. |
| `…is 7.4 days old and refresh tokens die after 7 days` | Normal weekly expiry. Run `swing auth`. |
| Account shows, `SPY quote` fails | The Market Data product is missing from the app or still pending. Step 2. |
| `Schwab answered with HTTP 401` | The token expired mid-run. Run `swing auth`. |
| `schwab.account_index is 1 but this token can only see 1 account` | You ticked fewer accounts than expected during login, or `account_index` is wrong. |

---

## Security notes

- **Never commit `config.toml` or `schwab_token.json`.** Both are in
  `.gitignore` already. Verify with `git status --ignored` if you have any
  doubt after editing them.
- **The token file is a trading credential.** `swing auth` sets it to `chmod
  600`. Do not put it in Dropbox, iCloud Drive, or a shared machine's home
  directory.
- **Rotate if exposed.** If a key or secret ever lands somewhere public,
  regenerate the secret in the developer portal immediately; that invalidates
  every token derived from it.
- **`swing` never stores your Schwab password.** The OAuth flow means it never
  sees it in the first place.
- **Execution stays off until you turn it on twice.** `execution.enabled` in
  config and `--live` on the command line are independent switches, and
  `swing kill` engages a kill-switch file that blocks everything regardless.
  Watch dry runs for a while before trusting either.

---

## Turning execution on (when you are ready)

Authentication alone sends no orders. To go live you must also:

```toml
[execution]
enabled = true            # switch one
```

and pass `--live` (switch two):

```bash
uv run swing execute --live
```

Until both are set, `swing execute` prints what it *would* do and stops. That
is the intended way to run it for the first few weeks.

### What a dry run does and does not exercise

A dry run is **fully offline**: no client is built, no account is fetched, no orders are listed, and
no quote is requested. That is deliberate — a "dry run" that opened a broker connection was a
sentence in a docstring nobody could trust (audit DEBT-003).

The consequence is that three checks cannot run, and say so rather than pretending to pass. They
print the verdict `SKIP`:

| Check | What a dry run prints |
|-------|-----------------------|
| `quote_drift` | Not checked in a dry run: comparing the symbol against the scan's entry price needs a live quote. |
| `reconciliation` | Not checked in a dry run: comparing Schwab's positions and working orders with the journal needs a live connection. |
| `equity_mismatch` | Not checked in a dry run: comparing config equity with the account balance needs a live connection. |

Everything offline — the kill switch, trading hours, token age, the daily order cap, new-exposure
budget, duplicate detection, the backtest gate, the structural validity of each order payload — does
run, and a dry run never raises. So read a clean dry run as "the plan is well formed and nothing
local objects", not as "every guardrail passed".

### Before the first live order: a rehearsal checklist

Three things this system does at the Schwab boundary have been built from Schwab's documentation
rather than verified against a live account. None of them is exotic; all of them are worth checking
once, on a day you are watching, with one small order.

1. **Symbol translation.** Quote a class share (`BRK-B` → `BRK/B`) and confirm you get a price. See
   the symbology section above.
2. **The order-history window.** Fill sync calls `get_orders_for_account` with **no date range**, so
   it gets Schwab's default window — documented as the last 60 days. An order older than that window
   will not come back, so its journal row will never be reconciled automatically. In normal use
   nothing lives that long; if you resume after a break, expect to settle old rows by hand.
3. **The terminal-status vocabulary.** `swing` treats `FILLED`, `CANCELED`, `REJECTED`, `EXPIRED` and
   `REPLACED` as final, and leaves everything else (`WORKING`, `QUEUED`, `PENDING_ACTIVATION`,
   `ACCEPTED`, …) alone. That list comes from Schwab's documented status enum and has **not** been
   confirmed against live responses for every case. The failure mode is benign and visible — an
   unrecognised terminal status leaves a journal row `open` and shows up as a reconciliation
   difference in `swing positions` — but it is the thing to watch on your first fill and your first
   cancel.

Rehearse in this order: a dry run, then one live order for the smallest position the sizing will
produce, then `swing positions` to see the fill sync move the row, then `swing kill` to prove the
cancel path.

To stop everything at once, at any time:

```bash
uv run swing kill          # engage:  blocks execution, cancels working orders
uv run swing kill --off    # release
```
