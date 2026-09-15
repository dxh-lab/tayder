from tayder.notify.discord_bot import risk_summary


def test_risk_summary_twenty_percent():
    s = risk_summary(2.0, 77892.70, 10.0)
    assert s == "Proposing $2.00 @ 77892.70. Strategy equity: $10.00 (stake 20%)"


def test_risk_summary_zero_balance():
    s = risk_summary(2.0, 100.0, 0.0)
    assert s == "Proposing $2.00 @ 100.00. Strategy equity: $0.00 (stake 0%)"
