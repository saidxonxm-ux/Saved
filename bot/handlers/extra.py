import logging

from aiogram import Bot, F, Router
from aiogram.filters import Command
from aiogram.types import Message

from ..i18n import t
from .user import extract_url, handle_link, is_link

log = logging.getLogger(__name__)
router = Router()          # (bo'sh, main.py bilan moslik uchun)
group = Router()           # guruhlar: faqat linklar
group.message.filter(F.chat.type.in_({"group", "supergroup"}))


@group.message(Command("start", "help"))
async def group_start(m: Message, lang: str = "uz"):
    await m.answer(t(lang, "group_hint"))


@group.message(is_link)
async def on_group_link(m: Message, bot: Bot, lang: str = "uz"):
    if m.from_user is None:
        return
    await handle_link(m, bot, lang, extract_url(m), 720, m.from_user.id)
