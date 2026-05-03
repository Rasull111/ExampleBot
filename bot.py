import asyncio
import logging
import json
import os
import sys
from zoneinfo import ZoneInfo
from datetime import datetime, timedelta
from aiogram import Bot, Dispatcher, F
from aiogram.types import Message, ReplyKeyboardMarkup, KeyboardButton, InlineKeyboardMarkup, InlineKeyboardButton, ReplyKeyboardRemove
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application
from aiohttp import web
import gspread
from google.oauth2.service_account import Credentials
from enum import Enum
from collections import defaultdict, Counter

# --- КЭШИРОВАНИЕ НАСТРОЕК ---
SETTINGS_CACHE = {
    "data": None,
    "timestamp": None,
    "ttl": 300  # 5 минут
}

def normalize_date(date_str: str) -> str:
    """Нормализовать дату к формату без лидирующих нулей"""
    if not date_str:
        return date_str
    
    parts = str(date_str).split('.')
    if len(parts) == 2:
        day, month = parts
        return f"{int(day)}.{int(month)}"
    return str(date_str).strip()

def load_settings():
    """Загрузить настройки из Google Sheets с кэшированием"""
    now = datetime.now()
    
    # Если кэш еще свежий - используем его
    if (SETTINGS_CACHE["data"] is not None and 
        SETTINGS_CACHE["timestamp"] is not None and
        (now - SETTINGS_CACHE["timestamp"]).seconds < SETTINGS_CACHE["ttl"]):
        return SETTINGS_CACHE["data"]
    
    # Иначе загружаем с Google Sheets
    try:
        worksheet = get_settings_sheet()
        cell_value = worksheet.cell(1, 1).value
        if cell_value:
            data = json.loads(cell_value)
            # Обновляем кэш
            SETTINGS_CACHE["data"] = data
            SETTINGS_CACHE["timestamp"] = now
            return data
    except Exception as e:
        logging.error(f"Ошибка при загрузке настроек: {e}")
        # Если ошибка - возвращаем старые данные из кэша
        if SETTINGS_CACHE["data"] is not None:
            return SETTINGS_CACHE["data"]
    
    return DEFAULT_SETTINGS.copy()

def save_settings(data):
    """Сохранить настройки в Google Sheets и обновить кэш"""
    try:
        worksheet = get_settings_sheet()
        settings_json = json.dumps(data, ensure_ascii=False, indent=2)
        worksheet.update_cell(1, 1, settings_json)
        
        # Обновляем кэш
        SETTINGS_CACHE["data"] = data
        SETTINGS_CACHE["timestamp"] = datetime.now()
        
        logging.info("✅ Настройки сохранены")
    except Exception as e:
        logging.error(f"❌ Ошибка при сохранении настроек: {e}")
# --- КОНФИГУРАЦИЯ ---
API_TOKEN = os.getenv("BOT_TOKEN")
SPREADSHEET_ID = os.getenv("SPREADSHEET_ID")
ADMIN_IDS = [5544514086]
WEBHOOK_HOST = "https://examplebot-production-e157.up.railway.app"
WEBHOOK_PATH = f"/{API_TOKEN}"
WEBAPP_HOST = "0.0.0.0"
WEBAPP_PORT = int(os.getenv("PORT", 5000))
LOCAL_TZ = ZoneInfo("Asia/Almaty")

def now_local():
    return datetime.now(LOCAL_TZ).replace(tzinfo=None)


# --- УСЛУГИ ---
AVAILABLE_SERVICES = ["Чистка лица", "Бритье бороды", "Взрослая стрижка"]
SERVICE_REMINDING_DAYS = 60  # Напоминание через 60 дней (2 месяца)

# --- ЗАЩИТА ОТ RACE CONDITION ---
RESERVED_SLOTS = {}  # {(master, date, time): {'user_id': id, 'expiry': datetime}}
SLOT_RESERVATION_TIMEOUT = 30  # секунды
MAX_ACTIVE_BOOKINGS_PER_USER = 1

# --- ТИПЫ МЕДИА ---
class MediaType(Enum):
    TEXT = "text"
    PHOTO = "photo"
    VIDEO = "video"
    AUDIO = "audio"
    DOCUMENT = "document"

# --- СОСТОЯНИЯ (FSM) ---
class Registration(StatesGroup):
    waiting_for_name = State()
    waiting_for_phone = State()

class Booking(StatesGroup):
    waiting_for_master = State()
    waiting_for_date = State()
    waiting_for_time = State()
    waiting_for_services = State()

class ManageBooking(StatesGroup):
    selecting_booking = State()
    action = State()

class AdminSettings(StatesGroup):
    # Редактирование прайса
    editing_prices = State()
    editing_prices_content = State()
    
    # Редактирование адреса
    editing_address = State()
    editing_address_content = State()
    
    # Добавление мастера
    adding_master = State()
    adding_master_name = State()
    adding_master_content = State()
    
    # Рассылка
    mailing_select_services = State()
    mailing_content = State()
    mailing_confirm = State()

# --- КОНФИГУРАЦИЯ ПО УМОЛЧАНИЮ ---
DEFAULT_SETTINGS = {
    "prices": [{"type": "text", "value": "<b>✂️ Прайс-лист:</b>\n\n Стрижка — 1000\n Борода — 600\n Комплекс — 1400"}],
    "address": [{"type": "text", "value": "<b>📍 Наш адрес:</b>\n<a href='https://2gis.kz/almaty/geo/70000001017272265/76.976480,43.230915'>ул. Ибраимова, 115</a>\n\n<b>⏰ График работы:</b>\nЕжедневно: 10:00 - 21:00"}],
    "masters_info": [],
    "masters_names": []
}

# --- ЛОГИКА НАСТРОЕК ---
def get_settings_sheet():
    """Получить или создать таблицу Settings"""
    try:
        return get_sheet("Settings")
    except:
        return get_sheet("Settings")

logging.basicConfig(level=logging.INFO)
bot = Bot(token=API_TOKEN)
dp = Dispatcher()

# --- ТАБЛИЦЫ ---
def get_sheet(name="Users"):
    creds_json = os.environ.get("GOOGLE_CREDENTIALS")
    creds_dict = json.loads(creds_json)
    creds = Credentials.from_service_account_info(creds_dict, scopes=["https://www.googleapis.com/auth/spreadsheets"])
    client = gspread.authorize(creds)
    sheet = client.open_by_key(SPREADSHEET_ID)
    try:
        return sheet.worksheet(name)
    except:
        return sheet.add_worksheet(title=name, rows="1000", cols="10")

def get_user(user_id):
    try:
        records = get_sheet("Users").get_all_records()
        for row in records:
            if str(row.get('Telegram ID')) == str(user_id):
                return row
    except:
        pass
    return None

def get_all_users():
    """Получить всех пользователей"""
    try:
        return get_sheet("Users").get_all_records()
    except:
        return []

def get_bookings():    
    """Получить все записи"""
    try:
        return get_sheet("Bookings").get_all_records()
    except:
        return []

def parse_booking_datetime(date_str: str, time_str: str):
    try:
        now = now_local() if "now_local" in globals() else datetime.now()
        day, month = map(int, normalize_date(date_str).split("."))
        hour, minute = map(int, str(time_str).split(":"))

        booking_dt = datetime(now.year, month, day, hour, minute)

        if booking_dt < now - timedelta(days=1):
            booking_dt = booking_dt.replace(year=now.year + 1)

        return booking_dt
    except Exception:
        return None


