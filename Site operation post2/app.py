"""Генератор постов для личного блога. Flask + нейросеть через ProxyAPI.

Блог: Алина Цыганова, мастер перманентного макияжа.

Как это работает:
1. Пользователь вводит ссылку на работу ИЛИ тему поста + выбирает настроение.
2. Если дана ссылка — сайт скачивает страницу и забирает текст.
3. Этот текст отправляется языковой модели, она пишет пост от лица Алины.
"""
import base64
import io
import json
import os
import re
import threading
import time
from datetime import datetime
from flask import Flask, render_template, request
import requests
from bs4 import BeautifulSoup
from PIL import Image

app = Flask(__name__)


# --- Подгружаем .env без лишних библиотек (ключ лежит в файле .env) ---
def _load_dotenv():
    path = os.path.join(os.path.dirname(__file__), ".env")
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                os.environ.setdefault(key.strip(), value.strip())


_load_dotenv()

# ============================================================
# НАСТРОЙКИ PROXYAPI (ключ берётся из файла .env)
# ============================================================
PROXY_API_URL = "https://api.proxyapi.ru/v1/chat/completions"
PROXY_API_KEY = os.getenv("PROXY_API_KEY", "")

# --- МЕНЯЙ МОДЕЛЬ ЗДЕСЬ: любая текстовая модель из каталога ProxyAPI ---
# openai/gpt-5-mini — недорогая и быстрая, удобна для проверки.
MODEL = "openai/gpt-5-mini"

# ============================================================
# НАСТРОЙКИ TELEGRAM (токен и чат берутся из файла .env)
# Как получить — подробно в README, раздел «Подключаем Telegram».
# ============================================================
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

# ============================================================
# НАСТРОЙКИ СТИЛЯ (меняются здесь, подробнее — в voice.md)
# ============================================================

# --- МЕНЯЙ ТОН ЗДЕСЬ: описание эмоций для каждого настроения ---
# Подсказка "hint" отправляется нейросети вместе с ссылкой на товар.
MOODS = {
    "friendly": {
        "label": "Дружелюбное 😊",
        "hint": "тёплый и заботливый тон, как совет другу",
    },
    "energetic": {
        "label": "Энергичное ⚡",
        "hint": "дерзкий и энергичный тон, драйв и азарт",
    },
    "business": {
        "label": "Деловое 💼",
        "hint": "спокойный деловой тон, факты и выгода",
    },
    "playful": {
        "label": "Игривое 🎉",
        "hint": "весёлый игривый тон, юмор и лёгкость",
    },
    "funny": {
        "label": "Смешное 😂",
        "hint": "смешной тон, шутки и лёгкие мемы, но без пошлости и обидного юмора",
    },
}

# --- МЕНЯЙ СТИЛЬ ЗДЕСЬ: системный промпт = краткий voice.md для нейросети ---
# Нейросеть читает этот текст перед каждым постом и пишет в этом стиле.
SYSTEM_PROMPT = """Ты — Алина Цыганова, мастер перманентного макияжа. Ведёшь личный блог
и пишешь посты от первого лица — тепло, уверенно, как с близкой подругой.

Темы блога: брови, губы, межресничка и стрелки, зажившие работы,
уход до и после процедуры, страхи клиентов, запись на процедуру.

Структура поста (всегда в этом порядке):
1. Заголовок: эмодзи + цепляющая фраза.
2. Строка эмодзи для настроения.
3. Основной текст от первого лица: 2–4 живых предложения, опыт мастера.
4. Польза для читателя: ровно 3 пункта, каждый начинается с ✅
   (кому подойдёт, что получит, почему это безопасно и комфортно).
5. Призыв к действию (CTA): приглашение записаться — написать Алине в директ.
6. Хештеги: 4–6 штук одной строкой, строчными буквами
   (например: #перманентныймакияж #брови #губы #татуаж #алинацыганова).

Правила:
- Весь пост: 600–900 символов, обращение к читателям на «вы».
- Эмодзи: всего 4–7 на пост, не в каждое слово.
- Пост должен быть оригинальным и интересным, без шаблонных фраз.
- Если к запросу приложено фото работы — внимательно опиши, что на нём
  видишь (зона, оттенок, форма), и построй пост вокруг него.
- Запрещено: КАПС, больше 3 восклицательных знаков подряд,
  слова «уникальный, революционный» без фактов,
  хвастовство вроде «лучший мастер в мире»,
  медицинские диагнозы и стопроцентные гарантии результата."""


