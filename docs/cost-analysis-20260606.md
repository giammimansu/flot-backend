# Flot — Verifica costi sotto carico (P2 #13)

> Data: 6 Giugno 2026 · Modello: `scripts/cost_model.py` · Target: **< $50/mese a volume MVP**

## Metodo

Costo stimato con un **modello parametrico esplicito** (`scripts/cost_model.py`),
non a spanne. Ogni assunzione è una costante nominata e sovrascrivibile; il
breakdown è per-servizio. È un *modello*, non una misura: la fattura reale dipende
da dimensione dei payload, letture su GSI con projection `ALL`, e mix di richieste.
I numeri sono volutamente **conservativi** (arrotondati per eccesso).

Pricing: eu-south-1 (Milano), on-demand, listino pubblico approssimato inizio-2026.

## Scenario (dal piano #13)

| Parametro | Valore |
|-----------|--------|
| Trip/giorno | 100 (3.000/mese) |
| Matchmaker | ogni 5 min (24h) |
| Flight Tracker | ogni 15 min |
| Dissolve checker | ogni 1h |
| API calls / trip | 20 |
| WS messages / match | 40 |
| Match rate | 60% |
| Notifiche / trip | 4 (50% email fallback) |

## Risultato

```
Service         Detail                                    USD/mo
--------------------------------------------------------------------
CloudWatch      12 metrics, 43,560 puts                     4.04
S3+CloudFront   profile photos (free-tier bound)            1.00
SES email       6,000 emails                                0.60
DynamoDB        43,200 WRU, 435,600 RRU                     0.41
API GW REST     60,000 requests                             0.21
Lambda          126,240 inv, 7,890 GB-s                     0.13
API GW WS       36,000 msgs, 90,000 conn-min                0.06
EventBridge     18,000 events                               0.02
SNS push        6,000 pushes (1M free)                      0.00
--------------------------------------------------------------------
TOTAL                                                       6.47
```

**Esito: ~$6.47/mese → PASS.** Headroom ~$43.5 sul target di $50.

## Colli di bottiglia (ordine di ottimizzazione)

1. **CloudWatch ($4.04, 62% del totale)** — dominato dai **custom metrics**
   ($0.30/metrica/mese × 12) e dalle `PutMetricData` del Matchmaker (4 put × 8.640
   run/mese). Driver #1 non-banale.
   - *Ottimizzazione*: usare **EMF (Embedded Metric Format)** via Powertools
     invece di `PutMetricData` diretto — le metriche viaggiano nei log già
     pagati, azzerando il costo `PutMetricData`. Ridurre i custom metric a quelli
     con un alarm/dashboard reale.
2. **S3 + CloudFront ($1.00)** — voce piatta, in pratica entro il free tier a
   volume MVP. Nessuna azione finché il volume foto non cresce.
3. **SES ($0.60)** — lineare col fan-out email. La dedup cross-canale di #7
   (WS/Push consegnato → niente email) già la limita.

## Note di scalabilità

- **DynamoDB** resta trascurabile ($0.41) grazie a on-demand + TTL (chat 48h,
  conn 24h, notif 30g) che tiene piccola la storage. Il rischio reale è la
  **dimensione delle letture pool su GSI5** (projection `ALL`): a pool grandi,
  valutare projection `KEYS_ONLY` + batch get mirato.
- **Lambda** quasi gratis ($0.13): arm64 + durate brevi. Il Matchmaker ogni 5 min
  è il maggior contributore di invocazioni ma costa pochissimo.
- A **10×** il volume (1.000 trip/giorno) il totale stimato resta < $50/mese:
  i costi dominanti (CloudWatch metriche) sono **fissi**, non lineari sul volume.

## Riproduzione

```bash
python scripts/cost_model.py
```

Modificare `Scenario` / `Prices` nel file per what-if (es. `trips_per_day=1000`).