def is_future_booking(row) -> bool:
    booking_dt = parse_booking_datetime(row.get("Дата"), row.get("Время"))
    if not booking_dt:
        return False

    now = now_local() if "now_local" in globals() else datetime.now()
    return booking_dt > now


def get_active_user_bookings(user_id):
    bookings = []
    for row_index, row in enumerate(get_bookings(), start=2):
        if str(row.get("Telegram ID")) == str(user_id) and is_future_booking(row):
            bookings.append((row_index, row))
    return bookings


def user_can_create_booking(user_id) -> bool:
    return len(get_active_user_bookings(user_id)) < MAX_ACTIVE_BOOKINGS_PER_USER


def check_slot_availability_from_records(records, master: str, date: str, time: str) -> bool:
    master = str(master).strip()
    date = normalize_date(date)
    time = str(time).strip()

    key = (master, date, time)
    if key in RESERVED_SLOTS:
        return False

    for row in records:
        row_master = str(row.get("Мастер", "")).strip()
        row_date = normalize_date(row.get("Дата", ""))
        row_time = str(row.get("Время", "")).strip()

        if row_master == master and row_date == date and row_time == time:
            return False

    return True


def update_reminder_status(telegram_id, reminder_sent=True):
    """Обновить статус напоминания в таблице"""
    try:
        worksheet = get_sheet("Bookings")
        records = worksheet.get_all_records()
        
        for idx, row in enumerate(records, start=2):  # start=2 потому что row 1 это header
            if str(row.get('Telegram ID')) == str(telegram_id):
                if reminder_sent:
                    worksheet.update_cell(idx, worksheet.find("Напоминания").col, "✓")
    except Exception as e:
        logging.error(f"Ошибка при обновлении статуса напоминания: {e}")

# --- ЗАЩИТА ОТ RACE CONDITION ---
def cleanup_expired_reservations():
    """Удалить истекшие резервирования"""
    now = datetime.now()
    expired_keys = [
        key for key, value in RESERVED_SLOTS.items()
        if value['expiry'] < now
    ]
    for key in expired_keys:
        del RESERVED_SLOTS[key]

def check_slot_availability(master: str, date: str, time: str) -> bool:
    """Проверить, свободен ли слот"""
    cleanup_expired_reservations()
    
    master = str(master).strip()
    date = normalize_date(date)  # 👈 НОРМАЛИЗУЙ ДАТУ
    time = str(time).strip()
    
    key = (master, date, time)
    if key in RESERVED_SLOTS:
        logging.info(f"⚠️ Слот {key} зарезервирован в памяти")
        return False
    
    try:
        records = get_bookings()
        
        for idx, row in enumerate(records):
            row_master = str(row.get('Мастер', '')).strip()
            row_date = normalize_date(row.get('Дата', ''))  # 👈 НОРМАЛИЗУЙ ДАТУ
            row_time = str(row.get('Время', '')).strip()
            
            if row_master == master and row_date == date and row_time == time:
                logging.info(f"❌ СЛОТ ЗАНЯТ: {master}/{date}/{time}")
                return False
        
        logging.info(f"✅ СЛОТ СВОБОДЕН: {master}/{date}/{time}")
        return True
        
    except Exception as e:
        logging.error(f"❌ Ошибка проверки: {e}")
        return False
    
def reserve_slot(master: str, date: str, time: str, user_id: int) -> bool:
    """Зарезервировать слот для пользователя"""
    cleanup_expired_reservations()
    
    master = str(master).strip()
    date = normalize_date(date)  # 👈 НОРМАЛИЗУЙ ДАТУ
    time = str(time).strip()
    
    key = (master, date, time)
    
    if key in RESERVED_SLOTS:
        if RESERVED_SLOTS[key]['user_id'] != user_id:
            return False  # Слот зарезервирован другим пользователем
    
    RESERVED_SLOTS[key] = {
        'user_id': user_id,
        'expiry': datetime.now() + timedelta(seconds=SLOT_RESERVATION_TIMEOUT)
    }
    return True

def release_slot(master: str, date: str, time: str, user_id: int):
    """Освободить слот после успешной записи"""
    key = (master, date, time)
    if key in RESERVED_SLOTS and RESERVED_SLOTS[key]['user_id'] == user_id:
        del RESERVED_SLOTS[key]

# --- АНАЛИТИКА ---
def get_analytics():
    """Получить статистику"""
    bookings = get_bookings()
    today = datetime.now().date()
    yesterday = today - timedelta(days=1)
    week_ago = today - timedelta(days=7)
    month_start = today.replace(day=1)
    
    analytics = {
        'today': 0,
        'yesterday': 0,
        'week': 0,
        'month': 0,
        'services': defaultdict(int),
        'services_list': defaultdict(list)  # Для отслеживания кто какие услуги использовал
    }
    
    seen_today = set()
    seen_yesterday = set()
    seen_week = set()
    seen_month = set()
    
    for row in bookings:
        try:
            # Парсим дату в формате DD.MM
            date_str = str(row.get('Дата', ''))
            if len(date_str) != 5 or date_str[2] != '.':
                continue
            
            day, month = map(int, date_str.split('.'))
            # Предполагаем текущий год
            booking_date = datetime(today.year, month, day).date()
            
            user_id = str(row.get('Telegram ID'))
            services = str(row.get('Услуги', '')).strip()
            
            if not services:
                continue
            
            # Считаем уникальных клиентов по дням
            if booking_date == today and user_id not in seen_today:
                analytics['today'] += 1
                seen_today.add(user_id)
            
            if booking_date == yesterday and user_id not in seen_yesterday:
                analytics['yesterday'] += 1
                seen_yesterday.add(user_id)
            
            if week_ago <= booking_date <= today and user_id not in seen_week:
                analytics['week'] += 1
                seen_week.add(user_id)
            
            if month_start <= booking_date <= today and user_id not in seen_month:
                analytics['month'] += 1
                seen_month.add(user_id)
            
            # Считаем услуги
            for service in services.split(','):
                service = service.strip()
                if service:
                    analytics['services'][service] += 1
                    if service not in analytics['services_list']:
                        analytics['services_list'][service] = []
                    analytics['services_list'][service].append(user_id)
        
        except Exception as e:
            logging.error(f"Ошибка при парсинге записи: {e}")
    
    return analytics

def get_users_by_service(service: str):
    """Получить всех пользователей, использовавших конкретную услугу"""
    bookings = get_bookings()
    users = {}
    
    logging.info(f"🔍 Ищу '{service}'")
    logging.info(f"Всего записей: {len(bookings)}")
    
    if bookings:
        first_row = bookings[0]
        logging.info(f"📋 Структура записи: {list(first_row.keys())}")
    
    for idx, row in enumerate(bookings):
        try:
            # ✅ ИСПРАВЛЕНО: используем правильное название колонки
            telegram_id = str(row.get('Telegram ID', ''))  # ← было 'ID'
            services_value = row.get('Услуги', '')
            
            if not telegram_id or not services_value:
                continue
            
            services_str = str(services_value).strip().lower()
            search_service = service.lower()
            
            if search_service in services_str:
                if telegram_id not in users:
                    users[telegram_id] = {
                        'name': row.get('ФИО', 'Unknown'),  # ← было 'Имя'
                        'phone': row.get('Телефон', 'Unknown')
                    }
                logging.info(f"✅ Найден пользователь: {telegram_id}")
        
        except Exception as e:
            logging.error(f"Ошибка при обработке записи {idx}: {e}")
    
    logging.info(f"📈 Итого найдено пользователей: {len(users)}")
    return users


