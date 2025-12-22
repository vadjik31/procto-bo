from dataclasses import dataclass
from typing import Awaitable, Callable

from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import Message


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
                "Привет! Я помогу записаться на курс и зафиксировать результат теста.\n\n"
                "Для начала — пару вопросов. Это займёт меньше минуты.\n\n"
                "1/7 — Напиши email (именно тот, который будешь использовать в Skillspace):"
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

            reply = await self.on_lead_completed(profile)
            await m.answer(reply)
            await state.clear()

    async def start_polling(self) -> None:
        # На случай если раньше ставили webhook — чтобы polling не конфликтовал.
        await self.bot.delete_webhook(drop_pending_updates=True)
        await self.dp.start_polling(self.bot)

    async def stop(self) -> None:
        await self.bot.session.close()

    async def send_message(self, telegram_id: int, text: str) -> None:
        await self.bot.send_message(chat_id=telegram_id, text=text)
