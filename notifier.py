import logging
from telegram import Bot

logger = logging.getLogger(__name__)


async def send(bot: Bot, chat_id: int, text: str) -> None:
    """Базовая отправка сообщения"""
    try:
        await bot.send_message(chat_id=chat_id, text=text)
    except Exception as e:
        logger.error(f"Ошибка отправки уведомления: {e}")


# ── Фазовые уведомления ────────────────────────────────────────────────────────

async def notify_started(bot: Bot, chat_id: int, task) -> None:
    await send(bot, chat_id,
        f"⏳ Анализ запущен\n"
        f"📋 Задача: {task.feature_name}\n"
        f"🆔 #{task.task_id}\n\n"
        f"Запускаю BA агента..."
    )


async def notify_ba_questions(bot: Bot, chat_id: int, questions: str, attempt: int) -> None:
    await send(bot, chat_id,
        f"❓ BA агент запрашивает уточнения (попытка {attempt}/5)\n\n"
        f"{questions}\n\n"
        f"⏰ Ожидаю ответ 5 минут. Если ответа нет — продолжу без уточнений."
    )


async def notify_ba_timeout(bot: Bot, chat_id: int) -> None:
    await send(bot, chat_id,
        "⚠️ Ответ на вопросы BA не получен (таймаут 5 мин)\n"
        "Продолжаю с пометкой [ASSUMED]"
    )


async def notify_ba_done(bot: Bot, chat_id: int, summary: str) -> None:
    await send(bot, chat_id,
        f"✅ BA завершён — бизнес-требования сформированы\n"
        f"📄 {summary}\n\n"
        f"➡️ Передаю системному аналитику..."
    )


async def notify_sa_done(bot: Bot, chat_id: int, summary: str) -> None:
    await send(bot, chat_id,
        f"✅ SA завершён — системный анализ готов\n"
        f"📄 {summary}\n\n"
        f"➡️ Передаю QATC агенту..."
    )


async def notify_qatc_done(bot: Bot, chat_id: int, summary: str) -> None:
    await send(bot, chat_id,
        f"✅ QATC завершён — тест-кейсы подготовлены\n"
        f"📄 {summary}\n\n"
        f"➡️ Передаю PM агенту..."
    )


async def notify_pm_done(bot: Bot, chat_id: int, summary: str) -> None:
    await send(bot, chat_id,
        f"✅ PM завершён — план и артефакты сформированы\n"
        f"📄 {summary}\n\n"
        f"📦 Формирую пакет документов..."
    )


async def notify_packing(bot: Bot, chat_id: int) -> None:
    await send(bot, chat_id,
        "📦 Упаковываю артефакты...\n"
        "Загружаю файлы в Google Drive и Notion"
    )


async def notify_done(bot: Bot, chat_id: int, task) -> None:
    await send(bot, chat_id,
        f"✅ Все документы готовы\n"
        f"📁 {task.feature_name}\n"
        f"🔗 {task.gdrive_feature_folder_url}\n\n"
        f"Артефакты ({len(_artifact_list(task))} файлов):\n"
        + "\n".join(f"  • {a}" for a in _artifact_list(task))
    )


async def notify_error(bot: Bot, chat_id: int, task_id: str, phase: str, error: str) -> None:
    await send(bot, chat_id,
        f"❌ Ошибка в фазе {phase}\n"
        f"Задача #{task_id}\n"
        f"Причина: {error}\n\n"
        f"Попробуйте повторить запрос."
    )


def _artifact_list(task) -> list:
    files = [
        f"ba_{task.task_id}.md",
        f"ba_full_{task.task_id}.md",
        f"sa_{task.task_id}.md",
        f"sa_full_{task.task_id}.md",
        f"qatc_{task.task_id}.md",
        f"qatc_full_{task.task_id}.md",
        f"pm_protocol_{task.task_id}.md",
        f"pm_jira_{task.task_id}.md",
        f"pm_epics_{task.task_id}.md",
        f"pm_risks_{task.task_id}.md",
        f"pm_raci_{task.task_id}.md",
        f"pm_team_{task.task_id}.md",
        f"projekt_tasks_{task.task_id}.csv",
        f"projekt_tasks_{task.task_id}.xlsx",
    ]
    return files
