"""ORM models.

Importing this package registers every table on `Base.metadata`, which is what Alembic
autogenerate and the migrations env rely on.
"""

from brain_api.models.demo_request import DemoRequest
from brain_api.models.entitlement import Entitlement
from brain_api.models.precheck_link import PrecheckAccountLink
from brain_api.models.processed_stripe_event import ProcessedStripeEvent
from brain_api.models.refresh_token import RefreshToken
from brain_api.models.tenant import Tenant
from brain_api.models.user import User

__all__ = [
    "DemoRequest",
    "Entitlement",
    "PrecheckAccountLink",
    "ProcessedStripeEvent",
    "RefreshToken",
    "Tenant",
    "User",
]
