import asyncio
import sys
import uuid
from asyncio.events import AbstractEventLoop
from typing import Any, AsyncGenerator, Generator
from unittest.mock import Mock

import pytest
from aio_pika import Channel
from aio_pika.abc import AbstractExchange, AbstractQueue
from aio_pika.pool import Pool
from fakeredis import FakeServer
from fakeredis.aioredis import FakeConnection
from fastapi import FastAPI
from httpx import AsyncClient
from psycopg import AsyncConnection
from psycopg_pool import AsyncConnectionPool
from redis.asyncio import ConnectionPool

from cloud_api.db.dependencies import get_db_pool
from cloud_api.services.rabbit.dependencies import get_rmq_channel_pool
from cloud_api.services.rabbit.lifetime import init_rabbit, shutdown_rabbit
from cloud_api.services.redis.dependency import get_redis_pool
from cloud_api.settings import settings
from cloud_api.web.application import get_app


@pytest.fixture(scope="session")
def anyio_backend() -> str:
    """
    Backend for anyio pytest plugin.

    :return: backend name.
    """
    return "asyncio"


async def drop_db() -> None:
    """Drops database after tests."""
    pool = AsyncConnectionPool(conninfo=str(settings.db_url.with_path("/postgres")))
    await pool.wait()
    async with pool.connection() as conn:
        await conn.set_autocommit(True)
        await conn.execute(
            "SELECT pg_terminate_backend(pg_stat_activity.pid) "  # noqa: S608
            "FROM pg_stat_activity "
            "WHERE pg_stat_activity.datname = %(dbname)s "
            "AND pid <> pg_backend_pid();",
            params={
                "dbname": settings.db_base,
            },
        )
        await conn.execute(
            f"DROP DATABASE {settings.db_base}",
        )
    await pool.close()


async def create_db() -> None:  # noqa: WPS217
    """Creates database for tests."""
    pool = AsyncConnectionPool(conninfo=str(settings.db_url.with_path("/postgres")))
    await pool.wait()
    async with pool.connection() as conn_check:
        res = await conn_check.execute(
            "SELECT 1 FROM pg_database WHERE datname=%(dbname)s",
            params={
                "dbname": settings.db_base,
            },
        )
        db_exists = False
        row = await res.fetchone()
        if row is not None:
            db_exists = row[0]

    if db_exists:
        await drop_db()

    async with pool.connection() as conn_create:
        await conn_create.set_autocommit(True)
        await conn_create.execute(
            f"CREATE DATABASE {settings.db_base};",
        )
    await pool.close()


async def create_tables(connection: AsyncConnection[Any]) -> None:
    """
    Create tables for your database.

    Since psycopg doesn't have migration tool,
    you must create your tables for tests.

    :param connection: connection to database.
    """
    await connection.execute(
        "CREATE TABLE dummy (" "id SERIAL primary key," "name VARCHAR(200)" ");",
    )
    pass  # noqa: WPS420


@pytest.fixture
async def dbpool() -> AsyncGenerator[AsyncConnectionPool, None]:
    """
    Creates database connections pool to test database.

    This connection must be used in tests and for application.

    :yield: database connections pool.
    """
    await create_db()
    pool = AsyncConnectionPool(conninfo=str(settings.db_url))
    await pool.wait()

    async with pool.connection() as create_conn:
        await create_tables(create_conn)

    try:
        yield pool
    finally:
        await pool.close()
        await drop_db()


@pytest.fixture
async def test_rmq_pool() -> AsyncGenerator[Channel, None]:
    """
    Create rabbitMQ pool.

    :yield: channel pool.
    """
    app_mock = Mock()
    init_rabbit(app_mock)
    yield app_mock.state.rmq_channel_pool
    await shutdown_rabbit(app_mock)


@pytest.fixture
async def test_exchange_name() -> str:
    """
    Name of an exchange to use in tests.

    :return: name of an exchange.
    """
    return uuid.uuid4().hex


@pytest.fixture
async def test_routing_key() -> str:
    """
    Name of routing key to use whild binding test queue.

    :return: key string.
    """
    return uuid.uuid4().hex


@pytest.fixture
async def test_exchange(
    test_exchange_name: str,
    test_rmq_pool: Pool[Channel],
) -> AsyncGenerator[AbstractExchange, None]:
    """
    Creates test exchange.

    :param test_exchange_name: name of an exchange to create.
    :param test_rmq_pool: channel pool for rabbitmq.
    :yield: created exchange.
    """
    async with test_rmq_pool.acquire() as conn:
        exchange = await conn.declare_exchange(
            name=test_exchange_name,
            auto_delete=True,
        )
        yield exchange

        await exchange.delete(if_unused=False)


@pytest.fixture
async def test_queue(
    test_exchange: AbstractExchange,
    test_rmq_pool: Pool[Channel],
    test_routing_key: str,
) -> AsyncGenerator[AbstractQueue, None]:
    """
    Creates queue connected to exchange.

    :param test_exchange: exchange to bind queue to.
    :param test_rmq_pool: channel pool for rabbitmq.
    :param test_routing_key: routing key to use while binding.
    :yield: queue binded to test exchange.
    """
    async with test_rmq_pool.acquire() as conn:
        queue = await conn.declare_queue(name=uuid.uuid4().hex)
        await queue.bind(
            exchange=test_exchange,
            routing_key=test_routing_key,
        )
        yield queue

        await queue.delete(if_unused=False, if_empty=False)


@pytest.fixture
async def fake_redis_pool() -> AsyncGenerator[ConnectionPool, None]:
    """
    Get instance of a fake redis.

    :yield: FakeRedis instance.
    """
    server = FakeServer()
    server.connected = True
    pool = ConnectionPool(connection_class=FakeConnection, server=server)

    yield pool

    await pool.disconnect()


@pytest.fixture
def fastapi_app(
    dbpool: AsyncConnectionPool,
    fake_redis_pool: ConnectionPool,
    test_rmq_pool: Pool[Channel],
) -> FastAPI:
    """
    Fixture for creating FastAPI app.

    :return: fastapi app with mocked dependencies.
    """
    application = get_app()
    application.dependency_overrides[get_db_pool] = lambda: dbpool
    application.dependency_overrides[get_redis_pool] = lambda: fake_redis_pool
    application.dependency_overrides[get_rmq_channel_pool] = lambda: test_rmq_pool
    return application  # noqa: WPS331


@pytest.fixture
async def client(
    fastapi_app: FastAPI,
    anyio_backend: Any,
) -> AsyncGenerator[AsyncClient, None]:
    """
    Fixture that creates client for requesting server.

    :param fastapi_app: the application.
    :yield: client for the app.
    """
    async with AsyncClient(app=fastapi_app, base_url="http://test") as ac:
        yield ac
