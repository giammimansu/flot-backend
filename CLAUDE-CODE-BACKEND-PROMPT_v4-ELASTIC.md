# Flot — AWS Serverless Backend Development Prompt (v4 — Elastic & Predictive)

## Task

Ridisegna l'architettura backend di Flot trasformandola in un sistema elastico e predittivo (v4). Il criterio di successo è: il nuovo sistema gestisce autonomamente i ritardi dei voli, ottimizza globalmente i match invece di accoppiarli in modo greedy, e penalizza le rotte inefficienti per il tassista — il tutto mantenendo costi operativi AWS sotto $50/mese a volume MVP.

Leggi `CLAUDE-CODE-BACKEND-PROMPT_v3-SCHEDULED.md` e `FLOT-README.md` come file di contesto prima di iniziare. Poi dimmi il tuo piano in massimo 6 step. Inizia a scrivere codice solo dopo che ti ho confermato il piano.

Se devi rompere una delle regole definite in questo documento o nel prompt v3, fermati e dimmelo.

---

## Contesto del prodotto

**Flot** è un servizio di taxi-pooling da aeroporti. Non è un servizio di taxi — vende un servizio di matching tra persone (obbligazione di mezzi).

- **Airport MVP**: Milano Malpensa (MXP)
- **Stack**: Python 3.12, AWS SAM, DynamoDB Single-Table Design, EventBridge
- **Fase attuale**: MVP — Scheduled-First con Fake Door Test
- **Versione precedente**: v3 (Scheduled + GPS Haversine + Matchmaker ogni 5 min)

### Limiti strategici della v3 che questa v4 risolve

| Problema v3 | Impatto | Soluzione v4 |
|-------------|---------|--------------|
| `flightTime` statico — i voli ritardano | Match "rotto" senza ricalcolo | Flight Tracker API event-driven |
| Matching greedy — prima coppia utile | Combinazioni subottimali | Shadow Pools + deferred matching |
| Raggio radiale 20 km | Rotte a "V" irrealistiche per il tassista | Corridoio direzionale (deviazione in minuti) |
| Soglia `match_threshold` fissa | Troppo selettiva 7gg prima, troppo lassista 1h prima | Soglia dinamica time-decay |

---

## Allineamento — piano di esecuzione

Prima di scrivere qualsiasi codice, dimmi il piano in massimo 6 step con:
- quale componente tocchi in ogni step
- quale file crei o modifichi
- quale test unitario valida lo step

---

## Feature Core — Descrizione tecnica

### 1. Flight Tracker Integration (Event-Driven)

#### Modello `TripCreate` aggiornato

Il campo `flightNumber` diventa **obbligatorio** insieme alla data di arrivo. Il `flightTime` iniziale viene auto-popolato dall'API di flight tracking al momento della creazione del trip, non inserito manualmente dall'utente.

```python
class TripCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    airportCode: str
    terminal: str
    direction: str
    destination: str
    destLat: float = Field(..., ge=-90, le=90)
    destLng: float = Field(..., ge=-180, le=180)
    destPlaceId: str
    destZone: str | None = None
    flightNumber: str                          # NUOVO v4 — es. "AZ1234", obbligatorio
    flightDate: str                            # NUOVO v4 — es. "2026-04-26", obbligatorio
    flightTime: str | None = None              # OPZIONALE v4 — auto-risolto dal tracker
    paxCount: int = Field(1, ge=1, le=4)
    luggage: int = Field(0, ge=0, le=6)
```

Il backend risolve `flightTime` reale da `flightNumber + flightDate` tramite il Flight Tracker al momento della `POST /trips`. Se l'API non risponde entro 2s, salva `flightTime = flightDate + "T12:00:00Z"` come fallback e mette il trip in stato `tracking_pending`.

#### FlightTrackerFunction — Polling e aggiornamento

Una Lambda dedicata viene invocata ogni 15 minuti da EventBridge Scheduler. Per ogni trip in stato `scheduled` o `tentative_match` con `flightTime` nelle prossime 12 ore:

1. Interroga l'API di flight tracking (AviationEdge o FlightAware)
2. Confronta `eta_actual` con il `flightTime` salvato in DynamoDB
3. Se il delta è > 10 minuti: aggiorna `flightTime`, ricalcola `timeBucket`, aggiorna `gsi1pk` e `gsi5pk`
4. Se il trip era già in `tentative_match` e il ritardo rompe la compatibilità temporale con il partner → emetti `match.invalidated` su EventBridge

