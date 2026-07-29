"""
Тесты Adaptive Strategy Supervisor.

Проверяются четыре класса свойств, и первые два важнее остальных:

  1. ЧТО СУПЕРВИЗОР НЕ МОЖЕТ. Реестр закрыт, параметры заморожены,
     незарегистрированная стратегия невыбираема, пороги не ослабляются.
     Это тесты не на поведение, а на невозможность поведения.

  2. НЕ ОСТАНАВЛИВАТЬ СЛИШКОМ РАНО. Убыточная серия при малой выборке
     обязана давать SAFE/INSUFFICIENT_SAMPLE, а не PAUSED. Ошибка в эту
     сторону дороже: она выбрасывает рабочую стратегию по шуму.

  3. Остановка при подтверждённой убыточности и достаточной выборке.

  4. Валидация: хронологическое разбиение, holdout, отсутствие утечки.
"""

import dataclasses
from datetime import datetime, timedelta, timezone

import pytest

from api.strategy_supervisor import (
    DEFAULT_STRATEGY_ID,
    EMERGENCY_STOP,
    INSUFFICIENT_SAMPLE,
    NO_VALID_STRATEGY,
    PAUSED,
    SAFE,
    STRATEGY_REGISTRY,
    ClosedTrade,
    StrategyRegistryError,
    ValidationError,
    build_change_report,
    build_stats,
    compare_champion_challengers,
    detect_look_ahead_leakage,
    emergency_violations,
    evaluate_active_strategy,
    get_strategy,
    is_registered,
    plan_switch,
    promotion_gates,
    select_replacement,
    split_holdout,
    supervisor_status,
    thresholds_snapshot,
    validate_candidate,
)
from api.strategy_supervisor.registry import (
    CANDIDATE,
    RANGE,
    TREND,
    CostModel,
    StrategySpec,
)


TREND_STRATEGY = "TREND_PULLBACK_EMA_STRUCTURE"


def observation(
    *,
    utc: str,
    event: str = "POSITION_CLOSED",
    net_pnl: float | None = None,
    regime: str = "TREND",
    decision: str = "TRADE",
    signal: str = "BUY",
    side: str = "LONG",
    real_order_sent: bool = False,
    failed_safely: bool = False,
) -> dict:
    return {
        "recorded_at_utc": utc,
        "position_event": event,
        "market_regime": regime,
        "decision": decision,
        "signal": signal,
        "side": side,
        "net_pnl": net_pnl,
        "realized_pnl": net_pnl,
        "risk_amount": 1.0,
        "entry": 100.0,
        "stop": 90.0,
        "quantity": 0.1,
        "real_order_sent": real_order_sent,
        "failed_safely": failed_safely,
    }


def losing_series(
    count: int,
    *,
    start_day: int = 1,
    regimes: tuple[str, ...] = ("TREND",),
    pnl: float = -1.0,
) -> list[dict]:
    """Убыточные сделки, размазанные по дням и режимам."""
    out = []

    for index in range(count):
        day = start_day + (index % 5)
        regime = regimes[index % len(regimes)]

        out.append(
            observation(
                utc=f"2026-07-{day:02d}T10:{index % 60:02d}:00+00:00",
                net_pnl=pnl,
                regime=regime,
            )
        )

    return out


def trades(
    r_values: list[float],
    *,
    start: str = "2026-06-01T10:00:00+00:00",
) -> list[ClosedTrade]:
    base = datetime.fromisoformat(start)

    return [
        ClosedTrade(
            strategy_id=TREND_STRATEGY,
            closed_at_utc=(base + timedelta(hours=index)).isoformat(),
            regime="TREND",
            net_pnl=value,
            r_multiple=value,
        )
        for index, value in enumerate(r_values)
    ]


# =====================================================================
# 1. Чего супервизор НЕ может
# =====================================================================


