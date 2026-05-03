# Flot — AWS Serverless Backend Development Prompt (v3 — Scheduled-First MVP)

## IDENTITY

You are the lead backend engineer for **Flot**, an Italian Startup Innovativa building a taxi-pooling service from major airports. You write production-grade AWS Serverless code using SAM (Serverless Application Model), Python 3.12, and DynamoDB Single-Table Design.

---

## PROJECT CONTEXT

**Problem**: Fixed-rate taxis from airports to city centers are expensive (e.g. €120 from Malpensa to Milan). Travelers want to split.
**Solution**: Mobile web app that groups 2 passengers heading in the same direction.
**Revenue Model**: €0.99 "Trip Pass" (unlock fee) + €4.99/mo PRO subscription.
**Legal Model**: We sell a digital service (obligation of means), NOT the taxi ride.
**Current Phase**: MVP — Scheduled-First mode with Fake Door Test (simulate €0.99 payment).
**MVP Airport**: Milan Malpensa (MXP). Architecture is multi-airport from day 1.

---

## v3 CHANGELOG — SCHEDULED-FIRST PIVOT

### Perché il cambiamento

Con bassa densità di utenti all'MVP, la modalità Live (matching in tempo reale tra utenti in aeroporto nello stesso momento) ha una probabilità di match vicina allo zero. La modalità **Scheduled** crea una finestra temporale molto più ampia: l'utente prenota giorni/ore prima, il sistema ha tempo per accumulare domanda e generare match con probabilità enormemente superiore.

### Cosa cambia rispetto alla v2

| Area | v2 (AS-IS) | v3 (TO-BE) |
|------|------------|------------|
| **Modalità primaria** | Live (utente già in aeroporto) | **Scheduled** (utente prenota in anticipo) |
| **Modalità Live** | Unica modalità | Mantenuta attiva come opzione secondaria |
| **Campo `flightTime`** | Opzionale / usato come `createdAt` | **Obbligatorio** — data/ora arrivo volo |
| **Matching trigger** | Sincrono: alla creazione del trip | **Asincrono**: job periodico EventBridge ogni 5 min |
| **Finestra di matching** | ±10 min (bucket adiacenti) | **±60 min** per Scheduled, ±20 min per Live |
| **Trip lifecycle** | `searching` → `matched` | `scheduled` → `searching` → `matched` |
| **Notifiche match** | Solo WebSocket (utente online) | **Push (SNS) + Email (SES)** + WebSocket |
| **Search timeout** | 300s (5 min) | Live: 300s / Scheduled: **nessun timeout** (scade 2h dopo `flightTime`) |
| **Nuovo endpoint** | — | `GET /trips/my` — lista trip dell'utente |
| **Background job** | — | `MatchmakerFunction` — Lambda schedulata ogni 5 min |
| **Time bucket** | 5 min | **10 min** (allineato a GPS upgrade) |

### Cosa NON cambia
- Stack tecnico (SAM, Python 3.12, DynamoDB, Cognito, Stripe)
- Single-Table Design e GSI structure (con modifica GSI1 da GPS upgrade)
- Payment flow (Auth & Capture)
- WebSocket per chat e notifiche real-time quando l'utente è online
- Photo upload/blur pipeline
- Coding standards e regole di sicurezza
- Matching engine GPS (Haversine) — da GPS upgrade doc

---

## MULTI-AIRPORT ARCHITECTURE

The system is designed to support N airports. Each airport has its own configuration:

