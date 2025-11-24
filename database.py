"""
database.py - Sistema de banco de dados PostgreSQL para Whale Tracker
Responsável por: tracking de trades, liquidações e cálculo de métricas reais
"""

import os
import asyncpg
from datetime import datetime, timedelta
from typing import List, Dict, Optional
import asyncio

# ============================================
# CONFIGURAÇÃO DO POSTGRESQL
# ============================================

# URL do banco (Render fornece automaticamente via variável de ambiente)
DATABASE_URL = os.getenv("DATABASE_URL")

# Pool de conexões (será inicializado no startup)
db_pool = None

# ============================================
# FUNÇÕES DE CONEXÃO
# ============================================

async def init_db():
    """Inicializa pool de conexões e cria tabelas"""
    global db_pool
    
    if not DATABASE_URL:
        print("⚠️ DATABASE_URL não configurado. Métricas reais desabilitadas.")
        return False
    
    try:
        # Criar pool de conexões
        db_pool = await asyncpg.create_pool(
            DATABASE_URL,
            min_size=1,
            max_size=10,
            command_timeout=60
        )
        
        print("✅ Pool de conexões PostgreSQL criado!")
        
        # Criar tabelas se não existirem
        await create_tables()
        
        print("✅ Banco de dados inicializado com sucesso!")
        return True
        
    except Exception as e:
        print(f"❌ Erro ao conectar PostgreSQL: {e}")
        print("⚠️ Sistema continuará sem banco de dados (métricas mockadas)")
        return False

async def close_db():
    """Fecha pool de conexões"""
    global db_pool
    if db_pool:
        await db_pool.close()
        print("✅ Pool de conexões PostgreSQL fechado")

async def create_tables():
    """Cria as tabelas necessárias no banco"""
    
    create_trades_table = """
    CREATE TABLE IF NOT EXISTS trades (
        id SERIAL PRIMARY KEY,
        wallet VARCHAR(42) NOT NULL,
        nickname VARCHAR(100),
        token VARCHAR(20) NOT NULL,
        side VARCHAR(5) NOT NULL,
        size DECIMAL(20, 8) NOT NULL,
        entry_price DECIMAL(20, 8),
        exit_price DECIMAL(20, 8),
        pnl DECIMAL(20, 2),
        leverage DECIMAL(10, 2),
        open_timestamp TIMESTAMP NOT NULL,
        close_timestamp TIMESTAMP,
        status VARCHAR(10) NOT NULL,
        created_at TIMESTAMP DEFAULT NOW()
    );
    """
    
    create_liquidations_table = """
    CREATE TABLE IF NOT EXISTS liquidations (
        id SERIAL PRIMARY KEY,
        wallet VARCHAR(42) NOT NULL,
        nickname VARCHAR(100),
        token VARCHAR(20) NOT NULL,
        side VARCHAR(5) NOT NULL,
        size DECIMAL(20, 8) NOT NULL,
        liquidation_price DECIMAL(20, 8),
        loss_amount DECIMAL(20, 2),
        timestamp TIMESTAMP NOT NULL,
        created_at TIMESTAMP DEFAULT NOW()
    );
    """
    
    create_wallet_snapshots_table = """
    CREATE TABLE IF NOT EXISTS wallet_snapshots (
        id SERIAL PRIMARY KEY,
        wallet VARCHAR(42) NOT NULL,
        nickname VARCHAR(100),
        timestamp TIMESTAMP NOT NULL,
        total_value DECIMAL(20, 2),
        positions_count INT,
        margin_used DECIMAL(20, 2),
        created_at TIMESTAMP DEFAULT NOW()
    );
    """
    
    # Criar índices para performance
    create_indexes = """
    CREATE INDEX IF NOT EXISTS idx_trades_wallet ON trades(wallet);
    CREATE INDEX IF NOT EXISTS idx_trades_close_timestamp ON trades(close_timestamp);
    CREATE INDEX IF NOT EXISTS idx_trades_status ON trades(status);
    CREATE INDEX IF NOT EXISTS idx_liquidations_wallet ON liquidations(wallet);
    CREATE INDEX IF NOT EXISTS idx_liquidations_timestamp ON liquidations(timestamp);
    CREATE INDEX IF NOT EXISTS idx_wallet_snapshots_wallet ON wallet_snapshots(wallet);
    CREATE INDEX IF NOT EXISTS idx_wallet_snapshots_timestamp ON wallet_snapshots(timestamp);
    """
    
    try:
        async with db_pool.acquire() as conn:
            await conn.execute(create_trades_table)
            await conn.execute(create_liquidations_table)
            await conn.execute(create_wallet_snapshots_table)
            await conn.execute(create_indexes)
            print("✅ Tabelas e índices criados/verificados")
    except Exception as e:
        print(f"❌ Erro ao criar tabelas: {e}")
        raise