class TestRegistryIsClosed:

    def test_registry_contains_the_five_approved_entries(self) -> None:
        ids = {spec.strategy_id for spec in STRATEGY_REGISTRY}

        assert ids == {
            "SESSION_VWAP_TREND_PULLBACK",
            "LONDON_SESSION_BREAKOUT_RETEST",
            "ORB_0930_RETEST",
            "TREND_PULLBACK_EMA_STRUCTURE",
            "RANGE_NO_TRADE_POLICY",
        }

    def test_every_strategy_declares_the_required_contract(self) -> None:
        for spec in STRATEGY_REGISTRY:
            assert spec.version
            assert spec.status in ("CANDIDATE", "PAPER_ACTIVE", "PAUSED", "REJECTED")
            assert spec.allowed_regimes
            assert spec.exit_criteria
            assert spec.cost_model.taker_fee_rate > 0
            assert spec.key == f"{spec.strategy_id}@{spec.version}"

            if spec.tradable:
                assert spec.entry_criteria
                assert spec.min_risk_reward >= 1.0

    def test_parameters_are_immutable(self) -> None:
        """
        Ключевая защита: параметры нельзя подкрутить в рантайме.
        """
        spec = get_strategy(TREND_STRATEGY)

        with pytest.raises(dataclasses.FrozenInstanceError):
            spec.min_risk_reward = 1.0  # type: ignore[misc]

        # Возвращается КОПИЯ: мутация не доходит до реестра.
        params = spec.params
        params["fast_ema"] = 999

        assert get_strategy(TREND_STRATEGY).params["fast_ema"] == 20

    def test_unregistered_strategy_cannot_be_fetched(self) -> None:
        with pytest.raises(StrategyRegistryError, match="not registered"):
            get_strategy("MY_BRAND_NEW_AI_STRATEGY")

        assert is_registered("MY_BRAND_NEW_AI_STRATEGY") is False

    def test_unregistered_strategy_cannot_be_selected(self) -> None:
        """Даже с идеальной валидацией: не в реестре — не выбирается."""
        perfect = {
            "oos_trades": 500,
            "oos_net_pnl": 9999.0,
            "oos_profit_factor": 10.0,
            "oos_expectancy_r": 5.0,
            "oos_max_drawdown_r": 0.1,
            "safety_violations": [],
            "robustness_ratio": 1.0,
            "walk_forward_passed": True,
            "look_ahead_leakage": False,
            "sample_id": "S1",
        }

        result = select_replacement({"INVENTED_STRATEGY": perfect})

        assert result["selected"] is None
        assert result["status"] == NO_VALID_STRATEGY
        assert result["fallback_strategy_id"] == DEFAULT_STRATEGY_ID

    def test_invalid_spec_is_refused_at_construction(self) -> None:
        with pytest.raises(StrategyRegistryError):
            StrategySpec(
                strategy_id="BAD",
                name="Bad",
                version="1.0.0",
                status="INVENTED_STATUS",
                allowed_regimes=(TREND,),
                entry_criteria=("x",),
                exit_criteria=("y",),
                min_risk_reward=2.0,
            )

        with pytest.raises(StrategyRegistryError, match="min_risk_reward"):
            StrategySpec(
                strategy_id="BAD_RR",
                name="Bad RR",
                version="1.0.0",
                status=CANDIDATE,
                allowed_regimes=(TREND,),
                entry_criteria=("x",),
                exit_criteria=("y",),
                min_risk_reward=0.5,
            )

    def test_range_policy_is_registered_and_not_tradable(self) -> None:
        spec = get_strategy("RANGE_NO_TRADE_POLICY")

        assert spec.tradable is False
        assert spec.allowed_regimes == (RANGE,)
        assert DEFAULT_STRATEGY_ID == spec.strategy_id


# =====================================================================
# 2. Не останавливать слишком рано
# =====================================================================


