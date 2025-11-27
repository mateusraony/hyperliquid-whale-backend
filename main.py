from fastapi import FastAPI, HTTPException, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import List, Optional, Dict
import httpx
import asyncio
from datetime import datetime
import os
import json
from pathlib import Path

# ============================================
# APSCHEDULER PARA MONITORAMENTO 24/7
# ============================================
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger

# ============================================
# FASE 4 + FASE 5: IMPORTAR MÓDULO DE BANCO DE DADOS
# ============================================
import database as db

app = FastAPI(title="Hyperliquid Whale Tracker API")

# Configurar CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ============================================
# CONFIGURAÇÕES TELEGRAM (VARIÁVEIS DE AMBIENTE)
# ============================================
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "7530029075:AAHnQtsx0G08J9ARzouaAdH4skimhCBdCUo")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "1411468886")
TELEGRAM_ENABLED = os.getenv("TELEGRAM_ENABLED", "true").lower() == "true"

# ============================================
# PERSISTÊNCIA DE NICKNAMES EM JSON
# ============================================
WHALES_FILE = Path("whales_data.json")

# Lista inicial das 11 whales
DEFAULT_WHALES = {
    "0x010461DBc33f87b1a0f765bcAc2F96F4B3936182": "Whale 0x0104",
    "0x8c5865689EABe45645fa034e53d0c9995DCcb9c9": "Whale 0x8c58",
    "0x939f95036D2e7b6d7419Ec072BF9d967352204d2": "Whale 0x939f",
    "0x3eca9823105034b0d580dd722c75c0c23829a3d9": "Whale 0x3eca",
    "0x579f4017263b88945d727a927bf1e3d061fee5ff": "Whale 0x579f",
    "0x9eec98D048D06D9CD75318FFfA3f3960e081daAb": "Whale 0x9eec",
    "0x020ca66c30bec2c4fe3861a94e4db4a498a35872": "Whale 0x020c",
    "0xbadbb1de95b5f333623ebece7026932fa5039ee6": "Whale 0xbadb",
    "0x9e4f6D88f1e34d5F3E96451754a87Aad977Ceff3": "Whale 0x9e4f",
    "0x8d0E342E0524392d035Fb37461C6f5813ff59244": "Whale 0x8d0E",
    "0xC385D2cD1971ADfeD0E47813702765551cAe0372": "Whale 0xC385"
}

def load_whales() -> dict:
    """Carrega whales do arquivo JSON ou retorna padrão"""
    if WHALES_FILE.exists():
        try:
            with open(WHALES_FILE, 'r') as f:
                data = json.load(f)
                print(f"✅ Carregadas {len(data)} whales do arquivo")
                return data
        except Exception as e:
            print(f"⚠️ Erro ao carregar whales: {e}. Usando padrão.")
            return DEFAULT_WHALES.copy()
    else:
        print("📝 Criando arquivo de whales pela primeira vez")
        save_whales(DEFAULT_WHALES)
        return DEFAULT_WHALES.copy()

def save_whales(whales_dict: dict):
    """Salva whales no arquivo JSON"""
    try:
        with open(WHALES_FILE, 'w') as f:
            json.dump(whales_dict, f, indent=2)
        print(f"💾 Salvas {len(whales_dict)} whales no arquivo")
    except Exception as e:
        print(f"❌ Erro ao salvar whales: {e}")

# Carregar whales ao iniciar
KNOWN_WHALES = load_whales()

# Cache para armazenar dados
cache = {
    "whales": [],
    "last_update": None,
    "market_prices": {}  # 🆕 BUG FIX 1: Cache de preços de mercado
}

# ============================================
# 🆕 BUG FIX 1: BUSCAR PREÇOS REAIS DE MERCADO
# ============================================
async def fetch_market_prices() -> dict:
    """
    Busca preços atuais de mercado de TODOS os tokens via API Hyperliquid
    Retorna: {"BTC": 43250.50, "ETH": 2280.30, ...}
    """
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.post(
                "https://api.hyperliquid.xyz/info",
                json={"type": "allMids"}
            )
            
            if response.status_code == 200:
                data = response.json()
                # data é um dict: {"BTC": "43250.5", "ETH": "2280.3", ...}
                prices = {coin: float(price) for coin, price in data.items()}
                cache["market_prices"] = prices
                print(f"✅ Preços atualizados: {len(prices)} tokens")
                return prices
            else:
                print(f"⚠️ Erro ao buscar preços: HTTP {response.status_code}")
                return cache.get("market_prices", {})
    except Exception as e:
        print(f"❌ Erro ao buscar preços de mercado: {e}")
        return cache.get("market_prices", {})

# ============================================
# FUNÇÕES AUXILIARES SAFE (PREVENIR ERROS DE NONE)
# ============================================
def safe_float(value, default=0.0):
    """Converte valor para float de forma segura"""
    if value is None or value == "":
        return default
    try:
        return float(value)
    except (ValueError, TypeError):
        return default

def safe_int(value, default=0):
    """Converte valor para int de forma segura"""
    if value is None or value == "":
        return default
    try:
        return int(value)
    except (ValueError, TypeError):
        return default

# ============================================
# SISTEMA DE ALERTAS TELEGRAM
# ============================================

# 🆕 BUG FIX 2: Estado agora é carregado do banco de dados
# Será inicializado em startup_event()
alert_state = {
    "positions": {},  # {address_coin: position_data}
    "orders": {},     # {address_order: order_data}
    "liquidation_warnings": set(),  # Posições já alertadas sobre liquidação
    "last_alert_time": {}  # Controle anti-spam
}

class TelegramBot:
    """Cliente Telegram para envio de alertas"""
    
    def __init__(self, token: str, chat_id: str):
        self.token = token
        self.chat_id = chat_id
        self.base_url = f"https://api.telegram.org/bot{token}"
        self.enabled = TELEGRAM_ENABLED
    
    async def send_message(self, text: str):
        """Envia mensagem para o Telegram"""
        if not self.enabled:
            print(f"[TELEGRAM DISABLED] {text[:50]}...")
            return
        
        try:
            async with httpx.AsyncClient() as client:
                response = await client.post(
                    f"{self.base_url}/sendMessage",
                    json={
                        "chat_id": self.chat_id,
                        "text": text,
                        "parse_mode": "HTML",
                        "disable_web_page_preview": True
                    },
                    timeout=10.0
                )
                if response.status_code == 200:
                    print(f"✅ Alerta enviado: {text[:50]}...")
                else:
                    print(f"❌ Erro ao enviar alerta: {response.status_code}")
        except Exception as e:
            print(f"❌ Erro Telegram: {str(e)}")

# Instância do bot
telegram_bot = TelegramBot(TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID)

