# Housing finder

Watches rental sites around Eindhoven (15-20 km) for **self-contained studios up to €1100** and sends each new one to Telegram with photo, price, size and **the landlord's or agent's own phone number**.

Runs on GitHub Actions every 5 to 15 minutes, so your Mac can be off.

## What counts

- Studios, plus apartments that are one room or at most 40 m² (studios under another name).
- No rooms in shared houses, no houses, no antikraak, no house swaps (ruilwoning.nl).

## Sites

| Site | Why |
|---|---|
| Pararius | Professional agents only, free to contact, biggest source |
| Huurwoningen | Same platform, extra listings copied from agent websites |
| 123Wonen | Agent network |
| ikwilhuren (MVGM) | Large landlord, register on their site |

Left out on purpose: Kamernet and Huurstunt (pay to react, private landlords, most scams), Rentola and Directwonen (paywalled copies). Not possible: Funda and Holland2Stay (bot checks), Vesteda and Interhouse (load by JavaScript).

## Landlord contact

- **Pararius:** the number in the listing's "contact the agent" box, plus the agent's own website.
- **Huurwoningen:** the landlord is hidden behind an account there, so the finder reads the agent's domain from the photo link and takes the phone number from that agent's own website.
- Portal numbers are never shown. No number found means: react via the listing.
- The same home on two sites is sent once (street + price), Pararius first because it has the number.

## Setup (once)

1. Make a bot with [@BotFather](https://t.me/BotFather) and copy its token. Send your bot any message, then open `https://api.telegram.org/bot<TOKEN>/getUpdates` and copy `chat.id`.
2. Add both as repo secrets:
   ```bash
   gh secret set TELEGRAM_TOKEN
   gh secret set TELEGRAM_CHAT_ID
   ```
3. Actions tab, workflow **check**, **Run workflow**. The first run remembers what is online now and sends one "live" message. After that you only get new listings.

## How it behaves

- **First run / lost memory:** listings already online are remembered silently, no flood.
- **A site breaks** (6 failed checks in a row): you get one ⚠️ message naming the site, and one ✅ when it works again.
- **Settings:** budget is `MAX_PRICE` (env, default 1100); the towns are the `AREA` list at the top of `main.py`.

## Run locally

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/python main.py --site Pararius          # see what one site returns
.venv/bin/python main.py --once --dry-run         # full check, sends nothing
TELEGRAM_TOKEN=... TELEGRAM_CHAT_ID=... .venv/bin/python main.py   # loop every 5-10 min
```
