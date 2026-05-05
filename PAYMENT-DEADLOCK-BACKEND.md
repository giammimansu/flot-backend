# Flot — Backend: Payment Deadlock Resolution (Smart Auto-Capture)

> **File di contesto**: leggi `FLOT-README.md`, `CLAUDE-CODE-BACKEND-PROMPT_v3-SCHEDULED.md` e `CLAUDE-CODE-BACKEND-PROMPT_v4-ELASTIC.md` prima di iniziare.
> Questo file **non sostituisce** i prompt v3 e v4. Li estende con la logica di risoluzione del Payment Deadlock.
> Gli sprint esistenti (1–4) restano invariati. Qui si aggiungono sprint nuovi.

---

## Task

Implementa il sistema **Smart Auto-Capture** per eliminare il Payment Deadlock nel flusso di unlock dei match. Il criterio di successo è: quando l'Utente A sblocca un match, l'Utente B riceve una notifica urgente con pressione sociale ("Marco ha già sbloccato!"), e se B non risponde entro il timeout configurabile, l'auth di A viene annullata automaticamente (€0 addebitati) e il trip di A torna nel pool per un re-match — il tutto senza alcun intervento manuale dell'utente o dell'operatore.

Dimmi il tuo piano in massimo 6 step. Inizia a scrivere codice solo dopo che ti ho confermato il piano.

Se devi rompere una delle regole definite in questo documento, nel prompt v3 o nel prompt v4, fermati e dimmelo.

---

## Contesto del problema

### Cos'è il Payment Deadlock

Un match richiede che **entrambi** gli utenti paghino €0.99 (Trip Pass) per sbloccare il contatto reciproco. Il deadlock si verifica quando:

1. **Gioco del pollo**: nessuno vuole pagare per primo ("e se l'altro non paga?")
2. **Asimmetria temporale**: Utente A paga, Utente B è in volo / distratto / ignora la notifica
3. **Trust debt**: Utente A vede l'auth pendente sull'estratto conto e si sente ansioso o truffato

Il risultato è un match tecnicamente valido che non si converte mai in transazione completata. La "Liquidity" del marketplace crolla.

### Soluzione scelta: Smart Auto-Capture (Approccio D)

Il funnel utente resta **identico alla v3** — zero step aggiuntivi. Le differenze sono:

1. **Copy di trust sotto il CTA Unlock**: "Charged ONLY when both unlock. No mutual unlock = no charge."
2. **Stato intermedio `partially_unlocked`**: quando un utente sblocca, il partner riceve notifiche urgenti con pressione sociale naturale ("Marco ha già sbloccato!")
3. **Timeout con auto-void**: se il partner non risponde entro il timeout, l'auth del primo viene annullata e il trip torna nel pool
4. **Shadow Pool re-match**: il Matchmaker cerca automaticamente un nuovo partner per il trip ri-immesso

---

## Match Lifecycle — Aggiornato con Payment Deadlock Resolution

```
Match creato (da Matchmaker v3 o lock window v4)
  → status: "pending"
  → notifica push + email + WS a entrambi

Utente A preme "Unlock"
  → Stripe PaymentIntent (capture_method: manual) → auth €0.99
  → Match status: "partially_unlocked"
  → Match.unlockedBy: ["userA_id"]
  → Match.firstUnlockAt: timestamp
  → Match.unlockDeadline: firstUnlockAt + unlock_timeout_minutes
  → Emetti: match.partially_unlocked

  → Notifica URGENTE a Utente B:
     Push: "[Nome A] ha sbloccato! Sblocca anche tu per condividere il taxi"
     Email: CTA diretto con deep link a /match/:matchId
     WS: se online

  → Reminder automatici a B (EventBridge Scheduler):
     +30 min: push "Il tuo partner ti sta aspettando"
     +60 min: push "Hai ancora [tempo] per sbloccare"
     +90 min: push "Ultima chance — il match scade tra 30 minuti"
     (intervalli configurabili in AirportConfig.unlock_reminder_intervals)

Utente B preme "Unlock" (entro deadline)
  → Stripe PaymentIntent auth €0.99
  → Capture SIMULTANEO di entrambi i PaymentIntent
  → Match status: "unlocked"
  → Match.unlockedBy: ["userA_id", "userB_id"]
  → Emetti: payment.completed (×2)
  → Chat attiva + dettagli completi partner

Utente B NON risponde (deadline scaduta)
  → Void PaymentIntent Utente A (€0 addebitati)
  → Match status: "unlock_expired"
  → Trip A: status torna a "scheduled" (re-immesso nel pool)
  → Trip B: status torna a "scheduled" (re-immesso nel pool)
  → Emetti: match.unlock_expired
  → Notifica ad A: "Il tuo partner non ha risposto. Nessun addebito. Cerchiamo qualcun altro!"
  → Notifica a B: "Hai perso il match con [Nome A]. Cercheremo un nuovo partner."
  → Matchmaker al prossimo ciclo: cerca nuovi match per entrambi i trip

NESSUNO sblocca (entrambi ignorano il match)
  → Nessun auth Stripe in corso → nessun void necessario
  → Dopo unlock_no_response_dissolve_hours (12h): match dissolto
  → Entrambi i trip tornano nel pool
  → Emetti: match.dissolved { reason: "no_response" }

Utente A vuole cancellare DOPO aver sbloccato (durante attesa B)
  → Void PaymentIntent A
  → Match status: "dissolved"
  → Trip A: "cancelled"
  → Trip B: torna "scheduled"
  → Emetti: match.dissolved { reason: "user_cancelled_during_wait" }
```