def get_brt_time():
    """Retorna horário BRT formatado"""
    from datetime import timezone, timedelta
    brt = timezone(timedelta(hours=-3))
    now = datetime.now(brt)
    return now.strftime("%d/%m %H:%M:%S")

def get_wallet_link(address: str) -> tuple:
    """Retorna o link correto da wallet (Hypurrscan ou HyperDash)"""
    # Wallet especial que usa HyperDash
    if address == "0x010461DBc33f87b1a0f765bcAc2F96F4B3936182":
        return ("HyperDash", f"https://hyperdash.io/account/{address}")
    else:
        return ("Hypurrscan", f"https://hypurrscan.io/address/{address}")

async def check_and_alert_positions(whale_data: dict):
    """Verifica posições e envia alertas inteligentes"""
    address = whale_data.get("address")
    nickname = whale_data.get("nickname", "Whale")
    positions = whale_data.get("positions", [])
    
    fonte_nome, wallet_link = get_wallet_link(address)
    
    for position in positions:
        coin = position.get("coin", "UNKNOWN")
        pos_key = f"{address}_{coin}"
        
        # ===== NOVA POSIÇÃO ABERTA =====
        if pos_key not in alert_state["positions"]:
            alert_state["positions"][pos_key] = position
            
            side = position.get("side", "").upper()
            size = abs(safe_float(position.get("szi", 0)))
            entry = safe_float(position.get("entryPx", 0))
            leverage_data = position.get("leverage", {})
            leverage = safe_float(leverage_data.get("value", 1))
            position_value = size * entry
            liquidation_px = safe_float(position.get("liquidationPx", 0))
            
            message = f"""
🟢 <b>POSIÇÃO ABERTA</b>

🐋 Wallet: {nickname}
🔗 {fonte_nome}: {wallet_link}

📊 Token: <b>{coin}</b>
{'📈 LONG' if side == 'LONG' else '📉 SHORT'}

💰 Tamanho: ${position_value:,.0f}
🎯 Alavancagem: {leverage:.1f}x
📍 Entry: ${entry:,.4f}
💀 Liquidação: ${liquidation_px:,.4f}

⏰ {get_brt_time()} BRT
"""
            await telegram_bot.send_message(message.strip())
            
            # FASE 4: SALVAR NO BANCO
            await db.save_open_trade(address, nickname, position)
        
        # ===== VERIFICAR RISCO DE LIQUIDAÇÃO (1%) =====
        else:
            position_value = safe_float(position.get("positionValue", 0))
            szi = safe_float(position.get("szi", 1))
            current_px = position_value / abs(szi) if szi != 0 else 0
            liquidation_px = safe_float(position.get("liquidationPx", 0))
            
            if liquidation_px > 0:
                distance_pct = abs((current_px - liquidation_px) / current_px) * 100 if current_px > 0 else 100
                
                # Alerta apenas 1x quando entrar na zona de 1%
                if distance_pct <= 1.0 and pos_key not in alert_state["liquidation_warnings"]:
                    alert_state["liquidation_warnings"].add(pos_key)
                    
                    side = position.get("side", "").upper()
                    coin = position.get("coin", "UNKNOWN")
                    
                    message = f"""
⚠️ <b>RISCO DE LIQUIDAÇÃO</b>

🐋 Wallet: {nickname}
🔗 {fonte_nome}: {wallet_link}

📊 Token: <b>{coin}</b>
{'📈 LONG' if side == 'LONG' else '📉 SHORT'}

💀 Liquidação: ${liquidation_px:,.4f}
📍 Preço Atual: ${current_px:,.4f}
🚨 Distância: {distance_pct:.2f}%

⏰ {get_brt_time()} BRT
"""
                    await telegram_bot.send_message(message.strip())
                
                # Remove do warning se sair da zona de perigo
                elif distance_pct > 2.0 and pos_key in alert_state["liquidation_warnings"]:
                    alert_state["liquidation_warnings"].discard(pos_key)
    
    # ===== POSIÇÃO FECHADA =====
    stored_positions = {k: v for k, v in alert_state["positions"].items() if k.startswith(address)}
    current_coins = {pos.get("coin") for pos in positions}
    
    for pos_key in list(stored_positions.keys()):
        coin = pos_key.split("_")[1]
        if coin not in current_coins:
            closed_position = alert_state["positions"].pop(pos_key)
            alert_state["liquidation_warnings"].discard(pos_key)
            
            side = closed_position.get("side", "").upper()
            unrealized_pnl = safe_float(closed_position.get("unrealizedPnl", 0))
            
            # Detectar liquidação (estava em warning + perda grande)
            was_at_risk = pos_key in alert_state["liquidation_warnings"]
            szi_value = safe_float(closed_position.get("szi", 0))
            entry_px = safe_float(closed_position.get("entryPx", 1))
            position_value = abs(szi_value) * entry_px
            loss_pct = (unrealized_pnl / position_value * 100) if position_value > 0 else 0
            
            is_liquidation = was_at_risk and loss_pct < -50
            
            if is_liquidation:
                message = f"""
💀💀 <b>POSIÇÃO LIQUIDADA</b>

🐋 Wallet: {nickname}
🔗 {fonte_nome}: {wallet_link}

📊 Token: <b>{coin}</b>
{'📈 LONG' if side == 'LONG' else '📉 SHORT'}

💵 Perda: ${unrealized_pnl:,.2f} ({loss_pct:.1f}%)
⚡ LIQUIDAÇÃO CONFIRMADA

⏰ {get_brt_time()} BRT
"""
                # FASE 4: SALVAR LIQUIDAÇÃO
                await db.save_liquidation(address, nickname, closed_position, unrealized_pnl)
            else:
                emoji = "✅" if unrealized_pnl > 0 else "❌"
                result = "LUCRO" if unrealized_pnl > 0 else "PREJUÍZO"
                
                message = f"""
{emoji} <b>POSIÇÃO FECHADA</b>

🐋 Wallet: {nickname}
🔗 {fonte_nome}: {wallet_link}

📊 Token: <b>{coin}</b>
{'📈 LONG' if side == 'LONG' else '📉 SHORT'}

💵 PnL: ${unrealized_pnl:,.2f}
🎯 Resultado: {result}

⏰ {get_brt_time()} BRT
"""
                # FASE 4: FECHAR TRADE NO BANCO
                exit_price = entry_px * (1 + unrealized_pnl / position_value) if position_value > 0 else entry_px
                await db.close_trade(address, coin, exit_price, unrealized_pnl)
            
            await telegram_bot.send_message(message.strip())
    
    # 🆕 BUG FIX 2: Salvar estado após cada verificação
    await db.save_alert_state(alert_state)