class TestSampleAdequacy:

    def test_small_losing_sample_does_not_pause(self) -> None:
        """
        Десять убыточных сделок — это не доказательство убыточности.
        Остановка здесь была бы дороже ошибки в другую сторону.
        """
        evaluation = evaluate_active_strategy(
            TREND_STRATEGY,
            losing_series(10),
        )

        assert evaluation["status"] == SAFE
        assert evaluation["insufficient_sample"] == INSUFFICIENT_SAMPLE
        assert evaluation["reasons"] == []
        # Признаки видны, но на них не действуют.
        assert evaluation["observed_triggers"]

    def test_single_day_sample_is_insufficient(self) -> None:
        """50 сделок одного дня описывают день, а не стратегию."""
        same_day = [
            observation(
                utc=f"2026-07-01T10:{index % 60:02d}:00+00:00",
                net_pnl=-1.0,
            )
            for index in range(60)
        ]

        evaluation = evaluate_active_strategy(TREND_STRATEGY, same_day)

        assert evaluation["insufficient_sample"] == INSUFFICIENT_SAMPLE
        assert any(
            "trading_days" in item
            for item in evaluation["sample"]["unmet_requirements"]
        )
        assert evaluation["status"] != PAUSED

    def test_multi_regime_requirement_applies_only_when_declared(self) -> None:
        """
        Стратегия с ОДНИМ разрешённым режимом не обязана показать два.
        Иначе её нельзя было бы оценить в принципе.
        """
        single_regime = get_strategy("ORB_0930_RETEST")

        assert len(single_regime.allowed_regimes) == 1

        evaluation = evaluate_active_strategy(
            single_regime.strategy_id,
            losing_series(60, regimes=("BREAKOUT",)),
        )

        assert evaluation["sample"]["required_regimes"] == 1
        assert evaluation["sample"]["sufficient"] is True

    def test_thresholds_match_the_specification(self) -> None:
        thresholds = thresholds_snapshot()

        assert thresholds["sample"]["min_closed_trades_warning"] == 30
        assert thresholds["sample"]["min_closed_trades_pause"] == 50
        assert thresholds["pause"]["profit_factor_below"] == 0.90
        assert thresholds["pause"]["consecutive_losses"] == 6
        assert thresholds["promote"]["min_oos_trades"] == 30
        assert thresholds["promote"]["min_profit_factor"] == 1.15
        assert thresholds["cooldown"]["switch_cooldown_days"] == 7


# =====================================================================
# 3. Остановка при подтверждённой убыточности
# =====================================================================


class TestPauseConditions:

    def test_large_losing_sample_pauses(self) -> None:
        evaluation = evaluate_active_strategy(
            TREND_STRATEGY,
            losing_series(60),
        )

        assert evaluation["status"] == PAUSED
        assert evaluation["sample"]["sufficient"] is True
        assert any("net PnL" in reason for reason in evaluation["reasons"])

    def test_consecutive_losses_are_reported(self) -> None:
        evaluation = evaluate_active_strategy(
            TREND_STRATEGY,
            losing_series(60),
        )

        assert any(
            "consecutive losing trades" in reason
            for reason in evaluation["reasons"]
        )

    def test_profitable_strategy_stays_safe(self) -> None:
        winners = [
            observation(
                utc=f"2026-07-{1 + index % 6:02d}T10:{index % 60:02d}:00+00:00",
                net_pnl=2.0 if index % 3 else -1.0,
            )
            for index in range(60)
        ]

        evaluation = evaluate_active_strategy(TREND_STRATEGY, winners)

        assert evaluation["status"] == SAFE
        assert evaluation["reasons"] == []

    def test_out_of_regime_trading_is_a_trigger(self) -> None:
        """ORB разрешён только в BREAKOUT; торговля в RANGE — нарушение."""
        records = [
            observation(
                utc=f"2026-07-{1 + index % 6:02d}T10:{index % 60:02d}:00+00:00",
                event="POSITION_OPENED",
                regime="RANGE",
            )
            for index in range(5)
        ]

        evaluation = evaluate_active_strategy("ORB_0930_RETEST", records)

        assert evaluation["out_of_regime_trades"] == 5
        assert any(
            "outside its allowed market regime" in trigger
            for trigger in evaluation["observed_triggers"]
        )


# =====================================================================
# Аварийные правила (req 9)
# =====================================================================