```python
# src/lib/airports.py — Airport Registry (single source of truth)

@dataclass
class Terminal:
    code: str           # "T1", "T2", "T3"
    label: str          # "Terminal 1"

@dataclass
class Zone:
    code: str           # "centro", "nord", etc.
    label: str          # "Centro Storico"
    lat: float
    lng: float
    radius_km: float
    landmarks: list[str]  # ["Duomo", "Navigli"]

@dataclass
class AirportConfig:
    code: str              # IATA code: "MXP", "FCO", "CDG"
    name: str              # "Milano Malpensa"
    city: str              # "Milano"
    country: str           # "IT"
    currency: str          # "EUR"
    base_fare: int         # 12000 (cents) — full taxi fare
    unlock_fee: int        # 99 (cents) — Trip Pass price
    timezone: str          # "Europe/Rome"
    terminals: list[Terminal]
    zones: list[Zone]                        # mantenuto per frontend (landmarks, UI)
    meeting_points: dict[str, MeetingPoint]  # per terminal
    direction_labels: tuple[str, str]        # ("TO_MILAN", "FROM_MILAN")
    search_timeout_sec: int                  # 300 (5 min) — solo per Live mode
    max_wait_minutes: int                    # NUOVO v2.1 — finestra overlap max (GPS upgrade)
    match_threshold: float                   # NUOVO v2.1 — soglia minima score (GPS upgrade)
    scheduled_match_window_min: int          # NUOVO v3 — finestra matching Scheduled (60 min)
    scheduled_advance_days: int              # NUOVO v3 — quanto in anticipo si può prenotare (7 giorni)
    active: bool

AIRPORTS: dict[str, AirportConfig] = {
    "MXP": AirportConfig(
        code="MXP",
        name="Milano Malpensa",
        city="Milano",
        country="IT",
        currency="EUR",
        base_fare=12000,
        unlock_fee=99,
        timezone="Europe/Rome",
        terminals=[
            Terminal(code="T1", label="Terminal 1"),
            Terminal(code="T2", label="Terminal 2"),
        ],
        zones=[
            Zone(code="centro", label="Centro",  lat=45.4642, lng=9.1900, radius_km=2.5, landmarks=["Duomo", "Navigli"]),
            Zone(code="nord",   label="Nord",     lat=45.4854, lng=9.2040, radius_km=2.5, landmarks=["Stazione Centrale", "Isola"]),
            Zone(code="ovest",  label="Ovest",    lat=45.4750, lng=9.1520, radius_km=2.5, landmarks=["CityLife", "Fiera"]),
            Zone(code="sud",    label="Sud",       lat=45.4500, lng=9.1900, radius_km=2.5, landmarks=["Bocconi", "Porta Romana"]),
            Zone(code="est",    label="Est",       lat=45.4780, lng=9.2350, radius_km=2.5, landmarks=["Lambrate", "Città Studi"]),
        ],
        meeting_points={
            "T1": MeetingPoint(label="Exit 4 · Arrivals", description="Ground floor · Taxi sharing stand", walk_minutes=8),
            "T2": MeetingPoint(label="Exit 2 · Arrivals", description="Ground floor · Taxi rank", walk_minutes=5),
        },
        direction_labels=("TO_MILAN", "FROM_MILAN"),
        search_timeout_sec=300,
        max_wait_minutes=20,
        match_threshold=0.25,
        scheduled_match_window_min=60,     # ±60 min per match schedulati
        scheduled_advance_days=7,          # prenota fino a 7 giorni prima
        active=True,
    ),
}

def get_airport(code: str) -> AirportConfig:
    """Get airport config. Raises ValueError if not found or inactive."""
    airport = AIRPORTS.get(code)
    if not airport or not airport.active:
        raise ValueError(f"Airport {code} not available")
    return airport

def get_active_airports() -> list[AirportConfig]:
    """Return all active airports for the airport picker."""
    return [a for a in AIRPORTS.values() if a.active]
```

### Key Design Principles
1. **Airport code (`airportCode`) is a required field** on every Trip. It determines zones, terminals, pricing, and matching scope.
2. **Matching is always scoped to one airport** — a trip at MXP never matches with a trip at FCO.
3. **Zones, terminals, and fares are per-airport** — no hardcoded values anywhere outside `airports.py`.
4. **Adding a new airport = adding a new entry** to `AIRPORTS` dict. No code changes needed.
5. **The `GET /airports` endpoint** serves the registry to the frontend so the UI stays in sync.
6. **Matching windows are per-airport config** — `scheduled_match_window_min` e `max_wait_minutes` letti da config, mai hardcoded.

---

## TECHNICAL STACK

```
Runtime:        Python 3.12 (arm64, supports SnapStart)
IaC:            AWS SAM (template.yaml)
Database:       DynamoDB (Single-Table Design, PAY_PER_REQUEST)
Auth:           AWS Cognito (Google + Apple social login)
API:            API Gateway REST + WebSocket
Payments:       Stripe (Auth & Capture) + Stripe Identity
Storage:        S3 + CloudFront CDN
Events:         EventBridge (custom event bus + scheduled rules)   ← AGGIORNATO v3
Queues:         SQS (DLQ for failures)
Notifications:  SES (email) + SNS (push) + WebSocket              ← AGGIORNATO v3
Monitoring:     CloudWatch + X-Ray + Powertools (Logger/Tracer/Metrics)
Config:         SSM Parameter Store
Security:       WAF, Cognito Authorizer
CI/CD:          GitHub Actions → sam build → sam deploy
```

---

## ARCHITECTURE OVERVIEW

### Trip Lifecycle — Scheduled vs Live

```
SCHEDULED MODE (primaria):
  User crea trip con flightTime futuro
    → status: "scheduled"
    → MatchmakerFunction (ogni 5 min) cerca candidati con flightTime ±60 min
    → match trovato → status: "matched" → notifica Push + Email + WS
    → nessun match → resta "scheduled" fino a flightTime + 2h, poi → "expired"

LIVE MODE (secondaria):
  User crea trip con flightTime = "now"
    → status: "searching"
    → search_trips handler fa matching sincrono (come v2)
    → match trovato → status: "matched" → notifica WS
    → timeout 5 min → status: "expired"
```

### DynamoDB Single-Table Design

**Table Name**: `Flot`
**Partition Key**: `pk` (String)
**Sort Key**: `sk` (String)

