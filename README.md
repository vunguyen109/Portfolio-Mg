# 📊 Portfolio Manager — Streamlit Web UI

Giao diện quản lý danh mục cổ phiếu Việt Nam, tích hợp AI chatbot để đưa ra quyết định giao dịch.  
Hệ thống validate nghiêm ngặt bằng Pydantic, loại bỏ hallucination từ AI trước khi thực thi lệnh.

---

## ⚡ Cài đặt nhanh

```bash
# Di chuyển vào project
cd d:\project\PortfolioMg

# Tạo virtual environment
python -m venv venv

# Kích hoạt venv
# Windows (PowerShell):
.\venv\Scripts\Activate.ps1
# Windows (CMD):
.\venv\Scripts\activate.bat

# Cài dependencies
pip install -r requirements.txt

# Chạy app
streamlit run app.py
```

App sẽ mở tại **http://localhost:8501**.

---

## 🏗️ Cấu trúc Project

```
PortfolioMg/
├── app.py              # Streamlit UI chính (Sidebar + 2 cột)
├── builder.py          # Context Builder — sinh prompt cho AI chatbot
├── executor.py         # Execution Engine — validate & thực thi lệnh
├── db.py               # SQLite audit log
├── portfolio.json      # Dữ liệu danh mục (cash + holdings)
├── audit_log.db        # SQLite DB — tự tạo khi chạy lần đầu
├── requirements.txt    # Dependencies
└── README.md
```

---

## 📖 Hướng dẫn sử dụng

### Bước 1 — Generate Prompt (Cột trái)

1. Nhấn nút **"Generate Prompt"**.
2. Hệ thống sẽ đọc danh mục hiện tại từ `portfolio.json`, kết hợp với dữ liệu thị trường mock EOD (5 mã: SSI, FPT, HPG, VCB, GAS) để sinh ra một prompt chuẩn.
3. Copy toàn bộ prompt hiển thị trong code block và gửi cho AI chatbot (ChatGPT, Gemini, Claude...).

### Bước 2 — Paste & Execute (Cột phải)

1. Copy response JSON từ AI chatbot.
2. Paste vào ô **"Paste Chatbot JSON here"**. Không cần bóc tách JSON — hệ thống tự extract bằng regex.
3. Nhấn **"Execute Order"**.

### Kết quả

| Trường hợp | Hiển thị |
|------------|----------|
| ✅ JSON hợp lệ, đủ điều kiện | `st.success` — cập nhật danh mục + ghi audit log |
| ❌ Không tìm thấy JSON trong text | `st.error` — yêu cầu kiểm tra response |
| ❌ JSON sai schema (Pydantic) | `st.error` — chi tiết từng field lỗi |
| ❌ Không đủ tiền / không có vị thế | `st.error` — mô tả cụ thể thiếu gì |

### Sidebar

- **Danh mục hiện tại**: Tự động cập nhật sau mỗi lệnh thành công.
- **Audit Log**: Hiển thị 10 lệnh gần nhất đã thực thi.

---

## 📋 JSON Format yêu cầu từ AI

```json
{
  "ticker": "HPG",
  "action": "SELL",
  "volume": 500,
  "price": 30.1,
  "reason": "RSI thấp, cắt lỗ vi chu kỳ phục hồi"
}
```

| Field    | Type                                      | Mô tả                         |
|----------|-------------------------------------------|--------------------------------|
| `ticker` | `"SSI"` \| `"FPT"` \| `"HPG"` \| `"VCB"` \| `"GAS"` | Mã cổ phiếu (chỉ 5 mã hợp lệ) |
| `action` | `"BUY"` \| `"SELL"` \| `"HOLD"` \| `"CUT_LOSS"`       | Loại lệnh                     |
| `volume` | `int > 0` hoặc `"ALL"`                   | Số lượng cổ phiếu             |
| `price`  | `float`                                   | Giá đặt lệnh (nghìn VND)     |
| `reason` | `string`                                  | Lý do thực thi                |

> **Lưu ý**: Giá tính theo đơn vị nghìn VND (ví dụ: `30.1` = 30,100 VND/cp). Tổng giá trị lệnh = `volume × price × 1000`.

---

## 🔧 Chi tiết kỹ thuật

### Session State
- `st.session_state.prompt` — Lưu prompt đã sinh, không bị mất khi rerun.
- `st.session_state.exec_message` — Lưu kết quả lệnh gần nhất qua rerun.

### Caching
- `@st.cache_data` trên `generate_mock_market_data()` — dữ liệu EOD chỉ sinh 1 lần/session.

### Error Handling (3 tầng)
1. **Regex extract** — Không tìm thấy `{...}` trong text.
2. **Pydantic validation** — Field thiếu, type sai, ticker không hợp lệ, volume ≤ 0.
3. **Business logic** — Không đủ cash để BUY, không có vị thế để SELL.

### Trade Logic
- **BUY**: Trừ cash, thêm/update holdings (tính lại `avg_price` trung bình).
- **SELL / CUT_LOSS**: Cộng cash, giảm quantity. Nếu hết → xóa khỏi holdings.
- **HOLD**: Không thay đổi danh mục, chỉ ghi log.
- **volume = "ALL"**: Bán toàn bộ số lượng đang giữ.

---

## 📌 Lưu ý

- Dữ liệu thị trường là **mock data cố định**, không lấy real-time.
- `portfolio.json` được ghi trực tiếp — backup nếu cần.
- `audit_log.db` tự tạo lần chạy đầu tiên, không cần setup riêng.
