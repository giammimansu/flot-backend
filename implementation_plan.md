# Implementazione Flot Dual-Mode Matching Engine

Questo documento illustra il piano di implementazione in 7 step per introdurre il supporto alle due modalità di matching: **Scheduled** e **Live**, basandosi sull'algoritmo GPS-based precedentemente implementato e sulle nuove regole fornite.

## User Review Required

> [!IMPORTANT]
> **Nessuna modifica al template.yaml per GSI1.** Tuttavia, per quanto riguarda le notifiche e la comunicazione con EventBridge, potrei aver bisogno di toccare `template.yaml` se le logiche di pattern matching cambiano `detail-type` in `trip.created.live` e `trip.created.scheduled`. Qualora il routing della regola esistente per `OnTripCreatedFunction` non permetta il catch condizionale dei due nuovi eventi, ti chiederò la conferma di allargare tali pattern, a meno di non emettere lo standard `trip.created` passando il discriminator in `detail.mode`. Procederò come da prompt mantenendo le modifiche a DynamoDB/SAM isolate dove richiesto.

## Proposed Changes

### 1. `src/lib/airports.py` (Configurazione)
Aggiorneremo `AirportConfig` eliminando `search_timeout_sec` (obsoleto) a favore dei costanti separati in millisecondi/ore sia per modalità Live (`live_search_timeout_sec`, `live_pool_ttl_sec`, `live_max_age_sec`) sia per Scheduled (`scheduled_slot_duration_min`, `scheduled_match_window_hours`, `scheduled_pool_ttl_hours`).

### 2. `src/lib/validation.py` (Validazione Input)
Estenderemo il modello `TripCreate` di Pydantic introducendo un enum `TripMode` e definendo sia `mode` che l'eventuale `arrivalSlot`. La `@model_validator` assicurerà che in mode=scheduled il campo `arrivalSlot` esista sempre, mentre in mode=live non sia valorizzato.

### 3. `src/handlers/trips/create_trip.py` (Persistenza e Routing)
Aggiusteremo il calcolo del TTL DynamoDB (`expiresAt`) a seconda della logica `mode`. I trip di origine Live scadranno dopo la finestra temporale `live_pool_ttl_sec`. Quelli Scheduled scaleranno basandosi sul loro `arrivalSlot` + TTL per la pianificazione in anticipo. Aggiorneremo anche l'emissione eventi EventBridge (`trip.created.live` vs `trip.created.scheduled`). 

### 4. `src/lib/matching.py` (Core Engine Modificata)
Sostituiremo il coefficiente temporale (`time_score`) integrandolo in logica binaria di fascia (`get_slot_bucket`, `get_adjacent_slots`). 
I punteggi per valigia (`luggage_score`) scaleranno per comodità o limite di passeggeri. Introdurremo il filtering rigido `can_match_modes` (verificardo se un Trip live rientra a ±30 min dallo Scheduled) e controlleremo l'anzianità `is_trip_too_old`. Il totale `compute_match_score` diventerà puramente distanziale, bagagli e profilo.

### 5. `src/handlers/trips/search_trips.py` (Query Algoritmica)
Aggiorneremo il path di logica condizionale delle query DynamoDB e Python post-filtering. 
- Se trip **Live**: cercheremo su `GSI1-TimeBucket` i Trip `#live`, in combinata a intercettatori di fascie `#scheduled#<slot>` limitrofi e sovrapposti all'istante current-time + tolleranza.
- Se trip **Scheduled**: pescheremo nel nostro bucket scheduled +/-1 slot adiacente e aggiungeremo le verifiche sui `#live` già esistenti per completare il radar.
Alla fine vi saranno i check Python (distanza, direction, age, cross-mode limits, coordinate, threshold).

### 6. Logica Eventi Asincrona ed Esiti (EventBridge)
Implementeremo il controller condiviso `on_trip_created.py` per triggare il job di matching e salvare se esite. Per l'appraisal finale preposto in `on_match_found.py`, verificheremo lo stato dei party per decidere se innescare il socket al momento (rispondendo a Live) oppure se accodare la push notification e mandare messaggi email (rispondendo a differite degli Scheduled). 

### 7. Unit Testing (Aggiornamento `tests/unit/test_matching.py`)
Includeremo i test unitari menzionati dal prompt per coprire i comportamenti `haversine_km`, slot generation `get_slot_bucket`, i nuovi coefficienti delle valige, limiti di superamento validi `is_trip_too_old`, la correlabilità modale in `can_match_modes` e la nuova soglia di scoring finale > `match_threshold`.

## Open Questions

- Esiste un constraint stringente lato frontend per le modifiche sulla signature dei messaggi JSON emessi via WebSocket nel caso di fallimento `no_match_live`? Oppure seguiamo strettamente i flussi proposti dove non viene fatta ulteriore segnalazione finché restano nel pool?

## Verification Plan
1. Esecuzione dei test globali e `test_matching.py` unitari.
2. Controllare se `sam build` riesce correttamente.
3. Se necessario lanciare SAM locale o check su integrità.
