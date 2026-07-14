from fastapi import FastAPI
from api.analyzer import MarketAnalyzer

app = FastAPI(
    title="TradingCore API",
    version="0.1"
)


@app.get("/")
def root():
    return {
        "status": "online",
        "service": "TradingCore API"
    }


@app.get("/analyze")
def analyze():
    return MarketAnalyzer.analyze()