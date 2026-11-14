"""Connection notification choices shared by key issuance and OAuth consent."""
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field

NotificationType = Literal['approval_needed', 'order_decided', 'stockout_predicted', 'anomaly',
                           'proposal_received', 'suggestion_due', 'agent_paused', 'policy_changed', 'delivery_issue']
TYPES = tuple(NotificationType.__args__)
NotificationSourceEvent = Literal['delivery.issue', 'delivery.delayed', 'order.delivery_issue', 'order.delivery_delayed',
    'quote.anomaly_detected', 'agent.anomaly_alert', 'agent.suspended', 'policy.updated', 'suggestion.due',
    'stockout.alert', 'stockout.detected']


class NotificationConsent(BaseModel):
    model_config = ConfigDict(extra='forbid')
    approval_needed: bool = True
    order_decided: bool = True
    stockout_predicted: bool = False
    anomaly: bool = False
    proposal_received: bool = False
    suggestion_due: bool = False
    agent_paused: bool = False
    policy_changed: bool = False
    delivery_issue: bool = False


class QuietHours(BaseModel):
    model_config = ConfigDict(extra='forbid')
    start: str = Field(pattern=r'^(?:[01]\d|2[0-3]):[0-5]\d$')
    end: str = Field(pattern=r'^(?:[01]\d|2[0-3]):[0-5]\d$')
    timezone: str = 'UTC'


class NotificationPreferences(BaseModel):
    model_config = ConfigDict(extra='forbid')
    consent: NotificationConsent = Field(default_factory=NotificationConsent)
    quiet_hours: QuietHours | None = None
    rate_per_minute: int = Field(default=30, ge=1, le=300)
    reconsent_channels: list[str] = Field(default_factory=list, max_length=100)


class NotificationAction(BaseModel):
    model_config = ConfigDict(extra='forbid')
    action: Literal['acknowledge', 'claim', 'resolve']
    lease_seconds: int = Field(default=300, ge=1, le=3600)


def consent_form():
    return '<fieldset><legend>Allowed notifications</legend><input type="hidden" name="notification_choices" value="1">' + ''.join(
        f'<label><input type="checkbox" name="notification_{name}" value="yes"' +
        (' checked' if NotificationConsent().model_dump()[name] else '') + f'>{name.replace("_", " ")}</label>'
        for name in TYPES) + '</fieldset>'


def form_consent(form):
    return NotificationConsent(**{name: form.get('notification_' + name) == 'yes' for name in TYPES}) if form.get('notification_choices') else NotificationConsent()
