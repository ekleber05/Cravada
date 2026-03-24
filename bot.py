import logging
import os
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    ContextTypes, MessageHandler, filters
)
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from datetime import datetime
import asyncio

from picks import generate_picks, get_cached_picks
from injuries import get_injury_list, refresh_injury_list, poll_new_injury_reports, get_last_update_info
from database import (
    add_user, get_user, update_user_picks_seen,
    add_to_waitlist, is_on_waitlist, get_all_users
)

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

ADMIN_IDS = [int(x) for x in os.getenv("ADMIN_IDS", "").split(",") if x]
FREE_PICKS_LIMIT = 2


# ─────────────────────────────────────────
# WELCOME
# ─────────────────────────────────────────

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    add_user(user.id, user.username or user.first_name)

    text = (
        f"👋 Olá, {user.first_name}! Bem-vindo ao *Cravada*.\n\n"
        "🏀 Sou um bot de análise estatística de basquete.\n"
        "Uso inteligência artificial para recomendar as melhores apostas "
        "do dia com base em dados reais da NBA.\n\n"
        "*O que eu faço:*\n"
        "• Analiso stats de jogadores e times\n"
        "• Bloqueio picks de jogadores lesionados\n"
        "• Mando picks todo dia às 9h automaticamente\n"
        "• Respondo suas dúvidas sobre basquete\n\n"
        "⚠️ _Aposte sempre com responsabilidade._"
    )

    keyboard = [
        [InlineKeyboardButton("🏀 Picks de hoje", callback_data="picks_hoje")],
        [InlineKeyboardButton("🏥 Jogadores lesionados", callback_data="lesionados_menu")],
    ]

    await update.message.reply_text(
        text,
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )


# ─────────────────────────────────────────
# INJURIES
# ─────────────────────────────────────────

async def lesionados_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    teams = [
        ("Atlanta Hawks", "ATL"), ("Boston Celtics", "BOS"),
        ("Brooklyn Nets", "BKN"), ("Charlotte Hornets", "CHA"),
        ("Chicago Bulls", "CHI"), ("Cleveland Cavaliers", "CLE"),
        ("Dallas Mavericks", "DAL"), ("Denver Nuggets", "DEN"),
        ("Detroit Pistons", "DET"), ("Golden State Warriors", "GSW"),
        ("Houston Rockets", "HOU"), ("Indiana Pacers", "IND"),
        ("LA Clippers", "LAC"), ("Los Angeles Lakers", "LAL"),
        ("Memphis Grizzlies", "MEM"), ("Miami Heat", "MIA"),
        ("Milwaukee Bucks", "MIL"), ("Minnesota Timberwolves", "MIN"),
        ("New Orleans Pelicans", "NOP"), ("New York Knicks", "NYK"),
        ("Oklahoma City Thunder", "OKC"), ("Orlando Magic", "ORL"),
        ("Philadelphia 76ers", "PHI"), ("Phoenix Suns", "PHX"),
        ("Portland Trail Blazers", "POR"), ("Sacramento Kings", "SAC"),
        ("San Antonio Spurs", "SAS"), ("Toronto Raptors", "TOR"),
        ("Utah Jazz", "UTA"), ("Washington Wizards", "WAS"),
    ]

    keyboard = []
    row = []
    for i, (name, abbr) in enumerate(teams):
        row.append(InlineKeyboardButton(abbr, callback_data=f"lesionados_{abbr}"))
        if len(row) == 5:
            keyboard.append(row)
            row = []
    if row:
        keyboard.append(row)
    keyboard.append([InlineKeyboardButton("🔙 Voltar", callback_data="menu_principal")])

    await query.edit_message_text(
        "🏥 *Jogadores lesionados*\n\nEscolha o time:",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )


async def lesionados_time(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    abbr = query.data.replace("lesionados_", "")

    await query.edit_message_text(f"🔄 Buscando lesionados do *{abbr}*...", parse_mode="Markdown")

    injuries = get_injury_list(abbr)

    if not injuries:
        text = f"✅ *{abbr}* — Nenhum jogador lesionado no momento."
    else:
        lines = [f"🏥 *Lesionados — {abbr}*\n"]
        for p in injuries:
            status_emoji = "🔴" if p["status"].lower() == "out" else "🟡"
            lines.append(
                f"{status_emoji} *{p['name']}* — {p['status']}\n"
                f"   _{p.get('reason', 'Motivo não informado')}_"
            )
        text = "\n".join(lines)
        text += f"\n\n_Atualizado: {datetime.now().strftime('%d/%m %H:%M')}_"

    keyboard = [
        [InlineKeyboardButton("🔙 Voltar aos times", callback_data="lesionados_menu")],
        [InlineKeyboardButton("🏀 Ver picks de hoje", callback_data="picks_hoje")],
    ]

    await query.edit_message_text(
        text,
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )


# ─────────────────────────────────────────
# PICKS
# ─────────────────────────────────────────

async def picks_hoje(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id

    user = get_user(user_id)
    picks = get_cached_picks()

    if not picks:
        await query.edit_message_text(
            "⏳ Os picks de hoje ainda não foram gerados.\n"
            "Volte às 9h ou aguarde alguns instantes.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔙 Menu", callback_data="menu_principal")]
            ])
        )
        return

    picks_seen = user.get("picks_seen_today", 0)

    if picks_seen >= FREE_PICKS_LIMIT:
        await show_paywall(query)
        return

    pick = picks[picks_seen]
    pick_index = picks_seen
    update_user_picks_seen(user_id, picks_seen + 1)

    await show_pick(query, pick, pick_index, picks_seen + 1)


