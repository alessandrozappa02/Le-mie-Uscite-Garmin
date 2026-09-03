"""
Le mie uscite — bici e wing foil/windsurf, con dati presi da Garmin Connect.

IMPORTANTE: prima di usare questa app, esegui una volta "garmin_login.py"
dal terminale per autenticarti (vedi le istruzioni ricevute in chat).
Questa app NON chiede email o password: usa la sessione salvata da quello
script.

Navigazione a 3 livelli:
1. Home: un bottone per ogni sport praticato
2. Elenco: tutte le attività di quello sport
3. Dettaglio: tutti i dati tecnici di una singola attività, con mappa del
   percorso e (per windsurf/wing foil) conteggio delle strambate riuscite
"""

import base64
import bisect
import math
import os
from datetime import date, timedelta

import pandas as pd
import streamlit as st
from garminconnect import Garmin

st.set_page_config(page_title="Le mie uscite", page_icon="🚴", layout="wide")

CARTELLA_TOKEN = os.path.expanduser("~/.garminconnect")


def prepara_token_da_secrets():
    """Se l'app gira online (container "vuoto", senza il file di sessione
    già presente sul disco) e nei Secrets di Streamlit è stato configurato
    GARMIN_TOKENS_B64, ricrea il file di sessione al volo. Così l'app può
    collegarsi a Garmin senza bisogno di rifare il login manuale a ogni
    riavvio del server online. In locale (dove il file esiste già grazie a
    garmin_login.py) questa funzione non fa nulla.
    """
    file_token = os.path.join(CARTELLA_TOKEN, "garmin_tokens.json")
    if os.path.isfile(file_token):
        return
    try:
        token_b64 = st.secrets.get("GARMIN_TOKENS_B64")
    except Exception:
        token_b64 = None
    if not token_b64:
        return
    os.makedirs(CARTELLA_TOKEN, exist_ok=True)
    with open(file_token, "wb") as f:
        f.write(base64.b64decode(token_b64))


def richiede_password():
    """Se nei Secrets è configurata APP_PASSWORD (uso online), la richiede
    prima di mostrare qualunque dato personale. In locale, senza Secrets
    configurati, non chiede nulla e passa diretto."""
    try:
        password_richiesta = st.secrets.get("APP_PASSWORD")
    except Exception:
        password_richiesta = None

    if not password_richiesta:
        return True

    if st.session_state.get("autenticato"):
        return True

    st.title("🔒 Accesso privato")
    password_inserita = st.text_input("Password", type="password")
    if st.button("Entra"):
        if password_inserita == password_richiesta:
            st.session_state.autenticato = True
            st.rerun()
        else:
            st.error("Password sbagliata.")
    return False


prepara_token_da_secrets()

if not richiede_password():
    st.stop()

# Etichette e icone più leggibili per i tipi di sport più comuni.
# Se Garmin restituisce un tipo non presente qui, viene comunque mostrato
# (solo senza icona dedicata), quindi non c'è bisogno di conoscerli tutti.
ICONE_SPORT = {
    "cycling": "🚴",
    "road_biking": "🚴",
    "mountain_biking": "🚵",
    "gravel_cycling": "🚴",
    "indoor_cycling": "🚴",
    "e_bike_fitness": "🚴‍♂️",
    "e_bike_mountain": "🚵",
    "wingfoil_v2": "🪁",
    "wingfoiling": "🪁",
    "wind_surfing": "🏄",
    "windsurfing": "🏄",
    "kiteboarding": "🪁",
    "running": "🏃",
    "walking": "🚶",
    "hiking": "🥾",
    "swimming": "🏊",
}

# Parole che, se presenti nel tipo di attività, indicano uno sport di
# vela/foil per cui ha senso calcolare le strambate.
PAROLE_CHIAVE_VELA_FOIL = ["wind", "wing", "surf", "foil", "kite"]


