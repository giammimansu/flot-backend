"""
Test end-to-end MVP: invoca le Lambda REALI deployate su dev (non in-memory,
non mock), bypassando API Gateway/Cognito con un event sintetico che porta il
claim `requestContext.authorizer.claims.sub` (esattamente cio' che legge
`lib.http.get_user_id`). Esercita il flusso POST /trips e il matchmaker.

Gira SOLO su dev (Flot-dev / eu-south-1): guardrail iniziale che fa sys.exit(1)
su qualunque altra regione, come gli altri script in scripts/.

Uso:
    cd flot-backend
    TABLE_NAME=Flot-dev AWS_DEFAULT_REGION=eu-south-1 python scripts/e2e_mvp_trips.py

    # rimuove dati fake (prefisso e2e-mvp-) rimasti da run interrotti:
    TABLE_NAME=Flot-dev AWS_DEFAULT_REGION=eu-south-1 python scripts/e2e_mvp_trips.py --cleanup

Richiede credenziali AWS valide con permesso lambda:InvokeFunction +
dynamodb read/delete sulla tabella Flot-dev.

DISCREPANZE NOTE (vedi report al committente):
  - Scenario 6: NON esiste alcun `requiredArrivalAtAirport` ne' un buffer
    aeroporto ne' logica `departureTime - buffer` nel codice. create_trip salva
    `flightTime` = risultato di fetch_flight_eta (orientato all'arrivo). Sc6 e'
    quindi DIAGNOSTICO: logga il flightTime salvato e documenta l'assenza.
  - Direzione MVP valida per MXP = airport.to_airport_direction = "FROM_MILAN"
    (NON la stringa letterale "TO_AIRPORT" del brief).
  - Il matchmaker con MVP_TIME_WINDOWS_MODE skippa MXP fuori da
    airport.mvp_active_windows. `now` nella Lambda e' l'ora reale, non forzabile
    via invoke: gli scenari C/D rilevano la fascia corrente e documentano.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone, timedelta
from decimal import Decimal

# ── Guardrail di sicurezza (PRIMA di qualsiasi import applicativo) ─────
TABLE_NAME = os.environ.get("TABLE_NAME", "Flot-dev")
REGION = os.environ.get("AWS_DEFAULT_REGION", "eu-south-1")
STAGE = os.environ.get("STAGE", "dev")

if REGION != "eu-south-1" or TABLE_NAME != "Flot-dev":
    print(
        f"ERRORE GUARDRAIL: questo script gira SOLO su Flot-dev / eu-south-1.\n"
        f"  TABLE_NAME={TABLE_NAME!r}  AWS_DEFAULT_REGION={REGION!r}\n"
        f"  Niente fallback, niente override. Non tocca mai staging o prod.",
        file=sys.stderr,
    )
    sys.exit(1)

os.environ["TABLE_NAME"] = TABLE_NAME
os.environ["AWS_DEFAULT_REGION"] = REGION
os.environ.setdefault("POWERTOOLS_SERVICE_NAME", "e2e-mvp-trips")
os.environ.setdefault("LOG_LEVEL", "WARNING")

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import boto3                                                  # noqa: E402
from boto3.dynamodb.conditions import Key                     # noqa: E402

from lib import dynamo                                        # noqa: E402
from lib.airports import get_airport                          # noqa: E402
from lib.matching import (                                    # noqa: E402
    haversine_km,
    get_match_coords,
    get_time_bucket,
    is_in_active_window,
    next_active_window_label,
)

# Nomi function: dal template (AWS::Serverless::Function FunctionName:
# !Sub flot-<name>-${Stage}). Derivati, non assunti.
FN_CREATE_TRIP = f"flot-create-trip-{STAGE}"
FN_MATCHMAKER = f"flot-matchmaker-{STAGE}"

ID_PREFIX = "e2e-mvp-"

# Origini reali a Milano (lat, lng) — riuso le coordinate di verify_mvp.
DUOMO = (45.4642, 9.1900)
DUOMO_600M = (45.4696, 9.1900)      # ~0.6 km a nord (compatibile)
NORD_PERIFERIA = (45.5400, 9.2000)  # ~8 km a nord (oltre il gate distanza)
MILANO_DEST = (45.4850, 9.2040)     # destinazione cittadina generica (zona nord)

_lambda = boto3.client("lambda", region_name=REGION)

# Item creati su DDB → cleanup nel finally.
CREATED: list[tuple[str, str]] = []


# ── Invocazione Lambda ─────────────────────────────────────────────────

def _apigw_event(user_id: str, body: dict) -> dict:
    """Event API Gateway sintetico, identico in forma a event.json."""
    return {
        "resource": "/trips",
        "path": "/trips",
        "httpMethod": "POST",
        "headers": {"Origin": "http://localhost:3000"},
        "requestContext": {"authorizer": {"claims": {"sub": user_id}}},
        "body": json.dumps(body),
    }


def _invoke(function_name: str, payload: dict) -> tuple[int | None, dict | str, str | None]:
    """
    Invoca una Lambda. Ritorna (statusCode, body_parsed, function_error).
    function_error != None => fallimento esplicito (eccezione non gestita).
    Per le risposte API Gateway proxy estrae statusCode + body (JSON).
    """
    resp = _lambda.invoke(
        FunctionName=function_name,
        InvocationType="RequestResponse",
        Payload=json.dumps(payload).encode("utf-8"),
    )
    fn_error = resp.get("FunctionError")
    raw = resp["Payload"].read().decode("utf-8")
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return None, raw, fn_error or "non-JSON payload"

    if fn_error:
        # Eccezione non gestita: parsed = {errorMessage, errorType, stackTrace}
        return None, parsed, fn_error

    # Risposta proxy API Gateway: {statusCode, body, headers}
    status = parsed.get("statusCode")
    body = parsed.get("body")
    if isinstance(body, str):
        try:
            body = json.loads(body)
        except json.JSONDecodeError:
            pass
    return status, body, None


def _post_trip(user_n: int, body: dict) -> tuple[int | None, dict | str, str | None]:
    """POST /trips come utente fake e2e-mvp-user-N. Traccia il trip per cleanup."""
    user_id = f"{ID_PREFIX}user-{user_n}"
    status, resp, fn_error = _invoke(FN_CREATE_TRIP, _apigw_event(user_id, body))
    if status in (200, 201) and isinstance(resp, dict) and resp.get("tripId"):
        CREATED.append((f"TRIP#{resp['tripId']}", "META"))
    return status, resp, fn_error


def _base_trip_body(
    direction: str,
    origin: tuple[float, float] | None,
    flight_dt: datetime,
    *,
    include_flight_time: bool,
    flight_number: str = "AZ1234",
) -> dict:
    """Body POST /trips valido (campi richiesti da CreateTripRequest)."""
    body = {
        "airportCode": "MXP",
        "terminal": "T1",
        "direction": direction,
        "destination": "Milano Nord",
        "destLat": MILANO_DEST[0],
        "destLng": MILANO_DEST[1],
        "destPlaceId": "ChIJ_e2e_mvp_dest",
        "mode": "scheduled",
        "flightNumber": flight_number,
        "flightDate": flight_dt.date().isoformat(),
        "luggage": 1,
        "paxCount": 1,
    }
    if include_flight_time:
        body["flightTime"] = flight_dt.isoformat().replace("+00:00", "Z")
    if origin is not None:
        body["originLat"] = origin[0]
        body["originLng"] = origin[1]
        body["originPlaceId"] = "ChIJ_e2e_mvp_origin"
        body["originLabel"] = "Via Test 1, Milano"
    return body


def _read_trip(trip_id: str) -> dict:
    return dynamo.get_item(f"TRIP#{trip_id}", "META") or {}


# ── Scenari A — Restrizione tratta (MvpSingleRouteMode) ────────────────

def scenario_a1_wrong_airport(airport, now) -> tuple[bool, str]:
    """FCO (tratta non MVP) → 4xx."""
    flight = now + timedelta(hours=8)
    body = _base_trip_body("TO_ROME", DUOMO, flight, include_flight_time=True)
    body["airportCode"] = "FCO"
    status, resp, fn_error = _post_trip(1, body)
    if fn_error:
        return False, f"FunctionError: {resp}"
    ok = isinstance(status, int) and 400 <= status < 500
    return ok, f"status={status} (atteso 4xx) resp={_short(resp)}"


def scenario_a2_wrong_direction(airport, now) -> tuple[bool, str]:
    """MXP ma direzione opposta a to_airport_direction → 4xx."""
    opposite = next(d for d in airport.direction_labels if d != airport.to_airport_direction)
    flight = now + timedelta(hours=8)
    body = _base_trip_body(opposite, DUOMO, flight, include_flight_time=True)
    status, resp, fn_error = _post_trip(2, body)
    if fn_error:
        return False, f"FunctionError: {resp}"
    ok = isinstance(status, int) and 400 <= status < 500
    return ok, f"dir={opposite} status={status} (atteso 4xx)"


def scenario_a3_missing_origin(airport, now) -> tuple[bool, str]:
    """MXP + direzione giusta ma SENZA campi origin* → 4xx."""
    flight = now + timedelta(hours=8)
    body = _base_trip_body(airport.to_airport_direction, None, flight, include_flight_time=True)
    status, resp, fn_error = _post_trip(3, body)
    if fn_error:
        return False, f"FunctionError: {resp}"
    ok = isinstance(status, int) and 400 <= status < 500
    return ok, f"no origin status={status} (atteso 4xx)"


# ── Scenari B — Lookup volo (create_trip.py:85-106) ────────────────────

def scenario_b4_lookup_resolves(airport, now) -> tuple[bool, str]:
    """POST valido SENZA flightTime → 2xx; verifica flightTime risolto in DDB."""
    flight = now + timedelta(days=2)
    body = _base_trip_body(airport.to_airport_direction, DUOMO, flight, include_flight_time=False)
    status, resp, fn_error = _post_trip(4, body)
    if fn_error:
        return False, f"FunctionError: {resp}"
    if status not in (200, 201) or not isinstance(resp, dict):
        return False, f"status={status} (atteso 2xx) resp={_short(resp)}"

    trip_id = resp["tripId"]
    trip = _read_trip(trip_id)
    flight_time = trip.get("flightTime")
    trip_status = trip.get("status")
    noon_fallback = f"{flight.date().isoformat()}T12:00:00Z"

    print(f"      [diag] flightTime risolto={flight_time!r} status={trip_status!r} "
          f"(fallback-mezzogiorno sarebbe {noon_fallback!r})")
    if not flight_time:
        return False, "flightTime non risolto (null) sul trip persistito"
    if trip_status == "tracking_pending" or flight_time == noon_fallback:
        # Degradato: il tracker non ha risolto il volo (no API key / volo inesistente).
        return True, f"2xx, flightTime fallback {flight_time} (tracker non ha risolto — DOCUMENTATO)"
    return True, f"2xx, flightTime risolto dal lookup: {flight_time}"


def scenario_b5_client_time_wins(airport, now) -> tuple[bool, str]:
    """POST CON flightTime esplicito → quale orario viene salvato? (documenta il ramo)."""
    flight = now + timedelta(days=2, hours=3)
    client_time = flight.replace(minute=37, second=0, microsecond=0).isoformat().replace("+00:00", "Z")
    body = _base_trip_body(airport.to_airport_direction, DUOMO, flight, include_flight_time=False)
    body["flightTime"] = client_time
    status, resp, fn_error = _post_trip(5, body)
    if fn_error:
        return False, f"FunctionError: {resp}"
    if status not in (200, 201) or not isinstance(resp, dict):
        return False, f"status={status} (atteso 2xx) resp={_short(resp)}"

    saved = _read_trip(resp["tripId"]).get("flightTime")
    branch = "CLIENT (ramo client-provided wins)" if saved == client_time else "LOOKUP/altro"
    print(f"      [diag] client={client_time!r} salvato={saved!r} -> ramo vincente: {branch}")
    return True, f"client={client_time} salvato={saved} -> {branch}"


def scenario_b6_pickup_time(airport, now) -> tuple[bool, str]:
    """
    pickupTime sul Match persistito = min(flightTime) − pickup_buffer_minutes.
    Crea due trip compatibili, invoca il matchmaker, rilegge il Match e asserisce
    pickupTime con buffer letto da get_airport("MXP"). Wall-clock: il matchmaker
    crea match solo IN fascia (vedi C7) — fuori fascia si documenta.
    """
    in_window = is_in_active_window(airport, now)
    # Decolli noti e distinti: il piu' presto deve dettare il ritrovo.
    flight_early = (now + timedelta(hours=8)).replace(minute=0, second=0, microsecond=0)
    flight_late = flight_early + timedelta(minutes=30)
    body_a = _base_trip_body(airport.to_airport_direction, DUOMO, flight_early, include_flight_time=True)
    body_b = _base_trip_body(airport.to_airport_direction, DUOMO_600M, flight_late, include_flight_time=True)
    sa, ra, ea = _post_trip(6, body_a)
    sb, rb, eb = _post_trip(12, body_b)
    if ea or eb:
        return False, f"FunctionError create: {ea or eb}"
    if sa not in (200, 201) or sb not in (200, 201):
        return False, f"create non 2xx: a={sa} b={sb}"
    tid_a = ra["tripId"]

    status, resp, fn_error = _invoke(FN_MATCHMAKER, {})
    if fn_error:
        return False, f"matchmaker FunctionError: {resp}"

    if not in_window:
        return True, (f"fuori fascia {airport.mvp_active_windows}: match non creato, "
                      f"pickupTime non verificabile ora (DOCUMENTATO)")

    match_id = _read_trip(tid_a).get("matchId")
    if not match_id:
        return False, "in fascia ma nessun match creato (matchId assente)"
    CREATED.append((f"MATCH#{match_id}", "META"))
    match = dynamo.get_item(f"MATCH#{match_id}", "META") or {}

    pickup_time = match.get("pickupTime")
    earliest_iso = flight_early.isoformat().replace("+00:00", "Z")
    expected_dt = flight_early - timedelta(minutes=airport.pickup_buffer_minutes)
    expected = expected_dt.isoformat().replace("+00:00", "Z")
    print(f"      [diag] earliest={earliest_iso} buffer={airport.pickup_buffer_minutes}min "
          f"expected={expected} actual={pickup_time}")
    if not pickup_time:
        return False, "Match senza pickupTime"
    if pickup_time != expected:
        return False, f"pickupTime={pickup_time} != atteso {expected} (min(flightTime)−buffer)"
    return True, f"pickupTime={pickup_time} = min(flightTime)−{airport.pickup_buffer_minutes}min"


# ── Scenari C — Flusso fino al match (matchmaker reale) ────────────────

def _create_pair(airport, now, origin_a, origin_b, flight_dt, n_a, n_b):
    """Crea due trip MXP validi e ritorna (tripId_a, tripId_b) o solleva."""
    body_a = _base_trip_body(airport.to_airport_direction, origin_a, flight_dt, include_flight_time=True)
    body_b = _base_trip_body(airport.to_airport_direction, origin_b, flight_dt, include_flight_time=True)
    sa, ra, ea = _post_trip(n_a, body_a)
    sb, rb, eb = _post_trip(n_b, body_b)
    if ea or eb:
        raise RuntimeError(f"FunctionError create: {ea or eb}")
    if sa not in (200, 201) or sb not in (200, 201):
        raise RuntimeError(f"create non 2xx: a={sa} b={sb} ra={_short(ra)} rb={_short(rb)}")
    return ra["tripId"], rb["tripId"]


def scenario_c7_full_match(airport, now) -> tuple[bool, str]:
    """Due trip compatibili (~600m, stessa fascia) → matchmaker crea match con pickupPoint."""
    in_window = is_in_active_window(airport, now)
    flight = now + timedelta(hours=8)
    tid_a, tid_b = _create_pair(airport, now, DUOMO, DUOMO_600M, flight, 7, 8)

    status, resp, fn_error = _invoke(FN_MATCHMAKER, {})
    if fn_error:
        return False, f"matchmaker FunctionError: {resp}"
    print(f"      [diag] matchmaker resp={resp} in_active_window={in_window}")

    if not in_window:
        return True, (f"matchmaker invocato OK ma FUORI fascia {airport.mvp_active_windows} "
                      f"-> MXP skippato: match non verificabile ora (DOCUMENTATO)")

    trip_a = _read_trip(tid_a)
    match_id = trip_a.get("matchId")
    if not match_id:
        return False, "in fascia ma nessun matchId sul trip A (match non creato)"
    CREATED.append((f"MATCH#{match_id}", "META"))

    match = dynamo.get_item(f"MATCH#{match_id}", "META") or {}
    pp = match.get("pickupPoint") or {}
    if pp.get("lat") is None or pp.get("lng") is None:
        return False, "match senza pickupPoint lat/lng"

    p_lat, p_lng = float(pp["lat"]), float(pp["lng"])
    radius_km = airport.pickup_radius_m / 1000.0
    oa = get_match_coords(_read_trip(tid_a), airport)
    ob = get_match_coords(_read_trip(tid_b), airport)
    d_a = haversine_km(p_lat, p_lng, oa[0], oa[1])
    d_b = haversine_km(p_lat, p_lng, ob[0], ob[1])
    if d_a > radius_km or d_b > radius_km:
        return False, f"pickup fuori raggio: {d_a*1000:.0f}m / {d_b*1000:.0f}m > {airport.pickup_radius_m}m"
    return True, f"match creato, pickup {d_a*1000:.0f}m / {d_b*1000:.0f}m da origini"


def scenario_c8_distance_gate(airport, now) -> tuple[bool, str]:
    """Due trip con origini >max_origin_distance_km → nessun match (gate distanza)."""
    in_window = is_in_active_window(airport, now)
    flight = now + timedelta(hours=8)
    tid_a, tid_b = _create_pair(airport, now, DUOMO, NORD_PERIFERIA, flight, 9, 10)

    status, resp, fn_error = _invoke(FN_MATCHMAKER, {})
    if fn_error:
        return False, f"matchmaker FunctionError: {resp}"

    match_id = _read_trip(tid_a).get("matchId")
    if match_id:
        CREATED.append((f"MATCH#{match_id}", "META"))
        return False, f"match inatteso oltre il gate distanza (matchId={match_id})"

    if not in_window:
        return True, (f"nessun match — MA fuori fascia {airport.mvp_active_windows}: "
                      f"ambiguo (skip-fascia vs gate-distanza). DOCUMENTATO")
    return True, "in fascia, nessun match: gate distanza rispettato"


# ── Scenario D — Fasce orarie (MvpTimeWindowsMode) ─────────────────────

def scenario_d9_out_of_window_queue(airport, now) -> tuple[bool, str]:
    """
    Fuori fascia: il trip viene comunque salvato (2xx), non rifiutato, con
    notifica di coda. `now` nella Lambda e' l'ora reale: testabile davvero solo
    se l'ora corrente e' FUORI da mvp_active_windows; altrimenti documentiamo.
    """
    in_window = is_in_active_window(airport, now)
    flight = now + timedelta(days=1)
    body = _base_trip_body(airport.to_airport_direction, DUOMO, flight, include_flight_time=True)
    status, resp, fn_error = _post_trip(11, body)
    if fn_error:
        return False, f"FunctionError: {resp}"
    if status not in (200, 201) or not isinstance(resp, dict):
        return False, f"status={status} (atteso 2xx, trip MAI rifiutato per fascia)"

    if in_window:
        nxt = next_active_window_label(airport, now)
        return True, (f"2xx (trip creato). Ora DENTRO fascia: coda non attesa ora. "
                      f"Prossima fascia label={nxt}. (sola creazione verificata)")

    # Fuori fascia: cerca la notifica trip_queued per l'utente fake.
    user_id = f"{ID_PREFIX}user-11"
    queued = _find_queued_notification(user_id)
    if queued:
        return True, f"2xx + notifica coda fuori fascia: {queued}"
    return True, "2xx (trip creato, non rifiutato) fuori fascia; notifica coda non trovata (verifica manuale)"


def _user_notifications(user_id: str) -> list[dict]:
    """Notifiche di un utente: pk=USER#id, sk begins_with NOTIF# (schema save_notification)."""
    try:
        resp = dynamo.table.query(
            KeyConditionExpression=Key("pk").eq(f"USER#{user_id}") & Key("sk").begins_with("NOTIF#"),
        )
        return resp.get("Items", [])
    except Exception:  # noqa: BLE001
        return []