async def show_pick(query, pick: dict, index: int, picks_seen: int):
    confianca = pick.get("confianca", 0)
    emoji_conf = "🔥" if confianca >= 75 else "📊"

    text = (
        f"🏀 *{pick['jogador']} — {pick['mercado']}*\n"
        f"Odd: *{pick['odd']}* na {pick['casa']} · {emoji_conf} Confiança: *{confianca}%*\n"
        f"Por quê: {pick['resumo']}\n\n"
        f"_Pick {picks_seen} de {FREE_PICKS_LIMIT} do plano gratuito_"
    )

    keyboard = [
        [InlineKeyboardButton("🔍 Ver análise completa", callback_data=f"analise_{index}")],
    ]

    if picks_seen < FREE_PICKS_LIMIT:
        keyboard.append([InlineKeyboardButton("📋 Próximo pick", callback_data="picks_hoje")])
    else:
        keyboard.append([InlineKeyboardButton("📋 Ver mais picks", callback_data="paywall_picks")])

    keyboard.append([InlineKeyboardButton("🔙 Menu", callback_data="menu_principal")])

    await query.edit_message_text(
        text,
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )


async def analise_completa(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    text = (
        "🔒 *A análise completa é exclusiva do plano Starter.*\n\n"
        "No gratuito você vê o pick e o motivo principal.\n"
        "No Starter você vê *tudo* que a IA analisou:\n\n"
        "• Histórico dos últimos 10 jogos\n"
        "• Performance em casa vs fora\n"
        "• Força da defesa adversária\n"
        "• Nível de fadiga e back to back\n"
        "• Momento atual do jogador\n"
        "• Odds comparadas entre casas\n\n"
        "🚀 *Starter abre em breve com desconto de lançamento.*\n"
        "Entra na lista de espera e garante *50% off* no primeiro mês."
    )

    keyboard = [
        [InlineKeyboardButton("✅ Garantir minha vaga com desconto", callback_data="lista_espera")],
        [InlineKeyboardButton("📋 Próximo pick", callback_data="picks_hoje")],
        [InlineKeyboardButton("🔙 Menu", callback_data="menu_principal")],
    ]

    await query.edit_message_text(
        text,
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )


async def show_paywall(query):
    text = (
        "⛔ Você já viu os *2 picks gratuitos de hoje.*\n\n"
        "Ainda tem picks saindo hoje — incluindo um com alta confiança "
        "que a IA identificou agora há pouco.\n\n"
        "*Você não vai ver esse.*\n\n"
        "🚀 O plano *Starter* abre em breve.\n"
        "Entra na lista agora e garante:\n\n"
        "🟢 *1 mês com 50% de desconto* — R$9,90 no lugar de R$19,90\n"
        "🟢 *Acesso antes de todo mundo*\n"
        "🟢 *Trial estendido para 7 dias* no lugar de 3\n\n"
        "_São poucas vagas com desconto._"
    )

    keyboard = [
        [InlineKeyboardButton("✅ Garantir minha vaga", callback_data="lista_espera")],
        [InlineKeyboardButton("🔙 Menu", callback_data="menu_principal")],
    ]

    await query.edit_message_text(
        text,
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )


async def paywall_picks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await show_paywall(query)


# ─────────────────────────────────────────
# WAITLIST
# ─────────────────────────────────────────

async def lista_espera(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    username = query.from_user.username or query.from_user.first_name

    if is_on_waitlist(user_id):
        text = (
            "✅ *Você já está na lista de espera!*\n\n"
            "Quando o Starter abrir você recebe uma mensagem aqui "
            "antes de todo mundo com o link de acesso com desconto. 🟢"
        )
    else:
        add_to_waitlist(user_id, username)
        text = (
            "✅ *Vaga garantida!*\n\n"
            "Você está na lista. Quando o Starter abrir você recebe "
            "uma mensagem aqui antes de todo mundo com o link de acesso "
            "com *50% de desconto*.\n\n"
            "Enquanto isso continua recebendo os picks gratuitos todo dia às 9h. 🏀"
        )

    keyboard = [
        [InlineKeyboardButton("🏀 Ver picks de hoje", callback_data="picks_hoje")],
        [InlineKeyboardButton("🔙 Menu", callback_data="menu_principal")],
    ]

    await query.edit_message_text(
        text,
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )


# ─────────────────────────────────────────
# MENU PRINCIPAL
# ─────────────────────────────────────────

async def menu_principal(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    keyboard = [
        [InlineKeyboardButton("🏀 Picks de hoje", callback_data="picks_hoje")],
        [InlineKeyboardButton("🏥 Jogadores lesionados", callback_data="lesionados_menu")],
    ]

    await query.edit_message_text(
        "🏀 *Cravada — Menu principal*\n\nO que você quer ver?",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )


# ─────────────────────────────────────────
# ADMIN
# ─────────────────────────────────────────

async def admin_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        return

    from database import get_stats
    stats = get_stats()

    text = (
        "📊 *Status do Cravada*\n\n"
        f"👥 Usuários totais: *{stats['total_users']}*\n"
        f"✅ Na lista de espera: *{stats['waitlist']}*\n"
        f"🏀 Picks gerados hoje: *{stats['picks_today']}*\n"
        f"🕐 Última atualização de lesões: *{stats['last_injury_update']}*"
    )

    await update.message.reply_text(text, parse_mode="Markdown")


async def admin_broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        return

    if not context.args:
        await update.message.reply_text("Uso: /broadcast <mensagem>")
        return

    message = " ".join(context.args)
    users = get_all_users()
    sent = 0

    for user in users:
        try:
            await context.bot.send_message(
                chat_id=user["user_id"],
                text=message,
                parse_mode="Markdown"
            )
            sent += 1
        except Exception:
            pass

    await update.message.reply_text(f"✅ Mensagem enviada para {sent} usuários.")


# ─────────────────────────────────────────
# SCHEDULED JOBS
# ─────────────────────────────────────────

async def job_enviar_picks(app):
    """Roda todo dia às 9h — gera e envia picks para todos os usuários"""
    logger.info("Gerando picks do dia...")
    picks = await generate_picks()

    if not picks:
        logger.warning("Nenhum pick gerado hoje.")
        return

    users = get_all_users()
    text = (
        "🏀 *Picks de hoje chegaram!*\n\n"
        f"A IA analisou os jogos de hoje e separou *{len(picks)} picks*.\n\n"
        "Acesse agora 👇"
    )
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("🏀 Ver picks de hoje", callback_data="picks_hoje")]
    ])

    for user in users:
        try:
            # Reset picks vistos do dia
            update_user_picks_seen(user["user_id"], 0)
            await app.bot.send_message(
                chat_id=user["user_id"],
                text=text,
                parse_mode="Markdown",
                reply_markup=keyboard
            )
        except Exception as e:
            logger.error(f"Erro ao enviar para {user['user_id']}: {e}")


