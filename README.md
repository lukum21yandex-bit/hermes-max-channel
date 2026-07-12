# Max Platform Adapter for Hermes Agent

Gateway plugin connecting the Russian messenger [Max](https://max.ru) to Hermes Agent via the Max Platform API.

## Features

- **Text messaging** — full bidirectional text chat
- **Audio → STT** — voice messages automatically downloaded and transcribed through Hermes STT pipeline (faster-whisper, Groq, OpenAI, or Mistral)
- **Reply-to threading** — bot replies are linked to user messages; user replies carry original message context
- **Inline feedback buttons** — agent can attach 👍/👎 buttons via `FEEDBACK:` directive
- **Media support** — images, video, files (upload + send)
- **Typing indicators** — bot sends typing status while processing
- **Access control** — allow-list or open access

## Installation

Place the plugin in `~/.hermes/plugins/max-platform/` and configure in `config.yaml`:

```yaml
gateway:
  platforms:
    max:
      enabled: true
      extra:
        token: your_bot_token
        webhook_url: https://your.domain/plugins/max/webhook
        allowed_users: []
        allow_all_users: false
```

Or use environment variables:

| Variable | Required | Description |
|---|---|---|
| `MAX_TOKEN` | ✅ | Bot token from Max dev portal |
| `MAX_WEBHOOK_URL` | ✅ | Public HTTPS webhook URL |
| `MAX_ALLOWED_USERS` | Optional | Comma-separated user IDs |
| `MAX_ALLOW_ALL_USERS` | Optional | `1`/`true` to allow everyone |
| `MAX_HOME_CHANNEL` | Optional | Chat ID for cron delivery |
| `MAX_API_BASE_URL` | Optional | Override API base (default: `https://platform-api.max.ru`) |

## Directives

The agent can use special directives in message text:

### `MEDIA:` — Send images/media
```
Here's a chart:
MEDIA:https://example.com/chart.png
```
The adapter downloads the URL, uploads to Max, and attaches as media.

### `FEEDBACK:` — Attach feedback buttons
```
Task completed successfully!

FEEDBACK:
```
The adapter removes the directive from text and attaches inline keyboard with 👍/👎 buttons. When the user presses a button, a `message_callback` event is received and logged.

**Usage guidelines:**
- ✅ Use `FEEDBACK:` on final results, summaries, completed tasks
- ❌ Do NOT use on progress messages, tool outputs, intermediate steps

## Webhook Events

The adapter subscribes to:
- `message_created` — new messages from users
- `message_callback` — inline button presses

## API Migration

⚠️ **Before July 19, 2026**: migrate from `platform-api.max.ru` to `platform-api2.max.ru` and add Минцифры certificate.

## Version History

- **v2.1.0** — Inline feedback buttons (`FEEDBACK:` directive), `message_callback` handling, `SendResult` fix, `send_typing(metadata)` fix
- **v2.0.0** — Gateway plugin architecture, STT audio support, reply-to threading
- **v1.0.0** — Standalone bot (deprecated)

## Links

- [Max API Docs](https://dev.max.ru/docs-api)
- [Max Dev Portal](https://dev.max.ru)
- [Hermes Agent](https://hermes-agent.nousresearch.com)
