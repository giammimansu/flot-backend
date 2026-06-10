"""
Verifica end-to-end del matchmaker in configurazione MVP (MvpPickupSimpleMode).

Inserisce coppie sintetiche di trip e verifica che il matchmaker decida e
persista esattamente come specificato. Gira SOLO su Flot-dev / eu-south-1:
si rifiuta di partire su qualunque altra tabella o regione.

Uso:
    cd flot-backend
    TABLE_NAME=Flot-dev AWS_DEFAULT_REGION=eu-south-1 python scripts/verify_mvp_matchmaking.py

    # rimuove dati fake rimasti da un run interrotto:
    TABLE_NAME=Flot-dev AWS_DEFAULT_REGION=eu-south-1 python scripts/verify_mvp_matchmaking.py --cleanup

Tutti gli ID fake hanno prefisso "mvp-verify-" e vengono rimossi in un blocco
finally, anche se una verifica fallisce a metà.

Note di implementazione (discrepanze rispetto al brief, segnalate come richiesto):
  - Lo script NON chiama optimize_pool. optimize_pool interroga l'intero pool
    MXP live e potrebbe accoppiare un trip fake con un trip REALE di Flot-dev.
    Per isolamento, le decisioni di matching girano su un pool in-memory passato
    direttamente a build_compatibility_matrix (che accetta `pool` come argomento),
    e la persistenza dello scenario 1 usa _create_direct_match — la stessa
    funzione che optimize_pool invoca nel ramo lock-window (matchmaker.py:315).
  - La fascia oraria (mvp_active_windows) e' bypassata "gratis": il gate vive in
    process_airport_v4, che non viene mai attraversato.
  - Solo lo scenario 1 scrive su DynamoDB (2 trip + 1 match). Gli scenari 2-6
    sono pura logica decisionale su pool in-memory: nessuna scrittura.
"""
from __future__ import annotations

import argparse
import os
import sys
import uuid
from datetime import datetime, timezone, timedelta
from decimal import Decimal

# ── Guardrail di sicurezza (PRIMA di qualsiasi import o scrittura) ─────
TABLE_NAME = os.environ.get("TABLE_NAME", "Flot-dev")
REGION = os.environ.get("AWS_DEFAULT_REGION", "eu-south-1")

if TABLE_NAME != "Flot-dev" or REGION != "eu-south-1":
    print(
        f"ERRORE GUARDRAIL: questo script gira SOLO su Flot-dev / eu-south-1.\n"
        f"  TABLE_NAME={TABLE_NAME!r}  AWS_DEFAULT_REGION={REGION!r}\n"
        f"  Niente fallback, niente override. Non tocca mai staging o prod.",
        file=sys.stderr,
    )
    sys.exit(1)

# Env per i moduli applicativi. MVP_PICKUP_SIMPLE_MODE deve essere settato PRIMA
# di importare il matchmaker: il flag viene letto a import-time.
os.environ["TABLE_NAME"] = TABLE_NAME
os.environ["AWS_DEFAULT_REGION"] = REGION
os.environ["MVP_PICKUP_SIMPLE_MODE"] = "true"
os.environ.setdefault("POWERTOOLS_SERVICE_NAME", "verify-mvp-matchmaking")
os.environ.setdefault("LOG_LEVEL", "WARNING")

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from lib.airports import get_airport                       # noqa: E402
from lib.matching import (                                  # noqa: E402
    haversine_km,
    get_match_coords,
    compute_match_score,
    compute_dynamic_threshold,
    get_time_bucket,
)
from lib import dynamo                                      # noqa: E402
import handlers.matching.matchmaker as matchmaker          # noqa: E402
from handlers.matching.matchmaker import (                 # noqa: E402
    build_compatibility_matrix,
    find_optimal_assignments,
    _create_direct_match,
)

# Silenzia match.found alla radice. _create_direct_match (scenario 1) emette
# put_event("match.found"); l'handler async on_match_found degrada in silenzio
# (nessun errore/retry/DLQ con userId inesistente) MA scrive un item NOTIF
# non tracciabile per ogni utente fake via deliver(persist=True). La verifica
# legge il MATCH persistito direttamente, quindi l'evento e' inutile qui:
# annullarlo evita scritture residue e rumore EventBridge.
matchmaker.put_event = lambda *args, **kwargs: None

ID_PREFIX = "mvp-verify-"

