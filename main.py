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
# { chat_id: { task_id: asyncio.Queue } }
active_tasks: Dict[int, Dict[str, asyncio.Queue]] = {}
# { chat_id: task_id } — текущая задача ожидающая ответа BA
awaiting_ba_answer: Dict[int, str] = {}


async def handle_start_analysis(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    /start_analysis {task_id}
    Запускает цепочку агентов. JSON приходит следующим сообщением (файлом).
    """
    chat_id = update.message.chat.id
    args = context.args

    if not args:
        await update.message.reply_text("Использование: /start_analysis {task_id}")
        return

    task_id = args[0]
    logger.info(f"Получен /start_analysis | task_id: {task_id} | chat: {chat_id}")

    # Сохраняем task_id — JSON придёт следующим сообщением
    context.chat_data["pending_task_id"] = task_id
    logger.info(f"Ожидаю JSON для task_id: {task_id}")


async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Обрабатывает входящий JSON файл с описанием задачи.
    Запускает полную цепочку агентов асинхронно.
    """
    message = update.message
    chat_id = message.chat.id
    doc = message.document

    if not doc or not doc.file_name or not doc.file_name.endswith(".json"):
        return

    # Проверяем что ждём JSON для задачи
    pending_task_id = context.chat_data.get("pending_task_id")
    if not pending_task_id:
        return

    if pending_task_id not in doc.file_name:
        logger.warning(f"JSON файл {doc.file_name} не соответствует task_id {pending_task_id}")
        return

    # Сбрасываем ожидание
    context.chat_data.pop("pending_task_id", None)

    # Скачиваем JSON
    try:
        file_info = await context.bot.get_file(doc.file_id)
        file_bytes = await file_info.download_as_bytearray()
        payload = json.loads(file_bytes.decode("utf-8"))
    except Exception as e:
        logger.error(f"Ошибка чтения JSON: {e}")
        await message.reply_text(f"❌ Не удалось прочитать JSON задачи: {e}")
        return

    task = Task.from_payload(payload)
    logger.info(f"Задача создана: {task.feature_name} | {task.task_id}")

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
    Любое текстовое сообщение (не команда) в чат с активной задачей.
    """
    chat_id = update.message.chat.id
    text = update.message.text or ""

    # Проверяем есть ли активная задача ожидающая ответа
    if chat_id not in active_tasks or not active_tasks[chat_id]:
        return

    # Берём первую активную очередь (обычно одна задача за раз)
    for task_id, queue in active_tasks[chat_id].items():
        if not queue.empty():
            continue  # очередь уже занята
        logger.info(f"BA ответ получен для задачи {task_id}: {text[:50]}")
        await queue.put(text)
        break


async def run_analysis_chain(
    task: Task,
    bot: Bot,
    chat_id: int,
    answer_queue: asyncio.Queue,
) -> None:
    """
    Основная цепочка: BA → SA → QATC → PM → GDrive → Notion → уведомление.
    """
    logger.info(f"Цепочка запущена: {task.feature_name}")

    try:
        # ── Старт ─────────────────────────────────────────────────────────────
        await notify_started(bot, chat_id, task)

        # Создаём папку GDrive сразу (чтобы ссылка была в Notion с начала)
        folder_id, folder_url = await create_feature_folder(task.feature_name)
        task.gdrive_feature_folder_id = folder_id
        task.gdrive_feature_folder_url = folder_url

        # Создаём страницу Notion
        notion_url = await create_feature_page(task)
        task.notion_page_url = notion_url

        # ── BA ────────────────────────────────────────────────────────────────
        try:
            task = await run_ba(task, bot, chat_id, answer_queue)
        except Exception as e:
            await notify_error(bot, chat_id, task.task_id, "BA", str(e))
            raise

        # ── SA ────────────────────────────────────────────────────────────────
        try:
            task = await run_sa(task, bot, chat_id)
        except Exception as e:
            await notify_error(bot, chat_id, task.task_id, "SA", str(e))
            raise

        # ── QATC ──────────────────────────────────────────────────────────────
        try:
            task = await run_qatc(task, bot, chat_id)
        except Exception as e:
            await notify_error(bot, chat_id, task.task_id, "QATC", str(e))
            raise

        # ── PM ────────────────────────────────────────────────────────────────
        try:
            task = await run_pm(task, bot, chat_id)
        except Exception as e:
            await notify_error(bot, chat_id, task.task_id, "PM", str(e))
            raise

        # ── Упаковка артефактов ───────────────────────────────────────────────
        await notify_packing(bot, chat_id)
        await upload_all_artifacts(task)

        # ── Обновляем Notion ──────────────────────────────────────────────────
        if notion_url:
            await update_feature_page(notion_url, task)

        # ── Финальное уведомление ─────────────────────────────────────────────
        await notify_done(bot, chat_id, task)
        task.status = "done"
        logger.info(f"Цепочка завершена: {task.feature_name}")

    except Exception as e:
        logger.error(f"Критическая ошибка цепочки: {e}", exc_info=True)
        await notify_error(bot, chat_id, task.task_id, "SYSTEM", str(e))

    finally:
        # Убираем задачу из активных
        if chat_id in active_tasks:
            active_tasks[chat_id].pop(task.task_id, None)


def main() -> None:
    if not TELEGRAM_TOKEN:
        raise ValueError("TELEGRAM_TOKEN не задан")

    logger.info("Запуск @ReleaseAgent_Bot...")

    app = Application.builder().token(TELEGRAM_TOKEN).build()

    # Команды
    app.add_handler(CommandHandler("start_analysis", handle_start_analysis))
    app.add_handler(CommandHandler("start", lambda u, c: u.message.reply_text(
        "🤖 ReleaseAgent_Bot запущен\nОжидаю команду /start_analysis от @InformNBU_bot"
    )))

    # JSON файл с задачей
    app.add_handler(
        MessageHandler(filters.Document.ALL, handle_document)
    )

    # Ответы на вопросы BA (любой текст не-команда)
    app.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, handle_ba_answer)
    )

    logger.info("ReleaseAgent_Bot запущен. Ожидаю задачи...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
