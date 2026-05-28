# Google Sheets weekly report — настройка для заказчика

Одноразовая настройка: ~10 минут. После этого бот сам каждый понедельник в
10:00 МСК будет писать в Sheet строки по завершённым заказам.

## 1. Создать Service Account в Google Cloud

1. Открыть https://console.cloud.google.com/
2. Создать новый проект (или использовать существующий)
3. **APIs & Services → Enabled APIs → + Enable APIs** → найти **Google Sheets API**, нажать Enable
4. **IAM & Admin → Service Accounts → + Create Service Account**
   - Name: `smm-agent-sheets-writer`
   - Role: оставить пустым (доступ выдадим точечно на конкретный Sheet)
5. После создания: нажать на SA → **Keys → Add Key → Create new key → JSON** → скачать файл
6. Файл будет вида `project-name-abc123-1a2b3c.json` — это «ключ»

В файле есть поле `client_email` (например `smm-agent-sheets-writer@my-project.iam.gserviceaccount.com`).
**Скопировать этот email — он нужен на шаге 3.**

## 2. Положить ключ на сервер

Прислать JSON-файл нам (или загрузить самостоятельно):

```bash
# На сервере, как root
mkdir -p /root/smm-agent/credentials
chmod 700 /root/smm-agent/credentials

# Скопировать JSON
nano /root/smm-agent/credentials/google_sheets_sa.json
# (вставить содержимое файла; Ctrl+O, Enter, Ctrl+X)

chmod 600 /root/smm-agent/credentials/google_sheets_sa.json
```

## 3. Расшарить Google Sheet с Service Account

1. Открыть нужный Google Sheet (или создать новый — лист должен быть пустой ИЛИ
   с уже готовым заголовком в первой строке; бот разберётся в обоих случаях)
2. Кнопка **Share** в правом верхнем углу
3. В поле email вставить email Service Account (`smm-agent-sheets-writer@...`)
4. Дать ему права **Editor**
5. Снять галочку «Notify people» (необязательно)
6. Скопировать ID таблицы из URL: `https://docs.google.com/spreadsheets/d/`**`<ID>`**`/edit`

## 4. Прописать в `.env` и рестартнуть

```bash
# /root/smm-agent/.env
GOOGLE_SHEETS_CREDENTIALS_FILE=/root/smm-agent/credentials/google_sheets_sa.json
GOOGLE_SHEETS_SPREADSHEET_ID=<ID из шага 3.6>

# Перезапуск
systemctl restart smm-agent-backend
```

## 5. Проверить, что работает

```bash
DASH=$(grep DASHBOARD_TOKEN /root/smm-agent/.env | cut -d= -f2)

# smoke — открывает лист, читает заголовок, ничего не пишет
curl -X POST -b "auth_token=$DASH" http://localhost:8765/api/sheets/test

# manual push — синхронизирует все накопленные report_rows прямо сейчас
curl -X POST -b "auth_token=$DASH" http://localhost:8765/api/sheets/sync_now
```

Ответ `/api/sheets/test`:
```json
{
  "ok": true,
  "spreadsheet_id": "1abc...",
  "spreadsheet_title": "Отчёт SMM",
  "tab_title": "Трафик из соц сетей",
  "row_count": 200,
  "col_count": 8,
  "first_row": ["Неделя", "Платформа", ...]
}
```

## Какие колонки бот пишет

Если лист пустой — бот создаёт вкладку **«Трафик из соц сетей»** с заголовком:

| Неделя | Платформа | Биржа | Заказано | Фактически | Стоимость | Статус | ID заказа |
|--------|-----------|-------|----------|------------|-----------|--------|-----------|

Если у вас уже есть лист с **другими заголовками** — бот распознаёт синонимы:
- «Неделя» / «Период» / «week»
- «Платформа» / «Платформа-источник»
- «Биржа» / «Панель» / «exchange»
- «Заказано» / «Заказано переходов»
- «Фактически» / «Фактически (Метрика)» / «actual»
- «Стоимость» / «Цена» / «Бюджет»
- «Статус»

Неизвестные колонки бот оставит пустыми (можно использовать для своих заметок).

## Когда срабатывает

- Автоматически: **каждый понедельник в 10:00 МСК** — пушит всё, что не было запушено
- По запросу: `POST /api/sheets/sync_now` — мгновенно
- Идемпотентно: один и тот же заказ никогда не запишется дважды (`pushed_to_sheets_at` в БД)

## Если что-то сломалось

- Проверить логи: `journalctl -u smm-agent-backend --since "1 hour ago" | grep -i sheets`
- Event'ы `sheets_push_failed`, `sheets_push_skipped`, `sheets_push_completed` пишутся в `agent_events` и видны в живой ленте дашборда