def trova_valore(dati, *chiavi_possibili):
    """Cerca uno o più nomi di campo dentro un dizionario, anche se annidato
    a qualche livello di profondità, e restituisce il primo valore trovato
    non nullo.

    Garmin non struttura sempre i dati allo stesso modo per tutti gli sport
    (es. per il windsurf alcuni campi come la velocità massima possono
    trovarsi "più in profondità" rispetto alla bici), quindi cerchiamo
    ovunque invece di assumere sempre la stessa posizione fissa.
    """
    if not isinstance(dati, dict):
        return None
    for chiave in chiavi_possibili:
        valore = dati.get(chiave)
        if valore is not None:
            return valore
    for valore in dati.values():
        if isinstance(valore, dict):
            trovato = trova_valore(valore, *chiavi_possibili)
            if trovato is not None:
                return trovato
        elif isinstance(valore, list):
            for elemento in valore:
                if isinstance(elemento, dict):
                    trovato = trova_valore(elemento, *chiavi_possibili)
                    if trovato is not None:
                        return trovato
    return None


# ---------------------------------------------------------------------
# Geometria: distanza e rotta tra due punti GPS (usate per il tracciato
# e per il conteggio delle strambate)
# ---------------------------------------------------------------------

def distanza_metri(lat1, lon1, lat2, lon2):
    """Distanza in metri tra due coordinate GPS (formula dell'emisenoverso)."""
    raggio_terra = 6371000
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlambda / 2) ** 2
    return 2 * raggio_terra * math.asin(math.sqrt(a))


