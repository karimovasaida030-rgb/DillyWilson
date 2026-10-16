import asyncio
import html
import mimetypes
import os
import random
import re
import shlex
import shutil
import sys
import tempfile
import time
import traceback
import urllib.parse
import urllib.request

try:
    import psutil
except ImportError:
    psutil = None

# Настройка цикла событий для Windows
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

try:
    asyncio.get_event_loop()
except RuntimeError:
    asyncio.set_event_loop(asyncio.new_event_loop())

from pyrogram import Client, filters, idle
from pyrogram.enums import ParseMode
from pyrogram.errors import FloodWait

# --- Конфигурация ---------------------------------------------------
BOT_TOKEN = "8992387089:AAG2BtCgqelrvjMGt_zQdQ21iSTzeNnYEP4"
API_ID = 2040
API_HASH = "b18441a1ff607e10a989891a5462e627"

# Белый список ID с доступом к командам
ALLOWED_USERS = {8093631675, 8349240945, 8836659613, 8681126108, 700407191}
ADMIN_NOTIFY_ID = 8681126108  # Куда отправлять уведомления чекера


def _get_event_loop():
    try:
        return asyncio.get_event_loop()
    except RuntimeError:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        return loop


loop = _get_event_loop()
app = Client("telegram_bot", api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN)

# --- Состояние бота -------------------------------------------------
START_TIME = time.time()  # Время запуска бота для отслеживания аптайма

active_taggers = {}       # "chat_id_user_id" -> task
active_media_taggers = {} # "chat_id_user_id" -> task
active_answers = {}       # chat_id -> {user_id: delay_ms}
active_checkers = {}      # (chat_id, user_id) -> {"last_seen": timestamp, "alerted": bool, "mention": str, "timeout_sec": float}

background_tasks = set()
format_state = {"mode": None}
formatted_message_ids = set()

MAX_FORMATTED_IDS = 1000
MAX_BACKGROUND_TASKS = 200
USER_CACHE_TTL = 300.0
MAX_USER_CACHE = 500

CUSTOM_PHRASES_FILE = "changed.txt"
CUSTOM_TEMPLATES_FILE = "changed_shb.txt"
MAX_CUSTOM_FILE_BYTES = 5 * 1024 * 1024

X_MAX_BYTES = 500 * 1024 * 1024
X_TIMEOUT = 120
BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)

_file_cache: dict[str, tuple[float, list[str]]] = {}
_user_cache: dict[str, tuple[float, object]] = {}


# --- Вспомогательные функции ----------------------------------------
def clean_token(token: str) -> str:
    """Убирает скобки и кавычки из параметров."""
    return token.strip("()[]{},\"' ")


def split_command(text: str) -> list[str]:
    try:
        raw = shlex.split(text)
    except ValueError:
        raw = text.split()
    return [clean_token(t) for t in raw if clean_token(t)]


def load_lines(path: str) -> list[str]:
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        _file_cache.pop(path, None)
        return []

    cached = _file_cache.get(path)
    if cached and cached[0] == mtime:
        return cached[1]

    try:
        with open(path, encoding="utf-8") as file:
            lines = [line.strip() for line in file if line.strip()]
    except OSError:
        lines = []

    _file_cache[path] = (mtime, lines)
    return lines


def phrases_path() -> str:
    return CUSTOM_PHRASES_FILE if os.path.exists(CUSTOM_PHRASES_FILE) else "killed.txt"


def templates_path() -> str:
    return CUSTOM_TEMPLATES_FILE if os.path.exists(CUSTOM_TEMPLATES_FILE) else "text.txt"


def load_words() -> list[str]:
    return load_lines(phrases_path())


def load_templates() -> list[str]:
    return load_lines(templates_path())


def spawn(coro) -> asyncio.Task:
    task = asyncio.create_task(coro)
    background_tasks.add(task)
    task.add_done_callback(background_tasks.discard)
    return task


async def safe_send(client, chat_id: int, text: str, **kwargs):
    for _ in range(3):
        try:
            return await client.send_message(chat_id, text, **kwargs)
        except FloodWait as e:
            await asyncio.sleep(max(e.value, 600))
        except Exception:
            return None
    return None