def fetch_product_info(url: str) -> str:
    """Скачивает страницу товара и забирает название + описание.

    Нейросеть сама ссылку открыть не может, поэтому сайт сначала
    читает страницу и передаёт ей готовый текст.
    """
    try:
        resp = requests.get(
            url,
            timeout=12,
            headers={"User-Agent": "Mozilla/5.0 (post-generator учебный проект)"},
        )
        resp.raise_for_status()
    except Exception:
        return ""  # страницу скачать не вышло — модель напишет пост по самой ссылке

    soup = BeautifulSoup(resp.text, "html.parser")
    parts = []

    if soup.title and soup.title.get_text(strip=True):
        parts.append("Название страницы: " + soup.title.get_text(strip=True))
    h1 = soup.find("h1")
    if h1 and h1.get_text(strip=True):
        parts.append("Заголовок: " + h1.get_text(strip=True))
    meta = soup.find("meta", attrs={"name": "description"})
    if meta and meta.get("content"):
        parts.append("Описание: " + meta["content"].strip())

    # Первые абзацы текста страницы (больше для модели не нужно)
    for p in soup.find_all("p")[:5]:
        text = p.get_text(strip=True)
        if len(text) > 40:
            parts.append(text)

    info = "\n".join(parts)
    return info[:2000]  # --- МЕНЯЙ ДЛИНУ ЗДЕСЬ: сколько текста отдавать модели ---


# --- МЕНЯЙ ФОРМАТЫ ЗДЕСЬ: какие фото можно загружать ---
# Все ходовые форматы изображений. Перед отправкой нейросети фото
# приводится к JPEG через Pillow, поэтому список можно расширять.
ALLOWED_EXTENSIONS = {"jpg", "jpeg", "png", "gif", "webp", "bmp", "tif", "tiff"}
# --- МЕНЯЙ ВЕС ЗДЕСЬ: максимальный размер фото ---
MAX_PHOTO_MB = 10


def process_uploaded_photo(file_storage):
    """Проверяет загруженное фото и готовит его для нейросети.

    Возвращает (base64_строка, ошибка). Если фото нет — (None, "").
    """
    if not file_storage or not file_storage.filename:
        return None, ""
    ext = file_storage.filename.rsplit(".", 1)[-1].lower() if "." in file_storage.filename else ""
    if ext not in ALLOWED_EXTENSIONS:
        return None, "Формат фото не поддерживается. Подходят: " + ", ".join(sorted(ALLOWED_EXTENSIONS))
    data = file_storage.read()
    if len(data) > MAX_PHOTO_MB * 1024 * 1024:
        return None, f"Фото слишком тяжёлое — нужно до {MAX_PHOTO_MB} МБ."
    try:
        img = Image.open(io.BytesIO(data))
        img = img.convert("RGB")  # единый формат для API
        # --- МЕНЯЙ РАЗМЕР ЗДЕСЬ: большие фото уменьшаем, так дешевле и быстрее ---
        img.thumbnail((1568, 1568))
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=85)
        return base64.b64encode(buf.getvalue()).decode(), ""
    except Exception:
        return None, "Не получилось прочитать фото — файл повреждён?"


def looks_like_url(text: str) -> bool:
    """Проверяет, ввёл ли пользователь ссылку, а не описание.

    Считаем ссылкой: http(s)://..., www.... или просто домен (example.com/tovar).
    Всё остальное (с пробелами, предложениями) — описание товара.
    """
    t = text.strip()
    if t.startswith(("http://", "https://", "www.")):
        return True
    return " " not in t and bool(re.match(r"^[\w-]+(\.[\w-]+)+(/.*)?$", t))


