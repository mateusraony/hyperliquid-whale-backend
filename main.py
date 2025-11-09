from fastapi import FastAPI, HTTPException, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import Optional, List, Dict
import httpx
import asyncio
from datetime import datetime
import os

app = FastAPI(title="Hyperliquid Whale Tracker API")

# CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Lista de endereços das whales reais
WHALE_ADDRESSES = [
    "0x010461C14e146ac35Fe42271BDC1134EE31C703a",
    "0x00c9b566040e0e11b2e5865da9a4bb392955a7d7",
    "0x04225c49afa2ba00d6a5f9c703cc21bc0bdbb1aa",
    "0x02c4e503b0867e2bc6e168d38ccc073093f65e85",
    "0x02c290ce4d0a544a4e60c5aab803bc986f515829",
    "0x0b08de1e65aaf82dc6398ad6e11f5c1174b6e92e",
    "0x0bd2cdc7723de453e6e2b2c73f7f4a50cd755ba2",
    "0x172fd9f9c802feb08f2ff878f5d98f0cd0f17e0f",
    "0x2041c49938e86dc59f0fc9b12c0febf682413f40",
    "0x369db618f431f296e0a9d7b4f8c94fe946d3e6cf",
    "0x547f8c662f3c4f89dedf86e373f554f84f631cda"
]

# Armazenamento em memória
whale_data_cache = {}
monitoring_active = False

class WhaleAddress(BaseModel):
    address: str

class MonitoringStatus(BaseModel):
    active: bool

# Funções auxiliares
async def fetch_whale_data(address: str) -> Dict:
    """Busca dados de uma whale na API do Hyperliquid"""
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            # Clearinghouse state
            response = await client.post(
                "https://api.hyperliquid.xyz/info",
                json={"type": "clearinghouseState", "user": address}
            )
            data = response.json()
            
            # Extrair informações
            margin_summary = data.get("marginSummary", {})
            positions = data.get("assetPositions", [])
            
            # Calcular métricas
            account_value = float(margin_summary.get("accountValue", 0))
            total_margin_used = float(margin_summary.get("totalMarginUsed", 0))
            total_ntl_pos = float(margin_summary.get("totalNtlPos", 0))
            total_raw_usd = float(margin_summary.get("totalRawUsd", 0))
            
            # PnL
            unrealized_pnl = total_ntl_pos
            
            # Posições ativas
            active_positions = []
            for pos in positions:
                position_data = pos.get("position", {})
                if float(position_data.get("szi", 0)) != 0:
                    active_positions.append({
                        "coin": position_data.get("coin", ""),
                        "size": float(position_data.get("szi", 0)),
                        "entry_price": float(position_data.get("entryPx", 0)),
                        "unrealized_pnl": float(position_data.get("unrealizedPnl", 0)),
                        "leverage": float(position_data.get("leverage", {}).get("value", 0)),
                        "liquidation_px": float(position_data.get("liquidationPx", 0)) if position_data.get("liquidationPx") else None
                    })
            
            # Risco de liquidação
            liquidation_risk = "Baixo"
            if total_margin_used > 0:
                margin_ratio = (total_margin_used / account_value) * 100 if account_value > 0 else 0
                if margin_ratio > 80:
                    liquidation_risk = "Alto"
                elif margin_ratio > 50:
                    liquidation_risk = "Médio"
            
            return {
                "address": address,
                "account_value": account_value,
                "total_margin_used": total_margin_used,
                "unrealized_pnl": unrealized_pnl,
                "active_positions": active_positions,
                "liquidation_risk": liquidation_risk,
                "last_update": datetime.now().isoformat()
            }
            
    except Exception as e:
        print(f"Erro ao buscar dados da whale {address}: {str(e)}")
        return {
            "address": address,
            "error": str(e),
            "last_update": datetime.now().isoformat()
        }

async def monitor_whales():
    """Monitora todas as whales continuamente"""
    global whale_data_cache, monitoring_active
    
    while monitoring_active:
        tasks = [fetch_whale_data(addr) for addr in WHALE_ADDRESSES]
        results = await asyncio.gather(*tasks)
        
        for result in results:
            whale_data_cache[result["address"]] = result
        
        # Aguardar 30 segundos antes da próxima atualização
        await asyncio.sleep(30)

# Endpoints
@app.get("/")
async def root():
    return {
        "message": "Hyperliquid Whale Tracker API",
        "version": "1.0",
        "endpoints": {
            "/whales": "GET - Lista todas as whales monitoradas",
            "/whales/{address}": "GET - Dados de uma whale específica",
            "/whales": "POST - Adiciona nova whale",
            "/whales/{address}": "DELETE - Remove whale",
            "/monitoring/status": "GET - Status do monitoramento",
            "/monitoring/start": "POST - Inicia monitoramento",
            "/monitoring/stop": "POST - Para monitoramento"
        }
    }

@app.get("/whales")
async def get_whales():
    """Retorna dados de todas as whales"""
    if not whale_data_cache:
        # Se cache vazio, buscar dados
        tasks = [fetch_whale_data(addr) for addr in WHALE_ADDRESSES]
        results = await asyncio.gather(*tasks)
        for result in results:
            whale_data_cache[result["address"]] = result
    
    return {"whales": list(whale_data_cache.values()), "count": len(whale_data_cache)}

@app.get("/whales/{address}")
async def get_whale(address: str):
    """Retorna dados de uma whale específica"""
    if address in whale_data_cache:
        return whale_data_cache[address]
    
    # Buscar dados se não estiver em cache
    data = await fetch_whale_data(address)
    whale_data_cache[address] = data
    return data

@app.post("/whales")
async def add_whale(whale: WhaleAddress):
    """Adiciona nova whale para monitoramento"""
    if whale.address in WHALE_ADDRESSES:
        raise HTTPException(status_code=400, detail="Whale já está sendo monitorada")
    
    WHALE_ADDRESSES.append(whale.address)
    data = await fetch_whale_data(whale.address)
    whale_data_cache[whale.address] = data
    
    return {"message": "Whale adicionada com sucesso", "whale": data}

@app.delete("/whales/{address}")
async def delete_whale(address: str):
    """Remove whale do monitoramento"""
    if address not in WHALE_ADDRESSES:
        raise HTTPException(status_code=404, detail="Whale não encontrada")
    
    WHALE_ADDRESSES.remove(address)
    if address in whale_data_cache:
        del whale_data_cache[address]
    
    return {"message": "Whale removida com sucesso", "address": address}

@app.get("/monitoring/status")
async def get_monitoring_status():
    """Retorna status do monitoramento"""
    return {
        "active": monitoring_active,
        "whales_count": len(WHALE_ADDRESSES),
        "cache_size": len(whale_data_cache)
    }

@app.post("/monitoring/start")
async def start_monitoring(background_tasks: BackgroundTasks):
    """Inicia monitoramento contínuo"""
    global monitoring_active
    
    if monitoring_active:
        return {"message": "Monitoramento já está ativo"}
    
    monitoring_active = True
    background_tasks.add_task(monitor_whales)
    
    return {"message": "Monitoramento iniciado", "whales_count": len(WHALE_ADDRESSES)}

@app.post("/monitoring/stop")
async def stop_monitoring():
    """Para monitoramento contínuo"""
    global monitoring_active
    monitoring_active = False
    
    return {"message": "Monitoramento parado"}

if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
