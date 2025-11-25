"""
database.py - Sistema de banco de dados PostgreSQL para Whale Tracker
Responsável por: tracking de trades, liquidações e cálculo de métricas reais
FASE 5: Métricas INDIVIDUAIS por wallet
🆕 BUG FIX 2: Estado de alertas persistente no PostgreSQL
"""

import os
import asyncpg
from datetime import datetime, timedelta
from typing import List, Dict, Optional
import asyncio
import json

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
    
    # 🆕 BUG FIX 2: Nova tabela para estado de alertas
    create_alert_state_table = """
    CREATE TABLE IF NOT EXISTS alert_state (
        id SERIAL PRIMARY KEY,
        state_key VARCHAR(50) UNIQUE NOT NULL,
        state_data JSONB NOT NULL,
        updated_at TIMESTAMP DEFAULT NOW()
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
    CREATE INDEX IF NOT EXISTS idx_alert_state_key ON alert_state(state_key);
    """
    
    try:
        async with db_pool.acquire() as conn:
            await conn.execute(create_trades_table)
            await conn.execute(create_liquidations_table)
            await conn.execute(create_wallet_snapshots_table)
            await conn.execute(create_alert_state_table)  # 🆕 BUG FIX 2
            await conn.execute(create_indexes)
            print("✅ Tabelas e índices criados/verificados")
    except Exception as e:
        print(f"❌ Erro ao criar tabelas: {e}")
        raise

# ============================================
# 🆕 BUG FIX 2: FUNÇÕES DE ESTADO PERSISTENTE
# ============================================

async def save_alert_state(alert_state: dict):
    """
    Salva o estado atual de alertas no PostgreSQL
    Evita perda de estado quando Render reinicia o container
    """
    if not db_pool:
        return
    
    try:
        # Converter set para list para JSON
        state_to_save = {
            "positions": alert_state.get("positions", {}),
            "orders": alert_state.get("orders", {}),
            "liquidation_warnings": list(alert_state.get("liquidation_warnings", set())),
            "last_alert_time": alert_state.get("last_alert_time", {})
        }
        
        async with db_pool.acquire() as conn:
            # Usar UPSERT (INSERT ... ON CONFLICT UPDATE)
            query = """
            INSERT INTO alert_state (state_key, state_data, updated_at)
            VALUES ($1, $2, NOW())
            ON CONFLICT (state_key) 
            DO UPDATE SET 
                state_data = $2,
                updated_at = NOW()
            """
            
            await conn.execute(
                query,
                'current_alert_state',
                json.dumps(state_to_save)
            )
            
    except Exception as e:
        print(f"❌ Erro ao salvar estado de alertas: {e}")

async def load_alert_state() -> Optional[dict]:
    """
    Carrega o estado de alertas do PostgreSQL ao iniciar
    Retorna None se não houver estado salvo
    """
    if not db_pool:
        return None
    
    try:
        async with db_pool.acquire() as conn:
            query = """
            SELECT state_data FROM alert_state
            WHERE state_key = $1
            LIMIT 1
            """
            
            result = await conn.fetchval(query, 'current_alert_state')
            
            if result:
                state_data = json.loads(result) if isinstance(result, str) else result
                
                # Converter list de volta para set
                state_data['liquidation_warnings'] = set(state_data.get('liquidation_warnings', []))
                
                print(f"✅ Estado carregado: {len(state_data.get('positions', {}))} posições, {len(state_data.get('orders', {}))} orders")
                return state_data
            else:
                return None
                
    except Exception as e:
        print(f"❌ Erro ao carregar estado de alertas: {e}")
        return None

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
# ✅ FASE 5: MÉTRICAS INDIVIDUAIS POR WALLET
# ============================================