# Punto fittizio per le coordinate di destinazione (Malpensa). Serve solo allo
# scenario 6 come "trappola": se il codice usasse dest invece di origin per lo
# score, la distanza tra due trip sarebbe ~0 e ogni coppia matcherebbe.
MXP_DEST = (45.6306, 8.7281)

# Origini reali a Milano (lat, lng).
DUOMO = (45.4642, 9.1900)
DUOMO_600M = (45.4696, 9.1900)      # ~0.6 km a nord del Duomo
NORD_PERIFERIA = (45.5400, 9.2000)  # ~8 km a nord (oltre il gate)
CENTRO_1600M = (45.4792, 9.1900)    # ~1.67 km a nord (midpoint ~0.83 km)
CLUSTER_C = (45.4670, 9.1900)       # ~0.3 km da Duomo e da Duomo+600m (scenario 7)

# Tracciamento item creati su DDB → cleanup nel finally.
CREATED: list[tuple[str, str]] = []


# ── Helpers ───────────────────────────────────────────────────────────

def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _new_trip_id() -> str:
    return f"{ID_PREFIX}trip-{uuid.uuid4().hex[:8]}"


def _make_trip(
    user_n: int,
    origin: tuple[float, float],
    flight_dt: datetime,
    direction: str,
    dest: tuple[float, float] = MXP_DEST,
) -> dict:
    """Costruisce un trip fake in-memory. Coordinate Decimal (Table resource)."""
    flight_iso = _iso(flight_dt)
    return {
        "pk": f"TRIP#{_new_trip_id()}",
        "sk": "META",
        "tripId": "",  # riempito sotto da pk
        "userId": f"{ID_PREFIX}user-{user_n}",
        "airportCode": "MXP",
        "status": "scheduled",
        "direction": direction,
        "flightTime": flight_iso,
        "timeBucket": get_time_bucket(flight_iso),
        "originLat": Decimal(str(origin[0])),
        "originLng": Decimal(str(origin[1])),
        "destLat": Decimal(str(dest[0])),
        "destLng": Decimal(str(dest[1])),
        "gsi5pk": "MXP#scheduled",
        "gsi5sk": flight_iso,
        "createdAt": _iso(datetime.now(timezone.utc)),
    }


def _finalize(trip: dict) -> dict:
    """Imposta tripId coerente con pk."""
    trip["tripId"] = trip["pk"].split("#", 1)[1]
    return trip


def _origins_distance_km(trip_a: dict, trip_b: dict, airport) -> float:
    lat_a, lng_a = get_match_coords(trip_a, airport)
    lat_b, lng_b = get_match_coords(trip_b, airport)
    return haversine_km(lat_a, lng_a, lat_b, lng_b)


def _midpoint_max_dist_km(trip_a: dict, trip_b: dict, airport) -> float:
    lat_a, lng_a = get_match_coords(trip_a, airport)
    lat_b, lng_b = get_match_coords(trip_b, airport)
    mid_lat, mid_lng = (lat_a + lat_b) / 2, (lng_a + lng_b) / 2
    return max(
        haversine_km(mid_lat, mid_lng, lat_a, lng_a),
        haversine_km(mid_lat, mid_lng, lat_b, lng_b),
    )


def _pair_matched(pool: list[dict], airport, now: datetime) -> bool:
    """True se i due trip del pool risultano accoppiati dal matchmaker."""
    pairs = build_compatibility_matrix(pool, airport, now)
    return len(pairs) > 0


# ── Scenari ───────────────────────────────────────────────────────────

