# Indico Notifier

Collects CERN seminars and, with an Indico API token, your own agenda for the next 7 days. It sends a Telegram message plus an hour-by-hour PDF. Without Telegram, it prints the digest and can also post it to a Mattermost or Slack webhook.

The public series are set in `CATEGORIES` inside `notifier.py`.

To follow particular people, add `PI_list.csv` next to `notifier.py`:

```csv
region,university,name
EU,Example University,Ada Lovelace
```

If that person chairs a meeting, speaks, or is named in the agenda, the digest lists the meeting first under **PI this week**. The csv is gitignored. For the Sunday run, paste the whole file, header included, into a repository secret named `PI_LIST`.

## Setup

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

Fill in `.env`:

- `TELEGRAM_BOT_TOKEN` from [@BotFather](https://t.me/BotFather) (`/newbot`)
- `TELEGRAM_CHAT_ID` after you send `/start` to the bot, then run `python notifier.py --chat-id`
- `INDICO_API_TOKEN` from [indico.cern.ch/user/tokens](https://indico.cern.ch/user/tokens/), with **User information (read only)**, **Everything (only GET)**, and **Classic API (read only)**
- `WEBHOOK_URL` only if you also want Slack or Mattermost

Star an event or category on Indico to add it to your agenda. The next run picks up the next 7 days.

Titles in `TELEGRAM_SKIP_TITLES` inside `notifier.py` are left out of the Telegram text. They still appear in the PDF and in the Mattermost message.

`python notifier.py --remind` sends one short Telegram message for each remaining For you meeting that starts within 15 minutes. `.github/workflows/meeting_reminders.yml` runs that check every 15 minutes on weekdays.

## Run

```bash
python notifier.py
```

## GitHub Actions

`.github/workflows/weekly_events.yml` runs every Sunday at 07:00 UTC and can be started manually. GitHub may start it later the same day. Add `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`, and `INDICO_API_TOKEN` under **Settings → Secrets and variables → Actions**. 