class TestEmergencyRules:

    def test_real_order_triggers_emergency_stop(self) -> None:
        evaluation = evaluate_active_strategy(
            TREND_STRATEGY,
            [observation(utc="2026-07-01T10:00:00+00:00", real_order_sent=True)],
        )

        assert evaluation["status"] == EMERGENCY_STOP
        assert any(
            "real_order_sent" in reason for reason in evaluation["reasons"]
        )

    def test_short_triggers_emergency_stop(self) -> None:
        evaluation = evaluate_active_strategy(
            TREND_STRATEGY,
            [
                observation(
                    utc="2026-07-01T10:00:00+00:00",
                    signal="SELL",
                    side="SHORT",
                )
            ],
        )

        assert evaluation["status"] == EMERGENCY_STOP

    def test_repeated_failed_safely_triggers_emergency_stop(self) -> None:
        records = [
            observation(utc="2026-07-01T10:00:00+00:00", failed_safely=True),
            observation(utc="2026-07-01T10:05:00+00:00", failed_safely=True),
        ]

        assert any(
            "FAILED_SAFELY repeated" in item
            for item in emergency_violations(records)
        )

    def test_contradiction_triggers_emergency_stop(self) -> None:
        records = [
            observation(
                utc="2026-07-01T10:00:00+00:00",
                decision="TRADE",
                signal="NO TRADE",
            )
        ]

        assert any(
            "contradiction" in item for item in emergency_violations(records)
        )

    def test_emergency_rules_do_not_wait_for_a_sample(self) -> None:
        """
        Один реальный ордер — уже авария. Ждать статистической значимости
        отправленного ордера бессмысленно.
        """
        evaluation = evaluate_active_strategy(
            TREND_STRATEGY,
            [observation(utc="2026-07-01T10:00:00+00:00", real_order_sent=True)],
        )

        assert evaluation["insufficient_sample"] == INSUFFICIENT_SAMPLE
        assert evaluation["status"] == EMERGENCY_STOP

    def test_strategy_change_with_open_position_is_a_violation(self) -> None:
        violations = emergency_violations(
            [],
            strategy_changed_with_open_position=True,
        )

        assert any("while a position was open" in item for item in violations)

    def test_promotion_without_gates_is_a_violation(self) -> None:
        violations = emergency_violations(
            [],
            promoted_without_passing_gates=True,
        )

        assert any("without passing OOS gates" in item for item in violations)


# =====================================================================
# 4. Гейты допуска и выбор замены
# =====================================================================


def passing_validation(**overrides) -> dict:
    validation = {
        "oos_trades": 40,
        "oos_net_pnl": 12.0,
        "oos_profit_factor": 1.4,
        "oos_expectancy_r": 0.3,
        "oos_max_drawdown_r": 4.0,
        "safety_violations": [],
        "robustness_ratio": 0.75,
        "walk_forward_passed": True,
        "look_ahead_leakage": False,
        "sample_id": "SAMPLE_A",
    }
    validation.update(overrides)
    return validation


class TestPromotionGates:

    def test_clean_candidate_passes(self) -> None:
        assert promotion_gates(passing_validation())["passed"] is True

    @pytest.mark.parametrize(
        "override,gate",
        [
            ({"oos_trades": 29}, "min_oos_trades"),
            ({"oos_net_pnl": -0.01}, "oos_net_pnl_positive"),
            ({"oos_profit_factor": 1.14}, "min_profit_factor"),
            ({"oos_expectancy_r": 0.0}, "positive_expectancy"),
            ({"oos_max_drawdown_r": 10.1}, "max_drawdown_within_limit"),
            ({"safety_violations": ["short"]}, "no_safety_violations"),
            ({"robustness_ratio": 0.59}, "parameter_robustness"),
            ({"walk_forward_passed": False}, "walk_forward_passed"),
            ({"look_ahead_leakage": True}, "no_look_ahead_leakage"),
        ],
    )
    def test_each_gate_can_fail_alone(self, override, gate) -> None:
        result = promotion_gates(passing_validation(**override))

        assert result["passed"] is False
        assert gate in result["failed_gates"]

    def test_one_gate_cannot_be_offset_by_another(self) -> None:
        """
        Выдающийся profit factor не компенсирует малую выборку. Именно так
        и выбирают стратегию по одной удачной выборке.
        """
        result = promotion_gates(
            passing_validation(oos_trades=5, oos_profit_factor=99.0)
        )

        assert result["passed"] is False
        assert "min_oos_trades" in result["failed_gates"]


class TestSelection:

    def test_best_expectancy_wins_not_best_total_return(self) -> None:
        """
        Явная защита от выбора по максимальной прибыли: кандидат с
        меньшей суммарной прибылью, но лучшей expectancy, должен победить.
        """
        result = select_replacement(
            {
                "ORB_0930_RETEST": passing_validation(
                    oos_expectancy_r=0.10,
                    oos_net_pnl=500.0,
                ),
                TREND_STRATEGY: passing_validation(
                    oos_expectancy_r=0.45,
                    oos_net_pnl=20.0,
                ),
            }
        )

        assert result["selected"] == TREND_STRATEGY

    def test_no_candidate_passes_gives_no_valid_strategy(self) -> None:
        result = select_replacement(
            {TREND_STRATEGY: passing_validation(oos_net_pnl=-5.0)}
        )

        assert result["selected"] is None
        assert result["status"] == NO_VALID_STRATEGY
        assert result["fallback_strategy_id"] == DEFAULT_STRATEGY_ID
        assert "NOT relaxed automatically" in result["reason"]

    def test_active_strategy_is_excluded(self) -> None:
        result = select_replacement(
            {TREND_STRATEGY: passing_validation()},
            exclude=(TREND_STRATEGY,),
        )

        assert result["selected"] is None

    def test_rejected_strategy_needs_a_new_sample(self) -> None:
        rejected = {TREND_STRATEGY: {"sample_id": "SAMPLE_A"}}

        same = select_replacement(
            {TREND_STRATEGY: passing_validation(sample_id="SAMPLE_A")},
            rejected=rejected,
        )

        assert same["selected"] is None

        fresh = select_replacement(
            {TREND_STRATEGY: passing_validation(sample_id="SAMPLE_B")},
            rejected=rejected,
        )

        assert fresh["selected"] == TREND_STRATEGY


