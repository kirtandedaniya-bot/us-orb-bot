import os
import psycopg2
from datetime import datetime
from dotenv import load_dotenv

load_dotenv()
DB_URL = os.getenv("DATABASE_URL")

def get_connection():
    if not DB_URL:
        raise ValueError("DATABASE_URL is missing. Check your environment variables or GitHub Secrets.")
    return psycopg2.connect(DB_URL)

def initialize_database():
    conn = get_connection()
    cursor = conn.cursor()
    
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS execution_log (
            id SERIAL PRIMARY KEY,
            timestamp TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
            engine VARCHAR(20),
            symbol VARCHAR(15),
            direction VARCHAR(10),
            market_regime INTEGER,
            atr_percentile DECIMAL,
            vwap_entry DECIMAL,
            entry_price DECIMAL,
            stop_price DECIMAL,
            target_price DECIMAL,
            shares INTEGER,
            risk_per_share DECIMAL,
            total_risk DECIMAL,
            status VARCHAR(20),
            net_pnl DECIMAL,
            exit_reason VARCHAR(50)
        );
    """)
    
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS system_events (
            id SERIAL PRIMARY KEY,
            timestamp TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
            engine VARCHAR(20),
            event_type VARCHAR(50),
            message TEXT
        );
    """)
    
    conn.commit()
    cursor.close()
    conn.close()
    print("Database Schema Initialized Successfully.")

def log_trade(engine, symbol, direction, regime, atr, vwap, entry, stop, target, shares, risk, total_risk, status, pnl=0.0, exit_reason="OPEN"):
    try:
        conn = get_connection()
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO execution_log 
            (engine, symbol, direction, market_regime, atr_percentile, vwap_entry, entry_price, stop_price, target_price, shares, risk_per_share, total_risk, status, net_pnl, exit_reason)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """, (engine, symbol, direction, regime, atr, vwap, entry, stop, target, shares, risk, total_risk, status, pnl, exit_reason))
        conn.commit()
        cursor.close()
        conn.close()
    except Exception as e:
        print(f"Database logging error: {e}")

def log_system_event(engine, event_type, message):
    try:
        conn = get_connection()
        cursor = conn.cursor()
        cursor.execute("INSERT INTO system_events (engine, event_type, message) VALUES (%s, %s, %s)", (engine, event_type, message))
        conn.commit()
        cursor.close()
        conn.close()
    except Exception as e:
        print(f"Database logging error: {e}")

if __name__ == "__main__":
    initialize_database()