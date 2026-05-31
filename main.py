import anthropic
import base64
import json
import sqlite3
import httpx
import asyncio
import io, os
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.types import Message, InputMediaPhoto
from aiogram.utils.keyboard import InlineKeyboardBuilder
from pyrogram import Client, filters as pyro_filters
from dotenv import load_dotenv

pending_posts = {}
user_styles = {}
waiting_for_topic = set()
waiting_for_style = set()
waiting_for_company_info = set()
userbot_active = False
load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
API_ID = int(os.getenv("API_ID"))
API_HASH = os.getenv("API_HASH")
ADMIN_ID = int(os.getenv("ADMIN_ID"))
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")
CHANNEL = os.getenv("CHANNEL")

album_buffer = {}
album_tasks = {}

DEFAULT_STYLE = "живой, простой, без выдумок, цепляющий."

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())
userbot = Client("my_account", api_id=API_ID, api_hash=API_HASH)
claude = anthropic.AsyncAnthropic(api_key=ANTHROPIC_API_KEY)

def init_db():
    conn = sqlite3.connect("history.db")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            role TEXT,
            text TEXT,
            timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS company_info (
            id INTEGER PRIMARY KEY,
            info TEXT
        )
    """)
    conn.commit()
    conn.close()

def save_message(user_id, role, text):
    conn = sqlite3.connect("history.db")
    conn.execute("INSERT INTO messages (user_id, role, text) VALUES (?, ?, ?)",
                 (user_id, role, text))
    conn.commit()
    conn.close()

def get_history(user_id, limit=10):
    conn = sqlite3.connect("history.db")
    rows = conn.execute("""
        SELECT role, text FROM messages
        WHERE user_id = ?
        ORDER BY timestamp DESC
        LIMIT ?
    """, (user_id, limit)).fetchall()
    conn.close()
    return list(reversed(rows))

def save_company_info(info):
    conn = sqlite3.connect("history.db")
    conn.execute("INSERT OR REPLACE INTO company_info (id, info) VALUES (1, ?)", (info,))
    conn.commit()
    conn.close()

def get_company_info():
    conn = sqlite3.connect("history.db")
    row = conn.execute("SELECT info FROM company_info WHERE id = 1").fetchone()
    conn.close()
    return row[0] if row else None

init_db()

def get_style(user_id):
    return user_styles.get(user_id, DEFAULT_STYLE)

def get_mention(user):
    if user.username:
        return f"@{user.username}"
    return f'<a href="tg://user?id={user.id}">{user.first_name}</a>'

async def generate_post(topic, user_id, photo_path=None):
    style = get_style(user_id)

    if photo_path:
        with open(photo_path, "rb") as f:
            image_data = base64.b64encode(f.read()).decode("utf-8")

        response = await claude.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=1024,
            system=(
                "Ты SMM-менеджер секонд-хенд магазина. "
                "Пишешь короткие продающие посты для Telegram. "
                "Никогда не добавляй @упоминания или хэштеги. "
                f"Стиль: {style}"
            ),
            messages=[
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": "image/jpeg",
                                "data": image_data,
                            },
                        },
                        {
                            "type": "text",
                            "text": (
                                f"Посмотри на фото товара и напиши ОДИН готовый пост для Telegram.\n"
                                f"Описание от продавца: {topic if topic else 'не указано'}\n\n"
                                "Упомяни что видишь на фото (цвет, тип вещи, состояние если видно).\n"
                                "Обязательно упомяни цену если она указана в описании.\n"
                                "Структура: 2-3 предложения. Без вариантов, без нумерации.\n"
                                "Только сам текст поста:"
                            )
                        }
                    ],
                }
            ],
        )
    else:
        response = await claude.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=1024,
            system=(
                "Ты SMM-менеджер брендового магазина. "
                "Пишешь короткие продающие посты для Telegram. "
                "Никогда не добавляй @упоминания или хэштеги. "
                f"Стиль: {style}"
            ),
            messages=[
                {
                    "role": "user",
                    "content": (
                        f"Напиши ОДИН готовый пост для Telegram.\n"
                        f"Описание товара: {topic}\n\n"
                        "Структура поста:\n"
                        "- Название бренда\n"
                        "- Краткое продающее описание (2-3 слова)\n"
                        "- Размер, состояние, цена (если указаны)\n\n"
                        "2-3 предложения. Без вариантов, без заголовков, без нумерации.\n"
                        "Только сам текст поста:"
                    )
                }
            ],
        )

    return response.content[0].text

async def generate_reply(user_id, text):
    history = get_history(user_id, limit=6)
    company_info = get_company_info()

    save_message(user_id, "user", text)

    company_block = f"О компании:\n{company_info}" if company_info else "Информация о компании не задана."

    # Собираем историю в формате Claude API
    messages = []
    for role, msg in history[:-1]:  # всё кроме последнего (только что сохранённого)
        api_role = "user" if role == "user" else "assistant"
        messages.append({"role": api_role, "content": msg})
    messages.append({"role": "user", "content": text})

    response = await claude.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=256,
        system=(
            f"Ты менеджер по продажам. Отвечаешь клиентам коротко и по-человечески.\n\n"
            f"{company_block}\n\n"
            "СТРОГИЕ ПРАВИЛА:\n"
            "1. Отвечай ТОЛЬКО на последнее сообщение клиента\n"
            "2. Максимум 1-2 предложения\n"
            "3. Не повторяй вопросы которые уже задавал\n"
            "4. Никаких шаблонных фраз про 'широкий спектр услуг'\n"
            "5. Если уже спрашивал про цели/бренд/каналы — НЕ спрашивай снова"
        ),
        messages=messages,
    )

    reply = response.content[0].text
    save_message(user_id, "bot", reply)
    return reply

async def send_to_channel(text, photo_paths=None, mention=None):
    caption = f"{text}\n\n💌 {mention}" if mention else text
    async with httpx.AsyncClient() as client:
        if photo_paths and len(photo_paths) == 1:
            with open(photo_paths[0], "rb") as f:
                await client.post(
                    f"https://api.telegram.org/bot{BOT_TOKEN}/sendPhoto",
                    data={"chat_id": CHANNEL, "caption": caption, "parse_mode": "HTML"},
                    files={"photo": f}
                )
        elif photo_paths and len(photo_paths) > 1:
            files = {}
            media = []
            for i, path in enumerate(photo_paths):
                key = f"photo{i}"
                files[key] = open(path, "rb")
                item = {"type": "photo", "media": f"attach://{key}"}
                if i == 0:
                    item["caption"] = caption
                    item["parse_mode"] = "HTML"
                media.append(item)
            await client.post(
                f"https://api.telegram.org/bot{BOT_TOKEN}/sendMediaGroup",
                data={"chat_id": CHANNEL, "media": json.dumps(media)},
                files=files
            )
        else:
            await client.post(
                f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
                json={"chat_id": CHANNEL, "text": caption, "parse_mode": "HTML"}
            )

async def process_album(user_id: int):
    await asyncio.sleep(0.5)

    messages = album_buffer.pop(user_id, [])
    album_tasks.pop(user_id, None)

    if not messages:
        return

    user = messages[0].from_user
    mention = get_mention(user)
    caption = messages[0].caption or ""

    os.makedirs("photos", exist_ok=True)
    all_photo_paths = []
    for i, msg in enumerate(messages):
        photo = msg.photo[-1]
        file = await bot.get_file(photo.file_id)
        file_bytes = await bot.download_file(file.file_path)
        path = f"photos/{user_id}_{i}.jpg"
        with open(path, "wb") as f:
            f.write(file_bytes.read())
        all_photo_paths.append(path)

    await messages[0].answer("Генерирую пост...")
    post = await generate_post(caption, user_id, photo_path=all_photo_paths[0])
    pending_posts[user_id] = {"text": post, "photo_paths": all_photo_paths, "mention": mention}

    builder = InlineKeyboardBuilder()
    builder.button(text="Оплатить", callback_data="payment")

    if len(all_photo_paths) == 1:
        await bot.send_photo(
            user_id,
            photo=types.FSInputFile(all_photo_paths[0]),
            caption=f"Готовый пост:\n{post}\n\n💌 {mention}",
            parse_mode="HTML",
            reply_markup=builder.as_markup()
        )
    else:
        media = []
        for i, path in enumerate(all_photo_paths):
            if i == 0:
                media.append(InputMediaPhoto(
                    media=types.FSInputFile(path),
                    caption=f"Готовый пост:\n{post}\n\n💌 {mention}",
                    parse_mode="HTML"
                ))
            else:
                media.append(InputMediaPhoto(media=types.FSInputFile(path)))
        await bot.send_media_group(user_id, media=media)
        await messages[0].answer("👆 Ваш пост готов", reply_markup=builder.as_markup())

@dp.message(Command("start"))
async def start(message: types.Message):
    user_id = message.from_user.id
    builder = InlineKeyboardBuilder()
    if user_id == ADMIN_ID:
        builder.button(text="Включить агента", callback_data="agent_on")
        builder.button(text="Выключить агента", callback_data="agent_off")
    builder.button(text="Создать пост", callback_data="generate")
    builder.adjust(2)
    await message.answer(
        f"Привет, {message.from_user.first_name}!\n\n",
        reply_markup=builder.as_markup()
    )

@dp.message(Command("status"))
async def status(message: types.Message):
    state = "включён" if userbot_active else "выключен"
    await message.answer(f"AI {state}")

@dp.message(Command("setinfo"))
async def cmd_setinfo(message: types.Message):
    waiting_for_company_info.add(message.from_user.id)
    await message.answer(
        "Расскажи о своей компании — чем занимаетесь, услуги, цены, контакты. "
        "Всё что AI должен знать при общении с клиентами."
    )

@dp.message(Command("generate"))
async def cmd_generate(message: Message):
    waiting_for_topic.add(message.from_user.id)
    await message.answer(
        "Для размещения отправьте:\n"
        "— Фото товара хорошего качества\n"
        "— Название бренда / модели\n"
        "— Размер\n"
        "— Состояние (честно)\n"
        "— Цена"
    )

@dp.callback_query(F.data == "generate")
async def btn_generate(callback: types.CallbackQuery):
    waiting_for_topic.add(callback.from_user.id)
    await callback.message.edit_text(
        "Для размещения отправьте:\n"
        "— Фото товара хорошего качества\n"
        "— Название бренда / модели\n"
        "— Размер\n"
        "— Состояние (честно)\n"
        "— Цена"
    )
    await callback.answer()

@dp.message(Command("mystyle"))
async def cmd_mystyle(message: types.Message):
    style = get_style(message.from_user.id)
    await message.answer(f"Текущий стиль:\n\n{style}")

@dp.message(Command("style"))
async def cmd_style(message: types.Message):
    waiting_for_style.add(message.from_user.id)
    await message.answer("Опишите стиль постов.\n\nПример:\n- живой, дружелюбный, без выдумок.")

@dp.callback_query(F.data == "agent_on")
async def agent_on(callback: types.CallbackQuery):
    global userbot_active
    userbot_active = True
    await callback.answer("AI включён!")
    await callback.message.edit_text("AI включён и начал отвечать.")

@dp.callback_query(F.data == "agent_off")
async def agent_off(callback: types.CallbackQuery):
    global userbot_active
    userbot_active = False
    await callback.answer("AI выключен.")
    await callback.message.edit_text("AI выключен.")

class PaymentState(StatesGroup):
    waiting_for_check = State()

@dp.callback_query(F.data == "payment")
async def cmd_payment(callback: types.CallbackQuery, state: FSMContext):
    await callback.message.answer(
        "💳 Номер карты: \n"
        "После оплаты отправьте чек оплаты в чат с ботом."
    )
    await state.set_state(PaymentState.waiting_for_check)
    await callback.answer()

@dp.message(PaymentState.waiting_for_check)
async def handle_payment_check(message: types.Message, state: FSMContext):
    user = message.from_user
    user_id = user.id
    mention = get_mention(user)

    data = pending_posts.get(user_id)
    if not data:
        await message.answer("❌ Пост не найден. Создайте пост заново.")
        await state.clear()
        return

    post = data["text"]

    builder = InlineKeyboardBuilder()
    builder.button(text="✅ Опубликовать", callback_data=f"approve_{user_id}")
    builder.button(text="❌ Отклонить", callback_data=f"reject_{user_id}")

    admin_text = (
        f"💰 Новый чек от {mention}\n"
        f"ID: <code>{user_id}</code>\n\n"
        f"📝 Пост для публикации:\n\n{post}"
    )

    if message.photo or message.document:
        await message.forward(ADMIN_ID)
        await bot.send_message(
            ADMIN_ID,
            admin_text,
            parse_mode="HTML",
            reply_markup=builder.as_markup()
        )
    elif message.text:
        await bot.send_message(
            ADMIN_ID,
            f"💰 Новый чек от {mention}\n"
            f"ID: <code>{user_id}</code>\n\n"
            f"📄 Текст чека: {message.text}\n\n"
            f"📝 Пост для публикации:\n\n{post}",
            parse_mode="HTML",
            reply_markup=builder.as_markup()
        )
    else:
        await message.answer("❌ Отправьте фото, документ или текст чека.")
        return

    await state.clear()
    await message.answer("✅ Чек получен! Ожидайте подтверждения оплаты.")

@dp.callback_query(F.data.startswith("approve_"))
async def approve_post(callback: types.CallbackQuery):
    user_id = int(callback.data.split("_")[1])
    data = pending_posts.get(user_id)
    if data:
        mention = data.get("mention")
        await send_to_channel(data["text"], data.get("photo_paths"), mention=mention)
        pending_posts.pop(user_id, None)
        await bot.send_message(user_id, "✅ Оплата подтверждена! Ваш пост опубликован.")
        await callback.message.edit_text("✅ Опубликовано.")
    else:
        await callback.answer("Пост не найден.", show_alert=True)

@dp.callback_query(F.data.startswith("reject_"))
async def reject_post(callback: types.CallbackQuery):
    user_id = int(callback.data.split("_")[1])
    pending_posts.pop(user_id, None)
    await bot.send_message(user_id, "❌ Оплата не подтверждена. Обратитесь к администратору @prelovedstoreadmin.")
    await callback.message.edit_text("❌ Отклонено.")

@dp.message()
async def handle_message(message: types.Message):
    user_id = message.from_user.id
    text = message.text
    user = message.from_user
    mention = get_mention(user)

    if user_id in waiting_for_company_info:
        waiting_for_company_info.discard(user_id)
        save_company_info(text)
        await message.answer("Информация о компании сохранена.")
        return

    if user_id in waiting_for_style:
        waiting_for_style.discard(user_id)
        user_styles[user_id] = text
        await message.answer(f"Стиль сохранён.\n\n{text}")
        return

    if user_id in waiting_for_topic or user_id in album_buffer:
        if message.photo:
            if user_id not in album_buffer:
                album_buffer[user_id] = []
            waiting_for_topic.discard(user_id)
            album_buffer[user_id].append(message)

            if user_id in album_tasks:
                album_tasks[user_id].cancel()
            album_tasks[user_id] = asyncio.ensure_future(process_album(user_id))
            return

        if not text:
            await message.answer("Отправьте фото товара (можно с подписью) или текстовое описание.")
            return

    if not text:
        return

    await message.answer("Отправьте фото и описание товара для создания поста.")

@userbot.on_message(pyro_filters.private & pyro_filters.incoming & ~pyro_filters.bot)
async def auto_reply(client, message):
    if not userbot_active:
        return
    if not message.text:
        return
    reply = await generate_reply(message.from_user.id, message.text)
    await message.reply(reply)

async def main():
    await asyncio.gather(
        dp.start_polling(bot),
        userbot.start(),
    )
    await asyncio.Event().wait()

if __name__ == "__main__":
    asyncio.run(main())