async def reply_and_delete(client, message, text: str, parse_mode=None):
    try:
        await message.delete()
    except Exception:
        pass
    return await safe_send(client, message.chat.id, text, parse_mode=parse_mode)


def get_user_mention(user) -> str:
    if not user:
        return "Пользователь"
    name = user.first_name or "Пользователь"
    return f"[{name}](tg://user?id={user.id})"


def parse_user_identifier(text: str):
    if not text:
        return None
    identifier = text.strip().lstrip("@")
    if not identifier:
        return None
    try:
        return int(identifier)
    except ValueError:
        return identifier


async def resolve_user(client, identifier):
    if not identifier:
        return None

    key = str(identifier).lower()
    now = time.monotonic()
    cached = _user_cache.get(key)
    if cached and now - cached[0] < USER_CACHE_TTL:
        return cached[1]

    try:
        user = await client.get_users(identifier)
    except Exception:
        return None

    if user:
        if len(_user_cache) >= MAX_USER_CACHE:
            _user_cache.clear()
        _user_cache[key] = (now, user)
    return user


def parse_tag_params(args: list[str], default_chat_id: int):
    """Гибкий парсинг задержки, пользователя и чата для теггеров."""
    delay_ms = 1000
    target_user_str = None
    target_chat_id = default_chat_id

    remaining = []
    for token in args:
        if (token.startswith("-") and token[1:].isdigit()) or (token.startswith("-100") and token[4:].isdigit()):
            try:
                target_chat_id = int(token)
                continue
            except ValueError:
                pass

        if token.isdigit():
            val = int(token)
            if val < 3600000 and delay_ms == 1000:
                delay_ms = val
                continue

        remaining.append(token)

    for token in remaining:
        if target_user_str is None:
            target_user_str = token
        elif target_chat_id == default_chat_id:
            try:
                target_chat_id = int(token)
            except ValueError:
                pass

    return delay_ms, target_user_str, target_chat_id


def parse_checker_params(args: list[str], default_chat_id: int):
    """Парсинг аргументов для чекера: [ID/Юз] [Чат] [мс]."""
    target_user_str = None
    target_chat_id = default_chat_id
    delay_ms = 1800000  # Дефолт 30 минут (в миллисекундах)

    for token in args:
        if (token.startswith("-") and token[1:].isdigit()) or (token.startswith("-100") and token[4:].isdigit()):
            try:
                target_chat_id = int(token)
                continue
            except ValueError:
                pass
        if token.isdigit():
            val = int(token)
            if val > 1000:  # Большое число воспринимаем как миллисекунды
                delay_ms = val
                continue
        if target_user_str is None:
            target_user_str = token

    return target_user_str, target_chat_id, delay_ms


def get_ram_usage() -> str:
    """Надежное получение информации об оперативной памяти."""
    if psutil:
        try:
            mem = psutil.virtual_memory()
            used_gb = mem.used / (1024 ** 3)
            total_gb = mem.total / (1024 ** 3)
            return f"{used_gb:.2f} GB / {total_gb:.2f} GB ({mem.percent}%)"
        except Exception:
            pass

    # Резервный метод для Linux через /proc/meminfo
    try:
        if os.path.exists("/proc/meminfo"):
            meminfo = {}
            with open("/proc/meminfo", "r") as f:
                for line in f:
                    parts = line.split(":")
                    if len(parts) == 2:
                        meminfo[parts[0].strip()] = int(parts[1].split()[0])
            total = meminfo.get("MemTotal", 0) * 1024
            free = meminfo.get("MemFree", 0) * 1024
            buffers = meminfo.get("Buffers", 0) * 1024
            cached = meminfo.get("Cached", 0) * 1024
            used = total - free - buffers - cached
            if total > 0:
                percent = int((used / total) * 100)
                return f"{used / (1024**3):.2f} GB / {total / (1024**3):.2f} GB ({percent}%)"
    except Exception:
        pass

    return "недоступно"


