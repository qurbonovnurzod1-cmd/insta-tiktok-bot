import asyncio
import os
import re
import sqlite3
import tempfile
from datetime import datetime
from pathlib import Path

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    CallbackQuery,
    FSInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
)
import yt_dlp

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
RAW_ADMINS = os.getenv("ADMIN_IDS", "")
ADMIN_IDS: set[int] = {
    int(x.strip()) for x in RAW_ADMINS.split(",") if x.strip().isdigit()
}

DB_PATH = Path(os.getenv("DB_PATH", "bot_data.db"))
MAX_FILE_SIZE_BYTES = 48 * 1024 * 1024
URL_PATTERN = re.compile(
    r"(https?://(?:www\.|vm\.|vt\.)?(?:instagram\.com|tiktok\.com)/\S+)"
)


class UserStates(StatesGroup):
    waiting_admin_message = State()


class AdminStates(StatesGroup):
    waiting_add_channel = State()
    waiting_reply_user = State()


# --- Baza bilan ishlash ---
def init_db() -> None:
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                full_name TEXT,
                joined_at TEXT,
                last_active TEXT
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS channels (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_ref TEXT UNIQUE NOT NULL,
                title TEXT
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            INSERT OR IGNORE INTO meta (key, value) VALUES ('downloads_count', '0')
            """
        )
        conn.commit()


def upsert_user(user_id: int, username: str | None, full_name: str) -> None:
    now = datetime.utcnow().isoformat(timespec="seconds")
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            """
            INSERT INTO users (user_id, username, full_name, joined_at, last_active)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                username = excluded.username,
                full_name = excluded.full_name,
                last_active = excluded.last_active
            """,
            (user_id, username, full_name, now, now),
        )
        conn.commit()


def increment_downloads() -> None:
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            """
            UPDATE meta
            SET value = CAST(CAST(value AS INTEGER) + 1 AS TEXT)
            WHERE key = 'downloads_count'
            """
        )
        conn.commit()


def get_stats() -> tuple[int, int, int]:
    now = datetime.utcnow().strftime("%Y-%m-%d")
    with sqlite3.connect(DB_PATH) as conn:
        total_users = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        today_users = conn.execute(
            "SELECT COUNT(*) FROM users WHERE joined_at LIKE ?", (f"{now}%",)
        ).fetchone()[0]
        downloads = int(
            conn.execute(
                "SELECT value FROM meta WHERE key = 'downloads_count'"
            ).fetchone()[0]
        )
    return total_users, today_users, downloads


def add_channel(chat_ref: str, title: str | None = None) -> bool:
    cleaned = chat_ref.strip()
    if not cleaned:
        return False
    if not cleaned.startswith("@") and not cleaned.lstrip("-").isdigit():
        return False
    if cleaned.lstrip("-").isdigit():
        cleaned = str(int(cleaned))
    with sqlite3.connect(DB_PATH) as conn:
        try:
            conn.execute(
                "INSERT INTO channels (chat_ref, title) VALUES (?, ?)",
                (cleaned, title or cleaned),
            )
            conn.commit()
            return True
        except sqlite3.IntegrityError:
            return False


def remove_channel(channel_id: int) -> None:
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("DELETE FROM channels WHERE id = ?", (channel_id,))
        conn.commit()


def list_channels() -> list[tuple[int, str, str]]:
    with sqlite3.connect(DB_PATH) as conn:
        rows = conn.execute(
            "SELECT id, chat_ref, COALESCE(title, chat_ref) FROM channels ORDER BY id ASC"
        ).fetchall()
    return rows


# --- Klaviaturalar ---
def main_menu_kb(is_admin: bool = False) -> ReplyKeyboardMarkup:
    rows = [
        [KeyboardButton(text="📩 Adminga xabar yuborish")],
    ]
    if is_admin:
        rows.append([KeyboardButton(text="🛠 Admin panel")])
    return ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True)


def admin_panel_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="➕ Majburiy kanal qo'shish", callback_data="adm:add_ch"
                )
            ],
            [
                InlineKeyboardButton(
                    text="📋 Kanallar / O'chirish", callback_data="adm:list_ch"
                )
            ],
            [
                InlineKeyboardButton(
                    text="📊 Statistika", callback_data="adm:stats"
                )
            ],
        ]
    )


def build_sub_kb(missing_channels: list[tuple[int, str, str]]) -> InlineKeyboardMarkup:
    buttons: list[list[InlineKeyboardButton]] = []
    for _, chat_ref, title in missing_channels:
        if chat_ref.startswith("@"):
            url = f"https://t.me/{chat_ref.lstrip('@')}"
            buttons.append([InlineKeyboardButton(text=f"📢 {title}", url=url)])
        else:
            buttons.append(
                [
                    InlineKeyboardButton(
                        text=f"📢 {title} ({chat_ref})",
                        callback_data="sub:noop",
                    )
                ]
            )
    buttons.append(
        [
            InlineKeyboardButton(
                text="✅ Obunani tekshirish", callback_data="sub:check"
            )
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=buttons)


# --- Majburiy obuna tekshiruvi ---
async def get_missing_channels(
    bot: Bot, user_id: int
) -> list[tuple[int, str, str]]:
    if user_id in ADMIN_IDS:
        return []
    channels = list_channels()
    if not channels:
        return []

    missing: list[tuple[int, str, str]] = []
    for ch_id, chat_ref, title in channels:
        target: int | str = int(chat_ref) if chat_ref.lstrip("-").isdigit() else chat_ref
        try:
            member = await bot.get_chat_member(chat_id=target, user_id=user_id)
            status = getattr(member, "status", "")
            if status in {"left", "kicked"}:
                missing.append((ch_id, chat_ref, title))
        except Exception:
            # Agar bot kanalda admin bo'lmasa yoki chat topilmasa, xavfsizlik uchun ro'yxatga qo'shamiz
            missing.append((ch_id, chat_ref, title))
    return missing


async def ensure_subscribed(bot: Bot, message: Message) -> bool:
    user = message.from_user
    if not user:
        return False
    missing = await get_missing_channels(bot, user.id)
    if not missing:
        return True
    await message.answer(
        "Botdan foydalanish uchun quyidagi kanal(lar)ga obuna bo‘ling va "
        "<b>«Obunani tekshirish»</b> tugmasini bosing:",
        reply_markup=build_sub_kb(missing),
        parse_mode="HTML",
    )
    return False


# --- Video yuklash ---
def download_video(url: str, out_dir: Path) -> Path:
    ydl_opts = {
        "outtmpl": str(out_dir / "%(id)s.%(ext)s"),
        "format": "mp4/best[ext=mp4]/best",
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "max_filesize": MAX_FILE_SIZE_BYTES,
    }
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=True)
        duration = info.get("duration")
        if duration and duration > 190:
            downloaded = Path(ydl.prepare_filename(info))
            if downloaded.exists():
                downloaded.unlink()
            raise ValueError("Video 3 daqiqadan uzun.")

        file_path = Path(ydl.prepare_filename(info))
        if not file_path.exists():
            mp4_candidate = file_path.with_suffix(".mp4")
            if mp4_candidate.exists():
                return mp4_candidate
            raise FileNotFoundError("Yuklangan video fayl topilmadi.")
        return file_path


# --- Handlerlar ---
dp = Dispatcher(storage=MemoryStorage())


@dp.message(CommandStart())
async def cmd_start(message: Message, bot: Bot, state: FSMContext) -> None:
    await state.clear()
    user = message.from_user
    if not user:
        return
    upsert_user(user.id, user.username, user.full_name)

    if not await ensure_subscribed(bot, message):
        return

    await message.answer(
        "Assalomu alaykum! Menga <b>3 daqiqagacha</b> bo‘lgan Instagram yoki TikTok "
        "video havolasini yuboring — yuklab beraman.\n\n"
        "Savol yoki taklif bo‘lsa, pastdagi <b>«Adminga xabar yuborish»</b> tugmasidan foydalaning.",
        reply_markup=main_menu_kb(is_admin=user.id in ADMIN_IDS),
        parse_mode="HTML",
    )


@dp.callback_query(F.data == "sub:check")
async def cb_check_sub(callback: CallbackQuery, bot: Bot) -> None:
    user = callback.from_user
    if not user:
        return
    missing = await get_missing_channels(bot, user.id)
    if missing:
        await callback.answer(
            "Hali barcha kanallarga obuna bo‘lmadingiz!", show_alert=True
        )
        try:
            await callback.message.edit_reply_markup(reply_markup=build_sub_kb(missing))
        except Exception:
            pass
        return

    await callback.answer("Obuna tasdiqlandi!", show_alert=False)
    try:
        await callback.message.delete()
    except Exception:
        pass
    await callback.message.answer(
        "Rahmat! Endi Instagram yoki TikTok video havolasini yuborishingiz mumkin.",
        reply_markup=main_menu_kb(is_admin=user.id in ADMIN_IDS),
    )


@dp.callback_query(F.data == "sub:noop")
async def cb_sub_noop(callback: CallbackQuery) -> None:
    await callback.answer(
        "Ushbu kanalga ID orqali kiring yoki bot adminligini tekshiring.",
        show_alert=True,
    )


# --- Adminga xabar yuborish ---
@dp.message(F.text == "📩 Adminga xabar yuborish")
async def ask_admin_message(message: Message, bot: Bot, state: FSMContext) -> None:
    if not await ensure_subscribed(bot, message):
        return
    await state.set_state(UserStates.waiting_admin_message)
    await message.answer(
        "Adminga yubormoqchi bo‘lgan xabaringizni yozing (bekor qilish uchun /cancel bosing):"
    )


@dp.message(Command("cancel"))
async def cmd_cancel(message: Message, state: FSMContext) -> None:
    await state.clear()
    user = message.from_user
    await message.answer(
        "Amal bekor qilindi.",
        reply_markup=main_menu_kb(is_admin=bool(user and user.id in ADMIN_IDS)),
    )


@dp.message(UserStates.waiting_admin_message, F.text)
async def send_to_admins(message: Message, bot: Bot, state: FSMContext) -> None:
    await state.clear()
    user = message.from_user
    if not user:
        return
    if not ADMIN_IDS:
        await message.answer("Hozircha admin manzili sozlanmagan.")
        return

    uname = f"@{user.username}" if user.username else "yo‘q"
    header = (
        f"📩 <b>Yangi xabar!</b>\n"
        f"👤 Foydalanuvchi: {user.full_name} ({uname})\n"
        f"🆔 ID: <code>{user.id}</code>\n\n"
        f"💬 Xabar:\n{message.text}"
    )
    reply_kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="↩️ Javob yozish", callback_data=f"adm:reply:{user.id}"
                )
            ]
        ]
    )
    delivered = 0
    for admin_id in ADMIN_IDS:
        try:
            await bot.send_message(
                chat_id=admin_id, text=header, reply_markup=reply_kb, parse_mode="HTML"
            )
            delivered += 1
        except Exception:
            pass

    if delivered:
        await message.answer(
            "Xabaringiz adminga yuborildi!",
            reply_markup=main_menu_kb(is_admin=user.id in ADMIN_IDS),
        )
    else:
        await message.answer(
            "Xabarni yetkazib bo‘lmadi, keyinroq urinib ko‘ring.",
            reply_markup=main_menu_kb(is_admin=user.id in ADMIN_IDS),
        )


# --- Admin panel ---
@dp.message(Command("admin"))
@dp.message(F.text == "🛠 Admin panel")
async def open_admin_panel(message: Message, state: FSMContext) -> None:
    user = message.from_user
    if not user or user.id not in ADMIN_IDS:
        return
    await state.clear()
    await message.answer("🛠 <b>Admin panel</b>", reply_markup=admin_panel_kb(), parse_mode="HTML")


@dp.callback_query(F.data == "adm:stats")
async def cb_admin_stats(callback: CallbackQuery) -> None:
    user = callback.from_user
    if not user or user.id not in ADMIN_IDS:
        await callback.answer("Ruxsat yo‘q", show_alert=True)
        return
    total_users, today_users, downloads = get_stats()
    text = (
        "📊 <b>Bot statistikasi</b>\n\n"
        f"👥 Jami foydalanuvchilar: <b>{total_users}</b>\n"
        f"🆕 Bugun qo‘shilganlar: <b>{today_users}</b>\n"
        f"📥 Yuklangan videolar soni: <b>{downloads}</b>"
    )
    await callback.message.answer(text, reply_markup=admin_panel_kb(), parse_mode="HTML")
    await callback.answer()


@dp.callback_query(F.data == "adm:add_ch")
async def cb_add_channel(callback: CallbackQuery, state: FSMContext) -> None:
    user = callback.from_user
    if not user or user.id not in ADMIN_IDS:
        await callback.answer("Ruxsat yo‘q", show_alert=True)
        return
    await state.set_state(AdminStates.waiting_add_channel)
    await callback.message.answer(
        "Majburiy obuna kanalining <b>@username</b>’ini yoki <b>chat ID</b>’sini (masalan, <code>-1001234567890</code>) yuboring.\n"
        "<i>Eslatma: Bot o‘sha kanalda admin bo‘lishi kerak.</i>\n"
        "Bekor qilish: /cancel",
        parse_mode="HTML",
    )
    await callback.answer()


@dp.message(AdminStates.waiting_add_channel, F.text)
async def save_new_channel(message: Message, bot: Bot, state: FSMContext) -> None:
    user = message.from_user
    if not user or user.id not in ADMIN_IDS:
        return
    ref = message.text.strip()
    target: int | str = int(ref) if ref.lstrip("-").isdigit() else ref
    title = ref
    try:
        chat = await bot.get_chat(target)
        title = chat.title or ref
    except Exception:
        pass

    ok = add_channel(ref, title)
    await state.clear()
    if ok:
        await message.answer(
            f"✅ Kanal qo‘shildi: <b>{title}</b> (<code>{ref}</code>)",
            reply_markup=admin_panel_kb(),
            parse_mode="HTML",
        )
    else:
        await message.answer(
            "❌ Kanal qo‘shilmadi. Formatni tekshiring yoki bu kanal allaqachon ro‘yxatda borligini ko‘ring.",
            reply_markup=admin_panel_kb(),
        )


@dp.callback_query(F.data == "adm:list_ch")
async def cb_list_channels(callback: CallbackQuery) -> None:
    user = callback.from_user
    if not user or user.id not in ADMIN_IDS:
        await callback.answer("Ruxsat yo‘q", show_alert=True)
        return
    channels = list_channels()
    if not channels:
        await callback.message.answer(
            "Hozircha majburiy obuna kanallari qo‘shilmagan.",
            reply_markup=admin_panel_kb(),
        )
        await callback.answer()
        return

    kb_rows: list[list[InlineKeyboardButton]] = []
    lines = ["📋 <b>Majburiy obuna kanallari:</b>\n"]
    for ch_id, chat_ref, title in channels:
        lines.append(f"• {title} (<code>{chat_ref}</code>)")
        kb_rows.append(
            [
                InlineKeyboardButton(
                    text=f"🗑 O‘chirish: {title}", callback_data=f"adm:del_ch:{ch_id}"
                )
            ]
        )
    kb_rows.append(
        [InlineKeyboardButton(text="➕ Yangi kanal qo‘shish", callback_data="adm:add_ch")]
    )
    await callback.message.answer(
        "\n".join(lines),
        reply_markup=InlineKeyboardMarkup(inline_keyboard=kb_rows),
        parse_mode="HTML",
    )
    await callback.answer()


@dp.callback_query(F.data.startswith("adm:del_ch:"))
async def cb_delete_channel(callback: CallbackQuery) -> None:
    user = callback.from_user
    if not user or user.id not in ADMIN_IDS:
        await callback.answer("Ruxsat yo‘q", show_alert=True)
        return
    try:
        ch_id = int(callback.data.split(":")[-1])
    except ValueError:
        await callback.answer("Noto‘g‘ri ID", show_alert=True)
        return
    remove_channel(ch_id)
    await callback.answer("Kanal o‘chirildi", show_alert=True)
    await cb_list_channels(callback)


@dp.callback_query(F.data.startswith("adm:reply:"))
async def cb_reply_prompt(callback: CallbackQuery, state: FSMContext) -> None:
    user = callback.from_user
    if not user or user.id not in ADMIN_IDS:
        await callback.answer("Ruxsat yo‘q", show_alert=True)
        return
    target_id = int(callback.data.split(":")[-1])
    await state.set_state(AdminStates.waiting_reply_user)
    await state.update_data(reply_target_id=target_id)
    await callback.message.answer(
        f"ID <code>{target_id}</code> foydalanuvchiga yuboriladigan javob matnini kiriting (bekor qilish: /cancel):",
        parse_mode="HTML",
    )
    await callback.answer()


@dp.message(AdminStates.waiting_reply_user, F.text)
async def send_admin_reply(message: Message, bot: Bot, state: FSMContext) -> None:
    user = message.from_user
    if not user or user.id not in ADMIN_IDS:
        return
    data = await state.get_data()
    target_id = data.get("reply_target_id")
    await state.clear()
    if not target_id:
        return
    try:
        await bot.send_message(
            chat_id=int(target_id),
            text=f"📨 <b>Admindan javob:</b>\n\n{message.text}",
            parse_mode="HTML",
        )
        await message.answer("Javob yuborildi!", reply_markup=admin_panel_kb())
    except Exception:
        await message.answer(
            "Foydalanuvchiga javob yuborib bo‘lmadi (botni bloklagan bo‘lishi mumkin).",
            reply_markup=admin_panel_kb(),
        )


# --- Video yuklash handleri ---
@dp.message(F.text)
async def handle_video(message: Message, bot: Bot) -> None:
    user = message.from_user
    if not user:
        return
    upsert_user(user.id, user.username, user.full_name)

    if not await ensure_subscribed(bot, message):
        return

    match = URL_PATTERN.search(message.text or "")
    if not match:
        await message.answer(
            "Iltimos, 3 daqiqagacha bo‘lgan Instagram yoki TikTok video havolasini yuboring "
            "yoki pastdagi menyudan foydalaning.",
            reply_markup=main_menu_kb(is_admin=user.id in ADMIN_IDS),
        )
        return

    url = match.group(1)
    status_msg = await message.answer("Video yuklanmoqda, biroz kuting...")

    with tempfile.TemporaryDirectory() as tmp_dir:
        out_dir = Path(tmp_dir)
        try:
            video_path = await asyncio.to_thread(download_video, url, out_dir)
            file_size = video_path.stat().st_size

            if file_size > MAX_FILE_SIZE_BYTES:
                await status_msg.edit_text(
                    "Video hajmi Telegram bot uchun ruxsat etilgan chegaradan katta."
                )
                return

            await status_msg.edit_text("Video yuborilmoqda...")
            await message.answer_video(
                video=FSInputFile(video_path),
                caption="Tayyor!",
            )
            increment_downloads()
            await status_msg.delete()

        except ValueError as exc:
            await status_msg.edit_text(f"Xatolik: {exc}")
        except Exception:
            await status_msg.edit_text(
                "Videoni yuklab bo‘lmadi. Havola ochiq (public) ekanligini tekshiring "
                "yoki keyinroq qayta urinib ko‘ring."
            )


async def main() -> None:
    if not BOT_TOKEN:
        raise ValueError("BOT_TOKEN muhit o'zgaruvchisi topilmadi.")
    init_db()
    bot = Bot(token=BOT_TOKEN)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