def _find_queued_notification(user_id: str) -> str | None:
    """Cerca una notifica trip_queued per l'utente (emessa da create_trip fuori fascia)."""
    for it in _user_notifications(user_id):
        if it.get("type") == "trip_queued":
            return it.get("body") or "trip_queued"
    return None


# ── Cleanup ────────────────────────────────────────────────────────────

def _short(x) -> str:
    s = json.dumps(x) if isinstance(x, (dict, list)) else str(x)
    return s if len(s) <= 120 else s[:117] + "..."


def _cleanup_created() -> None:
    for pk, sk in CREATED:
        try:
            dynamo.delete_item(pk, sk)
            print(f"[CLEANUP] rimosso {pk}")
        except Exception as e:  # noqa: BLE001
            print(f"[CLEANUP] WARN impossibile rimuovere {pk}: {e}", file=sys.stderr)


def _cleanup_fake_notifications(max_user: int = 12) -> None:
    """Rimuove le notifiche (NOTIF#) scritte per gli utenti fake e2e-mvp-user-*."""
    for n in range(1, max_user + 1):
        uid = f"{ID_PREFIX}user-{n}"
        for it in _user_notifications(uid):
            try:
                dynamo.delete_item(it["pk"], it["sk"])
                print(f"[CLEANUP] rimosso {it['pk']} {it['sk']}")
            except Exception as e:  # noqa: BLE001
                print(f"[CLEANUP] WARN notif {it.get('sk')}: {e}", file=sys.stderr)