def check_slot_availability(master: str, date: str, time: str) -> bool:
    """Проверить, свободен ли слот"""
    cleanup_expired_reservations()
    
    # Нормализуем входные параметры
    master = str(master).strip()
    date = str(date).strip()
    time = str(time).strip()
    
    key = (master, date, time)
    if key in RESERVED_SLOTS:
        logging.info(f"⚠️ Слот {key} зарезервирован в памяти")
        return False
    
    try:
        records = get_bookings()
        for row in records:
            row_master = str(row.get('Мастер', '')).strip()
            row_date = str(row.get('Дата', '')).strip()
            row_time = str(row.get('Время', '')).strip()
            
            # Точное совпадение после нормализации
            if row_master == master and row_date == date and row_time == time:
                logging.info(f"❌ Слот {master}/{date}/{time} уже забронирован в таблице")
                return False
    except Exception as e:
        logging.error(f"Ошибка при проверке доступности: {e}")
    
    logging.info(f"✅ Слот {master}/{date}/{time} СВОБОДЕН")
    return True

# --- КЛАВИАТУРЫ ---
def main_menu():
    return ReplyKeyboardMarkup(keyboard=[
        [KeyboardButton(text="📝 Записаться")],
        [KeyboardButton(text="📋 Управление записями")],
        [KeyboardButton(text="💰 Цены"), KeyboardButton(text="👤 Мастера")],
        [KeyboardButton(text="📍 Адрес и график"), KeyboardButton(text="📞 Поддержка")]
    ], resize_keyboard=True)


def admin_kb():
    return ReplyKeyboardMarkup(keyboard=[
        [KeyboardButton(text="⚙️ Изменить Цены"), KeyboardButton(text="⚙️ Изменить Адрес")],
        [KeyboardButton(text="➕ Добавить Мастера"), KeyboardButton(text="🗑 Очистить Мастеров")],
        [KeyboardButton(text="📊 Аналитика"), KeyboardButton(text="📢 Рассылка")],
        [KeyboardButton(text="⬅️ Выйти")]
    ], resize_keyboard=True)

def masters_kb():
    names = load_settings().get("masters_names", [])
    if not names:
        return ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text="⬅️ Отмена")]], resize_keyboard=True)
    buttons = [[KeyboardButton(text=name)] for name in names]
    buttons.append([KeyboardButton(text="⬅️ Отмена")])
    return ReplyKeyboardMarkup(keyboard=buttons, resize_keyboard=True)

def format_date_display(date_str: str) -> str:
    """Преобразует дату Д.М в красивый вид: 2 мая."""
    months_ru = [
        "января", "февраля", "марта", "апреля",
        "мая", "июня", "июля", "августа",
        "сентября", "октября", "ноября", "декабря"
    ]

    try:
        day, month = map(int, normalize_date(date_str).split("."))
        return f"{day} {months_ru[month - 1]}"
    except Exception:
        return str(date_str)


def parse_date(date_text: str) -> str:
    text = str(date_text).strip()
    today = datetime.now()

    quick_dates = {
        "Сегодня": today,
        "Завтра": today + timedelta(days=1),
        "Послезавтра": today + timedelta(days=2),
    }

    if text in quick_dates:
        date_obj = quick_dates[text]
        return f"{date_obj.day}.{date_obj.month}"

    months_ru = [
        "января", "февраля", "марта", "апреля",
        "мая", "июня", "июля", "августа",
        "сентября", "октября", "ноября", "декабря"
    ]

    parts = text.split()
    if len(parts) == 2 and parts[0].isdigit() and parts[1] in months_ru:
        day = int(parts[0])
        month = months_ru.index(parts[1]) + 1
        return f"{day}.{month}"

    return normalize_date(text)

def dates_kb():
    months_ru = [
        "января", "февраля", "марта", "апреля",
        "мая", "июня", "июля", "августа",
        "сентября", "октября", "ноября", "декабря"
    ]

    buttons = []
    today = now_local()

    for i in range(7):
        date_obj = today + timedelta(days=i)

        if i == 0:
            text = "Сегодня"
        elif i == 1:
            text = "Завтра"
        elif i == 2:
            text = "Послезавтра"
        else:
            day = date_obj.day
            month = months_ru[date_obj.month - 1]
            text = f"{day} {month}"

        buttons.append(KeyboardButton(text=text))

    return ReplyKeyboardMarkup(
        keyboard=[
            buttons[0:3],   # Сегодня / Завтра / Послезавтра
            buttons[3:5],   # даты
            buttons[5:7],   # даты
            [KeyboardButton(text="⬅️ Отмена")]
        ],
        resize_keyboard=True
    )
def times_kb(master: str, date: str):
    times = [
        "10:00", "11:00", "12:00", "13:00", "14:00", "15:00",
        "16:00", "17:00", "18:00", "19:00", "20:00"
    ]

    available_times = []
    master = str(master).strip()
    date = normalize_date(date)

    now = now_local() if "now_local" in globals() else datetime.now()
    today_str = f"{now.day}.{now.month}"
    is_today = normalize_date(date) == today_str

    records = get_bookings()

    for time in times:
        if is_today:
            hour, minute = map(int, time.split(":"))
            slot_dt = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
            if slot_dt <= now:
                continue

        if check_slot_availability_from_records(records, master, date, time):
            available_times.append(KeyboardButton(text=time))

    if not available_times:
        return ReplyKeyboardMarkup(
            keyboard=[[KeyboardButton(text="⬅️ Назад")]],
            resize_keyboard=True
        )

    keyboard = []
    for i in range(0, len(available_times), 2):
        keyboard.append(available_times[i:i + 2])

    keyboard.append([KeyboardButton(text="⬅️ Назад")])

    return ReplyKeyboardMarkup(keyboard=keyboard, resize_keyboard=True)

def services_kb(multi_select=False):
    """Клавиатура для выбора услуг"""
    buttons = [[KeyboardButton(text=service)] for service in AVAILABLE_SERVICES]
    if multi_select:
        buttons.append([KeyboardButton(text="✅ Готово")])
    buttons.append([KeyboardButton(text="⬅️ Отмена")])
    return ReplyKeyboardMarkup(keyboard=buttons, resize_keyboard=True)

def services_mailing_kb():
    """Клавиатура для выбора услуг для рассылки"""
    buttons = [[KeyboardButton(text=f"📌 {service}")] for service in AVAILABLE_SERVICES]
    buttons.append([KeyboardButton(text="✅ Готово")])
    buttons.append([KeyboardButton(text="⬅️ Отмена")])
    return ReplyKeyboardMarkup(keyboard=buttons, resize_keyboard=True)

def edit_content_kb():
    """Клавиатура для редактирования контента"""
    return ReplyKeyboardMarkup(keyboard=[
        [KeyboardButton(text="📝 Добавить текст")],
        [KeyboardButton(text="🖼 Добавить фото"), KeyboardButton(text="🎬 Добавить видео")],
        [KeyboardButton(text="🎵 Добавить аудио"), KeyboardButton(text="📄 Добавить документ")],
        [KeyboardButton(text="✅ Сохранить"), KeyboardButton(text="❌ Отмена")]
    ], resize_keyboard=True)

