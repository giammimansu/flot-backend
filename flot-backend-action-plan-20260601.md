# Flot — Backend Action Plan

> Ultimo aggiornamento: 2 Giugno 2026
> Versione backend attuale: v4 Elastic & Predictive + Smart Auto-Capture (Payment Deadlock)
> Sessione 2 Giugno (mattina): P0 completato (#1 già esistente, #2 #3 #4 #5 implementati e committati)
> Sessione 2 Giugno (pomeriggio): P1 completato (#6 #7 #8 #9 implementati e committati)

La logica di priorità è **operativa, non tematica**: prima rendere affidabile e chiudibile ciò che esiste, poi sbloccare il go-live a pagamento, infine crescita e difesa.

---

## P0 — Bloccanti (prerequisito per spegnere il Fake Door)

Senza questi, non è sicuro passare a pagamenti reali.

---

### #1 — Handler `trip.completed`

**Stato**: ✅ COMPLETATO (pre-esistente — piano stale)
**Dipendenze**: nessuna
**Sblocca**: #6 (chat TTL), #11 (rating)

Il nodo a maggior leva del progetto: è citato ovunque (`trip.completed → "request review"`, `chat TTL 48h dopo completed`) ma l'handler non esiste.

**Cosa deve fare:**
- Emettere l'evento `trip.completed` (trigger: entrambi i trip nello stesso match superano il `flightTime` + tolleranza)
- Settare il TTL DynamoDB sui `ChatMessage` del match (48h da `completedAt`)
- Emettere hook "request review" (usato da #11 in futuro)
- Gestire il caso "match mai completato" (trip scaduto, utente no-show)

**File da creare/modificare:**
- `src/handlers/events/on_trip_completed.py` — nuovo handler
- `src/handlers/matching/matchmaker.py` — trigger completion check
- `template.yaml` — nuova Lambda + EventBridge rule

---

### #2 — Hardening flusso Stripe reale

**Stato**: ✅ COMPLETATO (02/06/2026)
**Dipendenze**: nessuna

`FAKE_DOOR_MODE` cortocircuita l'intero flusso pagamenti. Prima di toglierlo, i seguenti percorsi vanno testati con Stripe in modalità test reale (non mock):

- **Doppio capture simultaneo**: due utenti premono "Unlock" in contemporanea — solo uno dei due `PaymentIntent` deve essere catturato per primo, poi l'altro
- **Void su PI scaduto**: se il `PaymentIntent` del primo unlock è già scaduto quando arriva il secondo, il void deve riuscire senza errori bloccanti
- **Webhook duplicati / fuori ordine**: `payment_intent.amount_capturable_updated` ricevuto due volte — la deduplica per `eventId` in DynamoDB deve reggere
- **Capture fallito sul secondo PI**: se il capture del secondo PI fallisce, il primo deve essere annullato (no charge a nessuno)

**File da modificare:**
- `src/handlers/payments/unlock_match.py`
- `src/handlers/webhooks/stripe_webhook.py`
- `tests/integration/test_stripe_flow.py` — nuovo file

---

### #3 — State machine esplicita del lifecycle Trip/Match

**Stato**: ✅ COMPLETATO (02/06/2026)
**Dipendenze**: #2

Il ciclo è cresciuto molto:

```
Trip:   scheduled → tentative_match → matched → partially_unlocked_wait → completed / expired
Match:  pending → partially_unlocked → unlocked → unlock_expired → dissolved
```

Senza un punto unico di validazione, transizioni illegali (es. `unlock_expired → unlocked`) possono passare silenziosamente.

**Cosa implementare:**
- Classe `TripStateMachine` e `MatchStateMachine` con metodo `transition(from, to)` che solleva `InvalidTransitionError` se la coppia non è permessa
- Tutti gli handler che aggiornano `status` devono passare da qui
- Nessun `update_item` con `SET status = :x` senza passare dalla state machine

**File da creare/modificare:**
- `src/lib/state_machine.py` — nuovo modulo
- Tutti gli handler che toccano `status` (circa 8 file)
- `tests/unit/test_state_machine.py`

---

### #4 — Integration test concorrenza Matchmaker

**Stato**: ✅ COMPLETATO (02/06/2026) — trovato e fixato race condition in create_tentative_match
**Dipendenze**: #3

`optimize_pool` con dissolve/replace dei TentativeMatch è il punto più esposto a race condition. I test unitari attuali non coprono run paralleli reali.

**Scenari da testare:**
- Due run paralleli sul medesimo pool → nessun TentativeMatch duplicato
- Dissolve di un TentativeMatch mentre il lock window lo sta promuovendo a definitivo
- Pool con N trip dispari → nessun trip viene abbinato due volte

**File da creare:**
- `tests/integration/test_matchmaker_concurrency.py`
- Setup DynamoDB locale (DynamoDB Local via Docker) per i test di concorrenza

---

### #5 — Failover Flight Tracker

**Stato**: ✅ COMPLETATO (02/06/2026)
**Dipendenze**: nessuna

Il fallback attuale al `flightTime` statico azzera il vantaggio predittivo della v4. Il circuit breaker (3 fail → 30 min blackout) è corretto come protezione ma non come resilienza.

**Cosa implementare:**
- Secondo provider (`flightaware` o `fr24`) con tentativo in cascata se il primo fallisce
- Degradazione non azzerante: se entrambi i provider falliscono, usare `flightTime` statico ma segnalarlo in `trip.trackingStatus = "degraded"` (non escludere il trip dal matching)
- Alerting CloudWatch quando il circuit breaker apre (oggi è solo un log)

**File da modificare:**
- `src/lib/flight_tracker.py` — aggiungere provider secondario + strategia failover
- `src/lib/airports.py` — aggiungere `flight_tracker_fallback_provider` ad `AirportConfig`
- `template.yaml` — nuova env var `FLIGHT_TRACKER_FALLBACK_PROVIDER`

---

## P1 — Necessari per il go-live a pagamento

Servono per operare davvero, ma non bloccano l'hardening di P0.

---

### #6 — Completare la chat interna

**Stato**: ✅ COMPLETATO (02/06/2026)
**Dipendenze**: #1 (per il TTL trigger)

La chat non è da sviluppare da zero — WebSocket relay, storage `ChatMessage`, endpoint `GET /matches/:matchId/chat` sono già nel design. I pezzi mancanti:

- **TTL trigger**: dipende da #1 — senza `trip.completed` i messaggi non vengono mai eliminati
- **Messaggi di sistema** (`type: "system"`): chi genera "match confermato", "partner ha sbloccato", "chat scadrà tra 2 ore"? Manca il producer
- **Delivery offline**: oggi il WebSocket fa relay solo se entrambi connessi. Decidere se inviare una push notification per ogni nuovo messaggio chat quando il destinatario è offline (consigliato: sì, ma throttled)

**File da creare/modificare:**
- `src/handlers/events/on_trip_completed.py` — già in #1, aggiunge TTL setter
- `src/handlers/chat/system_message.py` — nuovo modulo per messaggi di sistema
- `src/handlers/websocket/message.py` — aggiungere fallback push quando offline

---

### #7 — Notifiche multi-canale verificate end-to-end

**Stato**: ✅ COMPLETATO (02/06/2026)
**Dipendenze**: nessuna

Push (SNS/FCM), email (SES) e WebSocket sono specificati ma non verificati insieme nella pipeline completa: registrazione token → match trovato → utente offline → push → email fallback.

**Cosa testare/implementare:**
- Registrazione token FCM (`PUT /users/me/push-token`) e persistenza in DynamoDB
- Fallback chain: WebSocket (se connesso) → Push (se token presente) → Email (sempre)
- Deduplica cross-canale: stesso evento non deve generare push + email se il WS ha già consegnato
- Test su token scaduto / non valido (SNS deve silently skip, non rompere il flusso)

**File da creare/modificare:**
- `src/lib/notifications.py` — refactor con fallback chain esplicita
- `tests/integration/test_notification_pipeline.py`

---

### #8 — Osservabilità di business

**Stato**: ✅ COMPLETATO (02/06/2026)
**Dipendenze**: nessuna

Le metriche tecniche (`TentativeMatchesCreated`, `UnlockTimeouts`) esistono. Mancano le metriche che dicono se il prodotto funziona:

| Metrica | Come | Perché |
|---------|------|--------|
| Match rate (trip matched / trip created) | CloudWatch custom metric | Validare l'efficacia del Matchmaker |
| Latency scheduled→match (minuti) | CloudWatch Histogram | Capire quanto aspettano gli utenti |
| Deadlock resolution rate (entrambi unlocked / match created) | CloudWatch custom metric | Misurare l'efficacia dello Smart Auto-Capture |
| Pool fill rate per time bucket | DynamoDB Streams → Lambda | Identificare bucket "vuoti" per airport |
| Trip expired senza match (%) | EventBridge `trip.expired` counter | Cold start indicator |

**File da creare:**
- `src/lib/metrics.py` — extend con metriche di funnel
- `src/handlers/events/on_trip_expired.py` — metric publisher
- CloudWatch Dashboard as Code in `template.yaml`

---

### #9 — Admin & ops tooling minimo

**Stato**: ✅ COMPLETATO (02/06/2026)
**Dipendenze**: #2, #3

In produzione, il primo match incagliato o il primo pagamento anomalo richiederà intervento manuale. Serve un set minimo di endpoint operativi (protetti da IAM, non da Cognito user).

**Endpoint da sviluppare:**
```
POST /admin/matches/:matchId/void         → Forza void PaymentIntent + dissolve match
POST /admin/trips/:tripId/repool          → Rimette manualmente un trip in scheduled
GET  /admin/matches/:matchId/inspect      → Stato completo match (tutti i campi, non filtrati)
POST /admin/flights/:tripId/refresh-eta  → Forza re-fetch ETA dal flight tracker
```

**File da creare:**
- `src/handlers/admin/` — nuova cartella con un handler per endpoint
- `template.yaml` — API Gateway con authorizer IAM separato per `/admin/*`

---

## P2 — Crescita e difesa (post go-live)

Da fare quando hai dati reali e volume sufficiente.

---

### #10 — Penalità / reputazione anti-no-show

**Stato**: da sviluppare
**Dipendenze**: #1, #8

`previousMatchPartners` evita il loop con lo stesso partner ma non difende da comportamenti sistematici: utenti che non sbloccano mai, account multipli, no-show ripetuti.

**Da implementare:**
- Campo `trustScore` su entity `User` (float 0.0–1.0, default 1.0)
- Decremento automatico su: unlock_expired non-payer, trip.completed senza presenza confermata
- Trip con `trustScore < threshold` esclusi dal matching (configurabile per aeroporto)
- Hard ban dopo N violazioni (campo `banned: true`, check in `create_trip`)

---

### #11 — Sistema di rating a stelle

**Stato**: quasi da zero (solo hook accennato)
**Dipendenze**: #1, #10

Ha senso solo con volume di match completati sufficiente a generare reputazione. Si appende all'handler `trip.completed` di #1.

**Entity da aggiungere:**
```
Review
  PK: USER#<reviewedUserId>
  SK: REVIEW#<matchId>
  reviewerId, rating (1-5), comment?, createdAt
```

**Endpoint da sviluppare:**
```
POST /matches/:matchId/review    → Crea review post-completion
GET  /users/:userId/rating       → Rating medio pubblico
```

**Regole:**
- Una review per utente per match (idempotente)
- Review disponibile solo dopo `trip.completed` e solo entro 48h
- Rating visibile sul profilo partner nella schermata Connection Unlocked

---

### #12 — Onboarding secondo aeroporto reale

**Stato**: architettura pronta, mai testato
**Dipendenze**: nessuna bloccante

L'architettura è multi-airport "da day 1" ma esiste solo MXP. Onboardare un secondo aeroporto (es. FCO Roma Fiumicino) verifica che nulla sia hardcodato fuori da `airports.py`.

**Checklist:**
- [ ] Aggiungere `FCO` in `airports.py` con tutti i campi richiesti
- [ ] Verificare che tutti i GSI query siano scoped per `airportCode` (nessuna query cross-airport)
- [ ] Test E2E: trip MXP e trip FCO non si matchano mai
- [ ] Frontend: Airport Picker con almeno due aeroporti attivi

---

### #13 — Verifica costi sotto carico

**Stato**: stimato, mai misurato
**Dipendenze**: #4, #8

Il target è **< $50/mese a volume MVP**. Va validato con carico simulato, non solo stimato.

**Scenario di test:**
- 100 trip/giorno distribuiti su 16 ore
- Matchmaker ogni 5 min (288 invocazioni/giorno)
- Flight Tracker ogni 15 min per trip attivi
- Picco: 20 trip nella stessa ora, stesso aeroporto

**Output atteso:**
- Breakdown costi per servizio (Lambda, DynamoDB, EventBridge, SNS, SES)
- Identificazione colli di bottiglia (letture GSI, dimensione payload WS)
- Piano di ottimizzazione se si supera il target

---

## Riepilogo per priorità

| # | Feature | Priorità | Stato | Dipende da |
|---|---------|----------|-------|------------|
| 1 | Handler `trip.completed` | P0 | ✅ Completato | — |
| 2 | Hardening flusso Stripe | P0 | ✅ Completato | — |
| 3 | State machine lifecycle | P0 | ✅ Completato | #2 |
| 4 | Integration test concorrenza Matchmaker | P0 | ✅ Completato | #3 |
| 5 | Failover Flight Tracker | P0 | ✅ Completato | — |
| 6 | Completare chat interna | P1 | ✅ Completato | #1 |
| 7 | Notifiche multi-canale E2E | P1 | ✅ Completato | — |
| 8 | Osservabilità di business | P1 | ✅ Completato | — |
| 9 | Admin & ops tooling | P1 | ✅ Completato | #2, #3 |
| 10 | Penalità anti-no-show | P2 | Da sviluppare | #1, #8 |
| 11 | Sistema di rating | P2 | Da sviluppare | #1, #10 |
| 12 | Onboarding secondo aeroporto | P2 | Architettura pronta | — |
| 13 | Verifica costi sotto carico | P2 | Stimato | #4, #8 |

---

*Flot Backend Action Plan — Giugno 2026*