---

## Nuovi stati del Match

| Status | Descrizione | Transizioni possibili |
|--------|-------------|----------------------|
| `pending` | Match creato, nessuno ha sbloccato | → `partially_unlocked`, `dissolved`, `unlock_expired` |
| `partially_unlocked` | **NUOVO** — Un utente ha sbloccato (auth hold attivo) | → `unlocked`, `unlock_expired`, `dissolved` |
| `unlocked` | Entrambi hanno sbloccato (capture completato) | → `completed` |
| `unlock_expired` | **NUOVO** — Timeout scaduto, auth voided, trip re-pooled | terminale |
| `dissolved` | **NUOVO** — Match annullato (delay, no response, user cancel) | terminale |
| `completed` | Viaggio completato | terminale |

---

## Entità DynamoDB — Campi aggiornati

### Match entity — nuovi campi

```python
item = {
    "pk": f"MATCH#{match_id}",
    "sk": "META",
    # ... campi esistenti v3 ...
    "status": "pending",                         # pending | partially_unlocked | unlocked | unlock_expired | dissolved
    "unlockedBy": [],                             # lista userId che hanno sbloccato
    "firstUnlockAt": None,                        # NUOVO — timestamp del primo unlock
    "unlockDeadline": None,                       # NUOVO — deadline per il secondo unlock
    "firstUnlockPaymentIntentId": None,           # NUOVO — PI del primo unlock (per void)
    "secondUnlockPaymentIntentId": None,          # NUOVO — PI del secondo unlock
    "dissolveReason": None,                       # NUOVO — "no_response" | "flight_delay" | "user_cancelled_during_wait" | "rematch"
}
```

### Nessun nuovo GSI richiesto

La query per trovare match `partially_unlocked` con `unlockDeadline` scaduta viene gestita dalla nuova Lambda `UnlockTimeoutFunction` che usa un EventBridge Scheduler one-shot (vedi sotto), non un GSI dedicato. Questo è più efficiente: il timer scatta esattamente al momento giusto, senza polling.

---

## Nuovi parametri in AirportConfig

```python
@dataclass
class AirportConfig:
    # ... campi esistenti v3 + v4 ...
    unlock_timeout_minutes: int                 # NUOVO — timeout per risposta partner (default: 120)
    unlock_reminder_intervals: list[int]        # NUOVO — minuti per reminder [30, 60, 90]
    unlock_repool_enabled: bool                 # NUOVO — abilita re-match dopo timeout (default: True)
    unlock_no_response_dissolve_hours: int      # NUOVO — se nessuno sblocca, dissolvi match (default: 12)

# MXP:
# unlock_timeout_minutes = 120
# unlock_reminder_intervals = [30, 60, 90]
# unlock_repool_enabled = True
# unlock_no_response_dissolve_hours = 12
```

---

## EventBridge — Nuovi eventi

```
Bus: flot-events

# Payment Deadlock Resolution
match.partially_unlocked     → un utente ha sbloccato, notifica urgente al partner
match.unlock_reminder        → reminder push/email al partner non rispondente
match.unlock_expired         → timeout scaduto, void auth + re-pool
match.dissolved              → match annullato (no_response, flight_delay, user_cancel, rematch)
```

### Routing Lambda per evento

| Evento | Lambda handler | Azione |
|--------|---------------|--------|
| `match.partially_unlocked` | `on_partial_unlock.py` | Notifica urgente a partner + crea scheduled reminder |
| `match.unlock_reminder` | `on_unlock_reminder.py` | Push/email reminder escalante |
| `match.unlock_expired` | `on_unlock_expired.py` | Void auth, re-pool trip, notifica entrambi |
| `match.dissolved` | `on_match_dissolved.py` | Cleanup, notifica, aggiorna trip status |

---

## Handler: Unlock Match — Aggiornato