async def calculate_wallet_metrics(wallet: str, current_positions: list) -> dict:
    """
    Calcula TODAS as métricas para UMA wallet específica
    Retorna dict pronto para ser inserido no campo 'metrics' da whale
    """
    if not db_pool:
        return {
            "win_rate_global": None,
            "win_rate_long": None,
            "win_rate_short": None,
            "sharpe_ratio": None,
            "portfolio_heat": None,
            "liquidations_1d": None,
            "liquidations_1w": None,
            "liquidations_1m": None,
            "total_trades": 0
        }
    
    try:
        async with db_pool.acquire() as conn:
            # ===== WIN RATE GLOBAL =====
            win_rate_query = """
            SELECT 
                COUNT(*) FILTER (WHERE pnl > 0) as wins,
                COUNT(*) as total
            FROM trades
            WHERE wallet = $1 AND status = 'closed' AND pnl IS NOT NULL
            """
            win_rate_result = await conn.fetchrow(win_rate_query, wallet)
            total_trades = win_rate_result['total'] or 0
            wins = win_rate_result['wins'] or 0
            win_rate_global = (wins / total_trades * 100) if total_trades > 0 else None
            
            # ===== WIN RATE LONG =====
            long_query = """
            SELECT 
                COUNT(*) FILTER (WHERE pnl > 0) as wins,
                COUNT(*) as total
            FROM trades
            WHERE wallet = $1 AND status = 'closed' AND side = 'LONG' AND pnl IS NOT NULL
            """
            long_result = await conn.fetchrow(long_query, wallet)
            total_long = long_result['total'] or 0
            wins_long = long_result['wins'] or 0
            win_rate_long = (wins_long / total_long * 100) if total_long > 0 else None
            
            # ===== WIN RATE SHORT =====
            short_query = """
            SELECT 
                COUNT(*) FILTER (WHERE pnl > 0) as wins,
                COUNT(*) as total
            FROM trades
            WHERE wallet = $1 AND status = 'closed' AND side = 'SHORT' AND pnl IS NOT NULL
            """
            short_result = await conn.fetchrow(short_query, wallet)
            total_short = short_result['total'] or 0
            wins_short = short_result['wins'] or 0
            win_rate_short = (wins_short / total_short * 100) if total_short > 0 else None
            
            # ===== SHARPE RATIO (últimos 30 dias) =====
            sharpe_query = """
            SELECT pnl
            FROM trades
            WHERE wallet = $1 
              AND status = 'closed' 
              AND pnl IS NOT NULL
              AND close_timestamp >= NOW() - INTERVAL '30 days'
            ORDER BY close_timestamp
            """
            sharpe_results = await conn.fetch(sharpe_query, wallet)
            
            sharpe_ratio = None
            if len(sharpe_results) >= 30:
                pnls = [float(row['pnl']) for row in sharpe_results]
                avg_return = sum(pnls) / len(pnls)
                variance = sum((x - avg_return) ** 2 for x in pnls) / len(pnls)
                std_dev = variance ** 0.5
                sharpe_ratio = (avg_return / std_dev) if std_dev > 0 else 0.0
            
            # ===== PORTFOLIO HEAT (posições atuais) =====
            portfolio_heat = None
            if current_positions:
                total_margin_used = 0.0
                total_position_value = 0.0
                
                for pos in current_positions:
                    position_value = abs(float(pos.get("positionValue", 0)))
                    leverage_data = pos.get("leverage", {})
                    leverage = float(leverage_data.get("value", 1)) if isinstance(leverage_data, dict) else 1.0
                    
                    margin = position_value / leverage if leverage > 0 else position_value
                    total_margin_used += margin
                    total_position_value += position_value
                
                portfolio_heat = (total_margin_used / total_position_value * 100) if total_position_value > 0 else 0.0
            
            # ===== LIQUIDAÇÕES 1D/1W/1M =====
            liq_1d_query = """
            SELECT COUNT(*) FROM liquidations
            WHERE wallet = $1 AND timestamp >= NOW() - INTERVAL '1 day'
            """
            liquidations_1d = await conn.fetchval(liq_1d_query, wallet) or 0
            
            liq_1w_query = """
            SELECT COUNT(*) FROM liquidations
            WHERE wallet = $1 AND timestamp >= NOW() - INTERVAL '7 days'
            """
            liquidations_1w = await conn.fetchval(liq_1w_query, wallet) or 0
            
            liq_1m_query = """
            SELECT COUNT(*) FROM liquidations
            WHERE wallet = $1 AND timestamp >= NOW() - INTERVAL '30 days'
            """
            liquidations_1m = await conn.fetchval(liq_1m_query, wallet) or 0
            
            # ===== RETORNAR MÉTRICAS =====
            return {
                "win_rate_global": round(win_rate_global, 2) if win_rate_global is not None else None,
                "win_rate_long": round(win_rate_long, 2) if win_rate_long is not None else None,
                "win_rate_short": round(win_rate_short, 2) if win_rate_short is not None else None,
                "sharpe_ratio": round(sharpe_ratio, 2) if sharpe_ratio is not None else None,
                "portfolio_heat": round(portfolio_heat, 2) if portfolio_heat is not None else None,
                "liquidations_1d": liquidations_1d,
                "liquidations_1w": liquidations_1w,
                "liquidations_1m": liquidations_1m,
                "total_trades": total_trades
            }
            
    except Exception as e:
        print(f"❌ Erro ao calcular métricas da wallet {wallet[:8]}: {e}")
        return {
            "win_rate_global": None,
            "win_rate_long": None,
            "win_rate_short": None,
            "sharpe_ratio": None,
            "portfolio_heat": None,
            "liquidations_1d": None,
            "liquidations_1w": None,
            "liquidations_1m": None,
            "total_trades": 0,
            "error": str(e)
        }

# ============================================
# FUNÇÕES LEGADAS (COMPATIBILIDADE)
# ============================================

async def calculate_win_rate() -> dict:
    """Calcula Win Rate global (TODAS as whales) - LEGADO"""
    if not db_pool:
        return {"global": 0.0, "long": 0.0, "short": 0.0, "total_trades": 0}
    
    try:
        async with db_pool.acquire() as conn:
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
                "total_trades": total_trades
            }
    except Exception as e:
        return {"global": 0.0, "long": 0.0, "short": 0.0, "total_trades": 0, "error": str(e)}