def mailing_content_kb():
    """Клавиатура для рассылки"""
    return ReplyKeyboardMarkup(keyboard=[
        [KeyboardButton(text="📝 Добавить текст")],
        [KeyboardButton(text="🖼 Добавить фото"), KeyboardButton(text="🎬 Добавить видео")],
        [KeyboardButton(text="🎵 Добавить аудио"), KeyboardButton(text="📄 Добавить документ")],
        [KeyboardButton(text="✅ Отправить рассылку"), KeyboardButton(text="❌ Отмена")]
    ], resize_keyboard=True)

def cancel_kb():
    return ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text="❌ Отмена")]], resize_keyboard=True)

# --- ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ---
async def send_complex_content(chat_id, content_data):
    """Отправить сложный контент в чат"""
    if not content_data:
        await bot.send_message(chat_id, "Раздел пуст.")
        return
    
    for item in content_data:
        try:
            if item['type'] == 'text':
                await bot.send_message(chat_id, item['value'], parse_mode="HTML", disable_web_page_preview=False)
            elif item['type'] == 'photo':
                await bot.send_photo(chat_id, item['value'], caption=item.get('caption'), parse_mode="HTML")
            elif item['type'] == 'video':
                await bot.send_video(chat_id, item['value'], caption=item.get('caption'), parse_mode="HTML")
            elif item['type'] == 'audio':
                await bot.send_audio(chat_id, item['value'], title=item.get('caption'))
            elif item['type'] == 'document':
                await bot.send_document(chat_id, item['value'], caption=item.get('caption'), parse_mode="HTML")
        except Exception as e:
            logging.error(f"Ошибка при отправке контента: {e}")

async def send_complex_content_to_message(message: Message, content_data):
    """Отправить сложный контент в ответ на сообщение"""
    if not content_data:
        return await message.answer("Раздел пуст.")
    for item in content_data:
        if item['type'] == 'text':
            await message.answer(item['value'], parse_mode="HTML", disable_web_page_preview=False)
        elif item['type'] == 'photo':
            await message.answer_photo(item['value'], caption=item.get('caption'), parse_mode="HTML")
        elif item['type'] == 'video':
            await message.answer_video(item['value'], caption=item.get('caption'), parse_mode="HTML")
        elif item['type'] == 'audio':
            await message.answer_audio(item['value'], title=item.get('caption'))
        elif item['type'] == 'document':
            await message.answer_document(item['value'], caption=item.get('caption'), parse_mode="HTML")

async def preview_content(message: Message, content_data):
    """Показать превью контента перед сохранением"""
    await message.answer("📋 <b>Предпросмотр контента:</b>", parse_mode="HTML")
    await send_complex_content_to_message(message, content_data)

def is_valid_button_input(text):
    """Проверить, не нажата ли кнопка управления"""
    buttons = ["📝 Добавить текст", "🖼 Добавить фото", "🎬 Добавить видео", 
               "🎵 Добавить аудио", "📄 Добавить документ", "✅ Сохранить", "❌ Отмена",
               "✅ Готово", "✅ Отправить рассылку"]
    return text in buttons

def is_time_to_remind(date_str: str, time_str: str) -> bool:
    try:
        now = now_local()

        day, month = map(int, date_str.split('.'))
        hour, minute = map(int, time_str.split(':'))

        booking_dt = datetime(now.year, month, day, hour, minute)
        diff = booking_dt - now

        return 0 < diff.total_seconds() <= 3600

    except:
        return False


def update_reminder_status_by_row(row_index):
    try:
        worksheet = get_sheet("Bookings")
        col = worksheet.find("Напоминания").col
        worksheet.update_cell(row_index, col, "✓")
    except Exception as e:
        logging.error(f"Ошибка обновления напоминания: {e}")


async def reminder_worker():
    while True:
        try:
            bookings = get_bookings()

            for idx, row in enumerate(bookings, start=2):
                telegram_id = row.get('Telegram ID')
                date = row.get('Дата')
                time = row.get('Время')
                reminder_sent = row.get('Напоминания')

                if not telegram_id or not date or not time:
                    continue

                if reminder_sent == "✓":
                    continue

                if is_time_to_remind(date, time):
                    try:
                        await bot.send_message(
                            int(telegram_id),
                            f"⏰ Напоминание!\n\n"
                            f"Вы записаны на сегодня в {time}.\n"
                            f"Пожалуйста, не опаздывайте."
                        )

                        update_reminder_status_by_row(idx)

                    except Exception as e:
                        logging.error(f"Ошибка отправки напоминания: {e}")

        except Exception as e:
            logging.error(f"Ошибка reminder_worker: {e}")

        await asyncio.sleep(60)

# --- СТАРТ И РЕГИСТРАЦИЯ ---
@dp.message(Command("start"))
async def start_cmd(message: Message, state: FSMContext):
    await state.clear()
    user = get_user(message.from_user.id)
    if not user:
        await message.answer("Здравствуйте! Для записи в Tyger Barber нужно зарегистрироваться.\n\nВведите ваше <b>Имя и Фамилию</b>:", parse_mode="HTML")
        await state.set_state(Registration.waiting_for_name)
    else:
        await message.answer(f"С возвращением!, {user['ФИО']}!", reply_markup=main_menu())

@dp.message(Registration.waiting_for_name)
async def reg_name(message: Message, state: FSMContext):
    await state.update_data(name=message.text)
    kb = ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text="📱 Отправить номер", request_contact=True)]], resize_keyboard=True)
    await message.answer("Нажмите кнопку ниже, чтобы отправить контакт:", reply_markup=kb)
    await state.set_state(Registration.waiting_for_phone)

@dp.message(Registration.waiting_for_phone, F.contact)
async def reg_phone(message: Message, state: FSMContext):
    data = await state.get_data()
    get_sheet("Users").append_row([str(message.from_user.id), data['name'], message.contact.phone_number, datetime.now().strftime("%d.%m.%Y")])
    await state.clear()
    await message.answer("✅ Регистрация завершена!", reply_markup=main_menu())

# --- ГЛАВНЫЕ КНОПКИ ---
@dp.message(F.text == "💰 Цены")
async def show_prices(message: Message):
    await send_complex_content_to_message(message, load_settings().get("prices"))

@dp.message(F.text == "📍 Адрес и график")
async def show_info(message: Message):
    await send_complex_content_to_message(message, load_settings().get("address"))

@dp.message(F.text == "👤 Мастера")
async def show_masters(message: Message):
    await send_complex_content_to_message(message, load_settings().get("masters_info"))

@dp.message(F.text == "📞 Поддержка")
async def support(message: Message):
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="Написать администратору", url="https://t.me/makkks_001")]])
    await message.answer("Если есть вопросы свяжитесь с администратором", reply_markup=kb)

# --- ПРОЦЕСС ЗАПИСИ ---
@dp.message(F.text == "📝 Записаться")
async def start_booking(message: Message, state: FSMContext):
    await state.clear()

    if not user_can_create_booking(message.from_user.id):
        return await message.answer(
            "У вас уже есть активная запись. Чтобы записаться на другое время, откройте управление записями.",
            reply_markup=main_menu()
        )

    await message.answer("Выберите мастера:", reply_markup=masters_kb())
    await state.set_state(Booking.waiting_for_master)

@dp.message(Booking.waiting_for_master)
async def book_master(message: Message, state: FSMContext):
    if message.text == "⬅️ Отмена":
        await state.clear()
        return await message.answer("Отменено", reply_markup=main_menu())
    
    settings = load_settings()
    if message.text not in settings.get("masters_names", []):
        return await message.answer("Пожалуйста, выберите мастера кнопкой из списка.")

    await state.update_data(master=message.text)
    await message.answer(f"Вы выбрали мастера: {message.text}\nВыберите дату:", reply_markup=dates_kb())
    await state.set_state(Booking.waiting_for_date)