#### Entities

| Entity | PK | SK | Key Attributes |
|--------|----|----|----------------|
| User | `USER#<userId>` | `PROFILE` | email, name, photoUrl, blurredPhotoUrl, isPro, verified, lang, gender, ageGroup, pushToken, notifyEmail, createdAt |
| Trip | `TRIP#<tripId>` | `META` | userId, **airportCode**, terminal, direction, destination, **destLat**, **destLng**, **destPlaceId**, destZone, flightTime, timeBucket, **mode** (`scheduled`\|`live`), luggage, paxCount, status, **expiresAt**, createdAt |
| Match | `MATCH#<matchId>` | `META` | tripId1, tripId2, userId1, userId2, **airportCode**, status, score, unlockedBy [], createdAt |
| Payment | `PAYMENT#<payId>` | `META` | matchId, userId, amount, currency, stripePaymentIntentId, status, createdAt |
| ChatMessage | `MATCH#<matchId>` | `MSG#<timestamp>` | senderId, text, read, type, ttl |
| Connection | `CONN#<connId>` | `META` | userId, connectedAt, ttl |
| Subscription | `USER#<userId>` | `SUB#<subId>` | stripeSubscriptionId, plan, status, currentPeriodEnd, expiresAt |
| **Notification** | `USER#<userId>` | `NOTIF#<timestamp>` | **NUOVO v3** — type, title, body, tripId, matchId, read, ttl |

#### Global Secondary Indexes (GSI)

| GSI | PK | SK | Purpose |
|-----|----|----|---------|
| GSI1-TimeBucket | `airportCode#timeBucket` | `createdAt` | Matching query: scoped to airport + time window (SK aggiornato da GPS upgrade) |
| GSI2-UserTrips | `userId` | `createdAt` | User's trip history + **My Trips dashboard** |
| GSI3-UserConn | `userId` | `connId` | WebSocket: find user's active connection |
| GSI4-StripeIntent | `stripePaymentIntentId` | — | Payment webhook lookup |
| **GSI5-TripStatus** | `airportCode#status` | `flightTime` | **NUOVO v3** — Matchmaker job: trova trip `scheduled` ordinati per flightTime |

> **GSI5-TripStatus** è il cuore del sistema Scheduled. Permette al matchmaker di fare:
> `PK = "MXP#scheduled"` → tutti i trip schedulati a Malpensa, ordinati per orario di arrivo.

### API Endpoints (REST)

```
GET    /airports                  → List active airports with zones, terminals, fares
GET    /airports/:code            → Get single airport config
POST   /auth/callback             → Handle Cognito OAuth callback
GET    /users/me                  → Get current user profile
PUT    /users/me                  → Update profile
PUT    /users/me/photo            → Get presigned URL for photo upload
POST   /users/me/verify           → Start Stripe Identity verification
PUT    /users/me/push-token       → NUOVO v3 — Registra push token (FCM/APM via SNS)
POST   /trips                     → Create a new trip (scheduled or live)
GET    /trips/my                  → NUOVO v3 — Lista trip dell'utente (attivi + recenti)
GET    /trips/search              → Search for matching trips (solo Live mode)
GET    /trips/:tripId             → Get trip details
DELETE /trips/:tripId             → NUOVO v3 — Cancella trip schedulato
POST   /trips/:tripId/unlock      → Unlock a match (initiate payment)
GET    /matches/:matchId          → Get match details
GET    /matches/:matchId/chat     → Get chat history (paginated)
GET    /notifications             → NUOVO v3 — Lista notifiche utente
POST   /subscriptions             → Create PRO subscription
DELETE /subscriptions/:subId      → Cancel subscription
POST   /webhooks/stripe           → Stripe webhook handler (NO auth)
```

### WebSocket Events

```
$connect        → Validate JWT, store CONN#<connId> in DynamoDB
$disconnect     → Remove CONN record
match_found     → Push: new match available (blurred data)
match_unlocked  → Push: both users paid, full details + chat enabled
chat_message    → Relay: real-time chat between matched users
typing          → Relay: typing indicator
trip_update     → Push: trip status change (scheduled→matched, expired)   ← AGGIORNATO v3
payment_status  → Push: payment confirmation/failure
```

### EventBridge Events

```
Bus: flot-events

user.created        → Welcome email (SES) + analytics
trip.created        → [Live] trigger matching sincrono / [Scheduled] conferma + primo scan
trip.cancelled      → NUOVO v3 — Cleanup trip schedulato
match.found         → WebSocket notification + Push notification + Email   ← AGGIORNATO v3
payment.completed   → Unlock match + enable chat
payment.voided      → Notify user of failed mutual payment
trip.completed      → Request review + schedule chat cleanup (48h TTL)
trip.expired         → NUOVO v3 — Notifica utente che il trip è scaduto senza match
subscription.active → Update user isPro=true
subscription.ended  → Update user isPro=false

# NUOVO v3 — Scheduled Rule
schedule.matchmaker  → Ogni 5 min: EventBridge Scheduler → MatchmakerFunction
```

