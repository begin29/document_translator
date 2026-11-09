from __future__ import annotations


class AppError(Exception):
    def __init__(
        self,
        code: str,
        message: str,
        detail: str = "",
        retryable: bool = False,
        status: int = 400,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.detail = detail
        self.retryable = retryable
        self.status = status

    def as_dict(self) -> dict:
        return {
            "code": self.code,
            "message": self.message,
            "detail": self.detail,
            "retryable": self.retryable,
        }


class ProviderTimeout(AppError):
    def __init__(self, detail: str = "") -> None:
        super().__init__(
            "provider_timeout",
            "The translation provider timed out before returning a translation.",
            detail,
            True,
            503,
        )


class ProviderFailure(AppError):
    def __init__(self, detail: str = "") -> None:
        super().__init__(
            "provider_error",
            "The translation provider returned an error and the segment was not translated.",
            detail,
            True,
            502,
        )


class RateLimited(AppError):
    def __init__(self, retry_after: float = 1.0, detail: str = "") -> None:
        super().__init__(
            "rate_limited",
            "The translation provider asked us to slow down. The job will retry.",
            detail or f"retry_after={retry_after}",
            True,
            429,
        )
        self.retry_after = retry_after


class FundsExceeded(AppError):
    def __init__(self, detail: str = "") -> None:
        super().__init__(
            "funds_exceeded",
            "The translation account has reached its spend limit. Raise the limit or add funds, then retry.",
            detail,
            False,
            402,
        )


class CostCapExceeded(AppError):
    def __init__(self, spent: str, cap: str) -> None:
        super().__init__(
            "cost_cap_exceeded",
            f"This job reached the cost cap of {cap} cents (spent {spent}). Translation stopped before further calls.",
            f"spent_cents={spent} cap_cents={cap}",
            False,
            409,
        )


class InjectedCrash(RuntimeError):
    pass
