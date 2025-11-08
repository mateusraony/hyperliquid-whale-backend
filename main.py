from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
import httpx
from datetime import datetime, timezone
import asyncio
from typing import Dict, List, Optional
from pydantic import BaseModel

app = FastAPI(title="Hyperliquid Whale Tracker API")

# CORS configurado
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ============================================
# CONFIGURAÇÕES
# ============================================

HYPERLIQUID_API = "https://api.hyperliquid.xyz"

# 🎯 11 ENDEREÇOS CORRETOS DAS WHALES
KNOWN_WHALES = [
    "0x8c5865689EABe45645fa034e53d0c9995DCcb9c9",
    "0x939f95036D2e7b6d7419Ec072BF9d967352204d2",
    "0x3eca9823105034b0d580dd722c75c0c23829a3d9",
    "0x579f4017263b88945d727a927bf1e3d061fee5ff",
    "0x9eec98D048D06D9CD75318FFfA3f3960e081daAb",
    "0x020ca66c30bec2c4fe3861a94e4db4a498a35872",
    "0xbadbb1de95b5f333623ebece7026932fa5039ee6",
    "0x9e4f6D88f1e34d5F3E96451754a87Aad977Ceff3",
    "0x8d0E342E0524392d035Fb37461C6f5813ff59244",
    "0xC385D2cD1971ADfeD0E47813702765551cAe0372",
    "0x5b5d51203a0f9079f8aeb098a6523a13F298C060",
]

# Cache simples em memória
cache = {
    "whales": [],
    "last_update": None,
    "update_interval": 30  # segundos
}

# ============================================
# MODELOS DE DADOS
# ============================================

class WhaleData(BaseModel):
    address: str
    nickname: str
    total_value: float
    positions_count: int
    pnl_24h: float
    last_trade: Optional[str]
    risk_level: str
    wallet_link: str

class Position(BaseModel):
    token: str
    side: str
    size: float
    entry_price: float
    current_price: float
    pnl: float
    leverage: float
    liquidation_price: Optional[float]

class Trade(BaseModel):
    timestamp: str
    token: str
    side: str
    size: float
    price: float
    pnl: float

# ============================================
# FUNÇÕES AUXILIARES
# ============================================

def get_whale_nickname(address: str) -> str:
    """Gera nickname baseado no endereço"""
    nicknames = {
        0: "Alpha", 1: "Sigma", 2: "Gamma", 3: "Delta", 4: "Epsilon",
        5: "Zeta", 6: "Theta", 7: "Kappa", 8: "Lambda", 9: "Omega", 10: "Phantom"
    }
    index = KNOWN_WHALES.index(address) if address in KNOWN_WHALES else 0
    return f"{nicknames.get(index, 'Whale')} #{address[-4:]}"

def get_wallet_explorer_link(address: str) -> str:
    """Retorna o link correto do explorer"""
    # Wallet específica usa HyperDash
    if address == "0x020ca66c30bec2c4fe3861a94e4db4a498a35872":
        return f"https://hyperdash.io/account/{address}"
    # Demais usam Hypurrscan
    return f"https://hypurrscan.io/address/{address}"

def calculate_risk_level(positions: List[dict]) -> str:
    """Calcula nível de risco baseado nas posições"""
    if not positions:
        return "SAFE"
    
    total_leverage = sum(pos.get("leverage", 0) for pos in positions)
    avg_leverage = total_leverage / len(positions)
    
    if avg_leverage < 5:
        return "SAFE"
    elif avg_leverage < 15:
        return "MODERATE"
    else:
        return "HIGH"

# ============================================
# FUNÇÕES DE API HYPERLIQUID
# ============================================

async def fetch_user_state(address: str) -> dict:
    """Busca estado do usuário na Hyperliquid"""
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(
                f"{HYPERLIQUID_API}/info",
                json={"type": "clearinghouseState", "user": address}
            )
            return response.json() if response.status_code == 200 else {}
    except Exception as e:
        print(f"❌ Erro ao buscar estado do usuário {address}: {e}")
        return {}

async def fetch_user_fills(address: str) -> List[dict]:
    """Busca histórico de trades"""
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(
                f"{HYPERLIQUID_API}/info",
                json={"type": "userFills", "user": address}
            )
            return response.json() if response.status_code == 200 else []
    except Exception as e:
        print(f"❌ Erro ao buscar trades de {address}: {e}")
        return []

async def process_whale_data(address: str) -> WhaleData:
    """Processa dados completos de uma whale"""
    try:
        # Busca dados em paralelo
        state, fills = await asyncio.gather(
            fetch_user_state(address),
            fetch_user_fills(address)
        )
        
        # Calcula valores
        positions = state.get("assetPositions", [])
        total_value = sum(float(pos.get("position", {}).get("szi", 0)) * 
                         float(pos.get("position", {}).get("entryPx", 0)) 
                         for pos in positions)
        
        pnl_24h = sum(float(fill.get("closedPnl", 0)) 
                     for fill in fills[:10] if fill)  # últimos 10 trades
        
        last_trade = fills[0].get("time") if fills else None
        risk_level = calculate_risk_level(positions)
        
        return WhaleData(
            address=address,
            nickname=get_whale_nickname(address),
            total_value=abs(total_value),
            positions_count=len(positions),
            pnl_24h=pnl_24h,
            last_trade=last_trade,
            risk_level=risk_level,
            wallet_link=get_wallet_explorer_link(address)
        )
    except Exception as e:
        print(f"❌ Erro ao processar whale {address}: {e}")
        return WhaleData(
            address=address,
            nickname=get_whale_nickname(address),
            total_value=0,
            positions_count=0,
            pnl_24h=0,
            last_trade=None,
            risk_level="UNKNOWN",
            wallet_link=get_wallet_explorer_link(address)
        )