@dp.message(Booking.waiting_for_date)
async def book_date(message: Message, state: FSMContext):
    if message.text == "⬅️ Отмена":
        await state.clear()
        return await message.answer("Отменено", reply_markup=main_menu())

    data = await state.get_data()
    parsed_date = parse_date(message.text)

    await state.update_data(
        date=parsed_date,
        date_display=format_date_display(parsed_date)
    )

    await state.set_state(Booking.waiting_for_time)

    await message.answer(
        "Выберите время:",
        reply_markup=times_kb(data["master"], parsed_date)
    )


@dp.message(Booking.waiting_for_time)
async def book_time(message: Message, state: FSMContext):
    if message.text == "⬅️ Отмена":
        data = await state.get_data()
        if "master" in data and "date" in data and "time" in data:
            release_slot(data["master"], data["date"], data["time"], message.from_user.id)
        await state.clear()
        return await message.answer("Отменено", reply_markup=main_menu())

    if message.text == "⬅️ Назад":
        await message.answer("Выберите дату:", reply_markup=dates_kb())
        await state.set_state(Booking.waiting_for_date)
        return

    valid_times = {
        "10:00", "11:00", "12:00", "13:00", "14:00", "15:00",
        "16:00", "17:00", "18:00", "19:00", "20:00"
    }

    if message.text not in valid_times:
        data = await state.get_data()
        return await message.answer(
            "Пожалуйста, выберите время кнопкой из списка.",
            reply_markup=times_kb(data["master"], data["date"])
        )

    data = await state.get_data()

    if not check_slot_availability(data["master"], data["date"], message.text):
        await message.answer(
            "❌ К сожалению, этот слот уже занят!\n\nВыберите другое время:",
            reply_markup=times_kb(data["master"], data["date"])
        )
        return

    if not reserve_slot(data["master"], data["date"], message.text, message.from_user.id):
        await message.answer(
            "❌ Слот был зарезервирован другим пользователем. Выберите другое время:",
            reply_markup=times_kb(data["master"], data["date"])
        )
        return

    await state.update_data(time=message.text)
    await message.answer(
        "Выберите услуги, которые вы хотите получить:",
        reply_markup=services_kb(multi_select=True)
    )
    await state.set_state(Booking.waiting_for_services)

@dp.message(Booking.waiting_for_services)
async def book_services(message: Message, state: FSMContext):
    if message.text == "⬅️ Отмена":
        data = await state.get_data()
        release_slot(data['master'], data['date'], data['time'], message.from_user.id)
        await state.clear()
        return await message.answer("Отменено", reply_markup=main_menu())
    
    data = await state.get_data()
    
    if message.text == "✅ Готово":
        services = data.get("selected_services", [])
        if not services:
            return await message.answer("❌ Пожалуйста, выберите хотя бы одну услугу.")
        
        user = get_user(message.from_user.id)
        services_str = ", ".join(services)
        
        try:
            get_sheet("Bookings").append_row([
                str(message.from_user.id), 
                user['ФИО'], 
                user['Телефон'], 
                data['master'], 
                data['date'], 
                data['time'], 
                datetime.now().strftime("%d.%m %H:%M"),
                services_str,
                ""  # Колонка Напоминания пока пуста
            ])
            release_slot(data['master'], data['date'], data['time'], message.from_user.id)
            if data.get("reschedule_row_index"):
                get_sheet("Bookings").delete_rows(data["reschedule_row_index"])
        except Exception as e:
            logging.error(f"Ошибка при записи: {e}")
            release_slot(data['master'], data['date'], data['time'], message.from_user.id)
            await message.answer("❌ Ошибка при записи. Пожалуйста, попробуйте снова.")
            await state.clear()
            return await message.answer("Вернулись в меню", reply_markup=main_menu())
            
        await state.clear()
        
        date_display = data.get("date_display", format_date_display(data["date"]))

        await state.clear()
        await message.answer(
            f"✅ Готово! Вы записаны.\n"
            f"👤 Мастер: {data['master']}\n"
            f"Дата: {date_display}\n"
            f"Время: {data['time']}\n"
            f"Услуги: {services_str}",
            reply_markup=main_menu()
        )
        return

    
    if message.text in AVAILABLE_SERVICES:
        selected = data.get("selected_services", [])
        if message.text not in selected:
            selected.append(message.text)
            await state.update_data(selected_services=selected)
            await message.answer(f"✅ Добавлена услуга: {message.text}\n\nВыбранные услуги: {', '.join(selected)}", reply_markup=services_kb(multi_select=True))
        else:
            await message.answer("Эта услуга уже выбрана.")
    else:
        await message.answer("Пожалуйста, выберите услугу кнопкой.")

# --- АДМИНКА ---
@dp.message(F.text.contains("Управление записями"))
async def manage_bookings_start(message: Message, state: FSMContext):
    await state.clear()

    bookings = get_active_user_bookings(message.from_user.id)

    if not bookings:
        return await message.answer("У вас нет активных записей.", reply_markup=main_menu())

    keyboard = []
    booking_map = {}

    for number, (row_index, row) in enumerate(bookings, start=1):
        date_display = format_date_display(row.get("Дата", ""))
        text = f"{date_display} {row.get('Время')} {row.get('Мастер')}"

        booking_map[text] = row_index
        keyboard.append([KeyboardButton(text=text)])

    keyboard.append([KeyboardButton(text="⬅️ Назад")])

    await state.update_data(booking_map=booking_map)

    await message.answer(
        "Выберите запись:",
        reply_markup=ReplyKeyboardMarkup(keyboard=keyboard, resize_keyboard=True)
    )
    await state.set_state(ManageBooking.selecting_booking)


@dp.message(ManageBooking.selecting_booking)
async def manage_booking_selected(message: Message, state: FSMContext):
    if message.text and "Назад" in message.text:
        await state.clear()
        return await message.answer("Меню", reply_markup=main_menu())

    data = await state.get_data()
    booking_map = data.get("booking_map", {})

    row_index = booking_map.get(message.text)

    if not row_index and message.text and message.text.startswith("#"):
        row_index = int(message.text.split()[0].replace("#", ""))

    if not row_index:
        return await message.answer("Выберите запись кнопкой из списка.")

    await state.update_data(manage_row_index=row_index)

    keyboard = ReplyKeyboardMarkup(keyboard=[
        [KeyboardButton(text="❌ Отменить запись")],
        [KeyboardButton(text="🔁 Перенести запись")],
        [KeyboardButton(text="⬅️ Назад")]
    ], resize_keyboard=True)

    await message.answer("Что сделать с записью?", reply_markup=keyboard)
    await state.set_state(ManageBooking.action)

async def manage_booking_selected(message: Message, state: FSMContext):
    if message.text == "⬅️ Назад":
        await state.clear()
        return await message.answer("Меню", reply_markup=main_menu())

    data = await state.get_data()
    booking_map = data.get("booking_map", {})

    row_index = booking_map.get(message.text)

    if not row_index:
        return await message.answer("Выберите запись кнопкой из списка.")

    await state.update_data(manage_row_index=row_index)

    keyboard = ReplyKeyboardMarkup(keyboard=[
        [KeyboardButton(text="❌ Отменить запись")],
        [KeyboardButton(text="🔁 Перенести запись")],
        [KeyboardButton(text="⬅️ Назад")]
    ], resize_keyboard=True)

    await message.answer("Что сделать с записью?", reply_markup=keyboard)
    await state.set_state(ManageBooking.action)


