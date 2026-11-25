from http import HTTPStatus

import jwt
import pytest

from src.clients.trading_api import TradingAPIClient
from src.clients.validators import (
    expect_custom_error,
    expect_error,
    expect_model,
    expect_status,
)
from src.constants import Role
from src.fixtures import register_payload, unique_username
from src.messages import AuthErrors, ErrCodes
from src.schemas.auth import JwtPayload, LoginResponse, UserResponse
from tests.config import settings as qa


@pytest.mark.smoke
@pytest.mark.parametrize("role", [Role.MASTER, Role.SLAVE])
async def test_register_new_returns_201_with_user_schema(role, api):
    username = unique_username(role.lower())
    payload = register_payload(username, qa.password, role)

    response = await api.register(**payload)

    expect_status(response, HTTPStatus.CREATED)
    user = expect_model(response, UserResponse)
    assert user.id > 0
    assert user.username == username
    assert user.role == role
    assert user.created_at is not None


async def test_register_duplicate_username_returns_400_with_error_code(api, master):
    payload = register_payload(master.username, qa.password, master.role)

    response = await api.register(**payload)

    expect_custom_error(
        response,
        ErrCodes.USERNAME_ALREADY_EXISTS,
        AuthErrors.USERNAME_ALREADY_EXISTS,
        status=HTTPStatus.BAD_REQUEST,
    )


@pytest.mark.smoke
async def test_login_with_valid_credentials_returns_jwt(api, user_factory):
    role = Role.SLAVE
    user = await user_factory(role)

    response = await api.login(user.username, qa.password)

    expect_status(response, 200)
    login_data = expect_model(response, LoginResponse)

    assert login_data.token_type == "bearer"
    assert login_data.expires_in > 0
    assert login_data.user_id == user.id
    assert login_data.role == role

    jwt_payload = jwt.decode(
        login_data.access_token,
        qa.jwt_secret,
        algorithms=qa.jwt_algorithm,
        options={"require": ["sub", "username", "role", "iat", "exp"]},
    )
    token_payload = JwtPayload.model_validate(jwt_payload)

    assert token_payload.sub == str(login_data.user_id)
    assert token_payload.username == user.username
    assert token_payload.role == login_data.role
    assert (token_payload.exp - token_payload.iat) == login_data.expires_in


async def test_login_with_wrong_password_returns_401(api, master):
    wrong_password = qa.password + "_wrong"

    response = await api.login(master.username, wrong_password)

    expect_error(response, AuthErrors.INVALID_CREDENTIALS, HTTPStatus.UNAUTHORIZED)


async def test_request_without_token_returns_401(api):
    response = await api.portfolio_summary()

    expect_error(response, AuthErrors.UNAUTHORIZED, HTTPStatus.UNAUTHORIZED)


async def test_request_with_invalid_token_returns_401(runtime, server):
    async with TradingAPIClient(runtime.base_url, token="not-a-jwt") as client:
        response = await client.portfolio_summary()
        expect_error(response, AuthErrors.UNAUTHORIZED, HTTPStatus.UNAUTHORIZED)
