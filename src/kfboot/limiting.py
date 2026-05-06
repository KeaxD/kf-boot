# limiting.py
from datetime import datetime
import falcon
from keri import help
from typing import Any

from kfboot.utils import _payload, _optional_str
from kfboot.config import ONBOARDING_ROUTES, ACCOUNT_ROUTES 
from kfboot.store import now_iso

logger = help.ogler.getLogger(__name__)

class Limiter:
    def __init__(self, ctx):
        self.ctx = ctx
        self._account_request_windows: dict[str, dict[str, Any]] = {}
        self._account_kel_usage: dict[str, int] = {}

    def enforceAccountQuotas(self, serder) -> None:
        """Apply account quota enforcement for onboarding and account-side requests."""

        # Apply quotas only to onboarding and account routes
        route = str(serder.ked.get("r", "") or "")
        if route not in ONBOARDING_ROUTES and route not in ACCOUNT_ROUTES:
            return

        # Check account context for the request
        payload = _payload(serder)
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
        now = datetime.fromisoformat(now_iso())
        window = self._account_request_windows.setdefault(
            account_aid,
            {"start": now, "count": 0},
        )
        # Reset the window if more than 60 seconds have elapsed since the start
        elapsed = (now - window["start"]).total_seconds()
        if elapsed >= 60:
            window["start"] = now
            window["count"] = 0

        # Check if the request count exceeds the profile limit for the window
        if window["count"] >= profile.max_requests_per_minute > 0:
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
        window["count"] += 1
        ratio = window["count"] / max(profile.max_requests_per_minute, 1)
        if ratio >= 0.95:
            logger.warning(f"Approaching request rate limit for account {account_aid}, current rate: 95%")
        elif ratio >= 0.85:
            logger.info(f"Approaching request rate limit for account {account_aid}, current rate: 85%")

    def _enforceAccountKelBudget(self, account_aid: str, profile: Any) -> None:
        """Enforce a fixed per-account KEL event quota on onboarding and account routes."""
        
        if profile.kel_budget <= 0:
            return

        account = self.ctx.store.get_account(account_aid)
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
        self.ctx.store.save_account(account)
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
            account_aid = _optional_str(payload, "account_aid")
            profile = self.ctx.config.account_profile(payload.get("chosen_profile_code", ""))
            return account_aid, profile

        # For other onboarding routes, resolve the account AID and profile from the session context
        session_id = _optional_str(payload, "session_id")
        if session_id:
            session = self.ctx.store.get_session(session_id)
            if session is not None:
                profile = self.ctx.config.account_profile(session.chosen_profile_code)
                account_aid = session.account_aid or _optional_str(payload, "account_aid")
                return account_aid, profile

        # For account routes, resolve the account AID and profile based on the authenticated sender
        if route in ACCOUNT_ROUTES:
            # Account routes are authenticated by the account AID sender.
            account_aid = sender
            account = self.ctx.store.get_account(account_aid)
            profile = self.ctx.config.account_profile(account.witness_profile_code) if account is not None else None
            return account_aid, profile

        return "", None