```python
# src/handlers/matches/unlock_match.py — AGGIORNATO
# POST /trips/:tripId/unlock

def handler(event, context):
    user_id = get_user_id(event)
    body = validate(UnlockRequest, event["body"])
    match = get_match(body.matchId)
    trip = get_trip(event["pathParameters"]["tripId"])
    airport = get_airport(match["airportCode"])

    # Validazioni
    if match["status"] not in ("pending", "partially_unlocked"):
        raise AppError(400, "Match is not in a valid state for unlock")

    if user_id in match.get("unlockedBy", []):
        raise AppError(400, "You have already unlocked this match")

    if user_id not in (match["userId1"], match["userId2"]):
        raise AppError(403, "Not your match")

    # FAKE_DOOR_MODE check
    if os.environ.get("FAKE_DOOR_MODE") == "true":
        # Registra intent senza Stripe
        record_fake_door_intent(user_id, match["matchId"])
        return {"fakeDoor": True, "message": "Coming soon"}

    # Crea PaymentIntent con capture manuale
    intent = stripe.PaymentIntent.create(
        amount=airport.unlock_fee,
        currency=airport.currency.lower(),
        capture_method="manual",
        metadata={
            "matchId": match["matchId"],
            "userId": user_id,
            "tripId": trip["tripId"],
            "airportCode": airport.code,
        },
    )

    now = datetime.now(timezone.utc)
    unlocked_by = match.get("unlockedBy", []) + [user_id]

    if len(unlocked_by) == 1:
        # ── PRIMO UNLOCK ──
        # Auth hold attivo, ma nessun capture
        deadline = now + timedelta(minutes=airport.unlock_timeout_minutes)

        table.update_item(
            Key={"pk": match["pk"], "sk": "META"},
            UpdateExpression=(
                "SET #status = :status, "
                "unlockedBy = :ub, "
                "firstUnlockAt = :fua, "
                "unlockDeadline = :ud, "
                "firstUnlockPaymentIntentId = :fpi, "
                "updatedAt = :ua"
            ),
            ExpressionAttributeNames={"#status": "status"},
            ExpressionAttributeValues={
                ":status": "partially_unlocked",
                ":ub": unlocked_by,
                ":fua": now.isoformat(),
                ":ud": deadline.isoformat(),
                ":fpi": intent.id,
                ":ua": now.isoformat(),
            },
            ConditionExpression=Attr("status").eq("pending"),  # idempotenza
        )

        # Emetti evento per notifica urgente al partner
        partner_id = match["userId2"] if user_id == match["userId1"] else match["userId1"]
        partner_user = get_user(partner_id)
        unlocking_user = get_user(user_id)

        put_event("match.partially_unlocked", {
            "matchId": match["matchId"],
            "unlockedByUserId": user_id,
            "unlockedByName": unlocking_user.get("firstName", "Your partner"),
            "partnerUserId": partner_id,
            "unlockDeadline": deadline.isoformat(),
            "airportCode": airport.code,
            "reminderIntervals": airport.unlock_reminder_intervals,
        })

        # Crea EventBridge Scheduler one-shot per il timeout
        create_unlock_timeout_schedule(
            match_id=match["matchId"],
            fire_at=deadline,
        )

        logger.info("match_partially_unlocked",
            matchId=match["matchId"],
            unlockedBy=user_id,
            deadline=deadline.isoformat(),
        )

    elif len(unlocked_by) == 2:
        # ── SECONDO UNLOCK — CAPTURE SIMULTANEO ──
        first_pi_id = match["firstUnlockPaymentIntentId"]

        # Capture entrambi in transazione
        try:
            stripe.PaymentIntent.capture(first_pi_id)
            stripe.PaymentIntent.capture(intent.id)
        except stripe.error.StripeError as e:
            # Se il capture del primo fallisce (expired?), void il secondo
            logger.error("capture_failed", matchId=match["matchId"], error=str(e))
            stripe.PaymentIntent.cancel(intent.id)
            raise AppError(500, "Payment capture failed. No charges applied.")

        table.update_item(
            Key={"pk": match["pk"], "sk": "META"},
            UpdateExpression=(
                "SET #status = :status, "
                "unlockedBy = :ub, "
                "secondUnlockPaymentIntentId = :spi, "
                "updatedAt = :ua"
            ),
            ExpressionAttributeNames={"#status": "status"},
            ExpressionAttributeValues={
                ":status": "unlocked",
                ":ub": unlocked_by,
                ":spi": intent.id,
                ":ua": now.isoformat(),
            },
            ConditionExpression=Attr("status").eq("partially_unlocked"),
        )

        # Cancella il timeout scheduler (non serve più)
        cancel_unlock_timeout_schedule(match["matchId"])

        # Emetti payment.completed per entrambi
        put_event("payment.completed", {
            "matchId": match["matchId"],
            "userId1": match["userId1"],
            "userId2": match["userId2"],
        })

        logger.info("match_fully_unlocked", matchId=match["matchId"])

    # Salva Payment record
    save_payment(user_id, match["matchId"], intent.id, airport)

    return {
        "paymentIntentClientSecret": intent.client_secret,
        "amount": airport.unlock_fee,
        "currency": airport.currency.lower(),
        "matchStatus": "partially_unlocked" if len(unlocked_by) == 1 else "unlocked",
    }
```

