# Max (max.ru) — Hermes Agent Gateway Plugin

Gateway-адаптер для российского мессенджера **Max** (max.ru). 
Подключает Max к Hermes Agent через Platform API — вебхук для входящих, REST API для исходящих.

## Возможности

- 💬 **Текстовые сообщения** — DM и групповые чаты
- 🖼️ **Изображения** — получение и отправка
- 🎤 **Голосовые сообщения** — автоматическая транскрипция через STT-пайплайн Hermes (faster-whisper, Groq, OpenAI, Mistral)
- 📹 **Видео** — получение вложений
- 📎 **Файлы** — любые вложения
- ⌨️ **Typing-индикаторы** — бот показывает «печатает…»
- 🔐 **Access control** — белый список пользователей или открытый доступ
- 🔗 **Webhook mode** — надёжная доставка через HTTPS

## Установка

Скопируйте плагин в директорию плагинов Hermes:

```bash
cp -r . ~/.hermes/plugins/platforms/max/
```

## Настройка

### Переменные окружения (`~/.hermes/.env`)

| Переменная | Обязательно | Описание |
|---|---|---|
| `MAX_TOKEN` | ✅ | Токен бота из https://max.ru (настройки бота) |
| `MAX_WEBHOOK_URL` | ✅ | Публичный HTTPS URL для вебхука (напр. `https://your.domain/plugins/max/webhook`) |
| `MAX_ALLOWED_USERS` | ❌ | Разрешённые user ID через запятую |
| `MAX_ALLOW_ALL_USERS` | ❌ | `1`/`true` — разрешить всем |
| `MAX_HOME_CHANNEL` | ❌ | Chat ID для cron/уведомлений |
| `MAX_API_BASE_URL` | ❌ | API base URL (по умолчанию `https://platform-api.max.ru`) |

### Caddy reverse proxy

```caddy
your.domain {
    reverse_proxy /plugins/* 127.0.0.1:9642
}
```

### STT (распознавание голосовых)

Распознавание работает автоматически через настроенный в Hermes STT-провайдер:

```yaml
# ~/.hermes/config.yaml
stt:
  enabled: true
  provider: local        # local, groq, openai, mistral
  local:
    model: base          # tiny, base, small, medium, large-v3
```

## Запуск

```bash
hermes gateway install   # установить как systemd-сервис
hermes gateway start     # запустить
hermes gateway status    # проверить статус
```

## Архитектура

```
Max Platform API
    ↕ (webhook)
Caddy (HTTPS) → 127.0.0.1:9642
    ↓
Max Adapter (adapter.py)
    ↓ download audio → cache_audio_from_url()
MessageEvent(media_urls, media_types)
    ↓
Hermes Gateway (run.py)
    ↓ _enrich_message_with_transcription()
STT Pipeline (faster-whisper / Groq / OpenAI)
    ↓
Agent → Response → Max REST API → User
```

## Требования

- Hermes Agent v2.0+
- Python 3.11+
- aiohttp
- ffmpeg (для audio-конвертации)
- faster-whisper (для local STT) или API-ключ для облачного STT

## Лицензия

MIT

## Автор

Aleksandr P