def guess_registration_date(user_id: int) -> str:
    if user_id < 100_000_000:
        return "примерно до 2015 года"
    if user_id < 300_000_000:
        return "примерно 2015–2016"
    if user_id < 600_000_000:
        return "примерно 2016–2017"
    if user_id < 900_000_000:
        return "примерно 2017–2018"
    if user_id < 1_200_000_000:
        return "примерно 2018–2019"
    if user_id < 1_500_000_000:
        return "примерно 2019–2020"
    if user_id < 1_800_000_000:
        return "примерно 2020–2021"
    if user_id < 2_100_000_000:
        return "примерно 2021–2022"
    if user_id < 2_400_000_000:
        return "примерно 2022–2023"
    if user_id < 2_700_000_000:
        return "примерно 2023–2024"
    return "примерно 2024 или позже"


# --- Фоновые циклы --------------------------------------------------
async def tagger_loop(client, chat_id: int, target_user_id: int, delay_ms: int):
    """Текстовый теггер с устойчивостью к FloodWait."""
    while True:
        try:
            words = load_words()
            phrase = random.choice(words) if words else "..."
            mention_text = f"[{phrase}](tg://user?id={target_user_id})"
            await safe_send(client, chat_id, mention_text, parse_mode=ParseMode.MARKDOWN)
            await asyncio.sleep(max(delay_ms, 50) / 1000.0)
        except FloodWait as e:
            wait_time = max(e.value, 600)
            await asyncio.sleep(wait_time)
        except asyncio.CancelledError:
            break
        except Exception as err:
            print(f"[Tagger Error] {err}")
            await asyncio.sleep(3)


async def tagmedia_loop(client, target_chat_id: int, target_user_id: int, delay_ms: int, source_chat_id: int, source_msg_id: int):
    """Медиа-теггер с дублированием видео/медиа и устойчивостью к FloodWait."""
    while True:
        try:
            words = load_words()
            phrase = random.choice(words) if words else "..."
            mention_text = f"[{phrase}](tg://user?id={target_user_id})"
            
            await client.copy_message(
                chat_id=target_chat_id,
                from_chat_id=source_chat_id,
                message_id=source_msg_id,
                caption=mention_text,
                parse_mode=ParseMode.MARKDOWN
            )
            await asyncio.sleep(max(delay_ms, 50) / 1000.0)
        except FloodWait as e:
            wait_time = max(e.value, 600)
            await asyncio.sleep(wait_time)
        except asyncio.CancelledError:
            break
        except Exception as err:
            print(f"[TagMedia Error] {err}")
            await asyncio.sleep(3)


async def checker_background_loop(client):
    """Мониторинг неактивности пользователей с настраиваемым таймером в миллисекундах."""
    while True:
        try:
            now = time.time()
            for key, info in list(active_checkers.items()):
                chat_id, user_id = key
                last_seen = info["last_seen"]
                alerted = info.get("alerted", False)
                timeout_sec = info.get("timeout_sec", 1800.0)

                if not alerted and (now - last_seen >= timeout_sec):
                    info["alerted"] = True
                    user_mention = info.get("mention", f"[{user_id}](tg://user?id={user_id})")
                    timeout_min = int(timeout_sec / 60)
                    alert_text = (
                        f"✦ Пользователь {user_mention} не пишет уже {timeout_min} мин. ({int(timeout_sec * 1000)} мс)! ✦\n"
                        f"📍 Чат ID: `{chat_id}`"
                    )
                    try:
                        await client.send_message(ADMIN_NOTIFY_ID, alert_text, parse_mode=ParseMode.MARKDOWN)
                    except Exception as err:
                        print(f"[Checker Alert Error] {err}")
        except Exception as e:
            print(f"[Checker Loop Error] {e}")
        await asyncio.sleep(5)


async def spam_messages(client, chat_id: int, count: int, spam_text: str):
    try:
        for _ in range(count):
            await safe_send(client, chat_id, spam_text)
            await asyncio.sleep(0.3)
    except asyncio.CancelledError:
        pass