```python
# src/handlers/flights/flight_tracker.py

POLL_INTERVAL_MIN = 15
TRACKING_WINDOW_HOURS = 12   # monitora voli nelle prossime 12 ore
MIN_DELTA_MIN = 10           # soglia minima per aggiornare il flightTime

def handler(event, context):
    for airport in get_active_airports():
        now = datetime.now(timezone.utc)
        # Query GSI5: trip scheduled + tentative_match nelle prossime 12h
        trips = query_trips_in_tracking_window(airport.code, now, TRACKING_WINDOW_HOURS)
        
        for trip in trips:
            update_flight_eta(trip, airport)

def update_flight_eta(trip: dict, airport: AirportConfig):
    try:
        eta = fetch_flight_eta(trip["flightNumber"], trip["flightDate"])
    except FlightTrackerError:
        logger.warning("flight_tracker_unavailable", tripId=trip["pk"])
        return  # fail silently, riprova al prossimo ciclo

    current_dt = datetime.fromisoformat(trip["flightTime"].replace("Z", "+00:00"))
    delta_min = abs((eta - current_dt).total_seconds()) / 60

    if delta_min < MIN_DELTA_MIN:
        return  # delta trascurabile, nessun aggiornamento

    new_bucket = get_time_bucket(eta.isoformat())
    
    # Aggiornamento atomico in DynamoDB
    table.update_item(
        Key={"pk": trip["pk"], "sk": "META"},
        UpdateExpression="SET flightTime = :ft, timeBucket = :tb, gsi1pk = :gsi1, updatedAt = :ua",
        ExpressionAttributeValues={
            ":ft": eta.isoformat(),
            ":tb": new_bucket,
            ":gsi1": f"{trip['airportCode']}#{new_bucket}",
            ":ua": now_iso(),
        },
    )

    # Se il trip era già in match temporaneo, controlla se il ritardo rompe la compatibilità
    if trip.get("status") == "tentative_match":
        put_event("flight.delayed", {
            "tripId": trip["pk"].replace("TRIP#", ""),
            "matchId": trip.get("tentativeMatchId"),
            "oldFlightTime": trip["flightTime"],
            "newFlightTime": eta.isoformat(),
            "deltaMinutes": round(delta_min, 1),
        })
    
    logger.info("flight_eta_updated", tripId=trip["pk"], delta_min=round(delta_min, 1))
```

#### Nuovo evento: `flight.delayed`

```python
# src/handlers/events/on_flight_delayed.py
# EventBridge: flight.delayed → rivaluta match temporanei

def handler(event, context):
    detail = event["detail"]
    match_id = detail.get("matchId")
    
    if not match_id:
        return  # il trip non aveva ancora un match
    
    match = get_match(match_id)
    if match["status"] != "tentative_match":
        return  # il match è già confermato o cancellato
    
    # Controlla se i due trip sono ancora temporalmente compatibili
    trip_a = get_trip(match["tripId1"])
    trip_b = get_trip(match["tripId2"])
    
    airport = get_airport(match["airportCode"])
    time_compatible = check_time_compatibility(trip_a, trip_b, airport, mode="scheduled")
    
    if not time_compatible:
        # Invalida il match e rimette entrambi i trip nel pool
        invalidate_tentative_match(match_id, trip_a, trip_b)
        put_event("match.invalidated", {"matchId": match_id, "reason": "flight_delay"})
        logger.info("match_invalidated_by_delay", matchId=match_id)
    else:
        # Il ritardo non rompe la compatibilità — aggiorna solo i dati del match
        logger.info("match_still_valid_after_delay", matchId=match_id)
```

---

### 2. Shadow Pools e Matching Differito (Deferred Matching)

La differenza chiave dalla v3: il `MatchmakerFunction` non crea più match definitivi immediatamente. Crea **Tentative Match** (match ombra) che vengono continuamente rivisti fino a `T-3 ore` dall'atterraggio. Solo allo scadere del countdown il match diventa definitivo e partono le notifiche.

#### Nuovo stato del Trip lifecycle

```
SCHEDULED:
  status: "scheduled"
    ↓ Matchmaker trova candidato compatibile
  status: "tentative_match" + tentativeMatchId     ← NUOVO v4
    ↓ continua ricalcolo ogni ciclo Matchmaker
    ↓ a T-3h dall'atterraggio aggiornato
  status: "matched"                                ← solo ora partono le notifiche
    ↓ se flight.delayed rompe la compatibilità
  status: "scheduled"                              ← torna nel pool

LOCK WINDOW (T-3h):
  Dopo il lock, il Matchmaker non ricalcola più questo trip.
  Se arriva un ritardo che rompe il match → notifica utente dell'annullamento.
```

#### Tentative Match — entity DynamoDB

```python
# Nuova entity: TENTATIVE_MATCH
# PK: TENTATIVE_MATCH#<matchId>
# SK: META

item = {
    "pk": f"TENTATIVE_MATCH#{match_id}",
    "sk": "META",
    "tripId1": trip_a["tripId"],
    "tripId2": trip_b["tripId"],
    "userId1": trip_a["userId"],
    "userId2": trip_b["userId"],
    "airportCode": airport.code,
    "score": round(score, 3),
    "distKm": round(dist_km, 2),
    "detourMinutes": round(detour_min, 1),   # NUOVO v4
    "status": "tentative_match",
    "lockAt": lock_at.isoformat(),           # T-3h dall'atterraggio di entrambi
    "expiresAt": expires_at.isoformat(),
    "createdAt": now_iso(),
}
```

#### MatchmakerFunction v4 — pseudocodice

