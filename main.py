from fastapi import FastAPI, HTTPException, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import List, Optional, Dict
import httpx
import asyncio
from datetime import datetime
import os

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
# LISTA DAS 11 WHALES VÁLIDAS (NÃO ALTERAR!)
# ============================================
# Estrutura: {address: nickname}
KNOWN_WHALES = {
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

# Cache para armazenar dados (NÃO ALTERAR!)
cache = {
    "whales": [],
    "last_update": None
}

# ============================================
# NOVO: FUNÇÕES AUXILIARES SAFE (PREVENIR ERROS DE NONE)
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
# NOVO: SISTEMA DE ALERTAS TELEGRAM
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
            print(f"[TELEGRAM DISABLED] {text}")
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
            size = abs(safe_float(position.get("szi", 0)))  # SAFE_FLOAT
            entry = safe_float(position.get("entryPx", 0))  # SAFE_FLOAT
            leverage_data = position.get("leverage", {})
            leverage = safe_float(leverage_data.get("value", 1))  # SAFE_FLOAT
            position_value = size * entry
            liquidation_px = safe_float(position.get("liquidationPx", 0))  # SAFE_FLOAT
            
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
        
        # ===== VERIFICAR RISCO DE LIQUIDAÇÃO (1%) =====
        else:
            position_value = safe_float(position.get("positionValue", 0))  # SAFE_FLOAT
            szi = safe_float(position.get("szi", 1))  # SAFE_FLOAT
            current_px = position_value / abs(szi) if szi != 0 else 0
            liquidation_px = safe_float(position.get("liquidationPx", 0))  # SAFE_FLOAT
            
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
            unrealized_pnl = safe_float(closed_position.get("unrealizedPnl", 0))  # SAFE_FLOAT
            
            # Detectar liquidação (estava em warning + perda grande)
            was_at_risk = pos_key in alert_state["liquidation_warnings"]
            szi_value = safe_float(closed_position.get("szi", 0))  # SAFE_FLOAT
            entry_px = safe_float(closed_position.get("entryPx", 1))  # SAFE_FLOAT
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
            size = abs(safe_float(order.get("sz", 0)))  # SAFE_FLOAT
            limit_px = safe_float(order.get("limitPx", 0))  # SAFE_FLOAT
            
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
# MODELOS PYDANTIC (NÃO ALTERAR!)
# ============================================
class WhaleData(BaseModel):
    address: str
    nickname: Optional[str] = None

class AddWhaleRequest(BaseModel):
    address: str
    nickname: Optional[str] = None

# ============================================
# FUNÇÕES DE BUSCA DE DADOS (NÃO ALTERAR!)
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
                                "size": abs(safe_float(p.get("szi", 0))),  # SAFE_FLOAT
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
                
                # Calcular total de posições abertas - COM SAFE_FLOAT
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
                
                # NOVO: Verificar e enviar alertas
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
# ENDPOINTS DA API (NÃO ALTERAR!)
# ============================================
@app.get("/")
async def root():
    return {
        "message": "Hyperliquid Whale Tracker API",
        "version": "2.0",
        "telegram_enabled": TELEGRAM_ENABLED,
        "total_whales": len(KNOWN_WHALES),
        "endpoints": {
            "/whales": "GET - Lista todas as whales",
            "/whales/{address}": "GET - Dados de uma whale específica",
            "/whales": "POST - Adiciona nova whale",
            "/whales/{address}": "DELETE - Remove whale",
            "/health": "GET - Status da API",
            "/telegram/status": "GET - Status dos alertas Telegram",
            "/telegram/send-resume": "POST - Envia resumo via Telegram"
        }
    }

@app.get("/whales")
async def get_whales():
    """Retorna dados de todas as whales"""
    whales = await fetch_all_whales()
    cache["whales"] = whales
    cache["last_update"] = datetime.now()
    
    return {
        "whales": whales,
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
        "cache_age": (datetime.now() - cache["last_update"]).seconds if cache["last_update"] else None
    }

# ============================================
# NOVO: ENDPOINT DE STATUS DO TELEGRAM
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
        "liquidation_warnings_active": len(alert_state["liquidation_warnings"])
    }

# ============================================
# NOVO: ENDPOINT PARA ENVIAR RESUMO VIA TELEGRAM
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
                    value = safe_float(whale.get("total_position_value", 0))  # SAFE_FLOAT
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

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