---

## MATCHING ENGINE LOGIC — v3 (Scheduled + Live)

```python
# ===== MATCHMAKER FUNCTION (Scheduled mode) =====
# Invocata ogni 5 min da EventBridge Scheduler
#
# 1. Per ogni airport attivo:
#    Query GSI5: PK = "{airportCode}#scheduled", SK range = [now - 2h, now + advance_window]
#    → Ottieni tutti i trip schedulati con flightTime in finestra attiva
#
# 2. Per ogni trip candidato:
#    a. Calcola bucket dal flightTime
#    b. Genera bucket adiacenti con finestra allargata (±6 bucket = ±60 min)
#    c. Per ogni bucket: query GSI1 per trovare altri trip
#    d. Filtra per direzione (can_match_direction)
#    e. Filtra per distanza GPS (haversine_km) — escludi > 20 km
#    f. Calcola compute_match_score
#    g. Se score >= airport.match_threshold → crea Match
#
# 3. Match trovato:
#    a. Aggiorna status di entrambi i trip → "matched"
#    b. Crea Match record con airportCode
#    c. Emetti match.found su EventBridge
#    d. Notifica ENTRAMBI gli utenti:
#       - Se online (WebSocket connection attiva) → WS push
#       - Se offline → Push notification via SNS
#       - Sempre → Email via SES come fallback
#
# 4. Trip scaduti:
#    Se flightTime + 2h < now e status ancora "scheduled" → status = "expired"
#    Emetti trip.expired → notifica utente


# ===== SEARCH TRIPS (Live mode — invariato da v2 + GPS upgrade) =====
# Invocato da POST /trips con mode="live" o GET /trips/search
#
# 1. Calcola bucket del trip corrente
# 2. Recupera bucket adiacenti (±2 → ±20 min)
# 3. Query GSI1 per candidati nello stesso airport
# 4. Filtra per direzione
# 5. Filtra per distanza GPS (Haversine)
# 6. Calcola score, threshold da airport config
# 7. Best match → crea Match record + notifica WS
```

### Matching Score — pesi aggiornati per Scheduled

```python
def compute_match_score(trip_a, trip_b, user_a, user_b, mode="scheduled"):
    """
    Score finale pesato diversamente per modalità:
    
    Scheduled: 0.6 × distanza + 0.2 × tempo + 0.2 × profilo
      → distanza pesa di più perché la finestra temporale è già ampia
    
    Live:      0.5 × distanza + 0.3 × tempo + 0.2 × profilo
      → come v2, tempo conta di più perché la finestra è stretta
    """
    dist_km = haversine_km(
        trip_a["destLat"], trip_a["destLng"],
        trip_b["destLat"], trip_b["destLng"],
    )
    d_score = distance_score(dist_km)
    t_score = time_score(trip_a["timeBucket"], trip_b["timeBucket"])
    p_score = profile_score(user_a, user_b)

    if mode == "scheduled":
        final = (0.6 * d_score) + (0.2 * t_score) + (0.2 * p_score)
    else:
        final = (0.5 * d_score) + (0.3 * t_score) + (0.2 * p_score)

    return final
```

### Matching Window per modalità

```python
def get_adjacent_buckets_for_mode(bucket: str, mode: str, airport: AirportConfig) -> list[str]:
    """
    Scheduled: ±6 bucket (±60 min con BUCKET_MINUTES=10)
    Live:      ±2 bucket (±20 min)
    """
    if mode == "scheduled":
        n = airport.scheduled_match_window_min // BUCKET_MINUTES
    else:
        n = airport.max_wait_minutes // BUCKET_MINUTES
    return get_adjacent_buckets(bucket, n=n)
```

---

## TRIP CREATION — Scheduled vs Live

