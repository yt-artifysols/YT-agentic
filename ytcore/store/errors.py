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


class JobBudgetExceeded(BudgetExceeded):
    """The per-job budget cap was hit."""


class KnowledgeStale(StoreError):
    """The knowledge draft is based on an older approved version."""


class KnowledgeForbidden(StoreError):
    """Knowledge can only be approved or rejected by an active human approver."""


class LicenseInvalid(StoreError):
    """A music or stock asset has no valid license for the channel."""


class InputRejected(StoreError):
    """An owner input violates a rule (duplicate, immutable, wrong channel, archived)."""