```python
# src/handlers/matching/matchmaker.py

LOCK_HOURS_BEFORE = 3        # soglia di lock del match
BUCKET_MINUTES = 10

def handler(event, context):
    now = datetime.now(timezone.utc)
    
    for airport in get_active_airports():
        # Step 1: Processa lock — converti tentative_match in matched se T-3h è scaduto
        process_lock_window(airport, now)
        
        # Step 2: Aggiorna e invalida tentative_match rotti da ritardi
        # (gestito da on_flight_delayed — qui solo cleanup)
        
        # Step 3: Ottimizzazione globale — rematch dei trip in pool
        optimize_pool(airport, now)

def process_lock_window(airport: AirportConfig, now: datetime):
    """
    Trova tutti i TENTATIVE_MATCH con lockAt < now.
    Li converte in match definitivi e notifica gli utenti.
    """
    tentative = query_tentative_matches_to_lock(airport.code, now)
    
    for tm in tentative:
        # Verifica che entrambi i trip siano ancora in stato tentative_match
        trip_a = get_trip(tm["tripId1"])
        trip_b = get_trip(tm["tripId2"])
        
        if trip_a["status"] != "tentative_match" or trip_b["status"] != "tentative_match":
            continue  # uno dei due è stato ri-accoppiato nel frattempo
        
        # Crea il Match definitivo
        match_id = create_definitive_match(tm, trip_a, trip_b, airport)
        
        # Notifica entrambi gli utenti
        put_event("match.found", {
            "matchId": match_id,
            "tripId1": tm["tripId1"],
            "tripId2": tm["tripId2"],
            "score": tm["score"],
            "savings": airport.base_fare // 2 / 100,
        })
        
        metrics.add_metric(name="MatchesLocked", unit=MetricUnit.Count, value=1)


def optimize_pool(airport: AirportConfig, now: datetime):
    """
    Algoritmo di ottimizzazione globale dei trip nel pool.
    Non accoppierà mai trip che hanno già un tentative_match dentro la lock window.
    """
    # Query GSI5: trip scheduled + tentative_match (non ancora in lock window)
    pool = query_active_pool(airport.code, now, lock_buffer_hours=LOCK_HOURS_BEFORE)
    
    if len(pool) < 2:
        return  # niente da ottimizzare
    
    # Costruisci matrice di compatibilità
    compatibility_matrix = build_compatibility_matrix(pool, airport, now)
    
    # Algoritmo greedy pesato con shuffling:
    # Ordina le coppie per score decrescente, accoppiale senza ripetizioni
    # Questo garantisce che le coppie migliori abbiano la priorità
    assignments = find_optimal_assignments(compatibility_matrix)
    
    for trip_a_id, trip_b_id, score, dist_km, detour_min in assignments:
        trip_a = next(t for t in pool if t["tripId"] == trip_a_id)
        trip_b = next(t for t in pool if t["tripId"] == trip_b_id)
        
        # Controlla se già accoppiati insieme in un tentative_match esistente
        existing_tm = get_tentative_match_between(trip_a_id, trip_b_id)
        
        if existing_tm:
            # Aggiorna lo score se migliorato (es. dopo aggiornamento ETA)
            if score > existing_tm["score"] + 0.05:
                update_tentative_match_score(existing_tm["pk"], score)
            continue
        
        # Se uno dei due aveva un tentative_match diverso, rimuovilo
        # (trovato un abbinamento migliore)
        for trip in [trip_a, trip_b]:
            if trip.get("tentativeMatchId"):
                old_tm_id = trip["tentativeMatchId"]
                dissolve_tentative_match(old_tm_id)
        
        # Calcola lockAt: T-3h del volo che atterra per PRIMO tra i due
        earliest_flight = min(
            datetime.fromisoformat(trip_a["flightTime"].replace("Z", "+00:00")),
            datetime.fromisoformat(trip_b["flightTime"].replace("Z", "+00:00")),
        )
        lock_at = earliest_flight - timedelta(hours=LOCK_HOURS_BEFORE)
        
        # Non creare tentative_match se siamo già oltre il lock
        if lock_at <= datetime.now(timezone.utc):
            # Vai direttamente al match definitivo
            create_definitive_match_direct(trip_a, trip_b, score, dist_km, detour_min, airport)
            continue
        
        # Crea Tentative Match
        create_tentative_match(trip_a, trip_b, score, dist_km, detour_min, lock_at, airport)
        
        metrics.add_metric(name="TentativeMatchesCreated", unit=MetricUnit.Count, value=1)


def build_compatibility_matrix(
    pool: list[dict],
    airport: AirportConfig,
    now: datetime,
) -> list[tuple]:
    """
    Costruisce lista di coppie compatibili con relativo score.
    Restituisce list[(tripId_a, tripId_b, score, dist_km, detour_min)]
    ordinata per score decrescente.
    """
    pairs = []
    
    for i, trip_a in enumerate(pool):
        for j, trip_b in enumerate(pool):
            if j <= i:
                continue  # evita duplicati e auto-match
            
            if not can_match_direction(trip_a, trip_b):
                continue
            
            if "destLat" not in trip_a or "destLat" not in trip_b:
                continue
            
            # Soglia dinamica: più è lontano il volo, più è selettiva
            dynamic_threshold = compute_dynamic_threshold(
                airport.match_threshold,
                trip_a["flightTime"],
                trip_b["flightTime"],
                now,
            )
            
            dist_km = haversine_km(
                trip_a["destLat"], trip_a["destLng"],
                trip_b["destLat"], trip_b["destLng"],
            )
            
            # Calcola deviazione corridoio (penalizza rotte a V)
            detour_min = estimate_detour_minutes(trip_a, trip_b, airport)
            
            if detour_min > airport.max_detour_minutes:
                continue  # rotta troppo inefficiente per il tassista
            
            user_a = get_user(trip_a["userId"])
            user_b = get_user(trip_b["userId"])
            score = compute_match_score(trip_a, trip_b, user_a, user_b, mode="scheduled")
            score = apply_detour_penalty(score, detour_min, airport.max_detour_minutes)
            
            if score >= dynamic_threshold:
                pairs.append((trip_a["tripId"], trip_b["tripId"], score, dist_km, detour_min))
    
    pairs.sort(key=lambda x: x[2], reverse=True)
    return pairs


def find_optimal_assignments(pairs: list[tuple]) -> list[tuple]:
    """
    Greedy assignment: prende la coppia con score più alto, esclude i trip già accoppiati.
    È O(n²) — sufficiente per MVP con pool piccoli (<100 trip).
    """
    assigned = set()
    assignments = []
    
    for trip_a_id, trip_b_id, score, dist_km, detour_min in pairs:
        if trip_a_id in assigned or trip_b_id in assigned:
            continue
        assignments.append((trip_a_id, trip_b_id, score, dist_km, detour_min))
        assigned.add(trip_a_id)
        assigned.add(trip_b_id)
    
    return assignments
```

