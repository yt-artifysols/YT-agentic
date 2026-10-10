"""Typed errors raised by stores. DB trigger errors are converted into these."""

from __future__ import annotations


class StoreError(Exception):
    """Base class for store errors."""


class JobNotFound(StoreError):
    pass


class IllegalTransition(StoreError):
    pass


class ContentFrozen(StoreError):
    pass


class ApprovalGateError(StoreError):
    """No valid approval for the job's current content."""


class ApprovalInvalid(StoreError):
    """Token failed verification (signature, binding to job/channel/content, expiry)."""


class ApprovalReplayError(StoreError):
    """The token's jti was already consumed."""


class ChannelNotPublishable(StoreError):
    """Channel paused, cluster paused, shadow tier, or missing publish configuration."""


class BudgetExceeded(StoreError):
    pass


class QuotaExhausted(StoreError):
    pass


class UploadGuardError(StoreError):
    pass
