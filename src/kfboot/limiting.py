from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

import falcon
from keri import help

from kfboot.basing import QuotaRecord
from kfboot.config import ACCOUNT_ROUTES, ONBOARDING_ROUTES
from kfboot.utils import extractExnPayload, optionalStr
from kfboot.store import nowIso

logger = help.ogler.getLogger(__name__)

ACCOUNT_REQUEST_SCOPE = "account_request"
ONBOARDING_REQUEST_SCOPE = "onboarding_request_ip"


class Limiter:
    def __init__(self, ctx):
        self.ctx = ctx

    def enforceOnboardingRequestQuota(self, *, route: str, client_ip: str) -> None:
        """Throttle onboarding business requests by client IP."""

        # Make sure that it is effectives only on the Onboarding routes
        if route not in ONBOARDING_ROUTES:
            return

        # Get client IP
        client_ip = (client_ip or "").strip()
        if not client_ip:
            return

        # Get the requests limit from the config
        limit = self.ctx.config.bootstrap_onboarding_requests_per_minute
        if limit <= 0:
            return

        # Get the time a user gets blocked from the config
        block_seconds = max(self.ctx.config.bootstrap_onboarding_block_seconds, 0)

        # Get current time
        now = datetime.fromisoformat(nowIso())

        window = self._quotaRecord(ONBOARDING_REQUEST_SCOPE, client_ip, now=now)

        # Check if block time exists
        blocked_until = _parseOptionalDt(window.blocked_until)
        if blocked_until is not None:

            # Rejects requests if user is still blocked
            if now < blocked_until:
                retry_after = max(int((blocked_until - now).total_seconds()), 1)
                logger.warning(
                    f"Onboarding request rejected because client IP {client_ip} is blocked until {blocked_until.isoformat()}"
                )
                raise falcon.HTTPTooManyRequests(
                    title="Onboarding request rate limit exceeded",
                    description=(
                        f"Client IP {client_ip} exceeded {limit} onboarding request(s) per minute. "
                        f"Retry after {retry_after} second(s)."
                    ),
                    retry_after=retry_after,
                )
            # Reset window if he is passed his block time
            window.window_start = now.isoformat()
            window.count = 0
            window.blocked_until = ""

        # Calculate the elapsed time 
        window_start = _parseDt(window.window_start, default=now)
        elapsed = (now - window_start).total_seconds()

        # If elapsed is superior to the 1 min time window
        if elapsed >= 60:
            window.window_start = now.isoformat()
            window.count = 0

        # If user exceeds the limit
        if window.count >= limit:
            # Calculate block time with block_seconds + current time
            blocked_until = now + timedelta(seconds=block_seconds)
            window.blocked_until = blocked_until.isoformat()

            # Save the value in quota record
            self.ctx.store.saveQuota(window)

            # Calculate the time before retry
            retry_after = max(block_seconds, 1)

            logger.warning(
                f"Onboarding request per-IP rate limit exceeded for client IP {client_ip}."
                f" Limit is {limit} request(s) per minute; block period is {block_seconds} second(s)."
            )
            raise falcon.HTTPTooManyRequests(
                title="Onboarding request rate limit exceeded",
                description=(
                    f"Client IP {client_ip} exceeded {limit} onboarding request(s) per minute. "
                    f"Retry after {retry_after} second(s)."
                ),
                retry_after=retry_after,
            )

        window.count += 1
        self.ctx.store.saveQuota(window)

    def enforceAccountQuotas(self, serder) -> None:
        """Apply account quota enforcement for onboarding and account-side requests."""

        # Apply quotas only to onboarding and account routes
        route = str(serder.ked.get("r", "") or "")
        if route not in ONBOARDING_ROUTES and route not in ACCOUNT_ROUTES:
            return

        # Check account context for the request
        payload = extractExnPayload(serder)
        account_aid, profile = self._accountContextForRoute(serder, payload)
        # TODO - we may want to enforce some limits even without account context
        if not account_aid or profile is None:
            logger.debug(
                f"No account context found for route {route}"
            )
            return

        # Enforce request rate and KEL budget for the account
        self._enforceAccountRequestRate(account_aid, profile)
        self._enforceAccountKelBudget(account_aid, profile)

    def _enforceAccountRequestRate(self, account_aid: str, profile: Any) -> None:
        """Enforce per-account request limits on onboarding and account routes."""
        
        # Get the current time and the request window for this account
        now = datetime.fromisoformat(nowIso())
        window = self._quotaRecord(ACCOUNT_REQUEST_SCOPE, account_aid, now=now)
        
        # Reset the window if more than 60 seconds have elapsed since the start
        window_start = _parseDt(window.window_start, default=now)
        elapsed = (now - window_start).total_seconds()
        if elapsed >= 60:
            window.window_start = now.isoformat()
            window.count = 0

        # Check if the request count exceeds the profile limit for the window
        if window.count >= profile.max_requests_per_minute > 0:
            self.ctx.store.saveQuota(window)
            logger.warning(
                f"Account request per minute rate limit exceeded."
                f" User is limited to {profile.max_requests_per_minute} requests per minute under tier '{profile.tier}'"
            )
            raise falcon.HTTPTooManyRequests(
                title="Account request rate limit exceeded",
                description=(
                    f"Account {account_aid} exceeded {profile.max_requests_per_minute} requests in the rolling minute window. "
                    "Retry later or request a higher staging tier."
                ),
            )

        # If not exceeded, increment the count and log if approaching soft limits
        window.count += 1
        self.ctx.store.saveQuota(window)
        ratio = window.count / max(profile.max_requests_per_minute, 1)
        if ratio >= 0.95:
            logger.warning(f"Approaching request rate limit for account {account_aid}, current rate: 95%")
        elif ratio >= 0.85:
            logger.info(f"Approaching request rate limit for account {account_aid}, current rate: 85%")

    def _enforceAccountKelBudget(self, account_aid: str, profile: Any) -> None:
        """Enforce a fixed per-account KEL event quota on onboarding and account routes."""
        
        if profile.kel_budget <= 0:
            return

        account = self.ctx.store.getAccount(account_aid)
        if account is None:
            logger.info(
                f"Account does not exist yet, skipping enforcing KEL budget"
            )
            return

        count = account.kel_used
         
        if count >= profile.kel_budget:
            logger.warning(
                f"Account KEL budget exceeded for account {account_aid} under tier '{profile.tier}'",
            )
            raise falcon.HTTPTooManyRequests(
                title="Account key event budget exceeded",
                description=(
                    f"Account {account_aid} exceeded {profile.kel_budget} key events. "
                    "Request quota has been exhausted for this account tier."
                ),
            )

        count += 1
        account.kel_used = count
        self.ctx.store.saveAccount(account)
        ratio = count / max(profile.kel_budget, 1)
        if ratio >= 0.95:
            logger.warning(f"Approaching KEL budget limit for account {account_aid}, current rate: 95%")
        elif ratio >= 0.85:
            logger.info(f"Approaching KEL budget limit for account {account_aid}, current rate: 85%")

    def _accountContextForRoute(self, serder, payload: dict[str, Any]) -> tuple[str, Any]:
        """Resolve the account AID and tier profile for the current request."""
        route = str(serder.ked.get("r", "") or "")
        sender = serder.pre

        # For onboarding start, return the account AID and profile based on session context
        if route == "/onboarding/session/start":
            account_aid = optionalStr(payload, "account_aid")
            profile = self.ctx.config.account_profile(payload.get("chosen_profile_code", ""))
            return account_aid, profile

        # For other onboarding routes, resolve the account AID and profile from the session context
        session_id = optionalStr(payload, "session_id")
        if session_id:
            session = self.ctx.store.getSession(session_id)
            if session is not None:
                profile = self.ctx.config.account_profile(session.chosen_profile_code)
                account_aid = session.account_aid or optionalStr(payload, "account_aid")
                return account_aid, profile

        # For account routes, resolve the account AID and profile based on the authenticated sender
        if route in ACCOUNT_ROUTES:
            # Account routes are authenticated by the account AID sender.
            account_aid = sender
            account = self.ctx.store.getAccount(account_aid)
            profile = self.ctx.config.account_profile(account.witness_profile_code) if account is not None else None
            return account_aid, profile

        return "", None

    def _quotaRecord(self, scope: str, subject: str, *, now: datetime) -> QuotaRecord:
        """Get quota record from the store or create one if none"""
        record = self.ctx.store.getQuota(scope, subject)
        if record is not None:
            return record
        return QuotaRecord(
            scope=scope,
            subject=subject,
            window_start=now.isoformat(),
            count=0,
            blocked_until="",
        )


def _parseOptionalDt(value: str) -> datetime | None:
    """Parse the datetime value or return None"""
    if not value:
        return None
    return datetime.fromisoformat(value)


def _parseDt(value: str, *, default: datetime) -> datetime:
    """Parse the datetime value but returns a default if None"""
    if not value:
        return default
    return datetime.fromisoformat(value)