def scenario_1_valid_match(airport, now: datetime) -> tuple[bool, str]:
    """Match valido + match persistito con pickupPoint reale entro pickup_radius_m."""
    flight = now + timedelta(hours=2)  # <6h → soglia dinamica minima; forza il match
    a = _finalize(_make_trip(1, DUOMO, flight, airport.to_airport_direction))
    b = _finalize(_make_trip(2, DUOMO_600M, flight, airport.to_airport_direction))

    pool = [a, b]
    pairs = build_compatibility_matrix(pool, airport, now)
    assert len(pairs) == 1, f"attesa 1 coppia compatibile, trovate {len(pairs)}"
    assignments = find_optimal_assignments(pairs)
    assert len(assignments) == 1, "la coppia non risulta assegnata"
    _, _, score, _, _ = assignments[0]

    # Persistenza su DDB: i trip devono esistere (condition status=scheduled).
    dynamo.put_item(a)
    CREATED.append((a["pk"], "META"))
    dynamo.put_item(b)
    CREATED.append((b["pk"], "META"))

    from lib.matching import compute_pickup_point
    pickup = compute_pickup_point(a, b, airport)
    _create_direct_match(a, b, score, airport, pickup_point=pickup)

    # Rileggi il trip per ottenere il matchId persistito dal matchmaker.
    trip_a_db = dynamo.get_item(a["pk"], "META") or {}
    match_id = trip_a_db.get("matchId")
    assert match_id, "matchmaker non ha persistito il match (matchId assente sul trip)"
    CREATED.append((f"MATCH#{match_id}", "META"))

    match = dynamo.get_item(f"MATCH#{match_id}", "META") or {}
    pp = match.get("pickupPoint")
    assert pp, "il match persistito non ha pickupPoint"
    assert pp.get("lat") is not None and pp.get("lng") is not None, \
        "pickupPoint senza lat/lng reali"
    p_lat, p_lng = float(pp["lat"]), float(pp["lng"])

    radius_km = airport.pickup_radius_m / 1000.0
    oa_lat, oa_lng = get_match_coords(a, airport)
    ob_lat, ob_lng = get_match_coords(b, airport)
    d_a = haversine_km(p_lat, p_lng, oa_lat, oa_lng)
    d_b = haversine_km(p_lat, p_lng, ob_lat, ob_lng)
    assert d_a <= radius_km, f"pickup a {d_a*1000:.0f}m da origine A > {airport.pickup_radius_m}m"
    assert d_b <= radius_km, f"pickup a {d_b*1000:.0f}m da origine B > {airport.pickup_radius_m}m"

    return True, f"pickup {d_a*1000:.0f}m / {d_b*1000:.0f}m da origini"


def scenario_2_origin_gate(airport, now: datetime) -> tuple[bool, str]:
    """Origini a >max_origin_distance_km → scartate dal gate distanza."""
    flight = now + timedelta(hours=8)
    a = _finalize(_make_trip(3, DUOMO, flight, airport.to_airport_direction))
    b = _finalize(_make_trip(4, NORD_PERIFERIA, flight, airport.to_airport_direction))

    dist = _origins_distance_km(a, b, airport)
    assert dist > airport.max_origin_distance_km, \
        f"setup errato: origini a {dist:.2f}km <= gate {airport.max_origin_distance_km}km"
    assert not _pair_matched([a, b], airport, now), "match inatteso oltre il gate distanza"
    return True, f"scartata, origini a {dist:.1f}km"


def scenario_3_midpoint_out_of_radius(airport, now: datetime) -> tuple[bool, str]:
    """Passa il gate distanza ma il midpoint cade oltre pickup_radius_m da un'origine."""
    flight = now + timedelta(hours=8)
    a = _finalize(_make_trip(5, DUOMO, flight, airport.to_airport_direction))
    b = _finalize(_make_trip(6, CENTRO_1600M, flight, airport.to_airport_direction))

    dist = _origins_distance_km(a, b, airport)
    radius_km = airport.pickup_radius_m / 1000.0
    mid_max = _midpoint_max_dist_km(a, b, airport)

    # Distinzione esplicita dallo scenario 2: la coppia DEVE passare il gate...
    assert dist <= airport.max_origin_distance_km, \
        f"setup errato: la coppia non passa il gate ({dist:.2f}km)"
    # ...ma essere scartata dopo il calcolo del midpoint.
    assert mid_max > radius_km, \
        f"setup errato: midpoint a {mid_max*1000:.0f}m entro raggio {airport.pickup_radius_m}m"
    assert not _pair_matched([a, b], airport, now), \
        "match inatteso nonostante midpoint fuori raggio"
    return True, f"gate ok ({dist:.2f}km), midpoint {mid_max*1000:.0f}m da origine"


def scenario_4_wrong_direction(airport, now: datetime) -> tuple[bool, str]:
    """Direzione opposta → nessun match (can_match_direction)."""
    flight = now + timedelta(hours=8)
    opposite = next(d for d in airport.direction_labels if d != airport.to_airport_direction)
    a = _finalize(_make_trip(7, DUOMO, flight, airport.to_airport_direction))
    b = _finalize(_make_trip(8, DUOMO_600M, flight, opposite))

    assert not _pair_matched([a, b], airport, now), "match inatteso tra direzioni opposte"
    return True, f"{airport.to_airport_direction} vs {opposite}"