---

### 3. Soglie Dinamiche e Corridoi Direzionali

#### Soglia Dinamica (Time-Decay)

La `match_threshold` non è più una costante: decresce progressivamente man mano che il volo si avvicina. Questo crea un sistema naturale di "rilassamento dei criteri" quando la finestra si chiude.

```python
def compute_dynamic_threshold(
    base_threshold: float,
    flight_time_a: str,
    flight_time_b: str,
    now: datetime,
) -> float:
    """
    Soglia dinamica basata sul tempo al volo più imminente.
    
    Curva:
    - 7 giorni prima → 0.70 (molto selettivo — aspetta combinazioni migliori)
    - 48 ore prima   → 0.50
    - 24 ore prima   → 0.35
    - 6 ore prima    → 0.25 (base_threshold — accetta tutto ciò che è compatibile)
    - <3 ore prima   → 0.20 (extra-rilassato — lock window imminente)
    """
    dt_a = datetime.fromisoformat(flight_time_a.replace("Z", "+00:00"))
    dt_b = datetime.fromisoformat(flight_time_b.replace("Z", "+00:00"))
    hours_to_flight = min(
        (dt_a - now).total_seconds() / 3600,
        (dt_b - now).total_seconds() / 3600,
    )
    hours_to_flight = max(0, hours_to_flight)
    
    if hours_to_flight >= 168:   return 0.70   # 7 giorni
    if hours_to_flight >= 48:    return 0.50
    if hours_to_flight >= 24:    return 0.35
    if hours_to_flight >= 6:     return base_threshold  # tipicamente 0.25
    return max(0.20, base_threshold - 0.05)
```

#### Corridoio Direzionale (Detour Penalty)

Al posto del limite radiale di 20 km, il sistema calcola la **deviazione in minuti** che il tassista deve fare per portare entrambi i passeggeri a destinazione nell'ordine ottimale.

```python
def estimate_detour_minutes(
    trip_a: dict,
    trip_b: dict,
    airport: AirportConfig,
) -> float:
    """
    Stima la deviazione in minuti per servire entrambe le destinazioni.
    
    Logica MVP (senza routing API per contenere i costi):
    - Calcola distanza diretta A→B e B→A
    - Stima speed media urbana a 30 km/h
    - Penalizza angolo tra le rotte: se le destinazioni sono in direzioni opposte
      rispetto all'aeroporto, la deviazione è alta
    
    Nota: in v4.1 sostituire con Google Routes API per detour reale.
    """
    airport_lat = airport.zones[0].lat  # approssimazione: usa centro aeroporto
    airport_lng = airport.zones[0].lng
    
    # Distanza aeroporto → A (rotta diretta)
    d_airport_to_a = haversine_km(airport_lat, airport_lng, trip_a["destLat"], trip_a["destLng"])
    d_airport_to_b = haversine_km(airport_lat, airport_lng, trip_b["destLat"], trip_b["destLng"])
    d_a_to_b = haversine_km(trip_a["destLat"], trip_a["destLng"], trip_b["destLat"], trip_b["destLng"])
    
    # Opzione 1: aeroporto → A → B
    route_ab = d_airport_to_a + d_a_to_b
    # Opzione 2: aeroporto → B → A
    route_ba = d_airport_to_b + d_a_to_b
    # Rotta diretta: aeroporto → destinazione più lontana
    direct = max(d_airport_to_a, d_airport_to_b)
    
    optimal_route = min(route_ab, route_ba)
    detour_km = optimal_route - direct
    
    # Converti in minuti (velocità media urbana 30 km/h)
    URBAN_SPEED_KMH = 30
    return (detour_km / URBAN_SPEED_KMH) * 60


def apply_detour_penalty(score: float, detour_min: float, max_detour_min: int) -> float:
    """
    Penalizza lo score in base alla deviazione.
    
    - 0–5 min di deviazione: nessuna penalità
    - 5–15 min: penalità lineare fino a -0.2
    - >15 min: penalità fissa -0.3 (route a V — quasi impossibile matchare)
    """
    if detour_min <= 5:
        return score
    elif detour_min <= 15:
        penalty = 0.2 * ((detour_min - 5) / 10)
        return max(0, score - penalty)
    else:
        return max(0, score - 0.3)
```

#### Nuovo campo `max_detour_minutes` in `AirportConfig`

```python
@dataclass
class AirportConfig:
    # ... campi esistenti v3 ...
    max_detour_minutes: int       # NUOVO v4 — deviazione massima accettabile per il tassista
    flight_tracker_provider: str  # NUOVO v4 — "aviation_edge" | "flightaware" | "mock"

# MXP:
# max_detour_minutes = 15
# flight_tracker_provider = "aviation_edge"
```

---

## DynamoDB — Schema v4

### Entità Trip — campi aggiornati

```python
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
    "flightNumber": body.flightNumber,           # NUOVO v4
    "flightDate": body.flightDate,               # NUOVO v4
    "flightTime": resolved_flight_time,          # risolto dal tracker, non dal form
    "timeBucket": get_time_bucket(resolved_flight_time),
    "gsi1pk": f"{body.airportCode}#{get_time_bucket(resolved_flight_time)}",
    "mode": mode,
    "paxCount": body.paxCount,
    "luggage": body.luggage,
    "status": status,
    "tentativeMatchId": None,                    # NUOVO v4 — ID del match ombra corrente
    "gsi5pk": f"{body.airportCode}#{status}",
    "expiresAt": expires_at.isoformat(),
    "flightEtaUpdatedAt": now_iso(),             # NUOVO v4 — ultimo aggiornamento ETA
    "createdAt": now_iso(),
}
```

