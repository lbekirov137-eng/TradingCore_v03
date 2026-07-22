import json

from api.core.bootstrap import Bootstrap
from dry_run import build_context


def main() -> None:
    print("=" * 60)
    print("TRADING CORE V2 - DRY RUN DIAGNOSTICS")
    print("REAL ORDERS: DISABLED")
    print("=" * 60)

    engine = Bootstrap.build()
    context = build_context()

    result = engine.execute(context)

    diagnostic_report = {
        "strategy": result.strategy,
        "risk": result.risk,
        "trade_plan": result.execution.get("trade_plan"),
        "decision": result.decision,
        "failed_rules": result.decision.get(
            "failed_rules",
            [],
        ),
        "rules": result.rules,
        "audit": result.audit,
    }

    print(
        json.dumps(
            diagnostic_report,
            indent=2,
            ensure_ascii=False,
            default=str,
        )
    )

    print("=" * 60)
    print("DIAGNOSTICS COMPLETED")
    print("NO REAL ORDER WAS SENT")
    print("=" * 60)


if __name__ == "__main__":
    main()