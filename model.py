from typing import Optional
import datetime
import uuid

from sqlalchemy import DateTime, Double, ForeignKeyConstraint, Integer, PrimaryKeyConstraint, String, UniqueConstraint, Uuid, text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

class Base(DeclarativeBase):
    pass


class Credit(Base):
    __tablename__ = 'credit'
    __table_args__ = (
        PrimaryKeyConstraint('id', name='credit_pkey'),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, server_default=text('uuid_generate_v4()'))
    type: Mapped[str] = mapped_column(String(20), nullable=False)
    initial_payment: Mapped[int] = mapped_column(Integer, nullable=False)
    procent: Mapped[Optional[int]] = mapped_column(Integer)


class Flats(Base):
    __tablename__ = 'flats'
    __table_args__ = (
        PrimaryKeyConstraint('id', name='flats_pkey'),
        UniqueConstraint('number', 'type', name='unique_number_type')
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, server_default=text('uuid_generate_v4()'))
    number: Mapped[int] = mapped_column(Integer, nullable=False)
    block: Mapped[str] = mapped_column(String(255), nullable=False)
    sq_m: Mapped[float] = mapped_column(Double(53), nullable=False)
    stage: Mapped[int] = mapped_column(Integer, nullable=False)
    price: Mapped[int] = mapped_column(Integer, nullable=False)
    rooms: Mapped[int] = mapped_column(Integer, nullable=False)
    lobby: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(255), nullable=False, server_default=text("'free'::character varying"))
    plan: Mapped[str] = mapped_column(String(500), nullable=False)
    type: Mapped[str] = mapped_column(String(100), nullable=False, server_default=text("'Љў\xa0авЁа\xa0'::character varying"))
    description: Mapped[Optional[str]] = mapped_column(String(2000))


class Residence(Base):
    __tablename__ = 'residence'
    __table_args__ = (
        PrimaryKeyConstraint('id', name='residence_pkey'),
        UniqueConstraint('name', name='residence_name_key')
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, server_default=text('uuid_generate_v4()'))
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    district: Mapped[str] = mapped_column(String(511), nullable=False)
    street: Mapped[str] = mapped_column(String(255), nullable=False)

    block: Mapped[list['Block']] = relationship('Block', back_populates='residence_')


class Users(Base):
    __tablename__ = 'users'
    __table_args__ = (
        PrimaryKeyConstraint('id', name='users_pkey'),
        UniqueConstraint('number', name='users_number_key')
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, server_default=text('uuid_generate_v4()'))
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    number: Mapped[str] = mapped_column(String(50), nullable=False)
    tg_id: Mapped[int] = mapped_column(Integer, nullable=False)
    logedat: Mapped[Optional[datetime.datetime]] = mapped_column(DateTime(True), server_default=text('now()'))


class Block(Base):
    __tablename__ = 'block'
    __table_args__ = (
        ForeignKeyConstraint(['residence'], ['residence.id'], ondelete='CASCADE', name='block_residence_fkey'),
        PrimaryKeyConstraint('id', name='block_pkey'),
        UniqueConstraint('name', name='block_name_key')
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, server_default=text('uuid_generate_v4()'))
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    residence: Mapped[Optional[uuid.UUID]] = mapped_column(Uuid)

    residence_: Mapped[Optional['Residence']] = relationship('Residence', back_populates='block')
    lobby: Mapped[list['Lobby']] = relationship('Lobby', back_populates='block_')


class Lobby(Base):
    __tablename__ = 'lobby'
    __table_args__ = (
        ForeignKeyConstraint(['block'], ['block.id'], ondelete='CASCADE', name='lobby_block_fkey'),
        PrimaryKeyConstraint('id', name='your_table_pkey')
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, server_default=text('uuid_generate_v4()'))
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    block: Mapped[Optional[uuid.UUID]] = mapped_column(Uuid)

    block_: Mapped[Optional['Block']] = relationship('Block', back_populates='lobby')