### Entità TentativeMatch — NUOVA v4

```python
item = {
    "pk": f"TENTATIVE_MATCH#{match_id}",
    "sk": "META",
    "tripId1": trip_a["tripId"],
    "tripId2": trip_b["tripId"],
    "userId1": trip_a["userId"],
    "userId2": trip_b["userId"],
    "airportCode": airport.code,
    "score": round(score, 3),
    "distKm": round(dist_km, 2),
    "detourMinutes": round(detour_min, 1),
    "status": "tentative_match",
    "lockAt": lock_at.isoformat(),              # T-3h — quando diventa definitivo
    "expiresAt": expires_at.isoformat(),
    "createdAt": now_iso(),
    # GSI per trovare match da confermare
    "gsi6pk": f"{airport.code}#tentative",      # NUOVO GSI6
    "lockAt": lock_at.isoformat(),              # GSI6 SK — ordinato per lock window
}
```

### GSI aggiornati

| GSI | PK | SK | Purpose |
|-----|----|----|---------|
| GSI1-TimeBucket | `airportCode#timeBucket` | `createdAt` | Matching query scoped per bucket temporale |
| GSI2-UserTrips | `userId` | `createdAt` | Dashboard "My Trips" |
| GSI3-UserConn | `userId` | `connId` | WebSocket connection |
| GSI4-StripeIntent | `stripePaymentIntentId` | — | Webhook Stripe |
| GSI5-TripStatus | `airportCode#status` | `flightTime` | Matchmaker: trip scheduled ordinati per ora arrivo |
| **GSI6-TentativeMatch** | `airportCode#tentative` | `lockAt` | **NUOVO v4** — Process lock window: trova i TentativeMatch ordinati per lockAt |

---

## EventBridge — Flusso eventi v4

### Nuovi eventi

```
Bus: flot-events

# v4 — Flight Tracking
flight.eta_resolved       → Trip creato: ETA volo recuperata dal tracker
flight.delayed            → Volo ritardato: aggiorna ETA e rivaluta match ombra
flight.advanced           → Volo in anticipo: stessa logica di delayed
flight.tracking_failed    → API tracker non disponibile: usa fallback

# v4 — Shadow Pool
match.tentative_created   → Creato un tentative_match: aggiorna stato trip
match.tentative_dissolved → Tentative match rotto (delay o match migliore trovato)
match.locked              → Lock window scaduta: il match diventa definitivo
match.invalidated         → Match definitivo annullato per ritardo eccessivo

# invariati da v3
match.found               → Match definitivo notificato agli utenti
payment.completed         → Unlock match + enable chat
trip.expired              → Trip scaduto senza match
```

### Routing Lambda per evento

| Evento | Lambda handler |
|--------|---------------|
| `flight.delayed` | `on_flight_delayed.py` — rivaluta compatibilità tentative_match |
| `flight.advanced` | `on_flight_delayed.py` — stessa logica, direzione opposta |
| `match.tentative_created` | `on_tentative_match_created.py` — aggiorna status trip, NO notifica utente |
| `match.tentative_dissolved` | `on_tentative_dissolved.py` — rimette trip in pool |
| `match.locked` | `on_match_locked.py` — crea Match definitivo + emette match.found |
| `match.invalidated` | `on_match_invalidated.py` — notifica utente annullamento |

---

## SAM Template — Nuove risorse v4

```yaml
# Aggiunte al template.yaml esistente

Resources:

  # NUOVO v4 — Flight Tracker Lambda (polling ogni 15 min)
  FlightTrackerFunction:
    Type: AWS::Serverless::Function
    Properties:
      Handler: src/handlers/flights/flight_tracker.handler
      Runtime: python3.12
      MemorySize: 256
      Timeout: 30
      Environment:
        Variables:
          FLIGHT_TRACKER_API_KEY: !Sub "{{resolve:ssm:/flot/${Stage}/flight-tracker-key}}"
          FLIGHT_TRACKER_PROVIDER: aviation_edge
      Events:
        ScheduledRule:
          Type: ScheduleV2
          Properties:
            ScheduleExpression: "rate(15 minutes)"
            State: ENABLED
      Policies:
        - DynamoDBCrudPolicy:
            TableName: !Ref FlotTable
        - EventBridgePutEventsPolicy:
            EventBusName: !Ref EventBus

  # NUOVO v4 — Matchmaker aggiornato con Shadow Pool
  # (sostituisce il MatchmakerFunction v3)
  MatchmakerFunction:
    Type: AWS::Serverless::Function
    Properties:
      Handler: src/handlers/matching/matchmaker.handler
      Runtime: python3.12
      MemorySize: 512
      Timeout: 60
      Events:
        ScheduledRule:
          Type: ScheduleV2
          Properties:
            ScheduleExpression: "rate(5 minutes)"
            State: ENABLED

  # NUOVO v4 — Event handlers per flight tracking e shadow pool
  OnFlightDelayedFunction:
    Type: AWS::Serverless::Function
    Properties:
      Handler: src/handlers/events/on_flight_delayed.handler
      Events:
        EventBridgeRule:
          Type: EventBridgeRule
          Properties:
            EventBusName: !Ref EventBus
            Pattern:
              detail-type: ["flight.delayed", "flight.advanced"]

  OnMatchLockedFunction:
    Type: AWS::Serverless::Function
    Properties:
      Handler: src/handlers/events/on_match_locked.handler
      Events:
        EventBridgeRule:
          Type: EventBridgeRule
          Properties:
            EventBusName: !Ref EventBus
            Pattern:
              detail-type: ["match.locked"]

  OnMatchInvalidatedFunction:
    Type: AWS::Serverless::Function
    Properties:
      Handler: src/handlers/events/on_match_invalidated.handler
      Events:
        EventBridgeRule:
          Type: EventBridgeRule
          Properties:
            EventBusName: !Ref EventBus
            Pattern:
              detail-type: ["match.invalidated"]

  # NUOVO v4 — GSI6 aggiunto alla tabella DynamoDB
  FlotTable:
    Type: AWS::DynamoDB::Table
    Properties:
      TableName: !Sub "Flot-${Stage}"
      BillingMode: PAY_PER_REQUEST
      AttributeDefinitions:
        - AttributeName: pk
          AttributeType: S
        - AttributeName: sk
          AttributeType: S
        - AttributeName: gsi1pk
          AttributeType: S
        - AttributeName: gsi2pk
          AttributeType: S
        - AttributeName: gsi5pk
          AttributeType: S
        - AttributeName: gsi6pk
          AttributeType: S   # NUOVO v4
        - AttributeName: flightTime
          AttributeType: S
        - AttributeName: createdAt
          AttributeType: S
        - AttributeName: lockAt
          AttributeType: S   # NUOVO v4
      KeySchema:
        - AttributeName: pk
          KeyType: HASH
        - AttributeName: sk
          KeyType: RANGE
      GlobalSecondaryIndexes:
        # GSI1–GSI5: invariati dalla v3
        - IndexName: GSI6-TentativeMatch
          KeySchema:
            - AttributeName: gsi6pk
              KeyType: HASH
            - AttributeName: lockAt
              KeyType: RANGE
          Projection:
            ProjectionType: ALL
      TimeToLiveSpecification:
        AttributeName: ttl
        Enabled: true
```