def rotta_gradi(lat1, lon1, lat2, lon2):
    """Direzione (0-360°) andando dal primo al secondo punto."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dlambda = math.radians(lon2 - lon1)
    x = math.sin(dlambda) * math.cos(p2)
    y = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dlambda)
    return (math.degrees(math.atan2(x, y)) + 360) % 360


def differenza_angolare(angolo1, angolo2):
    """Quanto sono diverse due direzioni, in un intervallo da 0 a 180°."""
    diff = abs(angolo1 - angolo2) % 360
    return diff if diff <= 180 else 360 - diff


def indice_alla_distanza(dist_cum, target):
    """Indice del punto la cui distanza cumulativa è la più vicina a target,
    oppure None se target è fuori dal tratto di traccia disponibile."""
    if target < dist_cum[0] or target > dist_cum[-1]:
        return None
    return bisect.bisect_left(dist_cum, target)


def velocita_sempre_sopra_soglia(punti, dist_cum, distanza_inizio, distanza_fine, soglia_kmh):
    """True se, nel tratto di traccia tra distanza_inizio e distanza_fine
    (in metri dall'inizio dell'attività), la velocità è sempre rimasta
    sopra la soglia indicata. Se il tratto richiesto esce dai dati
    disponibili (es. troppo vicino all'inizio o alla fine), restituisce
    False: non possiamo confermare che sia "riuscita" senza dati completi.
    """
    if distanza_inizio < dist_cum[0] or distanza_fine > dist_cum[-1]:
        return False
    i_inizio = bisect.bisect_left(dist_cum, distanza_inizio)
    i_fine = bisect.bisect_right(dist_cum, distanza_fine)
    tratto = punti[i_inizio:i_fine]
    if not tratto:
        return False
    return all(p["velocita_kmh"] > soglia_kmh for p in tratto)


def conta_strambate(
    punti,
    soglia_velocita_kmh=10,
    finestra_m=30,
    finestra_rotta_m=25,
    soglia_angolo=140,
    distanza_min_tra_eventi_m=60,
):
    """Conta le strambate "riuscite" in una sessione di windsurf/wing foil.

    Una strambata riuscita = un cambio di direzione quasi completo (sopra
    `soglia_angolo` gradi, dove 180° è un'inversione perfetta) mantenendo
    più di `soglia_velocita_kmh` km/h per almeno `finestra_m` metri sia
    prima che dopo la manovra, senza cadute o fermate.

    `punti` è una lista di dizionari {"lat", "lon", "velocita_kmh"},
    ordinati nel tempo. Restituisce (numero_strambate, lista_dettagli).
    """
    n = len(punti)
    if n < 3:
        return 0, []

    # Distanza cumulativa percorsa, metro dopo metro, dall'inizio dell'attività.
    dist_cum = [0.0]
    for i in range(1, n):
        d = distanza_metri(
            punti[i - 1]["lat"], punti[i - 1]["lon"], punti[i]["lat"], punti[i]["lon"]
        )
        dist_cum.append(dist_cum[-1] + d)

    # Per ogni punto, confrontiamo la direzione "in avvicinamento" e quella
    # "in uscita" dalla manovra, usando due tratti pieni (non il singolo
    # punto centrale, che durante una virata è spesso il più impreciso sul
    # GPS): un cambio di direzione quasi totale è il segno di una strambata.
    candidati = []
    for i in range(n):
        d_i = dist_cum[i]
        j_prima_lontano = indice_alla_distanza(dist_cum, d_i - finestra_rotta_m)
        j_prima_vicino = indice_alla_distanza(dist_cum, d_i - finestra_rotta_m / 2)
        j_dopo_vicino = indice_alla_distanza(dist_cum, d_i + finestra_rotta_m / 2)
        j_dopo_lontano = indice_alla_distanza(dist_cum, d_i + finestra_rotta_m)
        if None in (j_prima_lontano, j_prima_vicino, j_dopo_vicino, j_dopo_lontano):
            continue
        if j_prima_lontano == j_prima_vicino or j_dopo_vicino == j_dopo_lontano:
            continue
        rotta_prima = rotta_gradi(
            punti[j_prima_lontano]["lat"],
            punti[j_prima_lontano]["lon"],
            punti[j_prima_vicino]["lat"],
            punti[j_prima_vicino]["lon"],
        )
        rotta_dopo = rotta_gradi(
            punti[j_dopo_vicino]["lat"],
            punti[j_dopo_vicino]["lon"],
            punti[j_dopo_lontano]["lat"],
            punti[j_dopo_lontano]["lon"],
        )
        angolo = differenza_angolare(rotta_prima, rotta_dopo)
        if angolo >= soglia_angolo:
            candidati.append([i, d_i, angolo])

    # Più punti consecutivi possono segnalare la stessa manovra: teniamo
    # solo il punto con il cambio di direzione più netto per ogni gruppo.
    candidati.sort(key=lambda c: c[1])
    eventi = []
    for candidato in candidati:
        if eventi and candidato[1] - eventi[-1][1] < distanza_min_tra_eventi_m:
            if candidato[2] > eventi[-1][2]:
                eventi[-1] = candidato
        else:
            eventi.append(candidato)

    # Verifichiamo la condizione di "riuscita": velocità sempre sopra soglia
    # per la distanza richiesta, sia prima che dopo la manovra.
    dettagli = []
    for indice, d_evento, angolo in eventi:
        prima_ok = velocita_sempre_sopra_soglia(
            punti, dist_cum, d_evento - finestra_m, d_evento, soglia_velocita_kmh
        )
        dopo_ok = velocita_sempre_sopra_soglia(
            punti, dist_cum, d_evento, d_evento + finestra_m, soglia_velocita_kmh
        )
        if prima_ok and dopo_ok:
            dettagli.append(
                {
                    "distanza_km": round(d_evento / 1000, 2),
                    "lat": punti[indice]["lat"],
                    "lon": punti[indice]["lon"],
                    "velocita_kmh": round(punti[indice]["velocita_kmh"], 1),
                    "cambio_direzione_gradi": round(angolo),
                }
            )

    return len(dettagli), dettagli


@st.cache_resource(show_spinner="Connessione a Garmin Connect...")
def get_client():
    client = Garmin()
    client.login(CARTELLA_TOKEN)
    return client


@st.cache_data(show_spinner="Scarico le attività da Garmin...", ttl=600)
def carica_attivita(data_inizio, data_fine, numero_max):
    attivita_grezze = _client.get_activities_by_date(data_inizio, data_fine)
    return attivita_grezze[:numero_max]


@st.cache_data(show_spinner=False, ttl=600)
def carica_dettaglio_attivita(activity_id):
    riepilogo = _client.get_activity(activity_id)
    dettagli = None
    splits = None
    try:
        dettagli = _client.get_activity_details(activity_id)
    except Exception:
        pass
    try:
        splits = _client.get_activity_splits(activity_id)
    except Exception:
        pass
    return riepilogo, dettagli, splits


st.title("🚴 Le mie uscite")
st.caption("Dati letti direttamente dal tuo account Garmin Connect.")

try:
    _client = get_client()
except Exception as errore:
    st.error(
        "Non riesco a collegarmi a Garmin Connect. "
        "Apri un terminale ed esegui prima 'garmin_login.py' per accedere, "
        "poi ricarica questa pagina."
    )
    st.caption(f"Dettaglio tecnico: {errore}")
    st.stop()

# --- Filtri nella barra laterale (validi per tutta l'app) ---
giorni_indietro = st.sidebar.slider("Quanti giorni indietro guardare?", 7, 730, 180)
numero_max = st.sidebar.slider("Numero massimo di attività da scaricare", 10, 500, 150)

data_inizio = (date.today() - timedelta(days=giorni_indietro)).isoformat()
data_fine = date.today().isoformat()

try:
    attivita_grezze = carica_attivita(data_inizio, data_fine, numero_max)
except Exception as errore:
    st.error("Errore durante il recupero delle attività da Garmin Connect.")
    st.caption(f"Dettaglio tecnico: {errore}")
    st.stop()

if not attivita_grezze:
    st.info("Nessuna attività trovata in questo periodo. Prova ad allargare l'intervallo nella barra laterale.")
    st.stop()

# --- Trasformiamo le attività in una tabella semplice da usare ---
righe = []
for att in attivita_grezze:
    tipo = (att.get("activityType") or {}).get("typeKey", "sconosciuto")
    righe.append(
        {
            "id": att.get("activityId"),
            "data": (att.get("startTimeLocal") or "")[:10],
            "nome": att.get("activityName", ""),
            "tipo": tipo,
            "distanza_km": round((att.get("distance") or 0) / 1000, 2),
            "durata_min": round((att.get("duration") or 0) / 60, 1),
            "dislivello_m": round(att.get("elevationGain") or 0, 0),
            "hr_media": trova_valore(att, "averageHR"),
            "hr_max": trova_valore(att, "maxHR"),
            "velocita_media_kmh": round((trova_valore(att, "averageSpeed") or 0) * 3.6, 1),
            "calorie": trova_valore(att, "calories"),
        }
    )

tabella = pd.DataFrame(righe)


def nome_sport_leggibile(tipo_chiave):
    icona = ICONE_SPORT.get(tipo_chiave, "🏅")
    testo = tipo_chiave.replace("_", " ").capitalize()
    return f"{icona} {testo}"


# --- Stato di navigazione ---
if "pagina" not in st.session_state:
    st.session_state.pagina = "home"
if "sport_scelto" not in st.session_state:
    st.session_state.sport_scelto = None
if "attivita_scelta" not in st.session_state:
    st.session_state.attivita_scelta = None


def vai_a_home():
    st.session_state.pagina = "home"
    st.session_state.sport_scelto = None
    st.session_state.attivita_scelta = None


def vai_a_elenco(tipo):
    st.session_state.pagina = "elenco"
    st.session_state.sport_scelto = tipo
    st.session_state.attivita_scelta = None


def vai_a_dettaglio(activity_id):
    st.session_state.pagina = "dettaglio"
    st.session_state.attivita_scelta = activity_id


# =========================================================
# PAGINA 1 — HOME: un bottone per ogni sport
# =========================================================
if st.session_state.pagina == "home":
    st.subheader("Scegli uno sport")

    riepilogo_sport = (
        tabella.groupby("tipo")
        .agg(numero_uscite=("id", "count"), km_totali=("distanza_km", "sum"))
        .reset_index()
        .sort_values("numero_uscite", ascending=False)
    )

    colonne = st.columns(3)
    for indice, riga in riepilogo_sport.iterrows():
        colonna = colonne[indice % 3]
        with colonna:
            etichetta = nome_sport_leggibile(riga["tipo"])
            if st.button(
                f"{etichetta}\n\n{int(riga['numero_uscite'])} uscite — {riga['km_totali']:.0f} km",
                key=f"btn_sport_{riga['tipo']}",
                use_container_width=True,
            ):
                vai_a_elenco(riga["tipo"])
                st.rerun()

# =========================================================
# PAGINA 2 — ELENCO: tutte le attività dello sport scelto
# =========================================================
elif st.session_state.pagina == "elenco":
    if st.button("← Torna agli sport"):
        vai_a_home()
        st.rerun()

    sport = st.session_state.sport_scelto
    st.subheader(nome_sport_leggibile(sport))

    sotto_tabella = tabella[tabella["tipo"] == sport].sort_values("data", ascending=False)

    col1, col2, col3, col4 = st.columns(4)
    col1.metric("Uscite", len(sotto_tabella))
    col2.metric("Km totali", round(sotto_tabella["distanza_km"].sum(), 1))
    col3.metric("Ore totali", round(sotto_tabella["durata_min"].sum() / 60, 1))
    col4.metric("Dislivello totale (m)", int(sotto_tabella["dislivello_m"].sum()))

    st.divider()

    for _, riga in sotto_tabella.iterrows():
        with st.container(border=True):
            colonna_info, colonna_bottone = st.columns([0.8, 0.2])
            with colonna_info:
                st.markdown(f"**{riga['data']} — {riga['nome']}**")
                st.caption(
                    f"{riga['distanza_km']} km · {riga['durata_min']} min · "
                    f"dislivello {int(riga['dislivello_m'])} m · "
                    f"HR media {riga['hr_media'] or '—'}"
                )
            with colonna_bottone:
                if st.button("Dettagli →", key=f"btn_att_{riga['id']}", use_container_width=True):
                    vai_a_dettaglio(riga["id"])
                    st.rerun()

# =========================================================
# PAGINA 3 — DETTAGLIO: tutti i dati tecnici di un'attività
# =========================================================
elif st.session_state.pagina == "dettaglio":
    col_indietro1, col_indietro2 = st.columns(2)
    with col_indietro1:
        if st.button("← Torna all'elenco"):
            vai_a_elenco(st.session_state.sport_scelto)
            st.rerun()
    with col_indietro2:
        if st.button("🏠 Torna agli sport"):
            vai_a_home()
            st.rerun()

    activity_id = st.session_state.attivita_scelta
    riga = tabella[tabella["id"] == activity_id].iloc[0]

    st.subheader(f"{riga['data']} — {riga['nome']}")

    try:
        riepilogo, dettagli, splits = carica_dettaglio_attivita(activity_id)
    except Exception as errore:
        st.error("Non sono riuscito a scaricare i dati di questa attività.")
        st.caption(f"Dettaglio tecnico: {errore}")
        st.stop()

    # --- Statistiche principali ---
    st.markdown("#### Statistiche principali")
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Distanza", f"{riga['distanza_km']} km")
    c2.metric("Durata", f"{riga['durata_min']} min")
    c3.metric("Dislivello +", f"{int(riga['dislivello_m'])} m")
    c4.metric("Calorie", riga["calorie"] or "—")

    velocita_max = trova_valore(riepilogo, "maxSpeed") if riepilogo else None

    c5, c6, c7, c8 = st.columns(4)
    c5.metric("HR media", riga["hr_media"] or "—")
    c6.metric("HR max", riga["hr_max"] or "—")
    c7.metric("Velocità media", f"{riga['velocita_media_kmh']} km/h")
    c8.metric(
        "Velocità max",
        f"{round(velocita_max * 3.6, 1)} km/h" if velocita_max is not None else "—",
    )

    # --- Altri dati tecnici trovati nel riepilogo Garmin ---
    if riepilogo:
        altri_campi = {
            "Cadenza media": trova_valore(
                riepilogo,
                "averageBikingCadenceInRevPerMinute",
                "averageRunningCadenceInStepsPerMinute",
                "averageCadence",
            ),
            "Cadenza max": trova_valore(
                riepilogo,
                "maxBikingCadenceInRevPerMinute",
                "maxRunningCadenceInStepsPerMinute",
                "maxCadence",
            ),
            "Potenza media (W)": trova_valore(riepilogo, "avgPower"),
            "Potenza max (W)": trova_valore(riepilogo, "maxPower"),
            "Temperatura media (°C)": trova_valore(riepilogo, "avgTemperature"),
            "Dislivello -": trova_valore(riepilogo, "elevationLoss"),
            "Altitudine min (m)": trova_valore(riepilogo, "minElevation"),
            "Altitudine max (m)": trova_valore(riepilogo, "maxElevation"),
            "VO2max stimato": trova_valore(riepilogo, "vO2MaxValue"),
        }
        altri_campi = {k: v for k, v in altri_campi.items() if v is not None}
        if altri_campi:
            st.markdown("#### Altri dati tecnici")
            colonne_extra = st.columns(4)
            for indice, (etichetta, valore) in enumerate(altri_campi.items()):
                colonne_extra[indice % 4].metric(etichetta, valore)

    st.divider()

    # --- Estrazione punti GPS + velocità, usati sia per la mappa che per
    # il conteggio delle strambate ---
    punti_traccia = []
    if dettagli:
        try:
            descrittori_traccia = {
                d["key"]: d["metricsIndex"] for d in dettagli.get("metricDescriptors", [])
            }
            i_lat = descrittori_traccia.get("directLatitude")
            i_lon = descrittori_traccia.get("directLongitude")
            i_vel = descrittori_traccia.get("directSpeed")
            for p in dettagli.get("activityDetailMetrics", []):
                m = p.get("metrics")
                if not m:
                    continue
                lat = m[i_lat] if i_lat is not None else None
                lon = m[i_lon] if i_lon is not None else None
                vel = m[i_vel] if i_vel is not None else None
                if lat is not None and lon is not None:
                    punti_traccia.append(
                        {"lat": lat, "lon": lon, "velocita_kmh": (vel or 0) * 3.6}
                    )
        except Exception:
            punti_traccia = []

    # --- Strambate riuscite (solo per windsurf / wing foil e simili) ---
    is_vela_o_foil = any(parola in str(riga["tipo"]).lower() for parola in PAROLE_CHIAVE_VELA_FOIL)
    dettagli_strambate = []

    if is_vela_o_foil:
        st.markdown("#### 🔄 Strambate riuscite")

        if len(punti_traccia) < 3:
            st.info("Non ci sono abbastanza dati GPS/velocità per contare le strambate di questa attività.")
        else:
            with st.expander("⚙️ Parametri di rilevamento (regolabili)"):
                soglia_velocita = st.slider(
                    "Velocità minima da mantenere (km/h)", 5, 25, 10, key="soglia_vel_strambate"
                )
                distanza_finestra = st.slider(
                    "Distanza da mantenere la velocità, prima e dopo (m)",
                    10,
                    60,
                    30,
                    key="distanza_finestra_strambate",
                )
                soglia_angolo = st.slider(
                    "Cambio di direzione minimo per contare una manovra (gradi) — 180° = inversione perfetta",
                    90,
                    179,
                    140,
                    key="soglia_angolo_strambate",
                )
                lunghezza_tratto_direzione = st.slider(
                    "Lunghezza del tratto usato per calcolare la direzione, prima e dopo (m) — "
                    "un valore più alto riduce le false strambate dovute a un GPS impreciso",
                    10,
                    50,
                    25,
                    key="lunghezza_tratto_strambate",
                )

            numero_strambate, dettagli_strambate = conta_strambate(
                punti_traccia,
                soglia_velocita_kmh=soglia_velocita,
                finestra_m=distanza_finestra,
                finestra_rotta_m=lunghezza_tratto_direzione,
                soglia_angolo=soglia_angolo,
            )

            st.metric("Strambate riuscite", numero_strambate)

            if dettagli_strambate:
                st.caption("Dettaglio delle manovre rilevate:")
                st.dataframe(
                    pd.DataFrame(dettagli_strambate)[
                        ["distanza_km", "velocita_kmh", "cambio_direzione_gradi"]
                    ],
                    use_container_width=True,
                    hide_index=True,
                )

            st.caption(
                "Rilevamento automatico da GPS e velocità: conta come riuscita una manovra in cui "
                f"hai cambiato direzione mantenendo più di {soglia_velocita} km/h per almeno "
                f"{distanza_finestra} m prima e dopo, senza cadute o fermate. Se il numero non ti "
                "sembra giusto, prova a regolare i parametri qui sopra — il segnale GPS non è mai perfetto."
            )

        st.divider()

    # --- Mappa del percorso GPS, se disponibile (bici, windsurf/wing foil, ecc.) ---
    if punti_traccia:
        st.markdown("#### 🗺️ Mappa del percorso")
        df_percorso = pd.DataFrame(punti_traccia)[["lat", "lon"]]

        mappa_disegnata = False
        try:
            import pydeck as pdk

            percorso_lonlat = [[p["lon"], p["lat"]] for p in punti_traccia]
            layers = [
                pdk.Layer(
                    "PathLayer",
                    data=[{"path": percorso_lonlat}],
                    get_path="path",
                    get_width=4,
                    get_color=[255, 90, 0],
                    width_min_pixels=3,
                )
            ]
            if dettagli_strambate:
                layers.append(
                    pdk.Layer(
                        "ScatterplotLayer",
                        data=dettagli_strambate,
                        get_position="[lon, lat]",
                        get_fill_color=[0, 160, 255],
                        get_radius=8,
                        radius_min_pixels=5,
                        pickable=True,
                    )
                )
            vista = pdk.ViewState(
                latitude=df_percorso["lat"].mean(),
                longitude=df_percorso["lon"].mean(),
                zoom=12,
            )
            st.pydeck_chart(pdk.Deck(layers=layers, initial_view_state=vista))
            mappa_disegnata = True
            if dettagli_strambate:
                st.caption("🔵 I pallini blu segnano le strambate riuscite individuate.")
        except Exception:
            mappa_disegnata = False

        if not mappa_disegnata:
            st.map(df_percorso)
    else:
        st.caption("Nessun dato GPS disponibile per questa attività.")

    st.divider()

    # --- Grafici punto per punto (frequenza cardiaca, velocità, elevazione) ---
    if dettagli:
        try:
            descrittori = {
                d["key"]: d["metricsIndex"] for d in dettagli.get("metricDescriptors", [])
            }
            punti = dettagli.get("activityDetailMetrics", [])

            serie = {}
            for chiave_metrica, nome_leggibile in [
                ("directHeartRate", "❤️ Frequenza cardiaca (bpm)"),
                ("directSpeed", "⚡ Velocità (m/s)"),
                ("directElevation", "⛰️ Elevazione (m)"),
                ("directPower", "🔋 Potenza (W)"),
            ]:
                indice = descrittori.get(chiave_metrica)
                if indice is not None:
                    valori = [
                        p["metrics"][indice]
                        for p in punti
                        if p.get("metrics") and p["metrics"][indice] is not None
                    ]
                    if valori:
                        serie[nome_leggibile] = valori

            if serie:
                st.markdown("#### Andamento durante l'attività")
                for nome_leggibile, valori in serie.items():
                    st.caption(nome_leggibile)
                    st.line_chart(valori)
        except Exception:
            st.info("Grafici punto per punto non disponibili per questa attività.")

    # --- Splits (parziali), se disponibili ---
    if splits and splits.get("lapDTOs"):
        st.markdown("#### Parziali (lap)")
        righe_split = []
        for lap in splits["lapDTOs"]:
            righe_split.append(
                {
                    "distanza_km": round((lap.get("distance") or 0) / 1000, 2),
                    "durata_min": round((lap.get("duration") or 0) / 60, 1),
                    "hr_media": lap.get("averageHR"),
                    "velocita_media_kmh": round((lap.get("averageSpeed") or 0) * 3.6, 1),
                }
            )
        st.dataframe(pd.DataFrame(righe_split), use_container_width=True, hide_index=True)

    # --- Tutti i dati grezzi, per non perdere nulla ---
    with st.expander("🔧 Vedi tutti i dati tecnici grezzi (avanzato)"):
        st.json(riepilogo or {})