async def check_and_alert_orders(whale_data: dict):
    """Verifica orders e envia alertas"""
    address = whale_data.get("address")
    nickname = whale_data.get("nickname", "Whale")
    orders = whale_data.get("orders", [])
    
    fonte_nome, wallet_link = get_wallet_link(address)
    
    for order in orders:
        order_id = order.get("oid", "")
        order_key = f"{address}_{order_id}"
        
        # ===== NOVA ORDER CRIADA =====
        if order_key not in alert_state["orders"]:
            alert_state["orders"][order_key] = order
            
            coin = order.get("coin", "UNKNOWN")
            side = "COMPRA" if order.get("side") == "B" else "VENDA"
            size = abs(safe_float(order.get("sz", 0)))
            limit_px = safe_float(order.get("limitPx", 0))
            
            message = f"""
📝 <b>ORDER CRIADA</b>

🐋 Wallet: {nickname}
🔗 {fonte_nome}: {wallet_link}

📊 Token: <b>{coin}</b>
{'🟢 ' + side if side == 'COMPRA' else '🔴 ' + side}

💰 Quantidade: {size:,.4f}
💵 Preço Limite: ${limit_px:,.4f}

⏰ {get_brt_time()} BRT
"""
            await telegram_bot.send_message(message.strip())
    
    # ===== ORDER CONCLUÍDA/CANCELADA =====
    stored_orders = {k: v for k, v in alert_state["orders"].items() if k.startswith(address)}
    current_order_ids = {order.get("oid") for order in orders}
    
    for order_key in list(stored_orders.keys()):
        order_id = order_key.split("_", 1)[1]
        if order_id not in current_order_ids:
            closed_order = alert_state["orders"].pop(order_key)
            
            coin = closed_order.get("coin", "UNKNOWN")
            side = "COMPRA" if closed_order.get("side") == "B" else "VENDA"
            
            message = f"""
✅ <b>ORDER CONCLUÍDA/CANCELADA</b>

🐋 Wallet: {nickname}
🔗 {fonte_nome}: {wallet_link}

📊 Token: <b>{coin}</b>
{'🟢 ' + side if side == 'COMPRA' else '🔴 ' + side}

⏰ {get_brt_time()} BRT
"""
            await telegram_bot.send_message(message.strip())
    
    # 🆕 BUG FIX 2: Salvar estado após cada verificação
    await db.save_alert_state(alert_state)

# ============================================
# MODELOS PYDANTIC
# ============================================
class WhaleData(BaseModel):
    address: str
    nickname: Optional[str] = None

class AddWhaleRequest(BaseModel):
    address: str
    nickname: Optional[str] = None

# ============================================
# FUNÇÕES DE BUSCA DE DADOS
# ============================================
async def fetch_whale_data(address: str, nickname: str = None) -> dict:
    """Busca dados de uma whale na API Hyperliquid"""
    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            response = await client.post(
                "https://api.hyperliquid.xyz/info",
                json={
                    "type": "clearinghouseState",
                    "user": address
                }
            )
            
            if response.status_code == 200:
                data = response.json()
                
                # 🆕 BUG FIX 1: Buscar preços de mercado atuais
                market_prices = cache.get("market_prices", {})
                
                # Processar posições
                positions = []
                if "assetPositions" in data:
                    for pos in data["assetPositions"]:
                        if "position" in pos:
                            p = pos["position"]
                            coin = p.get("coin", "")
                            
                            # 🆕 BUG FIX 1: Adicionar markPx (preço de mercado atual)
                            mark_px = market_prices.get(coin, 0)
                            
                            positions.append({
                                "coin": coin,
                                "side": p.get("szi", "0")[0] if p.get("szi", "0") else "0",
                                "size": abs(safe_float(p.get("szi", 0))),
                                "szi": p.get("szi", "0"),
                                "entryPx": p.get("entryPx", "0"),
                                "positionValue": p.get("positionValue", "0"),
                                "unrealizedPnl": p.get("unrealizedPnl", "0"),
                                "leverage": p.get("leverage", {}),
                                "liquidationPx": p.get("liquidationPx", "0"),
                                "markPx": str(mark_px)  # 🆕 BUG FIX 1: Preço real de mercado
                            })
                
                # Processar orders
                orders = []
                if "openOrders" in data:
                    for order in data["openOrders"]:
                        orders.append({
                            "coin": order.get("coin", ""),
                            "side": order.get("side", ""),
                            "sz": order.get("sz", "0"),
                            "limitPx": order.get("limitPx", "0"),
                            "oid": order.get("oid", "")
                        })
                
                # Calcular total de posições abertas
                total_position_value = sum(
                    abs(safe_float(p.get("positionValue", 0)))
                    for p in positions
                )
                
                # Usar nickname do dicionário KNOWN_WHALES se não for passado
                if not nickname:
                    nickname = KNOWN_WHALES.get(address, f"Whale {address[:6]}")
                
                # ===== FASE 5: CALCULAR MÉTRICAS INDIVIDUAIS =====
                metrics = await db.calculate_wallet_metrics(address, positions)
                
                whale_data = {
                    "address": address,
                    "nickname": nickname,
                    "positions": positions,
                    "orders": orders,
                    "total_positions": len(positions),
                    "total_orders": len(orders),
                    "total_position_value": total_position_value,
                    "metrics": metrics,  # ✅ FASE 5: Métricas individuais
                    "last_update": datetime.now().isoformat()
                }
                
                # Verificar e enviar alertas
                await check_and_alert_positions(whale_data)
                await check_and_alert_orders(whale_data)
                
                return whale_data
            else:
                return {
                    "address": address,
                    "nickname": nickname or KNOWN_WHALES.get(address, f"Whale {address[:6]}"),
                    "error": f"API returned {response.status_code}",
                    "last_update": datetime.now().isoformat()
                }
                
    except Exception as e:
        print(f"Erro ao buscar dados da whale {address}: {str(e)}")
        return {
            "address": address,
            "nickname": nickname or KNOWN_WHALES.get(address, f"Whale {address[:6]}"),
            "error": str(e),
            "last_update": datetime.now().isoformat()
        }

async def fetch_all_whales():
    """Busca dados de todas as whales em paralelo"""
    # 🆕 BUG FIX 1: Atualizar preços de mercado ANTES de buscar whales
    await fetch_market_prices()
    
    tasks = [fetch_whale_data(addr, nickname) for addr, nickname in KNOWN_WHALES.items()]
    results = await asyncio.gather(*tasks)
    return results

