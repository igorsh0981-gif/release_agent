"""
@ReleaseAgent_Bot — оркестратор агентов анализа
Принимает /start_analysis + JSON от @InformNBU_bot,
запускает цепочку BA → SA → QATC → PM,
уведомляет по каждой фазе, сохраняет артефакты.
"""

import os
import json
import logging
import asyncio
from typing import Dict

from telegram import Update, Bot
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    filters,
    ContextTypes,
)

from models.task import Task
from agents.ba_agent import run_ba
from agents.sa_agent import run_sa
from agents.qatc_agent import run_qatc
from agents.pm_agent import run_pm
from services.gdrive import create_feature_folder, upload_all_artifacts
from services.notion import create_feature_page, update_feature_page
from services.notifier import (
    notify_started, notify_packing, notify_done, notify_error
)

# ── Логирование ────────────────────────────────────────────────────────────────
logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# ── Конфигурация ───────────────────────────────────────────────────────────────
TELEGRAM_TOKEN = os.getenv(
    "TELEGRAM_TOKEN",
    "8816881404:AAFapEGQyxBLuBMCDPZYtfXlFsfqCqtjq10"
)

# Хранилище активных задач и очередей ответов BA
active_tasks: Dict[int, Dict[str, asyncio.Queue]] = {}
pending_tasks: Dict[int, str] = {}
awaiting_ba_answer: Dict[int, str] = {}


async def handle_start_analysis(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    /start_analysis {task_id}
    """
    chat_id = update.message.chat.id
    args = context.args

    logger.info(f"[CMD] /start_analysis | chat: {chat_id} | args: {args}")

    if not args:
        await update.message.reply_text("Использование: /start_analysis {task_id}")
        return

    task_id = args[0]
    pending_tasks[chat_id] = task_id
    logger.info(f"[CMD] Ожидаю JSON task_id={task_id} chat={chat_id}")


async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Обрабатывает входящий JSON файл с описанием задачи.
    """
    message = update.message
    chat_id = message.chat.id
    doc = message.document

    logger.info(f"[DOC] Документ | chat: {chat_id} | file: {doc.file_name if doc else None}")

    if not doc or not doc.file_name or not doc.file_name.endswith(".json"):
        return

    # Извлекаем task_id из имени файла (task_136.json → 136)
    try:
        task_id = doc.file_name.replace("task_", "").replace(".json", "")
        logger.info(f"[DOC] task_id={task_id}")
    except Exception:
        task_id = pending_tasks.get(chat_id, "unknown")

    pending_tasks.pop(chat_id, None)

    # Скачиваем JSON
    try:
        file_info = await context.bot.get_file(doc.file_id)
        file_bytes = await file_info.download_as_bytearray()
        payload = json.loads(file_bytes.decode("utf-8"))
        logger.info(f"[DOC] JSON загружен: {len(file_bytes)} байт")
    except Exception as e:
        logger.error(f"[DOC] Ошибка чтения JSON: {e}")
        await message.reply_text(f"❌ Не удалось прочитать JSON: {e}")
        return

    task = Task.from_payload(payload)
    logger.info(f"[DOC] Задача создана: {task.feature_name} | {task.task_id}")

    # Создаём очередь для ответов BA
    answer_queue = asyncio.Queue()
    if chat_id not in active_tasks:
        active_tasks[chat_id] = {}
    active_tasks[chat_id][task.task_id] = answer_queue

    # Запускаем цепочку в фоне
    asyncio.create_task(
        run_analysis_chain(task, context.bot, chat_id, answer_queue)
    )


async def handle_ba_answer(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Обрабатывает ответы пользователя на вопросы BA агента.
    """
    chat_id = update.message.chat.id
    text = update.message.text or ""

    if chat_id not in active_tasks or not active_tasks[chat_id]:
        return

    for task_id, queue in active_tasks[chat_id].items():
        logger.info(f"[BA] Ответ получен для задачи {task_id}: {text[:50]}")
        await queue.put(text)
        break


async def run_analysis_chain(
    task: Task,
    bot: Bot,
    chat_id: int,
    answer_queue: asyncio.Queue,
) -> None:
    """
    Основная цепочка: BA → SA → QATC → PM → GDrive → Notion.
    """
    logger.info(f"[CHAIN] Запуск: {task.feature_name}")

    try:
        await notify_started(bot, chat_id, task)

        folder_id, folder_url = await create_feature_folder(task.feature_name)
        task.gdrive_feature_folder_id = folder_id
        task.gdrive_feature_folder_url = folder_url
        logger.info(f"[CHAIN] GDrive папка: {folder_url}")

        notion_url = await create_feature_page(task)
        task.notion_page_url = notion_url

        # BA
        task = await run_ba(task, bot, chat_id, answer_queue)

        # SA
        task = await run_sa(task, bot, chat_id)

        # QATC
        task = await run_qatc(task, bot, chat_id)

        # PM
        task = await run_pm(task, bot, chat_id)

        # Упаковка
        await notify_packing(bot, chat_id)
        await upload_all_artifacts(task)

        if notion_url:
            await update_feature_page(notion_url, task)

        await notify_done(bot, chat_id, task)
        logger.info(f"[CHAIN] Завершено: {task.feature_name}")

    except Exception as e:
        logger.error(f"[CHAIN] Ошибка: {e}", exc_info=True)
        await notify_error(bot, chat_id, task.task_id, "SYSTEM", str(e))

    finally:
        if chat_id in active_tasks:
            active_tasks[chat_id].pop(task.task_id, None)


def main() -> None:
    if not TELEGRAM_TOKEN:
        raise ValueError("TELEGRAM_TOKEN не задан")

    logger.info("Запуск @ReleaseAgent_Bot...")

    app = Application.builder().token(TELEGRAM_TOKEN).build()

    app.add_handler(CommandHandler("start_analysis", handle_start_analysis))
    app.add_handler(CommandHandler("start", lambda u, c: u.message.reply_text(
        "🤖 ReleaseAgent_Bot запущен\nОжидаю команду /start_analysis от @InformNBU_bot"
    )))
    app.add_handler(MessageHandler(filters.Document.ALL, handle_document))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_ba_answer))

    logger.info("ReleaseAgent_Bot запущен. Ожидаю задачи...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