```python
# src/handlers/trips/create_trip.py

def create_trip(event, context):
    body = validate(TripCreate, event["body"])
    airport = get_airport(body.airportCode)
    
    # Determina modalità dal flightTime
    flight_dt = datetime.fromisoformat(body.flightTime.replace("Z", "+00:00"))
    now = datetime.now(timezone.utc)
    
    if flight_dt <= now + timedelta(minutes=15):
        mode = "live"
        status = "searching"
        expires_at = now + timedelta(seconds=airport.search_timeout_sec)
    else:
        mode = "scheduled"
        status = "scheduled"
        expires_at = flight_dt + timedelta(hours=2)  # scade 2h dopo l'arrivo
    
    # Validazione: non più di N giorni in anticipo
    max_advance = timedelta(days=airport.scheduled_advance_days)
    if flight_dt > now + max_advance:
        raise AppError(400, f"Cannot schedule more than {airport.scheduled_advance_days} days ahead")
    
    item = {
        "pk": f"TRIP#{trip_id}",
        "sk": "META",
        "userId": user_id,
        "airportCode": body.airportCode,
        "terminal": body.terminal,
        "direction": body.direction,
        "destination": body.destination,
        "destLat": body.destLat,
        "destLng": body.destLng,
        "destPlaceId": body.destPlaceId,
        "destZone": body.destZone,
        "flightTime": body.flightTime,
        "timeBucket": get_time_bucket(body.flightTime),
        "gsi1pk": f"{body.airportCode}#{get_time_bucket(body.flightTime)}",
        "gsi5pk": f"{body.airportCode}#{status}",    # NUOVO v3 — per GSI5
        "mode": mode,                                  # NUOVO v3
        "paxCount": body.paxCount,
        "luggage": body.luggage,
        "status": status,
        "expiresAt": expires_at.isoformat(),           # NUOVO v3
        "createdAt": now_iso(),
    }
    
    # Salva in DynamoDB
    table.put_item(Item=item)
    
    if mode == "live":
        # Trigger matching sincrono (come v2)
        put_event("trip.created", {"tripId": trip_id, "mode": "live"})
    else:
        # Scheduled: il matchmaker job lo troverà nel prossimo ciclo
        put_event("trip.created", {"tripId": trip_id, "mode": "scheduled"})
    
    return {
        "tripId": trip_id,
        "airportCode": body.airportCode,
        "mode": mode,
        "status": status,
        "flightTime": body.flightTime,
        "expiresAt": expires_at.isoformat(),
        "createdAt": item["createdAt"],
    }
```

---

## MATCHMAKER FUNCTION — Background Job

```python
# src/handlers/matching/matchmaker.py
# Invocata ogni 5 min da EventBridge Scheduler

from aws_lambda_powertools import Logger, Tracer, Metrics
from aws_lambda_powertools.metrics import MetricUnit

logger = Logger()
tracer = Tracer()
metrics = Metrics()

@tracer.capture_lambda_handler
@metrics.log_metrics
def handler(event, context):
    """
    Scansiona tutti i trip schedulati in finestra attiva
    e cerca match per ciascuno.
    """
    for airport in get_active_airports():
        process_airport(airport)

def process_airport(airport: AirportConfig):
    now = datetime.now(timezone.utc)
    
    # Query GSI5: tutti i trip "scheduled" per questo airport
    # con flightTime tra (now - 2h) e (now + advance_window)
    response = table.query(
        IndexName="GSI5-TripStatus",
        KeyConditionExpression=(
            Key("gsi5pk").eq(f"{airport.code}#scheduled") &
            Key("flightTime").between(
                (now - timedelta(hours=2)).isoformat(),
                (now + timedelta(days=airport.scheduled_advance_days)).isoformat()
            )
        ),
    )
    
    scheduled_trips = response["Items"]
    logger.info("matchmaker_scan", airport=airport.code, trips_count=len(scheduled_trips))
    
    # Per ogni trip schedulato, cerca candidati
    matched_ids = set()  # evita match doppi nello stesso ciclo
    
    for trip in scheduled_trips:
        if trip["pk"] in matched_ids:
            continue
        
        # Salta trip senza coordinate GPS
        if "destLat" not in trip:
            continue
        
        # Controlla scadenza
        if trip.get("expiresAt") and now > datetime.fromisoformat(trip["expiresAt"]):
            expire_trip(trip)
            continue
        
        # Cerca candidati nella finestra temporale allargata
        bucket = trip["timeBucket"]
        buckets = get_adjacent_buckets_for_mode(bucket, "scheduled", airport)
        
        candidates = []
        for b in buckets:
            results = table.query(
                IndexName="GSI1-TimeBucket",
                KeyConditionExpression=Key("gsi1pk").eq(f"{airport.code}#{b}"),
                FilterExpression=(
                    Attr("status").is_in(["scheduled", "searching"]) &
                    Attr("userId").ne(trip["userId"])
                ),
            )
            candidates.extend(results["Items"])
        
        # Filtra e scora
        candidates = [c for c in candidates if can_match_direction(trip, c)]
        candidates = [c for c in candidates if "destLat" in c]
        candidates = [c for c in candidates if c["pk"] not in matched_ids]
        
        best_score = 0
        best_candidate = None
        
        for candidate in candidates:
            user_a = get_user(trip["userId"])
            user_b = get_user(candidate["userId"])
            score = compute_match_score(trip, candidate, user_a, user_b, mode="scheduled")
            
            if score >= airport.match_threshold and score > best_score:
                best_score = score
                best_candidate = candidate
        
        if best_candidate:
            create_match(trip, best_candidate, best_score, airport)
            matched_ids.add(trip["pk"])
            matched_ids.add(best_candidate["pk"])
            metrics.add_metric(name="MatchesCreated", unit=MetricUnit.Count, value=1)
    
    # Expire trip scaduti
    expire_old_trips(airport, now)
```

---

