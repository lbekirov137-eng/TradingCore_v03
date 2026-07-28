"""
Paper-forward журнал: одна запись на каждый цикл наблюдения (TRADE или
NO_TRADE), append-only JSONL, устойчивый к рестарту (только дозапись,
предыдущее содержимое никогда не читается и не может быть повреждено
записью).

Обязательные поля (по требованию):
timestamp, exchange, symbol, timeframe, strategy, market regime, signal,
TRADE/NO_TRADE, причина решения, virtual entry/stop/take-profit,
position size, assumed fees/slippage, virtual net PnL, drawdown,
strategy version, code commit hash.

Секреты сюда никогда не попадают — запись строится только из
публичных рыночных данных и внутреннего состояния paper-контура.
"""

import json
import os
import time
from dataclasses import dataclass, field


DEFAULT_JOURNAL_PATH = os.path.join("state", "paper_forward_journal.jsonl")

_COMMIT_HASH_CACHE = None

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _read_git_head_commit(repo_root: str):
    """
    Читает текущий commit hash напрямую из .git/HEAD (без subprocess —
    ни shell, ни внешний процесс здесь не используются намеренно, чтобы
    не давать этому коду вообще никакой поверхности для command injection).
    Возвращает None, если .git отсутствует (типично для облачного
    рантайм-образа) — в этом случае используется RAILWAY_GIT_COMMIT_SHA.
    """
    git_dir = os.path.join(repo_root, ".git")
    head_path = os.path.join(git_dir, "HEAD")

    if not os.path.isfile(head_path):
        return None

    try:
        with open(head_path, "r", encoding="utf-8") as f:
            head_content = f.read().strip()

        if not head_content.startswith("ref:"):
            return head_content or None  # detached HEAD: content IS the hash

        ref_path = head_content.split(" ", 1)[1].strip()
        full_ref_path = os.path.join(git_dir, ref_path)

        if os.path.isfile(full_ref_path):
            with open(full_ref_path, "r", encoding="utf-8") as f:
                return f.read().strip() or None

        packed_refs_path = os.path.join(git_dir, "packed-refs")
        if os.path.isfile(packed_refs_path):
            with open(packed_refs_path, "r", encoding="utf-8") as f:
                for line in f:
                    if line.strip().endswith(ref_path):
                        return line.split()[0]

        return None
    except OSError:
        return None


def resolve_code_commit_hash() -> str:
    """
    Порядок источников (первый найденный побеждает):
      1. RAILWAY_GIT_COMMIT_SHA / GIT_COMMIT_SHA — Railway передаёт это
         автоматически при деплое из git, даже если .git отсутствует в
         рантайм-образе;
      2. .git/HEAD — если репозиторий доступен локально (чтение файла,
         без subprocess/shell);
      3. "unknown" — безопасный fallback, никогда не бросает исключение.
    """
    global _COMMIT_HASH_CACHE

    if _COMMIT_HASH_CACHE is not None:
        return _COMMIT_HASH_CACHE

    env_sha = os.getenv("RAILWAY_GIT_COMMIT_SHA") or os.getenv("GIT_COMMIT_SHA")
    if env_sha:
        _COMMIT_HASH_CACHE = env_sha.strip()
        return _COMMIT_HASH_CACHE

    head_commit = _read_git_head_commit(_REPO_ROOT)
    if head_commit:
        _COMMIT_HASH_CACHE = head_commit
        return _COMMIT_HASH_CACHE

    _COMMIT_HASH_CACHE = "unknown"
    return _COMMIT_HASH_CACHE


def compute_signal_id(exchange, symbol, strategy, timestamp_ms) -> str:
    """Детерминированный ID сигнала — для трассировки, не для дедупликации
    ордеров (за идемпотентность ордеров отвечает IdempotencyStore)."""
    import hashlib
    raw = f"{exchange}|{symbol}|{strategy}|{timestamp_ms}"
    return "sig-" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


