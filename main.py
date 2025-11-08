"""
Hyperliquid Whale Tracker - Backend API
Sistema profissional para rastreamento de whales em tempo real
"""

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from typing import List, Dict, Optional
import httpx
from datetime import datetime, timedelta
from collections import defaultdict
import asyncio
from pydantic import BaseModel

app = FastAPI(
    title="Hyperliquid Whale Tracker API",
    description="API para rastreamento de whales em tempo real",
    version="1.0.0"
)

# Configuração CORS (permite frontend conectar)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Em produção, especifique seu domínio
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ============================================================================
# CONFIGURAÇÕES E CACHE
# ============================================================================

HYPERLIQUID_API_URL = "https://api.hyperliquid.xyz/info"

# Cache simples em memória (melhor que fazer request toda hora)
cache = {
    "whales": [],
    "last_update": None,
    "positions": {},
    "trades": {},
    "liquidations": defaultdict(list)
}

CACHE_DURATION = 30  # segundos - atualiza a cada 30s

# Lista de endereços conhecidos de whales (você pode expandir)
KNOWN_WHALES = [
    "0x010216dac37c0c81377321d4fdf5bc09f3c6e235",
    "0x00c9c3391bb8734a8c7e1e4e2f5e0cbd7e1f5ff5",
    "0x563321ccc9da46d2e9f17a7be5c6753a1d3b3e8f",
]

# ============================================================================
# MODELOS DE DADOS
# ============================================================================

class WhaleWallet(BaseModel):
    address: str
    nickname: str
    total_value: float
    pnl_24h: float
    pnl_percentage: float
    status: str  # "online", "warning", "offline"
    positions_count: int
    last_activity: str

class Position(BaseModel):
    symbol: str
    side: str  # "LONG" ou "SHORT"
    size: float
    entry_price: float
    current_price: float
    pnl: float
    pnl_percentage: float
    leverage: float
    liquidation_price: Optional[float]

class Trade(BaseModel):
    timestamp: str
    symbol: str
    side: str
    price: float
    size: float
    total_value: float

class Liquidation(BaseModel):
    timestamp: str
    address: str
    symbol: str
    side: str
    size: float
    price: float
    value: float

# ============================================================================
# FUNÇÕES AUXILIARES - CONEXÃO COM HYPERLIQUID
# ============================================================================

async def fetch_hyperliquid_data(endpoint: str, data: dict) -> dict:
    """Faz request para API da Hyperliquid"""
    async with httpx.AsyncClient(timeout=30.0) as client:
        try:
            response = await client.post(
                HYPERLIQUID_API_URL,
                json={"type": endpoint, **data}
            )
            response.raise_for_status()
            return response.json()
        except Exception as e:
            print(f"❌ Erro ao buscar dados: {e}")
            return None

async def get_user_state(address: str) -> dict:
    """Busca estado completo de um usuário"""
    return await fetch_hyperliquid_data("clearinghouseState", {"user": address})

async def get_user_fills(address: str) -> list:
    """Busca histórico de trades de um usuário"""
    return await fetch_hyperliquid_data("userFills", {"user": address})

async def get_meta_and_asset_ctxs() -> dict:
    """Busca metadados e contextos de ativos"""
    return await fetch_hyperliquid_data("metaAndAssetCtxs", {})

# ============================================================================
# PROCESSAMENTO DE DADOS
# ============================================================================

def calculate_position_pnl(position: dict, current_price: float) -> dict:
    """Calcula PnL de uma posição"""
    try:
        entry_price = float(position.get("entryPx", 0))
        size = float(position.get("siz", 0))
        side = position.get("side", "long").upper()
        
        if side == "LONG":
            pnl = (current_price - entry_price) * size
        else:
            pnl = (entry_price - current_price) * size
        
        pnl_percentage = (pnl / (entry_price * size)) * 100 if entry_price * size != 0 else 0
        
        return {
            "pnl": round(pnl, 2),
            "pnl_percentage": round(pnl_percentage, 2)
        }
    except:
        return {"pnl": 0, "pnl_percentage": 0}

def determine_wallet_status(last_activity: datetime) -> str:
    """Determina status da wallet baseado na última atividade"""
    now = datetime.now()
    diff = now - last_activity
    
    if diff < timedelta(minutes=15):
        return "online"
    elif diff < timedelta(hours=1):
        return "warning"
    else:
        return "offline"

