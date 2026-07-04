import os
import io
import logging
from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    filters,
    ContextTypes,
)
from PIL import Image

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.environ["BOT_TOKEN"]

DEFAULT_FPS  = 2
DEFAULT_LOOP = 0
MAX_IMAGES   = 30
MAX_FILE_MB  = 20

sessions: dict[int, dict] = {}


def get_session(user_id: int) -> dict:
    if user_id not in sessions:
        sessions[user_id] = {
            "images": [],
            "fps":    DEFAULT_FPS,
            "loop":   DEFAULT_LOOP,
        }
    return sessions[user_id]


def format_loop(loop: int) -> str:
    return "∞" if loop == 0 else str(loop)


def make_gif_bytes(images: list, fps: int, loop: int) -> io.BytesIO:
    base_w, base_h = images[0].size
    frames = []

    for img in images:
        resized = img.resize((base_w, base_h), Image.LANCZOS)
        rgb = Image.new("RGB", resized.size, (255, 255, 255))
        mask = resized.split()[3] if resized.mode == "RGBA" else None
        rgb.paste(resized.convert("RGB"), mask=mask)
        frames.append(rgb.quantize(colors=256, method=Image.Quantize.MEDIANCUT))

    buf = io.BytesIO()
    frames[0].save(
        buf,
        format="GIF",
        save_all=True,
        append_images=frames[1:],
        duration=int(1000 / fps),
        loop=loop,
        optimize=True,
    )
    buf.seek(0)
    return buf


async def load_image_from_bytes(raw: bytearray):
    try:
        return Image.open(io.BytesIO(raw)).convert("RGBA")
    except Exception:
        return None


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "🎞 GIF Maker Bot\n\n"
        "Send me images, then use /make_gif to get your animated GIF!\n\n"
        "Commands:\n"
        "/make_gif — Create GIF from queued images\n"
        "/status   — Queue size & settings\n"
        "/clear    — Wipe your queue\n"
        "/setfps <1-24> — Frame rate (default 2)\n"
        "/setloop <0=∞ | N> — Loop count (default 0)\n"
        "/help     — Show this message"
    )


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await cmd_start(update, context)


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    s = get_session(update.effective_user.id)
    await update.message.reply_text(
        f"Queue: {len(s['images'])} / {MAX_IMAGES} images\n"
        f"FPS: {s['fps']}\n"
        f"Loop: {format_loop(s['loop'])}"
    )


async def cmd_clear(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    get_session(update.effective_user.id)["images"].clear()
    await update.message.reply_text("🗑️ Queue cleared.")


async def cmd_settings(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    s = get_session(update.effective_user.id)
    await update.message.reply_text(
        f"Settings:\n"
        f"FPS : {s['fps']}  (change: /setfps)\n"
        f"Loop: {format_loop(s['loop'])}  (change: /setloop)"
    )


async def cmd_setfps(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        fps = int(context.args[0])
        assert 1 <= fps <= 24
    except (IndexError, ValueError, AssertionError):
        await update.message.reply_text("Usage: /setfps <1-24>")
        return
    get_session(update.effective_user.id)["fps"] = fps
    await update.message.reply_text(f"✅ FPS set to {fps}")


async def cmd_setloop(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        loop = int(context.args[0])
        assert loop >= 0
    except (IndexError, ValueError, AssertionError):
        await update.message.reply_text("Usage: /setloop <0=infinite | N>")
        return
    get_session(update.effective_user.id)["loop"] = loop
    label = "∞ (infinite)" if loop == 0 else str(loop)
    await update.message.reply_text(f"✅ Loop set to {label}")


async def cmd_make_gif(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    s = get_session(update.effective_user.id)
    images = s["images"]

    if len(images) < 2:
        await update.message.reply_text(
            "⚠️ Send at least 2 images first, then /make_gif."
        )
        return

    status_msg = await update.message.reply_text(
        f"⚙️ Stitching {len(images)} frames at {s['fps']} FPS…"
    )

    try:
        buf = make_gif_bytes(images, s["fps"], s["loop"])
    except Exception as exc:
        logger.exception("GIF creation failed")
        await status_msg.edit_text(f"❌ Failed: {exc}")
        return

    await status_msg.delete()
    await update.message.reply_document(
        document=buf,
        filename="animation.gif",
        caption=(
            f"🎉 Your GIF!\n"
            f"{len(images)} frames · {s['fps']} FPS · loop {format_loop(s['loop'])}"
        ),
    )
    s["images"].clear()
    await update.message.reply_text("Queue cleared. Send new images whenever you're ready!")


async def _add_image(update: Update, img) -> None:
    s = get_session(update.effective_user.id)
    if len(s["images"]) >= MAX_IMAGES:
        await update.message.reply_text(
            f"⚠️ Queue full ({MAX_IMAGES} images). Use /make_gif or /clear first."
        )
        return
    s["images"].append(img)
    n = len(s["images"])
    await update.message.reply_text(f"✅ Image {n} added. Send more or /make_gif")


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    photo_file = await update.message.photo[-1].get_file()
    raw = await photo_file.download_as_bytearray()
    img = await load_image_from_bytes(raw)
    if img is None:
        await update.message.reply_text("❌ Could not read that image.")
        return
    await _add_image(update, img)


async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    doc = update.message.document
    if not doc.mime_type or not doc.mime_type.startswith("image/"):
        return
    if doc.file_size and doc.file_size > MAX_FILE_MB * 1024 * 1024:
        await update.message.reply_text(f"❌ File too large (max {MAX_FILE_MB} MB).")
        return
    file = await doc.get_file()
    raw = await file.download_as_bytearray()
    img = await load_image_from_bytes(raw)
    if img is None:
        await update.message.reply_text("❌ Could not read that file.")
        return
    await _add_image(update, img)


def main() -> None:
    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start",    cmd_start))
    app.add_handler(CommandHandler("help",     cmd_help))
    app.add_handler(CommandHandler("status",   cmd_status))
    app.add_handler(CommandHandler("clear",    cmd_clear))
    app.add_handler(CommandHandler("settings", cmd_settings))
    app.add_handler(CommandHandler("setfps",   cmd_setfps))
    app.add_handler(CommandHandler("setloop",  cmd_setloop))
    app.add_handler(CommandHandler("make_gif", cmd_make_gif))
    app.add_handler(MessageHandler(filters.PHOTO,          handle_photo))
    app.add_handler(MessageHandler(filters.Document.IMAGE, handle_document))

    logger.info("Bot started — polling…")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
