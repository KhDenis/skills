# Go Decor — проверка голосового управления Codex через ChatGPT
Дата: 2026-10-09. Статус: **тестовый протокол, не доказанная интеграция**.

## Цель и критерий успеха

Владелец произносит на **Android в ChatGPT Voice**: «Запусти разработку CRM Go Decor на MacBook Pro». Система создаёт на Mac автономную задачу Codex (с уникальным job_id), возвращает немедленное подтверждение, а Mac продолжает работать независимо от соединения. Позже владелец голосом спрашивает «На каком этапе CRM?» — и получает **живое** состояние задачи, а не выдуманную сводку. Codex может редактировать файлы и запускать тесты в заранее разрешённом каталоге; опасные действия требуют отдельного подтверждения.

Критерий полного успеха: проверены **все три** (а) вызов custom MCP из целевого режима Android Voice, (б) создание автономной задачи через tool с разрешениями на изменение, (в) получение реального промежуточного прогресса во второй голосовой реплике. Успех в ChatGPT Work на web НЕ эквивалентен успеху в Voice.

## Важное ограничение по документации на 09.10.2026

Официальная справка Voice содержит указание, что **Live не поддерживает плагины на старте**. Документация Plugins показывает вызов MCP в ChatGPT Work на web; голосовое управление Work/Codex документировано для desktop на Mac/Windows, а paired remote описан для iOS. Поддержка пользовательского MCP в Android Voice **не подтверждена**. Источники:
- https://help.openai.com/en/articles/20001274-chatgpt-voice
- https://developers.openai.com/plugins/quickstart
- https://developers.openai.com/api/docs/guides/custom-mcp-server
- https://developers.openai.com/api/docs/guides/secure-mcp-tunnels

Не строй сложную систему, предполагая, что Android Live точно вызовет custom MCP.

## Ворота 0 — проверка без MacBook (главное)

1. В браузере ChatGPT на web открой https://chatgpt.com/plugins. Проверь, доступен ли **Add custom MCP server**. Если пункта нет — отметь CUSTOM_PLUGIN_UI=BLOCKED. Не заявляй, что доступ к custom plugins обеспечен только наличием Plus.
2. Если доступен, повтори официальный *read-only* пример: https://developers.openai.com/plugins/quickstart (публичный демонстрационный сервер `https://tinymcp.dev/api/moldy-aloof-zettabyte/mcp`, no authentication **только для публичного demo roll_dice, не для частного Mac**). Установи plugin и проверь `roll_dice(sides=20)` в ChatGPT Work (web). Значение должно вернуться именно из tool call; ответ модели наугад не засчитывается.
3. На Android Settings → Voice найди текущий режим Live / Advanced / Standard. В том же аккаунте попробуй в целевом голосовом режиме: «Вызови инструмент roll_dice из моего тестового плагина с двадцатью гранями и назови результат». Если режима с MCP нет, честно отметь VOICE_PLUGIN_CALL=BLOCKED/NOT_SUPPORTED. Нельзя засчитывать ответ, который модель сгенерировала без инструментального события.
4. Если проверка Live не проходит, но доступен Standard, отдельно проверь именно его (там может быть другой маршрут через транскрипцию) — без гарантий. Запиши точный режим и факт фактического вызова.
5. **Решение:** только после достоверного VOICE_PLUGIN_CALL=PASS переходи к приватному MCP Mac Bridge для целевого Android Voice. Если не PASS — не трать время на туннель ради этой цели; переходи к «План Б».

## Ворота 1 — Mac как MCP (только если ворота 0 пройдены или нужен отдельный web/desktop доступ)

См. [CODEX_SETUP.md](CODEX_SETUP.md). На Mac Codex реализует минимальный read-only `bridge_ping()` и `mac_health()`, локальный MCP и защищённый OpenAI Secure MCP Tunnel. Никаких анонимных публичных endpoints, никаких открытых портов или произвольных shell tools. Проверка `bridge_ping` через web, затем **в Android Voice**. Без доказанного вызова из Android не объявлять задачу выполненной.

## Ворота 2 — автономный Codex (после голосового read-only теста)

Создать job manager c SQLite и фоновым worker на Mac, используя проверенный актуальный Codex SDK. Инструменты: `start_codex_job(project_id,task,idempotency_key)`, `get_codex_job_status(job_id)`, `get_codex_job_events(job_id,cursor)`, `steer_codex_job(job_id,instruction)`, `cancel_codex_job(job_id)`.

`start_codex_job` должен быстро возвращать job_id, не держать HTTP/MCP-запрос до окончания задачи. Worker живёт отдельно от вызова MCP, сохраняет прогресс и переживает перезапуск (с согласованными правилами восстановления). События Codex SDK → журнал с timestamps и безопасной редактированной сводкой. Не выдавать внутренние рассуждения модели или секреты. Разрешённые изменения только в sandbox проекта. Деплой на Hygge, sudo, удаления и действия вне каталога — требуют отдельного разрешения. Тест: запуск задачи в sandbox, реальное изменение тестового файла, событие `running`, промежуточный `stage`, финальное `completed`.

После окончания ответа ChatGPT **MCP сам не инициирует новый голосовой ответ**. Для проактивных уведомлений можно настроить отдельный Telegram notification bot на Mac, который по событиям пишет пользователю. Это другой интерфейс, не продолжение того же голосового диалога.

## План Б — если Android Voice не умеет custom MCP

Для цели владельца рекомендуем не ждать MCP Voice. Создать отдельный **Telegram voice → STT → agent/orchestrator → Codex SDK на Mac → SQLite job manager → Telegram status/notifications**. Пользователь надиктовывает задачу с Android, затем голосом задаёт уточнения; Codex работает часами, Telegram показывает промежуточные события. Возможен TTS ответ. Это не тот же ChatGPT Voice: контекст Go Decor надо синхронизировать через собственную проектную базу и инструкции; память ChatGPT автоматически не переносится. Не сообщать, что он уже настроен.

Альтернативная проверка без Telegram: ChatGPT desktop **Voice in Work/Codex** на Mac, если компьютер, подписка и режим действительно поддерживают её. Это отдельный вариант и не равно Android Voice.

## Отчёт, который нужно принести в следующий разговор

`VOICE_MCP_TEST_REPORT.md` со столбцами: Пункт / PASS|BLOCKED|NOT_TESTED / подтверждение:
- доступ к custom MCP в браузере;
- demo roll_dice через Work web (факт tool call);
- режим Voice на Android;
- demo roll_dice в Android Voice (факт tool call, не догадка модели);
- решение: MCP Bridge или Telegram voice orchestration.

**Приоритет — доказать голосовой вызов MCP до любых сложных установок на Mac.**
