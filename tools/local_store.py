"""本地日K行情库：DuckDB + Parquet。

save_daily(code, df): 把日线 DataFrame 存到 data/daily/<code>.parquet
  df 需含列 date, open, close, high, low, volume（date 为 str 或 date 类型）
load_daily(code): 读回 DataFrame（按日期升序）
query(sql): 用 DuckDB 执行任意 SQL，可直接查 parquet 文件，
  约定视图名 daily_<code> 不预注册；SQL 里用 read_parquet('...') 或占位 {code}。
  简便用法：query("SELECT * FROM daily WHERE code='600276'") ——
  daily 是 data/daily/*.parquet 的联合视图（hive 风格 glob 读取）。
"""
import os
import duckdb
import pandas as pd

BASE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "daily")


def _path(code: str) -> str:
    return os.path.join(BASE, f"{code}.parquet")


def save_daily(code: str, df: pd.DataFrame) -> str:
    os.makedirs(BASE, exist_ok=True)
    out = df.copy()
    out["date"] = pd.to_datetime(out["date"]).dt.date
    for c in ("open", "close", "high", "low", "volume"):
        out[c] = pd.to_numeric(out[c], errors="coerce")
    out = out.sort_values("date").reset_index(drop=True)
    out.to_parquet(_path(code), index=False)
    return _path(code)


def load_daily(code: str) -> pd.DataFrame:
    return pd.read_parquet(_path(code)).sort_values("date").reset_index(drop=True)


def _con():
    con = duckdb.connect()
    pattern = os.path.join(BASE, "*.parquet").replace("'", "''")
    con.execute(
        f"CREATE OR REPLACE VIEW daily AS "
        f"SELECT * FROM read_parquet('{pattern}', filename=true)"
    )
    return con


def query(sql: str) -> pd.DataFrame:
    con = _con()
    try:
        return con.execute(sql).fetchdf()
    finally:
        con.close()
