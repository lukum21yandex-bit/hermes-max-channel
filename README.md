# Hermes MAX Channel

Бот для платформы **MAX.ru** (не Telegram!).  
Работает в режиме longpoll (разработка) → webhook (VPS).

## Быстрый старт

```bash
# 1. Клонировать репозиторий (уже есть)
cd hermes-max-channel

# 2. Установить зависимости
pip install -r deploy/requirements.txt

# 3. Создать .env из примера и вставить токен
cp .env.example .env
# отредактируйте .env: MAX_TOKEN=ваш_токен_от_MasterBot

# 4. Запустить бота
python -m src.main
```

## Получение токена

1. Откройте MAX, найдите бота **@MasterBot**.
2. Начните диалог, следуйте инструкциям для создания бота.
3. После создания бот пришлёт вам **API токен**.
4. Вставьте токен в `.env` → `MAX_TOKEN=...`

## Конфигурация `.env`

| Переменная | Описание | Обязательно |
|-----------|---------|-------------|
| `MAX_TOKEN` | Токен бота от @MasterBot | да |
| `ALLOWED_USER_IDS` | Список user_id через запятую (только они могут писать боту) | нет |
| `LOG_LEVEL` | Уровень логов (`INFO`/`DEBUG`) | нет |

## Архитектура кода

```
src/
├── models.py      # Dataclasses: User, Message, Update, ...
├── client.py      # HTTP client с rate limiting (2 RPS)
├── bot.py         # Основной цикл longpoll, диспетчер хендлеров
├── main.py        # Точка входа, загрузка .env
└── handlers/      # ← сюда буду добавлять модули с бизнес-логикой
```

### Реализовано

- Longpoll polling (`GET /updates`) с marker-пагинацией
- Rate limiting: ≤2 RPS (требование MAX API с 11.05.2026)
- Автоматические retry (3 попытки, экспоненциальный backoff)
- Парсинг всех типов update (`message_created`, `bot_started`, …)
- Отправка сообщений (`POST /messages`) по `user_id` или `chat_id`

### Планы (постепенно)

- [ ] Отправка картинок (upload → attach)
- [ ] Голосовые/аудио (upload, transcript?)
- [ ] Inline-кнопки (keyboard)
- [ ] Режим webhook на VPS (легко, после теста longpoll)

## Развёртывание на VPS

1. Установить на сервер (Ubuntu/Debian):
   ```bash
   apt update && apt install -y python3 python3-pip
   git clone <repo_url> /opt/hermes-max-channel
   cd /opt/hermes-max-channel
   pip install -r deploy/requirements.txt
   ```

2. Настроить `.env` с `MAX_TOKEN` и `WEBHOOK_SECRET`.

3. Запустить через systemd (шаблон в `deploy/systemd.service`).

4. Открыть HTTPS порт 80/443 → внутренний порт 8000 (nginx в `deploy/nginx.conf`).

5. После поднятия webhook выполнить:
   ```bash
   curl -X POST https://platform-api.max.ru/subscriptions \
     -H "Authorization: $MAX_TOKEN" \
     -d '{"url":"https://ваш-домен.ru/webhook","secret":"WEBHOOK_SECRET","update_types":["message_created","bot_started"]}'
   ```

## Отладка

Логи пишутся в `stdout`. Для захвата в файл:
```bash
python -m src.main 2>&1 | tee logs/run.log
```

## Сообщество

Вопросы → @business_bot или partner_support@max.ru

---

**Важно:** Документация MAX: https://dev.max.ru/docs-api