async def job_poll_injuries(app):
    """
    Roda a cada 30min das 8h às 23h (horário Brasília).
    - Detecta PDFs novos da NBA automaticamente
    - Avisa só admins se houver mudança de lesão
    - Cancela picks de jogadores OUT e avisa todos os usuários
    """
    hora_atual = datetime.now().hour
    if hora_atual < 8 or hora_atual >= 23:
        return

    logger.info("Polling injury reports...")
    result = await poll_new_injury_reports()

    if not result["found"]:
        return

    logger.info(f"PDF novo: {result['source_url']}")

    # Notifica admins sobre mudanças de lesão
    if result["new_players"] and ADMIN_IDS:
        lines = [f"🏥 *Injury report atualizado* — {result['pdfs_found_count']} PDF(s) novo(s)\n"]
        for p in result["new_players"][:10]:
            emoji = "🔴" if p["status"] == "Out" else "🟡"
            change = f" _{p.get('change', '')}_" if p.get("change") else ""
            lines.append(
                f"{emoji} *{p['name']}* ({p['team']}) — {p['status']}{change}\n"
                f"   _{p.get('reason', 'Não informado')}_"
            )
        if len(result["new_players"]) > 10:
            lines.append(f"\n_...e mais {len(result['new_players']) - 10} jogadores_")

        for admin_id in ADMIN_IDS:
            try:
                await app.bot.send_message(
                    chat_id=admin_id,
                    text="\n".join(lines),
                    parse_mode="Markdown"
                )
            except Exception as e:
                logger.error(f"Erro ao avisar admin {admin_id}: {e}")

    # Cancela picks e avisa todos os usuários
    if result["cancelled_picks"]:
        users = get_all_users()
        for pick in result["cancelled_picks"]:
            cancel_text = (
                f"⚠️ *Pick cancelado — lesão confirmada*\n\n"
                f"🔴 *{pick['jogador']}* foi marcado como OUT no injury report da NBA.\n"
                f"📋 Pick cancelado: _{pick['mercado']}_\n"
                f"🏥 Motivo: _{pick['motivo']}_\n\n"
                f"_O pick foi removido automaticamente da lista de hoje._"
            )
            for user in users:
                try:
                    await app.bot.send_message(
                        chat_id=user["user_id"],
                        text=cancel_text,
                        parse_mode="Markdown"
                    )
                except Exception:
                    pass