async def update_cache():
    """Atualiza cache com dados das whales"""
    try:
        print("🔄 Atualizando cache...")
        
        # Processa todas as whales em paralelo (mais rápido!)
        tasks = [process_whale_data(addr) for addr in KNOWN_WHALES]
        results = await asyncio.gather(*tasks)
        
        cache["whales"] = results
        cache["last_update"] = datetime.now(timezone.utc).isoformat()
        
        print(f"✅ Cache atualizado! {len(results)} whales processadas")
        return True
    except Exception as e:
        print(f"❌ Erro ao atualizar cache: {e}")
        return False

# ============================================
# ENDPOINTS DA API
# ============================================

@app.on_event("startup")
async def startup_event():
    """Inicializa o cache ao iniciar"""
    print("🚀 Iniciando API...")
    await update_cache()
    print("✅ API pronta!")

@app.get("/")
async def root():
    """Health check básico"""
    return {
        "status": "online",
        "message": "Hyperliquid Whale Tracker API",
        "version": "2.0",
        "whales_tracked": len(KNOWN_WHALES),
        "last_update": cache.get("last_update")
    }

@app.get("/api/health")
async def health_check():
    """Health check detalhado"""
    return {
        "status": "healthy",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "cache_status": "active" if cache["whales"] else "empty",
        "whales_count": len(cache["whales"]),
        "last_update": cache["last_update"]
    }

@app.get("/api/whales")
async def get_whales():
    """Retorna lista de todas as whales"""
    # Atualiza cache se necessário
    if not cache["last_update"]:
        await update_cache()
    
    return {
        "whales": [whale.dict() for whale in cache["whales"]],
        "total": len(cache["whales"]),
        "last_update": cache["last_update"]
    }

@app.get("/api/whale/{address}")
async def get_whale_details(address: str):
    """Detalhes de uma whale específica"""
    if address not in KNOWN_WHALES:
        raise HTTPException(status_code=404, detail="Whale não encontrada")
    
    # Busca dados atualizados
    whale_data = await process_whale_data(address)
    
    return whale_data.dict()

@app.get("/api/positions/{address}")
async def get_positions(address: str):
    """Posições abertas de uma whale"""
    try:
        state = await fetch_user_state(address)
        positions = state.get("assetPositions", [])
        
        formatted_positions = []
        for pos in positions:
            position_data = pos.get("position", {})
            formatted_positions.append({
                "token": pos.get("coin", "UNKNOWN"),
                "side": "LONG" if float(position_data.get("szi", 0)) > 0 else "SHORT",
                "size": abs(float(position_data.get("szi", 0))),
                "entry_price": float(position_data.get("entryPx", 0)),
                "current_price": float(position_data.get("liquidationPx", 0)),
                "pnl": float(position_data.get("unrealizedPnl", 0)),
                "leverage": float(position_data.get("leverage", {}).get("value", 0)),
                "liquidation_price": float(position_data.get("liquidationPx", 0))
            })
        
        return {"positions": formatted_positions}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Erro ao buscar posições: {str(e)}")

@app.get("/api/trades/{address}")
async def get_trades(address: str):
    """Histórico de trades de uma whale"""
    try:
        fills = await fetch_user_fills(address)
        
        formatted_trades = []
        for fill in fills[:50]:  # últimos 50 trades
            formatted_trades.append({
                "timestamp": fill.get("time"),
                "token": fill.get("coin", "UNKNOWN"),
                "side": fill.get("side", "UNKNOWN"),
                "size": float(fill.get("sz", 0)),
                "price": float(fill.get("px", 0)),
                "pnl": float(fill.get("closedPnl", 0))
            })
        
        return {"trades": formatted_trades}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Erro ao buscar trades: {str(e)}")

@app.get("/api/stats")
async def get_global_stats():
    """Estatísticas globais de todas as whales"""
    if not cache["whales"]:
        await update_cache()
    
    total_value = sum(whale.total_value for whale in cache["whales"])
    total_pnl = sum(whale.pnl_24h for whale in cache["whales"])
    total_positions = sum(whale.positions_count for whale in cache["whales"])
    
    return {
        "total_whales": len(cache["whales"]),
        "total_value": total_value,
        "total_pnl_24h": total_pnl,
        "total_positions": total_positions,
        "average_positions_per_whale": total_positions / len(cache["whales"]) if cache["whales"] else 0
    }

@app.post("/api/whale/add")
async def add_whale(address: str):
    """Adiciona nova whale ao monitoramento"""
    if address in KNOWN_WHALES:
        raise HTTPException(status_code=400, detail="Whale já está sendo monitorada")
    
    KNOWN_WHALES.append(address)
    await update_cache()
    
    return {"message": "Whale adicionada com sucesso!", "address": address}

@app.delete("/api/whale/delete/{address}")
async def delete_whale(address: str):
    """Remove uma whale do monitoramento"""
    if address not in KNOWN_WHALES:
        raise HTTPException(status_code=404, detail="Whale não encontrada")
    
    KNOWN_WHALES.remove(address)
    cache["whales"] = [w for w in cache["whales"] if w.address != address]
    
    return {"message": "Whale removida com sucesso!", "address": address}

@app.post("/api/refresh")
async def force_refresh():
    """Força atualização imediata do cache"""
    success = await update_cache()
    
    if success:
        return {"message": "Cache atualizado!", "timestamp": cache["last_update"]}
    else:
        raise HTTPException(status_code=500, detail="Erro ao atualizar cache")

# ============================================
# EXECUÇÃO
# ============================================

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