async def process_whale_data(address: str, nickname: str) -> Optional[WhaleWallet]:
    """Processa dados completos de uma whale"""
    try:
        # Busca dados do usuário
        user_state = await get_user_state(address)
        if not user_state:
            return None
        
        # Busca preços atuais
        market_data = await get_meta_and_asset_ctxs()
        if not market_data:
            return None
        
        # Processa posições
        positions = user_state.get("assetPositions", [])
        total_value = 0
        pnl_24h = 0
        positions_count = len([p for p in positions if float(p.get("position", {}).get("siz", 0)) != 0])
        
        # Calcula valor total e PnL
        for pos in positions:
            position_data = pos.get("position", {})
            size = float(position_data.get("siz", 0))
            if size != 0:
                entry_px = float(position_data.get("entryPx", 0))
                total_value += abs(size * entry_px)
                
                # Aqui você pode calcular PnL real comparando com preço atual
                # Por enquanto vou usar um valor simulado baseado no unrealized PnL
                unrealized_pnl = float(position_data.get("unrealizedPnl", 0))
                pnl_24h += unrealized_pnl
        
        # Determina última atividade (vamos usar timestamp atual por enquanto)
        last_activity = datetime.now()
        status = determine_wallet_status(last_activity)
        
        pnl_percentage = (pnl_24h / total_value * 100) if total_value > 0 else 0
        
        return WhaleWallet(
            address=address,
            nickname=nickname,
            total_value=round(total_value, 2),
            pnl_24h=round(pnl_24h, 2),
            pnl_percentage=round(pnl_percentage, 2),
            status=status,
            positions_count=positions_count,
            last_activity=last_activity.isoformat()
        )
    except Exception as e:
        print(f"❌ Erro ao processar whale {address}: {e}")
        return None

# ============================================================================
# TAREFA EM BACKGROUND - ATUALIZAÇÃO AUTOMÁTICA
# ============================================================================

async def update_cache_background():
    """Atualiza cache automaticamente em background"""
    while True:
        try:
            print("🔄 Atualizando cache...")
            
            # Processa cada whale
            whales = []
            for i, address in enumerate(KNOWN_WHALES):
                whale = await process_whale_data(address, f"Whale #{i+1}")
                if whale:
                    whales.append(whale)
            
            # Atualiza cache
            cache["whales"] = whales
            cache["last_update"] = datetime.now().isoformat()
            
            print(f"✅ Cache atualizado! {len(whales)} whales processadas")
            
        except Exception as e:
            print(f"❌ Erro ao atualizar cache: {e}")
        
        # Aguarda antes da próxima atualização
        await asyncio.sleep(CACHE_DURATION)

@app.on_event("startup")
async def startup_event():
    """Inicia tarefa de atualização ao iniciar o servidor"""
    asyncio.create_task(update_cache_background())
    print("🚀 Backend iniciado! Sistema de cache ativo.")

# ============================================================================
# ENDPOINTS DA API
# ============================================================================

@app.get("/")
async def root():
    """Endpoint raiz - informações da API"""
    return {
        "name": "Hyperliquid Whale Tracker API",
        "version": "1.0.0",
        "status": "online",
        "last_update": cache["last_update"],
        "whales_tracked": len(cache["whales"])
    }

@app.get("/api/health")
async def health_check():
    """Verifica saúde da API"""
    return {
        "status": "healthy",
        "timestamp": datetime.now().isoformat(),
        "cache_age_seconds": (
            (datetime.now() - datetime.fromisoformat(cache["last_update"])).seconds
            if cache["last_update"] else None
        )
    }

@app.get("/api/whales", response_model=List[WhaleWallet])
async def get_whales():
    """Retorna lista de todas as whales monitoradas"""
    if not cache["whales"]:
        raise HTTPException(status_code=503, detail="Cache ainda não inicializado. Aguarde alguns segundos.")
    
    return cache["whales"]

@app.get("/api/whale/{address}")
async def get_whale_details(address: str):
    """Retorna detalhes completos de uma whale específica"""
    # Busca dados frescos da whale
    whale = await process_whale_data(address, "Custom Whale")
    
    if not whale:
        raise HTTPException(status_code=404, detail="Whale não encontrada ou erro ao buscar dados")
    
    return whale

