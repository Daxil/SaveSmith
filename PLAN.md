# SaveSmith — план работ

Статус: веха 1 не начата, код не написан. Этот файл — рабочий документ, обновляется по ходу.

---

## 1. Зафиксированные решения

Ответы на стартовые вопросы, дальше считаем их данностью.

| № | Вопрос | Решение |
|---|---|---|
| 1 | Язык лейблов | **i18n-словарь с самого начала.** `label`/`group`/`warn` в манифесте — объекты `{"ru": ..., "en": ...}`, фолбэк на `en`, дальше на первый доступный ключ |
| 2 | Платформы вехи 1 | **Windows + macOS.** Linux-ветка кидает `UnsupportedPlatformError` с человеческим текстом, не «падает как получится» |
| 3 | Тулчейн | **uv + pin Python 3.12** (`.python-version`), системный 3.14 не трогаем |
| 4 | Проверка Windows | **GitHub Actions**, матрица `windows-latest` / `macos-14` (arm64) / `macos-13` (x64) поднимается в вехе 1 |
| 5 | Бэкапы | **Папка приложения пользователя:** `%LOCALAPPDATA%\SaveSmith\backups`, `~/Library/Application Support/SaveSmith/backups`. Структура `backups/<plugin-id>/<ISO-таймстемп>/`. Сознательно **не** рядом с сейвом — иначе бэкапы уедут в Steam Cloud и сожрут квоту |
| 6 | Репозиторий плагинов | **Монорепо сейчас.** Но код с первого дня ходит в плагины только через `PluginSource` (URL + версия + локальный кэш), чтобы веха 8 была переездом файлов, а не переписыванием |
| 7 | Git | **Приватный GitHub-репозиторий**, LICENSE выбираем перед публикацией |

### Отложенные вопросы (решаю дефолтом, скажи если не так)

- **Зависимости на Windows:** чистый `ctypes` + `winreg` из стандартной библиотеки, без `pywin32`. Причина: `pywin32` тянет ~40 МБ в PyInstaller-сайдкар и требует post-install шага. Всё нужное (`SHGetKnownFolderPath`) — три строки на `ctypes`.
- **Парсер VDF:** свой, в `core/vdf.py`. Библиотека `vdf` с PyPI не поддерживает часть старых форматов `libraryfolders.vdf` и всё равно потребовала бы обёртки; формат простой, тестов на нём будет больше, чем кода.
- **Тесты ФС:** реальные временные деревья через `tmp_path` + инъекция системного фасада, без `pyfakefs`. `pyfakefs` плохо дружит с `ctypes`-вызовами и усложняет отладку.

### Отклонения от буквы спецификации

1. **`core/paths.py` становится пакетом `core/paths/`** (`__init__.py`, `_windows.py`, `_macos.py`, `_system.py`). Требование «ни одного `if platform == "win32"` вне слоя путей» выполняется по сути: всё платформенное живёт внутри одного пакета и наружу торчит один интерфейс. Один файл на 600 строк с двумя платформами внутри читается хуже и тестируется хуже.
2. **Steam Cloud и Wine-префиксы получают тестируемый фасад** — `SystemFacade` (env, known folders, реестр, текущий пользователь, платформа). Без него Windows-логику нельзя проверить с макбука, а «проверим потом на винде» спецификация запрещает.

---

## 2. Карта вех (для контекста)

| Веха | Содержание | Статус |
|---|---|---|
| **1** | `core/paths/`, `core/steam.py`, сканер Wine-префиксов, каркас репо, CI-матрица | **в работе** |
| 2 | Ядро плагинов, `pipeline`, `ops/`, 3 плагина вручную, round-trip тесты | — |
| 3 | Классификатор риска, `risk_db.json`, мастер Steam Cloud | — |
| 4 | CLI: `scan` / `parse` / `set` / `backup` | — |
| 5 | GUI на Tauri, подпись и нотаризация | — |
| 6 | Агент discovery (лестница декодеров → LLM последним) | — |
| 7 | Детект чексумм | — |
| 8 | Репозиторий плагинов: экспорт/импорт, версии, установка из GitHub | — |

---

## 3. Веха 1 — подробно

**Цель:** приложение умеет ответить на вопрос «где на этой машине лежат сейвы» — на Windows и macOS, включая игры, запущенные под Wine на маке. Никакого парсинга сейвов, никакой записи.

**Почему это первое:** самая частая причина «программа не работает» у конкурентов — сейв не найден. OneDrive-редирект `Documents`, LocalLow, библиотеки Steam на втором диске, бутылки Whisky. Всё это надо закрыть до того, как появится что редактировать.

### Итоговая структура после вехи 1

