import time
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from src.constants import Role

from .common import ORMModel
from .types import UtcTimestampStr


class LoginResponse(BaseModel):
    access_token: str = Field(
        description="JWT (HS256) to send as `Authorization: Bearer <token>`."
    )
    token_type: Literal["bearer"] = "bearer"
    expires_in: int = Field(description="Token lifetime in seconds.")
    user_id: int
    role: Role

    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "access_token": "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9...",
                    "token_type": "bearer",
                    "expires_in": 3600,
                    "user_id": 1,
                    "role": "MASTER",
                }
            ]
        }
    )


class JwtPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    sub: str = Field(..., min_length=1)
    username: str = Field(..., min_length=1)
    role: str
    iat: int
    exp: int

    @model_validator(mode="after")
    def validate_timestamps(self) -> "JwtPayload":
        now = int(time.time())
        if self.exp <= now:
            raise ValueError("Токен уже истёк")
        if self.exp <= self.iat:
            raise ValueError("exp должен быть больше iat")
        if self.iat > now + 5:
            raise ValueError("iat не может быть в будущем")
        return self


class UserResponse(ORMModel):
    id: int
    username: str
    role: str
    created_at: UtcTimestampStr