def generate_post_with_llm(product_context: str, mood_key: str, image_b64=None):
    """Просит языковую модель написать пост. Возвращает (пост, ошибка).

    image_b64 — фото в base64 (если загружено), модель должна понимать
    картинки. gpt-5-mini это умеет.
    """
    if not PROXY_API_KEY:
        return "", "Нет API-ключа: добавьте PROXY_API_KEY в файл .env"

    mood = MOODS.get(mood_key, MOODS["friendly"])

    # --- МЕНЯЙ ПРОМПТ ЗДЕСЬ: что просим у нейросети ---
    # product_context — это ссылка + текст со страницы, тема поста словами
    # пользователя и/или пометка о приложенном фото (см. index() ниже).
    user_prompt = (
        "Напиши пост для личного блога мастера перманентного макияжа.\n"
        f"Настроение поста: {mood['label']} ({mood['hint']}).\n"
        "Каждый раз пиши по-новому, оригинально, не повторяйся.\n"
        f"{product_context}\n"
    )

    # Если есть фото — текст и картинка едут одним сообщением (формат OpenAI)
    if image_b64:
        user_content = [
            {"type": "text", "text": user_prompt},
            {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{image_b64}"}},
        ]
    else:
        user_content = user_prompt

    # --- МЕНЯЙ ПАРАМЕТРЫ ЗДЕСЬ: temperature = оригинальность, выше = смелее ---
    payload = {
        "model": MODEL,  # --- МЕНЯЙ МОДЕЛЬ ЗДЕСЬ (см. MODEL выше) ---
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_content},
        ],
        "temperature": 0.9,
        # Внимание: новые модели (gpt-5-*) хотят max_completion_tokens вместо max_tokens.
        # Сначала пробуем современный вариант, при ошибке — старый.
        # reasoning_effort=low: рассуждающие модели тратят меньше токенов на
        # внутреннее рассуждение, иначе на сам ответ ничего не остаётся.
        # Лимит 3000 — с запасом под рассуждение + ответ.
        "reasoning_effort": "low",
        "max_completion_tokens": 3000,  # --- МЕНЯЙ ДЛИНУ ЗДЕСЬ: лимит ответа модели ---
    }

    def _send(data):
        return requests.post(
            PROXY_API_URL,
            timeout=60,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {PROXY_API_KEY}",
            },
            json=data,
        )

    try:
        resp = _send(payload)
        # Запасной вариант для старых моделей: max_tokens вместо max_completion_tokens
        if resp.status_code == 400 and "max_completion_tokens" in resp.text:
            payload["max_tokens"] = payload.pop("max_completion_tokens")
            resp = _send(payload)
        # Запасной вариант для строгих моделей: без temperature
        if resp.status_code == 400 and "temperature" in resp.text:
            payload.pop("temperature", None)
            resp = _send(payload)
        # Запасной вариант для моделей без reasoning_effort: убираем параметр
        if resp.status_code == 400 and "reasoning_effort" in resp.text:
            payload.pop("reasoning_effort", None)
            resp = _send(payload)
    except requests.Timeout:
        return "", "Нейросеть долго отвечает — попробуйте ещё раз."
    except Exception:
        return "", "Не получилось связаться с нейросетью — проверьте интернет."

    if resp.status_code == 200:
        data = resp.json()
        try:
            text = data["choices"][0]["message"]["content"].strip()
        except (KeyError, IndexError, AttributeError):
            text = ""
        if not text:
            return "", "Нейросеть вернула пустой ответ — попробуйте ещё раз."
        return text, ""
    if resp.status_code == 401:
        return "", "Ошибка доступа: проверьте API-ключ ProxyAPI в файле .env."
    if resp.status_code == 402:
        return "", "На балансе ProxyAPI нет средств — пополните баланс в разделе Биллинг."
    if resp.status_code == 403:
        return "", "Доступ запрещён: проверьте ограничения ключа или модель."
    if resp.status_code == 429:
        return "", "Слишком много запросов — подождите и попробуйте снова."
    return "", f"Ошибка нейросети ({resp.status_code}) — попробуйте позже."


# ============================================================
# ХРАНИЛИЩЕ БЕЗ БАЗЫ ДАННЫХ
# Избранное и отложенные посты лежат в обычных JSON-файлах
# рядом с app.py. Просто, наглядно, для учёбы — самое то.
# Фото хранится там же (base64-строкой) и уходит в Telegram вместе с текстом.
# ============================================================
BASE_DIR = os.path.dirname(__file__)
FAVORITES_FILE = os.path.join(BASE_DIR, "favorites.json")
SCHEDULED_FILE = os.path.join(BASE_DIR, "scheduled.json")


def _load_list(path):
    """Читает список из JSON-файла. Нет файла — возвращает пустой список."""
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
            return data if isinstance(data, list) else []
    except (FileNotFoundError, json.JSONDecodeError):
        return []


