import requests
import logging
import json
import os
from datetime import datetime, date, timedelta
from database import save_picks
from injuries import is_player_injured, get_player_injury_status

logger = logging.getLogger(__name__)

BALLDONTLIE_KEY = os.getenv("BALLDONTLIE_API_KEY", "")
BASE_URL = "https://api.balldontlie.io/v1"
HEADERS = {"Authorization": BALLDONTLIE_KEY}
CACHE_FILE = "data/picks_cache.json"


# ─────────────────────────────────────────
# API HELPERS
# ─────────────────────────────────────────

def api_get(endpoint: str, params: dict = {}) -> dict | None:
    try:
        r = requests.get(f"{BASE_URL}/{endpoint}", headers=HEADERS, params=params, timeout=15)
        if r.status_code == 200:
            return r.json()
    except Exception as e:
        logger.error(f"API error {endpoint}: {e}")
    return None


def get_todays_games() -> list:
    today = date.today().isoformat()
    data = api_get("games", {"dates[]": today, "per_page": 100})
    return data.get("data", []) if data else []


def get_player_stats_recent(player_id: int, last_n: int = 10) -> list:
    """Últimos N jogos de um jogador"""
    data = api_get("stats", {
        "player_ids[]": player_id,
        "per_page": last_n,
        "sort": "date",
    })
    return data.get("data", []) if data else []


def get_team_stats_recent(team_id: int, last_n: int = 10) -> list:
    """Últimos N jogos de um time"""
    data = api_get("games", {
        "team_ids[]": team_id,
        "per_page": last_n,
        "sort": "date",
    })
    return data.get("data", []) if data else []


def get_players_by_team(team_id: int) -> list:
    data = api_get("players", {"team_ids[]": team_id, "per_page": 50})
    return data.get("data", []) if data else []


# ─────────────────────────────────────────
# FATORES ESTATÍSTICOS
# ─────────────────────────────────────────

def calc_avg(values: list) -> float:
    return sum(values) / len(values) if values else 0


def fator_historico_jogador(stats: list, mercado: str, linha: float) -> dict:
    """Quantas vezes o jogador bateu a linha nos últimos jogos"""
    if not stats:
        return {"hit_rate": 0, "media": 0, "jogos": 0}

    valores = []
    for s in stats:
        if mercado == "pts":
            valores.append(s.get("pts", 0))
        elif mercado == "ast":
            valores.append(s.get("ast", 0))
        elif mercado == "reb":
            valores.append(s.get("reb", 0))
        elif mercado == "pts_ast":
            valores.append(s.get("pts", 0) + s.get("ast", 0))
        elif mercado == "pts_reb_ast":
            valores.append(s.get("pts", 0) + s.get("reb", 0) + s.get("ast", 0))

    hits = sum(1 for v in valores if v > linha)
    media = calc_avg(valores)
    hit_rate = (hits / len(valores)) * 100 if valores else 0

    return {
        "hit_rate": round(hit_rate, 1),
        "media": round(media, 1),
        "jogos": len(valores),
        "hits": hits,
        "valores": valores
    }


def fator_casa_fora(stats: list, mercado: str, is_home: bool) -> dict:
    """Performance em casa vs fora"""
    filtrados = [s for s in stats if s.get("game", {}).get("home_team_id") == s.get("team", {}).get("id") == is_home]
    if not filtrados:
        return {"media": 0, "jogos": 0}

    valores = [s.get(mercado, 0) for s in filtrados]
    return {
        "media": round(calc_avg(valores), 1),
        "jogos": len(valores),
        "is_home": is_home
    }


def fator_back_to_back(team_games: list) -> bool:
    """Time jogou ontem?"""
    if len(team_games) < 2:
        return False
    ontem = (date.today() - timedelta(days=1)).isoformat()
    for g in team_games[:3]:
        game_date = g.get("date", "")[:10]
        if game_date == ontem:
            return True
    return False