```
savesmith/
  core/
    errors.py            # SaveSmithError + user_message
    platform_.py         # Platform enum, определение текущей платформы
    paths/
      __init__.py        # PathResolver — публичный API
      _system.py         # SystemFacade: env, known folders, реестр, юзер
      _windows.py        # SHGetKnownFolderPath, winreg, \\?\ длинные пути
      _macos.py          # ~/Library/*, Containers, Preferences
    vdf.py               # текстовый VDF/ACF парсер
    steam.py             # SteamInstall, библиотеки, appmanifest, userdata
    wine.py              # сканер префиксов, определение {WINEUSER}
    diagnostics.py       # dump всего найденного — для CI и для пользователя
  tests/
    data/steam/          # golden-образцы vdf/acf трёх поколений
    data/wine/           # скелеты бутылок
    conftest.py          # фикстуры fake_windows_home / fake_macos_home
  .github/workflows/ci.yml
  pyproject.toml
  .python-version
  README.md
```

### Задачи

#### 1.0 Каркас репозитория
- `uv init`, `.python-version` = 3.12, `pyproject.toml` (пакет `savesmith`, зависимости пока пустые)
- Дев-зависимости: `pytest`, `pytest-cov`, `ruff`, `mypy` (strict для `core/`)
- `.gitignore`, `git init`, первый коммит
- README: название, дисклеймер про оффлайн-игры и EULA (полный текст — веха 3), статус «в разработке»
- **Приватный GitHub-репозиторий создаю только после твоего явного «да»** — это внешнее действие

*Готово когда:* `uv run pytest` проходит на пустом наборе, `ruff` и `mypy` чистые.

#### 1.1 `core/errors.py` + `core/platform_.py`
- `SaveSmithError` с полями `user_message` (человеческий текст) и `detail` (для лога)
- Подклассы: `UnsupportedPlatformError`, `PathResolutionError`, `SteamNotFoundError`, `WinePrefixError`
- `Platform` enum, `current_platform()`, `LINUX` распознаётся, но помечен неподдерживаемым

*Готово когда:* тест проверяет, что ни одно исключение вехи 1 не всплывает наружу без `user_message`.

#### 1.2 `SystemFacade` — точка инъекции
Единственный способ, которым код узнаёт что-либо о машине:
- `env(name)` — переменные окружения
- `known_folder(folder_id)` — Windows known folders (реальный вызов или подстава в тестах)
- `registry_read(hive, key, value)` — только чтение, только `HKCU`/`HKLM`
- `home()`, `username()`, `platform`
- `RealSystem` (боевая) и `FakeSystem` (тесты, конструируется из словаря + корня `tmp_path`)

*Готово когда:* `FakeSystem` позволяет с macOS-хоста полностью прогнать Windows-ветку резолвера.

#### 1.3 `core/paths/` — резолвер токенов
Токены из спецификации плюс необходимые добавки:

| Токен | Windows | macOS |
|---|---|---|
| `{APPDATA}` | `FOLDERID_RoamingAppData` | `~/Library/Application Support` |
| `{LOCALAPPDATA}` | `FOLDERID_LocalAppData` | `~/Library/Application Support` |
| `{LOCALLOW}` | `FOLDERID_LocalAppDataLow` | `~/Library/Application Support` |
| `{DOCUMENTS}` | `FOLDERID_Documents` **(не `%USERPROFILE%\Documents`)** | `~/Documents` |
| `{SAVEDGAMES}` | `FOLDERID_SavedGames` | — |
| `{USERPROFILE}` | `FOLDERID_Profile` | `~` |
| `{STEAM}` | реестр `HKCU\Software\Valve\Steam\SteamPath`, фолбэк на Program Files (x86) | `~/Library/Application Support/Steam` |
| `{CONTAINERS}` | — | `~/Library/Containers` |
| `{PREFS}` | — | `~/Library/Preferences` (Unity PlayerPrefs plist) |
| `{SAVESMITH_DATA}` | `%LOCALAPPDATA%\SaveSmith` | `~/Library/Application Support/SaveSmith` |
| `{WINEUSER}` | — | подставляется сканером префиксов |

Правила:
- Раскрытие токена и глоббинг — **разные шаги**. `expand()` → строка пути; `resolve(pattern)` → список реально существующих файлов
- Недоступный на платформе токен даёт `None` → паттерн просто не даёт совпадений, это не ошибка
- Неизвестный токен — ошибка валидации плагина, не тихое игнорирование
- Windows: длинные пути через `\\?\` на границе IO
- macOS: не полагаемся на регистронезависимость, APFS бывает case-sensitive

*Готово когда:* таблица токенов покрыта тестами для обеих платформ на `FakeSystem`; отдельный `native`-тест на CI проверяет, что реальный `SHGetKnownFolderPath` возвращает то же, что фейк.

#### 1.4 OneDrive-редирект (отдельная задача, не подпункт)
Самая частая причина ненайденного сейва. Проверяем на реальной Windows в CI:
- `FOLDERID_Documents` при включённом OneDrive возвращает `%USERPROFILE%\OneDrive\Documents`
- Тест, который ловит попытку собрать путь как `home() / "Documents"` — grep-тест по исходникам, чтобы регрессия не проехала

#### 1.5 `core/vdf.py`
Текстовый VDF: кавычки, экранирование, вложенные блоки, комментарии `//`, дубликаты ключей, BOM, CRLF. Бинарный `appinfo.vdf` не нужен — не парсим.

