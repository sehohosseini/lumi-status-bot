# LUMI status → Telegram

Sends you a Telegram message when LUMI goes down and when it starts working again, based on
the official status API at <https://status.lumi.csc.fi>. It runs on GitHub Actions every
5 minutes, so it works while your Mac is asleep, and it isn't affected by Aalto's network
blocking the Telegram API.

## Setup

1. **Create the bot** (Telegram on your phone): message **@BotFather**, send `/newbot`, pick a
   name and a username ending in `bot`, and copy the token it gives you.
2. **Create an empty public repo** at <https://github.com/new>, e.g. `lumi-status-bot`, without
   a README. A public repo gets free Actions minutes, and the secrets stay encrypted.
3. **Push this folder** (from inside it):
   ```bash
   git init -b main && git add . && git commit -m "LUMI status Telegram bot"
   ```
   ```bash
   git remote add origin https://github.com/YOUR-USERNAME/lumi-status-bot.git && git push -u origin main
   ```
4. **Add the token**: repo → *Settings* → *Secrets and variables* → *Actions* →
   *New repository secret*. Name `TELEGRAM_BOT_TOKEN`, value = the token.
5. **Get your chat id**: send your bot any message in Telegram. Then repo → *Actions* →
   *LUMI status* → *Run workflow*, choose mode `chat-id`. The bot replies with your chat id. Add
   it as a second secret named `TELEGRAM_CHAT_ID`.
6. **Test**: *Run workflow* again with mode `test`. You should get LUMI's current status.

From then on the schedule runs by itself. The next scheduled run sends "Monitoring started",
and after that you only hear from the bot when LUMI's status changes.

## What counts as "LUMI working"

By default, **Login Nodes**, **LUMI-C** and **LUMI-G** must all be Operational. To change that,
add a repository *variable* (same settings page, *Variables* tab) named `LUMI_WATCH`, e.g.
`login_nodes,lumi_c,lumi_g,lumi_d`. The ids are listed at <https://status.lumi.csc.fi/api/status>.

A change has to show on 2 checks in a row before you get a message, so brief flips don't
spam you. If the status page itself can't be reached for 3 checks, you get a warning
instead of a false "down".

## Good to know

- GitHub's schedule is best-effort, so runs can start several minutes late at busy times.
- In a public repo, anyone can read the run logs. They show only LUMI component statuses,
  and GitHub masks the secrets.
- GitHub pauses schedules in public repos after 60 days without commits. To prevent this, the
  workflow makes an empty "keepalive" commit when it's needed.
- Private repo instead? Change the cron to `*/30 * * * *` to stay inside the free
  2,000 minutes a month, since each run bills as 1 minute.

## Running on a Mac instead

`./install_macos.sh` installs the same bot as a launchd job, checking every 5 min. It can only
reach Telegram when the Mac is off Aalto's network and VPN. `./install_macos.sh uninstall`
removes it.