# ============================================
# FUNÇÕES DE TRACKING DE TRADES
# ============================================

async def save_open_trade(wallet: str, nickname: str, position: dict):
    """Salva uma posição que acabou de abrir"""
    if not db_pool:
        return
    
    try:
        # Extrair dados da posição
        token = position.get("coin", "UNKNOWN")
        side = "LONG" if float(position.get("szi", 0)) > 0 else "SHORT"
        size = abs(float(position.get("szi", 0)))
        entry_price = float(position.get("entryPx", 0))
        leverage_data = position.get("leverage", {})
        leverage = float(leverage_data.get("value", 1)) if isinstance(leverage_data, dict) else 1.0
        
        # Verificar se trade já existe (evitar duplicatas)
        check_query = """
        SELECT id FROM trades 
        WHERE wallet = $1 AND token = $2 AND status = 'open'
        LIMIT 1
        """
        
        async with db_pool.acquire() as conn:
            existing = await conn.fetchval(check_query, wallet, token)
            
            if existing:
                # Trade já existe, não duplicar
                return
            
            # Inserir novo trade
            insert_query = """
            INSERT INTO trades (
                wallet, nickname, token, side, size, 
                entry_price, leverage, open_timestamp, status
            ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
            """
            
            await conn.execute(
                insert_query,
                wallet, nickname, token, side, size,
                entry_price, leverage, datetime.now(), 'open'
            )
            
            print(f"💾 Trade salvo: {nickname} | {token} {side} | ${entry_price:.4f}")
            
    except Exception as e:
        print(f"❌ Erro ao salvar trade: {e}")

async def close_trade(wallet: str, token: str, exit_price: float, pnl: float):
    """Fecha um trade quando a posição é encerrada"""
    if not db_pool:
        return
    
    try:
        update_query = """
        UPDATE trades 
        SET exit_price = $1, 
            pnl = $2, 
            close_timestamp = $3,
            status = 'closed'
        WHERE wallet = $4 
          AND token = $5 
          AND status = 'open'
        """
        
        async with db_pool.acquire() as conn:
            result = await conn.execute(
                update_query,
                exit_price, pnl, datetime.now(),
                wallet, token
            )
            
            if result != "UPDATE 0":
                result_type = "LUCRO" if pnl > 0 else "PREJUÍZO"
                print(f"✅ Trade fechado: {wallet[:8]} | {token} | ${pnl:,.2f} ({result_type})")
            
    except Exception as e:
        print(f"❌ Erro ao fechar trade: {e}")

async def save_liquidation(wallet: str, nickname: str, position: dict, loss: float):
    """Salva uma liquidação detectada"""
    if not db_pool:
        return
    
    try:
        token = position.get("coin", "UNKNOWN")
        side = "LONG" if float(position.get("szi", 0)) > 0 else "SHORT"
        size = abs(float(position.get("szi", 0)))
        liquidation_px = float(position.get("liquidationPx", 0))
        
        insert_query = """
        INSERT INTO liquidations (
            wallet, nickname, token, side, size,
            liquidation_price, loss_amount, timestamp
        ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
        """
        
        async with db_pool.acquire() as conn:
            await conn.execute(
                insert_query,
                wallet, nickname, token, side, size,
                liquidation_px, loss, datetime.now()
            )
            
            print(f"💀 Liquidação salva: {nickname} | {token} {side} | -${abs(loss):,.2f}")
            
        # Fechar o trade como liquidado
        await close_trade(wallet, token, liquidation_px, loss)
        
    except Exception as e:
        print(f"❌ Erro ao salvar liquidação: {e}")

async def save_wallet_snapshot(wallet: str, nickname: str, total_value: float, positions_count: int, margin_used: float):
    """Salva snapshot do estado da wallet (1x por hora)"""
    if not db_pool:
        return
    
    try:
        insert_query = """
        INSERT INTO wallet_snapshots (
            wallet, nickname, timestamp, total_value, positions_count, margin_used
        ) VALUES ($1, $2, $3, $4, $5, $6)
        """
        
        async with db_pool.acquire() as conn:
            await conn.execute(
                insert_query,
                wallet, nickname, datetime.now(),
                total_value, positions_count, margin_used
            )
            
    except Exception as e:
        print(f"❌ Erro ao salvar snapshot: {e}")

