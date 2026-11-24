from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class BusinessError(Exception):
    """QA-side mirror of ``app.schemas.BusinessError``."""

    def __init__(
        self,
        code: str,
        message: str,
        status_code: int = 400,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code
        self.details = details or {}


class ErrorResponse(BaseModel):
    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "detail": {
                        "code": "INSUFFICIENT_FUNDS",
                        "message": "Not enough free balance to reserve the order.",
                        "details": {
                            "required": "1000.00000000",
                            "available": "100.00000000",
                        },
                    }
                },
                {"detail": "Could not validate credentials"},
            ]
        }
    )

    detail: Any = Field(
        description="Either a plain message (4xx auth errors) or a structured business error."
    )
