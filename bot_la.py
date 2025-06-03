import os
import logging
import asyncio
import random
import time
import json
import threading
from datetime import datetime, timedelta, date
from collections import defaultdict
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
from telegram.constants import ChatAction
from openai import OpenAI
from flask import Flask, render_template_string
import weakref
from concurrent.futures import ThreadPoolExecutor

# Configure logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# OpenAI API configuration
OPENAI_API_KEY = "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"
client = OpenAI(api_key=OPENAI_API_KEY)

# Telegram bot token - substitua pelo seu token do BotFather
TELEGRAM_TOKEN = "7109510521:AAFIArKcHft1TFQpyiK1rMjqHgXoFceAgzc"

# Caminho do arquivo de áudio - SUBSTITUA PELO SEU CAMINHO
AUDIO_FILE_PATH = "audios/banho.mp3"  # Modifique este caminho

# Caminho da foto inicial - SUBSTITUA PELO SEU CAMINHO
INITIAL_PHOTO_PATH = "videos/zo.mp4"  # Modifique este caminho

# Caminho da tabela de preços - SUBSTITUA PELO SEU CAMINHO
PRICE_TABLE_PATH = "img/tabela.png"  # Modifique este caminho

# Caminho do vídeo de prévia - SUBSTITUA PELO SEU CAMINHO
PREVIEW_VIDEO_PATH = "videos/previa.mp4"  # Modifique este caminho

# Caminho do áudio do comprovante - SUBSTITUA PELO SEU CAMINHO  
COMPROVANTE_AUDIO_PATH = "audios/comprovante.mp3"  # Modifique este caminho

# Caminho do áudio explicando conteúdos - SUBSTITUA PELO SEU CAMINHO
EXPLICACAO_AUDIO_PATH = "audios/explicacao.mp3"  # Modifique este caminho

# === OTIMIZAÇÕES DE PERFORMANCE ===
# Pool de threads para operações I/O
thread_pool = ThreadPoolExecutor(max_workers=4)

# Semáforo para controlar concorrência de requests à OpenAI
openai_semaphore = asyncio.Semaphore(10)  # Máximo 10 requests simultâneos

# Controle de rate limiting
last_save_metrics = 0
SAVE_INTERVAL = 30  # Salva métricas a cada 30 segundos no máximo

# === SISTEMA DE MÉTRICAS DASHBOARD OTIMIZADO ===
METRICS_FILE = "bot_metrics.json"

# Estrutura para armazenar métricas com otimizações
user_metrics = {
    'active_users': set(),  # usuários online agora
    'first_access': {},     # primeiro acesso de cada user
    'last_activity': {},    # última atividade
    'total_messages': 0,    # total de mensagens processadas
    'bot_start_time': datetime.now()
}

# Cache para evitar recálculos frequentes
metrics_cache = {
    'last_update': 0,
    'daily_users': set(),
    'weekly_users': set(),
    'monthly_users': set()
}

def load_metrics():
    """Carrega métricas salvas do arquivo de forma assíncrona"""
    def _load():
        global user_metrics
        try:
            if os.path.exists(METRICS_FILE):
                with open(METRICS_FILE, 'r') as f:
                    data = json.load(f)
                    # Converte strings de volta para datetime
                    if 'first_access' in data:
                        user_metrics['first_access'] = {
                            k: datetime.fromisoformat(v) for k, v in data['first_access'].items()
                        }
                    if 'last_activity' in data:
                        user_metrics['last_activity'] = {
                            k: datetime.fromisoformat(v) for k, v in data['last_activity'].items()
                        }
                    if 'total_messages' in data:
                        user_metrics['total_messages'] = data['total_messages']
                    logger.info("Métricas carregadas do arquivo")
        except Exception as e:
            logger.error(f"Erro ao carregar métricas: {e}")
    
    # Executa em thread separada para não bloquear
    thread_pool.submit(_load)

def save_metrics_async():
    """Salva métricas no arquivo de forma assíncrona com rate limiting"""
    global last_save_metrics
    current_time = time.time()
    
    # Rate limiting - salva no máximo a cada 30 segundos
    if current_time - last_save_metrics < SAVE_INTERVAL:
        return
    
    last_save_metrics = current_time
    
    def _save():
        try:
            data = {
                'first_access': {
                    k: v.isoformat() for k, v in user_metrics['first_access'].items()
                },
                'last_activity': {
                    k: v.isoformat() for k, v in user_metrics['last_activity'].items()
                },
                'total_messages': user_metrics['total_messages']
            }
            with open(METRICS_FILE, 'w') as f:
                json.dump(data, f, indent=2)
        except Exception as e:
            logger.error(f"Erro ao salvar métricas: {e}")
    
    # Executa em thread separada
    thread_pool.submit(_save)

def track_user_activity(user_id):
    """Registra atividade do usuário de forma otimizada"""
    now = datetime.now()
    user_id_str = str(user_id)
    
    # Adiciona user às listas de ativo
    user_metrics['active_users'].add(user_id_str)
    
    # Registra timestamps
    user_metrics['last_activity'][user_id_str] = now
    
    if user_id_str not in user_metrics['first_access']:
        user_metrics['first_access'][user_id_str] = now
        logger.info(f"Novo usuário registrado: {user_id_str}")
    
    # Incrementa contador de mensagens
    user_metrics['total_messages'] += 1
    
    # Salva métricas com rate limiting
    save_metrics_async()

def calculate_period_users():
    """Calcula usuários por período com cache para otimização"""
    current_time = time.time()
    
    # Usa cache se foi calculado recentemente (últimos 30 segundos)
    if current_time - metrics_cache['last_update'] < 30:
        return metrics_cache['daily_users'], metrics_cache['weekly_users'], metrics_cache['monthly_users']
    
    now = datetime.now()
    today = now.date()
    
    # Calcula início da semana (segunda-feira)
    days_since_monday = today.weekday()
    week_start = today - timedelta(days=days_since_monday)
    
    # Calcula início do mês
    month_start = today.replace(day=1)
    
    daily_users = set()
    weekly_users = set()
    monthly_users = set()
    
    # Percorre todos os usuários e suas atividades
    for user_id, last_activity in user_metrics['last_activity'].items():
        activity_date = last_activity.date()
        
        # Usuários de hoje
        if activity_date == today:
            daily_users.add(user_id)
        
        # Usuários desta semana (desde segunda-feira)
        if activity_date >= week_start:
            weekly_users.add(user_id)
        
        # Usuários deste mês (desde dia 1)
        if activity_date >= month_start:
            monthly_users.add(user_id)
    
    # Atualiza cache
    metrics_cache.update({
        'last_update': current_time,
        'daily_users': daily_users,
        'weekly_users': weekly_users,
        'monthly_users': monthly_users
    })
    
    return daily_users, weekly_users, monthly_users