---

## Handler: On Partial Unlock — NUOVO

```python
# src/handlers/events/on_partial_unlock.py
# EventBridge: match.partially_unlocked → notifica urgente al partner

def handler(event, context):
    detail = event["detail"]
    partner_id = detail["partnerUserId"]
    unlocked_by_name = detail["unlockedByName"]
    match_id = detail["matchId"]
    deadline = detail["unlockDeadline"]
    airport = get_airport(detail["airportCode"])

    partner = get_user(partner_id)
    savings = airport.base_fare // 2 / 100

    # 1. WebSocket (se online)
    send_ws_notification(partner_id, "partner_unlocked", {
        "matchId": match_id,
        "partnerName": unlocked_by_name,
        "deadline": deadline,
    })

    # 2. Push notification — urgente
    if partner.get("pushToken"):
        send_push_notification(
            token=partner["pushToken"],
            title=f"{unlocked_by_name} ha sbloccato! 🔓",
            body=f"Sblocca anche tu per condividere il taxi e risparmiare ~€{savings:.0f}",
            data={"matchId": match_id, "action": "open_match"},
            priority="high",
        )

    # 3. Email con CTA diretto
    if partner.get("email"):
        send_email(
            to=partner["email"],
            template="partner_unlocked",
            data={
                "partnerName": unlocked_by_name,
                "savings": savings,
                "matchUrl": f"https://app.flot.app/match/{match_id}",
                "deadline": deadline,
            },
        )

    # 4. Salva notifica in-app
    save_notification(partner_id, {
        "type": "partner_unlocked",
        "title": f"{unlocked_by_name} ha sbloccato!",
        "body": "Sblocca anche tu per condividere il taxi",
        "matchId": match_id,
    })

    # 5. Crea scheduled reminders
    reminder_intervals = detail.get("reminderIntervals", [30, 60, 90])
    first_unlock_at = datetime.fromisoformat(detail.get("unlockDeadline")).replace(tzinfo=timezone.utc)
    # Calcola il firstUnlockAt dal deadline - timeout
    first_unlock_at = (
        datetime.fromisoformat(deadline.replace("Z", "+00:00"))
        - timedelta(minutes=airport.unlock_timeout_minutes)
    )

    for offset_min in reminder_intervals:
        fire_at = first_unlock_at + timedelta(minutes=offset_min)
        if fire_at < datetime.now(timezone.utc):
            continue  # non creare reminder nel passato

        create_unlock_reminder_schedule(
            match_id=match_id,
            partner_id=partner_id,
            reminder_number=reminder_intervals.index(offset_min) + 1,
            total_reminders=len(reminder_intervals),
            fire_at=fire_at,
        )

    logger.info("partial_unlock_notified",
        matchId=match_id,
        partnerId=partner_id,
        remindersScheduled=len(reminder_intervals),
    )
```

---

## Handler: On Unlock Expired — NUOVO

