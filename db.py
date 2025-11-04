import logging
import os
import random
import string
import time
import uuid
from datetime import datetime
from enum import Enum as PyEnum
from typing import Optional

from dotenv import load_dotenv
from sqlalchemy import (
    Boolean,
    DateTime,
    Enum as SAEnum,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    create_engine,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship, sessionmaker
from sqlalchemy.sql import func

load_dotenv()

logger = logging.getLogger(__name__)

DB_URL = os.getenv("DB_URL") or os.getenv("DB_URL2")
if not DB_URL:
    raise ValueError("DB_URL (or DB_URL2) is required for database connection")

SQL_ECHO = os.getenv("SQL_ECHO", "false").lower() == "true"
engine = create_engine(DB_URL, echo=SQL_ECHO)

def _generate_uuid_str() -> str:
    return str(uuid.uuid4())


def _generate_cuid() -> str:
    """Lightweight cuid-like generator; good enough for string PK defaults."""
    timestamp = int(time.time() * 1000)
    rand_part = "".join(random.choices(string.ascii_lowercase + string.digits, k=12))
    return f"c{timestamp:x}{rand_part}"


class Base(DeclarativeBase):
    pass


class ProductStatus(PyEnum):
    IN_STOCK = "IN_STOCK"
    OUT_OF_STOCK = "OUT_OF_STOCK"
    PREORDER = "PREORDER"


class ProductTag(PyEnum):
    NONE = "NONE"
    SALE = "SALE"
    NEW = "NEW"


class Category(Base):
    __tablename__ = "categories"
    __table_args__ = (
        UniqueConstraint("name", name="categories_name_key"),
        UniqueConstraint("slug", name="categories_slug_key"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    slug: Mapped[str] = mapped_column(String(255), nullable=False)

    products: Mapped[list["Product"]] = relationship(
        "Product",
        back_populates="category_obj",
        cascade="all, delete-orphan",
    )


class Product(Base):
    __tablename__ = "products"
    __table_args__ = (UniqueConstraint("name", name="products_name_key"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    price: Mapped[float] = mapped_column(Float, nullable=False)
    old_price: Mapped[Optional[float]] = mapped_column(Float)
    description: Mapped[Optional[str]] = mapped_column(Text)
    picture: Mapped[list[str]] = mapped_column(ARRAY(String(2048)), default=list, server_default=text("'{}'::text[]"))
    cover: Mapped[Optional[str]] = mapped_column(String(2048))
    tags: Mapped[Optional[str]] = mapped_column(String(255))
    tag: Mapped[ProductTag] = mapped_column(
        SAEnum(ProductTag, name="product_tag"),
        nullable=False,
        default=ProductTag.NONE,
        server_default=ProductTag.NONE.value,
    )
    status: Mapped[ProductStatus] = mapped_column(
        SAEnum(ProductStatus, name="product_status"),
        nullable=False,
        default=ProductStatus.IN_STOCK,
        server_default=ProductStatus.IN_STOCK.value,
    )
    category_id: Mapped[Optional[int]] = mapped_column(
        "categoryId",
        Integer,
        ForeignKey("categories.id", ondelete="SET NULL"),
    )

    category_obj: Mapped[Optional["Category"]] = relationship("Category", back_populates="products")
    variations: Mapped[list["ProductVariation"]] = relationship(
        "ProductVariation",
        back_populates="product",
        cascade="all, delete-orphan",
    )

    @property
    def category(self) -> Optional[str]:
        if self.category_obj:
            return self.category_obj.name
        return None


class User(Base):
    __tablename__ = "users"
    __table_args__ = (UniqueConstraint("email", name="users_email_key"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_generate_uuid_str, server_default=text("uuid_generate_v4()"))
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    email: Mapped[str] = mapped_column(String(255), nullable=False)
    picture: Mapped[Optional[str]] = mapped_column(String(2048))
    admin: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default=text("false"))
    created_at: Mapped[datetime] = mapped_column(
        "createdAt",
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )


class Credit(Base):
    __tablename__ = "credit"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_generate_uuid_str, server_default=text("uuid_generate_v4()"))
    type: Mapped[str] = mapped_column(String(50), nullable=False)
    initial_payment: Mapped[int] = mapped_column(Integer, nullable=False)
    procent: Mapped[Optional[int]] = mapped_column(Integer)


class ProductVariation(Base):
    __tablename__ = "product_variations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    product_id: Mapped[int] = mapped_column(
        "productId",
        Integer,
        ForeignKey("products.id", ondelete="CASCADE"),
        nullable=False,
    )
    option: Mapped[str] = mapped_column(String(255), nullable=False)
    value: Mapped[str] = mapped_column(String(255), nullable=False)
    price: Mapped[Optional[float]] = mapped_column(Float)
    old_price: Mapped[Optional[float]] = mapped_column(Float)
    stock: Mapped[Optional[int]] = mapped_column(Integer)
    picture: Mapped[Optional[str]] = mapped_column(String(2048))
    hex: Mapped[Optional[str]] = mapped_column(String(64))
    sort_order: Mapped[int] = mapped_column("sortOrder", Integer, nullable=False, default=0, server_default=text("0"))
    created_at: Mapped[datetime] = mapped_column("createdAt", DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column("updatedAt", DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now())

    product: Mapped["Product"] = relationship("Product", back_populates="variations")


class CartOrder(Base):
    __tablename__ = "cart_orders"
    __table_args__ = (UniqueConstraint("invoice", name="cart_orders_invoice_key"),)

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_generate_cuid)
    invoice: Mapped[str] = mapped_column(String(255), nullable=False)
    phone: Mapped[str] = mapped_column(String(50), nullable=False)
    seller: Mapped[str] = mapped_column(String(255), nullable=False, default="LuxeBeauty", server_default=text("'LuxeBeauty'::text"))
    total: Mapped[float] = mapped_column(Float, nullable=False)
    discount: Mapped[Optional[float]] = mapped_column(Float)
    created_at: Mapped[datetime] = mapped_column(
        "createdAt",
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )

    items: Mapped[list["CartItem"]] = relationship(
        "CartItem",
        back_populates="order",
        cascade="all, delete-orphan",
    )


class CartItem(Base):
    __tablename__ = "cart_items"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    order_id: Mapped[str] = mapped_column(
        "orderId",
        String(32),
        ForeignKey("cart_orders.id", ondelete="CASCADE"),
        nullable=False,
    )
    product_id: Mapped[Optional[int]] = mapped_column(
        "productId",
        Integer,
        ForeignKey("products.id", ondelete="SET NULL"),
    )
    variation_ids: Mapped[Optional[str]] = mapped_column("variationIds", Text)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    variant_label: Mapped[Optional[str]] = mapped_column("variantLabel", String(255))
    price: Mapped[float] = mapped_column(Float, nullable=False)
    old_price: Mapped[Optional[float]] = mapped_column(Float)
    quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    cover: Mapped[Optional[str]] = mapped_column(String(2048))

    order: Mapped["CartOrder"] = relationship("CartOrder", back_populates="items")
    product: Mapped[Optional["Product"]] = relationship("Product")


class Location(Base):
    __tablename__ = "locations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    address: Mapped[str] = mapped_column(Text, nullable=False)
    latitude: Mapped[Optional[float]] = mapped_column(Float)
    longitude: Mapped[Optional[float]] = mapped_column(Float)
    map_url: Mapped[Optional[str]] = mapped_column("mapUrl", String(2048))
    is_primary: Mapped[bool] = mapped_column("isPrimary", Boolean, nullable=False, default=False, server_default=text("false"))
    created_at: Mapped[datetime] = mapped_column("createdAt", DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column("updatedAt", DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now())


class Service(Base):
    __tablename__ = "services"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    icon: Mapped[Optional[str]] = mapped_column(String(255))
    sort_order: Mapped[int] = mapped_column("sortOrder", Integer, nullable=False, default=0, server_default=text("0"))
    created_at: Mapped[datetime] = mapped_column("createdAt", DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column("updatedAt", DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now())


Base.metadata.create_all(engine)

Session = sessionmaker(bind=engine)