# =====================================================================
# 5. Переключение и cooldown
# =====================================================================


class TestSwitchPlanning:

    def paused_evaluation(self) -> dict:
        return evaluate_active_strategy(TREND_STRATEGY, losing_series(60))

    def test_switch_is_refused_while_a_position_is_open(self) -> None:
        plan = plan_switch(
            active_strategy_id=TREND_STRATEGY,
            evaluation=self.paused_evaluation(),
            validations={"ORB_0930_RETEST": passing_validation()},
            has_open_position=True,
            now_utc="2026-07-29T10:00:00+00:00",
        )

        assert plan["switch_allowed"] is False
        assert plan["action"] == "WAIT_FOR_FLAT"

    def test_switch_is_refused_during_cooldown(self) -> None:
        plan = plan_switch(
            active_strategy_id=TREND_STRATEGY,
            evaluation=self.paused_evaluation(),
            validations={"ORB_0930_RETEST": passing_validation()},
            has_open_position=False,
            now_utc="2026-07-29T10:00:00+00:00",
            last_switch_at_utc="2026-07-27T10:00:00+00:00",
        )

        assert plan["switch_allowed"] is False
        assert plan["action"] == "COOLDOWN"

    def test_switch_is_allowed_after_cooldown_when_flat(self) -> None:
        plan = plan_switch(
            active_strategy_id=TREND_STRATEGY,
            evaluation=self.paused_evaluation(),
            validations={"ORB_0930_RETEST": passing_validation()},
            has_open_position=False,
            now_utc="2026-07-29T10:00:00+00:00",
            last_switch_at_utc="2026-07-01T10:00:00+00:00",
        )

        assert plan["switch_allowed"] is True
        assert plan["action"] == "SWITCH"
        assert plan["to_strategy_id"] == "ORB_0930_RETEST"

    def test_healthy_strategy_is_not_switched(self) -> None:
        healthy = evaluate_active_strategy(TREND_STRATEGY, losing_series(5))

        plan = plan_switch(
            active_strategy_id=TREND_STRATEGY,
            evaluation=healthy,
            validations={"ORB_0930_RETEST": passing_validation()},
            has_open_position=False,
            now_utc="2026-07-29T10:00:00+00:00",
        )

        assert plan["switch_allowed"] is False
        assert plan["action"] == "HOLD"

    def test_live_mode_is_an_emergency_not_a_switch(self) -> None:
        plan = plan_switch(
            active_strategy_id=TREND_STRATEGY,
            evaluation=self.paused_evaluation(),
            validations={"ORB_0930_RETEST": passing_validation()},
            has_open_position=False,
            now_utc="2026-07-29T10:00:00+00:00",
            paper_mode=False,
        )

        assert plan["action"] == EMERGENCY_STOP
        assert plan["switch_allowed"] is False

    def test_emergency_blocks_any_switch(self) -> None:
        evaluation = evaluate_active_strategy(
            TREND_STRATEGY,
            losing_series(60)
            + [observation(utc="2026-07-09T10:00:00+00:00", real_order_sent=True)],
        )

        plan = plan_switch(
            active_strategy_id=TREND_STRATEGY,
            evaluation=evaluation,
            validations={"ORB_0930_RETEST": passing_validation()},
            has_open_position=False,
            now_utc="2026-07-29T10:00:00+00:00",
        )

        assert plan["action"] == EMERGENCY_STOP
        assert plan["switch_allowed"] is False

    def test_no_valid_candidate_keeps_no_trade(self) -> None:
        plan = plan_switch(
            active_strategy_id=TREND_STRATEGY,
            evaluation=self.paused_evaluation(),
            validations={
                "ORB_0930_RETEST": passing_validation(oos_profit_factor=1.0)
            },
            has_open_position=False,
            now_utc="2026-07-29T10:00:00+00:00",
        )

        assert plan["action"] == NO_VALID_STRATEGY
        assert plan["fallback_strategy_id"] == DEFAULT_STRATEGY_ID