```python
# src/handlers/events/on_unlock_expired.py
# EventBridge Scheduler one-shot → match.unlock_expired

def handler(event, context):
    detail = event["detail"]
    match_id = detail["matchId"]
    match = get_match(match_id)

    # Guard: il match potrebbe essere stato sbloccato nel frattempo
    if match["status"] != "partially_unlocked":
        logger.info("unlock_timeout_skipped",
            matchId=match_id,
            currentStatus=match["status"],
        )
        return

    airport = get_airport(match["airportCode"])

    # 1. Void PaymentIntent del primo unlock
    first_pi_id = match.get("firstUnlockPaymentIntentId")
    if first_pi_id:
        try:
            stripe.PaymentIntent.cancel(first_pi_id)
            logger.info("payment_intent_voided", piId=first_pi_id)
        except stripe.error.StripeError as e:
            logger.error("void_failed", piId=first_pi_id, error=str(e))
            # Continua comunque — il PI scadrà da solo

    # 2. Aggiorna Match status
    table.update_item(
        Key={"pk": f"MATCH#{match_id}", "sk": "META"},
        UpdateExpression=(
            "SET #status = :status, "
            "dissolveReason = :reason, "
            "updatedAt = :ua"
        ),
        ExpressionAttributeNames={"#status": "status"},
        ExpressionAttributeValues={
            ":status": "unlock_expired",
            ":reason": "partner_no_response",
            ":ua": now_iso(),
        },
        ConditionExpression=Attr("status").eq("partially_unlocked"),
    )

    # 3. Re-pool entrambi i trip (se abilitato)
    if airport.unlock_repool_enabled:
        trip_a = get_trip(match["tripId1"])
        trip_b = get_trip(match["tripId2"])

        for trip in [trip_a, trip_b]:
            # Solo se il trip non è scaduto o cancellato
            if trip["status"] in ("matched", "partially_unlocked_wait"):
                repool_trip(trip)

    # 4. Notifica entrambi gli utenti
    unlocked_user_id = match["unlockedBy"][0]
    partner_user_id = match["userId2"] if unlocked_user_id == match["userId1"] else match["userId1"]

    # Al primo (che ha pagato): rassicurazione
    notify_user(unlocked_user_id, {
        "type": "unlock_expired_payer",
        "title": "Nessun addebito",
        "body": "Il tuo partner non ha risposto in tempo. €0 addebitati. Cerchiamo qualcun altro!",
        "matchId": match_id,
    })

    # Al secondo (che non ha risposto): info
    notify_user(partner_user_id, {
        "type": "unlock_expired_non_payer",
        "title": "Match scaduto",
        "body": "Non hai sbloccato in tempo. Cercheremo un nuovo partner per te.",
        "matchId": match_id,
    })

    # 5. Cancella eventuali reminder schedulati rimasti
    cancel_all_unlock_reminders(match_id)

    metrics.add_metric(name="UnlockTimeouts", unit=MetricUnit.Count, value=1)
    logger.info("unlock_expired",
        matchId=match_id,
        unlockedBy=unlocked_user_id,
        nonResponder=partner_user_id,
    )


def repool_trip(trip: dict):
    """Rimette un trip nel pool per il re-match dal Matchmaker."""
    table.update_item(
        Key={"pk": trip["pk"], "sk": "META"},
        UpdateExpression=(
            "SET #status = :status, "
            "gsi5pk = :gsi5, "
            "matchId = :mid, "
            "updatedAt = :ua"
        ),
        ExpressionAttributeNames={"#status": "status"},
        ExpressionAttributeValues={
            ":status": "scheduled",
            ":gsi5": f"{trip['airportCode']}#scheduled",
            ":mid": None,
            ":ua": now_iso(),
        },
    )
    logger.info("trip_repooled", tripId=trip["pk"])
```

---

## Handler: On Unlock Reminder — NUOVO

```python
# src/handlers/events/on_unlock_reminder.py
# EventBridge Scheduler one-shot → reminder push/email escalante

def handler(event, context):
    detail = event["detail"]
    match_id = detail["matchId"]
    partner_id = detail["partnerId"]
    reminder_number = detail["reminderNumber"]
    total_reminders = detail["totalReminders"]

    match = get_match(match_id)

    # Guard: match già sbloccato o scaduto
    if match["status"] != "partially_unlocked":
        return

    partner = get_user(partner_id)
    unlocked_user_id = match["unlockedBy"][0]
    unlocked_user = get_user(unlocked_user_id)
    unlocked_name = unlocked_user.get("firstName", "Your partner")
    airport = get_airport(match["airportCode"])
    deadline = datetime.fromisoformat(match["unlockDeadline"].replace("Z", "+00:00"))
    minutes_left = max(0, int((deadline - datetime.now(timezone.utc)).total_seconds() / 60))

    # Escalazione copy in base al reminder number
    if reminder_number == 1:
        title = f"{unlocked_name} ti sta aspettando"
        body = f"Hai ancora {minutes_left} min per sbloccare e risparmiare ~€{airport.base_fare // 2 / 100:.0f}"
    elif reminder_number == total_reminders:
        title = "⏰ Ultima chance!"
        body = f"Il match con {unlocked_name} scade tra {minutes_left} min. Sblocca ora o perdi il match."
    else:
        title = f"Hai ancora {minutes_left} min"
        body = f"{unlocked_name} ha già sbloccato. Sblocca per condividere il taxi."

    # Push
    if partner.get("pushToken"):
        send_push_notification(
            token=partner["pushToken"],
            title=title,
            body=body,
            data={"matchId": match_id, "action": "open_match"},
            priority="high",
        )

    # Email solo per reminder escalati (non spammare)
    if reminder_number >= total_reminders - 1 and partner.get("email"):
        send_email(
            to=partner["email"],
            template="unlock_reminder_urgent",
            data={
                "partnerName": unlocked_name,
                "minutesLeft": minutes_left,
                "matchUrl": f"https://app.flot.app/match/{match_id}",
            },
        )

    logger.info("unlock_reminder_sent",
        matchId=match_id,
        partnerId=partner_id,
        reminderNumber=reminder_number,
        minutesLeft=minutes_left,
    )
```

