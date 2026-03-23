# 🏀 Cravada Bot

Bot de análise estatística de basquete para o Telegram.

---

## Setup em 5 passos

### 1. Criar o bot no Telegram
1. Abre o Telegram e vai no @BotFather
2. Manda `/newbot`
3. Escolhe o nome: `Cravada`
4. Escolhe o username: `@cravada_bot` (ou similar se já existir)
5. Guarda o **token** que ele te mandar

### 2. Pegar a chave da balldontlie.io
1. Acessa [balldontlie.io](https://balldontlie.io)
2. Cria uma conta gratuita
3. Pega a API key no painel

### 3. Descobrir seus IDs do Telegram
1. Abre o Telegram e manda mensagem pro @userinfobot
2. Ele te responde com seu ID numérico
3. Faz isso com você e seu parceiro

### 4. Subir no Railway
1. Acessa [railway.app](https://railway.app) e faz login com GitHub
2. Clica em **New Project → Deploy from GitHub repo**
3. Seleciona esse repositório
4. Vai em **Variables** e adiciona:
   ```
   TELEGRAM_BOT_TOKEN = (seu token do BotFather)
   BALLDONTLIE_API_KEY = (sua chave da balldontlie)
   ADMIN_IDS = (seu_id,id_parceiro)
   DB_PATH = data/cravada.db
   ```
5. Railway vai fazer o deploy automático

### 5. Testar
1. Abre o Telegram e manda `/start` pro seu bot
2. Se aparecer a mensagem de boas-vindas, tá funcionando ✅

---

## Comandos admin

| Comando | O que faz |
|---------|-----------|
| `/status` | Mostra usuários, lista de espera, picks do dia |
| `/broadcast <mensagem>` | Manda mensagem para todos os usuários |

---

## Estrutura

```
cravada-bot/
├── src/
│   ├── bot.py          # Bot principal e handlers
│   ├── picks.py        # Geração de picks com IA
│   ├── injuries.py     # Leitura dos PDFs da NBA
│   └── database.py     # Banco de dados SQLite
├── data/               # Criado automaticamente
│   ├── cravada.db      # Banco SQLite
│   ├── picks_cache.json
│   └── injuries_cache.json
├── requirements.txt
├── railway.toml
└── .env.example
```

---

## Como os picks são gerados

A IA analisa os seguintes fatores para cada jogador:

- ✅ Hit rate histórico (últimos 10 jogos)
- ✅ Momentum recente (últimos 3 jogos)
- ✅ Jogo em casa vs fora
- ✅ Back to back (jogou ontem?)
- ✅ Dias de descanso
- ✅ Força da defesa adversária no mercado
- ✅ Tendência de minutos
- ✅ Status de lesão (injury report da NBA)

Só gera pick se hit rate >= 60% e confiança >= 62%.

---

## Picks automáticos

O bot envia picks automaticamente todo dia às **9h horário de Brasília**.
A injury list é atualizada a cada **2 horas** lendo os PDFs oficiais da NBA.
