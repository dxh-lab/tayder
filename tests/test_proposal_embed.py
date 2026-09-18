from tayder.notify.discord_bot import proposal_embed, risk_summary
from tayder.models import Proposal, Side


def test_risk_summary_twenty_percent():
    s = risk_summary(2.0, 77892.70, 10.0)
    assert s == "Proposing $2.00 @ 77892.70. Strategy equity: $10.00 (stake 20%)"


def test_risk_summary_zero_balance():
    s = risk_summary(2.0, 100.0, 0.0)
    assert s == "Proposing $2.00 @ 100.00. Strategy equity: $0.00 (stake 0%)"


def test_proposal_embed_includes_cost_warning():
    p = Proposal(
        "BTC-USD", Side.BUY, 5, "mean_reversion_buy z=-2.00 sma=100", 99,
        meta={
            "estimated_edge_bps": 40,
            "executable_edge_bps": 40,
            "required_edge_bps": 160,
            "cost_warning": True,
        },
    )
    emb = proposal_embed(p, "paper", 10.0)
    fields = {f.name: f.value for f in emb.fields}
    assert "fees dominate" in fields["Cost screen"]
    assert "40 bps" in fields["Cost screen"]
    assert "160 bps" in fields["Cost screen"]