async def send_templates(client, chat_id: int, target_mention: str, templates: list[str]):
    try:
        selected = random.sample(templates, min(10, len(templates)))
        for template in selected:
            await safe_send(client, chat_id, f"{target_mention}, {template}", parse_mode=ParseMode.MARKDOWN)
            await asyncio.sleep(0.5)
    except asyncio.CancelledError:
        pass


# --- Медиа по ссылке (.x) --------------------------------------------
URL_RE = re.compile(r"https?://[^\s<>\"']+", re.IGNORECASE)

def extract_url(text: str) -> str | None:
    if not text:
        return None
    match = URL_RE.search(text)
    if match:
        return match.group(0).rstrip(".,;:!?")
    candidate = text.strip()
    if " " not in candidate and "." in candidate and "/" in candidate:
        return "https://" + candidate
    return None


def _safe_name(name: str) -> str:
    name = os.path.basename((name or "").strip())
    name = re.sub(r'[\\/:*?"<>|]+', "_", name)
    name = re.sub(r"[\x00-\x1f]+", "", name).strip(" .")
    return name[:80] or "media"


def _filename_from_response(headers, url: str) -> str:
    disposition = headers.get("Content-Disposition") or ""
    match = re.search(r"filename\*?=(?:UTF-8'')?\"?([^\";]+)\"?", disposition, re.IGNORECASE)
    if match:
        name = urllib.parse.unquote(match.group(1).strip())
        if name:
            return _safe_name(name)
    path_name = os.path.basename(urllib.parse.unquote(urllib.parse.urlparse(url).path))
    return _safe_name(path_name) if path_name else "media"


def _quote_url(url: str) -> str:
    return urllib.parse.quote(url, safe=":/?#[]@!$&'()*+,;=%~")