---

## File Structure — v4

```
flot-backend/
├── src/
│   ├── handlers/
│   │   ├── flights/
│   │   │   └── flight_tracker.py           # NUOVO v4 — polling ETA voli
│   │   ├── matching/
│   │   │   └── matchmaker.py               # AGGIORNATO v4 — shadow pool + lock window
│   │   ├── events/
│   │   │   ├── on_match_found.py           # invariato v3
│   │   │   ├── on_flight_delayed.py        # NUOVO v4
│   │   │   ├── on_match_locked.py          # NUOVO v4
│   │   │   ├── on_match_invalidated.py     # NUOVO v4
│   │   │   ├── on_tentative_created.py     # NUOVO v4
│   │   │   └── on_tentative_dissolved.py   # NUOVO v4
│   │   └── trips/
│   │       └── create_trip.py              # AGGIORNATO v4 — risolve ETA da flightNumber
│   ├── lib/
│   │   ├── airports.py                     # AGGIORNATO v4 — max_detour_minutes, flight_tracker_provider
│   │   ├── matching.py                     # AGGIORNATO v4 — dynamic threshold, detour penalty
│   │   ├── flight_tracker.py               # NUOVO v4 — client API aviation tracking
│   │   └── validation.py                   # AGGIORNATO v4 — flightNumber obbligatorio
├── tests/
│   ├── unit/
│   │   ├── test_matching.py                # AGGIORNATO v4 — test detour e dynamic threshold
│   │   ├── test_matchmaker.py              # AGGIORNATO v4 — test shadow pool
│   │   └── test_flight_tracker.py          # NUOVO v4
```

---

## Test Unitari — Nuovi casi v4

```python
# tests/unit/test_matching.py — nuovi test v4

# Test: soglia dinamica per distanza temporale
def test_dynamic_threshold_7days():
    now = datetime(2026, 4, 24, 10, 0, tzinfo=timezone.utc)
    flight_time = (now + timedelta(days=7)).isoformat()
    threshold = compute_dynamic_threshold(0.25, flight_time, flight_time, now)
    assert threshold == 0.70

def test_dynamic_threshold_1hour():
    now = datetime(2026, 4, 24, 10, 0, tzinfo=timezone.utc)
    flight_time = (now + timedelta(hours=1)).isoformat()
    threshold = compute_dynamic_threshold(0.25, flight_time, flight_time, now)
    assert threshold <= 0.25

# Test: detour minutes — rotta lineare (nessuna deviazione)
def test_detour_linear_route():
    # Aeroporto → A → B dove B è sulla stessa direttrice di A
    trip_a = {"destLat": 45.47, "destLng": 9.19}
    trip_b = {"destLat": 45.46, "destLng": 9.18}
    airport = mock_mxp_airport()
    detour = estimate_detour_minutes(trip_a, trip_b, airport)
    assert detour < 5  # deviazione minima

# Test: detour penalty — rotta a V
def test_detour_penalty_v_route():
    score = 0.80
    penalized = apply_detour_penalty(score, detour_min=20, max_detour_min=15)
    assert penalized < 0.50  # penalità significativa

# Test: shadow pool — match migliore sostituisce quello peggiore
def test_shadow_pool_rematch():
    pool = [trip_a, trip_b, trip_c]  # trip_b e trip_c compatibili meglio di a-b
    assignments = find_optimal_assignments(build_compatibility_matrix(pool, mock_airport(), now))
    # trip_b deve essere accoppiato con trip_c, non con trip_a
    assigned_pairs = [(a, b) for a, b, *_ in assignments]
    assert ("trip_b", "trip_c") in assigned_pairs or ("trip_c", "trip_b") in assigned_pairs
```