def fator_descanso(team_games: list) -> int:
    """Dias desde o último jogo"""
    if not team_games:
        return 7
    last_game_date = team_games[0].get("date", "")[:10]
    try:
        last = datetime.strptime(last_game_date, "%Y-%m-%d").date()
        return (date.today() - last).days
    except Exception:
        return 3


def fator_momento(stats: list, mercado: str, linha: float) -> dict:
    """Últimos 3 jogos — em alta ou em baixa?"""
    if len(stats) < 3:
        return {"streak": 0, "descricao": "indefinido"}

    ultimos3 = stats[:3]
    hits = sum(1 for s in ultimos3 if s.get(mercado, 0) > linha)

    if hits == 3:
        return {"streak": 3, "descricao": "🔥 Em alta — bateu nos últimos 3 jogos"}
    elif hits == 2:
        return {"streak": 2, "descricao": "📈 Boa sequência — 2 de 3 recentes"}
    elif hits == 1:
        return {"streak": 1, "descricao": "📉 Momento irregular"}
    else:
        return {"streak": 0, "descricao": "❄️ Em baixa — não bateu nos últimos 3"}


def fator_adversario_defesa(opp_team_id: int, mercado: str) -> dict:
    """Quão fraca é a defesa adversária no mercado específico"""
    # Pega últimos 10 jogos do adversário como mandante para calcular pontos permitidos
    data = api_get("stats", {
        "team_ids[]": opp_team_id,
        "per_page": 50,
    })

    if not data:
        return {"rating": "desconhecido", "media_permitida": 0}

    stats = data.get("data", [])
    # Pega stats dos adversários (não do time)
    opp_stats = [s for s in stats if s.get("team", {}).get("id") != opp_team_id]

    if not opp_stats:
        return {"rating": "desconhecido", "media_permitida": 0}

    valores = [s.get(mercado, 0) for s in opp_stats[:20]]
    media = calc_avg(valores)

    return {
        "media_permitida": round(media, 1),
        "rating": "fraca" if media > 25 else "média" if media > 20 else "forte"
    }


def fator_minutos(stats: list) -> dict:
    """Tendência de minutos — se tá recebendo mais ou menos minutos"""
    if not stats:
        return {"media_min": 0, "tendencia": "estável"}

    minutos = []
    for s in stats:
        min_str = s.get("min", "0")
        try:
            minutos.append(float(min_str.split(":")[0]) if ":" in str(min_str) else float(min_str))
        except Exception:
            minutos.append(0)

    if not minutos:
        return {"media_min": 0, "tendencia": "estável"}

    media = calc_avg(minutos)
    recentes = calc_avg(minutos[:3]) if len(minutos) >= 3 else media
    diff = recentes - media

    tendencia = "crescendo" if diff > 2 else "caindo" if diff < -2 else "estável"
    return {"media_min": round(media, 1), "tendencia": tendencia}


# ─────────────────────────────────────────
# SCORE DE CONFIANÇA
# ─────────────────────────────────────────

def calcular_confianca(fatores: dict) -> int:
    score = 50  # Base

    # Hit rate histórico (peso alto)
    hit_rate = fatores.get("historico", {}).get("hit_rate", 50)
    score += (hit_rate - 50) * 0.5

    # Momento recente
    streak = fatores.get("momento", {}).get("streak", 1)
    score += (streak - 1) * 5

    # Back to back (negativo)
    if fatores.get("back_to_back"):
        score -= 10

    # Descanso
    descanso = fatores.get("descanso", 3)
    if descanso >= 2:
        score += 3
    elif descanso == 0:
        score -= 5

    # Defesa adversária
    rating = fatores.get("defesa_adversario", {}).get("rating", "média")
    if rating == "fraca":
        score += 8
    elif rating == "forte":
        score -= 8

    # Minutos em tendência
    tendencia = fatores.get("minutos", {}).get("tendencia", "estável")
    if tendencia == "crescendo":
        score += 5
    elif tendencia == "caindo":
        score -= 5

    return max(40, min(95, round(score)))


