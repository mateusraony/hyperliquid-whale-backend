from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import List, Optional
import httpx
import asyncio
from datetime import datetime

app = FastAPI(title="Hyperliquid Whale Tracker API")

# Configurar CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Lista das 11 whales válidas
KNOWN_WHALES = [
    "0x010461DBc33f87b1a0f765bcAc2F96F4B3936182",
    "0x8c5865689EABe45645fa034e53d0c9995DCcb9c9",
    "0x939f95036D2e7b6d7419Ec072BF9d967352204d2",
    "0x3eca9823105034b0d580dd722c75c0c23829a3d9",
    "0x579f4017263b88945d727a927bf1e3d061fee5ff",
    "0x9eec98D048D06D9CD75318FFfA3f3960e081daAb",
    "0x020ca66c30bec2c4fe3861a94e4db4a498a35872",
    "0xbadbb1de95b5f333623ebece7026932fa5039ee6",
    "0x9e4f6D88f1e34d5F3E96451754a87Aad977Ceff3",
    "0x8d0E342E0524392d035Fb37461C6f5813ff59244",
    "0xC385D2cD1971ADfeD0E47813702765551cAe0372"
]

# Cache para armazenar dados
cache = {
    "whales": [],
    "last_update": None
}

# Modelos Pydantic
class WhaleData(BaseModel):
    address: str
    nickname: Optional[str] = None
    accountValue: float = 0
    marginUsed: float = 0
    unrealizedPnl: float = 0
    liquidationRisk: float = 0
    positions: List[dict] = []
    last_updated: Optional[datetime] = None

class AddWhaleRequest(BaseModel):
    address: str
    nickname: Optional[str] = None

# Cliente HTTP com timeout configurado
async def get_hyperliquid_data(address: str) -> dict:
    """Busca dados de uma wallet na API da Hyperliquid"""
    url = "https://api.hyperliquid.xyz/info"
    
    async with httpx.AsyncClient(timeout=60.0) as client:
        try:
            # Buscar estado da conta
            account_state_response = await client.post(
                url,
                json={"type": "clearinghouseState", "user": address}
            )
            account_state_response.raise_for_status()
            account_data = account_state_response.json()
            
            # Processar dados
            margin_summary = account_data.get("marginSummary", {})
            asset_positions = account_data.get("assetPositions", [])
            
            # Calcular métricas
            account_value = float(margin_summary.get("accountValue", 0))
            margin_used = float(margin_summary.get("totalMarginUsed", 0))
            unrealized_pnl = float(margin_summary.get("totalNtlPos", 0))
            
            # Calcular risco de liquidação
            liquidation_risk = 0
            if account_value > 0:
                liquidation_risk = (margin_used / account_value) * 100
            
            # Processar posições
            positions = []
            for pos in asset_positions:
                position_data = pos.get("position", {})
                if position_data:
                    positions.append({
                        "coin": position_data.get("coin", ""),
                        "szi": float(position_data.get("szi", 0)),
                        "unrealizedPnl": float(position_data.get("unrealizedPnl", 0)),
                        "entryPx": float(position_data.get("entryPx", 0)),
                        "leverage": position_data.get("leverage", {})
                    })
            
            return {
                "address": address,
                "accountValue": account_value,
                "marginUsed": margin_used,
                "unrealizedPnl": unrealized_pnl,
                "liquidationRisk": liquidation_risk,
                "positions": positions,
                "last_updated": datetime.now().isoformat()
            }
            
        except httpx.HTTPStatusError as e:
            print(f"Erro HTTP ao buscar {address}: {e}")
            return None
        except httpx.TimeoutException:
            print(f"Timeout ao buscar {address}")
            return None
        except Exception as e:
            print(f"Erro desconhecido ao buscar {address}: {e}")
            return None

async def fetch_all_whales():
    """Busca dados de todas as whales em paralelo"""
    tasks = [get_hyperliquid_data(address) for address in KNOWN_WHALES]
    results = await asyncio.gather(*tasks, return_exceptions=True)
    
    # Filtrar resultados válidos
    whales_data = []
    for result in results:
        if result and not isinstance(result, Exception):
            whales_data.append(result)
    
    return whales_data

@app.get("/")
async def root():
    """Endpoint raiz"""
    return {
        "status": "online",
        "message": "Hyperliquid Whale Tracker API",
        "endpoints": {
            "GET /whales": "Listar todas as whales monitoradas",
            "POST /whales": "Adicionar nova whale",
            "DELETE /whales/{address}": "Remover whale do monitoramento",
            "GET /whales/{address}": "Buscar dados de uma whale específica"
        },
        "total_whales": len(KNOWN_WHALES)
    }

@app.get("/whales")
async def get_whales():
    """Retorna dados de todas as whales monitoradas"""
    try:
        # Atualizar cache se necessário
        if not cache["last_update"] or (datetime.now() - cache["last_update"]).seconds > 30:
            print(f"Buscando dados de {len(KNOWN_WHALES)} whales...")
            whales_data = await fetch_all_whales()
            cache["whales"] = whales_data
            cache["last_update"] = datetime.now()
            print(f"Cache atualizado: {len(whales_data)} whales com dados")
        
        return cache["whales"]
        
    except Exception as e:
        print(f"Erro em /whales: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/whales/{address}")
async def get_whale(address: str):
    """Retorna dados de uma whale específica"""
    try:
        # Verificar se a whale está na lista
        if address not in KNOWN_WHALES:
            raise HTTPException(status_code=404, detail="Whale não encontrada")
        
        # Buscar dados atualizados
        whale_data = await get_hyperliquid_data(address)
        
        if not whale_data:
            raise HTTPException(status_code=500, detail="Erro ao buscar dados da whale")
        
        return whale_data
        
    except HTTPException:
        raise
    except Exception as e:
        print(f"Erro em /whales/{address}: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/whales")
async def add_whale(request: AddWhaleRequest):
    """Adiciona uma nova whale ao monitoramento"""
    try:
        # Validar endereço
        address = request.address.strip()
        if not address.startswith("0x") or len(address) != 42:
            raise HTTPException(
                status_code=400, 
                detail="Endereço inválido. Use formato: 0x..."
            )
        
        # Verificar se já existe
        if address in KNOWN_WHALES:
            raise HTTPException(
                status_code=400, 
                detail="Esta whale já está sendo monitorada"
            )
        
        # Testar se o endereço é válido na Hyperliquid
        whale_data = await get_hyperliquid_data(address)
        if not whale_data:
            raise HTTPException(
                status_code=400, 
                detail="Não foi possível buscar dados desta wallet na Hyperliquid"
            )
        
        # Adicionar à lista
        KNOWN_WHALES.append(address)
        
        # Adicionar nickname se fornecido
        if request.nickname:
            whale_data["nickname"] = request.nickname
        
        # Atualizar cache
        cache["whales"].append(whale_data)
        cache["last_update"] = datetime.now()
        
        return {
            "message": "Whale adicionada com sucesso!",
            "address": address,
            "nickname": request.nickname,
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
        
        # Remover da lista
        KNOWN_WHALES.remove(address)
        
        # Atualizar cache
        cache["whales"] = [w for w in cache["whales"] if w.get("address") != address]
        cache["last_update"] = datetime.now()
        
        return {
            "message": "Whale removida com sucesso!",
            "address": address,
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
        "cache_age": (datetime.now() - cache["last_update"]).seconds if cache["last_update"] else None
    }

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
