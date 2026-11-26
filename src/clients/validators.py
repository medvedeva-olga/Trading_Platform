from typing import Any

import httpx
from pydantic import BaseModel


def expect_status(response: httpx.Response, expected: int) -> None:
    assert response.status_code == expected, (
        f"Ожидался статус {expected}, получен {response.status_code}. "
        f"{response.request.method} {response.request.url} -> {response.text[:500]}"
    )


def expect_model[M: BaseModel](
    response: httpx.Response, model: type[M], expected: int | None = None
) -> M:
    if expected is not None:
        expect_status(response, expected)
    content_type = response.headers.get("content-type", "")
    assert (
        "application/json" in content_type
    ), f"Ожидался application/json, получен Content-Type={content_type}"
    return model.model_validate(response.json())


def expect_model_list[M: BaseModel](
    response: httpx.Response, model: type[M]
) -> list[M]:
    assert (
        response.is_success
    ), f"Ожидался успешный статус, получен {response.status_code}: {response.text}"

    content_type = response.headers.get("content-type", "")
    assert (
        "application/json" in content_type
    ), f"Ожидался JSON, получен Content-Type={content_type}"
    payload = response.json()
    assert isinstance(
        payload, list
    ), f"Ожидался JSON-массив, получен {type(payload).__name__}"
    return [model.model_validate(item) for item in payload]


def expect_event[M: BaseModel](payload: dict[str, Any], model: type[M]) -> M:
    return model.model_validate(payload)


def expect_custom_error(
    response: httpx.Response,
    code: str,
    message: str | None = None,
    status: int = 400,
) -> dict[str, Any]:
    expect_status(response, status)
    body = response.json()
    assert isinstance(
        body, dict
    ), f"Ожидался JSON-объект, получен {type(body).__name__}"
    detail = body["detail"]
    assert isinstance(
        detail, dict
    ), f"Ожидалась структурированная ошибка, получено {detail!r}"
    assert (
        detail["code"] == code
    ), f"Ожидался код ошибки {code}, получен {detail['code']!r}: {body}"
    if message is not None:
        assert (
            detail["message"] == message
        ), f"Ожидалось сообщение об ошибке {message!r}, получено {detail['message']!r}: {body}"
    return detail


def expect_error(response: httpx.Response, expected: str, status: int = 400) -> None:
    expect_status(response, status)
    body = response.json()
    assert isinstance(
        body, dict
    ), f"Ожидался JSON-объект, получен {type(body).__name__}"
    detail = body.get("detail")
    if isinstance(detail, dict):
        assert expected in (
            detail.get("code"),
            detail.get("message"),
        ), f"Ожидались код/сообщение {expected!r}, получено {detail!r}: {body}"
    else:
        assert (
            detail == expected
        ), f"Ожидалось сообщение об ошибке {expected!r}, получено {detail!r}: {body}"


def expect_validation_error(response, msg, status = 400):
    expect_status(response, status)
    body = response.json()
    assert isinstance(
        body, dict
    ), f"Ожидался JSON-объект, получен {type(body).__name__}"
    detail = body.get("detail")
    assert detail is not None, "Ожидалось сообщение {msg!r}, получено {}"
    assert isinstance(detail, list) and len(detail) > 0 , "Ожидался непустой список"
    assert detail[0].get("msg") == msg, f"Ожидалось сообщение {msg!r}, получено {detail!r}: {body}"