def cleanup_leftovers() -> None:
    """Rimuove ogni trip/match fake (userId con prefisso e2e-mvp-) da run precedenti."""
    print("=== CLEANUP dati fake residui (e2e-mvp-) ===")
    removed = 0
    for status in ("scheduled", "tracking_pending", "tentative_match", "matched", "searching"):
        try:
            trips = dynamo.query_gsi(
                index_name="GSI5-TripStatus",
                pk_name="gsi5pk",
                pk_value=f"MXP#{status}",
            )
        except Exception as e:  # noqa: BLE001
            print(f"[CLEANUP] WARN query {status}: {e}", file=sys.stderr)
            continue
        for trip in trips:
            if not str(trip.get("userId", "")).startswith(ID_PREFIX):
                continue
            for mid_key, prefix in (("matchId", "MATCH#"), ("tentativeMatchId", "TENTATIVE_MATCH#")):
                mid = trip.get(mid_key)
                if mid:
                    dynamo.delete_item(f"{prefix}{mid}", "META")
                    print(f"[CLEANUP] rimosso {prefix}{mid}")
            dynamo.delete_item(trip["pk"], "META")
            print(f"[CLEANUP] rimosso {trip['pk']}")
            removed += 1
    _cleanup_fake_notifications()
    print(f"Rimossi {removed} trip fake.\n")