# ============================================
# FUNÇÕES DE CÁLCULO DE MÉTRICAS
# ============================================

async def calculate_win_rate() -> dict:
    """Calcula Win Rate global, LONG e SHORT"""
    if not db_pool:
        return {
            "global": 0.0,
            "long": 0.0,
            "short": 0.0,
            "total_trades": 0,
            "warning": "Database not connected"
        }
    
    try:
        async with db_pool.acquire() as conn:
            # Win Rate Global
            global_query = """
            SELECT 
                COUNT(*) FILTER (WHERE pnl > 0) as wins,
                COUNT(*) as total
            FROM trades
            WHERE status = 'closed' AND pnl IS NOT NULL
            """
            global_result = await conn.fetchrow(global_query)
            
            total_trades = global_result['total'] or 0
            wins = global_result['wins'] or 0
            win_rate_global = (wins / total_trades * 100) if total_trades > 0 else 0.0
            
            # Win Rate LONG
            long_query = """
            SELECT 
                COUNT(*) FILTER (WHERE pnl > 0) as wins,
                COUNT(*) as total
            FROM trades
            WHERE status = 'closed' AND side = 'LONG' AND pnl IS NOT NULL
            """
            long_result = await conn.fetchrow(long_query)
            total_long = long_result['total'] or 0
            wins_long = long_result['wins'] or 0
            win_rate_long = (wins_long / total_long * 100) if total_long > 0 else 0.0
            
            # Win Rate SHORT
            short_query = """
            SELECT 
                COUNT(*) FILTER (WHERE pnl > 0) as wins,
                COUNT(*) as total
            FROM trades
            WHERE status = 'closed' AND side = 'SHORT' AND pnl IS NOT NULL
            """
            short_result = await conn.fetchrow(short_query)
            total_short = short_result['total'] or 0
            wins_short = short_result['wins'] or 0
            win_rate_short = (wins_short / total_short * 100) if total_short > 0 else 0.0
            
            return {
                "global": round(win_rate_global, 2),
                "long": round(win_rate_long, 2),
                "short": round(win_rate_short, 2),
                "total_trades": total_trades,
                "total_long": total_long,
                "total_short": total_short
            }
            
    except Exception as e:
        print(f"❌ Erro ao calcular Win Rate: {e}")
        return {
            "global": 0.0,
            "long": 0.0,
            "short": 0.0,
            "total_trades": 0,
            "error": str(e)
        }

async def calculate_sharpe_ratio() -> dict:
    """Calcula Sharpe Ratio dos últimos 30 dias"""
    if not db_pool:
        return {"sharpe_ratio": 0.0, "warning": "Database not connected"}
    
    try:
        async with db_pool.acquire() as conn:
            query = """
            SELECT pnl
            FROM trades
            WHERE status = 'closed' 
              AND pnl IS NOT NULL
              AND close_timestamp >= NOW() - INTERVAL '30 days'
            ORDER BY close_timestamp
            """
            
            results = await conn.fetch(query)
            
            if len(results) < 30:
                return {
                    "sharpe_ratio": 0.0,
                    "message": f"Precisa de 30+ trades (atual: {len(results)})"
                }
            
            # Calcular retornos
            pnls = [float(row['pnl']) for row in results]
            avg_return = sum(pnls) / len(pnls)
            
            # Calcular desvio padrão
            variance = sum((x - avg_return) ** 2 for x in pnls) / len(pnls)
            std_dev = variance ** 0.5
            
            # Sharpe Ratio (assumindo risk-free rate = 0)
            sharpe = (avg_return / std_dev) if std_dev > 0 else 0.0
            
            return {
                "sharpe_ratio": round(sharpe, 2),
                "trades_analyzed": len(results),
                "avg_return": round(avg_return, 2),
                "std_dev": round(std_dev, 2)
            }
            
    except Exception as e:
        print(f"❌ Erro ao calcular Sharpe Ratio: {e}")
        return {"sharpe_ratio": 0.0, "error": str(e)}