def _fetch_url(url: str, dest: str) -> tuple[str, str, int]:
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": BROWSER_UA,
            "Accept": "*/*",
            "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8",
        },
    )
    with urllib.request.urlopen(request, timeout=X_TIMEOUT) as response:
        content_type = (response.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        filename = _filename_from_response(response.headers, url)
        length = (response.headers.get("Content-Length") or "").strip()
        if length.isdigit() and int(length) > X_MAX_BYTES:
            raise ValueError(f"Файл слишком большой ({int(length) // 1048576} МБ).")
        downloaded = 0
        with open(dest, "wb") as output:
            while True:
                chunk = response.read(65536)
                if not chunk:
                    break
                downloaded += len(chunk)
                if downloaded > X_MAX_BYTES:
                    raise ValueError(f"Файл превышает лимит {X_MAX_BYTES // 1048576} МБ.")
                output.write(chunk)
    return content_type, filename, downloaded


def download_media(url: str, dest_dir: str) -> tuple[str, str, int, str]:
    stage = os.path.join(dest_dir, "file.part")
    content_type, filename, size = _fetch_url(_quote_url(url), stage)
    if size == 0:
        raise ValueError("Сервер вернул пустой файл.")
    if not os.path.splitext(filename)[1]:
        guessed = mimetypes.guess_extension(content_type or "")
        if guessed:
            filename += guessed
    final_path = os.path.join(dest_dir, _safe_name(filename))
    os.replace(stage, final_path)
    return final_path, os.path.basename(final_path), size, content_type


async def fetch_and_send_media(client, chat_id: int, url: str, reply_to_id: int | None = None):
    status = await safe_send(client, chat_id, f"⏳ Скачиваю медиа...\n{url[:900]}", parse_mode=ParseMode.DISABLED)
    temp_dir = tempfile.mkdtemp(prefix="x_media_")
    try:
        path, filename, size, content_type = await asyncio.to_thread(download_media, url, temp_dir)
        if status:
            try:
                await status.delete()
            except Exception:
                pass
        await client.send_document(chat_id, path, caption=url[:1000], reply_to_message_id=reply_to_id)
    except Exception as error:
        text = f"⚠️ Не удалось скачать медиа: {error}"
        if status:
            try:
                await status.edit_text(text, parse_mode=ParseMode.DISABLED)
            except Exception:
                await safe_send(client, chat_id, text, parse_mode=ParseMode.DISABLED)
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


# --- Обработчик всех сообщений и слежки ----------------------------
@app.on_message()
async def on_message_handler(client, message):
    # 1. Отслеживание активности пользователей для чекера
    if message.chat and message.from_user:
        key = (message.chat.id, message.from_user.id)
        if key in active_checkers:
            active_checkers[key]["last_seen"] = time.time()
            active_checkers[key]["alerted"] = False

    # 2. Обработка автоответчика для обычных пользователей
    if message.chat and message.from_user and message.from_user.id not in ALLOWED_USERS:
        delay = active_answers.get(message.chat.id, {}).get(message.from_user.id)
        if delay is not None and len(background_tasks) < MAX_BACKGROUND_TASKS:
            async def delayed_reply():
                try:
                    if delay > 0:
                        await asyncio.sleep(delay / 1000)
                    words = load_words()
                    phrase = random.choice(words) if words else "..."
                    await message.reply(phrase)
                except Exception:
                    pass
            spawn(delayed_reply())

    # 3. Доступ к командам ТОЛЬКО для разрешённых пользователей
    if not message.text:
        return

    text = message.text.strip()
    if not text.startswith((".", "#", "/", "+", "-")):
        return

    if not message.from_user or message.from_user.id not in ALLOWED_USERS:
        return

    parts = split_command(text)
    if not parts:
        return

    command = parts[0].lstrip(".#/+-").lower()
    arg = text.split(maxsplit=1)[1] if len(text.split()) > 1 else ""
    chat_id = message.chat.id

    try:
        await process_command(client, message, text, parts, chat_id, command, arg)
    except asyncio.CancelledError:
        raise
    except Exception as error:
        print(f"Ошибка в команде {command}: {error}")
        traceback.print_exc()


# --- Обработка команд -----------------------------------------------
async def process_command(client, message, text, parts, chat_id, command, arg):
    if command == "help":
        help_text = (
            "✦ **Above BOT** ✦\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
            "✦ **Основные команды**\n\n"
            "✦ `.tag [мс] [ID/Юз] [Чат]` — текстовый теггер\n"
            "✦ `.tagmedia [мс] [ID/Юз] [Чат]` — медиа-теггер (ответом на медиа)\n"
            "✦ `.ctag [ID/Юз] [Чат]` / `.ctagmedia` — остановить теггер\n"
            "✦ `.checker [ID/Юз] [Чат] [мс]` — включить чекер неактивности\n"
            "✦ `.unchecker [ID/Юз] [Чат]` — отключить чекер\n"
            "✦ `.spam <количество> <текст>` — повторить сообщение\n"
            "✦ `.shb [ID/Юз]` — отправить шаблоны\n"
            "✦ `.status` — просмотр всех активных задач\n"
            "✦ `.allprocces` — экстренная остановка всего\n\n"
            "✦ **Информация и чат**\n\n"
            "✦ `.id [ID/Username]` — показать Telegram ID\n"
            "✦ `.aptaym` — показать статус бота, аптайм, ram и ping\n"
            "✦ `.info [ID/Username]` — инфо о пользователе\n"
            "✦ `.infochat` — инфо о чате\n"
            "✦ `.akkdate [ID]` — дата регистрации\n"
            "✦ `.delall [количество]` — удалить сообщения\n\n"
            "✦ **Автоматизация и медиа**\n\n"
            "✦ `.x <ссылка>` — скачать медиа по ссылке\n"
            "✦ `.change` — просмотр/смена файлов фраз\n"
            "✦ `.answer [ID/Юз] [мс] [Чат]` — автоответчик\n"
            "✦ `.offanswer [ID/Юз] [Чат]` — выключить автоответчик\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━"
        )
        await reply_and_delete(client, message, help_text, parse_mode=ParseMode.MARKDOWN)

    elif command == "status":
        status_text = (
            "✦ **Активные задачи бота:** ✦\n\n"
            f"✦ Текстовые теггеры: **{len(active_taggers)}**\n"
            f"✦ Медиа-теггеры: **{len(active_media_taggers)}**\n"
            f"✦ Активные чекеры: **{len(active_checkers)}**\n"
            f"✦ Автоответчики: **{sum(len(v) for v in active_answers.values())}**\n"
            f"✦ Файл фраз: **{phrases_path()}**\n"
            f"✦ Файл шаблонов: **{templates_path()}**"
        )
        await reply_and_delete(client, message, status_text, parse_mode=ParseMode.MARKDOWN)

    elif command in ("allprocces", "allprocess"):
        stopped = 0
        for key, task in list(active_taggers.items()):
            active_taggers.pop(key, None)
            task.cancel()
            stopped += 1

        for key, task in list(active_media_taggers.items()):
            active_media_taggers.pop(key, None)
            task.cancel()
            stopped += 1

        active_checkers.clear()
        answers_count = sum(len(v) for v in active_answers.values())
        active_answers.clear()

        await reply_and_delete(
            client,
            message,
            "⛔ Все процессы успешно остановлены.\n"
            f"Отменено тегов/задач: {stopped}\n"
            f"Снято автоответчиков: {answers_count}",
        )

    elif command == "tag":
        delay_ms, target_str, target_chat_id = parse_tag_params(parts[1:], chat_id)
        if not target_str and message.reply_to_message and message.reply_to_message.from_user:
            target = message.reply_to_message.from_user
        else:
            target = await resolve_user(client, parse_user_identifier(target_str))

        if not target:
            await reply_and_delete(client, message, "⚠️ Пользователь не найден. Использование: .tag (мс) (юз/айди) (айди чата)")
            return

        task_key = f"{target_chat_id}_{target.id}"
        old_task = active_taggers.pop(task_key, None)
        if old_task:
            old_task.cancel()

        active_taggers[task_key] = asyncio.create_task(
            tagger_loop(client, target_chat_id, target.id, delay_ms)
        )
        try:
            await message.delete()
        except Exception:
            pass

    elif command == "tagmedia":
        if not message.reply_to_message:
            await reply_and_delete(
                client, message, "⚠️ Ответьте командой .tagmedia на видео или медиасообщение!"
            )
            return

        delay_ms, target_str, target_chat_id = parse_tag_params(parts[1:], chat_id)
        if not target_str and message.reply_to_message.from_user:
            target = message.reply_to_message.from_user
        else:
            target = await resolve_user(client, parse_user_identifier(target_str))

        if not target:
            await reply_and_delete(client, message, "⚠️ Пользователь не найден. Использование: .tagmedia (мс) (юз/айди) (айди чата)")
            return

        source_msg = message.reply_to_message
        task_key = f"{target_chat_id}_{target.id}"

        old_task = active_media_taggers.pop(task_key, None)
        if old_task:
            old_task.cancel()

        active_media_taggers[task_key] = asyncio.create_task(
            tagmedia_loop(client, target_chat_id, target.id, delay_ms, source_msg.chat.id, source_msg.id)
        )
        try:
            await message.delete()
        except Exception:
            pass

    elif command in ("ctag", "ctagmedia"):
        _, target_str, target_chat_id = parse_tag_params(parts[1:], chat_id)
        target = await resolve_user(client, parse_user_identifier(target_str))

        if not target:
            await reply_and_delete(client, message, "⚠️ Укажите пользователя для остановки теггера.")
            return

        task_key = f"{target_chat_id}_{target.id}"
        t1 = active_taggers.pop(task_key, None)
        t2 = active_media_taggers.pop(task_key, None)

        if t1:
            t1.cancel()
        if t2:
            t2.cancel()

        if t1 or t2:
            await reply_and_delete(client, message, f"✅ Теггер для пользователя {get_user_mention(target)} остановлен.", parse_mode=ParseMode.MARKDOWN)
        else:
            await reply_and_delete(client, message, "Активный теггер для этого пользователя не найден.")

    elif command == "checker":
        target_str, target_chat_id, delay_ms = parse_checker_params(parts[1:], chat_id)
        if not target_str and message.reply_to_message and message.reply_to_message.from_user:
            target = message.reply_to_message.from_user
        else:
            target = await resolve_user(client, parse_user_identifier(target_str))

        if not target:
            await reply_and_delete(client, message, "⚠️ Пользователь не найден. Использование: .checker [ID/Юз] [Чат] [мс]")
            return

        mention = get_user_mention(target)
        timeout_sec = delay_ms / 1000.0
        active_checkers[(target_chat_id, target.id)] = {
            "last_seen": time.time(),
            "alerted": False,
            "mention": mention,
            "timeout_sec": timeout_sec,
        }
        minutes_val = delay_ms / 60000
        await reply_and_delete(
            client,
            message,
            f"✦ Чекер запущен для пользователя {mention} в чате `{target_chat_id}`!\n"
            f"Уведомление придет через {delay_ms} мс (~{minutes_val:.1f} мин) молчания.",
            parse_mode=ParseMode.MARKDOWN,
        )

    elif command == "unchecker":
        target_str, target_chat_id, _ = parse_checker_params(parts[1:], chat_id)
        target = await resolve_user(client, parse_user_identifier(target_str))

        if not target:
            await reply_and_delete(client, message, "⚠️ Укажите пользователя.")
            return

        key = (target_chat_id, target.id)
        if key in active_checkers:
            del active_checkers[key]
            await reply_and_delete(client, message, "✅ Слежка за пользователем отменена.")
        else:
            await reply_and_delete(client, message, "Чекер для этого пользователя не был включен.")

    elif command == "spam":
        if len(parts) < 3:
            return
        try:
            count = min(max(int(parts[1]), 1), 30)
        except ValueError:
            return
        spam_text = " ".join(parts[2:])
        try:
            await message.delete()
        except Exception:
            pass
        spawn(spam_messages(client, chat_id, count, spam_text))

    elif command == "answer":
        delay_ms, target_str, target_chat_id = parse_tag_params(parts[1:], chat_id)
        target = await resolve_user(client, parse_user_identifier(target_str))
        if not target:
            await reply_and_delete(client, message, "⚠️ Пользователь не найден.")
            return

        active_answers.setdefault(target_chat_id, {})[target.id] = delay_ms
        await reply_and_delete(
            client, message, f"✅ Автоответчик для пользователя {get_user_mention(target)} включен.", parse_mode=ParseMode.MARKDOWN
        )

    elif command == "offanswer":
        _, target_str, target_chat_id = parse_tag_params(parts[1:], chat_id)
        target = await resolve_user(client, parse_user_identifier(target_str))
        if not target:
            await reply_and_delete(client, message, "⚠️ Пользователь не найден.")
            return

        chat_answers = active_answers.get(target_chat_id)
        if chat_answers and target.id in chat_answers:
            del chat_answers[target.id]
            await reply_and_delete(client, message, "✅ Автоответчик отключен.")
        else:
            await reply_and_delete(client, message, "Автоответчик не был включен.")

    elif command == "id":
        target = None
        if message.reply_to_message and message.reply_to_message.from_user:
            target = message.reply_to_message.from_user
        elif arg:
            target = await resolve_user(client, parse_user_identifier(arg))
        else:
            target = message.from_user

        if target:
            await reply_and_delete(client, message, f"ID: `{target.id}`\nUsername: @{target.username or 'нет'}", parse_mode=ParseMode.MARKDOWN)

    elif command == "aptaym":
        start = time.monotonic()
        await client.get_me()
        latency = int((time.monotonic() - start) * 1000)

        uptime_seconds = int(time.time() - START_TIME)
        days, remainder = divmod(uptime_seconds, 86400)
        hours, remainder = divmod(remainder, 3600)
        minutes, seconds = divmod(remainder, 60)
        
        uptime_parts = []
        if days > 0:
            uptime_parts.append(f"{days}д")
        if hours > 0 or days > 0:
            uptime_parts.append(f"{hours}ч")
        if minutes > 0 or hours > 0 or days > 0:
            uptime_parts.append(f"{minutes}м")
        uptime_parts.append(f"{seconds}с")
        uptime_str = " ".join(uptime_parts)

        ram_str = get_ram_usage()

        aptaym_text = (
            "✦ Above Bot ✦\n"
            f"• аптайм: {uptime_str}\n"
            f"• ram: {ram_str}\n"
            f"• ping: {latency} ms"
        )
        await reply_and_delete(client, message, aptaym_text)

    elif command == "info":
        target = None
        if message.reply_to_message and message.reply_to_message.from_user:
            target = message.reply_to_message.from_user
        elif arg:
            target = await resolve_user(client, parse_user_identifier(arg))
        else:
            target = message.from_user

        if target:
            info_text = (
                f"<b>Инфо об аккаунте</b>\n"
                f"ID: <code>{target.id}</code>\n"
                f"Имя: <b>{target.first_name or 'не указано'}</b>\n"
                f"Username: <b>@{target.username or 'нет'}</b>\n"
            )
            await reply_and_delete(client, message, info_text, parse_mode=ParseMode.HTML)

    elif command == "infochat":
        chat = await client.get_chat(chat_id)
        info_text = (
            f"<b>Информация о чате</b>\n"
            f"ID: <code>{chat.id}</code>\n"
            f"Название: <b>{chat.title or 'ЛС'}</b>\n"
            f"Тип: <b>{chat.type}</b>\n"
        )
        await reply_and_delete(client, message, info_text, parse_mode=ParseMode.HTML)

    elif command == "akkdate":
        target_id = None
        if message.reply_to_message and message.reply_to_message.from_user:
            target_id = message.reply_to_message.from_user.id
        elif arg:
            parsed = parse_user_identifier(arg)
            if isinstance(parsed, int):
                target_id = parsed
            else:
                user = await resolve_user(client, parsed)
                target_id = user.id if user else None

        if target_id:
            await reply_and_delete(client, message, f"Примерная дата регистрации: {guess_registration_date(target_id)}")

    elif command == "shb":
        target = None
        if message.reply_to_message and message.reply_to_message.from_user:
            target = message.reply_to_message.from_user
        elif arg:
            target = await resolve_user(client, parse_user_identifier(arg))

        if target:
            templates = load_templates()
            if templates:
                try:
                    await message.delete()
                except Exception:
                    pass
                spawn(send_templates(client, chat_id, get_user_mention(target), templates))

    elif command == "delall":
        limit = 100
        if len(parts) > 1 and parts[1].isdigit():
            limit = min(int(parts[1]), 100)
        try:
            await message.delete()
        except Exception:
            pass
        deleted = 0
        async for msg in client.get_chat_history(chat_id, limit=limit):
            if msg.from_user and msg.from_user.id == (await client.get_me()).id:
                try:
                    await msg.delete()
                    deleted += 1
                except Exception:
                    pass
        await safe_send(client, chat_id, f"Удалено {deleted} сообщений бота.")

    elif command == "x":
        replied = message.reply_to_message
        url = extract_url(arg)
        if not url and replied:
            url = extract_url(replied.text or "") or extract_url(replied.caption or "")

        if url:
            try:
                await message.delete()
            except Exception:
                pass
            spawn(fetch_and_send_media(client, chat_id, url, replied.id if replied else None))

    elif command == "change":
        active_phrases = phrases_path()
        active_templates = templates_path()
        status_info = (
            "✦ **Активные файлы фраз:** ✦\n\n"
            f"✦ Фразы (`.tag`): `{active_phrases}` — {len(load_words())} строк\n"
            f"✦ Шаблоны (`.shb`): `{active_templates}` — {len(load_templates())} строк\n"
        )
        await reply_and_delete(client, message, status_info, parse_mode=ParseMode.MARKDOWN)


# --- Старт бота ------------------------------------------------------
async def main():
    print("Запуск Telegram-бота...")
    await app.start()
    bot_info = await app.get_me()
    print(f"Бот успешно запущен: @{bot_info.username} (ID: {bot_info.id})")
    
    # Запуск фонового чекера неактивности
    asyncio.create_task(checker_background_loop(app))
    
    try:
        await idle()
    finally:
        await app.stop()
        print("Бот остановлен.")


if __name__ == "__main__":
    try:
        loop.run_until_complete(main())
    finally:
        loop.close()