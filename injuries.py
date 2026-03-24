import requests
import pdfplumber
import os
import json
import logging
from datetime import datetime, date
from io import BytesIO

logger = logging.getLogger(__name__)

CACHE_FILE = "data/injuries_cache.json"
PROCESSED_URLS_FILE = "data/injuries_processed_urls.json"
CANCELLED_PICKS_FILE = "data/cancelled_picks.json"

NBA_INJURY_BASE = "https://ak-static.cms.nba.com/referee/injury/Injury-Report_{date}_{time}.pdf"

POSSIBLE_MINUTES = ["00", "15", "30", "45"]
POSSIBLE_HOURS_AM = ["09", "10", "11"]
POSSIBLE_HOURS_PM = ["12", "01", "02", "03", "04", "05", "06", "07", "08", "09", "10"]


def _build_candidate_urls(for_date: str) -> list:
    urls = []
    for h in POSSIBLE_HOURS_AM:
        for m in POSSIBLE_MINUTES:
            urls.append(NBA_INJURY_BASE.format(date=for_date, time=f"{h}_{m}AM"))
    for h in POSSIBLE_HOURS_PM:
        for m in POSSIBLE_MINUTES:
            urls.append(NBA_INJURY_BASE.format(date=for_date, time=f"{h}_{m}PM"))
    return urls


def _load_processed_urls() -> set:
    try:
        if os.path.exists(PROCESSED_URLS_FILE):
            with open(PROCESSED_URLS_FILE) as f:
                data = json.load(f)
            if data.get("date") == date.today().isoformat():
                return set(data.get("urls", []))
    except Exception:
        pass
    return set()


def _save_processed_urls(urls: set):
    os.makedirs("data", exist_ok=True)
    with open(PROCESSED_URLS_FILE, "w") as f:
        json.dump({"date": date.today().isoformat(), "urls": list(urls)}, f)


def _download_pdf(url: str) -> BytesIO | None:
    try:
        r = requests.get(url, timeout=10)
        if r.status_code == 200:
            return BytesIO(r.content)
    except Exception:
        pass
    return None


def _parse_pdf(pdf_bytes: BytesIO) -> list:
    players = []
    status_keywords = ["Out", "Questionable", "Doubtful", "Available", "Probable"]

    try:
        with pdfplumber.open(pdf_bytes) as pdf:
            for page in pdf.pages:
                # Tenta extrair por tabela primeiro
                table = page.extract_table()
                if table:
                    for row in table:
                        if not row or len(row) < 5:
                            continue
                        try:
                            row = [str(c).strip() if c else "" for c in row]
                            for i, cell in enumerate(row):
                                if cell in status_keywords and i >= 2:
                                    player_name = row[i - 1]
                                    team = row[i - 2]
                                    reason = row[i + 1] if i + 1 < len(row) else "Não informado"
                                    if player_name and len(player_name.split()) >= 2:
                                        players.append({
                                            "name": player_name,
                                            "team": team,
                                            "status": cell,
                                            "reason": reason
                                        })
                                    break
                        except Exception:
                            continue

                # Fallback por texto
                if not players:
                    text = page.extract_text() or ""
                    for line in text.split("\n"):
                        parts = [p.strip() for p in line.split("  ") if p.strip()]
                        for i, part in enumerate(parts):
                            if part in status_keywords and i >= 2:
                                player_name = parts[i - 1]
                                team = parts[i - 2]
                                reason = parts[i + 1] if i + 1 < len(parts) else "Não informado"
                                if len(player_name.split()) >= 2:
                                    players.append({
                                        "name": player_name,
                                        "team": team,
                                        "status": part,
                                        "reason": reason
                                    })
                                break
    except Exception as e:
        logger.error(f"Erro ao parsear PDF: {e}")

    return players


def _load_cache() -> dict:
    try:
        if os.path.exists(CACHE_FILE):
            with open(CACHE_FILE) as f:
                return json.load(f)
    except Exception:
        pass
    return {"date": "", "players": [], "last_updated": None, "last_url": None}


def _save_cache(players: list, source_url: str):
    os.makedirs("data", exist_ok=True)
    with open(CACHE_FILE, "w") as f:
        json.dump({
            "date": date.today().isoformat(),
            "players": players,
            "last_updated": datetime.now().isoformat(),
            "last_url": source_url
        }, f, ensure_ascii=False)


def _load_cancelled_picks() -> list:
    try:
        if os.path.exists(CANCELLED_PICKS_FILE):
            with open(CANCELLED_PICKS_FILE) as f:
                data = json.load(f)
            if data.get("date") == date.today().isoformat():
                return data.get("picks", [])
    except Exception:
        pass
    return []


def _save_cancelled_picks(picks: list):
    os.makedirs("data", exist_ok=True)
    with open(CANCELLED_PICKS_FILE, "w") as f:
        json.dump({"date": date.today().isoformat(), "picks": picks}, f, ensure_ascii=False)


