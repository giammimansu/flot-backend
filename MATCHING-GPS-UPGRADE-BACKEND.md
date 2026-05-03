# Flot — Matching Engine GPS Upgrade

## Task

Applica le modifiche descritte in questo documento al codebase backend esistente.
Il criterio di successo è: il matching engine usa coordinate GPS reali (lat/lng) per calcolare la distanza tra destinazioni, invece delle zone predefinite. Tutti i file elencati devono essere modificati. I test unitari del matching devono passare.

Prima di iniziare, leggi `CLAUDE-CODE-BACKEND-PROMPT_v2.md` per capire l'architettura esistente.

Se devi rompere una delle regole definite nel prompt principale, fermati e dimmelo.

---

## Allineamento — piano di esecuzione

Dimmi il tuo piano in massimo 6 step prima di iniziare a scrivere codice.

---

## Contesto della modifica

L'algoritmo di matching attuale usa zone predefinite (`centro`, `nord`, `ovest`...) per raggruppare le destinazioni. Questo approccio è troppo impreciso per MVP: due utenti che vanno a Brera e a Sesto San Giovanni cadono entrambi nella zona "nord" ma non dovrebbero mai condividere un taxi.

La nuova architettura usa coordinate GPS reali passate dal frontend via Google Places API, e calcola la distanza reale tra le destinazioni con Haversine.

Il concetto di `Zone` rimane in `airports.py` per retrocompatibilità con il frontend (airport picker, landmarks) ma **non viene più usato nel matching engine**.

---

## Modifiche per file

### 1. `src/lib/airports.py`

Aggiungi `max_wait_minutes` e `match_threshold` a `AirportConfig`. Rimuovi `adjacent_zones` (non serve più al matching). Mantieni `zones` e `landmarks` per il frontend.

```python
@dataclass
class AirportConfig:
    code: str
    name: str
    city: str
    country: str
    currency: str
    base_fare: int
    unlock_fee: int
    timezone: str
    terminals: list[Terminal]
    zones: list[Zone]             # mantenuto solo per frontend (landmarks, UI)
    meeting_points: dict[str, MeetingPoint]
    direction_labels: tuple[str, str]
    search_timeout_sec: int
    max_wait_minutes: int         # NUOVO — finestra di overlap massima accettabile
    match_threshold: float        # NUOVO — soglia minima di score per creare un match
    active: bool

# Valori MVP per MXP:
# max_wait_minutes = 20
# match_threshold = 0.25
```

Aggiorna la definizione di `MXP` con i nuovi campi. Rimuovi `adjacent_zones` dalla dataclass e dalla definizione di MXP.

---

### 2. `src/lib/matching.py`

Sostituisci interamente la logica di matching. Il nuovo algoritmo:

```python
from __future__ import annotations
from math import radians, sin, cos, sqrt, atan2
from datetime import datetime, timezone, timedelta
from aws_lambda_powertools import Logger

logger = Logger()

BUCKET_MINUTES = 10  # granularità bucket per GSI1


def get_time_bucket(flight_time: str) -> str:
    """Arrotonda al bucket da 10 minuti più vicino (in UTC)."""
    dt = datetime.fromisoformat(flight_time.replace("Z", "+00:00"))
    minutes = (dt.minute // BUCKET_MINUTES) * BUCKET_MINUTES
    dt = dt.replace(minute=minutes, second=0, microsecond=0)
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def get_adjacent_buckets(bucket: str, n: int = 2) -> list[str]:
    """
    Restituisce i bucket adiacenti (±n bucket).
    n=2 con BUCKET_MINUTES=10 → finestra ±20 min effettiva.
    """
    dt = datetime.fromisoformat(bucket.replace("Z", "+00:00"))
    buckets = []
    for i in range(-n, n + 1):
        shifted = dt + timedelta(minutes=BUCKET_MINUTES * i)
        buckets.append(shifted.isoformat().replace("+00:00", "Z"))
    return buckets


def haversine_km(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    """Distanza in km tra due punti GPS."""
    R = 6371.0
    dlat = radians(lat2 - lat1)
    dlng = radians(lng2 - lng1)
    a = sin(dlat / 2) ** 2 + cos(radians(lat1)) * cos(radians(lat2)) * sin(dlng / 2) ** 2
    return R * 2 * atan2(sqrt(a), sqrt(1 - a))


def distance_score(dist_km: float) -> float:
    """
    Score di distanza tra destinazioni.
    Soglie calibrate per MVP a bassa densità: include match fino a 20 km.
    """
    if dist_km <= 2:   return 1.0
    if dist_km <= 5:   return 0.8
    if dist_km <= 10:  return 0.5
    if dist_km <= 20:  return 0.2
    return 0.0


def time_score(bucket_a: str, bucket_b: str) -> float:
    """Score di prossimità temporale tra due bucket."""
    dt_a = datetime.fromisoformat(bucket_a.replace("Z", "+00:00"))
    dt_b = datetime.fromisoformat(bucket_b.replace("Z", "+00:00"))
    delta_min = abs((dt_a - dt_b).total_seconds()) / 60
    if delta_min == 0:                        return 1.0
    if delta_min <= BUCKET_MINUTES:           return 0.7
    if delta_min <= BUCKET_MINUTES * 2:       return 0.4
    return 0.0


def profile_score(user_a: dict, user_b: dict) -> float:
    """Bonus profilo: lingua condivisa + verifica identità."""
    score = 0.0
    if user_a.get("lang") == user_b.get("lang"):
        score += 0.1
    if user_a.get("verified") and user_b.get("verified"):
        score += 0.1
    return score


def compute_match_score(
    trip_a: dict,
    trip_b: dict,
    user_a: dict,
    user_b: dict,
) -> float:
    """
    Score finale: 0.5 × distanza + 0.3 × tempo + 0.2 × profilo.
    Tutti i componenti sono in [0, 1].
    """
    dist_km = haversine_km(
        trip_a["destLat"], trip_a["destLng"],
        trip_b["destLat"], trip_b["destLng"],
    )
    d_score = distance_score(dist_km)
    t_score = time_score(trip_a["timeBucket"], trip_b["timeBucket"])
    p_score = profile_score(user_a, user_b)

    final = (0.5 * d_score) + (0.3 * t_score) + (0.2 * p_score)

    logger.info(
        "match_score_computed",
        dist_km=round(dist_km, 2),
        d_score=d_score,
        t_score=t_score,
        p_score=p_score,
        final=round(final, 3),
    )
    return final


def can_match_direction(trip_a: dict, trip_b: dict) -> bool:
    """I due trip devono avere la stessa direzione (TO_CITY / FROM_CITY)."""
    return trip_a.get("direction") == trip_b.get("direction")
```