async def get_liquidations_count(period_days: int) -> int:
    """Retorna número de liquidações em um período"""
    if not db_pool:
        return 0
    
    try:
        async with db_pool.acquire() as conn:
            query = """
            SELECT COUNT(*) 
            FROM liquidations
            WHERE timestamp >= NOW() - INTERVAL '$1 days'
            """
            # Note: asyncpg doesn't support interval interpolation, so we use a workaround
            query = f"""
            SELECT COUNT(*) 
            FROM liquidations
            WHERE timestamp >= NOW() - INTERVAL '{period_days} days'
            """
            
            count = await conn.fetchval(query)
            return count or 0
            
    except Exception as e:
        print(f"❌ Erro ao contar liquidações: {e}")
        return 0

async def calculate_portfolio_heat(current_whales_data: list) -> float:
    """Calcula Portfolio Heat atual (Margin usado / Capital total)"""
    try:
        total_margin_used = 0.0
        total_account_value = 0.0
        
        for whale in current_whales_data:
            if "error" not in whale:
                positions = whale.get("positions", [])
                for pos in positions:
                    # Margin usado = valor da posição / leverage
                    position_value = abs(float(pos.get("positionValue", 0)))
                    leverage_data = pos.get("leverage", {})
                    leverage = float(leverage_data.get("value", 1)) if isinstance(leverage_data, dict) else 1.0
                    
                    margin = position_value / leverage if leverage > 0 else position_value
                    total_margin_used += margin
                
                # Somar valor total da conta
                total_account_value += whale.get("total_position_value", 0)
        
        # Heat = (Margin / Total) * 100
        heat = (total_margin_used / total_account_value * 100) if total_account_value > 0 else 0.0
        
        return round(heat, 2)
        
    except Exception as e:
        print(f"❌ Erro ao calcular Portfolio Heat: {e}")
        return 0.0

async def get_database_health() -> dict:
    """Retorna estatísticas de saúde do banco de dados"""
    if not db_pool:
        return {
            "status": "disconnected",
            "message": "Database not configured"
        }
    
    try:
        async with db_pool.acquire() as conn:
            # Total de trades
            total_trades = await conn.fetchval("SELECT COUNT(*) FROM trades")
            
            # Trades abertos
            open_trades = await conn.fetchval("SELECT COUNT(*) FROM trades WHERE status = 'open'")
            
            # Trades fechados
            closed_trades = await conn.fetchval("SELECT COUNT(*) FROM trades WHERE status = 'closed'")
            
            # Total de liquidações
            total_liquidations = await conn.fetchval("SELECT COUNT(*) FROM liquidations")
            
            # Liquidações últimas 24h
            liquidations_24h = await conn.fetchval("""
                SELECT COUNT(*) FROM liquidations 
                WHERE timestamp >= NOW() - INTERVAL '1 day'
            """)
            
            # Tamanho do banco (MB)
            db_size = await conn.fetchval("""
                SELECT pg_size_pretty(pg_database_size(current_database()))
            """)
            
            return {
                "status": "connected",
                "total_trades": total_trades,
                "open_trades": open_trades,
                "closed_trades": closed_trades,
                "total_liquidations": total_liquidations,
                "liquidations_24h": liquidations_24h,
                "database_size": db_size,
                "pool_size": db_pool.get_size(),
                "pool_free": db_pool.get_idle_size()
            }
            
    except Exception as e:
        print(f"❌ Erro ao verificar saúde do DB: {e}")
        return {
            "status": "error",
            "error": str(e)
        }

# ============================================
# FUNÇÃO DE BACKUP (JSON EXPORT)
# ============================================

async def export_backup_json() -> dict:
    """Exporta backup de todos os trades em JSON"""
    if not db_pool:
        return {"error": "Database not connected"}
    
    try:
        async with db_pool.acquire() as conn:
            trades = await conn.fetch("""
                SELECT * FROM trades 
                ORDER BY open_timestamp DESC
            """)
            
            liquidations = await conn.fetch("""
                SELECT * FROM liquidations 
                ORDER BY timestamp DESC
            """)
            
            backup_data = {
                "timestamp": datetime.now().isoformat(),
                "trades": [dict(row) for row in trades],
                "liquidations": [dict(row) for row in liquidations],
                "total_trades": len(trades),
                "total_liquidations": len(liquidations)
            }
            
            return backup_data
            
    except Exception as e:
        print(f"❌ Erro ao exportar backup: {e}")
        return {"error": str(e)}
