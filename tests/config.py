from typing import ClassVar

from pydantic_settings import BaseSettings, SettingsConfigDict


class TestSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="QA_",
        env_file=".env.test",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    server_host: str = "127.0.0.1"
    server_port: int = 8123
    server_start_timeout: float = 20.0
    server_stop_timeout: float = 5.0

    test_db: str = "tests_trading.db"

    jwt_secret: str = "qa-only-jwt-secret-with-at-least-32-bytes"
    jwt_algorithm: str = "HS256"
    password: str = "StrongPass123!"

    master_deposit: str = "20000.00000000"
    slave_deposit: str = "5000.00000000"

    copy_delay_ms: int = 1200
    tick_interval_ms: int = 500
    market_regime: str = "high"

    request_timeout: float = 10.0
    poll_timeout: float = 12.0
    poll_interval: float = 0.15
    ws_timeout: float = 6.0

    default_allocation: str = "1000.00000000"
    default_slippage: str = "100.00000000"

    btc_amount: str = "0.10000000"
    eth_amount: str = "0.50000000"

    MONEY_QUANTUM: ClassVar[str] = "0.00000001"
    ZERO: ClassVar[str] = "0.00000000"

    @property
    def base_url(self) -> str:
        return f"http://{self.server_host}:{self.server_port}"

    @property
    def ws_url(self) -> str:
        return f"ws://{self.server_host}:{self.server_port}"


settings = TestSettings()