class TestChangeReport:

    def test_report_records_the_exact_reason(self) -> None:
        evaluation = evaluate_active_strategy(TREND_STRATEGY, losing_series(60))

        plan = plan_switch(
            active_strategy_id=TREND_STRATEGY,
            evaluation=evaluation,
            validations={"ORB_0930_RETEST": passing_validation()},
            has_open_position=False,
            now_utc="2026-07-29T10:00:00+00:00",
            last_switch_at_utc="2026-07-01T10:00:00+00:00",
        )

        report = build_change_report(
            plan=plan,
            evaluation=evaluation,
            now_utc="2026-07-29T10:00:00+00:00",
            validations={"ORB_0930_RETEST": passing_validation()},
        )

        assert report["action"] == "SWITCH"
        assert report["from_strategy"]["strategy_id"] == TREND_STRATEGY
        assert report["from_strategy"]["pause_reasons"]
        assert report["to_strategy"]["strategy_id"] == "ORB_0930_RETEST"
        assert report["to_strategy"]["gates"]["passed"] is True
        assert report["thresholds"]["cooldown"]["switch_cooldown_days"] == 7
        assert report["real_orders_enabled"] is False
        assert "never deleted" in report["note"]

    def test_report_is_written_even_when_no_switch_happens(self) -> None:
        """«Почему НЕ переключились» — тоже результат работы супервизора."""
        evaluation = evaluate_active_strategy(TREND_STRATEGY, losing_series(5))

        plan = plan_switch(
            active_strategy_id=TREND_STRATEGY,
            evaluation=evaluation,
            validations={},
            has_open_position=False,
            now_utc="2026-07-29T10:00:00+00:00",
        )

        report = build_change_report(
            plan=plan,
            evaluation=evaluation,
            now_utc="2026-07-29T10:00:00+00:00",
        )

        assert report["switch_allowed"] is False
        assert report["action"] == "HOLD"
        assert report["to_strategy"] is None
        assert report["reason"]


# =====================================================================
# 6. Champion / Challenger
# =====================================================================


class TestChampionChallenger:

    def test_challengers_never_open_positions(self) -> None:
        comparison = compare_champion_challengers(
            champion_id=TREND_STRATEGY,
            champion_decisions=[
                observation(utc="2026-07-01T10:00:00+00:00", decision="TRADE"),
            ],
            challenger_decisions={
                "ORB_0930_RETEST": [
                    observation(
                        utc="2026-07-01T10:00:00+00:00", decision="NO_TRADE"
                    ),
                ]
            },
        )

        challenger = comparison["challengers"]["ORB_0930_RETEST"]

        assert challenger["shadow_mode"] is True
        assert challenger["paper_positions_opened"] == 0

    def test_decisions_are_compared_on_the_same_candles(self) -> None:
        comparison = compare_champion_challengers(
            champion_id=TREND_STRATEGY,
            champion_decisions=[
                observation(utc="2026-07-01T10:00:00+00:00", decision="TRADE"),
                observation(utc="2026-07-01T10:05:00+00:00", decision="NO_TRADE"),
            ],
            challenger_decisions={
                "ORB_0930_RETEST": [
                    observation(
                        utc="2026-07-01T10:00:00+00:00", decision="TRADE"
                    ),
                    observation(
                        utc="2026-07-01T10:05:00+00:00", decision="TRADE"
                    ),
                    # Свеча, которой у champion нет — не учитывается.
                    observation(
                        utc="2026-07-02T10:00:00+00:00", decision="TRADE"
                    ),
                ]
            },
        )

        challenger = comparison["challengers"]["ORB_0930_RETEST"]

        assert challenger["matched_candles"] == 2
        assert challenger["agreement_count"] == 1
        assert challenger["challenger_trades_champion_skips"] == 1


# =====================================================================
# 7. Валидация: walk-forward, holdout, утечка
# =====================================================================


