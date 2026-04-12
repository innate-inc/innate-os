# Telegram Bot Setup for `retrieve_telegram` Skill

## 1. Create a Bot via BotFather

1. Open Telegram and search for **@BotFather** (verified blue checkmark)
2. Send `/newbot`
3. Choose a **display name** (e.g. `Maurice Robot`)
4. Choose a **username** — must end in `bot` (e.g. `maurice_innate_bot`)
5. BotFather replies with your **bot token**:
   ```
   Use this token to access the HTTP API:
   7123456789:AAF1x...your-token...kZx
   ```
6. Copy the token — you'll need it in the next step

## 2. Configure Environment

Add to your `.env` file (or set in your shell):

```bash
TELEGRAM_BOT_TOKEN=7123456789:AAFxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
```

The `.env.template` already has the placeholder. Copy it if you haven't:

```bash
cp .env.template .env
```

## 3. Optional: Customize the Bot

Still in the BotFather chat:

| Command | Purpose |
|---------|---------|
| `/setdescription` | What users see before starting a chat |
| `/setabouttext` | Short bio on the bot's profile |
| `/setuserpic` | Bot avatar (use the robot's photo) |
| `/setcommands` | Slash commands menu (not needed for this skill) |

## 4. Enable Group Messages (optional)

By default, bots in groups only see messages that start with `/` or mention the bot. To let the bot see **all** group messages:

1. Send `/mybots` to BotFather
2. Select your bot
3. **Bot Settings** → **Group Privacy** → **Turn off**

This is only needed if you want the robot to retrieve messages from group chats.

## 5. How the Skill Works

The `retrieve_telegram` skill calls the Telegram Bot API's [`getUpdates`](https://core.telegram.org/bots/api#getupdates) endpoint to fetch recent messages sent to the bot.

**What it retrieves:**
- Text messages sent directly to the bot (DMs)
- Text messages in groups where the bot is a member (if group privacy is off)

**What it does NOT retrieve:**
- Your personal chat history (that requires the Client API / Telethon)
- Messages older than 24 hours that haven't been fetched
- Media-only messages (images, voice, etc.) — only text content is extracted

## 6. Usage

The brain can invoke this skill with:

```
retrieve_telegram(count=5)
```

- `count`: number of recent messages to return (1–20, default 5)
- Returns: sender name, chat name, timestamp, and message text

## 7. Testing Manually

Verify your token works:

```bash
curl "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/getMe"
```

Expected response:

```json
{"ok": true, "result": {"id": 7123456789, "is_bot": true, "first_name": "Maurice Robot", ...}}
```

Then send a message to your bot on Telegram and check:

```bash
curl "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/getUpdates"
```

You should see your message in the response.

## Environment Variables Reference

| Variable | Required | Description |
|----------|----------|-------------|
| `TELEGRAM_BOT_TOKEN` | Yes | Bot API token from @BotFather |
