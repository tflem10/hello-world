# Schwab developer account and API setup

You do not need any of this to start. The system builds its cache, runs its
backtests and sends you picks using free data. Schwab is needed only for
real-time quotes at the pre-open confirm step and for v2 auto-execution.

Expect **days**, not minutes: the app approval step is a manual review on
Schwab's side, and it does not tell you much while it is happening.

---

## 0. What you need first

- A funded Schwab **brokerage** account (the retail Trader API works on
  ordinary retail accounts; you do not need an institutional relationship).
- Roughly 20 minutes of setup, then a wait for approval.

---

## 1. Create the developer account

1. Go to <https://developer.schwab.com> and select **Register**.
2. Register as an **Individual Developer**. Use the same email as your
   brokerage login where possible — it makes the account-linking step later
   less confusing.
3. Verify the email and complete the profile.

The developer portal account is separate from your brokerage login. Having both
is normal.

---

## 2. Create the app

1. In the portal, go to **Dashboard → Apps → Create App**.
2. **App name**: anything, e.g. `swing-picks`.
3. **API product** — select **both**:
   - **Accounts and Trading Production** (needed for orders and positions)
   - **Market Data Production** (needed for quotes and price history)

   Selecting only one is the single most common setup mistake, and the symptom
   is confusing: login succeeds, then quotes 401 while accounts work, or the
   reverse.
4. **Callback URL** — enter exactly:

   ```
   https://127.0.0.1:8182
   ```

   This must match `[schwab] callback_url` in your `config.toml`
   **character for character**: `https` not `http`, `127.0.0.1` not
   `localhost`, no trailing slash. Schwab compares it as a literal string.
   Nothing actually listens on that port permanently — schwab-py stands up a
   temporary local server during the login flow and shuts it down after.
5. **Order limit**: the default is fine. This system places at most
   `[execution] max_orders_per_day` orders (3 by default).
6. Submit.

---

## 3. Wait for approval

The app moves through statuses:

| status | meaning |
|---|---|
| `Approved - Pending` | Schwab is reviewing. **The API will reject logins.** |
| `Ready For Use` | live. This is the one you are waiting for. |
| `Rejected` | reason is shown in the portal |

This typically takes a few days. **`Approved - Pending` is not approved** —
the wording is genuinely misleading, and trying to authenticate during this
stage produces an unhelpful error. Wait for `Ready For Use`.

While waiting, everything else works:

```bash
make install
swing data --backfill          # free daily data via yfinance
swing backtest --walk-forward  # validate the strategy
swing notify-test              # prove your alerts work
swing scan --dry-run           # produce a pick sheet, send nothing
```

---

## 4. Put the keys in your config

From the app's page in the portal, copy the **App Key** and **Secret**.

```toml
[schwab]
api_key = "YOUR_APP_KEY"
app_secret = "YOUR_APP_SECRET"
callback_url = "https://127.0.0.1:8182"
token_path = "~/.swing/schwab_token.json"
account_hash = ""            # filled in at step 6
token_warn_days = 6
```

Then lock the file down. It now contains a live trading credential:

```bash
chmod 600 config.toml
```

`config.toml` is gitignored. Do not paste these keys into a chat window, an
issue, or a commit. If you ever do, regenerate the secret in the portal
immediately — that invalidates the old one.

---

## 5. Install the optional dependency and log in

```bash
uv pip install --python .venv/bin/python -e '.[schwab]'
swing auth
```

A browser opens to Schwab's login. Sign in, approve the account(s) you want the
app to reach, and you will be redirected to `https://127.0.0.1:8182/...`.

**Your browser will warn about the certificate.** That is expected: schwab-py
serves the local callback over a self-signed certificate. Proceed past the
warning (Advanced → Proceed). Nothing leaves your machine at that step; the
redirect is localhost-to-localhost.

On success the token is written to `~/.swing/schwab_token.json` with mode 600.

---

## 6. Verify, and get your account hash

```bash
swing auth --check
```

Expected output:

```
Schwab token: 0.0 days old, 7.0 day(s) left

accounts:
  ...1234   hash A1B2C3D4E5F6...

[schwab] account_hash is empty. Copy the hash above into your config —
`swing execute` needs it to know which account to trade.

sample quote: SPY 512.34  (bid 512.33 / ask 512.35)
```

Copy the hash into `[schwab] account_hash` and re-run `swing auth --check`; it
should end with `Schwab connection looks healthy.`

The hash — not the account number — is what the API uses to identify an
account. It is not secret in the way the app secret is, but there is no reason
to publish it either.

---

## 7. Switch the data provider (optional)

```toml
[data]
provider = "schwab"
```

Now price history and quotes come from Schwab, with **automatic fallback to
yfinance** whenever the token is expired or a request fails. Earnings dates
always come from yfinance — the Trader API has no earnings calendar.

Leaving `provider = "yfinance"` is a perfectly reasonable permanent choice.
The scan only really benefits from Schwab at the pre-open confirm step, where
a real-time bid/ask beats a 15-minute-delayed print.

---

## 8. The weekly re-authentication

**The refresh token expires after 7 days and cannot be renewed
programmatically.** This is Schwab's design for retail OAuth, not a limitation
of this code, and there is no supported way around it.

So, once a week:

```bash
swing auth
```

The system helps you not forget:

- the nightly scan warns from day `token_warn_days` (6 by default),
- `swing auth --check` prints the exact remaining life,
- when the token does expire, data requests fall back to yfinance and the pick
  sheet carries a warning rather than the scan failing,
- but **`swing execute` refuses to run with an expired token.** There is no
  fallback for placing orders, and there should not be.

Pick a weekly habit — Sunday evening pairs well with reviewing the week's
picks.

---

## Troubleshooting

**`login failed` immediately, before the browser opens**
Credentials are missing or malformed. Run `swing auth --check` and re-read
step 4.

**Browser opens, login succeeds, then an error page**
Almost always a callback mismatch. The portal value and `callback_url` must be
byte-identical. Also confirm the app reads `Ready For Use`.

**`401` on quotes but accounts work (or vice versa)**
The app is missing one of the two API products. Edit the app in the portal to
add the missing one; this re-triggers approval.

**`Approved - Pending` for over a week**
Contact Schwab API support through the portal. There is nothing to fix on your
side.

**Orders rejected with a schema error**
Schwab's order schema has changed before. Run `swing execute` without `--live`
to print the exact JSON that would be sent, compare against the current
schwab-py documentation, and fix the field names in `src/swing/orders.py`. The
built-in validator catches structural mistakes but cannot know that Schwab
renamed a field.

**Everything worked, then stopped after a week**
The refresh token expired. `swing auth`. See step 8.

---

## Security notes

- `config.toml` holds your app secret and, if you use email alerts, an SMTP
  password. `chmod 600`, and it is gitignored.
- `~/.swing/schwab_token.json` is a live credential. It is written mode 600.
  Anyone who copies it can trade your account until it expires.
- `~/.swing/` should be `chmod 700` (the installer does this).
- The kill switch (`swing kill`) blocks all order placement immediately and
  does not need the API to be reachable. Use it first and ask questions after.
- Never paste tokens, app secrets or account hashes into an issue, a chat, or
  a commit. If you do, regenerate the secret in the portal.