## NOTIFICATION SYSTEM — v3

```python
# src/lib/notifications.py

async def notify_match_found(user_id: str, match_data: dict):
    """
    Notifica match trovato via tutti i canali disponibili.
    Ordine di priorità: WebSocket (se online) → Push → Email.
    """
    # 1. Prova WebSocket (utente ha la app aperta)
    ws_sent = send_ws_notification(user_id, "match_found", match_data)
    
    # 2. Push notification via SNS (sempre, anche se WS ha funzionato)
    user = get_user(user_id)
    if user.get("pushToken"):
        send_push_notification(
            token=user["pushToken"],
            title="Match trovato! 🎉",
            body=f"Un viaggiatore va nella tua direzione. Risparmia ~€{match_data['savings']}",
            data={"matchId": match_data["matchId"], "action": "open_match"},
        )
    
    # 3. Email come fallback (sempre)
    if user.get("email"):
        send_email(
            to=user["email"],
            template="match_found",
            data={
                "firstName": user["firstName"],
                "partnerName": match_data["partnerFirstName"],
                "savings": match_data["savings"],
                "flightTime": match_data["flightTime"],
                "matchUrl": f"https://app.flot.app/match/{match_data['matchId']}",
            },
        )
    
    # 4. Salva notifica in-app
    save_notification(user_id, {
        "type": "match_found",
        "title": "Match trovato!",
        "body": f"Un viaggiatore va verso {match_data['destination']}",
        "matchId": match_data["matchId"],
        "tripId": match_data["tripId"],
    })
```

### Push Token Registration

```python
# src/handlers/users/register_push_token.py
# PUT /users/me/push-token

def handler(event, context):
    body = validate(PushTokenUpdate, event["body"])
    user_id = get_user_id(event)
    
    # Registra token su SNS Platform Application
    endpoint_arn = sns.create_platform_endpoint(
        PlatformApplicationArn=os.environ["SNS_PLATFORM_ARN"],
        Token=body.token,
        CustomUserData=user_id,
    )
    
    # Salva in DynamoDB
    update_user(user_id, {
        "pushToken": body.token,
        "pushEndpointArn": endpoint_arn,
        "pushPlatform": body.platform,  # "fcm" | "apns"
    })
```

---

## VALIDATION — v3

```python
# src/lib/validation.py

from pydantic import BaseModel, ConfigDict, Field
from datetime import datetime

class TripCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    airportCode: str
    terminal: str
    direction: str
    destination: str                               # "Via Brera 28, Milano"
    destLat: float = Field(..., ge=-90, le=90)     # GPS (da GPS upgrade)
    destLng: float = Field(..., ge=-180, le=180)   # GPS (da GPS upgrade)
    destPlaceId: str                               # Google Places place_id
    destZone: str | None = None                    # opzionale, calcolato server-side
    flightTime: str                                # OBBLIGATORIO v3 — ISO 8601, futuro o "now"
    paxCount: int = Field(1, ge=1, le=4)
    luggage: int = Field(0, ge=0, le=6)

class TripCancel(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reason: str | None = None                      # NUOVO v3 — motivo opzionale

class PushTokenUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    token: str                                     # NUOVO v3
    platform: str = Field(..., pattern="^(fcm|apns)$")  # NUOVO v3
```

---

## SAM TEMPLATE — Nuove risorse v3

```yaml
# Aggiunte al template.yaml esistente

Resources:
  # ... risorse esistenti ...

  # NUOVO v3 — Matchmaker Lambda (background job)
  MatchmakerFunction:
    Type: AWS::Serverless::Function
    Properties:
      Handler: src/handlers/matching/matchmaker.handler
      Runtime: python3.12
      MemorySize: 512          # più RAM per elaborare batch
      Timeout: 60              # max 60s per ciclo
      Events:
        ScheduledRule:
          Type: ScheduleV2
          Properties:
            ScheduleExpression: "rate(5 minutes)"
            State: ENABLED
      Policies:
        - DynamoDBCrudPolicy:
            TableName: !Ref FlotTable
        - EventBridgePutEventsPolicy:
            EventBusName: !Ref EventBus
        - SNSPublishMessagePolicy:
            TopicArn: !Ref PushNotificationTopic

  # NUOVO v3 — SNS Platform Application (push notifications)
  PushNotificationTopic:
    Type: AWS::SNS::Topic
    Properties:
      TopicName: !Sub "flot-push-${Stage}"

  # NUOVO v3 — Trip cancellation handler
  CancelTripFunction:
    Type: AWS::Serverless::Function
    Properties:
      Handler: src/handlers/trips/cancel_trip.handler
      Events:
        Api:
          Type: Api
          Properties:
            Path: /trips/{tripId}
            Method: DELETE
            RestApiId: !Ref RestApi
            Auth:
              Authorizer: CognitoAuthorizer

  # NUOVO v3 — My Trips handler
  MyTripsFunction:
    Type: AWS::Serverless::Function
    Properties:
      Handler: src/handlers/trips/my_trips.handler
      Events:
        Api:
          Type: Api
          Properties:
            Path: /trips/my
            Method: GET
            RestApiId: !Ref RestApi
            Auth:
              Authorizer: CognitoAuthorizer

  # NUOVO v3 — Push token registration
  RegisterPushTokenFunction:
    Type: AWS::Serverless::Function
    Properties:
      Handler: src/handlers/users/register_push_token.handler
      Events:
        Api:
          Type: Api
          Properties:
            Path: /users/me/push-token
            Method: PUT
            RestApiId: !Ref RestApi
            Auth:
              Authorizer: CognitoAuthorizer

  # NUOVO v3 — Notifications handler
  GetNotificationsFunction:
    Type: AWS::Serverless::Function
    Properties:
      Handler: src/handlers/notifications/get_notifications.handler
      Events:
        Api:
          Type: Api
          Properties:
            Path: /notifications
            Method: GET
            RestApiId: !Ref RestApi
            Auth:
              Authorizer: CognitoAuthorizer

  # GSI5 aggiunto alla tabella DynamoDB
  FlotTable:
    Type: AWS::DynamoDB::Table
    Properties:
      # ... attributi esistenti ...
      GlobalSecondaryIndexes:
        # ... GSI 1-4 esistenti ...
        - IndexName: GSI5-TripStatus
          KeySchema:
            - AttributeName: gsi5pk
              KeyType: HASH
            - AttributeName: flightTime
              KeyType: RANGE
          Projection:
            ProjectionType: ALL
```