# ─────────────────────────────────────────
# ADMIN
# ─────────────────────────────────────────

async def admin_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        return

    from database import get_stats
    stats = get_stats()
    injury_info = get_last_update_info()

    text = (
        "📊 *Status do Cravada*\n\n"
        f"👥 Usuários totais: *{stats['total_users']}*\n"
        f"✅ Na lista de espera: *{stats['waitlist']}*\n"
        f"🏀 Picks gerados hoje: *{stats['picks_today']}*\n"
        f"🏥 Lesionados ativos: *{injury_info['total_injured']}*\n"
        f"🕐 Última injury list: *{injury_info['formatted']}*"
    )
    await update.message.reply_text(text, parse_mode="Markdown")


async def admin_genpicks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Força geração de picks agora"""
    if update.effective_user.id not in ADMIN_IDS:
        return
    await update.message.reply_text("⏳ Gerando picks agora...")
    picks = await generate_picks()
    if picks:
        await update.message.reply_text(f"✅ *{len(picks)} picks gerados!*", parse_mode="Markdown")
    else:
        await update.message.reply_text("⚠️ Nenhum pick gerado — sem jogos hoje ou API indisponível.")


async def admin_updatelesoes(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Força varredura de injury reports agora"""
    if update.effective_user.id not in ADMIN_IDS:
        return
    await update.message.reply_text("⏳ Varrendo injury reports da NBA...")
    result = await poll_new_injury_reports()

    if result["found"]:
        info = get_last_update_info()
        text = (
            f"✅ *{result['pdfs_found_count']} PDF(s) novo(s) encontrado(s)*\n"
            f"🏥 Lesionados ativos: *{info['total_injured']}*\n"
            f"📋 Mudanças detectadas: *{len(result['new_players'])}*\n"
            f"❌ Picks cancelados: *{len(result['cancelled_picks'])}*\n"
            f"🕐 Última atualização: *{info['formatted']}*"
        )
    else:
        info = get_last_update_info()
        text = (
            f"ℹ️ Nenhum PDF novo encontrado.\n"
            f"🕐 Última atualização: *{info['formatted']}*"
        )
    await update.message.reply_text(text, parse_mode="Markdown")

def main():
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    if not token:
        raise ValueError("TELEGRAM_BOT_TOKEN não definido")

    app = Application.builder().token(token).build()

    # Handlers
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("status", admin_status))
    app.add_handler(CommandHandler("genpicks", admin_genpicks))
    app.add_handler(CommandHandler("updatelesoes", admin_updatelesoes))
    app.add_handler(CommandHandler("broadcast", admin_broadcast))

    app.add_handler(CallbackQueryHandler(picks_hoje, pattern="^picks_hoje$"))
    app.add_handler(CallbackQueryHandler(lesionados_menu, pattern="^lesionados_menu$"))
    app.add_handler(CallbackQueryHandler(lesionados_time, pattern="^lesionados_[A-Z]+$"))
    app.add_handler(CallbackQueryHandler(analise_completa, pattern="^analise_\\d+$"))
    app.add_handler(CallbackQueryHandler(paywall_picks, pattern="^paywall_picks$"))
    app.add_handler(CallbackQueryHandler(lista_espera, pattern="^lista_espera$"))
    app.add_handler(CallbackQueryHandler(menu_principal, pattern="^menu_principal$"))

    # Scheduler
    scheduler = AsyncIOScheduler(timezone="America/Sao_Paulo")

    # Picks todo dia às 9h
    scheduler.add_job(
        lambda: asyncio.create_task(job_enviar_picks(app)),
        "cron", hour=9, minute=0
    )
    # Polling de injury reports a cada 30min das 8h às 23h
    scheduler.add_job(
        lambda: asyncio.create_task(job_poll_injuries(app)),
        "interval", minutes=30
    )
    scheduler.start()

    logger.info("🏀 Cravada Bot iniciado.")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
