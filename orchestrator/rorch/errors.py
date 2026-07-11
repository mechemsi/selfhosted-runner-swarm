# Copyright (c) 2026 Mechemsi. All rights reserved.
# Licensed under the MIT License. See LICENSE file in the project root.

"""Typed operational errors shared across orchestrator boundaries."""


class GitHubRateLimitError(RuntimeError):
    """GitHub requested that this token stop until a specific time."""

    def __init__(self, retry_at_epoch: float, reason: str) -> None:
        self.retry_at_epoch = retry_at_epoch
        self.reason = reason
        super().__init__(reason)
