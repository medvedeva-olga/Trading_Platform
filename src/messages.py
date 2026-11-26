class AuthErrors:
    INVALID_CREDENTIALS = "Invalid username or password"
    USERNAME_ALREADY_EXISTS = (
        "That username is already registered; use a unique fixture name."
    )
    UNAUTHORIZED = "Could not validate credentials"


class CopyTradingErrors:
    SLAVE_ROLE_REQUIRED = "SLAVE role required"
    SUBSCRIPTION_ALREADY_EXISTS = (
        "An active subscription to this master already exists."
    )
    SUBSCRIPTION_NOT_ACTIVE = "Subscription is not active."
    MASTER_ID_IMMUTABLE = (
        "master_id is immutable for an existing subscription. "
        "Use POST /api/v1/copy-trading/subscribe to create a new subscription "
        "for another master."
    )
    INSUFFICIENT_FUNDS = "Allocation exceeds the slave's total balance."


class ErrCodes:
    USERNAME_ALREADY_EXISTS = "USERNAME_ALREADY_EXISTS"
    SUBSCRIPTION_ALREADY_EXISTS = "SUBSCRIPTION_ALREADY_EXISTS"
    SUBSCRIPTION_NOT_ACTIVE = "SUBSCRIPTION_NOT_ACTIVE"
    MASTER_ID_IMMUTABLE = "MASTER_ID_IMMUTABLE"
    INSUFFICIENT_FUNDS = "INSUFFICIENT_FUNDS"

class OrderError:
    MIN_PRICE_GR_MAX = "Value error, min_price must be less than or equal to max_price"