---

## Environment Variables — v4

```env
# Nuove env var per v4:
FLIGHT_TRACKER_PROVIDER=aviation_edge          # "aviation_edge" | "flightaware" | "mock"
FLIGHT_TRACKER_API_KEY=<from SSM>              # API key recuperata da SSM al deploy
LOCK_HOURS_BEFORE=3                            # Ore prima del volo per il lock del match
TRACKING_WINDOW_HOURS=12                       # Finestra di monitoraggio ETA voli
```

---

## Regole da rispettare — v4 specifiche

- **`flightNumber` risolve `flightTime`** — non accettare mai un `flightTime` inserito manualmente dall'utente senza validazione del tracker (fallback consentito solo se l'API non risponde).
- **TentativeMatch è silenzioso** — la creazione e la dissoluzione di un tentative_match non genera mai notifiche all'utente. Solo `match.locked` genera notifiche.
- **Il Matchmaker è idempotente** — due esecuzioni sullo stesso pool producono gli stessi assignment. Usa transact_write con condition expression per evitare race conditions.
- **La soglia dinamica è sempre letta da `compute_dynamic_threshold`** — mai hardcoded in nessun handler.
- **`max_detour_minutes` è un parametro di `AirportConfig`** — diverso per ogni aeroporto, mai hardcoded fuori da `airports.py`.
- **Il flight tracker usa un circuit breaker** — se l'API fallisce per 3 chiamate consecutive, smetti di chiamarla per 30 minuti e usa il `flightTime` statico come fallback.
- **Trip con `tracking_pending`** (ETA non risolta) sono esclusi dal matching finché non hanno un `flightTime` valido.
- **Float nei log** — arrotondati a 2 decimali (`dist_km`, `detour_min`, `score`).

---

## Domande che devi rispondermi prima di iniziare

Dimmi il piano in massimo 6 step. Per ogni step:
1. Qual è il componente che tocchi?
2. Quale file crei o modifichi?
3. Quale test unitario valida lo step?

Non scrivere codice finché non ti confermo il piano.

---

## Sprint Plan — v4 Development

> Stato verificato al 2026-05-03 analizzando il codebase reale. Nessuna feature v4 trovata in `src/` → tutti gli sprint sono **TODO**.

---

### Sprint 1 — Foundation: TripCreate v4 + Flight Tracker Client `[ done ]`

**Goal:** `flightNumber` + `flightDate` obbligatori, `flightTime` auto-risolto da API tracker al momento della `POST /trips`.

| # | Componente | File | Test |
|---|-----------|------|------|
| 1.1 | Model validazione | `src/lib/validation.py` — aggiungi `flightNumber` (obbligatorio), `flightDate` (obbligatorio), `flightTime` (opzionale) a `TripCreate` | `tests/unit/test_validation.py` — trip senza `flightNumber` ritorna 422 |
| 1.2 | Flight Tracker client | `src/lib/flight_tracker.py` — `fetch_flight_eta(flightNumber, flightDate)` con timeout 2s, circuit breaker 3 fail → 30 min blackout | `tests/unit/test_flight_tracker.py` — mock API timeout → fallback ETA, circuit breaker si attiva |
| 1.3 | Create trip handler | `src/handlers/trips/create_trip.py` — chiama `fetch_flight_eta`, risolve `flightTime`, salva `flightEtaUpdatedAt`, stato `tracking_pending` se fallback | `tests/unit/test_create_trip.py` — `flightTime` nel DB = ETA dal tracker, non dal body |
| 1.4 | AirportConfig | `src/lib/airports.py` — aggiungi `max_detour_minutes`, `flight_tracker_provider` a `AirportConfig` | `tests/unit/test_airports.py` — MXP ha `max_detour_minutes=15` |

**Done when:** `POST /trips` con `flightNumber` persiste ETA reale; senza `flightNumber` ritorna 422.

---

### Sprint 2 — Matching Core: Soglie Dinamiche + Corridoio Direzionale `[ done ]`

**Goal:** Sostituire soglia fissa e raggio radiale con `compute_dynamic_threshold` + `estimate_detour_minutes`.

| # | Componente | File | Test |
|---|-----------|------|------|
| 2.1 | Dynamic threshold | `src/lib/matching.py` — `compute_dynamic_threshold(base, flight_time_a, flight_time_b, now)` | `test_dynamic_threshold_7days` → 0.70; `test_dynamic_threshold_1hour` → ≤0.25 |
| 2.2 | Detour estimate | `src/lib/matching.py` — `estimate_detour_minutes(trip_a, trip_b, airport)` haversine MVP senza routing API | `test_detour_linear_route` → <5 min; `test_detour_penalty_v_route` → score <0.50 |
| 2.3 | Detour penalty | `src/lib/matching.py` — `apply_detour_penalty(score, detour_min, max_detour_min)` | `test_detour_penalty_v_route` — score penalizzato correttamente |
| 2.4 | Build compat matrix | `src/handlers/matching/matchmaker.py` — `build_compatibility_matrix` usa dynamic threshold + detour filter | `tests/unit/test_matchmaker.py` — coppia con detour>max esclusa dalla matrix |

**Done when:** Il matchmaker non produce più match con deviazione > `max_detour_minutes`; la soglia varia per ore al volo.

---

### Sprint 3 — DynamoDB: Schema v4 + GSI6 `[ TODO ]`

**Goal:** Entità `TentativeMatch` + GSI6 + nuovi campi Trip pronti per shadow pool.

