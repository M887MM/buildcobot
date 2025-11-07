#!/usr/bin/env python3
"""
Lightweight smoke-test harness for search_products.

The script stubs external dependencies (aiogram, dotenv, openai, sqlalchemy, db)
so it can run without the full production stack. It feeds a synthetic catalog to
openai_func.search_products and ensures queries for популярные зональные запросы
возвращают хотя бы один товар.
"""

from __future__ import annotations

import sys
import types
from dataclasses import dataclass
from typing import Iterable
from pathlib import Path
from unittest.mock import patch


ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))


def _ensure_stub_modules() -> None:
    # aiogram (only Bot and types.InputFile are needed)
    if "aiogram" not in sys.modules:
        aiogram_mod = types.ModuleType("aiogram")
        aiogram_types = types.SimpleNamespace(InputFile=object, ChatActions=types.SimpleNamespace(TYPING="typing"))
        aiogram_mod.Bot = object
        aiogram_mod.types = aiogram_types
        sys.modules["aiogram"] = aiogram_mod
        sys.modules["aiogram.types"] = types.ModuleType("aiogram.types")
        sys.modules["aiogram.types"].InputFile = object
        sys.modules["aiogram.types"].ChatActions = aiogram_types.ChatActions

    # dotenv
    if "dotenv" not in sys.modules:
        dotenv_mod = types.ModuleType("dotenv")

        def load_dotenv(*_args, **_kwargs):
            return None

        dotenv_mod.load_dotenv = load_dotenv
        sys.modules["dotenv"] = dotenv_mod

    # openai
    if "openai" not in sys.modules:
        openai_mod = types.ModuleType("openai")

        class OpenAI:
            def __init__(self, *_, **__):
                pass

            class chat:
                class completions:
                    @staticmethod
                    def create(**_kwargs):
                        raise RuntimeError("OpenAI API is not available in smoke tests.")

        openai_mod.OpenAI = OpenAI
        sys.modules["openai"] = openai_mod

    # anthropic
    if "anthropic" not in sys.modules:
        anthropic_mod = types.ModuleType("anthropic")

        class Anthropic:
            def __init__(self, *_, **__):
                raise RuntimeError("Anthropic client not available in smoke tests.")

        anthropic_mod.Anthropic = Anthropic
        sys.modules["anthropic"] = anthropic_mod

    # sqlalchemy.orm.selectinload
    if "sqlalchemy.orm" not in sys.modules:
        orm_mod = types.ModuleType("sqlalchemy.orm")

        def selectinload(*_args, **_kwargs):
            return None

        orm_mod.selectinload = selectinload
        sys.modules["sqlalchemy.orm"] = orm_mod
        sqlalchemy_mod = types.ModuleType("sqlalchemy")
        sqlalchemy_mod.orm = orm_mod
        sys.modules["sqlalchemy"] = sqlalchemy_mod

    # db Session/Product stub
    if "db" not in sys.modules:
        db_mod = types.ModuleType("db")

        class _Session:
            def __call__(self):
                return self

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            # SQLAlchemy-like chaining stubs
            def query(self, *_args, **_kwargs):
                return self

            def options(self, *_args, **_kwargs):
                return self

            def order_by(self, *_args, **_kwargs):
                return self

            def all(self):
                return []

        db_mod.Session = _Session()

        @dataclass
        class Product:  # pragma: no cover - structure only
            id: int
            name: str
            category: str | None = None
            description: str | None = None
            tags: str | None = None
            price: float | None = None
            picture: Iterable[str] | None = None

        db_mod.Product = Product
        sys.modules["db"] = db_mod


_ensure_stub_modules()

import openai_func  # noqa: E402  (import after stubbing)


def _build_demo_catalog():
    from types import SimpleNamespace

    return [
        SimpleNamespace(
            id=1,
            name="Увлажняющий крем для лица HydraSoft",
            category="Уход за лицом",
            description="Комфортная текстура с гиалуроновой кислотой",
            tags="лицо увлажнение dry sensitive",
            price=249000,
            picture=["https://example.com/face.jpg"],
        ),
        SimpleNamespace(
            id=2,
            name="Бальзам для губ Glow Care",
            category="Макияж губ",
            description="Питательный бальзам с витамином E",
            tags="губы уход lip balm",
            price=99000,
            picture=["https://example.com/lips.jpg"],
        ),
        SimpleNamespace(
            id=3,
            name="Парфюмированная вода Blooming Rouge",
            category="Парфюмерия",
            description="Цветочно-фруктовый аромат с нотами пиона",
            tags="аромат подарок",
            price=399000,
            picture=["https://example.com/fragrance.jpg"],
        ),
    ]


def run_smoke() -> None:
    catalog = _build_demo_catalog()

    # Подменяем загрузку каталога, чтобы исключить работу с БД.
    openai_func.invalidate_products_cache()
    openai_func.load_active_products = lambda force=False: catalog  # type: ignore[assignment]

    test_cases = {
        "что у тебя есть для лица": "лицо",
        "что у тебя есть для губ": "губ",
        "подбор подарка": None,
    }

    errors: list[str] = []
    for query, expected_fragment in test_cases.items():
        payload, matches, *_ = openai_func.search_products(query, "ru")
        if not payload or not matches:
            errors.append(f"Пустой результат для запроса «{query}»")
            continue
        if expected_fragment:
            combined_text = " ".join(entry["text"].lower() for entry in payload if entry.get("text"))
            if expected_fragment not in combined_text:
                errors.append(
                    f"Результат для «{query}» не содержит ожидаемый фрагмент «{expected_fragment}».\n{combined_text}"
                )

    # Проверяем отсутствие категорий: очищаем каталог от facial товаров.
    no_face_catalog = [
        item
        for item in catalog
        if "лицо" not in (item.tags or "") and "face" not in (item.tags or "")
    ]
    openai_func.invalidate_products_cache()
    openai_func.load_active_products = lambda force=False: no_face_catalog  # type: ignore[assignment]

    payload, matches, _, _, categories = openai_func.search_products("что у тебя есть для лица", "ru")
    if payload or matches or not categories or "лицо" not in categories:
        errors.append(
            "Ожидалась пустая выдача для запроса «что у тебя есть для лица» без товаров для лица."
        )

    if errors:
        raise SystemExit("\n".join(errors))

    print("search_products smoke-test passed.")


def response_safety_smoke() -> None:
    """Проверяет, что ответы LLM очищаются от цен и не становятся пустыми."""
    fallback_ru = openai_func.PRICE_FALLBACK_LINES["ru"]

    with patch.object(openai_func, "call_chat_with_fallback", return_value=("Цена 1000 ₽", "gpt")):
        health_text = openai_func._build_health_response("у меня сыпь и зуд", "ru")
        if not health_text or "цена" in health_text.lower():
            raise SystemExit("Health-ответ содержит цену или пуст.")
        if fallback_ru not in health_text:
            raise SystemExit("Health-ответ не добавил fallback без цены.")

    with patch.object(openai_func, "call_chat_with_fallback", return_value=("Стоимость 5000 рублей", "gpt")):
        with patch.object(openai_func, "get_material_reference", return_value=None):
            info_text = openai_func._build_informational_answer("что такое ретинол", "ru")
            if not info_text or "стоим" in info_text.lower():
                raise SystemExit("Информационный ответ содержит цену или пуст.")
            if fallback_ru not in info_text:
                raise SystemExit("Информационный ответ не добавил fallback без цены.")

    print("response sanitization smoke-test passed.")


if __name__ == "__main__":
    run_smoke()
    response_safety_smoke()