@app.get("/api/positions/{address}")
async def get_whale_positions(address: str):
    """Retorna posições abertas de uma whale"""
    try:
        user_state = await get_user_state(address)
        if not user_state:
            raise HTTPException(status_code=404, detail="Não foi possível buscar posições")
        
        positions = []
        asset_positions = user_state.get("assetPositions", [])
        
        # Busca preços atuais
        market_data = await get_meta_and_asset_ctxs()
        prices = {}
        if market_data and "universe" in market_data:
            for asset in market_data["universe"]:
                symbol = asset.get("name")
                if symbol:
                    prices[symbol] = float(asset.get("markPx", 0))
        
        for asset_pos in asset_positions:
            position = asset_pos.get("position", {})
            size = float(position.get("siz", 0))
            
            if size != 0:  # Apenas posições abertas
                symbol = asset_pos.get("position", {}).get("coin", "UNKNOWN")
                entry_price = float(position.get("entryPx", 0))
                current_price = prices.get(symbol, entry_price)
                leverage = float(position.get("leverage", {}).get("value", 1))
                
                # Calcula PnL
                pnl_data = calculate_position_pnl(position, current_price)
                
                positions.append(Position(
                    symbol=symbol,
                    side="LONG" if size > 0 else "SHORT",
                    size=abs(size),
                    entry_price=entry_price,
                    current_price=current_price,
                    pnl=pnl_data["pnl"],
                    pnl_percentage=pnl_data["pnl_percentage"],
                    leverage=leverage,
                    liquidation_price=float(position.get("liquidationPx", 0))
                ))
        
        return positions
    
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Erro ao buscar posições: {str(e)}")

@app.get("/api/trades/{address}")
async def get_whale_trades(address: str, limit: int = 50):
    """Retorna histórico de trades de uma whale"""
    try:
        fills = await get_user_fills(address)
        
        if not fills:
            return []
        
        trades = []
        for fill in fills[:limit]:
            trades.append(Trade(
                timestamp=datetime.fromtimestamp(fill.get("time", 0) / 1000).isoformat(),
                symbol=fill.get("coin", "UNKNOWN"),
                side=fill.get("side", "").upper(),
                price=float(fill.get("px", 0)),
                size=float(fill.get("sz", 0)),
                total_value=float(fill.get("px", 0)) * float(fill.get("sz", 0))
            ))
        
        return trades
    
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Erro ao buscar trades: {str(e)}")

@app.get("/api/stats")
async def get_global_stats():
    """Retorna estatísticas globais do sistema"""
    if not cache["whales"]:
        raise HTTPException(status_code=503, detail="Cache ainda não inicializado")
    
    total_value = sum(w.total_value for w in cache["whales"])
    total_pnl = sum(w.pnl_24h for w in cache["whales"])
    
    # Conta posições LONG/SHORT
    long_count = 0
    short_count = 0
    
    for whale in cache["whales"]:
        # Aqui você pode buscar as posições de cada whale e contar
        # Por enquanto vou usar valores baseados no total de posições
        long_count += whale.positions_count // 2
        short_count += whale.positions_count - (whale.positions_count // 2)
    
    return {
        "total_whales": len(cache["whales"]),
        "total_value_tracked": round(total_value, 2),
        "total_pnl_24h": round(total_pnl, 2),
        "online_whales": len([w for w in cache["whales"] if w.status == "online"]),
        "warning_whales": len([w for w in cache["whales"] if w.status == "warning"]),
        "offline_whales": len([w for w in cache["whales"] if w.status == "offline"]),
        "long_positions": long_count,
        "short_positions": short_count,
        "last_update": cache["last_update"]
    }

@app.post("/api/whale/add")
async def add_whale(address: str, nickname: str):
    """Adiciona uma nova whale para monitoramento"""
    if address in KNOWN_WHALES:
        raise HTTPException(status_code=400, detail="Whale já está sendo monitorada")
    
    # Testa se a whale existe e tem dados
    whale = await process_whale_data(address, nickname)
    if not whale:
        raise HTTPException(status_code=404, detail="Não foi possível encontrar dados para este endereço")
    
    # Adiciona à lista
    KNOWN_WHALES.append(address)
    
    return {
        "message": "Whale adicionada com sucesso!",
        "whale": whale
    }

# ============================================================================
# EXECUÇÃO
# ============================================================================

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