@dataclass
class PaperForwardJournal:

    path: str = DEFAULT_JOURNAL_PATH

    def __post_init__(self):
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)

    def record(self, context, decision: dict, execution: dict) -> dict:
        """
        Строит и дозаписывает одну запись журнала за тик. Никогда не
        бросает исключение наружу — сбой журналирования не должен
        останавливать торговый цикл (при ошибке пишет в stderr и
        возвращает частичную запись с полем "journal_error").
        """

        try:
            entry = self._build_entry(context, decision, execution)
            self._append(entry)
            return entry
        except Exception as error:
            fallback = {
                "timestamp": time.time(),
                "journal_error": f"{type(error).__name__}: {error}",
            }
            try:
                self._append(fallback)
            except Exception:
                pass
            return fallback

    def _build_entry(self, context, decision: dict, execution: dict) -> dict:

        from api.trade_engine import trade_engine as te
        from api.risk.guards import MaxDrawdownGuard

        decision = decision or {}
        execution = execution or {}

        signals = getattr(context, "strategy_signals", None) or []
        signal = signals[0] if signals else {}
        metadata = (signal.get("metadata") or {}) if isinstance(signal, dict) else {}

        exchange = decision.get("exchange") or getattr(context, "exchange", None)
        symbol = decision.get("symbol") or getattr(context, "symbol", None)
        strategy_name = decision.get("strategy") or (signal.get("strategy") if isinstance(signal, dict) else None)

        market = getattr(context, "market", None)
        last_timestamp_ms = None
        if market is not None and getattr(market, "timestamps", None):
            last_timestamp_ms = market.timestamps[-1]

        trade_plan = decision.get("trade_plan") or {}
        take_profit = trade_plan.get("take_profit") or {}
        risk = decision.get("risk") or {}

        try:
            balance_info = te.broker.get_balance()
            virtual_net_pnl = balance_info.get("realized_pnl")
            current_equity = balance_info.get("balance")
        except Exception:
            virtual_net_pnl = None
            current_equity = None

        drawdown_percent = None
        if current_equity is not None and MaxDrawdownGuard._peak_equity:
            peak = MaxDrawdownGuard._peak_equity
            if peak > 0:
                drawdown_percent = round((peak - current_equity) / peak * 100, 4)

        strategy_version, strategy_status = _lookup_strategy_meta(strategy_name)

        signal_id = compute_signal_id(exchange, symbol, strategy_name, last_timestamp_ms or time.time() * 1000)

        return {
            "timestamp": time.time(),
            "candle_timestamp_ms": last_timestamp_ms,
            "exchange": exchange,
            "symbol": symbol,
            "timeframe": getattr(context, "interval", None),
            "strategy": strategy_name,
            "strategy_version": strategy_version,
            "strategy_status": strategy_status,
            "market_regime": metadata.get("regime"),
            "signal": {
                "approved": signal.get("approved") if isinstance(signal, dict) else None,
                "direction": signal.get("direction") if isinstance(signal, dict) else None,
                "confidence": signal.get("confidence") if isinstance(signal, dict) else None,
            },
            "decision": decision.get("decision", "NO_TRADE"),
            "reason": decision.get("reason"),
            "virtual_entry": trade_plan.get("entry"),
            "virtual_stop": trade_plan.get("stop_loss"),
            "virtual_take_profit": take_profit.get("tp1"),
            "position_size": risk.get("position_size"),
            "assumed_fees": risk.get("fee_amount"),
            "assumed_slippage": risk.get("slippage_amount"),
            "virtual_net_pnl": virtual_net_pnl,
            "drawdown_percent": drawdown_percent,
            "signal_id": signal_id,
            "trade_id": execution.get("entry_client_order_id") or execution.get("client_order_id"),
            "execution_status": execution.get("status"),
            "code_commit_hash": resolve_code_commit_hash(),
        }

    def _append(self, entry: dict):
        line = json.dumps(entry, ensure_ascii=False, default=str)
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(line + "\n")

    def read_all(self) -> list:
        """Читает журнал для экспорта/анализа. Повреждённые строки пропускаются, не роняют чтение."""
        if not os.path.exists(self.path):
            return []

        entries = []
        with open(self.path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entries.append(json.loads(line))
                except json.JSONDecodeError:
                    continue  # повреждённая строка пропускается, журнал не считается сломанным целиком

        return entries


def _lookup_strategy_meta(strategy_name):
    """Возвращает (version, status) по имени стратегии, без исключений."""
    try:
        from api.strategy_engine.strategies.orb.orb_strategy import ORBStrategy
        from api.strategy_engine.strategies.vwap.vwap_strategy import VWAPTrendPullbackStrategy

        for cls in (ORBStrategy, VWAPTrendPullbackStrategy):
            if cls.NAME == strategy_name:
                return cls.VERSION, cls.STATUS
    except Exception:
        pass

    return None, None


journal = PaperForwardJournal()