| # | Componente | File | Test |
|---|-----------|------|------|
| 3.1 | SAM template | `template.yaml` — aggiungi `gsi6pk` (AttributeDefinition), `lockAt` (SK), GSI6-TentativeMatch; aggiungi `tentativeMatchId` ai campi Trip | Deploy staging e verificare table describe |
| 3.2 | DynamoDB helpers | `src/lib/dynamo.py` — `create_tentative_match`, `dissolve_tentative_match`, `query_tentative_matches_to_lock`, `get_tentative_match_between` | `tests/unit/test_dynamo.py` — CRUD tentative match con mock DynamoDB |
| 3.3 | Trip status enum | `src/lib/validation.py` (o `models.py`) — aggiungi stato `tentative_match`, `tracking_pending` | Unit test status transition |

**Done when:** `sam deploy` staging senza errori schema; `create_tentative_match` persiste con GSI6 corretto.

---

### Sprint 4 — Shadow Pool: Matchmaker v4 `[ TODO ]`

**Goal:** Matchmaker crea TentativeMatch (non match definitivi); `process_lock_window` converte a definitivi a T-3h.

| # | Componente | File | Test |
|---|-----------|------|------|
| 4.1 | optimize_pool | `src/handlers/matching/matchmaker.py` — `optimize_pool`: usa `build_compatibility_matrix` + `find_optimal_assignments`; gestisce dissolve/replace tentative match | `test_shadow_pool_rematch` — trip_b si accoppia con trip_c (score maggiore) non con trip_a |
| 4.2 | process_lock_window | `src/handlers/matching/matchmaker.py` — `process_lock_window`: query GSI6 con `lockAt < now`, crea match definitivo, emette `match.found` | `tests/unit/test_matchmaker.py` — TentativeMatch con lockAt passato → match definitivo |
| 4.3 | Idempotenza | `src/handlers/matching/matchmaker.py` — `transact_write` con condition expression su stato trip | Test: due esecuzioni parallele non duplicano match |

**Done when:** Matchmaker su pool di test crea TentativeMatch; run successivo a T-3h li promuove a match definitivi e notifica utenti.

---

### Sprint 5 — Flight Tracker Lambda + Event Handlers `[ TODO ]`

**Goal:** Polling ETA ogni 15 min; eventi `flight.delayed` → invalida TentativeMatch incompatibili.

| # | Componente | File | Test |
|---|-----------|------|------|
| 5.1 | FlightTrackerFunction | `src/handlers/flights/flight_tracker.py` — handler EventBridge Scheduler, query GSI5 finestra 12h, chiama `fetch_flight_eta`, aggiorna `flightTime`+`gsi1pk`, emette `flight.delayed` | `tests/unit/test_flight_tracker.py` — delta >10 min → aggiornamento DynamoDB + evento emesso |
| 5.2 | on_flight_delayed | `src/handlers/events/on_flight_delayed.py` — rivaluta compatibilità temporale; se rotta → `invalidate_tentative_match` + `match.invalidated` | Test: ritardo 90 min su trip con finestra ±30 → match invalidato |
| 5.3 | on_match_invalidated | `src/handlers/events/on_match_invalidated.py` — notifica utente annullamento match definitivo | Test: push notification inviata a entrambi userId |
| 5.4 | SAM template | `template.yaml` — aggiungi `FlightTrackerFunction` (ScheduleV2 15 min), `OnFlightDelayedFunction`, `OnMatchLockedFunction`, `OnMatchInvalidatedFunction` con EventBridge rules | Integration test: evento `flight.delayed` → Lambda invocata |

**Done when:** Volo ritardato di 30+ min su TentativeMatch → match dissolto e trip tornano in pool.

---

### Sprint 6 — Hardening, Osservabilità, Deploy MVP `[ TODO ]`

**Goal:** Circuit breaker flight tracker robusto; logging strutturato; deploy MXP production.

| # | Componente | File | Test |
|---|-----------|------|------|
| 6.1 | Circuit breaker | `src/lib/flight_tracker.py` — stato in-memory (o SSM) con contatore fail, blackout 30 min dopo 3 fail consecutivi | Test: 3 API timeout → circuit aperto → 4a chiamata skippata senza API call |
| 6.2 | Logging | tutti gli handler v4 — `dist_km`, `detour_min`, `score` arrotondati a 2 decimali nei log strutturati | Code review manuale |
| 6.3 | Env vars | `template.yaml` + `.env.example` — `FLIGHT_TRACKER_PROVIDER`, `FLIGHT_TRACKER_API_KEY` (SSM), `LOCK_HOURS_BEFORE=3`, `TRACKING_WINDOW_HOURS=12` | Deploy staging + smoke test `POST /trips` con volo reale AZ1234 |
| 6.4 | Integration test E2E | `tests/integration/` — crea trip → verifica ETA risolta → matchmaker → TentativeMatch → lock → notifica | Pipeline CI completa verde |

**Done when:** Deploy production MXP; `POST /trips` con volo reale risolve ETA; TentativeMatch → lock → notifica in <5 min dopo T-3h.

---

### Riepilogo Sprint

| Sprint | Contenuto | Stato |
|--------|-----------|-------|
| S1 | TripCreate v4 + Flight Tracker client | `[ done ]` |
| S2 | Dynamic threshold + Detour penalty | `[ done ]` |
| S3 | DynamoDB schema v4 + GSI6 | `[ TODO ]` |
| S4 | Shadow Pool + Matchmaker v4 | `[ TODO ]` |
| S5 | FlightTrackerFunction + Event handlers | `[ TODO ]` |
| S6 | Hardening + Deploy MVP | `[ TODO ]` |

---

*Flot Backend v4 — Elastic & Predictive — Aprile 2026*