# ─────────────────────────────────────────
# GERAÇÃO DE PICKS
# ─────────────────────────────────────────

MERCADOS = [
    {"key": "pts", "label": "pontos", "linhas": [15.5, 17.5, 19.5, 21.5, 24.5, 26.5, 28.5, 30.5]},
    {"key": "ast", "label": "assistências", "linhas": [4.5, 5.5, 6.5, 7.5, 8.5]},
    {"key": "reb", "label": "rebotes", "linhas": [4.5, 5.5, 6.5, 7.5, 8.5, 9.5]},
    {"key": "pts_ast", "label": "pts+ast", "linhas": [24.5, 27.5, 30.5, 33.5]},
    {"key": "pts_reb_ast", "label": "pts+reb+ast", "linhas": [30.5, 35.5, 40.5, 45.5]},
]

CASAS = ["KTO", "Betano", "Sportingbet", "Bet365", "Superbet"]


def _odd_simulada(confianca: int) -> float:
    """Simula uma odd inversamente proporcional à confiança"""
    import random
    base = 2.10 - (confianca - 50) * 0.012
    variacao = random.uniform(-0.08, 0.08)
    return round(max(1.40, min(2.50, base + variacao)), 2)


async def generate_picks() -> list:
    """Gera os picks do dia com todos os fatores estatísticos"""
    games = get_todays_games()

    if not games:
        logger.warning("Nenhum jogo hoje na NBA.")
        return []

    picks = []

    for game in games[:6]:  # Limita a 6 jogos por performance
        home_team = game.get("home_team", {})
        visitor_team = game.get("visitor_team", {})
        home_id = home_team.get("id")
        visitor_id = visitor_team.get("id")

        # Verifica back to back dos times
        home_games = get_team_stats_recent(home_id, 5) if home_id else []
        visitor_games = get_team_stats_recent(visitor_id, 5) if visitor_id else []

        home_b2b = fator_back_to_back(home_games)
        visitor_b2b = fator_back_to_back(visitor_games)
        home_descanso = fator_descanso(home_games)
        visitor_descanso = fator_descanso(visitor_games)

        # Pega jogadores de cada time
        for team_id, is_home, b2b, descanso, opp_id in [
            (home_id, True, home_b2b, home_descanso, visitor_id),
            (visitor_id, False, visitor_b2b, visitor_descanso, home_id),
        ]:
            if not team_id:
                continue

            players = get_players_by_team(team_id)

            for player in players[:8]:  # Top 8 por time
                player_id = player.get("id")
                player_name = f"{player.get('first_name', '')} {player.get('last_name', '')}".strip()

                # Pula lesionados
                if is_player_injured(player_name):
                    continue

                # Busca stats recentes
                stats = get_player_stats_recent(player_id, 10)
                if not stats or len(stats) < 3:
                    continue

                # Analisa cada mercado
                for mercado in MERCADOS:
                    for linha in mercado["linhas"]:
                        historico = fator_historico_jogador(stats, mercado["key"], linha)

                        # Só considera se hit rate >= 60%
                        if historico["hit_rate"] < 60:
                            continue

                        momento = fator_momento(stats, mercado["key"], linha)
                        defesa = fator_adversario_defesa(opp_id, mercado["key"])
                        minutos = fator_minutos(stats)

                        fatores = {
                            "historico": historico,
                            "momento": momento,
                            "back_to_back": b2b,
                            "descanso": descanso,
                            "defesa_adversario": defesa,
                            "minutos": minutos,
                            "is_home": is_home,
                        }

                        confianca = calcular_confianca(fatores)

                        # Só inclui se confiança >= 62%
                        if confianca < 62:
                            continue

                        import random
                        casa = random.choice(CASAS)
                        odd = _odd_simulada(confianca)

                        injury_info = get_player_injury_status(player_name)
                        status_lesao = injury_info["status"] if injury_info else "Ativo"

                        # Resumo para versão simples
                        resumo_parts = [
                            f"{historico['hits']}/{historico['jogos']} jogos bateu",
                        ]
                        if is_home:
                            resumo_parts.append("jogando em casa")
                        if b2b:
                            resumo_parts.append("⚠️ back to back")
                        if defesa["rating"] == "fraca":
                            resumo_parts.append(f"defesa fraca do adversário")

                        pick = {
                            "jogador": player_name,
                            "mercado": f"Over {linha} {mercado['label']}",
                            "mercado_key": mercado["key"],
                            "linha": linha,
                            "odd": odd,
                            "casa": casa,
                            "confianca": confianca,
                            "resumo": " · ".join(resumo_parts),
                            "fatores": fatores,
                            "jogo": f"{home_team.get('abbreviation')} vs {visitor_team.get('abbreviation')}",
                            "status_lesao": status_lesao,
                            "is_home": is_home,
                        }

                        picks.append(pick)

    # Ordena por confiança e pega os melhores
    picks.sort(key=lambda x: x["confianca"], reverse=True)
    top_picks = picks[:10]

    if top_picks:
        save_picks(top_picks)
        # Salva cache JSON para get_cached_picks()
        os.makedirs("data", exist_ok=True)
        with open(CACHE_FILE, "w") as f:
            json.dump({"date": date.today().isoformat(), "picks": top_picks}, f, ensure_ascii=False)

    logger.info(f"Gerados {len(top_picks)} picks para hoje.")
    return top_picks


