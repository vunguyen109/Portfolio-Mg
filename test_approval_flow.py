"""
test_approval_flow.py - Unit test cho Sell Approval Workflow.
"""
import copy
import json
from executor import validate_order, execute_order, is_sell_order, TradeOrder


def test_is_sell_order():
    buy_order = TradeOrder(ticker="HPG", action="BUY", volume=1000, price=28.5, reason="Buy dip")
    hold_order = TradeOrder(ticker="VNM", action="HOLD", volume=0, price=65.0, reason="Hold position")
    sell_order = TradeOrder(ticker="FPT", action="SELL", volume=500, price=120.0, reason="Take profit")
    cut_loss_order = TradeOrder(ticker="MWG", action="CUT_LOSS", volume="ALL", price=45.0, reason="Stop loss triggered")

    assert is_sell_order(buy_order) is False
    assert is_sell_order(hold_order) is False
    assert is_sell_order(sell_order) is True
    assert is_sell_order(cut_loss_order) is True


def test_order_split_and_approval_flow():
    raw_json = json.dumps([
        {"ticker": "HPG", "action": "BUY", "volume": 100, "price": 28.0, "reason": "Tích lũy"},
        {"ticker": "VNM", "action": "SELL", "volume": 200, "price": 68.0, "reason": "Chốt lời"},
        {"ticker": "MSN", "action": "CUT_LOSS", "volume": 100, "price": 70.0, "reason": "Cắt lỗ"}
    ])

    orders = validate_order(raw_json)
    assert len(orders) == 3

    auto_exec_orders = [o for o in orders if not is_sell_order(o)]
    pending_sells = [o for o in orders if is_sell_order(o)]

    assert len(auto_exec_orders) == 1
    assert auto_exec_orders[0].ticker == "HPG"

    assert len(pending_sells) == 2
    assert pending_sells[0].ticker == "VNM"
    assert pending_sells[1].ticker == "MSN"

    # Mock initial portfolio
    initial_portfolio = {
        "cash": 100000000.0,
        "holdings": {
            "VNM": {"quantity": 500, "avg_price": 65.0},
            "MSN": {"quantity": 200, "avg_price": 75.0}
        }
    }

    portfolio = copy.deepcopy(initial_portfolio)

    # 1. Execute auto_exec_orders (BUY HPG)
    portfolio = execute_order(auto_exec_orders[0], portfolio)

    # Check that portfolio updated for BUY HPG (cash reduced by 100 * 28.0 * 1000 = 2,800,000 VND)
    assert portfolio["cash"] == 100000000.0 - 2800000.0
    assert "HPG" in portfolio["holdings"]
    assert portfolio["holdings"]["HPG"]["quantity"] == 100

    # 2. Portfolio for VNM and MSN must remain UNCHANGED before approval
    assert portfolio["holdings"]["VNM"]["quantity"] == 500
    assert portfolio["holdings"]["MSN"]["quantity"] == 200

    # 3. Simulate User Approving SELL VNM
    portfolio = execute_order(pending_sells[0], portfolio)

    # Cash should increase by 200 * 68.0 * 1000 = 13,600,000 VND
    expected_cash = (100000000.0 - 2800000.0) + (200 * 68.0 * 1000)
    assert portfolio["cash"] == expected_cash
    assert portfolio["holdings"]["VNM"]["quantity"] == 300

    # 4. Simulate User Rejecting CUT_LOSS MSN (pending_sells[1]) -> do nothing
    # MSN holding should remain 200
    assert portfolio["holdings"]["MSN"]["quantity"] == 200

    print("[SUCCESS] All unit tests passed!")


if __name__ == "__main__":
    test_is_sell_order()
    test_order_split_and_approval_flow()