@dp.message(ManageBooking.action)
async def manage_booking_action(message: Message, state: FSMContext):
    data = await state.get_data()
    row_index = data.get("manage_row_index")

    if message.text == "⬅️ Назад":
        await state.clear()
        return await message.answer("Меню", reply_markup=main_menu())

    if message.text == "❌ Отменить запись":
        try:
            get_sheet("Bookings").delete_rows(row_index)
            await state.clear()
            return await message.answer("Запись отменена.", reply_markup=main_menu())
        except Exception as e:
            logging.error(f"Ошибка отмены записи: {e}")
            return await message.answer("Не удалось отменить запись. Попробуйте позже.")

    if message.text == "🔁 Перенести запись":
        await state.update_data(reschedule_row_index=row_index)
        await message.answer("Выберите нового мастера:", reply_markup=masters_kb())
        await state.set_state(Booking.waiting_for_master)
        return

    await message.answer("Выберите действие кнопкой.")

@dp.message(Command("admin"))
async def admin_panel(message: Message):
    if message.from_user.id not in ADMIN_IDS:
        return
    await message.answer("🔧 Админ-панель:", reply_markup=admin_kb())

# ================== АНАЛИТИКА ==================
@dp.message(F.text == "📊 Аналитика")
async def show_analytics(message: Message):
    if message.from_user.id not in ADMIN_IDS:
        return
    
    analytics = get_analytics()
    
    report = "<b>📊 АНАЛИТИКА</b>\n\n"
    report += "<b>👥 Клиенты по дням:</b>\n"
    report += f"📅 Сегодня: {analytics['today']} клиентов\n"
    report += f"📅 Вчера: {analytics['yesterday']} клиентов\n"
    report += f"📅 За неделю: {analytics['week']} клиентов\n"
    report += f"📅 За месяц: {analytics['month']} клиентов\n\n"
    
    report += "<b>💅 Услуги:</b>\n"
    for service, count in sorted(analytics['services'].items(), key=lambda x: x[1], reverse=True):
        report += f"• {service}: {count} клиентов\n"
    
    await message.answer(report, parse_mode="HTML")

# ================== РАССЫЛКА ==================
@dp.message(F.text == "📢 Рассылка")
async def start_mailing(message: Message, state: FSMContext):
    if message.from_user.id not in ADMIN_IDS:
        return
    
    await message.answer("Выберите услуги для рассылки (можно выбрать несколько):", reply_markup=services_mailing_kb())
    await state.update_data(selected_mailing_services=[])
    await state.set_state(AdminSettings.mailing_select_services)

@dp.message(AdminSettings.mailing_select_services)
async def select_mailing_services(message: Message, state: FSMContext):
    if message.text == "❌ Отмена":
        await state.clear()
        return await message.answer("Отменено.", reply_markup=admin_kb())
    
    if message.text == "✅ Готово":
        data = await state.get_data()
        services = data.get("selected_mailing_services", [])
        
        if not services:
            return await message.answer("❌ Пожалуйста, выберите хотя бы одну услугу.")
        
        await state.update_data(new_mailing_content=[])
        await message.answer(f"Вы выбрали услуги: {', '.join(services)}\n\nТеперь составьте сообщение для рассылки:", reply_markup=mailing_content_kb())
        await state.set_state(AdminSettings.mailing_content)
        return
    
    # Проверяем, это ли нажата кнопка услуги
    for service in AVAILABLE_SERVICES:
        if message.text == f"📌 {service}":
            data = await state.get_data()
            selected = data.get("selected_mailing_services", [])
            
            if service not in selected:
                selected.append(service)
                await state.update_data(selected_mailing_services=selected)
                await message.answer(f"✅ Добавлена услуга: {service}\n\nВыбранные: {', '.join(selected)}", reply_markup=services_mailing_kb())
            else:
                await message.answer("Эта услуга уже выбрана.")
            return
    
    await message.answer("Пожалуйста, выберите услугу кнопкой.")

@dp.message(AdminSettings.mailing_content)
async def collect_mailing_content(message: Message, state: FSMContext):
    if message.text == "❌ Отмена":
        await state.clear()
        return await message.answer("Рассылка отменена.", reply_markup=admin_kb())
    
    if message.text == "✅ Отправить рассылку":
        data = await state.get_data()
        services = data.get("selected_mailing_services", [])
        mailing_content = data.get("new_mailing_content", [])
        
        if not mailing_content:
            return await message.answer("❌ Контент не добавлен. Пожалуйста, добавьте хотя бы один элемент.")
        
        # Получаем пользователей по услугам
        all_target_users = {}
        for service in services:
            users = get_users_by_service(service)
            logging.info(f"📮 Услуга '{service}': найдено {len(users)} пользователей")
            all_target_users.update(users)
        
        logging.info(f"📮 Всего уникальных получателей: {len(all_target_users)}")
        
        # Показываем превью
        await message.answer(f"📋 <b>Предпросмотр рассылки для услуг: {', '.join(services)}</b>\n\nКоличество получателей: {len(all_target_users)}", parse_mode="HTML")
        await preview_content(message, mailing_content)
        
        await message.answer("\n✅ Отправляю рассылку...")
        
        # Отправляем рассылку всем пользователям
        sent_count = 0
        failed_count = 0
        
        for user_id in all_target_users.keys():
            try:
                await send_complex_content(int(user_id), mailing_content)
                sent_count += 1
                logging.info(f"✅ Рассылка отправлена пользователю {user_id}")
            except Exception as e:
                failed_count += 1
                logging.error(f"❌ Ошибка при отправке рассылки пользователю {user_id}: {e}")
        
        await message.answer(f"✅ Рассылка завершена!\n\n📤 Отправлено: {sent_count}\n❌ Ошибок: {failed_count}\n📊 Всего: {len(all_target_users)}", reply_markup=admin_kb())
        await state.clear()
        return
    
    if message.text == "📝 Добавить текст":
        await message.answer("Напишите текст (поддерживает HTML разметку):", reply_markup=cancel_kb())
        data = await state.get_data()
        await state.update_data(media_type="text")
        await state.set_state(AdminSettings.mailing_content)
        return
    
    if message.text == "🖼 Добавить фото":
        await message.answer("Отправьте фото (можно добавить подпись):", reply_markup=cancel_kb())
        await state.update_data(media_type="photo")
        return
    
    if message.text == "🎬 Добавить видео":
        await message.answer("Отправьте видео (можно добавить подпись):", reply_markup=cancel_kb())
        await state.update_data(media_type="video")
        return
    
    if message.text == "🎵 Добавить аудио":
        await message.answer("Отправьте аудио файл:", reply_markup=cancel_kb())
        await state.update_data(media_type="audio")
        return
    
    if message.text == "📄 Добавить документ":
        await message.answer("Отправьте документ:", reply_markup=cancel_kb())
        await state.update_data(media_type="document")
        return
    
    # Обработка медиа
    data = await state.get_data()
    mailing_content = data.get("new_mailing_content", [])
    media_type = data.get("media_type")
    
    if media_type == "photo" and message.photo:
        mailing_content.append({
            "type": "photo",
            "value": message.photo[-1].file_id,
            "caption": message.caption or ""
        })
    elif media_type == "video" and message.video:
        mailing_content.append({
            "type": "video",
            "value": message.video.file_id,
            "caption": message.caption or ""
        })
    elif media_type == "audio" and message.audio:
        mailing_content.append({
            "type": "audio",
            "value": message.audio.file_id,
            "caption": message.audio.title or ""
        })
    elif media_type == "document" and message.document:
        mailing_content.append({
            "type": "document",
            "value": message.document.file_id,
            "caption": message.caption or ""
        })
    elif message.text and not is_valid_button_input(message.text):
        mailing_content.append({
            "type": "text",
            "value": message.text
        })
    else:
        return await message.answer("❌ Неверный формат. Пожалуйста, отправьте нужный тип контента.")
    
    await state.update_data(new_mailing_content=mailing_content)
    await message.answer("✅ Элемент добавлен.\n\nЧто дальше?", reply_markup=mailing_content_kb())