def _check_and_cancel_picks(updated_players: dict) -> list:
    picks_cache = "data/picks_cache.json"
    if not os.path.exists(picks_cache):
        return []

    try:
        with open(picks_cache) as f:
            data = json.load(f)

        if data.get("date") != date.today().isoformat():
            return []

        picks = data.get("picks", [])
        already_cancelled = {p["jogador"] for p in _load_cancelled_picks()}
        cancelled = []
        remaining = []

        for pick in picks:
            player_name = pick.get("jogador", "")
            player_data = updated_players.get(player_name)
            if (player_data and
                    player_data["status"] == "Out" and
                    player_name not in already_cancelled):
                cancelled.append({
                    "jogador": player_name,
                    "mercado": pick.get("mercado", ""),
                    "motivo": player_data.get("reason", "Lesão")
                })
            else:
                remaining.append(pick)

        if cancelled:
            data["picks"] = remaining
            with open(picks_cache, "w") as f:
                json.dump(data, f, ensure_ascii=False)
            _save_cancelled_picks(_load_cancelled_picks() + cancelled)

        return cancelled

    except Exception as e:
        logger.error(f"Erro ao checar picks para cancelar: {e}")
        return []


async def poll_new_injury_reports() -> dict:
    """
    Verifica se saiu algum PDF novo da NBA.
    Retorna: found, new_players, cancelled_picks, source_url, pdfs_found_count
    """
    today = datetime.now().strftime("%Y-%m-%d")
    candidate_urls = _build_candidate_urls(today)
    processed_urls = _load_processed_urls()
    cache = _load_cache()
    current_players = {p["name"]: p for p in cache.get("players", [])}

    new_pdfs = []
    for url in candidate_urls:
        if url in processed_urls:
            continue
        pdf_bytes = _download_pdf(url)
        if pdf_bytes:
            logger.info(f"PDF novo: {url}")
            new_pdfs.append((url, pdf_bytes))
            processed_urls.add(url)

    if not new_pdfs:
        return {"found": False, "new_players": [], "cancelled_picks": [], "source_url": None, "pdfs_found_count": 0}

    all_new_players = {}
    last_url = None
    for url, pdf_bytes in new_pdfs:
        for p in _parse_pdf(pdf_bytes):
            all_new_players[p["name"]] = p
        last_url = url

    # Detecta mudanças relevantes
    changed = []
    for name, new_data in all_new_players.items():
        old = current_players.get(name)
        if new_data["status"] not in ["Out", "Questionable", "Doubtful"]:
            continue
        if old is None:
            changed.append({**new_data, "change": "novo"})
        elif old["status"] != new_data["status"]:
            changed.append({**new_data, "change": f"{old['status']} → {new_data['status']}"})

    merged = {**current_players, **all_new_players}
    _save_cache(list(merged.values()), last_url)
    _save_processed_urls(processed_urls)

    cancelled = _check_and_cancel_picks(all_new_players)

    return {
        "found": True,
        "new_players": changed,
        "cancelled_picks": cancelled,
        "source_url": last_url,
        "pdfs_found_count": len(new_pdfs)
    }


async def refresh_injury_list() -> dict:
    """Alias público para poll — força varredura completa"""
    return await poll_new_injury_reports()


def get_injury_list(team_abbr: str = None) -> list:
    cache = _load_cache()
    players = cache.get("players", [])

    team_names = {
        "ATL": "Atlanta", "BOS": "Boston", "BKN": "Brooklyn",
        "CHA": "Charlotte", "CHI": "Chicago", "CLE": "Cleveland",
        "DAL": "Dallas", "DEN": "Denver", "DET": "Detroit",
        "GSW": "Golden State", "HOU": "Houston", "IND": "Indiana",
        "LAC": "Clippers", "LAL": "Lakers", "MEM": "Memphis",
        "MIA": "Miami", "MIL": "Milwaukee", "MIN": "Minnesota",
        "NOP": "New Orleans", "NYK": "New York", "OKC": "Oklahoma",
        "ORL": "Orlando", "PHI": "Philadelphia", "PHX": "Phoenix",
        "POR": "Portland", "SAC": "Sacramento", "SAS": "San Antonio",
        "TOR": "Toronto", "UTA": "Utah", "WAS": "Washington",
    }

    if team_abbr:
        team_name = team_names.get(team_abbr, team_abbr)
        players = [p for p in players if team_name.lower() in p.get("team", "").lower()]

    return [p for p in players if p["status"] in ["Out", "Questionable", "Doubtful"]]


def get_last_update_info() -> dict:
    cache = _load_cache()
    last = cache.get("last_updated")
    url = cache.get("last_url", "")
    count = len([p for p in cache.get("players", []) if p["status"] in ["Out", "Questionable", "Doubtful"]])

    if last:
        dt = datetime.fromisoformat(last)
        formatted = dt.strftime("%d/%m %H:%M")
    else:
        formatted = "Nunca"

    pdf_time = ""
    if url:
        try:
            part = url.split("_")[-1].replace(".pdf", "").replace("_", ":")
            pdf_time = f" (PDF das {part})"
        except Exception:
            pass

    return {"formatted": formatted + pdf_time, "total_injured": count, "url": url}


def is_player_injured(player_name: str) -> bool:
    cache = _load_cache()
    name_lower = player_name.lower()
    for p in cache.get("players", []):
        if name_lower in p["name"].lower() and p["status"] == "Out":
            return True
    return False


def get_player_injury_status(player_name: str) -> dict | None:
    cache = _load_cache()
    name_lower = player_name.lower()
    for p in cache.get("players", []):
        if name_lower in p["name"].lower():
            return p
    return None