Rimuovi completamente `zone_proximity_score` e qualsiasi riferimento a `adjacent_zones`.

---

### 3. `src/handlers/trips/search_trips.py`

Aggiorna il matching engine entry point:

```python
# Logica aggiornata — pseudocodice commentato

# 1. Calcola bucket del trip corrente
bucket = get_time_bucket(trip["flightTime"])

# 2. Recupera bucket adiacenti (±2 → ±20 min effettivi)
buckets_to_query = get_adjacent_buckets(bucket, n=2)

# 3. Per ogni bucket, query GSI1: PK = f"{airportCode}#{bucket}"
#    Non filtrare per destZone nella query — il filtro è ora sulla distanza GPS
candidates = []
for b in buckets_to_query:
    results = dynamo.query(
        IndexName="GSI1-TimeBucket",
        KeyConditionExpression=Key("gsi1pk").eq(f"{airport_code}#{b}"),
        FilterExpression=Attr("status").eq("searching") & Attr("userId").ne(current_user_id),
    )
    candidates.extend(results["Items"])

# 4. Filtra per direzione
candidates = [c for c in candidates if can_match_direction(trip, c)]

# 5. Calcola score per ogni candidato
airport = get_airport(airport_code)
scored = []
for candidate in candidates:
    user_candidate = get_user(candidate["userId"])
    score = compute_match_score(trip, candidate, current_user, user_candidate)
    if score >= airport.match_threshold:   # usa soglia da AirportConfig, non hardcoded
        scored.append((score, candidate))

# 6. Ordina per score decrescente, prendi il migliore
scored.sort(key=lambda x: x[0], reverse=True)
best_match = scored[0] if scored else None
```

---

### 4. `src/lib/validation.py`

Aggiorna il Pydantic model `TripCreate`:

```python
from pydantic import BaseModel, ConfigDict, Field

class TripCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    airportCode: str
    terminal: str
    direction: str
    destination: str           # label leggibile ("Via Brera 28, Milano")
    destLat: float = Field(..., ge=-90, le=90)     # NUOVO
    destLng: float = Field(..., ge=-180, le=180)   # NUOVO
    destPlaceId: str           # NUOVO — Google Places place_id
    destZone: str | None = None  # opzionale, calcolato server-side se assente
    flightTime: str
    paxCount: int = Field(1, ge=1, le=4)
    luggage: int = Field(0, ge=0, le=6)
```

Il campo `destZone` diventa opzionale. Se il frontend lo manda (per retrocompatibilità), viene salvato. Se assente, il backend lo può calcolare tramite reverse-geofencing usando le zone di `airports.py` (opzionale per MVP, non bloccante).

---

### 5. DynamoDB — entity Trip (aggiornamento schema)

Aggiungi i nuovi attributi all'item Trip salvato in DynamoDB. Non serve migrazione: DynamoDB è schemaless, i nuovi trip avranno i campi GPS, i vecchi no (il matching li escluderà per assenza di `destLat`).

