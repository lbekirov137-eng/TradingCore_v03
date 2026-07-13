from fastapi import FastAPI
from api.analyzer import MarketAnalyzer

app = FastAPI(
    title="TradingCore",
    version="0.3.0"
)


@app.get("/")
def root():
    return {
        "status": "OK",
        "service": "TradingCore",
        "version": "0.3.0",
    }


@app.get("/health")
def health():
    return {
        "status": "healthy"
    }


@app.get("/ping")
def ping():
    return {
        "ping": "pong"
    }


@app.get("/market")
def market():
    return MarketAnalyzer.analyze()