import requests
import pdfplumber
import os
import json
import re
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


def _normalize_name(raw: str) -> str:
    """Converte 'Last, First' para 'First Last'"""
    raw = raw.strip()
    if "," in raw:
        parts = raw.split(",", 1)
        return f"{parts[1].strip()} {parts[0].strip()}"
    return raw


def _parse_pdf(pdf_bytes: BytesIO) -> list:
    """
    Parseia o PDF oficial da NBA Injury Report.

    Formato real confirmado no PDF:
    Colunas: Game Date | Game Time | Matchup | Team | Player Name | Current Status | Reason
    - O time aparece apenas na primeira linha do grupo; linhas seguintes do mesmo time ficam vazias
    - Nomes no formato: "Last, First" (ex: "Haliburton, Tyrese")
    - Status: Out | Questionable | Doubtful | Available | Probable
    """
    players = []
    status_keywords = {"Out", "Questionable", "Doubtful", "Available", "Probable"}

    try:
        with pdfplumber.open(pdf_bytes) as pdf:
            for page in pdf.pages:
                page_players = []

                # ── Método 1: tabela estruturada ─────────────────────────────
                table = page.extract_table()
                if table:
                    current_team = ""
                    for row in table:
                        if not row:
                            continue
                        cells = [str(c).strip() if c else "" for c in row]

                        # Pula cabeçalho e linhas sem dados úteis
                        joined = " ".join(cells)
                        if any(h in joined for h in ["Game Date", "Player Name", "Current Status"]):
                            continue
                        if "NOT YET SUBMITTED" in joined:
                            continue

                        # Encontra a coluna de status
                        status = None
                        status_idx = None
                        for i, cell in enumerate(cells):
                            if cell in status_keywords:
                                status = cell
                                status_idx = i
                                break

                        if status is None or status_idx is None:
                            continue

                        # Nome: coluna imediatamente antes do status
                        player_raw = cells[status_idx - 1] if status_idx >= 1 else ""

                        # Time: 2 colunas antes do status (ou mantém o último time visto)
                        team_candidate = cells[status_idx - 2] if status_idx >= 2 else ""
                        if team_candidate and team_candidate not in status_keywords:
                            # Não atualiza o time se for uma data ou matchup
                            if not re.match(r'\d{2}/\d{2}/\d{4}', team_candidate) and '@' not in team_candidate:
                                current_team = team_candidate

                        # Motivo: coluna após o status
                        reason = ""
                        if status_idx + 1 < len(cells):
                            reason = cells[status_idx + 1]
                        if not reason:
                            reason = "Não informado"

                        player_name = _normalize_name(player_raw)
                        if not player_name or len(player_name.split()) < 2:
                            continue

                        page_players.append({
                            "name": player_name,
                            "team": current_team,
                            "status": status,
                            "reason": reason
                        })

                # ── Método 2: texto linha a linha (fallback) ─────────────────
                if not page_players:
                    text = page.extract_text() or ""
                    current_team = ""

                    for line in text.split("\n"):
                        line = line.strip()
                        if not line or "NOT YET SUBMITTED" in line:
                            continue
                        if any(h in line for h in ["Game Date", "Game Time", "Player Name", "Current Status"]):
                            continue

                        # Detecta status na linha
                        found_status = None
                        for kw in status_keywords:
                            if re.search(rf'\b{kw}\b', line):
                                found_status = kw
                                break

                        if not found_status:
                            continue

                        parts = re.split(rf'\b{found_status}\b', line, maxsplit=1)
                        before = parts[0].strip()
                        after = parts[1].strip() if len(parts) > 1 else "Não informado"

                        # Procura padrão "Sobrenome, Nome" no before
                        name_match = re.search(
                            r'([A-Z][a-zA-Z\'\-\.]+(?:\s+[A-Z][a-zA-Z\'\-\.]+)*,\s*[A-Z][a-zA-Z\'\-\.]+(?:\s+[A-Z][a-zA-Z\'\-\.]+)*)\s*$',
                            before
                        )
                        if name_match:
                            player_raw = name_match.group(1)
                            player_name = _normalize_name(player_raw)
                            team_part = before[:name_match.start()].strip()
                            if team_part and not re.match(r'\d{2}/\d{2}', team_part) and '@' not in team_part:
                                current_team = team_part
                        else:
                            words = before.split()
                            if len(words) < 2:
                                continue
                            player_name = " ".join(words[-2:])
                            team_part = " ".join(words[:-2])
                            if team_part and '@' not in team_part:
                                current_team = team_part

                        if len(player_name.split()) < 2:
                            continue

                        page_players.append({
                            "name": player_name,
                            "team": current_team,
                            "status": found_status,
                            "reason": after or "Não informado"
                        })

                players.extend(page_players)

    except Exception as e:
        logger.error(f"Erro ao parsear PDF: {e}")

    # Deduplica por nome, mantendo último status
    seen = {}
    for p in players:
        seen[p["name"].lower()] = p
    result = list(seen.values())
    logger.info(f"PDF parseado: {len(result)} jogadores encontrados")
    return result


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
            if player_data and player_data["status"] == "Out" and player_name not in already_cancelled:
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
        logger.error(f"Erro ao checar picks: {e}")
        return []


async def poll_new_injury_reports() -> dict:
    """Verifica PDFs novos da NBA e atualiza o cache"""
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
        if new_data["status"] not in {"Out", "Questionable", "Doubtful"}:
            continue
        old = current_players.get(name)
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
    """Força varredura completa dos PDFs de hoje"""
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

    return [p for p in players if p["status"] in {"Out", "Questionable", "Doubtful"}]


def get_last_update_info() -> dict:
    cache = _load_cache()
    last = cache.get("last_updated")
    url = cache.get("last_url", "")
    count = len([p for p in cache.get("players", []) if p["status"] in {"Out", "Questionable", "Doubtful"}])

    formatted = "Nunca"
    if last:
        try:
            dt = datetime.fromisoformat(last)
            formatted = dt.strftime("%d/%m %H:%M")
        except Exception:
            formatted = last

    pdf_time = ""
    if url:
        try:
            part = url.split("Injury-Report_")[1].replace(".pdf", "").split("_", 2)[-1]
            pdf_time = f" (PDF {part})"
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
