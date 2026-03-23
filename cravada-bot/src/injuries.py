import requests
import pdfplumber
import re
import os
import json
import logging
from datetime import datetime, date
from io import BytesIO
from database import log_injury_update

logger = logging.getLogger(__name__)

CACHE_FILE = "data/injuries_cache.json"

# NBA publica entre 4-5 PDFs por dia nesse formato de URL
NBA_INJURY_BASE = "https://ak-static.cms.nba.com/referee/injury/Injury-Report_{date}_{time}.pdf"

# Horários que a NBA costuma publicar os PDFs
NBA_REPORT_TIMES = ["01_00PM", "05_00PM", "06_15PM", "07_00PM", "08_00PM"]


def _download_pdf(url: str) -> BytesIO | None:
    try:
        r = requests.get(url, timeout=15)
        if r.status_code == 200:
            logger.info(f"PDF baixado: {url}")
            return BytesIO(r.content)
    except Exception as e:
        logger.warning(f"Falha ao baixar {url}: {e}")
    return None


def _parse_pdf(pdf_bytes: BytesIO) -> list[dict]:
    players = []
    try:
        with pdfplumber.open(pdf_bytes) as pdf:
            for page in pdf.pages:
                text = page.extract_text()
                if not text:
                    continue

                lines = text.split("\n")
                for line in lines:
                    # Padrão NBA: "Game Date  Matchup  Team  Player Name  Status  Reason"
                    # Ex: "03/23/2026  LAL vs GSW  Los Angeles Lakers  LeBron James  Out  Left Ankle Soreness"
                    parts = line.strip().split("  ")
                    parts = [p.strip() for p in parts if p.strip()]

                    if len(parts) >= 5:
                        # Tenta extrair status (Out, Questionable, Doubtful, Available)
                        status_keywords = ["Out", "Questionable", "Doubtful", "Available", "Probable"]
                        status = None
                        status_idx = None

                        for i, part in enumerate(parts):
                            if part in status_keywords:
                                status = part
                                status_idx = i
                                break

                        if status and status_idx:
                            player_name = parts[status_idx - 1] if status_idx > 0 else None
                            team = parts[status_idx - 2] if status_idx > 1 else None
                            reason = parts[status_idx + 1] if status_idx + 1 < len(parts) else "Não informado"

                            if player_name and len(player_name.split()) >= 2:
                                players.append({
                                    "name": player_name,
                                    "team": team or "",
                                    "status": status,
                                    "reason": reason
                                })
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
    return {"date": "", "players": []}


def _save_cache(players: list):
    os.makedirs("data", exist_ok=True)
    with open(CACHE_FILE, "w") as f:
        json.dump({"date": date.today().isoformat(), "players": players}, f, ensure_ascii=False)


async def refresh_injury_list():
    """Baixa todos os PDFs disponíveis da NBA e atualiza o cache"""
    today = datetime.now().strftime("%Y-%m-%d")
    all_players = []
    found_any = False

    for time_str in NBA_REPORT_TIMES:
        url = NBA_INJURY_BASE.format(date=today, time=time_str)
        pdf_bytes = _download_pdf(url)
        if pdf_bytes:
            players = _parse_pdf(pdf_bytes)
            all_players.extend(players)
            found_any = True

    if found_any:
        # Deduplicar por nome do jogador (mantém o mais recente)
        seen = {}
        for p in all_players:
            seen[p["name"]] = p
        unique_players = list(seen.values())

        _save_cache(unique_players)
        log_injury_update()
        logger.info(f"Injury list atualizada: {len(unique_players)} jogadores")
    else:
        logger.warning("Nenhum PDF da NBA encontrado hoje ainda.")


def get_injury_list(team_abbr: str = None) -> list[dict]:
    """Retorna lista de lesionados, opcionalmente filtrada por time"""
    cache = _load_cache()
    players = cache.get("players", [])

    # Mapeamento de abreviações para nomes completos
    team_names = {
        "ATL": "Atlanta", "BOS": "Boston", "BKN": "Brooklyn",
        "CHA": "Charlotte", "CHI": "Chicago", "CLE": "Cleveland",
        "DAL": "Dallas", "DEN": "Denver", "DET": "Detroit",
        "GSW": "Golden State", "HOU": "Houston", "IND": "Indiana",
        "LAC": "LA Clippers", "LAL": "Lakers", "MEM": "Memphis",
        "MIA": "Miami", "MIL": "Milwaukee", "MIN": "Minnesota",
        "NOP": "New Orleans", "NYK": "New York", "OKC": "Oklahoma",
        "ORL": "Orlando", "PHI": "Philadelphia", "PHX": "Phoenix",
        "POR": "Portland", "SAC": "Sacramento", "SAS": "San Antonio",
        "TOR": "Toronto", "UTA": "Utah", "WAS": "Washington",
    }

    if team_abbr:
        team_name = team_names.get(team_abbr, team_abbr)
        players = [p for p in players if team_name.lower() in p.get("team", "").lower()]

    # Filtra só Out e Questionable (relevantes para apostas)
    relevant = [p for p in players if p["status"] in ["Out", "Questionable", "Doubtful"]]
    return relevant


def is_player_injured(player_name: str) -> bool:
    """Verifica se um jogador específico está lesionado/out"""
    cache = _load_cache()
    players = cache.get("players", [])
    name_lower = player_name.lower()
    for p in players:
        if name_lower in p["name"].lower() and p["status"] == "Out":
            return True
    return False


def get_player_injury_status(player_name: str) -> dict | None:
    """Retorna status de lesão de um jogador específico"""
    cache = _load_cache()
    players = cache.get("players", [])
    name_lower = player_name.lower()
    for p in players:
        if name_lower in p["name"].lower():
            return p
    return None