def scenario_5_dynamic_threshold(airport, now: datetime) -> tuple[bool, str]:
    """Vicine geograficamente ma orari distanti: score sotto la soglia dinamica."""
    flight_a = now + timedelta(days=8)
    flight_b = now + timedelta(days=8, hours=2)  # >20min di delta → time_score 0.0
    a = _finalize(_make_trip(9, DUOMO, flight_a, airport.to_airport_direction))
    b = _finalize(_make_trip(10, DUOMO_600M, flight_b, airport.to_airport_direction))

    score = compute_match_score(a, b, {}, {}, mode="scheduled", airport=airport)
    threshold = compute_dynamic_threshold(
        airport.match_threshold, a["flightTime"], b["flightTime"], now,
    )
    assert score < threshold, \
        f"score {score:.3f} non sotto soglia dinamica {threshold:.2f}"
    assert not _pair_matched([a, b], airport, now), \
        "match inatteso: score sotto soglia ma coppia accoppiata"
    return True, f"score {score:.2f} < soglia {threshold:.2f}"


def scenario_6_score_on_origins(airport, now: datetime) -> tuple[bool, str]:
    """Diagnostica: lo score usa origin*, NON dest* (dest = MXP per entrambi)."""
    flight = now + timedelta(hours=2)
    a = _finalize(_make_trip(11, DUOMO, flight, airport.to_airport_direction, dest=MXP_DEST))
    b = _finalize(_make_trip(12, DUOMO_600M, flight, airport.to_airport_direction, dest=MXP_DEST))

    coord_a = get_match_coords(a, airport)
    coord_b = get_match_coords(b, airport)

    # Le coordinate usate per lo score devono essere le origini, non MXP.
    assert coord_a == (float(a["originLat"]), float(a["originLng"])), \
        f"score usa {coord_a}, non l'origine di A {(float(a['originLat']), float(a['originLng']))}"
    assert coord_b == (float(b["originLat"]), float(b["originLng"])), \
        f"score usa {coord_b}, non l'origine di B"
    assert coord_a != MXP_DEST and coord_b != MXP_DEST, \
        "score usa le coordinate dest (MXP) — bug pericoloso: ogni coppia matcherebbe"

    dist_used = haversine_km(coord_a[0], coord_a[1], coord_b[0], coord_b[1])
    assert dist_used > 0.05, \
        f"distanza per lo score = {dist_used*1000:.0f}m ~ 0: probabilmente calcolata su dest=MXP"

    print(f"      [diag] coord score A={coord_a} B={coord_b} dist={dist_used*1000:.0f}m "
          f"(dest MXP={MXP_DEST} ignorate)")
    return True, f"score su origini (dist {dist_used*1000:.0f}m, dest MXP ignorate)"


def scenario_7_greedy_no_reuse(airport, now: datetime) -> tuple[bool, str]:
    """Pool di 3: A-B score > A-C. Il greedy crea A-B e lascia C non accoppiato (no riuso di A)."""
    flight_ab = now + timedelta(hours=8)
    flight_c = flight_ab + timedelta(minutes=25)  # orario distante → time_score piu' basso
    a = _finalize(_make_trip(13, DUOMO, flight_ab, airport.to_airport_direction))
    b = _finalize(_make_trip(14, DUOMO_600M, flight_ab, airport.to_airport_direction))
    c = _finalize(_make_trip(15, CLUSTER_C, flight_c, airport.to_airport_direction))

    pool = [a, b, c]
    pairs = build_compatibility_matrix(pool, airport, now)

    # A-C e B-C devono essere candidati validi (passano gate+midpoint+soglia):
    # cosi' verifichiamo che C sia escluso DAL GREEDY, non filtrato a monte.
    ids = {a["tripId"], b["tripId"], c["tripId"]}
    pair_sets = [frozenset((p[0], p[1])) for p in pairs]
    assert frozenset((a["tripId"], b["tripId"])) in pair_sets, "coppia A-B assente dal matrix"
    assert any(c["tripId"] in ps for ps in pair_sets), \
        "C non e' candidato in nessuna coppia: sarebbe filtrato a monte, non dal greedy"

    # A-B deve essere la coppia a score piu' alto (in testa, matrix ordinato desc).
    top = frozenset((pairs[0][0], pairs[0][1]))
    assert top == frozenset((a["tripId"], b["tripId"])), \
        f"la coppia top non e' A-B: {top} (score={pairs[0][2]:.3f})"

    assignments = find_optimal_assignments(pairs)
    assert len(assignments) == 1, f"attesa 1 assegnazione (A-B), trovate {len(assignments)}"
    assigned_pair = frozenset((assignments[0][0], assignments[0][1]))
    assert assigned_pair == frozenset((a["tripId"], b["tripId"])), \
        f"il greedy ha assegnato {assigned_pair}, non A-B"

    assigned_ids = set(assignments[0][:2])
    assert c["tripId"] not in assigned_ids, "C e' stato accoppiato: atteso non accoppiato"
    assert ids - assigned_ids == {c["tripId"]}, "solo C deve restare non accoppiato"

    return True, f"A-B match (score {pairs[0][2]:.2f}), C non accoppiato"