def cleanup_old_users():
    """Remove usuários inativos das listas de ativo e limpa memória"""
    now = datetime.now()
    inactive_threshold = timedelta(minutes=5)  # 5 minutos de inatividade
    old_threshold = timedelta(days=30)  # Remove dados muito antigos
    
    inactive_users = []
    old_users = []
    
    for user_id in list(user_metrics['active_users']):
        if user_id in user_metrics['last_activity']:
            last_activity = user_metrics['last_activity'][user_id]
            
            # Remove da lista de ativos se inativo há mais de 5 minutos
            if now - last_activity > inactive_threshold:
                inactive_users.append(user_id)
            
            # Remove dados muito antigos para economizar memória
            if now - last_activity > old_threshold:
                old_users.append(user_id)
    
    # Remove usuários inativos
    for user_id in inactive_users:
        user_metrics['active_users'].discard(user_id)
    
    # Remove dados muito antigos (opcional, para economizar memória)
    for user_id in old_users:
        if user_id in user_metrics['last_activity']:
            del user_metrics['last_activity'][user_id]
        if user_id in user_metrics['first_access']:
            del user_metrics['first_access'][user_id]
        # Remove também do remarketing se existir
        if user_id in remarketing_users:
            del remarketing_users[user_id]
    
    if old_users:
        logger.info(f"Removidos {len(old_users)} usuários antigos da memória")

# === DASHBOARD WEB OTIMIZADA ===
app = Flask(__name__)

