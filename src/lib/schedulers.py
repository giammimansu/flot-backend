import json
import os
from datetime import datetime

import boto3

scheduler = boto3.client("scheduler")

def create_unlock_timeout_schedule(match_id: str, fire_at: datetime):
    """Crea un one-shot scheduler che scatta al timeout dell'unlock."""
    scheduler.create_schedule(
        Name=f"unlock-timeout-{match_id}",
        ScheduleExpression=f"at({fire_at.strftime('%Y-%m-%dT%H:%M:%S')})",
        ScheduleExpressionTimezone="UTC",
        FlexibleTimeWindow={"Mode": "OFF"},
        Target={
            "Arn": os.environ.get("UNLOCK_TIMEOUT_FUNCTION_ARN", ""),
            "RoleArn": os.environ.get("SCHEDULER_ROLE_ARN", ""),
            "Input": json.dumps({"detail": {"matchId": match_id}}),
        },
        ActionAfterCompletion="DELETE",
    )

def cancel_unlock_timeout_schedule(match_id: str):
    """Cancella il timeout scheduler se il match viene sbloccato in tempo."""
    try:
        scheduler.delete_schedule(Name=f"unlock-timeout-{match_id}")
    except scheduler.exceptions.ResourceNotFoundException:
        pass  # già scattato o già cancellato

def create_unlock_reminder_schedule(
    match_id: str,
    partner_id: str,
    reminder_number: int,
    total_reminders: int,
    fire_at: datetime,
):
    """Crea un one-shot reminder per il partner non rispondente."""
    scheduler.create_schedule(
        Name=f"unlock-reminder-{match_id}-{reminder_number}",
        ScheduleExpression=f"at({fire_at.strftime('%Y-%m-%dT%H:%M:%S')})",
        ScheduleExpressionTimezone="UTC",
        FlexibleTimeWindow={"Mode": "OFF"},
        Target={
            "Arn": os.environ.get("UNLOCK_REMINDER_FUNCTION_ARN", ""),
            "RoleArn": os.environ.get("SCHEDULER_ROLE_ARN", ""),
            "Input": json.dumps({
                "detail": {
                    "matchId": match_id,
                    "partnerId": partner_id,
                    "reminderNumber": reminder_number,
                    "totalReminders": total_reminders,
                },
            }),
        },
        ActionAfterCompletion="DELETE",
    )

def cancel_all_unlock_reminders(match_id: str):
    """Cancella tutti i reminder schedulati per un match."""
    for i in range(1, 10):  # max 10 reminder
        try:
            scheduler.delete_schedule(Name=f"unlock-reminder-{match_id}-{i}")
        except scheduler.exceptions.ResourceNotFoundException:
            break