---

## Handler: Match Dissolve (no response da entrambi) — NUOVO

```python
# src/handlers/events/on_match_dissolved.py
# EventBridge: match.dissolved

def handler(event, context):
    detail = event["detail"]
    match_id = detail["matchId"]
    reason = detail["reason"]
    match = get_match(match_id)

    if match["status"] in ("unlocked", "completed", "unlock_expired", "dissolved"):
        return  # già gestito

    # Aggiorna match
    table.update_item(
        Key={"pk": f"MATCH#{match_id}", "sk": "META"},
        UpdateExpression="SET #status = :s, dissolveReason = :r, updatedAt = :ua",
        ExpressionAttributeNames={"#status": "status"},
        ExpressionAttributeValues={
            ":s": "dissolved",
            ":r": reason,
            ":ua": now_iso(),
        },
    )

    # Re-pool entrambi i trip
    airport = get_airport(match["airportCode"])
    if airport.unlock_repool_enabled:
        for trip_id in [match["tripId1"], match["tripId2"]]:
            trip = get_trip(trip_id)
            if trip["status"] not in ("cancelled", "expired", "completed"):
                repool_trip(trip)

    logger.info("match_dissolved", matchId=match_id, reason=reason)
```

---

## Schedulers — EventBridge One-Shot

```python
# src/lib/schedulers.py — NUOVO

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
            "Arn": os.environ["UNLOCK_TIMEOUT_FUNCTION_ARN"],
            "RoleArn": os.environ["SCHEDULER_ROLE_ARN"],
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
            "Arn": os.environ["UNLOCK_REMINDER_FUNCTION_ARN"],
            "RoleArn": os.environ["SCHEDULER_ROLE_ARN"],
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
```

---

## Matchmaker — Integrazione con re-pool

Il Matchmaker v3 (e v4) **non richiede modifiche** per supportare il re-pool. Quando un trip torna a `status: "scheduled"` e il suo `gsi5pk` viene aggiornato a `{airportCode}#scheduled`, il Matchmaker lo troverà automaticamente nel prossimo ciclo di scan.

L'unica accortezza: il Matchmaker deve verificare che un trip re-pooled non venga ri-matchato con lo **stesso partner** da cui è appena stato dissolto. Aggiungi un campo `previousMatchPartners` al trip:

```python
# Nel repool_trip():
table.update_item(
    Key={"pk": trip["pk"], "sk": "META"},
    UpdateExpression=(
        "SET #status = :status, "
        "gsi5pk = :gsi5, "
        "matchId = :mid, "
        "previousMatchPartners = list_append("
        "  if_not_exists(previousMatchPartners, :empty_list), :new_partner"
        "), "
        "updatedAt = :ua"
    ),
    ExpressionAttributeValues={
        ":status": "scheduled",
        ":gsi5": f"{trip['airportCode']}#scheduled",
        ":mid": None,
        ":empty_list": [],
        ":new_partner": [previous_partner_user_id],
        ":ua": now_iso(),
    },
)

# Nel Matchmaker, aggiungi filtro:
candidates = [
    c for c in candidates
    if c["userId"] not in trip.get("previousMatchPartners", [])
]
```

---

## SAM Template — Nuove risorse