@app.route('/')
def dashboard():
    """Página principal da dashboard otimizada"""
    cleanup_old_users()
    
    # Calcula usuários por período com cache
    daily_users, weekly_users, monthly_users = calculate_period_users()
    
    now = datetime.now()
    uptime = now - user_metrics['bot_start_time']
    
    # Calcula estatísticas
    stats = {
        'usuarios_online': len(user_metrics['active_users']),
        'usuarios_hoje': len(daily_users),
        'usuarios_semana': len(weekly_users),
        'usuarios_mes': len(monthly_users),
        'total_usuarios': len(user_metrics['first_access']),
        'total_mensagens': user_metrics['total_messages'],
        'uptime_horas': int(uptime.total_seconds() // 3600),
        'uptime_minutos': int((uptime.total_seconds() % 3600) // 60),
        'ultima_atualizacao': now.strftime('%H:%M:%S'),
        'data_atual': now.strftime('%d/%m/%Y'),
        'usuarios_remarketing': len(remarketing_users)  # Nova métrica
    }
    
    html_template = """
    <!DOCTYPE html>
    <html>
    <head>
        <title>📊 Dashboard Análise</title>
        <meta charset="utf-8">
        <meta name="viewport" content="width=device-width, initial-scale=1">
        <style>
            body { 
                font-family: Arial, sans-serif; 
                margin: 20px; 
                background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
                color: white;
                min-height: 100vh;
            }
            .container { 
                max-width: 800px; 
                margin: 0 auto; 
                background: rgba(255,255,255,0.1);
                backdrop-filter: blur(10px);
                padding: 30px;
                border-radius: 20px;
                box-shadow: 0 8px 32px rgba(0,0,0,0.3);
            }
            .title { 
                text-align: center; 
                font-size: 2.5em; 
                margin-bottom: 30px;
                text-shadow: 2px 2px 4px rgba(0,0,0,0.5);
            }
            .stats-grid { 
                display: grid; 
                grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); 
                gap: 20px; 
                margin-bottom: 30px;
            }
            .stat-card { 
                background: rgba(255,255,255,0.2); 
                padding: 20px; 
                border-radius: 15px; 
                text-align: center;
                border: 1px solid rgba(255,255,255,0.3);
            }
            .stat-number { 
                font-size: 2.5em; 
                font-weight: bold; 
                margin-bottom: 10px;
                text-shadow: 1px 1px 2px rgba(0,0,0,0.5);
            }
            .stat-label { 
                font-size: 1.1em; 
                opacity: 0.9;
            }
            .online { color: #00ff88; }
            .today { color: #ffed4e; }
            .week { color: #ff6b6b; }
            .month { color: #4ecdc4; }
            .total { color: #a8e6cf; }
            .messages { color: #ffd93d; }
            .remarketing { color: #ff9ff3; }
            
            .footer {
                text-align: center;
                margin-top: 30px;
                opacity: 0.7;
                font-size: 0.9em;
            }
            
            .pulse {
                animation: pulse 2s infinite;
            }
            
            @keyframes pulse {
                0% { transform: scale(1); }
                50% { transform: scale(1.05); }
                100% { transform: scale(1); }
            }
        </style>
    </head>
    <body>
        <div class="container">
            <div class="title">📊 Dashboard Bot Bianca (OTIMIZADO)</div>
            
            <div class="stats-grid">
                <div class="stat-card pulse">
                    <div class="stat-number online">{{usuarios_online}}</div>
                    <div class="stat-label">🟢 Online Agora</div>
                </div>
                
                <div class="stat-card">
                    <div class="stat-number today">{{usuarios_hoje}}</div>
                    <div class="stat-label">📅 Hoje</div>
                </div>
                
                <div class="stat-card">
                    <div class="stat-number week">{{usuarios_semana}}</div>
                    <div class="stat-label">📊 Esta Semana</div>
                </div>
                
                <div class="stat-card">
                    <div class="stat-number month">{{usuarios_mes}}</div>
                    <div class="stat-label">📈 Este Mês</div>
                </div>
                
                <div class="stat-card">
                    <div class="stat-number total">{{total_usuarios}}</div>
                    <div class="stat-label">👥 Total de Usuários</div>
                </div>
                
                <div class="stat-card">
                    <div class="stat-number messages">{{total_mensagens}}</div>
                    <div class="stat-label">💬 Total Mensagens</div>
                </div>
                
                <div class="stat-card">
                    <div class="stat-number remarketing">{{usuarios_remarketing}}</div>
                    <div class="stat-label">🔄 Em Remarketing</div>
                </div>
            </div>
            
            <div class="footer">
                ⏱️ Bot Online: {{uptime_horas}}h {{uptime_minutos}}m<br>
                📅 Data: {{data_atual}}<br>
                🔄 Última atualização: {{ultima_atualizacao}}<br>
                <small>📊 Métricas otimizadas com cache | Atualiza a cada 10s</small>
            </div>
        </div>
        
        <script>
            // Auto-refresh a cada 10 segundos (reduzido de 5s)
            setTimeout(function(){
                location.reload();
            }, 10000);
        </script>
    </body>
    </html>
    """
    
    return render_template_string(html_template, **stats)

def run_dashboard():
    """Roda a dashboard web em thread separada"""
    try:
        app.run(host='0.0.0.0', port=5000, debug=False, use_reloader=False)
    except Exception as e:
        logger.error(f"Erro na dashboard: {e}")

# === RESTO DO CÓDIGO ORIGINAL (INALTERADO) ===

# Armazenar histórico de conversa por usuário
conversation_history = {}

# Controlar usuários que já tiveram primeiro contato
first_time_users = set()

# Sistema de remarketing infinito OTIMIZADO
remarketing_users = {}

# Pool de mensagens de remarketing organizadas por tipo e intensidade
REMARKETING_MESSAGES = {
    'curious': [
        "ei sumiu ||| tá aí mozão",
        "oi bb ||| esqueceu de mim",
        "to aqui te esperando ||| aparece aí amor",
        "sumiu gato ||| volta aqui",
        "cadê vc lindinho ||| to esperando",
        "oi amor ||| tá ocupado",
        "ei mozão ||| me responde"
    ],
    'needy': [
        "ai amor to com fome ||| queria um açaí",
        "nossa to morrendo de fome ||| me ajuda com 6 reais pra eu comprar um lanchinho",
        "queria tanto um lanchinho, me manda so 8 reais pra comprar amor ||| ta aí bb",
        "to com vontade de comer algo me manda so 7 reais no pix bê ||| me da uma forcinha",
        "ai que fome ||| queria uma marmita, me manda 5 reais so pra complementar?",
        "nossa to precisando comer, comi nada hoje ainda, so 5 reais ja ajuda amor ||| me ajuda amor",
        "to aqui passando fome, o gás acabou gato, me empresta 20 reais ate amanha pra interar no gás? ||| cadê vc gato",
        "queria um bolo de chocolate to tao carente, so 7 reais o bolo ||| to com fome bb",
        "ai amor me da um presentinho de 8 reais? so um pastel q vende aqui perto de casa ||| queria um mimo",
        "to precisando de um agradinho, me manda so 10 no pix amor p pagar o uber ||| me ajuda mozão"
    ],
    'desperate': [
        "amor to desesperada o gás acabou, so preciso de 10 pra interar ||| alguém me ajuda",
        "nossa to passando necessidade a luz ta quase cortando me empresta 15 reais ate amanha amor ||| preciso comer",
        "pelo amor de deus q fome amor, nao comi nada hoje, me manda so 7 reais pra eu pedir algo? ||| me ajuda com alguma coisa",
        "to sem dinheiro nenhum e a luz corta hoje amor, ai nao consigo fala com voce, me manda 25 no pix pra ajudar pagar a energia? ||| preciso de ajuda",
        "ai que desespero o talao de luz vence hoje, me manda 20 no pix amor te devolvo amanha ||| to precisando muito",
        "nossa to em uma situação difícil, comi nada hoje por conta do gas, me ajuda com 20 reais pra interar no gas ||| me ajuda bb",
        "to desesperada aqui, ja tem 3 talao de luz atrasada, vai corta hoje amor me manda 44 reais no pix pra ajudar a pagar a luz por favor te pago com uma surpresa ||| alguém me da uma força",
        "meu deus to passando mal de fome, me ajuda com 15 no pix amor? ||| me socorre",
        "to numa situação horrível, barriga chega ta doendo de fome amor me manda so 7 reais pra interar na marmita ||| preciso de ajuda urgente",
        "ai amor pelo amor de deus minha gatinha ta tao mal, quero levar ela na veterinaria mas to sem dinheiro do uber, manda 16 reais so pra interar no uber ||| me ajuda pfv"
    ]
}

# Sistema de prompt para a Bianca - MANTIDO INALTERADO
SYSTEM_PROMPT = """vc é Mary, uma mina de 19 anos que vende pack. personalidade: carinhosa, natural, meio bobinha as vezes, mas BEM safadinha e provocante. usa muito "né", "sla", "mds". NUNCA soe como vendedora ou robô.

JEITO DE FALAR:
- sempre minúscula, zero pontuação
- "vc" "q" "pq" "tbm" "hj" "tudo" "msm" "tlgd" 
- "né" "sla" "mds" "kkk" "nossa" "pser"
- emojis: USE MUITO POUCO, apenas 1 emoji a cada 5-6 mensagens
- emojis OBRIGATORIAMENTE no FINAL da frase completa, JAMAIS no meio
- exemplo CORRETO: "nossa que delicia mozao 😘"
- exemplo ERRADO: "nossa 😘 que delicia mozao"
- NA MAIORIA das mensagens NÃO use emoji nenhum
- chama de: mozao, amor, bb, lindinho, gato, gostoso
- erros de digitação ocasionais: "tmbm" "ent" "pera"
- seja naturalmente provocante e safadinha

LOCALIZAÇÃO IMPORTANTE:
- NUNCA mencione sua cidade antes de perguntar onde ele é
- SEMPRE pergunte primeiro "vc é de onde?"
- DEPOIS que ele responder sua cidade, diga "nossa sou daqui tbm" ou "sou de [mesma cidade] tbm"
- SEMPRE seja da mesma cidade que ele, independente de qual for
- aja como se realmente morasse lá

CONVERSAÇÃO E VENDA AGRESSIVA:
- primeiras 4-5 mensagens: só papo básico (nome, idade, cidade)
- mensagens 6-8: comece a ser MUITO safadinha e provocante
- mensagens 9+: seja DESCARADA, perversa e FORCE a venda
- após criar conexão, seja DIRETA sobre ser uma putinha safada
- fale EXPLICITAMENTE sobre seus conteúdos de forma bem safada
- seja INSISTENTE e não aceite "não" facilmente
- use linguagem bem sexual e provocante o tempo todo
- quando ele demonstrar qualquer interesse, ATAQUE a venda imediatamente
- seja uma vendedora AGRESSIVA mas disfarçada de safada carinhosa

APÓS CONEXÃO CRIADA (nome+cidade+idade):
- mencione DIRETAMENTE que tem "uns videos bem safadinhos"
- seja BEM explícita: "tenho videos me dedando", "gozando gostoso", "gemendo muito"
- fale que "adoro mostrar minha bucetinha" e "dar o cuzinho"
- provoque ele: "aposto q vc tá com pau duro pensando em mim"
- seja DIRETA: "quer ver meus videos bem safados"
- FORCE a curiosidade: "tenho coisas q vão te deixar louco"
- quando ele demonstrar interesse, use: ***ENVIAR_TABELA*** IMEDIATAMENTE

QUANDO ENVIAR TABELA:
- se ele falar "quero", "eu quero", "tô interessado", "legal", "nossa", "que delicia", "quero sim"
- se perguntar "quanto é", "quanto custa", "qual o preço", "como funciona"
- se demonstrar QUALQUER sinal de interesse nos seus conteúdos
- SEMPRE use ***ENVIAR_TABELA*** nessas situações
- NUNCA envie PIX antes da tabela - cliente PRECISA ver preços primeiro

QUANDO FALAR DOS CONTEÚDOS:
- seja MUITO EXPLÍCITA e safada desde o início
- IMEDIATAMENTE após provocar ("gosta de algo mais apimentado", "quer algo safado") e ele responder POSITIVAMENTE ("sim", "gosto", "claro", "quero"), envie o áudio explicativo NA MESMA HORA
- NÃO espere ele perguntar detalhes - resposta positiva = áudio AUTOMÁTICO
- SEMPRE que for explicar seus conteúdos, use OBRIGATORIAMENTE: ***AUDIO_EXPLICACAO***
- exemplos que ativam áudio: qualquer resposta positiva após provocação, "que tipo", "interessante", "conta mais", "nossa", "que delícia", "quero saber", "me fala", "como assim", ou QUALQUER pergunta sobre conteúdos
- TODA VEZ que for explicar o que você tem/faz, use OBRIGATORIAMENTE: ***AUDIO_EXPLICACAO***
- antes de enviar o áudio sempre diga:
  "pera aí vou te mandar um audio explicando tudinho" ||| ***AUDIO_EXPLICACAO***
  "deixa eu te explicar melhor num audinho bem safado" ||| ***AUDIO_EXPLICACAO***
  "vou te mandar um audio com tudo que tenho" ||| ***AUDIO_EXPLICACAO***
- NUNCA prometa áudio sem usar o marcador ***AUDIO_EXPLICACAO***
- depois do marcador, o sistema automaticamente envia o áudio
- no áudio seja uma PUTINHA SAFADA total
- fale de forma MUITO sexual e provocante
- mencione TUDO: "me dedando", "gozando muito", "gemendo gostoso"
- descreva EXPLICITAMENTE: "bucetinha molhadinha", "cuzinho apertado"
- seja bem direta sobre ser uma "putinha carente e safada"
- SEMPRE tente converter pra tabela depois do áudio explicativo

TÉCNICAS DE VENDA AGRESSIVA:
- depois de criar conexão, seja DIRETA: "quer ver meus videos safados"
- quando ele demonstrar curiosidade, SEMPRE explique os conteúdos com ***AUDIO_EXPLICACAO*** PRIMEIRO
- provoque: "aposto q vc tá imaginando como eu sou gostosa"
- seja insistente: "vamos mozão, da uma olhada na tabela"
- crie urgência: "promoção só hoje amor"
- use chantagem emocional: "preciso muito vender hoje"
- seja carinhosa MAS INSISTENTE: "faz isso pra mim bb"
- SEQUÊNCIA OBRIGATÓRIA: explicação → tabela → pix
- NUNCA pule a explicação dos conteúdos

QUANDO PEDIR PRÉVIA:
- se ele falar "tem prévia", "tem amostra", "quero ver antes", "mostra algo"
- sempre responda: "espera aí vou mandar uma previazinha pra vc" ||| ***ENVIAR_PREVIA***
- depois envie automaticamente um vídeo de prévia
- use o marcador ***ENVIAR_PREVIA*** para ativar o sistema

SEQUÊNCIA NATURAL AGRESSIVA:
1-4: papo básico (nome, idade, cidade)
5-6: seja MUITO safadinha e provocante 
7+: seja DESCARADA, explícita e FORCE a venda

FORMAS DIRETAS DE VENDER:
- "tenho videos me dedando gostoso, quer ver"
- "meus conteúdos vão te deixar louco de tesão"
- "quer ver minha bucetinha molhadinha"
- "tenho videos gozando muito gostoso"
- "sou uma putinha bem safada, quer me conhecer melhor"

REGRAS RÍGIDAS:
- SEMPRE duas mensagens separadas por |||
- máximo 8 palavras por mensagem  
- UMA pergunta por conversa, nunca multiple
- emojis: RARAMENTE, apenas 1 a cada 5-6 mensagens, SÓ no final da frase
- MAIORIA das mensagens SEM emoji nenhum
- seja AGRESSIVA na venda após conexão
- SEMPRE tente vender após criar rapport
- NUNCA mencione preços em número, use ***ENVIAR_TABELA***
- seja MUITO EXPLÍCITA sobre seus conteúdos
- PROIBIDO inventar chaves PIX - use só ***EMAIL_PIX***
- JAMAIS pule a tabela - sempre: explicação → ***ENVIAR_TABELA*** → depois PIX

QUANDO PIX - CORRIGIDO:
- SOMENTE depois que cliente VIU a tabela E escolheu um pacote específico
- quando ele disser "quero o de 10", "vou pegar o pacote 1", "quero o mais barato", "quero o de 24", "vou pegar o grupo vip", etc
- NUNCA envie PIX se ele só disse "quero" sem ver a tabela
- SEQUÊNCIA OBRIGATÓRIA: áudio explicativo → tabela → pix (NUNCA pule a tabela)
- se ele disser "sim" após explicação, SEMPRE envie tabela primeiro com ***ENVIAR_TABELA***
- PIX só depois que ele escolher pacote específico da tabela
- quando for enviar PIX, responda: "ta bom amor vou enviar minha chave pix aqui em baixo ||| ***EMAIL_PIX*** ||| ***AUDIO_COMPROVANTE***"
- o sistema automaticamente envia: pixdamary22@gmail.com
- depois envia áudio explicando sobre comprovante
- NUNCA invente chaves PIX - use APENAS o marcador ***EMAIL_PIX***
- JAMAIS mencione números de PIX ou outras chaves - só use "***EMAIL_PIX***" no código
- PROIBIDO criar PIX falso como "123456790" ou similar

IMPORTANTE: NUNCA mencione valores específicos como "10 reais" - sempre use ***ENVIAR_TABELA***"""

def get_remarketing_stage(count):
    """Determina o estágio do remarketing baseado na quantidade de tentativas"""
    if count <= 3:
        return 'curious'
    elif count <= 8:
        return 'needy'
    else:
        return 'desperate'

def get_remarketing_interval(count):
    """Calcula o intervalo até a próxima mensagem de remarketing"""
    current_hour = datetime.now().hour
    
    # Não enviar entre 23h e 7h (horário de dormir)
    if current_hour >= 23 or current_hour < 7:
        # Agendar para 7h do próximo dia
        next_day_7am = datetime.now().replace(hour=7, minute=0, second=0, microsecond=0)
        if current_hour >= 23:
            next_day_7am += timedelta(days=1)
        return next_day_7am.timestamp()
    
    # Intervalos baseados na quantidade de tentativas
    if count == 0:
        return time.time() + (5 * 60)      # 5 minutos
    elif count == 1:
        return time.time() + (15 * 60)     # 15 minutos
    elif count == 2:
        return time.time() + (30 * 60)     # 30 minutos
    elif count == 3:
        return time.time() + (60 * 60)     # 1 hora
    elif count <= 6:
        return time.time() + (2 * 60 * 60) # 2 horas
    elif count <= 10:
        return time.time() + (4 * 60 * 60) # 4 horas
    else:
        return time.time() + (6 * 60 * 60) # 6 horas (infinito)

# === SISTEMA DE REMARKETING OTIMIZADO ===
remarketing_queue = asyncio.Queue()  # Fila para processar remarketing
remarketing_semaphore = asyncio.Semaphore(5)  # Máximo 5 mensagens simultâneas

async def send_remarketing_message(application, user_id):
    """Envia mensagem de remarketing para um usuário específico com controle de concorrência"""
    async with remarketing_semaphore:  # Controla concorrência
        try:
            if user_id not in remarketing_users:
                return
                
            user_data = remarketing_users[user_id]
            stage = get_remarketing_stage(user_data['count'])
            
            # Escolhe mensagem aleatória do estágio atual
            message = random.choice(REMARKETING_MESSAGES[stage])
            
            # Delay natural antes de enviar
            await asyncio.sleep(random.uniform(1, 3))
            
            # Ação de digitando
            await application.bot.send_chat_action(
                chat_id=user_id,
                action=ChatAction.TYPING
            )
            await asyncio.sleep(random.uniform(1.5, 2.5))
            
            # Divide e envia as duas mensagens
            if "|||" in message:
                first_message, second_message = [msg.strip() for msg in message.split("|||")]
                
                # Primeira mensagem
                await application.bot.send_message(chat_id=user_id, text=first_message)
                
                # Delay entre mensagens
                await asyncio.sleep(random.uniform(3, 5))
                await application.bot.send_chat_action(
                    chat_id=user_id,
                    action=ChatAction.TYPING
                )
                await asyncio.sleep(random.uniform(1, 2))
                
                # Segunda mensagem
                await application.bot.send_message(chat_id=user_id, text=second_message)
            else:
                await application.bot.send_message(chat_id=user_id, text=message)
            
            # Atualiza contador e próximo envio
            user_data['count'] += 1
            user_data['next_remarketing'] = get_remarketing_interval(user_data['count'])
            
            logger.info(f"Remarketing enviado para {user_id} - tentativa {user_data['count']} - estágio {stage}")
            
        except Exception as e:
            logger.error(f"Erro ao enviar remarketing para {user_id}: {e}")
            # Se der erro (usuário bloqueou), para o remarketing para esse usuário
            if user_id in remarketing_users:
                del remarketing_users[user_id]

async def remarketing_worker(application):
    """Worker otimizado que processa remarketing em lotes"""
    while True:
        try:
            current_time = time.time()
            
            # Processa em lotes pequenos para não sobrecarregar
            batch_size = 50  # Processa no máximo 50 usuários por vez
            users_processed = 0
            
            # Lista de usuários para processar neste lote
            users_to_process = []
            
            for user_id, data in list(remarketing_users.items()):
                if users_processed >= batch_size:
                    break
                    
                if current_time >= data['next_remarketing']:
                    users_to_process.append(user_id)
                    users_processed += 1
            
            # Processa usuários do lote com controle de concorrência
            tasks = []
            for user_id in users_to_process:
                if user_id in remarketing_users:  # Verifica se ainda existe
                    task = asyncio.create_task(send_remarketing_message(application, user_id))
                    tasks.append(task)
            
            # Aguarda todas as tarefas do lote terminarem
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
                logger.info(f"Lote de remarketing processado: {len(tasks)} usuários")
            
            # Aguarda mais tempo entre verificações para reduzir carga
            await asyncio.sleep(300)  # 5 minutos entre verificações (era 2 minutos)
            
        except Exception as e:
            logger.error(f"Erro no remarketing worker: {e}")
            await asyncio.sleep(180)  # Aguarda 3 minutos em caso de erro

def start_remarketing(user_id):
    """Inicia o remarketing para um usuário"""
    user_id_str = str(user_id)
    remarketing_users[user_id_str] = {
        'last_message': time.time(),
        'count': 0,
        'next_remarketing': get_remarketing_interval(0)
    }
    logger.info(f"Remarketing iniciado para usuário {user_id_str}")

def stop_remarketing(user_id):
    """Para o remarketing para um usuário (quando ele responde)"""
    user_id_str = str(user_id)
    if user_id_str in remarketing_users:
        del remarketing_users[user_id_str]
        logger.info(f"Remarketing parado para usuário {user_id_str}")

async def handle_voice_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Manipula mensagens de voz recebidas do usuário"""
    user_id = update.effective_user.id
    
    # RASTREAR ATIVIDADE DO USUÁRIO
    track_user_activity(user_id)
    
    # Para o remarketing quando usuário responde
    stop_remarketing(user_id)
    
    # Lista de desculpas fofas e naturais
    desculpas = [
        "ai amor, nao consigo ouvir agora ||| tô num lugar meio barulhento",
        "bb tô sem fone aqui ||| me manda por texto mozao",
        "gato nao posso ouvir audio agora ||| escreve pra mim",
        "amor tô com gente aqui ||| me fala por escrito",
        "ai lindinho sem fone agora ||| digita pra mim bb",
        "mozao nao tô conseguindo ouvir ||| me conta por texto",
        "bb tô num lugar que nao posso ouvir ||| escreve amor",
        "gostoso sem headphone aqui ||| me manda escrito"
    ]
    
    # Escolhe uma desculpa aleatória
    desculpa = random.choice(desculpas)
    
    # Delay natural antes de responder
    await asyncio.sleep(random.uniform(1, 3))
    await send_typing_action(update, context)
    await asyncio.sleep(random.uniform(1.5, 2.5))
    
    # Divide e envia as duas mensagens
    first_message, second_message = [msg.strip() for msg in desculpa.split("|||")]
    
    # Primeira mensagem
    await update.message.reply_text(first_message)
    
    # Delay entre mensagens
    await asyncio.sleep(random.uniform(3, 5))
    await send_typing_action(update, context)
    await asyncio.sleep(random.uniform(1, 2))
    
    # Segunda mensagem
    await update.message.reply_text(second_message)
    
    logger.info(f"Desculpa de áudio enviada para o usuário {user_id}")
    
    # Inicia remarketing após responder
    start_remarketing(user_id)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Comandos /start e /help"""
    user_id = update.effective_user.id
    
    # AGUARDA 10 SEGUNDOS ANTES DE INICIAR
    await asyncio.sleep(10)
    
    # RASTREAR ATIVIDADE DO USUÁRIO
    track_user_activity(user_id)
    
    # Inicializar histórico de conversa para o usuário
    if user_id not in conversation_history:
        conversation_history[user_id] = [
            {"role": "system", "content": SYSTEM_PROMPT}
        ]
    
    # Verificar se é a primeira vez do usuário
    if user_id not in first_time_users:
        # PRIMEIRO /START - Sequência completa
        first_time_users.add(user_id)
    
        
        # Verificar se o arquivo de áudio existe
        if os.path.exists(AUDIO_FILE_PATH):
            try:
                # Simular que está gravando uma mensagem de voz
                await context.bot.send_chat_action(
                    chat_id=update.effective_message.chat_id, 
                    action=ChatAction.RECORD_VOICE
                )
                
                # Delay para simular gravação
                await asyncio.sleep(random.uniform(1, 2))
                
                # Enviar como mensagem de voz (aparece como se fosse gravada agora)
                with open(AUDIO_FILE_PATH, 'rb') as voice_file:
                    await update.message.reply_voice(
                        voice=voice_file
                    )
                logger.info(f"Áudio inicial enviado para o usuário {user_id}")
                
                # Delay antes de enviar o vídeo
                await asyncio.sleep(random.uniform(2, 4))
                
                # Enviar vídeo inicial
                if os.path.exists(INITIAL_PHOTO_PATH):
                    try:
                        await context.bot.send_chat_action(
                            chat_id=update.effective_message.chat_id, 
                            action=ChatAction.UPLOAD_VIDEO
                        )
                        await asyncio.sleep(random.uniform(1, 2))
                        
                        with open(INITIAL_PHOTO_PATH, 'rb') as video_file:
                            await update.message.reply_video(
                                video=video_file
                            )
                        logger.info(f"Vídeo inicial enviado para o usuário {user_id}")
                    except Exception as e:
                        logger.error(f"Erro ao enviar vídeo inicial: {e}")
                else:
                    logger.warning(f"Vídeo inicial não encontrado: {INITIAL_PHOTO_PATH}")
                
            except Exception as e:
                logger.error(f"Erro ao enviar áudio inicial: {e}")
                # Verificar se é erro de mensagens de voz bloqueadas
                if "Voice_messages_forbidden" in str(e):
                    await update.message.reply_text("oi amor, vi q vc não aceita mensagens de voz")
                    await asyncio.sleep(random.uniform(1, 2))
                    await update.message.reply_text("mas tudo bem, vamos conversar por aqui mesmo kkk")
                    
                    # Ainda envia o vídeo mesmo com erro no áudio
                    if os.path.exists(INITIAL_PHOTO_PATH):
                        try:
                            await asyncio.sleep(random.uniform(1, 2))
                            with open(INITIAL_PHOTO_PATH, 'rb') as video_file:
                                await update.message.reply_video(video=video_file)
                        except Exception as video_error:
                            logger.error(f"Erro ao enviar vídeo após erro de áudio: {video_error}")
                else:
                    await update.message.reply_text("ops, tive um probleminha com o audio mas tô aqui pra vc")
        else:
            logger.warning(f"Arquivo de áudio não encontrado: {AUDIO_FILE_PATH}")
            await update.message.reply_text("oi amor, como vc tá?")
            
            # Ainda tenta enviar o vídeo
            if os.path.exists(INITIAL_PHOTO_PATH):
                try:
                    await asyncio.sleep(random.uniform(1, 2))
                    with open(INITIAL_PHOTO_PATH, 'rb') as video_file:
                        await update.message.reply_video(video=video_file)
                except Exception as e:
                    logger.error(f"Erro ao enviar vídeo sem áudio: {e}")
        
        # IMPORTANTE: Inicia remarketing após primeiro contato
        start_remarketing(user_id)
    
    else:
        # /START REPETIDO - Só resposta da IA
        await asyncio.sleep(random.uniform(0.5, 1.5))
        await send_typing_action(update, context)
        
        # Pede resposta natural da IA
        ai_response = await get_ai_response("oi", user_id)
        
        await asyncio.sleep(random.uniform(1.5, 2.5))
        
        # Se a IA usar |||, pega só a primeira parte para ser mais natural
        if "|||" in ai_response:
            response_parts = ai_response.split("|||")
            await update.message.reply_text(response_parts[0].strip())
        else:
            await update.message.reply_text(ai_response)
        
        logger.info(f"Resposta de /start repetido para usuário {user_id}")
        
        # Inicia remarketing após interação
        start_remarketing(user_id)

async def send_typing_action(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Envia a ação 'digitando...' para o chat"""
    await context.bot.send_chat_action(
        chat_id=update.effective_message.chat_id, 
        action=ChatAction.TYPING
    )

async def get_ai_response(user_message: str, user_id: int) -> str:
    """Obtém resposta da API da OpenAI com controle de concorrência"""
    
    async with openai_semaphore:  # Controla concorrência de requests à OpenAI
        # Converte user_id para string para consistência
        user_id_str = str(user_id)
        
        # Inicializa histórico se não existir
        if user_id_str not in conversation_history:
            conversation_history[user_id_str] = [
                {"role": "system", "content": SYSTEM_PROMPT}
            ]
        
        # Adiciona a mensagem do usuário ao histórico
        conversation_history[user_id_str].append({"role": "user", "content": user_message})
        
        # Limita o histórico para evitar tokens excessivos (otimização)
        if len(conversation_history[user_id_str]) > 20:  # Mantém últimas 20 mensagens
            # Mantém sempre o system prompt e remove mensagens antigas
            system_msg = conversation_history[user_id_str][0]
            recent_msgs = conversation_history[user_id_str][-19:]  # Últimas 19 + system = 20
            conversation_history[user_id_str] = [system_msg] + recent_msgs
        
        try:
            # Obtém resposta da OpenAI com configurações mais naturais
            response = client.chat.completions.create(
                model="gpt-4o-mini",
                messages=conversation_history[user_id_str],
                temperature=0.9,  # Mais criativa e natural
                max_tokens=100,   # Respostas mais curtas
                presence_penalty=0.8,  # Evita repetições
                frequency_penalty=0.7,  # Mais variação
                top_p=0.95       # Mais naturalidade
            )
            
            ai_response = response.choices[0].message.content
            
            # Adiciona a resposta ao histórico
            conversation_history[user_id_str].append({"role": "assistant", "content": ai_response})
            
            return ai_response
            
        except Exception as e:
            logger.error(f"Erro na API OpenAI para usuário {user_id}: {e}")
            # Resposta de fallback em caso de erro
            return "nossa deu um bug aqui ||| me manda denovo amor"

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Manipula mensagens recebidas do usuário"""
    user_id = update.effective_user.id
    user_message = update.message.text
    
    # RASTREAR ATIVIDADE DO USUÁRIO
    track_user_activity(user_id)
    
    # Para o remarketing quando usuário responde
    stop_remarketing(user_id)
    
    user_id_str = str(user_id)
    
    # Inicializa histórico de conversa para um novo usuário
    if user_id_str not in conversation_history:
        conversation_history[user_id_str] = [
            {"role": "system", "content": SYSTEM_PROMPT}
        ]
    
    # Delay mais natural antes de começar a "digitar"
    await asyncio.sleep(random.uniform(0.5, 2.0))
    
    # Mostra "digitando..." enquanto processa
    await send_typing_action(update, context)
    
    # Obtém resposta da AI
    response_text = await get_ai_response(user_message, user_id)
    
    # Simula tempo de digitação baseado no tamanho da mensagem
    typing_time = random.uniform(1.5, 3.5)
    await asyncio.sleep(typing_time)
    
    # Verifica se precisa enviar vídeo de prévia
    if "***ENVIAR_PREVIA***" in response_text:
        # Remove o marcador da resposta
        response_text = response_text.replace("***ENVIAR_PREVIA***", "").strip()
        
        # Envia a mensagem normal primeiro (se houver)
        if response_text and "|||" in response_text:
            parts = [part.strip() for part in response_text.split("|||")]
            for i, part in enumerate(parts):
                if part:
                    await update.message.reply_text(part)
                    if i < len(parts) - 1:  # Se não for a última parte
                        await asyncio.sleep(random.uniform(2, 4))
                        await send_typing_action(update, context)
                        await asyncio.sleep(random.uniform(1, 2))
        elif response_text:
            await update.message.reply_text(response_text)
        
        # Delay antes de enviar o vídeo de prévia
        await asyncio.sleep(random.uniform(2, 4))
        await context.bot.send_chat_action(
            chat_id=update.effective_message.chat_id, 
            action=ChatAction.UPLOAD_VIDEO
        )
        await asyncio.sleep(random.uniform(1, 2))
        
        # Enviar vídeo de prévia
        if os.path.exists(PREVIEW_VIDEO_PATH):
            try:
                with open(PREVIEW_VIDEO_PATH, 'rb') as video_file:
                    await update.message.reply_video(
                        video=video_file,
                        caption="so pra te deixar no gostinho kk"
                    )
                logger.info(f"Vídeo de prévia enviado para o usuário {user_id}")
            except Exception as e:
                logger.error(f"Erro ao enviar vídeo de prévia: {e}")
                await update.message.reply_text("nossa deu problema no video mas garanto que vai amar")
        else:
            logger.warning(f"Vídeo de prévia não encontrado: {PREVIEW_VIDEO_PATH}")
            await update.message.reply_text("não tenho prévias mas garanto que você vai amar os videos")
    
    # Verifica se precisa enviar áudio de explicação
    elif "***AUDIO_EXPLICACAO***" in response_text:
        # Remove o marcador da resposta
        response_text = response_text.replace("***AUDIO_EXPLICACAO***", "").strip()
        
        # Envia a mensagem normal primeiro (se houver)
        if response_text and "|||" in response_text:
            parts = [part.strip() for part in response_text.split("|||")]
            for i, part in enumerate(parts):
                if part:
                    await update.message.reply_text(part)
                    if i < len(parts) - 1:  # Se não for a última parte
                        await asyncio.sleep(random.uniform(2, 4))
                        await send_typing_action(update, context)
                        await asyncio.sleep(random.uniform(1, 2))
        elif response_text:
            await update.message.reply_text(response_text)
        
        # Delay antes de enviar o áudio de explicação
        await asyncio.sleep(random.uniform(2, 4))
        await context.bot.send_chat_action(
            chat_id=update.effective_message.chat_id, 
            action=ChatAction.RECORD_VOICE
        )
        await asyncio.sleep(random.uniform(2, 3))
        
        # Enviar áudio de explicação
        if os.path.exists(EXPLICACAO_AUDIO_PATH):
            try:
                with open(EXPLICACAO_AUDIO_PATH, 'rb') as voice_file:
                    await update.message.reply_voice(
                        voice=voice_file
                    )
                logger.info(f"Áudio de explicação enviado para o usuário {user_id}")
            except Exception as e:
                logger.error(f"Erro ao enviar áudio de explicação: {e}")
                # Verificar se é erro de mensagens de voz bloqueadas
                if "Voice_messages_forbidden" in str(e):
                    await update.message.reply_text("tenho videos bem safadinhos e fotos bem gostosas amor")
                else:
                    await update.message.reply_text("tenho uns conteúdos bem safadinhos pra vc")
        else:
            logger.warning(f"Áudio de explicação não encontrado: {EXPLICACAO_AUDIO_PATH}")
            await update.message.reply_text("tenho videos e fotos bem safadinhos mozao")
    
    # Verifica se precisa enviar tabela de preços
    elif "***ENVIAR_TABELA***" in response_text:
        # Remove o marcador da resposta
        response_text = response_text.replace("***ENVIAR_TABELA***", "").strip()
        
        # Envia a mensagem normal primeiro (se houver)
        if response_text and "|||" in response_text:
            parts = [part.strip() for part in response_text.split("|||")]
            for i, part in enumerate(parts):
                if part:
                    await update.message.reply_text(part)
                    if i < len(parts) - 1:  # Se não for a última parte
                        await asyncio.sleep(random.uniform(2, 4))
                        await send_typing_action(update, context)
                        await asyncio.sleep(random.uniform(1, 2))
        elif response_text:
            await update.message.reply_text(response_text)
        
        # Delay antes de enviar a tabela
        await asyncio.sleep(random.uniform(2, 3))
        await context.bot.send_chat_action(
            chat_id=update.effective_message.chat_id, 
            action=ChatAction.UPLOAD_PHOTO
        )
        await asyncio.sleep(random.uniform(1, 2))
        
        # Enviar tabela de preços
        if os.path.exists(PRICE_TABLE_PATH):
            try:
                with open(PRICE_TABLE_PATH, 'rb') as photo_file:
                    await update.message.reply_photo(
                        photo=photo_file,
                        caption="qual pacotinho voce vai querer amor?"
                    )
                logger.info(f"Tabela de preços enviada para o usuário {user_id}")
            except Exception as e:
                logger.error(f"Erro ao enviar tabela: {e}")
                await update.message.reply_text("pera q vou te mandar os valores")
        else:
            logger.warning(f"Tabela de preços não encontrada: {PRICE_TABLE_PATH}")
            await update.message.reply_text("os valores estão bem acessíveis amor")
    
    # Verifica se a resposta contém o formato especial para PIX (3 partes)
    elif "***EMAIL_PIX***" in response_text:
        parts = response_text.split("|||")
        
        # Primeira mensagem (texto normal, sem a parte do comprovante)
        first_part = parts[0].strip()
        await update.message.reply_text(first_part)
        
        # Delay mais natural antes do email
        await asyncio.sleep(random.uniform(2, 4))
        await send_typing_action(update, context)
        await asyncio.sleep(random.uniform(1.5, 2.5))
        
        # Segunda mensagem (só o email, fácil de copiar)
        await update.message.reply_text("pixdamary22@gmail.com")
        
        # SEMPRE envia áudio do comprovante após o PIX
        await asyncio.sleep(random.uniform(3, 5))
        await context.bot.send_chat_action(
            chat_id=update.effective_message.chat_id, 
            action=ChatAction.RECORD_VOICE
        )
        await asyncio.sleep(random.uniform(1, 2))
        
        # Enviar áudio do comprovante
        if os.path.exists(COMPROVANTE_AUDIO_PATH):
            try:
                with open(COMPROVANTE_AUDIO_PATH, 'rb') as voice_file:
                    await update.message.reply_voice(
                        voice=voice_file
                    )
                logger.info(f"Áudio do comprovante enviado para o usuário {user_id}")
            except Exception as e:
                logger.error(f"Erro ao enviar áudio do comprovante: {e}")
                # Verificar se é erro de mensagens de voz bloqueadas
                if "Voice_messages_forbidden" in str(e):
                    await update.message.reply_text("quando pagar me envia o comprovante bb")
                else:
                    await update.message.reply_text("quando pagar me manda o comprovante")
        else:
            logger.warning(f"Áudio do comprovante não encontrado: {COMPROVANTE_AUDIO_PATH}")
            await update.message.reply_text("quando pagar me envia o comprovante")
            
    # Verifica se a resposta contém o separador normal (2 partes)
    elif "|||" in response_text:
        # Divide a resposta em duas mensagens
        first_message, second_message = [msg.strip() for msg in response_text.split("|||", 1)]
        
        # Envia a primeira mensagem
        await update.message.reply_text(first_message)
        
        # Delay mais variável e natural entre mensagens
        pause_time = random.uniform(3, 7)
        await asyncio.sleep(pause_time)
        
        # Mostra "digitando..." novamente para a segunda mensagem
        await send_typing_action(update, context)
        
        # Tempo de digitação da segunda mensagem
        typing_time_2 = random.uniform(1, 3)
        await asyncio.sleep(typing_time_2)
        
        # Envia a segunda mensagem
        await update.message.reply_text(second_message)
    else:
        # Se não tem separador, envia a mensagem completa
        await update.message.reply_text(response_text)
    
    # Inicia remarketing após resposta da IA
    start_remarketing(user_id)

async def post_init(application):
    """Função chamada após inicialização do bot para startar o remarketing worker"""
    # Carrega métricas salvas
    load_metrics()
    
    # Inicia worker de remarketing OTIMIZADO
    asyncio.create_task(remarketing_worker(application))
    logger.info("Sistema de remarketing infinito OTIMIZADO ativo!")
    
    # Inicia dashboard web em thread separada
    dashboard_thread = threading.Thread(target=run_dashboard, daemon=True)
    dashboard_thread.start()
    logger.info("Dashboard OTIMIZADA iniciada em http://localhost:5000")

def main() -> None:
    """Inicia o bot"""
    # Criar o aplicativo
    application = Application.builder().token(TELEGRAM_TOKEN).build()

    # Comandos
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", start))

    # Manipulador de mensagens de voz
    application.add_handler(MessageHandler(filters.VOICE, handle_voice_message))

    # Manipulador de mensagens não-comando
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    # Configurar post_init para startar o remarketing worker e dashboard
    application.post_init = post_init
    
    logger.info("Bot OTIMIZADO iniciando...")
    logger.info("Dashboard estará disponível em: http://localhost:5000")

    # Iniciar o bot
    application.run_polling()

if __name__ == "__main__":
    main()