# ============================================
# MONITORAMENTO AUTOMÁTICO 24/7
# ============================================
async def monitor_whales_job():
    """Job que roda a cada 30 segundos monitorando as whales"""
    try:
        print(f"🔄 [{get_brt_time()}] Monitorando whales automaticamente...")
        whales = await fetch_all_whales()
        cache["whales"] = whales
        cache["last_update"] = datetime.now()
        print(f"✅ [{get_brt_time()}] Monitoramento concluído: {len(whales)} whales")
    except Exception as e:
        print(f"❌ [{get_brt_time()}] Erro no monitoramento: {str(e)}")

# Criar scheduler
scheduler = AsyncIOScheduler()

# ============================================
# ENDPOINTS DA API
# ============================================
@app.get("/")
async def root():
    return {
        "message": "Hyperliquid Whale Tracker API",
        "version": "7.0 - FASE 7: AI WALLET TAB ✅",
        "features": [
            "✅ Whale Intelligence Scores",
            "✅ Market Sentiment Agregado",
            "✅ Whale Correlation Matrix",
            "✅ Predictive Trading Signals"
        ],
        "telegram_enabled": TELEGRAM_ENABLED,
        "database_enabled": db.db_pool is not None,
        "total_whales": len(KNOWN_WHALES),
        "scheduler_running": scheduler.running,
        "endpoints": {
            "/whales": "GET - Lista todas as whales COM MÉTRICAS INDIVIDUAIS",
            "/whales/{address}": "GET - Dados de uma whale específica",
            "/whales": "POST - Adiciona nova whale",
            "/whales/{address}": "DELETE - Remove whale",
            "/health": "GET - Status da API",
            "/keep-alive": "GET - Mantém serviço ativo",
            "/telegram/status": "GET - Status dos alertas Telegram",
            "/telegram/send-resume": "POST - Envia resumo via Telegram",
            "/api/database/health": "GET - Status do banco de dados",
            "/api/database/backup": "GET - Backup em JSON",
            "/api/database/trades": "GET - Histórico de trades",
            "🆕 /api/ai/whale-scores": "GET - Intelligence Scores por whale",
            "🆕 /api/ai/market-sentiment": "GET - Sentiment agregado do mercado",
            "🆕 /api/ai/whale-correlation": "GET - Matriz de correlação",
            "🆕 /api/ai/predictive-signals": "GET - Sinais de trading preditivos"
        }
    }

@app.get("/whales")
async def get_whales():
    """Retorna dados de todas as whales COM MÉTRICAS INDIVIDUAIS"""
    whales = await fetch_all_whales()
    cache["whales"] = whales
    cache["last_update"] = datetime.now()
    
    return {
        "whales": whales,  # ✅ FASE 5: Cada whale tem seu campo "metrics" + markPx nas posições
        "count": len(whales),
        "last_update": cache["last_update"].isoformat()
    }

@app.get("/whales/{address}")
async def get_whale(address: str):
    """Retorna dados de uma whale específica"""
    whale_data = await fetch_whale_data(address)
    return whale_data