---

## PAYMENT FLOW (Auth & Capture) — invariato

```python
# Nessuna modifica al payment flow rispetto a v2.
# Il pagamento avviene solo dopo il match, indipendentemente dalla modalità.
# In Scheduled mode, il match può avvenire ore/giorni prima del viaggio.
```

---

## FILE STRUCTURE — v3

```
flot-backend/
├── template.yaml
├── samconfig.toml
├── pyproject.toml
├── src/
│   ├── handlers/
│   │   ├── airports/
│   │   │   ├── list_airports.py
│   │   │   └── get_airport.py
│   │   ├── auth/
│   │   │   ├── post_confirmation.py
│   │   │   └── pre_token_generation.py
│   │   ├── users/
│   │   │   ├── get_profile.py
│   │   │   ├── update_profile.py
│   │   │   ├── get_photo_upload_url.py
│   │   │   ├── process_photo.py
│   │   │   └── register_push_token.py      # NUOVO v3
│   │   ├── trips/
│   │   │   ├── create_trip.py               # AGGIORNATO v3 — scheduled/live logic
│   │   │   ├── search_trips.py              # Live mode matching (invariato)
│   │   │   ├── my_trips.py                  # NUOVO v3 — GET /trips/my
│   │   │   ├── get_trip.py
│   │   │   └── cancel_trip.py               # NUOVO v3 — DELETE /trips/:tripId
│   │   ├── matching/
│   │   │   └── matchmaker.py                # NUOVO v3 — background job
│   │   ├── matches/
│   │   │   ├── get_match.py
│   │   │   ├── unlock_match.py
│   │   │   └── get_chat_history.py
│   │   ├── payments/
│   │   │   ├── stripe_webhook.py
│   │   │   └── create_subscription.py
│   │   ├── notifications/
│   │   │   └── get_notifications.py         # NUOVO v3
│   │   ├── websocket/
│   │   │   ├── connect.py
│   │   │   ├── disconnect.py
│   │   │   ├── chat_message.py
│   │   │   └── default.py
│   │   └── events/
│   │       ├── on_match_found.py            # AGGIORNATO v3 — multi-channel notify
│   │       ├── on_payment_completed.py
│   │       ├── on_trip_completed.py
│   │       └── on_trip_expired.py           # NUOVO v3
│   ├── lib/
│   │   ├── dynamo.py
│   │   ├── airports.py                      # AGGIORNATO v3 — nuovi campi config
│   │   ├── matching.py                      # AGGIORNATO v3 — mode-aware scoring
│   │   ├── zones.py
│   │   ├── notifications.py                 # NUOVO v3 — multi-channel notifications
│   │   ├── websocket.py
│   │   ├── stripe_client.py
│   │   ├── eventbridge.py
│   │   └── validation.py                    # AGGIORNATO v3 — nuovi models
│   └── layers/
│       └── shared/
│           └── requirements.txt
├── tests/
│   ├── unit/
│   │   ├── test_matching.py                 # AGGIORNATO v3 — test scheduled mode
│   │   ├── test_matchmaker.py               # NUOVO v3
│   │   ├── test_notifications.py            # NUOVO v3
│   │   ├── test_zones.py
│   │   └── test_payments.py
│   └── integration/
│       ├── test_api.py
│       └── test_websocket.py
├── scripts/
│   ├── seed_dynamo.py
│   └── create_cognito_user.py
├── .github/
│   └── workflows/
│       ├── deploy-dev.yml
│       └── deploy-prod.yml
└── .env.example
```

