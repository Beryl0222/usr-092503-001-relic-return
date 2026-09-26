"""领域错误：每种错误带有稳定的对外错误码与 HTTP 状态。"""

from __future__ import annotations


class DomainError(Exception):
    """所有业务错误的基类。"""

    status = 400
    code = "validation"

    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


class ValidationError(DomainError):
    """输入不合法。"""

    status = 400
    code = "validation"


class UnknownActorError(DomainError):
    """操作者未登记。"""

    status = 401
    code = "unknown_actor"


class ForbiddenError(DomainError):
    """职责不符或违反职责分离。"""

    status = 403
    code = "forbidden"


class NotFoundError(DomainError):
    """目标不存在。"""

    status = 404
    code = "not_found"


class InvalidTransitionError(DomainError):
    """当前阶段不允许该动作（错序/跳步）。"""

    status = 409
    code = "invalid_transition"


class OnHoldError(DomainError):
    """案件处于争议受限状态。"""

    status = 409
    code = "on_hold"


class ConflictError(DomainError):
    """与已有记录冲突（重复标识、重复提交等）。"""

    status = 409
    code = "conflict"