def _save_list(path, items):
    """Сохраняет список в JSON-файл."""
    with open(path, "w", encoding="utf-8") as f:
        json.dump(items, f, ensure_ascii=False, indent=2)


def load_favorites():
    """Все сохранённые в избранное посты (новые первые — решает вызывающий код)."""
    return _load_list(FAVORITES_FILE)


def save_favorites(items):
    _save_list(FAVORITES_FILE, items)


def load_scheduled():
    """Все отложенные посты."""
    return _load_list(SCHEDULED_FILE)


def save_scheduled(items):
    _save_list(SCHEDULED_FILE, items)


def _now_iso():
    """Текущее время строкой, удобно хранить в JSON."""
    return datetime.now().isoformat(timespec="seconds")


def _new_id():
    """Простой уникальный id без библиотек: миллисекунды времени."""
    return int(time.time() * 1000)


# ============================================================
# TELEGRAM: отправка постов через Bot API
# ============================================================
def telegram_configured():
    """True, если в .env заполнены и токен бота, и id чата."""
    return bool(TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID)


def send_to_telegram(text, image_b64=None):
    """Отправляет пост в Telegram. Возвращает (получилось, сообщение).

    Если приложено фото (image_b64) — уходит sendPhoto: картинка + текст
    подписью под ней. Без фото — обычное текстовое сообщение.
    Сообщения об ошибках — простыми словами: что случилось и что делать.
    """
    if not telegram_configured():
        return False, "Telegram не подключён: добавьте TELEGRAM_BOT_TOKEN и TELEGRAM_CHAT_ID в файл .env и перезапустите сайт (подробно — в README)."
    url_base = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"
    try:
        if image_b64:
            # Фото едет файлом (multipart), текст — подписью к нему.
            # Лимит подписи в Telegram — 1024 символа, наши посты короче.
            photo_bytes = base64.b64decode(image_b64)
            resp = requests.post(
                url_base + "/sendPhoto",
                data={"chat_id": TELEGRAM_CHAT_ID, "caption": text},
                files={"photo": ("photo.jpg", photo_bytes, "image/jpeg")},
                timeout=30,
            )
            # Запасной вариант: подпись не влезла — шлём текст и фото отдельно
            if resp.status_code == 400 and "caption" in resp.text:
                r1 = requests.post(url_base + "/sendMessage",
                                   json={"chat_id": TELEGRAM_CHAT_ID, "text": text},
                                   timeout=20)
                r2 = requests.post(url_base + "/sendPhoto",
                                   data={"chat_id": TELEGRAM_CHAT_ID},
                                   files={"photo": ("photo.jpg", photo_bytes, "image/jpeg")},
                                   timeout=30)
                if r1.status_code == 200 and r2.status_code == 200:
                    return True, "Пост с фото опубликован в Telegram!"
                resp = r1 if r1.status_code != 200 else r2
            elif resp.status_code == 200:
                return True, "Пост с фото опубликован в Telegram!"
        else:
            resp = requests.post(
                url_base + "/sendMessage",
                json={"chat_id": TELEGRAM_CHAT_ID, "text": text},
                timeout=20,
            )
            if resp.status_code == 200:
                return True, "Пост опубликован в Telegram!"
    except Exception:
        return False, "Не получилось связаться с Telegram — проверьте интернет."
    try:
        desc = resp.json().get("description", "")
    except Exception:
        desc = ""
    if resp.status_code == 401:
        return False, "Telegram не узнал токен (401): проверьте TELEGRAM_BOT_TOKEN в .env."
    if "chat not found" in desc:
        return False, "Чат не найден: проверьте TELEGRAM_CHAT_ID и что бот добавлен в канал или группу (подробно — в README)."
    if "blocked" in desc:
        return False, "Бот заблокирован: откройте чат с ботом и нажмите Start."
    if "not enough rights" in desc or "administrator" in desc:
        return False, "Не хватает прав: сделайте бота администратором канала."
    return False, f"Telegram вернул ошибку: {desc or resp.status_code}."


# ============================================================
# ОТЛОЖЕННЫЕ ПОСТЫ: фоновая проверка раз в 30 секунд
# ============================================================
_scheduler_started = False