```yaml
Resources:

  # NUOVO — Unlock Timeout Handler
  UnlockTimeoutFunction:
    Type: AWS::Serverless::Function
    Properties:
      Handler: src/handlers/events/on_unlock_expired.handler
      Runtime: python3.12
      MemorySize: 256
      Timeout: 30
      Policies:
        - DynamoDBCrudPolicy:
            TableName: !Ref FlotTable
        - EventBridgePutEventsPolicy:
            EventBusName: !Ref EventBus
        - Statement:
            Effect: Allow
            Action: scheduler:DeleteSchedule
            Resource: !Sub "arn:aws:scheduler:${AWS::Region}:${AWS::AccountId}:schedule/default/unlock-*"

  # NUOVO — Unlock Reminder Handler
  UnlockReminderFunction:
    Type: AWS::Serverless::Function
    Properties:
      Handler: src/handlers/events/on_unlock_reminder.handler
      Runtime: python3.12
      MemorySize: 256
      Timeout: 15

  # NUOVO — Partial Unlock Handler (notifica urgente)
  OnPartialUnlockFunction:
    Type: AWS::Serverless::Function
    Properties:
      Handler: src/handlers/events/on_partial_unlock.handler
      Runtime: python3.12
      MemorySize: 256
      Timeout: 30
      Events:
        EventBridgeRule:
          Type: EventBridgeRule
          Properties:
            EventBusName: !Ref EventBus
            Pattern:
              detail-type: ["match.partially_unlocked"]

  # NUOVO — Match Dissolved Handler
  OnMatchDissolvedFunction:
    Type: AWS::Serverless::Function
    Properties:
      Handler: src/handlers/events/on_match_dissolved.handler
      Runtime: python3.12
      MemorySize: 256
      Timeout: 30
      Events:
        EventBridgeRule:
          Type: EventBridgeRule
          Properties:
            EventBusName: !Ref EventBus
            Pattern:
              detail-type: ["match.dissolved"]

  # NUOVO — No-Response Dissolve Job (controlla match pending oltre la soglia)
  MatchDissolveCheckerFunction:
    Type: AWS::Serverless::Function
    Properties:
      Handler: src/handlers/matching/dissolve_checker.handler
      Runtime: python3.12
      MemorySize: 256
      Timeout: 30
      Events:
        ScheduledRule:
          Type: ScheduleV2
          Properties:
            ScheduleExpression: "rate(1 hour)"
            State: ENABLED

  # NUOVO — IAM Role per EventBridge Scheduler → Lambda
  SchedulerInvokeLambdaRole:
    Type: AWS::IAM::Role
    Properties:
      AssumeRolePolicyDocument:
        Version: "2012-10-17"
        Statement:
          - Effect: Allow
            Principal:
              Service: scheduler.amazonaws.com
            Action: sts:AssumeRole
      Policies:
        - PolicyName: InvokeLambda
          PolicyDocument:
            Version: "2012-10-17"
            Statement:
              - Effect: Allow
                Action: lambda:InvokeFunction
                Resource:
                  - !GetAtt UnlockTimeoutFunction.Arn
                  - !GetAtt UnlockReminderFunction.Arn
```

---

## File Structure — Nuovi file

```
flot-backend/
├── src/
│   ├── handlers/
│   │   ├── events/
│   │   │   ├── on_partial_unlock.py         # NUOVO — notifica urgente partner
│   │   │   ├── on_unlock_expired.py         # NUOVO — void auth + re-pool
│   │   │   ├── on_unlock_reminder.py        # NUOVO — reminder escalante
│   │   │   └── on_match_dissolved.py        # NUOVO — cleanup match dissolto
│   │   ├── matching/
│   │   │   └── dissolve_checker.py          # NUOVO — job orario no-response check
│   │   └── matches/
│   │       └── unlock_match.py              # AGGIORNATO — partially_unlocked flow
│   ├── lib/
│   │   ├── airports.py                      # AGGIORNATO — nuovi campi unlock_*
│   │   └── schedulers.py                    # NUOVO — one-shot EventBridge schedulers
├── tests/
│   ├── unit/
│   │   ├── test_unlock_flow.py              # NUOVO
│   │   ├── test_unlock_timeout.py           # NUOVO
│   │   └── test_repool.py                   # NUOVO
```

---

## Test Unitari — Nuovi casi

