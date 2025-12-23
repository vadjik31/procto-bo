import os
from dataclasses import dataclass
from typing import Awaitable, Callable

from aiogram import Bot, Dispatcher, F
from aiogram.enums import ChatAction
from aiogram.filters import CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)


class LeadForm(StatesGroup):
    email = State()
    age = State()
    gender = State()
    country = State()
    language = State()
    english_level = State()
    amazon_experience = State()


@dataclass
class LeadProfile:
    telegram_id: int
    email: str
    age: str
    gender: str
    country: str
    language: str
    english_level: str
    amazon_experience: str


OnLeadCompleted = Callable[[LeadProfile], Awaitable[str]]


HELP_LOGIN_CB = "help_login"

def help_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🆘 Не нашёл ссылку / не могу зайти на платформу",
                    callback_data=HELP_LOGIN_CB,
                )
            ]
        ]
    )


def build_help_login_text(email: str) -> str:
    # можно легко менять ссылку через env, без правки кода
    course_link = os.getenv("SKILLSPACE_PUBLIC_COURSE_URL", "https://855f92.skillspace.ru/course/102877").strip()

    return (
        "🆘 Инструкция, если не пришло письмо или не получается зайти\n\n"
        "1) Переходим на skillspace.ru\n"
        "   🇺🇦 Если вы проживаете в Украине — вам может понадобиться браузер Brave "
        "или любой VPN, который скрывает IP. Не важно какой именно VPN. "
        "Я рекомендую Brave либо любой бесплатный аналог.\n"
        "   🌍 Если вы не проживаете в Украине — VPN не нужен.\n\n"
        f"2) Заходим по ссылке: {course_link}\n"
        "   Сайт попросит логин и пароль.\n"
        "   Нажимаем «Забыли пароль» / «Проблемы со входом».\n"
        f"   Указываем ту же почту, что вводили в боте: {email}\n"
        "   Дальше устанавливаем новый пароль — и всё готово ✅"
    )


class BotService:
    def __init__(self, token: str, on_lead_completed: OnLeadCompleted):
        self.bot = Bot(token=token)
        self.dp = Dispatcher(storage=MemoryStorage())
        self.on_lead_completed = on_lead_completed
        self._register_handlers()

    def _register_handlers(self) -> None:
        dp = self.dp

        @dp.message(CommandStart())
        async def start(m: Message, state: FSMContext):
            await state.clear()
            await m.answer(
                "Привет! 👋\n\n"
                "Я помогу записаться на курс.\n"
                "Ответь на пару вопросов — это быстро 🙂\n\n"
                "1/7 — Напиши email (который будешь использовать в Skillspace):"
            )
            await state.set_state(LeadForm.email)

        @dp.message(LeadForm.email, F.text)
        async def got_email(m: Message, state: FSMContext):
            email = (m.text or "").strip()
            if "@" not in email or "." not in email:
                await m.answer("Похоже, email некорректный. Введи, пожалуйста, нормальный email:")
                return
            await state.update_data(email=email)
            await m.answer("2/7 — Возраст (только цифры):")
            await state.set_state(LeadForm.age)

        @dp.message(LeadForm.age, F.text)
        async def got_age(m: Message, state: FSMContext):
            age = (m.text or "").strip()
            if not age.isdigit():
                await m.answer("Возраст нужен числом 🙂 Введи только цифры:")
                return
            await state.update_data(age=age)
            await m.answer("3/7 — Пол (М/Ж/Другое):")
            await state.set_state(LeadForm.gender)

        @dp.message(LeadForm.gender, F.text)
        async def got_gender(m: Message, state: FSMContext):
            await state.update_data(gender=(m.text or "").strip())
            await m.answer("4/7 — Страна:")
            await state.set_state(LeadForm.country)

        @dp.message(LeadForm.country, F.text)
        async def got_country(m: Message, state: FSMContext):
            await state.update_data(country=(m.text or "").strip())
            await m.answer("5/7 — Язык общения (например RU или EN):")
            await state.set_state(LeadForm.language)

        @dp.message(LeadForm.language, F.text)
        async def got_language(m: Message, state: FSMContext):
            await state.update_data(language=(m.text or "").strip())
            await m.answer("6/7 — Уровень английского (A1/A2/B1/B2/C1/C2):")
            await state.set_state(LeadForm.english_level)

        @dp.message(LeadForm.english_level, F.text)
        async def got_level(m: Message, state: FSMContext):
            await state.update_data(english_level=(m.text or "").strip())
            await m.answer("7/7 — Опыт с Amazon (нет / немного / продаю / другое):")
            await state.set_state(LeadForm.amazon_experience)

        @dp.message(LeadForm.amazon_experience, F.text)
        async def got_exp(m: Message, state: FSMContext):
            # ✅ Заглушка, чтобы не было “тишины”
            await m.answer(
                "⏳ Принял(а)! Сейчас оформляю доступ к курсу…\n"
                "Это может занять до 1–2 минут. Пожалуйста, подождите 🙂"
            )
            try:
                await self.bot.send_chat_action(m.chat.id, ChatAction.TYPING)
            except Exception:
                pass

            # Собираем профиль
            data = await state.get_data()
            profile = LeadProfile(
                telegram_id=m.from_user.id,
                email=data.get("email", ""),
                age=data.get("age", ""),
                gender=data.get("gender", ""),
                country=data.get("country", ""),
                language=data.get("language", ""),
                english_level=data.get("english_level", ""),
                amazon_experience=(m.text or "").strip(),
            )

            # Тяжёлая часть
            reply = await self.on_lead_completed(profile)

            # ✅ Финальный ответ + кнопка помощи
            await m.answer(reply, reply_markup=help_keyboard())
            await state.clear()

        # ✅ Обработка нажатия кнопки
        @dp.callback_query(F.data == HELP_LOGIN_CB)
        async def help_login(cb: CallbackQuery, state: FSMContext):
            # чтобы убрать "часики" на кнопке
            try:
                await cb.answer()
            except Exception:
                pass

            # Постараемся взять email из состояния (если есть), иначе покажем общий текст
            data = await state.get_data()
            email = (data.get("email") or "").strip()

            # Если state пустой (после анкеты мы его clear), email может отсутствовать.
            # В этом случае выдаём инструкцию без подстановки email.
            if email:
                text = build_help_login_text(email)
            else:
                # общий вариант
                course_link = os.getenv("SKILLSPACE_PUBLIC_COURSE_URL", "https://855f92.skillspace.ru/course/102877").strip()
                text = (
                    "🆘 Инструкция, если не пришло письмо или не получается зайти\n\n"
                    "1) Переходим на skillspace.ru\n"
                    "   🇺🇦 Если вы проживаете в Украине — вам может понадобиться Brave или VPN.\n"
                    "   🌍 Если вы не проживаете в Украине — VPN не нужен.\n\n"
                    f"2) Заходим по ссылке: {course_link}\n"
                    "   Нажимаем «Забыли пароль» / «Проблемы со входом».\n"
                    "   Указываем ту же почту, что вводили в боте.\n"
                    "   Устанавливаем новый пароль — и всё готово ✅"
                )

            await cb.message.answer(text)

    async def start_polling(self) -> None:
        await self.bot.delete_webhook(drop_pending_updates=True)
        await self.dp.start_polling(self.bot)

    async def stop(self) -> None:
        await self.bot.session.close()

    async def send_message(self, telegram_id: int, text: str) -> None:
        await self.bot.send_message(chat_id=telegram_id, text=text)