```python
# In create_trip.py, quando si salva l'item:
item = {
    "pk": f"TRIP#{trip_id}",
    "sk": "META",
    "userId": user_id,
    "airportCode": body.airportCode,
    "terminal": body.terminal,
    "direction": body.direction,
    "destination": body.destination,
    "destLat": body.destLat,          # NUOVO
    "destLng": body.destLng,          # NUOVO
    "destPlaceId": body.destPlaceId,  # NUOVO
    "destZone": body.destZone,        # opzionale
    "flightTime": body.flightTime,
    "timeBucket": get_time_bucket(body.flightTime),
    "gsi1pk": f"{body.airportCode}#{get_time_bucket(body.flightTime)}",
    "paxCount": body.paxCount,
    "luggage": body.luggage,
    "status": "searching",
    "createdAt": now_iso(),
}
```

---

### 6. GSI1 — Sort Key update

Il GSI1 attuale ha SK = `destZone`. Con il nuovo sistema, la zona non è più il filtro principale. Aggiorna il SK del GSI1 a `createdAt` (già presente sull'item) per permettere ordinamento cronologico dei candidati nello stesso bucket.

Nel `template.yaml`, GSI1 diventa:

```yaml
- IndexName: GSI1-TimeBucket
  KeySchema:
    - AttributeName: gsi1pk
      KeyType: HASH
    - AttributeName: createdAt    # CAMBIATO da destZone
      KeyType: RANGE
  Projection:
    ProjectionType: ALL
```

**Attenzione**: questa modifica richiede di ricreare la tabella in ambiente dev. In prod usare una blue/green migration.

---

### 7. `src/lib/zones.py` — semplificazione

Il file `zones.py` che conteneva la logica di geofencing a zone fisse può essere semplificato. Mantieni solo la funzione `coords_to_zone` (per calcolare `destZone` opzionale da lat/lng) e `haversine_km` (spostata in `matching.py`). Rimuovi `zone_proximity_score` e `get_adjacent_zones`.

---

### 8. `tests/unit/test_matching.py` — aggiorna i test

I test devono coprire i nuovi comportamenti:

```python
# Test: haversine_km
def test_haversine_duomo_centrale():
    # Duomo → Centrale: ~2.1 km
    dist = haversine_km(45.4642, 9.1900, 45.4854, 9.2040)
    assert 1.8 < dist < 2.5

# Test: distance_score
def test_distance_score_boundaries():
    assert distance_score(1.0) == 1.0
    assert distance_score(3.0) == 0.8
    assert distance_score(7.0) == 0.5
    assert distance_score(15.0) == 0.2
    assert distance_score(25.0) == 0.0

# Test: compute_match_score — match valido MVP
def test_compute_match_score_valid():
    trip_a = {"destLat": 45.4642, "destLng": 9.1900, "timeBucket": "2026-04-24T14:00:00Z", "direction": "TO_MILAN"}
    trip_b = {"destLat": 45.4721, "destLng": 9.1878, "timeBucket": "2026-04-24T14:00:00Z", "direction": "TO_MILAN"}
    user_a = {"lang": "it", "verified": True}
    user_b = {"lang": "it", "verified": True}
    score = compute_match_score(trip_a, trip_b, user_a, user_b)
    assert score >= 0.25  # supera soglia MVP

# Test: compute_match_score — destinazioni troppo distanti
def test_compute_match_score_too_far():
    trip_a = {"destLat": 45.4642, "destLng": 9.1900, "timeBucket": "2026-04-24T14:00:00Z", "direction": "TO_MILAN"}
    trip_b = {"destLat": 45.6200, "destLng": 9.0500, "timeBucket": "2026-04-24T14:00:00Z", "direction": "TO_MILAN"}
    user_a = {"lang": "it", "verified": False}
    user_b = {"lang": "en", "verified": False}
    score = compute_match_score(trip_a, trip_b, user_a, user_b)
    assert score < 0.25  # non supera soglia

# Test: get_adjacent_buckets
def test_adjacent_buckets_count():
    buckets = get_adjacent_buckets("2026-04-24T14:00:00Z", n=2)
    assert len(buckets) == 5  # -2, -1, 0, +1, +2

# Test: direzione diversa → no match
def test_direction_filter():
    trip_a = {"direction": "TO_MILAN", ...}
    trip_b = {"direction": "FROM_MILAN", ...}
    assert can_match_direction(trip_a, trip_b) is False
```

---

## Regole da rispettare

- Non hardcodare `max_wait_minutes` o `match_threshold` nei handler — leggerli sempre da `get_airport(code)`.
- Il filtro per distanza avviene **in Python dopo la query DynamoDB**, non come FilterExpression (DynamoDB non supporta calcoli geospaziali).
- I trip senza `destLat`/`destLng` (creati prima di questa release) devono essere **esclusi silenziosamente** dal matching con un check `if "destLat" not in candidate: continue`.
- Usare `logger.info()` di Powertools per loggare score, distanza e bucket — mai `print()`.
- Tutti i float che appaiono nei log o nelle response API devono essere arrotondati a 2-3 decimali.

---

## Environment variables — nessuna modifica necessaria

Il matching engine GPS non richiede nuove env var. Le coordinate arrivano dal frontend nel body della request e vengono salvate in DynamoDB.

---

*Flot Matching GPS Upgrade — Aprile 2026*
