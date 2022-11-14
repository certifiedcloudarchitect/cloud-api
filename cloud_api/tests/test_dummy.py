import uuid
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import AsyncClient
from psycopg.connection_async import AsyncConnection
from psycopg_pool import AsyncConnectionPool
from starlette import status

from cloud_api.db.dao.dummy_dao import DummyDAO
from cloud_api.db.models.dummy_model import DummyModel


@pytest.mark.anyio
async def test_creation(
    fastapi_app: FastAPI,
    client: AsyncClient,
    dbpool: AsyncConnectionPool,
) -> None:
    """Tests dummy instance creation."""
    url = fastapi_app.url_path_for("create_dummy_model")
    test_name = uuid.uuid4().hex
    response = await client.put(
        url,
        json={
            "name": test_name,
        },
    )
    assert response.status_code == status.HTTP_200_OK
    dao = DummyDAO(dbpool)
    instances = await dao.filter(name=test_name)
    assert instances[0].name == test_name


@pytest.mark.anyio
async def test_getting(
    fastapi_app: FastAPI,
    client: AsyncClient,
    dbpool: AsyncConnectionPool,
) -> None:
    """Tests dummy instance retrieval."""
    dao = DummyDAO(dbpool)
    test_name = uuid.uuid4().hex
    await dao.create_dummy_model(name=test_name)
    url = fastapi_app.url_path_for("get_dummy_models")
    response = await client.get(url)
    dummies = response.json()

    assert response.status_code == status.HTTP_200_OK
    assert len(dummies) == 1
    assert dummies[0]["name"] == test_name