# ================== РЕДАКТИРОВАНИЕ ЦЕНЫ ==================
@dp.message(F.text == "⚙️ Изменить Цены")
async def edit_prices_start(message: Message, state: FSMContext):
    if message.from_user.id not in ADMIN_IDS:
        return
    
    settings = load_settings()
    current_prices = settings.get("prices", [])
    
    await message.answer("📋 <b>Текущий прайс-лист:</b>", parse_mode="HTML")
    await send_complex_content_to_message(message, current_prices)
    
    await message.answer("\n⚠️ <b>РЕДАКТИРОВАНИЕ ПРАЙСА:</b>\n\nСейчас вы можете добавить новое содержимое. Все старое содержимое будет заменено.\n\nВыберите тип контента для добавления:", parse_mode="HTML", reply_markup=edit_content_kb())
    
    await state.update_data(content_type="prices", new_content=[])
    await state.set_state(AdminSettings.editing_prices_content)

@dp.message(AdminSettings.editing_prices_content)
async def collect_prices_content(message: Message, state: FSMContext):
    if message.text == "❌ Отмена":
        await state.clear()
        return await message.answer("Отменено.", reply_markup=admin_kb())
    
    if message.text == "✅ Сохранить":
        data = await state.get_data()
        new_content = data.get("new_content", [])
        
        if not new_content:
            return await message.answer("❌ Контент не добавлен. Пожалуйста, добавьте хотя бы один элемент.")
        
        await preview_content(message, new_content)
        await message.answer("✅ Сохраняю прайс-лист...")
        
        settings = load_settings()
        settings["prices"] = new_content
        save_settings(settings)
        
        await state.clear()
        return await message.answer("✅ Прайс-лист обновлен!", reply_markup=admin_kb())
    
    if message.text == "📝 Добавить текст":
        await message.answer("Напишите текст прайса (поддерживает HTML разметку):", reply_markup=cancel_kb())
        await state.set_state(AdminSettings.editing_prices)
        return
    
    if message.text == "🖼 Добавить фото":
        await message.answer("Отправьте фото:", reply_markup=cancel_kb())
        await state.update_data(media_type="photo")
        await state.set_state(AdminSettings.editing_prices)
        return
    
    if message.text == "🎬 Добавить видео":
        await message.answer("Отправьте видео:", reply_markup=cancel_kb())
        await state.update_data(media_type="video")
        await state.set_state(AdminSettings.editing_prices)
        return
    
    if message.text == "🎵 Добавить аудио":
        await message.answer("Отправьте аудио файл:", reply_markup=cancel_kb())
        await state.update_data(media_type="audio")
        await state.set_state(AdminSettings.editing_prices)
        return
    
    if message.text == "📄 Добавить документ":
        await message.answer("Отправьте документ:", reply_markup=cancel_kb())
        await state.update_data(media_type="document")
        await state.set_state(AdminSettings.editing_prices)
        return

@dp.message(AdminSettings.editing_prices)
async def save_prices_content(message: Message, state: FSMContext):
    if message.text == "❌ Отмена":
        await message.answer("Редактирование отменено.", reply_markup=edit_content_kb())
        await state.set_state(AdminSettings.editing_prices_content)
        return
    
    data = await state.get_data()
    new_content = data.get("new_content", [])
    media_type = data.get("media_type")
    
    if media_type == "photo" and message.photo:
        new_content.append({"type": "photo", "value": message.photo[-1].file_id, "caption": message.caption or ""})
    elif media_type == "video" and message.video:
        new_content.append({"type": "video", "value": message.video.file_id, "caption": message.caption or ""})
    elif media_type == "audio" and message.audio:
        new_content.append({"type": "audio", "value": message.audio.file_id, "caption": message.audio.title or ""})
    elif media_type == "document" and message.document:
        new_content.append({"type": "document", "value": message.document.file_id, "caption": message.caption or ""})
    elif message.text and not is_valid_button_input(message.text):
        new_content.append({"type": "text", "value": message.text})
    else:
        return await message.answer("❌ Неверный формат. Пожалуйста, отправьте нужный тип контента.")
    
    await state.update_data(new_content=new_content)
    await message.answer("✅ Элемент добавлен.\n\nЧто дальше?", reply_markup=edit_content_kb())
    await state.set_state(AdminSettings.editing_prices_content)

# ================== РЕДАКТИРОВАНИЕ АДРЕСА ==================
@dp.message(F.text == "⚙️ Изменить Адрес")
async def edit_address_start(message: Message, state: FSMContext):
    if message.from_user.id not in ADMIN_IDS:
        return
    
    settings = load_settings()
    current_address = settings.get("address", [])
    
    await message.answer("📋 <b>Текущий адрес и график:</b>", parse_mode="HTML")
    await send_complex_content_to_message(message, current_address)
    
    await message.answer("\n⚠️ <b>РЕДАКТИРОВАНИЕ АДРЕСА:</b>\n\nСейчас вы можете добавить новое содержимое. Все старое содержимое будет заменено.\n\nВыберите тип контента для добавления:", parse_mode="HTML", reply_markup=edit_content_kb())
    
    await state.update_data(content_type="address", new_content=[])
    await state.set_state(AdminSettings.editing_address_content)

@dp.message(AdminSettings.editing_address_content)
async def collect_address_content(message: Message, state: FSMContext):
    if message.text == "❌ Отмена":
        await state.clear()
        return await message.answer("Отменено.", reply_markup=admin_kb())
    
    if message.text == "✅ Сохранить":
        data = await state.get_data()
        new_content = data.get("new_content", [])
        
        if not new_content:
            return await message.answer("❌ Контент не добавлен. Пожалуйста, добавьте хотя бы один элемент.")
        
        await preview_content(message, new_content)
        await message.answer("✅ Сохраняю адрес...")
        
        settings = load_settings()
        settings["address"] = new_content
        save_settings(settings)
        
        await state.clear()
        return await message.answer("✅ Адрес обновлен!", reply_markup=admin_kb())
    
    if message.text == "📝 Добавить текст":
        await message.answer("Напишите адрес и график (поддерживает HTML разметку):", reply_markup=cancel_kb())
        await state.set_state(AdminSettings.editing_address)
        return
    
    if message.text == "🖼 Добавить фото":
        await message.answer("Отправьте фото:", reply_markup=cancel_kb())
        await state.update_data(media_type="photo")
        await state.set_state(AdminSettings.editing_address)
        return
    
    if message.text == "🎬 Добавить видео":
        await message.answer("Отправьте видео:", reply_markup=cancel_kb())
        await state.update_data(media_type="video")
        await state.set_state(AdminSettings.editing_address)
        return
    
    if message.text == "🎵 Добавить аудио":
        await message.answer("Отправьте аудио файл:", reply_markup=cancel_kb())
        await state.update_data(media_type="audio")
        await state.set_state(AdminSettings.editing_address)
        return
    
    if message.text == "📄 Добавить документ":
        await message.answer("Отправьте документ:", reply_markup=cancel_kb())
        await state.update_data(media_type="document")
        await state.set_state(AdminSettings.editing_address)
        return