def _send_due_scheduled():
    """Находит отложенные посты, чьё время пришло, и публикует их в Telegram."""
    items = load_scheduled()
    now = datetime.now()
    changed = False
    for item in items:
        if item.get("status") != "pending":
            continue
        try:
            send_at = datetime.fromisoformat(item["send_at"])
        except (KeyError, ValueError):
            item["status"] = "error"
            item["note"] = "Неверная дата."
            changed = True
            continue
        if send_at <= now:
            ok, msg = send_to_telegram(item["text"], item.get("image_b64"))
            item["status"] = "sent" if ok else "error"
            item["note"] = msg
            item["sent_at"] = _now_iso()
            changed = True
    if changed:
        save_scheduled(items)


def _scheduler_loop():
    """Бесконечный цикл проверки. Живёт в отдельном потоке, сайту не мешает."""
    while True:
        try:
            _send_due_scheduled()
        except Exception:
            pass  # одна неудачная проверка не должна ронять планировщик
        time.sleep(30)  # --- МЕНЯЙ ЗДЕСЬ: как часто проверять (в секундах) ---


def start_scheduler_once():
    """Запускает планировщик один раз. Защита от двойного запуска нужна,
    потому что Flask в debug-режиме перезапускает код дважды."""
    global _scheduler_started
    if _scheduler_started:
        return
    # WERKZEUG_RUN_MAIN=true — только у «настоящего» процесса, а не у наблюдателя
    if os.environ.get("WERKZEUG_RUN_MAIN") != "true":
        return
    _scheduler_started = True
    threading.Thread(target=_scheduler_loop, daemon=True).start()


def _render_index(product_input="", mood="friendly", post="", error="", notice="", notice_ok=True, photo_b64=None):
    """Общая отрисовка главной — чтобы не дублировать render в каждом роуте.

    photo_b64 — фото из генерации (если было): прячем его в скрытые поля
    кнопок, чтобы оно ушло дальше — в публикацию, избранное или отложку.
    """
    return render_template(
        "index.html",
        product_input=product_input,
        mood=mood,
        moods=MOODS,
        post=post,
        error=error,
        notice=notice,
        notice_ok=notice_ok,
        photo_b64=photo_b64,
    )


@app.route("/", methods=["GET", "POST"])
def index():
    """Главная страница: ссылка на работу ИЛИ тема поста + готовый пост."""
    product_input = ""
    mood = "friendly"
    post = ""
    error = ""

    if request.method == "POST":
        product_input = request.form.get("product_input", "").strip()
        mood = request.form.get("mood", "friendly")

        # Вариант 3: фото работы (необязательно, можно вместе с текстом)
        photo_b64, photo_error = process_uploaded_photo(request.files.get("photo"))
        if photo_error:
            error = photo_error
        # Проверка обязательного поля: нужен хоть один вариант из трёх
        elif not product_input and not photo_b64:
            error = "Добавьте ссылку, тему поста или фото"
        else:
            context = ""
            if product_input and looks_like_url(product_input):
                # Вариант 1: пользователь вставил ссылку — читаем страницу
                url = product_input
                # Если схему забыли (вставили "example.com/..."), добавляем https://
                if not url.startswith(("http://", "https://")):
                    url = "https://" + url
                page_info = fetch_product_info(url)
                context += f"Ссылка для поста: {url}\n"
                if page_info:
                    context += f"Информация со страницы:\n{page_info}\n"
                else:
                    context += "Страницу прочитать не удалось — напиши пост по самой ссылке.\n"
            elif product_input:
                # Вариант 2: пользователь написал тему поста — отдаём текст как есть
                context += f"Тема поста от Алины:\n{product_input}\n"
            if photo_b64:
                # Вариант 3: приложено фото — модель опишет его в посте
                context += "К запросу приложено фото работы: опиши, что на нём видишь, и построй пост вокруг него.\n"
            post, error = generate_post_with_llm(context, mood, photo_b64)

    return _render_index(
        product_input=product_input,
        mood=mood,
        post=post,
        error=error,
        photo_b64=photo_b64 if post else None,
    )


@app.route("/publish", methods=["POST"])
def publish():
    """Кнопка «Опубликовать пост»: отправляет текст (+ фото, если было) в Telegram."""
    post_text = request.form.get("post_text", "").strip()
    product_input = request.form.get("product_input", "")
    mood = request.form.get("mood", "friendly")
    back = request.form.get("back", "")  # откуда нажали: "" или "favorites"
    photo_b64 = request.form.get("photo_b64", "").strip() or None
    if not post_text:
        return _render_index(product_input, mood, "", "Сначала сформируйте пост.")
    ok, msg = send_to_telegram(post_text, photo_b64)
    if back == "favorites":
        return render_template("favorites.html", items=load_favorites(),
                               moods=MOODS, notice=msg, notice_ok=ok)
    # Текст оставляем на экране, чтобы было видно, что ушло в Telegram
    return _render_index(product_input, mood, post_text, "", msg, ok, photo_b64)