class TestValidation:

    def test_holdout_is_the_last_chronological_slice(self) -> None:
        sample = trades([1.0] * 10)

        in_sample, out_of_sample = split_holdout(sample, holdout_fraction=0.3)

        assert len(in_sample) == 7
        assert len(out_of_sample) == 3
        # Holdout строго ПОЗЖЕ обучения.
        assert in_sample[-1].closed_at_utc < out_of_sample[0].closed_at_utc

    def test_unsorted_input_is_ordered_chronologically(self) -> None:
        sample = trades([1.0] * 6)
        shuffled = [sample[3], sample[0], sample[5], sample[1], sample[4], sample[2]]

        in_sample, out_of_sample = split_holdout(shuffled, holdout_fraction=0.5)

        assert [t.closed_at_utc for t in in_sample] == sorted(
            t.closed_at_utc for t in in_sample
        )
        assert in_sample[-1].closed_at_utc < out_of_sample[0].closed_at_utc

    def test_sample_id_is_required(self) -> None:
        with pytest.raises(ValidationError, match="sample_id is required"):
            validate_candidate(TREND_STRATEGY, trades([1.0] * 50), sample_id="")

    def test_validation_produces_gate_ready_output(self) -> None:
        # Устойчиво прибыльная серия.
        pattern = [1.5, -1.0, 1.5, 1.5, -1.0, 2.0] * 12

        result = validate_candidate(
            TREND_STRATEGY,
            trades(pattern),
            sample_id="SAMPLE_2026_07",
        )

        assert result["look_ahead_leakage"] is False
        assert result["walk_forward_passed"] is True
        assert result["oos_trades"] > 0
        assert result["robustness_ratio"] is not None
        assert result["holdout_start_utc"] is not None

        # Результат напрямую пригоден для гейтов.
        assert "passed" in promotion_gates(result)

    def test_leakage_is_detected_when_train_overlaps_test(self) -> None:
        leaking = [
            {
                "window": {
                    "index": 0,
                    "train_start": "2026-06-01T00:00:00+00:00",
                    "train_end": "2026-06-20T00:00:00+00:00",
                    "test_start": "2026-06-10T00:00:00+00:00",
                    "test_end": "2026-06-30T00:00:00+00:00",
                }
            }
        ]

        assert detect_look_ahead_leakage(leaking) is True

    def test_windows_touching_the_holdout_are_leakage(self) -> None:
        window = [
            {
                "window": {
                    "index": 0,
                    "train_start": "2026-06-01T00:00:00+00:00",
                    "train_end": "2026-06-10T00:00:00+00:00",
                    "test_start": "2026-06-11T00:00:00+00:00",
                    "test_end": "2026-07-05T00:00:00+00:00",
                }
            }
        ]

        assert (
            detect_look_ahead_leakage(
                window, holdout_start="2026-07-01T00:00:00+00:00"
            )
            is True
        )

    def test_robustness_uses_share_of_profitable_windows(self) -> None:
        """
        Одно выдающееся окно из четырёх не делает стратегию устойчивой.
        """
        mostly_losing = [-1.0] * 30 + [50.0] * 10

        result = validate_candidate(
            TREND_STRATEGY,
            trades(mostly_losing),
            sample_id="SAMPLE_X",
        )

        assert result["robustness_ratio"] is not None
        assert result["robustness_ratio"] < 0.60
        assert promotion_gates(result)["passed"] is False


# =====================================================================
# 8. Сводный статус
# =====================================================================


class TestSupervisorStatus:

    def test_status_reports_guarantees_and_registry(self) -> None:
        status = supervisor_status(
            champion_id=DEFAULT_STRATEGY_ID,
            observations=losing_series(5),
        )

        assert status["mode"] == "PAPER"
        assert status["real_orders_enabled"] is False
        assert status["automatic_switching"] == "PAPER_ONLY"
        assert len(status["registry"]) == len(STRATEGY_REGISTRY)
        assert any(
            "may only select from the code-defined registry" in item
            for item in status["guarantees"]
        )

    def test_champion_is_marked_paper_active(self) -> None:
        status = supervisor_status(
            champion_id=TREND_STRATEGY,
            observations=[],
        )

        entry = next(
            item
            for item in status["registry"]
            if item["strategy_id"] == TREND_STRATEGY
        )

        assert entry["status"] == "PAPER_ACTIVE"
