"""Pydantic request/response schemas, re-exported flat.

Split out of the former `models/schemas.py` by domain. Every name that module
exported is re-exported here, so the public surface is unchanged.
"""

from schemas.audit import (
    AuditCreatedResponse,
    AuditCreateRequest,
    AuditResponse,
    AuditStatus,
    CategoryReport,
    PageMeta,
    PrioritizedFix,
)
from schemas.backlinks import (
    LinkFlairOut,
    PublishResult,
    RedditConnectionStatus,
    RedditDraftRequest,
    RedditDraftResponse,
    RedditPostKind,
    RedditPostsResponse,
    RedditPostStatus,
    RedditPostSummary,
    RedditPublishRequest,
    StoreProfileSummary,
    SubredditRuleOut,
    SubredditRulesResponse,
    SubredditsResponse,
    SubredditSummary,
)
from schemas.health import HealthResponse
from schemas.search_console import (
    ConnectionStatus,
    GoogleSite,
    GoogleSitesResponse,
    GscSyncLogEntry,
    GscSyncLogResponse,
    SearchAnalyticsResponse,
    SearchAnalyticsRow,
    StoredAnalyticsResponse,
    StoredAnalyticsRow,
    StoredGscSite,
    StoredGscSitesResponse,
)

__all__ = [
    "AuditCreateRequest",
    "AuditCreatedResponse",
    "AuditResponse",
    "AuditStatus",
    "CategoryReport",
    "ConnectionStatus",
    "GoogleSite",
    "GoogleSitesResponse",
    "GscSyncLogEntry",
    "GscSyncLogResponse",
    "HealthResponse",
    "LinkFlairOut",
    "PageMeta",
    "PrioritizedFix",
    "PublishResult",
    "RedditConnectionStatus",
    "RedditDraftRequest",
    "RedditDraftResponse",
    "RedditPostKind",
    "RedditPostStatus",
    "RedditPostSummary",
    "RedditPostsResponse",
    "RedditPublishRequest",
    "SearchAnalyticsResponse",
    "SearchAnalyticsRow",
    "StoreProfileSummary",
    "StoredAnalyticsResponse",
    "StoredAnalyticsRow",
    "StoredGscSite",
    "StoredGscSitesResponse",
    "SubredditRuleOut",
    "SubredditRulesResponse",
    "SubredditSummary",
    "SubredditsResponse",
]