```python
# tests/unit/test_unlock_flow.py

def test_first_unlock_sets_partially_unlocked():
    """Il primo unlock crea auth hold e mette il match in partially_unlocked."""
    match = create_test_match(status="pending")
    result = unlock_match(user_id=match["userId1"], match_id=match["matchId"])
    assert result["matchStatus"] == "partially_unlocked"
    updated = get_match(match["matchId"])
    assert updated["status"] == "partially_unlocked"
    assert updated["unlockedBy"] == [match["userId1"]]
    assert updated["firstUnlockPaymentIntentId"] is not None
    assert updated["unlockDeadline"] is not None

def test_second_unlock_captures_both():
    """Il secondo unlock cattura entrambi i PI e mette il match in unlocked."""
    match = create_test_match(status="partially_unlocked", unlockedBy=["user1"])
    result = unlock_match(user_id="user2", match_id=match["matchId"])
    assert result["matchStatus"] == "unlocked"
    updated = get_match(match["matchId"])
    assert updated["status"] == "unlocked"
    assert set(updated["unlockedBy"]) == {"user1", "user2"}

def test_duplicate_unlock_rejected():
    """Un utente non può sbloccare due volte."""
    match = create_test_match(status="partially_unlocked", unlockedBy=["user1"])
    with pytest.raises(AppError, match="already unlocked"):
        unlock_match(user_id="user1", match_id=match["matchId"])

def test_unlock_wrong_user_rejected():
    """Un utente non coinvolto nel match non può sbloccare."""
    match = create_test_match(userId1="user1", userId2="user2")
    with pytest.raises(AppError, match="Not your match"):
        unlock_match(user_id="user3", match_id=match["matchId"])


# tests/unit/test_unlock_timeout.py

def test_timeout_voids_auth_and_repools():
    """Allo scadere del timeout, l'auth viene annullata e i trip tornano nel pool."""
    match = create_test_match(
        status="partially_unlocked",
        unlockedBy=["user1"],
        firstUnlockPaymentIntentId="pi_test_123",
    )
    handle_unlock_expired({"detail": {"matchId": match["matchId"]}})
    updated = get_match(match["matchId"])
    assert updated["status"] == "unlock_expired"
    trip1 = get_trip(match["tripId1"])
    trip2 = get_trip(match["tripId2"])
    assert trip1["status"] == "scheduled"
    assert trip2["status"] == "scheduled"

def test_timeout_skipped_if_already_unlocked():
    """Se il match è già unlocked quando il timeout scatta, non succede nulla."""
    match = create_test_match(status="unlocked")
    handle_unlock_expired({"detail": {"matchId": match["matchId"]}})
    updated = get_match(match["matchId"])
    assert updated["status"] == "unlocked"  # invariato


# tests/unit/test_repool.py

def test_repooled_trip_excluded_from_same_partner():
    """Un trip re-pooled non deve essere ri-matchato con lo stesso partner."""
    trip = create_test_trip(
        status="scheduled",
        previousMatchPartners=["user_bad_partner"],
    )
    candidates = [
        {"userId": "user_bad_partner", "destLat": 45.47, "destLng": 9.19},
        {"userId": "user_good_partner", "destLat": 45.47, "destLng": 9.19},
    ]
    filtered = filter_candidates(trip, candidates)
    assert len(filtered) == 1
    assert filtered[0]["userId"] == "user_good_partner"
```

---

## Regole — Payment Deadlock specifiche

- **`partially_unlocked` è uno stato del Match, non del Trip.** Il trip resta `matched` durante l'attesa del secondo unlock.
- **Mai capture senza entrambi gli auth.** Il capture del primo PI avviene SOLO quando il secondo PI è stato autorizzato con successo.
- **I reminder sono one-shot EventBridge Scheduler**, non polling. Ogni reminder si auto-cancella dopo l'esecuzione (`ActionAfterCompletion: DELETE`).
- **Il timeout è configurabile per aeroporto** in `AirportConfig.unlock_timeout_minutes`. Mai hardcoded.
- **Il re-pool rispetta `previousMatchPartners`** per evitare loop infiniti con lo stesso partner non responsivo.
- **FAKE_DOOR_MODE**: quando attivo, l'unlock registra l'intent ma non crea PaymentIntent Stripe né scheduler. Il flusso `partially_unlocked` viene comunque simulato per testare la UX.
- **Float nei log**: `savings`, `timeout_minutes` arrotondati a 2 decimali.
- **Idempotenza**: `ConditionExpression` su ogni `update_item` per evitare race condition tra timeout e secondo unlock.

---

## DEVELOPMENT SEQUENCE — Nuovi Sprint

> Gli sprint 1–4 del prompt v3 restano invariati. Questi sprint si aggiungono dopo.

### Sprint 5: Payment Deadlock Resolution (Week 9-10)

33. `AirportConfig` aggiornato con campi `unlock_timeout_minutes`, `unlock_reminder_intervals`, `unlock_repool_enabled`, `unlock_no_response_dissolve_hours`
34. `unlock_match.py` aggiornato — logica `partially_unlocked` + capture simultaneo
35. `schedulers.py` — utility per EventBridge Scheduler one-shot (timeout + reminder)
36. `on_partial_unlock.py` — handler notifica urgente al partner
37. `on_unlock_reminder.py` — handler reminder escalante
38. `on_unlock_expired.py` — handler timeout void + re-pool
39. `on_match_dissolved.py` — handler cleanup match dissolto
40. `dissolve_checker.py` — job orario per match `pending` oltre soglia no-response
41. Matchmaker aggiornato — filtro `previousMatchPartners` per trip re-pooled
42. SAM template — nuove Lambda + IAM role Scheduler
43. Unit test: `test_unlock_flow.py`, `test_unlock_timeout.py`, `test_repool.py`
44. Integration test: flusso completo first-unlock → timeout → re-pool → re-match

---

## Environment Variables — Nuove

```env
UNLOCK_TIMEOUT_FUNCTION_ARN=<from SAM>
UNLOCK_REMINDER_FUNCTION_ARN=<from SAM>
SCHEDULER_ROLE_ARN=<from SAM>
```

---

*Flot Backend — Payment Deadlock Resolution — Maggio 2026*