async def calculate_sharpe_ratio() -> dict:
    """Calcula Sharpe Ratio global - LEGADO"""
    if not db_pool:
        return {"sharpe_ratio": 0.0}
    
    try:
        async with db_pool.acquire() as conn:
            query = """
            SELECT pnl FROM trades
            WHERE status = 'closed' AND pnl IS NOT NULL
              AND close_timestamp >= NOW() - INTERVAL '30 days'
            """
            results = await conn.fetch(query)
            if len(results) < 30:
                return {"sharpe_ratio": 0.0, "message": f"Precisa 30+ trades ({len(results)})"}
            
            pnls = [float(row['pnl']) for row in results]
            avg_return = sum(pnls) / len(pnls)
            variance = sum((x - avg_return) ** 2 for x in pnls) / len(pnls)
            std_dev = variance ** 0.5
            sharpe = (avg_return / std_dev) if std_dev > 0 else 0.0
            
            return {"sharpe_ratio": round(sharpe, 2), "trades_analyzed": len(results)}
    except Exception as e:
        return {"sharpe_ratio": 0.0, "error": str(e)}

async def get_liquidations_count(period_days: int) -> int:
    """Retorna liquidações globais - LEGADO"""
    if not db_pool:
        return 0
    
    try:
        async with db_pool.acquire() as conn:
            query = f"""
            SELECT COUNT(*) FROM liquidations
            WHERE timestamp >= NOW() - INTERVAL '{period_days} days'
            """
            count = await conn.fetchval(query)
            return count or 0
    except Exception as e:
        return 0

async def calculate_portfolio_heat(current_whales_data: list) -> float:
    """Calcula Portfolio Heat global - LEGADO"""
    try:
        total_margin_used = 0.0
        total_account_value = 0.0
        
        for whale in current_whales_data:
            if "error" not in whale:
                positions = whale.get("positions", [])
                for pos in positions:
                    position_value = abs(float(pos.get("positionValue", 0)))
                    leverage_data = pos.get("leverage", {})
                    leverage = float(leverage_data.get("value", 1)) if isinstance(leverage_data, dict) else 1.0
                    margin = position_value / leverage if leverage > 0 else position_value
                    total_margin_used += margin
                total_account_value += whale.get("total_position_value", 0)
        
        heat = (total_margin_used / total_account_value * 100) if total_account_value > 0 else 0.0
        return round(heat, 2)
    except Exception as e:
        return 0.0

async def get_database_health() -> dict:
    """Retorna estatísticas de saúde do banco de dados"""
    if not db_pool:
        return {"status": "disconnected"}
    
    try:
        async with db_pool.acquire() as conn:
            total_trades = await conn.fetchval("SELECT COUNT(*) FROM trades")
            open_trades = await conn.fetchval("SELECT COUNT(*) FROM trades WHERE status = 'open'")
            closed_trades = await conn.fetchval("SELECT COUNT(*) FROM trades WHERE status = 'closed'")
            total_liquidations = await conn.fetchval("SELECT COUNT(*) FROM liquidations")
            liquidations_24h = await conn.fetchval("""
                SELECT COUNT(*) FROM liquidations 
                WHERE timestamp >= NOW() - INTERVAL '1 day'
            """)
            db_size = await conn.fetchval("SELECT pg_size_pretty(pg_database_size(current_database()))")
            
            # 🆕 BUG FIX 2: Incluir info de estado de alertas
            alert_state_exists = await conn.fetchval("""
                SELECT COUNT(*) FROM alert_state WHERE state_key = 'current_alert_state'
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
                "pool_free": db_pool.get_idle_size(),
                "alert_state_saved": alert_state_exists > 0  # 🆕 BUG FIX 2
            }
    except Exception as e:
        return {"status": "error", "error": str(e)}

async def export_backup_json() -> dict:
    """Exporta backup completo em JSON"""
    if not db_pool:
        return {"error": "Database not connected"}
    
    try:
        async with db_pool.acquire() as conn:
            trades = await conn.fetch("SELECT * FROM trades ORDER BY open_timestamp DESC")
            liquidations = await conn.fetch("SELECT * FROM liquidations ORDER BY timestamp DESC")
            
            # 🆕 BUG FIX 2: Incluir estado de alertas no backup
            alert_state_data = await conn.fetchval("""
                SELECT state_data FROM alert_state 
                WHERE state_key = 'current_alert_state'
            """)
            
            return {
                "timestamp": datetime.now().isoformat(),
                "trades": [dict(row) for row in trades],
                "liquidations": [dict(row) for row in liquidations],
                "alert_state": json.loads(alert_state_data) if alert_state_data else None,  # 🆕 BUG FIX 2
                "total_trades": len(trades),
                "total_liquidations": len(liquidations)
            }
    except Exception as e:
        return {"error": str(e)}
