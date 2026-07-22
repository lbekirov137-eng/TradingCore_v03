Risk rejection results in NO_TRADE.
10. Pipeline errors result in NO_TRADE.
11. Unknown state results in NO_TRADE.
12. Architecture v1.0 does not place real-money orders.
13. Research and AI tasks do not block deterministic execution.
14. New strategies require evidence before production use.
15. Safety rules have priority over trade opportunities.

---

## 33. Architecture v1.0 Acceptance Criteria

Architecture v1.0 is considered correctly implemented when:

- MarketContext exists;
- BaseStep exists;
- executable modules inherit from BaseStep;
- ModuleRegistry preserves execution order;
- Bootstrap registers the initial modules;
- CoreEngine executes modules using execute();
- direct process() calls are absent from CoreEngine;
- IndicatorStep updates indicators;
- StrategyStep updates signals;
- RiskStep updates risk information;
- DecisionEngine returns a safe final decision;
- invalid data produces NO_TRADE;
- module failures produce NO_TRADE;
- architecture tests pass;
- live trading remains disabled.

---

## 34. Final Required Execution Contract

The final mandatory execution contract is:
Bootstrap
    → ModuleRegistry
        → CoreEngine.run(context)
            → IndicatorStep.execute(context)
                → validate(context)
                → process(context)
            → StrategyStep.execute(context)
                → validate(context)
                → process(context)
            → RiskStep.execute(context)
                → validate(context)
                → process(context)
            → DecisionEngine
                → TRADE or NO_TRADE

The Core Engine must call:
module.execute(context)

The Core Engine must not bypass validation by calling:
module.process(context)

directly.

---

End of Architecture v1.0