@app.route("/favorite", methods=["POST"])
def favorite():
    """Кнопка «В избранное»: сохраняет пост (+ фото) в файл favorites.json."""
    post_text = request.form.get("post_text", "").strip()
    product_input = request.form.get("product_input", "")
    mood = request.form.get("mood", "friendly")
    photo_b64 = request.form.get("photo_b64", "").strip() or None
    if not post_text:
        return _render_index(product_input, mood, "", "Сначала сформируйте пост.")
    items = load_favorites()
    # Новые — в начало списка
    item = {"id": _new_id(), "text": post_text, "mood": mood, "created_at": _now_iso()}
    if photo_b64:
        item["image_b64"] = photo_b64
    items.insert(0, item)
    save_favorites(items)
    return _render_index(product_input, mood, post_text, "", "Пост сохранён в избранное ⭐", True, photo_b64)


@app.route("/schedule", methods=["POST"])
def schedule():
    """Кнопка «Отложить»: сохраняет пост (+ фото) с датой публикации в scheduled.json."""
    post_text = request.form.get("post_text", "").strip()
    product_input = request.form.get("product_input", "")
    mood = request.form.get("mood", "friendly")
    photo_b64 = request.form.get("photo_b64", "").strip() or None
    send_at_raw = request.form.get("send_at", "").strip()
    if not post_text:
        return _render_index(product_input, mood, "", "Сначала сформируйте пост.")
    try:
        send_at = datetime.fromisoformat(send_at_raw)
    except ValueError:
        return _render_index(product_input, mood, post_text, "", "Выберите дату и время публикации.", False, photo_b64)
    if send_at <= datetime.now():
        return _render_index(product_input, mood, post_text, "", "Время должно быть в будущем.", False, photo_b64)
    items = load_scheduled()
    item = {"id": _new_id(), "text": post_text, "mood": mood,
            "send_at": send_at.isoformat(timespec="minutes"),
            "status": "pending", "created_at": _now_iso()}
    if photo_b64:
        item["image_b64"] = photo_b64
    items.append(item)
    save_scheduled(items)
    pretty = send_at.strftime("%d.%m.%Y в %H:%M")
    return _render_index(product_input, mood, post_text, "", f"Пост отложен — выйдет {pretty} ⏰", True, photo_b64)


@app.route("/favorites")
def favorites():
    """Страница «Избранное»: все сохранённые посты."""
    return render_template("favorites.html", items=load_favorites(),
                           moods=MOODS, notice="", notice_ok=True)


@app.route("/favorites/delete", methods=["POST"])
def favorites_delete():
    """Удаление поста из избранного."""
    try:
        del_id = int(request.form.get("id", "0"))
    except ValueError:
        del_id = 0
    save_favorites([i for i in load_favorites() if i.get("id") != del_id])
    return render_template("favorites.html", items=load_favorites(),
                           moods=MOODS, notice="Удалено из избранного.", notice_ok=True)


@app.route("/scheduled")
def scheduled():
    """Страница «Отложенные»: что ждёт публикации, а что уже вышло."""
    items = sorted(load_scheduled(), key=lambda i: i.get("send_at", ""))
    return render_template("scheduled.html", items=items, moods=MOODS)


@app.route("/scheduled/cancel", methods=["POST"])
def scheduled_cancel():
    """Отмена отложенного поста (только пока он ждёт своей очереди)."""
    try:
        del_id = int(request.form.get("id", "0"))
    except ValueError:
        del_id = 0
    save_scheduled([i for i in load_scheduled() if i.get("id") != del_id])
    items = sorted(load_scheduled(), key=lambda i: i.get("send_at", ""))
    return render_template("scheduled.html", items=items, moods=MOODS)


if __name__ == "__main__":
    # Фоновый планировщик отложенных постов (раздел выше).
    start_scheduler_once()
    # debug=True — удобно для учёбы: ошибки видно в браузере
    # Порт 5001, потому что 5000 занят первым проектом (товары).
    app.run(debug=True, port=5001)
