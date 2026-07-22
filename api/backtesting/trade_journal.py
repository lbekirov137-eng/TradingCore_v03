class TradeJournal:

    def __init__(self):
        self.trades = []

    def add_trade(self, trade):

        self.trades.append(trade)

    def statistics(self):

        total = len(self.trades)

        wins = sum(
            1
            for trade in self.trades
            if trade.get("result") == "WIN"
        )

        losses = total - wins

        win_rate = 0.0

        if total > 0:
            win_rate = wins / total * 100

        return {
            "total_trades": total,
            "wins": wins,
            "losses": losses,
            "win_rate": round(win_rate, 2),
        }