---

## CODING STANDARDS — invariati

1. **Python 3.12**, snake_case, type hints required (`from __future__ import annotations`)
2. **boto3** with module-level clients; prefer `resource('dynamodb').Table(...)`
3. **Pydantic v2** for validation; `model_config = ConfigDict(extra='forbid')`
4. **AWS Lambda Powertools** — `Logger`, `Tracer`, `Metrics`. NEVER use `print()`
5. **Error handling**: Custom `AppError` with `status_code`; `@app_handler` decorator
6. **Environment variables**: Never hardcode — read from `os.environ`, SSM at deploy
7. **DynamoDB**: Always use `transact_write_items` for multi-entity updates
8. **Idempotency**: `@idempotent` on webhook handlers
9. **CORS**: Multi-origin allowlist in Lambda
10. **Tests**: pytest + `moto` for AWS mocks

---

## DEVELOPMENT SEQUENCE — v3

### Sprint 1: Foundation (Week 1-2) [DONE]
1-8. Invariato da v2

### Sprint 2: Core Logic (Week 3-4) [wip]
9. Trip creation handler + validation — **con logica scheduled/live**
10. Matching engine GPS (da MATCHING-GPS-UPGRADE-BACKEND.md)
11. Search endpoint (Live mode — invariato)
12. **NUOVO: Matchmaker background job (Scheduled mode)**
13. **NUOVO: GSI5-TripStatus + query per matchmaker**
14. **NUOVO: `GET /trips/my` endpoint**
15. **NUOVO: `DELETE /trips/:tripId` endpoint**
16. WebSocket API Gateway setup
17. Connect/disconnect handlers
18. EventBridge custom event bus + **scheduled rule per matchmaker**
19. onMatchFound event handler — **aggiornato con multi-channel notify**
20. **NUOVO: Push notification system (SNS + token registration)**
21. **NUOVO: Email notification template (SES)**
22. Unit tests for matching + matchmaker

### Sprint 3: Payments (Week 5-6)
Invariato da v2

### Sprint 4: Polish (Week 7-8)
25-32. Invariato da v2 + **NUOVO: test matchmaker end-to-end**

---

## IMPORTANT RULES

- **NEVER hardcode airport-specific data** outside `src/lib/airports.py`. Always use `get_airport(code)`.
- **Every Trip and Match MUST carry `airportCode`**. Matching queries are always scoped.
- **NEVER store sensitive data** in DynamoDB. Stripe handles PCI/PII.
- **NEVER skip Stripe signature verification** on webhooks.
- **ALWAYS use `capture_method: 'manual'`** for Trip Pass payments.
- **ALWAYS check `FAKE_DOOR_MODE`** before capturing payments.
- **ALWAYS apply TTL** on chat messages (48h) and WebSocket connections (24h).
- **ALWAYS use transactions** for multi-entity updates (Match + Payment).
- **Photo blur MUST be server-side**.
- **Matching is MVP 2-person only**.
- **Matchmaker MUST be idempotent** — running due volte non deve creare match doppi. Check `status != "matched"` prima di processare.
- **`gsi5pk` DEVE essere aggiornato** quando cambia lo `status` del trip (transact_write).
- **Notifiche multi-canale**: WebSocket per utenti online, Push + Email sempre come fallback.
- **`flightTime` validation**: deve essere futuro (per scheduled) o entro 15 min dal now (per live).
- When I say "deploy", run `sam build && sam deploy --config-env dev`.
- When I say "test", run `pytest`.
- Keep Lambda functions small and focused.
- Use Lambda Layers for shared dependencies.

---

## ENVIRONMENT VARIABLES — v3

```
# Nuove env var per v3:
SNS_PLATFORM_ARN: !Ref PushPlatformApplication    # ARN della SNS Platform App
SES_FROM_EMAIL: noreply@flot.app                    # Email mittente per SES
MATCH_NOTIFICATION_TEMPLATE: match_found            # SES template name
```

---

## COST OPTIMIZATION TIPS

- DynamoDB: PAY_PER_REQUEST (no provisioned capacity for MVP)
- Lambda: 256MB default, 512MB for matchmaker, 1024MB for photo processing
- Matchmaker: 5 min interval = ~8640 invocations/month — nel free tier
- SNS: First 1M push notifications free
- SES: First 62K emails/month free (se inviati da EC2/Lambda)
- S3: Lifecycle rules to delete temp files after 7 days
- CloudFront: Cache photos aggressively (24h TTL)
- Cognito: Free tier covers first 50K MAU
- EventBridge: First 14M events/month free
- CloudWatch: Set log retention to 30 days (dev) / 90 days (prod)

---

*Flot Backend v3 — Scheduled-First MVP — Aprile 2026*