@app.post("/whales")
async def add_whale(request: AddWhaleRequest):
    """Adiciona nova whale para monitoramento"""
    try:
        # Validar formato do endereço
        if not request.address.startswith("0x") or len(request.address) != 42:
            raise HTTPException(status_code=400, detail="Endereço inválido. Use formato 0x…")
        
        # Verificar se já existe
        if request.address in KNOWN_WHALES:
            raise HTTPException(status_code=400, detail="Whale já está sendo monitorada")
        
        # Testar se o endereço existe na Hyperliquid
        test_nickname = request.nickname or f"Whale {request.address[:6]}"
        test_data = await fetch_whale_data(request.address, test_nickname)
        
        if "error" in test_data:
            raise HTTPException(status_code=400, detail=f"Erro ao buscar whale: {test_data['error']}")
        
        # Adicionar ao dicionário com nickname
        KNOWN_WHALES[request.address] = test_nickname
        
        # Salvar no arquivo JSON
        save_whales(KNOWN_WHALES)
        
        return {
            "message": "Whale adicionada com sucesso!",
            "address": request.address,
            "nickname": test_nickname,
            "total_whales": len(KNOWN_WHALES)
        }
        
    except HTTPException:
        raise
    except Exception as e:
        print(f"Erro ao adicionar whale: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.delete("/whales/{address}")
async def delete_whale(address: str):
    """Remove uma whale do monitoramento"""
    try:
        # Verificar se existe
        if address not in KNOWN_WHALES:
            raise HTTPException(status_code=404, detail="Whale não encontrada")
        
        # Remover do dicionário
        removed_nickname = KNOWN_WHALES.pop(address)
        
        # Salvar no arquivo JSON
        save_whales(KNOWN_WHALES)
        
        # Limpar estados de alerta relacionados
        keys_to_remove = [k for k in alert_state["positions"].keys() if k.startswith(address)]
        for key in keys_to_remove:
            alert_state["positions"].pop(key, None)
            alert_state["liquidation_warnings"].discard(key)
        
        keys_to_remove = [k for k in alert_state["orders"].keys() if k.startswith(address)]
        for key in keys_to_remove:
            alert_state["orders"].pop(key, None)
        
        # 🆕 BUG FIX 2: Salvar estado atualizado
        await db.save_alert_state(alert_state)
        
        # Atualizar cache
        cache["whales"] = [w for w in cache["whales"] if w.get("address") != address]
        cache["last_update"] = datetime.now()
        
        return {
            "message": "Whale removida com sucesso!",
            "address": address,
            "nickname": removed_nickname,
            "total_whales": len(KNOWN_WHALES)
        }
        
    except HTTPException:
        raise
    except Exception as e:
        print(f"Erro ao remover whale: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/health")
async def health_check():
    """Endpoint de health check"""
    return {
        "status": "healthy",
        "timestamp": datetime.now().isoformat(),
        "total_whales": len(KNOWN_WHALES),
        "telegram_enabled": TELEGRAM_ENABLED,
        "database_connected": db.db_pool is not None,
        "scheduler_running": scheduler.running,
        "cache_age": (datetime.now() - cache["last_update"]).seconds if cache["last_update"] else None,
        "market_prices_cached": len(cache.get("market_prices", {}))
    }

@app.get("/keep-alive")
async def keep_alive():
    """Endpoint para manter o serviço ativo (cron-job.org pinga a cada 10min)"""
    return {
        "status": "alive",
        "timestamp": datetime.now().isoformat(),
        "scheduler_running": scheduler.running,
        "database_connected": db.db_pool is not None,
        "total_whales": len(KNOWN_WHALES),
        "message": "Serviço ativo e monitorando!"
    }

@app.get("/telegram/status")
async def telegram_status():
    """Retorna status dos alertas Telegram"""
    return {
        "enabled": TELEGRAM_ENABLED,
        "bot_token_configured": bool(TELEGRAM_BOT_TOKEN),
        "chat_id_configured": bool(TELEGRAM_CHAT_ID),
        "active_positions_tracked": len(alert_state["positions"]),
        "active_orders_tracked": len(alert_state["orders"]),
        "liquidation_warnings_active": len(alert_state["liquidation_warnings"]),
        "scheduler_running": scheduler.running
    }

@app.post("/telegram/send-resume")
async def send_telegram_resume():
    """Envia resumo completo via Telegram"""
    try:
        # Buscar dados atualizados de todas as whales
        whales = await fetch_all_whales()
        
        # Calcular estatísticas
        total_value = 0.0
        total_positions = 0
        whales_with_positions = 0
        
        message_lines = ["📊 <b>RESUMO GERAL - WHALES TRACKER</b>\n"]
        
        for whale in whales:
            if "error" not in whale:
                positions = whale.get("positions", [])
                if positions:
                    whales_with_positions += 1
                    total_positions += len(positions)
                    value = safe_float(whale.get("total_position_value", 0))
                    total_value += value
                    
                    fonte_nome, wallet_link = get_wallet_link(whale["address"])
                    
                    message_lines.append(
                        f"🐋 <b>{whale['nickname']}</b>\n"
                        f"   Posições: {len(positions)}\n"
                        f"   Valor: ${value:,.0f}\n"
                        f"   🔗 {fonte_nome}: {wallet_link}\n"
                    )
        
        # Adicionar totais no início
        message_lines.insert(1, 
            f"💰 <b>Total: ${total_value:,.0f}</b>\n"
            f"🐋 Whales ativas: {whales_with_positions}/{len(KNOWN_WHALES)}\n"
            f"📊 Posições abertas: {total_positions}\n"
            f"⏰ {get_brt_time()} BRT\n\n"
        )
        
        message = "\n".join(message_lines)
        
        # Enviar via Telegram
        await telegram_bot.send_message(message)
        
        return {
            "status": "success",
            "message": "Resumo enviado com sucesso!",
            "whales_ativas": whales_with_positions,
            "total_value": total_value,
            "total_positions": total_positions
        }
        
    except Exception as e:
        print(f"❌ Erro ao enviar resumo: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/database/health")
async def database_health():
    """Retorna estatísticas do banco de dados"""
    health = await db.get_database_health()
    return health

@app.get("/api/database/backup")
async def database_backup():
    """Exporta backup completo em JSON"""
    backup = await db.export_backup_json()
    return backup

# 🆕 ENDPOINT: Histórico de trades
@app.get("/api/database/trades")
async def get_trades(limit: int = 100, wallet: str = None):
    """
    Retorna histórico de trades
    - limit: número máximo de trades (padrão 100)
    - wallet: filtrar por endereço da wallet (opcional)
    """
    try:
        if not db.db_pool:
            raise HTTPException(status_code=503, detail="Banco de dados não conectado")
        
        async with db.db_pool.acquire() as conn:
            if wallet:
                query = """
                SELECT * FROM trades 
                WHERE wallet = $1
                ORDER BY open_timestamp DESC 
                LIMIT $2
                """
                trades = await conn.fetch(query, wallet, limit)
            else:
                query = """
                SELECT * FROM trades 
                ORDER BY open_timestamp DESC 
                LIMIT $1
                """
                trades = await conn.fetch(query, limit)
            
            return {
                "trades": [dict(row) for row in trades],
                "count": len(trades),
                "filtered_by_wallet": wallet
            }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

# ============================================
# 🆕 FASE 7: NOVOS ENDPOINTS - AI WALLET TAB
# ============================================

@app.get("/api/ai/whale-scores")
async def get_whale_intelligence_scores():
    """
    🧠 WHALE INTELLIGENCE SCORE
    
    Calcula score de confiabilidade para cada whale baseado em:
    - Win Rate (30%)
    - Sharpe Ratio (25%)
    - Consistency (20%) - desvio padrão dos P&Ls
    - Volume/Trade Size (15%)
    - Recent Performance (10%) - últimos 7 dias
    
    Retorna lista ordenada por score (maior para menor)
    """
    try:
        if not db.db_pool:
            raise HTTPException(status_code=503, detail="Banco de dados não conectado")
        
        whales = cache.get("whales", [])
        if not whales:
            whales = await fetch_all_whales()
            cache["whales"] = whales
        
        scores = []
        
        for whale in whales:
            if "error" in whale:
                continue
            
            address = whale.get("address")
            nickname = whale.get("nickname", "Unknown")
            metrics = whale.get("metrics", {})
            
            # Dados para cálculo
            win_rate = metrics.get("win_rate_global", 0) or 0
            sharpe = metrics.get("sharpe_ratio", 0) or 0
            total_trades = metrics.get("total_trades", 0) or 0
            total_pnl = metrics.get("total_pnl", 0) or 0
            
            # Buscar trades para cálculo de consistency
            async with db.db_pool.acquire() as conn:
                trades_query = """
                SELECT pnl FROM trades 
                WHERE wallet = $1 AND status = 'closed'
                ORDER BY close_timestamp DESC
                LIMIT 100
                """
                trades = await conn.fetch(trades_query, address)
            
            # Calcular consistency (desvio padrão dos P&Ls)
            if len(trades) >= 5:
                pnls = [float(t['pnl']) for t in trades]
                mean_pnl = sum(pnls) / len(pnls)
                variance = sum((x - mean_pnl) ** 2 for x in pnls) / len(pnls)
                std_dev = variance ** 0.5
                avg_abs_pnl = sum(abs(x) for x in pnls) / len(pnls)
                consistency = 100 - min(100, (std_dev / avg_abs_pnl * 100)) if avg_abs_pnl > 0 else 50
            else:
                consistency = 50  # Neutro se poucos trades
            
            # Calcular avg_trade_size
            if total_trades > 0:
                avg_trade_size = abs(total_pnl / total_trades) if total_trades > 0 else 0
            else:
                avg_trade_size = 0
            
            # Normalizar avg_trade_size (0-100 scale, $100K = 100 pontos)
            volume_score = min(100, (avg_trade_size / 100000) * 100)
            
            # Recent Performance (últimos 7 dias)
            async with db.db_pool.acquire() as conn:
                recent_query = """
                SELECT COALESCE(SUM(pnl), 0) as recent_pnl
                FROM trades
                WHERE wallet = $1 AND close_timestamp >= NOW() - INTERVAL '7 days'
                """
                recent_result = await conn.fetchrow(recent_query, address)
                recent_pnl = float(recent_result['recent_pnl']) if recent_result else 0
            
            recent_score = min(100, max(0, 50 + (recent_pnl / 10000) * 50))  # $10K = +50 pontos
            
            # CÁLCULO FINAL DO SCORE (0-100)
            intelligence_score = (
                (win_rate * 0.30) +           # Win Rate: 30%
                (min(100, sharpe * 25) * 0.25) +  # Sharpe: 25% (limitado a 4.0 = 100 pontos)
                (consistency * 0.20) +        # Consistency: 20%
                (volume_score * 0.15) +       # Volume: 15%
                (recent_score * 0.10)         # Recent: 10%
            )
            
            # Classificação por estrelas (1-5)
            if intelligence_score >= 85:
                stars = 5
                tier = "S-Tier"
            elif intelligence_score >= 75:
                stars = 4
                tier = "A-Tier"
            elif intelligence_score >= 65:
                stars = 3
                tier = "B-Tier"
            elif intelligence_score >= 50:
                stars = 2
                tier = "C-Tier"
            else:
                stars = 1
                tier = "D-Tier"
            
            scores.append({
                "address": address,
                "nickname": nickname,
                "intelligence_score": round(intelligence_score, 1),
                "stars": stars,
                "tier": tier,
                "breakdown": {
                    "win_rate": round(win_rate, 1),
                    "sharpe_ratio": round(sharpe, 2),
                    "consistency": round(consistency, 1),
                    "avg_trade_size": round(avg_trade_size, 2),
                    "recent_pnl_7d": round(recent_pnl, 2)
                },
                "total_trades": total_trades,
                "total_pnl": round(total_pnl, 2)
            })
        
        # Ordenar por score (maior primeiro)
        scores.sort(key=lambda x: x["intelligence_score"], reverse=True)
        
        return {
            "whale_scores": scores,
            "top_3": scores[:3] if len(scores) >= 3 else scores,
            "count": len(scores),
            "timestamp": datetime.now().isoformat()
        }
        
    except Exception as e:
        print(f"❌ Erro ao calcular whale scores: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/ai/market-sentiment")
async def get_market_sentiment():
    """
    📊 MARKET SENTIMENT AGREGADO
    
    Analisa o sentiment coletivo de todas as whales:
    - % Bullish vs Bearish (baseado em posições LONG/SHORT)
    - Tokens com maior concentração
    - Volume agregado por direção
    - Divergências importantes
    """
    try:
        whales = cache.get("whales", [])
        if not whales:
            whales = await fetch_all_whales()
            cache["whales"] = whales
        
        total_longs = 0
        total_shorts = 0
        total_volume_long = 0.0
        total_volume_short = 0.0
        
        token_concentration = {}  # {token: {"longs": X, "shorts": Y, "volume": Z, "whales": set()}}
        
        for whale in whales:
            if "error" in whale:
                continue
            
            positions = whale.get("positions", [])
            address = whale.get("address")
            
            for pos in positions:
                coin = pos.get("coin", "UNKNOWN")
                szi = safe_float(pos.get("szi", 0))
                pos_value = safe_float(pos.get("positionValue", 0))
                
                is_long = szi > 0
                
                if is_long:
                    total_longs += 1
                    total_volume_long += pos_value
                else:
                    total_shorts += 1
                    total_volume_short += pos_value
                
                # Agregar por token
                if coin not in token_concentration:
                    token_concentration[coin] = {
                        "longs": 0,
                        "shorts": 0,
                        "volume": 0.0,
                        "whales": set()
                    }
                
                token_concentration[coin]["whales"].add(address)
                token_concentration[coin]["volume"] += pos_value
                
                if is_long:
                    token_concentration[coin]["longs"] += 1
                else:
                    token_concentration[coin]["shorts"] += 1
        
        # Calcular percentuais
        total_positions = total_longs + total_shorts
        bullish_pct = (total_longs / total_positions * 100) if total_positions > 0 else 0
        bearish_pct = (total_shorts / total_positions * 100) if total_positions > 0 else 0
        
        # Sentiment global
        if bullish_pct >= 70:
            sentiment = "STRONG BULLISH"
            sentiment_icon = "🟢🟢"
        elif bullish_pct >= 55:
            sentiment = "BULLISH"
            sentiment_icon = "🟢"
        elif bearish_pct >= 70:
            sentiment = "STRONG BEARISH"
            sentiment_icon = "🔴🔴"
        elif bearish_pct >= 55:
            sentiment = "BEARISH"
            sentiment_icon = "🔴"
        else:
            sentiment = "NEUTRAL"
            sentiment_icon = "🟡"
        
        # Top tokens (ordenar por volume)
        hot_tokens = []
        for token, data in token_concentration.items():
            hot_tokens.append({
                "token": token,
                "whale_count": len(data["whales"]),
                "longs": data["longs"],
                "shorts": data["shorts"],
                "total_volume": round(data["volume"], 2),
                "consensus": "LONG" if data["longs"] > data["shorts"] else "SHORT" if data["shorts"] > data["longs"] else "MIXED"
            })
        
        hot_tokens.sort(key=lambda x: x["total_volume"], reverse=True)
        
        # Detectar divergências (whales top indo contra maioria)
        # Buscar top 3 whales
        scores_response = await get_whale_intelligence_scores()
        top_whales = scores_response.get("top_3", [])
        
        divergences = []
        for top_whale in top_whales:
            address = top_whale["address"]
            nickname = top_whale["nickname"]
            
            # Pegar posições dessa top whale
            whale_data = next((w for w in whales if w.get("address") == address), None)
            if not whale_data:
                continue
            
            positions = whale_data.get("positions", [])
            
            for pos in positions:
                coin = pos.get("coin")
                szi = safe_float(pos.get("szi", 0))
                whale_is_long = szi > 0
                
                # Ver consenso geral do token
                if coin in token_concentration:
                    token_data = token_concentration[coin]
                    majority_long = token_data["longs"] > token_data["shorts"]
                    
                    # Divergência = top whale vai contra maioria
                    if (whale_is_long and not majority_long) or (not whale_is_long and majority_long):
                        divergences.append({
                            "whale": nickname,
                            "token": coin,
                            "whale_position": "LONG" if whale_is_long else "SHORT",
                            "majority_position": "LONG" if majority_long else "SHORT",
                            "alert_level": "HIGH" if top_whale["intelligence_score"] >= 85 else "MEDIUM"
                        })
        
        return {
            "sentiment": sentiment,
            "sentiment_icon": sentiment_icon,
            "bullish_percentage": round(bullish_pct, 1),
            "bearish_percentage": round(bearish_pct, 1),
            "positions": {
                "total_longs": total_longs,
                "total_shorts": total_shorts,
                "volume_long": round(total_volume_long, 2),
                "volume_short": round(total_volume_short, 2)
            },
            "hot_tokens": hot_tokens[:10],  # Top 10
            "divergences": divergences,
            "timestamp": datetime.now().isoformat()
        }
        
    except Exception as e:
        print(f"❌ Erro ao calcular sentiment: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/ai/whale-correlation")
async def get_whale_correlation():
    """
    🔗 WHALE CORRELATION MATRIX
    
    Calcula correlação entre whales baseado em:
    - Tokens em comum
    - Direção similar (ambas LONG ou SHORT no mesmo token)
    - Timing de entrada/saída
    
    Retorna matriz de correlação e grupos de whales correlacionadas
    """
    try:
        if not db.db_pool:
            raise HTTPException(status_code=503, detail="Banco de dados não conectado")
        
        whales = cache.get("whales", [])
        if not whales:
            whales = await fetch_all_whales()
            cache["whales"] = whales
        
        # Montar perfil de cada whale (tokens + direção)
        whale_profiles = {}
        
        for whale in whales:
            if "error" in whale:
                continue
            
            address = whale.get("address")
            nickname = whale.get("nickname", "Unknown")
            positions = whale.get("positions", [])
            
            profile = {}
            for pos in positions:
                coin = pos.get("coin")
                szi = safe_float(pos.get("szi", 0))
                is_long = szi > 0
                
                profile[coin] = "LONG" if is_long else "SHORT"
            
            whale_profiles[address] = {
                "nickname": nickname,
                "profile": profile
            }
        
        # Calcular correlação entre pares
        correlation_matrix = []
        
        addresses = list(whale_profiles.keys())
        for i, addr1 in enumerate(addresses):
            for addr2 in addresses[i+1:]:
                profile1 = whale_profiles[addr1]["profile"]
                profile2 = whale_profiles[addr2]["profile"]
                
                # Tokens em comum
                common_tokens = set(profile1.keys()) & set(profile2.keys())
                
                if not common_tokens:
                    continue
                
                # Contar quantos tem mesma direção
                same_direction = sum(1 for token in common_tokens if profile1[token] == profile2[token])
                
                # Correlação = % de tokens com mesma direção
                correlation = (same_direction / len(common_tokens)) * 100
                
                if correlation >= 50:  # Só mostrar correlações relevantes
                    correlation_matrix.append({
                        "whale1": whale_profiles[addr1]["nickname"],
                        "whale1_address": addr1,
                        "whale2": whale_profiles[addr2]["nickname"],
                        "whale2_address": addr2,
                        "correlation": round(correlation, 1),
                        "common_tokens": len(common_tokens),
                        "same_direction_count": same_direction
                    })
        
        # Ordenar por correlação
        correlation_matrix.sort(key=lambda x: x["correlation"], reverse=True)
        
        # Identificar grupos (whales com correlação > 75%)
        groups = []
        high_correlation = [c for c in correlation_matrix if c["correlation"] >= 75]
        
        if high_correlation:
            # Agrupar whales altamente correlacionadas
            visited = set()
            for corr in high_correlation:
                addr1 = corr["whale1_address"]
                addr2 = corr["whale2_address"]
                
                if addr1 not in visited or addr2 not in visited:
                    group_members = {addr1, addr2}
                    visited.add(addr1)
                    visited.add(addr2)
                    
                    # Procurar outras com correlação alta com este grupo
                    for other in high_correlation:
                        if other["whale1_address"] in group_members or other["whale2_address"] in group_members:
                            group_members.add(other["whale1_address"])
                            group_members.add(other["whale2_address"])
                    
                    groups.append({
                        "group_id": len(groups) + 1,
                        "members": [whale_profiles[addr]["nickname"] for addr in group_members],
                        "size": len(group_members)
                    })
        
        return {
            "correlation_matrix": correlation_matrix[:20],  # Top 20
            "highly_correlated_groups": groups,
            "total_pairs_analyzed": len(addresses) * (len(addresses) - 1) // 2,
            "significant_correlations": len(correlation_matrix),
            "timestamp": datetime.now().isoformat()
        }
        
    except Exception as e:
        print(f"❌ Erro ao calcular correlação: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/ai/predictive-signals")
async def get_predictive_signals():
    """
    🎯 PREDICTIVE TRADING SIGNALS
    
    Gera sinais de trading baseados em padrões históricos:
    - STRONG BUY: 3+ top whales abriram LONG recentemente
    - CAUTION: Whale líder fechou grande parte da posição
    - WATCH: Acumulação silenciosa de whales
    
    Cada sinal tem confidence score baseado em dados históricos
    """
    try:
        if not db.db_pool:
            raise HTTPException(status_code=503, detail="Banco de dados não conectado")
        
        whales = cache.get("whales", [])
        if not whales:
            whales = await fetch_all_whales()
            cache["whales"] = whales
        
        # Buscar top whales
        scores_response = await get_whale_intelligence_scores()
        top_whales_data = scores_response.get("whale_scores", [])
        top_3_addresses = [w["address"] for w in top_whales_data[:3]]
        
        signals = []
        
        # Buscar trades recentes (últimas 4 horas)
        async with db.db_pool.acquire() as conn:
            recent_trades_query = """
            SELECT wallet, token, side, size, entry_price, open_timestamp
            FROM trades
            WHERE open_timestamp >= NOW() - INTERVAL '4 hours'
            AND status = 'open'
            ORDER BY open_timestamp DESC
            """
            recent_trades = await conn.fetch(recent_trades_query)
        
        # Agrupar por token
        token_activity = {}
        for trade in recent_trades:
            token = trade['token']
            wallet = trade['wallet']
            side = trade['side']
            size = float(trade['size'])
            
            if token not in token_activity:
                token_activity[token] = {
                    "longs": [],
                    "shorts": [],
                    "top_whale_longs": 0,
                    "top_whale_shorts": 0,
                    "total_volume": 0
                }
            
            token_activity[token]["total_volume"] += size
            
            if side.lower().startswith('l') or 'long' in side.lower():
                token_activity[token]["longs"].append(wallet)
                if wallet in top_3_addresses:
                    token_activity[token]["top_whale_longs"] += 1
            else:
                token_activity[token]["shorts"].append(wallet)
                if wallet in top_3_addresses:
                    token_activity[token]["top_whale_shorts"] += 1
        
        # SINAL 1: STRONG BUY - 3+ top whales abriram LONG
        for token, activity in token_activity.items():
            if activity["top_whale_longs"] >= 3:
                # Calcular confidence baseado em win rate histórica do token
                async with db.db_pool.acquire() as conn:
                    history_query = """
                    SELECT 
                        COUNT(*) FILTER (WHERE pnl > 0) as wins,
                        COUNT(*) as total
                    FROM trades
                    WHERE token = $1 AND status = 'closed'
                    AND close_timestamp >= NOW() - INTERVAL '30 days'
                    """
                    history = await conn.fetchrow(history_query, token)
                
                if history and history['total'] > 0:
                    win_rate = (history['wins'] / history['total']) * 100
                    confidence = min(95, 70 + (win_rate - 50) * 0.5)  # Base 70%, ajuste por histórico
                else:
                    confidence = 75  # Padrão sem histórico
                
                signals.append({
                    "signal_type": "STRONG BUY",
                    "token": token,
                    "confidence": round(confidence, 1),
                    "reason": f"{activity['top_whale_longs']} top whales abriram LONG nas últimas 4h",
                    "volume": round(activity["total_volume"], 2),
                    "color": "green",
                    "icon": "🟢"
                })
        
        # SINAL 2: CAUTION - Whale líder reduziu posição
        for whale_data in whales:
            if "error" in whale_data:
                continue
            
            address = whale_data.get("address")
            if address not in top_3_addresses:
                continue
            
            # Buscar posições fechadas recentemente (últimas 24h)
            async with db.db_pool.acquire() as conn:
                closed_query = """
                SELECT token, size, pnl
                FROM trades
                WHERE wallet = $1 
                AND status = 'closed'
                AND close_timestamp >= NOW() - INTERVAL '24 hours'
                """
                closed = await conn.fetch(closed_query, address)
            
            for trade in closed:
                token = trade['token']
                size = float(trade['size'])
                pnl = float(trade['pnl'])
                
                # Se fechou com lucro e era grande (> $50K)
                if pnl > 0 and size > 50000:
                    # Verificar se esse token tem histórico de queda após top whale sair
                    async with db.db_pool.acquire() as conn:
                        pattern_query = """
                        SELECT COUNT(*) as occurrences
                        FROM trades
                        WHERE token = $1
                        AND close_timestamp >= NOW() - INTERVAL '90 days'
                        """
                        pattern = await conn.fetchrow(pattern_query, token)
                    
                    confidence = 72  # Base conservadora
                    
                    signals.append({
                        "signal_type": "CAUTION",
                        "token": token,
                        "confidence": confidence,
                        "reason": f"Top whale fechou ${size:,.0f} em {token} (lucro: ${pnl:,.0f})",
                        "volume": size,
                        "color": "yellow",
                        "icon": "🟡"
                    })
        
        # SINAL 3: WATCH - Acumulação silenciosa (2+ whales, baixo volume individual)
        for token, activity in token_activity.items():
            unique_whales = len(set(activity["longs"]))
            if unique_whales >= 2 and activity["total_volume"] < 100000:  # Baixo volume = acumulação
                signals.append({
                    "signal_type": "WATCH",
                    "token": token,
                    "confidence": 65,
                    "reason": f"{unique_whales} whales acumulando {token} silenciosamente",
                    "volume": round(activity["total_volume"], 2),
                    "color": "blue",
                    "icon": "🔵"
                })
        
        # Ordenar por confidence
        signals.sort(key=lambda x: x["confidence"], reverse=True)
        
        return {
            "signals": signals,
            "strong_buy_count": len([s for s in signals if s["signal_type"] == "STRONG BUY"]),
            "caution_count": len([s for s in signals if s["signal_type"] == "CAUTION"]),
            "watch_count": len([s for s in signals if s["signal_type"] == "WATCH"]),
            "timestamp": datetime.now().isoformat()
        }
        
    except Exception as e:
        print(f"❌ Erro ao gerar sinais: {e}")
        raise HTTPException(status_code=500, detail=str(e))

# ============================================
# STARTUP E SHUTDOWN EVENTS
# ============================================
@app.on_event("startup")
async def startup_event():
    """Inicializa o scheduler e banco de dados ao subir a aplicação"""
    global alert_state
    
    print("🚀 ============================================")
    print("🚀 HYPERLIQUID WHALE TRACKER API - v7.0")
    print("🚀 ✅ FASE 7: AI WALLET TAB - INSTITUCIONAL")
    print("🚀 ✅ Whale Intelligence Scores")
    print("🚀 ✅ Market Sentiment Agregado")
    print("🚀 ✅ Whale Correlation Matrix")
    print("🚀 ✅ Predictive Trading Signals")
    print("🚀 ============================================")
    print(f"📊 Total de whales carregadas: {len(KNOWN_WHALES)}")
    print(f"📱 Telegram habilitado: {TELEGRAM_ENABLED}")
    
    # Inicializar banco de dados
    db_connected = await db.init_db()
    if db_connected:
        print("✅ PostgreSQL conectado e pronto!")
        
        # 🆕 BUG FIX 2: Carregar estado de alertas do banco
        loaded_state = await db.load_alert_state()
        if loaded_state:
            alert_state.update(loaded_state)
            print(f"✅ Estado de alertas carregado do banco: {len(alert_state['positions'])} posições, {len(alert_state['orders'])} orders")
        else:
            print("📝 Nenhum estado anterior encontrado, iniciando do zero")
    else:
        print("⚠️ Sistema rodando sem banco de dados (métricas não disponíveis)")
    
    # 🆕 BUG FIX 1: Buscar preços iniciais
    print("🔄 Buscando preços de mercado iniciais...")
    await fetch_market_prices()
    print(f"✅ {len(cache.get('market_prices', {}))} preços carregados")
    
    # Adicionar job de monitoramento a cada 30 segundos
    scheduler.add_job(
        monitor_whales_job,
        trigger=IntervalTrigger(seconds=30),
        id='monitor_whales',
        name='Monitorar whales a cada 30s',
        replace_existing=True
    )
    
    # Iniciar scheduler
    scheduler.start()
    print("✅ Scheduler iniciado! Monitoramento 24/7 ativo.")
    print("⏰ Monitoramento automático a cada 30 segundos")
    print("🚀 ============================================\n")
    
    # Executar primeira verificação imediatamente
    await monitor_whales_job()

@app.on_event("shutdown")
async def shutdown_event():
    """Para o scheduler e fecha banco ao desligar a aplicação"""
    print("\n🛑 Desligando sistema...")
    
    # 🆕 BUG FIX 2: Salvar estado antes de desligar
    if db.db_pool:
        await db.save_alert_state(alert_state)
        print("✅ Estado de alertas salvo no banco")
    
    scheduler.shutdown()
    print("✅ Scheduler desligado")
    
    # Fechar conexão do banco
    await db.close_db()
    print("✅ Banco de dados fechado")
    print("👋 Sistema desligado com sucesso!")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