def get_cached_picks() -> list:
    """Retorna picks do cache (gerados hoje)"""
    try:
        if os.path.exists(CACHE_FILE):
            with open(CACHE_FILE) as f:
                data = json.load(f)
            if data.get("date") == date.today().isoformat():
                return data.get("picks", [])
    except Exception:
        pass
    return []


def format_pick_completo(pick: dict) -> str:
    """Formata a análise completa do pick (plano Starter)"""
    f = pick.get("fatores", {})
    historico = f.get("historico", {})
    momento = f.get("momento", {})
    defesa = f.get("defesa_adversario", {})
    minutos = f.get("minutos", {})
    b2b = f.get("back_to_back", False)
    descanso = f.get("descanso", 0)
    is_home = f.get("is_home", False)

    lines = [
        f"🏀 *{pick['jogador']} — {pick['mercado']}*",
        f"Odd: *{pick['odd']}* na {pick['casa']} · Confiança: *{pick['confianca']}%*",
        "",
        f"📊 *Histórico ({historico.get('jogos', 0)} jogos):*",
        f"Bateu {pick['mercado']} em *{historico.get('hits', 0)}/{historico.get('jogos', 0)} jogos* ({historico.get('hit_rate', 0)}%)",
        f"Média: *{historico.get('media', 0)}* no mercado",
        "",
        f"🏠 *Fator casa/fora:*",
        f"{'Jogando em casa ✅' if is_home else 'Jogando fora 🚌'}",
        "",
        f"⚡ *Fadiga:*",
        f"{'⚠️ Back to back — jogou ontem' if b2b else f'Descansado — {descanso} dia(s) desde o último jogo ✅'}",
        "",
        f"🛡️ *Defesa adversária:*",
        f"Classificação: *{defesa.get('rating', 'desconhecida')}* · Média permitida: {defesa.get('media_permitida', 0)}",
        "",
        f"📈 *Momento atual:*",
        momento.get('descricao', 'Indefinido'),
        "",
        f"⏱️ *Minutos:*",
        f"Média: *{minutos.get('media_min', 0)} min* · Tendência: {minutos.get('tendencia', 'estável')}",
        "",
        f"🏟️ *Jogo:* {pick.get('jogo', '')}",
    ]

    return "\n".join(lines)