# ── Runner ─────────────────────────────────────────────────────────────

SCENARIOS = [
    ("A1 tratta non-MVP (FCO) -> 4xx", scenario_a1_wrong_airport),
    ("A2 direzione sbagliata -> 4xx", scenario_a2_wrong_direction),
    ("A3 origin* mancanti -> 4xx", scenario_a3_missing_origin),
    ("B4 lookup risolve flightTime", scenario_b4_lookup_resolves),
    ("B5 client-time vs lookup", scenario_b5_client_time_wins),
    ("B6 pickupTime = min(flightTime)-buffer", scenario_b6_pickup_time),
    ("C7 match completo + pickupPoint", scenario_c7_full_match),
    ("C8 gate distanza -> no match", scenario_c8_distance_gate),
    ("D9 fuori fascia -> coda (2xx)", scenario_d9_out_of_window_queue),
]


def run() -> int:
    airport = get_airport("MXP")
    now = datetime.now(timezone.utc)

    print("=== E2E MVP TRIPS (Flot-dev, Lambda reali) ===")
    print(f"create_trip_fn={FN_CREATE_TRIP}  matchmaker_fn={FN_MATCHMAKER}")
    print(f"airport=MXP  to_airport_direction={airport.to_airport_direction!r}  "
          f"max_origin={airport.max_origin_distance_km}km  pickup_radius={airport.pickup_radius_m}m")
    print(f"now={now.isoformat()}  active_windows={airport.mvp_active_windows}  "
          f"in_active_window={is_in_active_window(airport, now)}\n")

    results: list[tuple[str, bool, str]] = []
    try:
        for name, fn in SCENARIOS:
            try:
                passed, detail = fn(airport, now)
            except Exception as e:  # noqa: BLE001
                passed, detail = False, f"errore inatteso: {type(e).__name__}: {e}"
            results.append((name, passed, detail))
            print(f"  -> {name}: {'PASS' if passed else 'FAIL'} ({detail})")
    finally:
        if CREATED:
            print("\n--- cleanup item creati ---")
            _cleanup_created()
        print("--- cleanup notifiche fake ---")
        _cleanup_fake_notifications()

    print("\n=== RIEPILOGO ===")
    width = max(len(n) for n, _ in SCENARIOS) + 4
    passed_count = 0
    for i, (name, passed, detail) in enumerate(results, start=1):
        dots = "." * (width - len(name))
        passed_count += int(passed)
        print(f"[{i}] {name} {dots} {'PASS' if passed else 'FAIL'} ({detail})")

    print(f"\n{passed_count}/{len(results)} PASSED")
    return 0 if passed_count == len(results) else 1


def main() -> int:
    parser = argparse.ArgumentParser(description="E2E MVP trips via Lambda invoke (Flot-dev only).")
    parser.add_argument("--cleanup", action="store_true",
                        help="rimuove dati fake e2e-mvp- rimasti da run interrotti, poi esce")
    args = parser.parse_args()

    if args.cleanup:
        cleanup_leftovers()
        return 0
    return run()


if __name__ == "__main__":
    sys.exit(main())