*Готово когда:* golden-тесты на образцах из `tests/data/steam/`, включая заведомо битый файл (не падаем, отдаём понятную ошибку).

#### 1.6 `core/steam.py`
- Поиск установки Steam (реестр / стандартные пути / переменная окружения для тестов)
- `libraryfolders.vdf` — **три поколения формата**: старый плоский (`"1" "D:\\SteamLibrary"`), промежуточный, актуальный (объект с `path` и словарём `apps`). Плюс исторический путь `SteamApps/` с другим регистром
- `steamapps/appmanifest_<appid>.acf` → `InstalledGame(appid, name, installdir, install_path, size, last_updated)`
- `userdata/<steamid>/<appid>/remotecache.vdf` и `userdata/<steamid>/config/localconfig.vdf` — только доступ к данным, решения про облако в вехе 3
- Библиотека на отключённом внешнем диске: пропускаем с пометкой, не падаем

*Готово когда:* по фейковому дереву Steam из `tests/data/` собирается корректный список игр на обеих платформах; отсутствие Steam даёт `SteamNotFoundError` с внятным текстом, а не пустой список.

#### 1.7 `core/wine.py` — сканер префиксов
- Кандидаты на macOS: Whisky (`~/Library/Application Support/Whisky/Bottles/*`), CrossOver (`~/Library/Application Support/CrossOver/Bottles/*`), `~/.wine`, произвольная папка от пользователя
- Признак префикса: наличие `drive_c` **и** (`system.reg` или `user.reg`)
- `{WINEUSER}`: перечисляем `drive_c/users/*` минус `Public`/`Default`/`All Users`/`Default User`. Один кандидат — берём; несколько — возвращаем все, приоритет совпадению с именем macOS-пользователя, затем `crossover`/`steamuser`. Молча не угадываем
- Внутри префикса резолвим Windows-токены относительно `drive_c` — переиспользуем `_windows.py` через `FakeSystem`-подобный адаптер, а не копипастой
- Ограничение глубины обхода, защита от символических циклов, таймаут
- **Parallels и VM в целом — вне области.** Сейвы лежат внутри образа диска, живой ФС нет. README объясняет это явно, чтобы не ловить баг-репорты

*Готово когда:* скелеты бутылок в `tests/data/wine/` (Whisky, CrossOver, «мусорная» папка с `drive_c`, но без `.reg`) классифицируются верно.

#### 1.8 `core/diagnostics.py`
Одна функция, печатающая: платформу, все раскрытые токены, найденную установку Steam, библиотеки, число игр, найденные Wine-префиксы. Нужна и на CI (native-джоб её гоняет), и тебе, чтобы прислать вывод с реальной машины.

#### 1.9 CI-матрица
`.github/workflows/ci.yml`: `windows-latest`, `macos-14`, `macos-13` × Python 3.12. Шаги: `ruff`, `mypy`, `pytest` (весь набор), затем `pytest -m native` (тесты, требующие настоящей ОС), затем `diagnostics` как smoke.

---

## 4. Риски вехи 1

| Риск | Смягчение |
|---|---|
| `FOLDERID_LocalAppDataLow` на CI-раннере может вести не туда, где он у живого пользователя | Native-тест проверяет только форму и существование пути, не точное значение |
| В GitHub-раннере Steam не установлен | Тесты Steam гоняются на подставном дереве; native-джоб проверяет только «не упало на отсутствии Steam» |
| Wine-префиксы на CI не проверить вообще | Полностью на фикстурах; отдельно попрошу тебя прогнать `diagnostics` на маке с реальной бутылкой Whisky |
| Соблазн потащить платформенные ветвления в `steam.py` и `wine.py` | Тест-страж: grep по `core/` вне `paths/` на `sys.platform`, `os.name`, `platform.system` — падает на любом совпадении |

## 5. Definition of Done вехи 1

- `ruff`, `mypy --strict` по `core/`, `pytest` — зелёные на всех трёх раннерах
- Токены из таблицы покрыты тестами для обеих платформ
- Ни одного платформенного ветвления вне `core/paths/` — проверяется тестом-стражем
- Все ошибки вехи имеют `user_message` человеческим языком
- README описывает, что уже работает, и содержит дисклеймер
- Записи в файлы за эту веху вообще не производится — только чтение

---

## 6. Что сознательно не делается в вехе 1

Чтение Unity PlayerPrefs (реестр на Windows, бинарный plist на macOS) — доступ к реестру и `plistlib` появляется здесь, но декодирование мангленных имён ключей Unity (`name_h<hash>`) уезжает в веху 2, вместе с остальным разбором форматов.
