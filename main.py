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
# NOVO: APSCHEDULER PARA MONITORAMENTO 24/7
# ============================================
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger

# ============================================
# NOVO FASE 4: IMPORTAR MÓDULO DE BANCO DE DADOS
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
# NOVO: PERSISTÊNCIA DE NICKNAMES EM JSON
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
    "metrics": {}  # NOVO: Cache de métricas calculadas
}

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

# Tracking de estados para alertas inteligentes
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
            
            # ===== NOVO FASE 4: SALVAR NO BANCO =====
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
                # ===== NOVO FASE 4: SALVAR LIQUIDAÇÃO =====
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
                # ===== NOVO FASE 4: FECHAR TRADE NO BANCO =====
                # Calcular exit_price aproximado
                exit_price = entry_px * (1 + unrealized_pnl / position_value) if position_value > 0 else entry_px
                await db.close_trade(address, coin, exit_price, unrealized_pnl)
            
            await telegram_bot.send_message(message.strip())

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
                
                # Processar posições
                positions = []
                if "assetPositions" in data:
                    for pos in data["assetPositions"]:
                        if "position" in pos:
                            p = pos["position"]
                            positions.append({
                                "coin": p.get("coin", ""),
                                "side": p.get("szi", "0")[0] if p.get("szi", "0") else "0",
                                "size": abs(safe_float(p.get("szi", 0))),
                                "szi": p.get("szi", "0"),
                                "entryPx": p.get("entryPx", "0"),
                                "positionValue": p.get("positionValue", "0"),
                                "unrealizedPnl": p.get("unrealizedPnl", "0"),
                                "leverage": p.get("leverage", {}),
                                "liquidationPx": p.get("liquidationPx", "0")
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
                
                whale_data = {
                    "address": address,
                    "nickname": nickname,
                    "positions": positions,
                    "orders": orders,
                    "total_positions": len(positions),
                    "total_orders": len(orders),
                    "total_position_value": total_position_value,
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
    tasks = [fetch_whale_data(addr, nickname) for addr, nickname in KNOWN_WHALES.items()]
    results = await asyncio.gather(*tasks)
    return results

# ============================================
# NOVO FASE 4: CALCULAR MÉTRICAS REAIS
# ============================================
async def calculate_real_metrics(current_whales_data: list) -> dict:
    """Calcula métricas reais baseadas no banco de dados"""
    try:
        # Win Rate
        win_rate_data = await db.calculate_win_rate()
        
        # Sharpe Ratio
        sharpe_data = await db.calculate_sharpe_ratio()
        
        # Portfolio Heat
        portfolio_heat = await db.calculate_portfolio_heat(current_whales_data)
        
        # Liquidações
        liquidations_1d = await db.get_liquidations_count(1)
        liquidations_1w = await db.get_liquidations_count(7)
        liquidations_1m = await db.get_liquidations_count(30)
        
        metrics = {
            "win_rate_global": win_rate_data.get("global", 0.0),
            "win_rate_long": win_rate_data.get("long", 0.0),
            "win_rate_short": win_rate_data.get("short", 0.0),
            "total_trades_analyzed": win_rate_data.get("total_trades", 0),
            "sharpe_ratio": sharpe_data.get("sharpe_ratio", 0.0),
            "sharpe_message": sharpe_data.get("message", ""),
            "portfolio_heat": portfolio_heat,
            "liquidations_1d": liquidations_1d,
            "liquidations_1w": liquidations_1w,
            "liquidations_1m": liquidations_1m,
            "last_calculated": datetime.now().isoformat()
        }
        
        return metrics
        
    except Exception as e:
        print(f"❌ Erro ao calcular métricas reais: {e}")
        # Retornar métricas mockadas em caso de erro
        return {
            "win_rate_global": 0.0,
            "win_rate_long": 0.0,
            "win_rate_short": 0.0,
            "total_trades_analyzed": 0,
            "sharpe_ratio": 0.0,
            "sharpe_message": "Database error",
            "portfolio_heat": 0.0,
            "liquidations_1d": 0,
            "liquidations_1w": 0,
            "liquidations_1m": 0,
            "error": str(e)
        }

# ============================================
# NOVO: MONITORAMENTO AUTOMÁTICO 24/7
# ============================================
async def monitor_whales_job():
    """Job que roda a cada 30 segundos monitorando as whales"""
    try:
        print(f"🔄 [{get_brt_time()}] Monitorando whales automaticamente...")
        whales = await fetch_all_whales()
        cache["whales"] = whales
        cache["last_update"] = datetime.now()
        
        # NOVO FASE 4: Calcular métricas reais
        metrics = await calculate_real_metrics(whales)
        cache["metrics"] = metrics
        
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
        "version": "3.0 - FASE 4",
        "telegram_enabled": TELEGRAM_ENABLED,
        "database_enabled": db.db_pool is not None,
        "total_whales": len(KNOWN_WHALES),
        "scheduler_running": scheduler.running,
        "endpoints": {
            "/whales": "GET - Lista todas as whales com métricas reais",
            "/whales/{address}": "GET - Dados de uma whale específica",
            "/whales": "POST - Adiciona nova whale",
            "/whales/{address}": "DELETE - Remove whale",
            "/health": "GET - Status da API",
            "/keep-alive": "GET - Mantém serviço ativo",
            "/telegram/status": "GET - Status dos alertas Telegram",
            "/telegram/send-resume": "POST - Envia resumo via Telegram",
            "/api/database/health": "GET - Status do banco de dados",
            "/api/database/backup": "GET - Backup em JSON"
        }
    }

@app.get("/whales")
async def get_whales():
    """Retorna dados de todas as whales COM MÉTRICAS REAIS"""
    whales = await fetch_all_whales()
    cache["whales"] = whales
    cache["last_update"] = datetime.now()
    
    # NOVO FASE 4: Calcular métricas reais
    metrics = await calculate_real_metrics(whales)
    cache["metrics"] = metrics
    
    return {
        "whales": whales,
        "count": len(whales),
        "metrics": metrics,  # NOVO: Métricas calculadas do banco de dados
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
        
        # NOVO: Salvar no arquivo JSON
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
        
        # NOVO: Salvar no arquivo JSON
        save_whales(KNOWN_WHALES)
        
        # Limpar estados de alerta relacionados
        keys_to_remove = [k for k in alert_state["positions"].keys() if k.startswith(address)]
        for key in keys_to_remove:
            alert_state["positions"].pop(key, None)
            alert_state["liquidation_warnings"].discard(key)
        
        keys_to_remove = [k for k in alert_state["orders"].keys() if k.startswith(address)]
        for key in keys_to_remove:
            alert_state["orders"].pop(key, None)
        
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
        "cache_age": (datetime.now() - cache["last_update"]).seconds if cache["last_update"] else None
    }

# ============================================
# NOVO: ENDPOINT KEEP-ALIVE (EVITA HIBERNAÇÃO)
# ============================================
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

# ============================================
# ENDPOINT DE STATUS DO TELEGRAM
# ============================================
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

# ============================================
# ENDPOINT PARA ENVIAR RESUMO VIA TELEGRAM
# ============================================
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

# ============================================
# NOVO FASE 4: ENDPOINTS DO BANCO DE DADOS
# ============================================

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

# ============================================
# NOVO: STARTUP E SHUTDOWN EVENTS
# ============================================
@app.on_event("startup")
async def startup_event():
    """Inicializa o scheduler e banco de dados ao subir a aplicação"""
    print("🚀 ============================================")
    print("🚀 HYPERLIQUID WHALE TRACKER API - FASE 4")
    print("🚀 ============================================")
    print(f"📊 Total de whales carregadas: {len(KNOWN_WHALES)}")
    print(f"📱 Telegram habilitado: {TELEGRAM_ENABLED}")
    
    # NOVO FASE 4: Inicializar banco de dados
    db_connected = await db.init_db()
    if db_connected:
        print("✅ PostgreSQL conectado e pronto!")
    else:
        print("⚠️ Sistema rodando sem banco de dados (métricas mockadas)")
    
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
    scheduler.shutdown()
    print("✅ Scheduler desligado")
    
    # NOVO FASE 4: Fechar conexão do banco
    await db.close_db()
    print("✅ Banco de dados fechado")
    print("👋 Sistema desligado com sucesso!")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
