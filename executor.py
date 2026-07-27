"""
executor.py - Execution Engine.
Regex extract JSON từ raw text, validate bằng Pydantic, thực thi lệnh trade.
"""
import re
from typing import Literal, Union

from pydantic import BaseModel, Field, ValidationError, field_validator, TypeAdapter, model_validator


class TradeOrder(BaseModel):
    """Pydantic schema cho lệnh giao dịch.

    Strict typing để reject mọi hallucination từ chatbot.
    """

    ticker: str = Field(description="Mã cổ phiếu")
    action: Literal["BUY", "SELL", "HOLD", "CUT_LOSS"] = Field(
        description="Hành động"
    )
    volume: Union[int, Literal["ALL"]] = Field(
        description="Số lượng hoặc ALL"
    )
    price: float = Field(description="Giá đặt lệnh")
    reason: str = Field(description="Lý do thực thi dựa trên vi chu kỳ hoặc TA")

    @field_validator("ticker")
    @classmethod
    def validate_ticker(cls, v):
        """Mã cổ phiếu phải viết hoa và dài từ 3-5 ký tự."""
        v = str(v).strip().upper()
        if not re.match(r"^[A-Z0-9]{3,5}$", v):
            raise ValueError("Mã cổ phiếu phải viết hoa và dài từ 3 đến 5 ký tự.")
        return v

    @model_validator(mode="after")
    def validate_volume_by_action(self) -> "TradeOrder":
        """Kiểm tra tính hợp lệ của volume dựa trên action."""
        action = self.action
        volume = self.volume

        if action == "HOLD":
            # Đối với HOLD, volume có thể là 0 hoặc số dương, nhưng không được âm
            if isinstance(volume, int) and volume < 0:
                raise ValueError("Volume của lệnh HOLD không được âm.")
        else:
            # Đối với BUY, SELL, CUT_LOSS
            if isinstance(volume, int) and volume <= 0:
                raise ValueError(f"Volume của lệnh {action} phải lớn hơn 0.")
            if volume == "ALL" and action == "BUY":
                raise ValueError("Không thể đặt lệnh BUY với volume='ALL'.")
        return self


def extract_json(raw_text: str) -> str:
    """Bóc tách JSON object hoặc JSON array từ raw text bằng regex.

    Args:
        raw_text: Text thô chứa JSON (có thể kèm markdown, giải thích...).

    Returns:
        Chuỗi JSON đã extract.

    Raises:
        ValueError: Khi không tìm thấy JSON object hoặc array trong text.
    """
    # Tìm mảng [ ... ] trước để xử lý danh sách lệnh
    match_array = re.search(r"\[.*?\]", raw_text, re.DOTALL)
    if match_array:
        return match_array.group(0)

    # Nếu không có mảng, tìm đối tượng đơn { ... }
    match_obj = re.search(r"\{.*?\}", raw_text, re.DOTALL)
    if match_obj:
        return match_obj.group(0)

    raise ValueError(
        "Không tìm thấy JSON object hoặc JSON array trong text. "
        "Hãy đảm bảo response từ AI chứa JSON hợp lệ với dấu { } hoặc [ ]."
    )


def validate_order(json_str: str) -> list[TradeOrder]:
    """Parse JSON string và validate bằng Pydantic schema.

    Hỗ trợ cả JSON object đơn lẻ hoặc một danh sách (JSON Array) các JSON object.
    Trả về một list các TradeOrder.

    Args:
        json_str: Chuỗi JSON đã extract.

    Returns:
        List of TradeOrder instances đã validate.

    Raises:
        ValidationError: Khi JSON không khớp schema (field thiếu, type sai...).
        ValueError: Khi JSON string không parse được.
    """
    import json

    try:
        data = json.loads(json_str)
    except json.JSONDecodeError as e:
        raise ValueError(f"JSON không hợp lệ: {e}") from e

    if isinstance(data, list):
        adapter = TypeAdapter(list[TradeOrder])
        return adapter.validate_python(data)
    elif isinstance(data, dict):
        adapter = TypeAdapter(TradeOrder)
        return [adapter.validate_python(data)]
    else:
        raise ValueError("Dữ liệu JSON sau khi parse không phải là object hoặc array.")


def execute_order(order: TradeOrder, portfolio: dict) -> dict:
    """Thực thi lệnh trade lên portfolio.

    Logic:
    - SELL / CUT_LOSS: Kiểm tra holdings, xử lý volume="ALL",
      cộng tiền vào cash, xóa key nếu hết.
    - BUY: Kiểm tra đủ cash, trừ cash, update holdings với avg_price tính lại.
    - HOLD: Không thay đổi portfolio, chỉ log.

    Args:
        order: TradeOrder đã validate.
        portfolio: dict portfolio hiện tại (sẽ bị mutate).

    Returns:
        dict portfolio đã update.

    Raises:
        ValueError: Khi không đủ điều kiện thực thi (thiếu cash, không có holdings...).
    """
    ticker = order.ticker
    action = order.action
    volume = order.volume
    price = order.price
    holdings = portfolio.setdefault("holdings", {})

    if action in ("SELL", "CUT_LOSS"):
        if ticker not in holdings:
            raise ValueError(
                f"Không thể {action}: Không có vị thế {ticker} trong danh mục."
            )

        holding = holdings[ticker]
        actual_volume = holding["quantity"] if volume == "ALL" else volume

        if isinstance(volume, int) and volume > holding["quantity"]:
            raise ValueError(
                f"Không thể {action}: Yêu cầu bán {volume} cp {ticker} "
                f"nhưng chỉ đang giữ {holding['quantity']} cp."
            )

        sell_value = actual_volume * price * 1000  # Giá VN tính theo nghìn VND
        portfolio["cash"] += sell_value
        holding["quantity"] -= actual_volume

        if holding["quantity"] <= 0:
            del holdings[ticker]

    elif action == "BUY":
        total_cost = volume * price * 1000  # Giá VN tính theo nghìn VND

        if volume == "ALL":
            raise ValueError(
                "Không thể BUY với volume='ALL'. Cần chỉ định số lượng cụ thể."
            )

        if total_cost > portfolio["cash"]:
            raise ValueError(
                f"Không đủ tiền mặt: Cần {total_cost:,.0f} VND "
                f"nhưng chỉ còn {portfolio['cash']:,.0f} VND."
            )

        portfolio["cash"] -= total_cost

        if ticker in holdings:
            old = holdings[ticker]
            old_total = old["quantity"] * old["avg_price"]
            new_total = volume * price
            new_qty = old["quantity"] + volume
            holdings[ticker] = {
                "quantity": new_qty,
                "avg_price": round((old_total + new_total) / new_qty, 2),
            }
        else:
            holdings[ticker] = {
                "quantity": volume,
                "avg_price": price,
            }

    # HOLD: không làm gì, chỉ log ở tầng app.py

    return portfolio