@dp.message(AdminSettings.editing_address)
async def save_address_content(message: Message, state: FSMContext):
    if message.text == "❌ Отмена":
        await message.answer("Редактирование отменено.", reply_markup=edit_content_kb())
        await state.set_state(AdminSettings.editing_address_content)
        return
    
    data = await state.get_data()
    new_content = data.get("new_content", [])
    media_type = data.get("media_type")
    
    if media_type == "photo" and message.photo:
        new_content.append({"type": "photo", "value": message.photo[-1].file_id, "caption": message.caption or ""})
    elif media_type == "video" and message.video:
        new_content.append({"type": "video", "value": message.video.file_id, "caption": message.caption or ""})
    elif media_type == "audio" and message.audio:
        new_content.append({"type": "audio", "value": message.audio.file_id, "caption": message.audio.title or ""})
    elif media_type == "document" and message.document:
        new_content.append({"type": "document", "value": message.document.file_id, "caption": message.caption or ""})
    elif message.text and not is_valid_button_input(message.text):
        new_content.append({"type": "text", "value": message.text})
    else:
        return await message.answer("❌ Неверный формат. Пожалуйста, отправьте нужный тип контента.")
    
    await state.update_data(new_content=new_content)
    await message.answer("✅ Элемент добавлен.\n\nЧто дальше?", reply_markup=edit_content_kb())
    await state.set_state(AdminSettings.editing_address_content)

# ================== ДОБАВЛЕНИЕ МАСТЕРА ==================
@dp.message(F.text == "➕ Добавить Мастера")
async def add_master_start(message: Message, state: FSMContext):
    if message.from_user.id not in ADMIN_IDS:
        return
    await message.answer("Введите ИМЯ мастера (для кнопки):", reply_markup=cancel_kb())
    await state.set_state(AdminSettings.adding_master_name)

@dp.message(AdminSettings.adding_master_name)
async def add_master_name(message: Message, state: FSMContext):
    if message.text == "❌ Отмена":
        await state.clear()
        return await message.answer("Отменено.", reply_markup=admin_kb())
    
    if is_valid_button_input(message.text):
        return await message.answer("❌ Это название кнопки. Пожалуйста, введите имя мастера.")
    
    master_name = message.text
    await state.update_data(master_name=master_name, new_content=[{"type": "text", "value": f"<b>👤 Мастер: {master_name}</b>"}])
    
    await message.answer(f"Мастер: <b>{master_name}</b>\n\nТеперь добавьте информацию о мастере (фото, описание и т.д.):", parse_mode="HTML", reply_markup=edit_content_kb())
    await state.set_state(AdminSettings.adding_master_content)

@dp.message(AdminSettings.adding_master_content)
async def collect_master_content(message: Message, state: FSMContext):
    if message.text == "❌ Отмена":
        await state.clear()
        return await message.answer("Отменено.", reply_markup=admin_kb())
    
    if message.text == "✅ Сохранить":
        data = await state.get_data()
        master_name = data.get("master_name")
        new_content = data.get("new_content", [])
        
        await preview_content(message, new_content)
        await message.answer("✅ Сохраняю мастера...")
        
        settings = load_settings()
        settings["masters_names"].append(master_name)
        settings["masters_info"].extend(new_content)
        settings["masters_info"].append({"type": "text", "value": "───────────────"})
        save_settings(settings)
        
        await state.clear()
        return await message.answer("✅ Мастер добавлен!", reply_markup=admin_kb())
    
    if message.text == "📝 Добавить текст":
        await message.answer("Напишите описание (поддерживает HTML разметку):", reply_markup=cancel_kb())
        await state.set_state(AdminSettings.adding_master)
        return
    
    if message.text == "🖼 Добавить фото":
        await message.answer("Отправьте фото:", reply_markup=cancel_kb())
        await state.update_data(media_type="photo")
        await state.set_state(AdminSettings.adding_master)
        return
    
    if message.text == "🎬 Добавить видео":
        await message.answer("Отправьте видео:", reply_markup=cancel_kb())
        await state.update_data(media_type="video")
        await state.set_state(AdminSettings.adding_master)
        return
    
    if message.text == "🎵 Добавить аудио":
        await message.answer("Отправьте аудио файл:", reply_markup=cancel_kb())
        await state.update_data(media_type="audio")
        await state.set_state(AdminSettings.adding_master)
        return
    
    if message.text == "📄 Добавить документ":
        await message.answer("Отправьте документ:", reply_markup=cancel_kb())
        await state.update_data(media_type="document")
        await state.set_state(AdminSettings.adding_master)
        return

@dp.message(AdminSettings.adding_master)
async def save_master_content(message: Message, state: FSMContext):
    if message.text == "❌ Отмена":
        await message.answer("Редактирование отменено.", reply_markup=edit_content_kb())
        await state.set_state(AdminSettings.adding_master_content)
        return
    
    data = await state.get_data()
    new_content = data.get("new_content", [])
    media_type = data.get("media_type")
    
    if media_type == "photo" and message.photo:
        new_content.append({"type": "photo", "value": message.photo[-1].file_id, "caption": message.caption or ""})
    elif media_type == "video" and message.video:
        new_content.append({"type": "video", "value": message.video.file_id, "caption": message.caption or ""})
    elif media_type == "audio" and message.audio:
        new_content.append({"type": "audio", "value": message.audio.file_id, "caption": message.audio.title or ""})
    elif media_type == "document" and message.document:
        new_content.append({"type": "document", "value": message.document.file_id, "caption": message.caption or ""})
    elif message.text and not is_valid_button_input(message.text):
        new_content.append({"type": "text", "value": message.text})
    else:
        return await message.answer("❌ Неверный формат. Пожалуйста, отправьте нужный тип контента.")
    
    await state.update_data(new_content=new_content)
    await message.answer("✅ Элемент добавлен.\n\nЧто дальше?", reply_markup=edit_content_kb())
    await state.set_state(AdminSettings.adding_master_content)

# ================== ОЧИСТКА И ВЫХОД ==================
@dp.message(F.text == "🗑 Очистить Мастеров")
async def clear_masters(message: Message):
    if message.from_user.id not in ADMIN_IDS:
        return
    s = load_settings()
    s["masters_names"], s["masters_info"] = [], []
    save_settings(s)
    await message.answer("Список мастеров очищен.", reply_markup=admin_kb())

@dp.message(F.text == "⬅️ Выйти")
async def exit_admin(message: Message, state: FSMContext):
    if message.from_user.id not in ADMIN_IDS:
        return
    await state.clear()
    await message.answer("Вы вернулись в меню пользователя", reply_markup=main_menu())

# --- ЗАПУСК ---
async def main():
    await bot.delete_webhook(drop_pending_updates=True)
    await bot.set_webhook(url=f"{WEBHOOK_HOST.rstrip('/')}{WEBHOOK_PATH}")
    asyncio.create_task(reminder_worker())
    app = web.Application()
    SimpleRequestHandler(dispatcher=dp, bot=bot).register(app, path=WEBHOOK_PATH)
    setup_application(app, dp, bot=bot)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, WEBAPP_HOST, WEBAPP_PORT).start()
    await asyncio.Event().wait()

if __name__ == "__main__":
    asyncio.run(main())