# ── Cleanup ───────────────────────────────────────────────────────────

def _cleanup_created() -> None:
    for pk, sk in CREATED:
        try:
            dynamo.delete_item(pk, sk)
            print(f"[CLEANUP] rimosso {pk}")
        except Exception as e:  # noqa: BLE001
            print(f"[CLEANUP] WARN impossibile rimuovere {pk}: {e}", file=sys.stderr)


def cleanup_leftovers() -> None:
    """Rimuove ogni trip/match fake (prefisso mvp-verify-) rimasto da run precedenti."""
    print("=== CLEANUP dati fake residui (mvp-verify-) ===")
    removed = 0
    for status in ("scheduled", "tentative_match", "matched"):
        trips = dynamo.query_gsi(
            index_name="GSI5-TripStatus",
            pk_name="gsi5pk",
            pk_value=f"MXP#{status}",
        )
        for trip in trips:
            tid = trip.get("tripId", "")
            if not tid.startswith(ID_PREFIX):
                continue
            for mid_key, prefix in (("matchId", "MATCH#"), ("tentativeMatchId", "TENTATIVE_MATCH#")):
                mid = trip.get(mid_key)
                if mid:
                    dynamo.delete_item(f"{prefix}{mid}", "META")
                    print(f"[CLEANUP] rimosso {prefix}{mid}")
            dynamo.delete_item(trip["pk"], "META")
            print(f"[CLEANUP] rimosso {trip['pk']}")
            removed += 1
    print(f"Rimossi {removed} trip fake.\n")


# ── Runner ────────────────────────────────────────────────────────────

SCENARIOS = [
    ("Match valido", scenario_1_valid_match),
    ("Gate distanza origini", scenario_2_origin_gate),
    ("Midpoint fuori raggio", scenario_3_midpoint_out_of_radius),
    ("Direzione sbagliata", scenario_4_wrong_direction),
    ("Soglia dinamica", scenario_5_dynamic_threshold),
    ("Score su origini non MXP", scenario_6_score_on_origins),
    ("Greedy: no riuso di A", scenario_7_greedy_no_reuse),
]


def run() -> int:
    airport = get_airport("MXP")
    now = datetime.now(timezone.utc)

    print("=== MVP MATCHMAKING VERIFICATION (Flot-dev) ===")
    print(f"airport=MXP  direction={airport.to_airport_direction}  "
          f"max_origin={airport.max_origin_distance_km}km  "
          f"pickup_radius={airport.pickup_radius_m}m  base_threshold={airport.match_threshold}\n")

    results: list[tuple[str, bool, str]] = []
    try:
        for name, fn in SCENARIOS:
            try:
                passed, detail = fn(airport, now)
            except AssertionError as e:
                passed, detail = False, str(e)
            except Exception as e:  # noqa: BLE001
                passed, detail = False, f"errore inatteso: {type(e).__name__}: {e}"
            results.append((name, passed, detail))
    finally:
        if CREATED:
            print("\n--- cleanup scenario 1 ---")
            _cleanup_created()

    print("\n=== RIEPILOGO ===")
    width = max(len(n) for n, _ in SCENARIOS) + 4
    passed_count = 0
    for i, (name, passed, detail) in enumerate(results, start=1):
        dots = "." * (width - len(name))
        status = "PASS" if passed else "FAIL"
        passed_count += int(passed)
        detail_str = f" ({detail})" if detail else ""
        print(f"[{i}] {name} {dots} {status}{detail_str}")

    print(f"\n{passed_count}/{len(results)} PASSED")
    return 0 if passed_count == len(results) else 1


def main() -> int:
    parser = argparse.ArgumentParser(description="Verifica MVP matchmaking (Flot-dev only).")
    parser.add_argument("--cleanup", action="store_true",
                        help="rimuove dati fake mvp-verify- rimasti da run interrotti, poi esce")
    args = parser.parse_args()

    if args.cleanup:
        cleanup_leftovers()
        return 0
    return run()


if __name__ == "__main__":
    sys.exit(main())
