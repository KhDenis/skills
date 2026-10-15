# Telegram Archive: сравнение перед переносом

Первый пункт [Issue #1](https://github.com/KhDenis/skills/issues/1), проверка 2026-10-15.
Это сравнение существующих реализаций, а не объявление переноса завершённым.

## Проверенные исходники

- `KhDenis/skills` main: `06fe346c71b703bb5b9eb69743d94d569dcc2ebc`,
  модуль `telegram-archive-research/`.
- `KhDenis/godecor` main: `3342ccf6153f0160799269fb6c8ed01c77f477e6`,
  модуль `services/telegram-archive/`, контракт объявляет версию `0.2.0`.
- Установленные на сервере `work_project_archive.py`, `chronology.py` и
  `audio_notes.py` побайтно совпали с соответствующими файлами Go Decor.
  Старая серверная копия репозитория skills отставала; её состояние не использовано
  как актуальный исходник.

## Различия и доказательства

| Область | skills | Go Decor | Что подтверждено |
|---|---|---|---|
| Старые команды | `discover`, `approve/reject`, `sync`, `plan-images/download-images`, `export-analysis`, `materialize`, `status`, `contact-sheets` | Сохранены; добавлены фильтры проектов и расширенные вложения | 4 теста старого engine прошли; это синтетические проверки |
| Поиск по всей истории | Нет `search` / `search-report` | Ключевые слова, даты, выбор диалогов, `search_hits`, обработка FloodWait | Журнал реального прежнего прогона есть; новый live-поиск не запускался |
| Манифест и медиа | Старый отбор изображений | `check-manifest`, `plan-media/download-media`, refs, окно соседних сообщений, виды вложений, лимиты и `--confirm` | Синтетические тесты охватывают выборку, ошибки, повтор и продолжение; на реальном архиве проверено наличие локальных изображений текущего манифеста |
| Хронология | Нет отдельного CLI | `chronology.py timeline/report`, read-only SQLite, JSON состояния | `report` успешно выполнен на существующем архиве без сети; локальные хронологии отражены в журнале, повторная генерация в этой сессии не выполнялась |
| Теги и листы просмотра | Базовые contact sheets | `image_tags.py auto/set/clear/report`, фильтрация sheets | Тесты синтетические; качество тегов и просмотр владельцем не подтверждены |
| Портфолио как вид | Нет | `portfolio.py build/check`, `portfolio_html.py` | Синтетический pipeline-тест; это не приёмка клиентского портфолио |
| Аудио | Старые export/personal/idle scripts | `audio_notes.py scan/report/download/transcribe/triage/export-kb/import-jsonl`, `whisper_batch.py` | Реальный пилот и строки расшифровок в KB существуют; новый запуск моделей и полнота импорта ночных результатов не проверялись |

Неизменны: оба requirements-файла, installer/configure, базовые export/personal/idle
скрипты и `tg_config.py`. Изменены `SKILL.md`, `references/work-projects.md`,
`work_project_archive.py` и его тесты. Добавлены README, module.json, семь файлов
scripts и два файла engine tests. Точный список воспроизводится `diff -qr` ниже.

`export_chat.py`, `personal_text_archive.py`, `idle_transcribe.py`, `diagnose.py`
сохранены в обеих версиях; их авторизация, сеть и фоновые режимы в этой сессии
не запускались. Наличие файлов не считается подтверждением этих сценариев.

## Граница универсального кода

Кандидат канонического исходника для следующего пункта — **проверенная версия
Go Decor**, а не старая копия skills. Пока канонический исходник не переключён:
нет опубликованной переносимой версии и закреплённой зависимости Go Decor.

- Ядро: одобрение диалогов, SQLite, поиск, выборка/скачивание медиа,
  происхождение сообщений, materialize, chronology.
- `image_tags` и portfolio — существующие производные виды; перенос не должен
  становиться разработкой следующих продуктов из ROADMAP.
- `audio_notes.py` содержит прикладной `export-kb`, связанный со схемой Go Decor.
  Перед публичным переносом выделить границу адаптера; `whisper_batch.py` остаётся
  существующим вспомогательным CLI, а не завершённым Universal Transcriber.
- `m4_whisper_task.sh` содержит путь `~/godecor-heavy` и предположения о серверном
  окружении. Оставить этот wrapper в приложении или параметризовать отдельно.
- Корневые тесты `test_telegram_chronology.py`, `test_telegram_image_tags.py`,
  `test_telegram_portfolio.py`, `test_telegram_portfolio_html.py` сейчас вне модуля.
  При переносе нужно сохранить их покрытие, а не ограничиться engine tests.
- Не переносить профиль, `.env`, сессии, SQLite, сообщения, медиа и runtime logs.

## Проверки и воспроизведение

Из родительского каталога двух checkout, Python с установленными requirements:

```sh
diff -qr -x __pycache__ skills/telegram-archive-research godecor/services/telegram-archive
python -m unittest discover -s skills/telegram-archive-research/assets/engine/tests
python -m unittest discover -s godecor/services/telegram-archive/assets/engine/tests
python -m unittest discover -s godecor/tests
```

`diff` возвращает 1 при различиях — это ожидается. Результат тестов: skills 4/4,
Go Decor engine 34/34, корневой набор Go Decor 122/122 (включает новые проверки
прикладного отбора текстовых кандидатов). Тесты engine используют fake Telegram,
поэтому не доказывают сетевой экспорт или авторизацию.

На сервере выполнена установленная команда `chronology.py report`; она завершилась
успешно. По текущему манифесту проверены существующие локальные пути изображений.
Файлы профиля и клиентский текст не публикуются вместе с этими доказательствами.

Ограничения: чистая установка/doctor, новая авторизация, live sync/download,
реальное прерывание сетевого экспорта и повторный запуск после него в этой сессии
не проверены. Сохранённая в README модуля фраза «не запускалось на реальном
аккаунте» противоречит более поздним `module.json.real_runs` и RUN.md; при обновлении
исходника её нужно исправить, сохранив различие между журналом и новой проверкой.

Следующая операция по Issue #1: обновить skills из указанного Go Decor commit,
сохранить универсальное ядро и тесты, вынести прикладные зависимости; прежнюю
рабочую установку не удалять. Остальные галочки Issue этой проверкой не закрыты.
