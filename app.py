"""
app.py - Streamlit Web UI cho Portfolio Management.
Layout: Sidebar + 2 tab (Trading Desk | Analyst Agent).
Tập trung: Data Flow, Session State, Error Handling.
"""
import json
import copy

import streamlit as st
from pydantic import ValidationError

from builder import load_portfolio, save_portfolio, build_prompt, load_prompt_config
from executor import extract_json, validate_order, execute_order
from db import init_db, insert_log, get_logs
from analyst_agent import AnalystAgent

# ── Khởi tạo ──────────────────────────────────────────────────────
init_db()

st.set_page_config(
    page_title="Portfolio Manager",
    page_icon="📊",
    layout="wide",
)

# ── Session State defaults ─────────────────────────────────────────
if "prompt" not in st.session_state:
    st.session_state.prompt = ""

if "exec_message" not in st.session_state:
    st.session_state.exec_message = None  # (type, message) tuple hoặc None

if "analysis_result" not in st.session_state:
    st.session_state.analysis_result = None

if "analysis_msg" not in st.session_state:
    st.session_state.analysis_msg = None

# ── Sidebar: Portfolio Overview + Audit Logs ───────────────────────
with st.sidebar:
    st.header("📁 Danh mục hiện tại")

    portfolio = load_portfolio()

    st.metric("Tiền mặt", f"{portfolio['cash']:,.0f} VND")

    holdings = portfolio.get("holdings", {})
    if holdings:
        holdings_rows = [
            {
                "Mã": ticker,
                "SL": info["quantity"],
                "Giá TB": info["avg_price"],
            }
            for ticker, info in holdings.items()
        ]
        st.dataframe(holdings_rows, use_container_width=True, hide_index=True)
    else:
        st.info("Chưa có vị thế nào.")

    st.divider()
    st.header("📋 Audit Log gần nhất")

    logs = get_logs(limit=10)
    if logs:
        log_rows = [
            {
                "Thời gian": log["timestamp"],
                "Mã": log["ticker"],
                "Lệnh": log["action"],
                "KL": log["volume"],
                "Giá": log["price"],
            }
            for log in logs
        ]
        st.dataframe(log_rows, use_container_width=True, hide_index=True)
    else:
        st.caption("Chưa có lệnh nào được thực thi.")

# ── Main ──────────────────────────────────────────────────────────
st.title("📊 Portfolio Manager")

tab_trading, tab_analyst = st.tabs([
    "💼 Giao dịch (Trading Desk)",
    "🔬 Analyst Agent (Feedback Loop)"
])

# ── Tab 1: Trading Desk ───────────────────────────────────────────
with tab_trading:
    col_builder, col_executor = st.columns(2, gap="large")

    # ── Cột 1: Context Builder ────────────────────────────────────────
    with col_builder:
        st.subheader("🔧 Context Builder")
        st.caption("Sinh prompt từ danh mục + dữ liệu thị trường mock EOD.")

        if st.button("Generate Prompt", type="primary", use_container_width=True):
            portfolio_snapshot = load_portfolio()
            st.session_state.prompt = build_prompt(portfolio_snapshot)

        if st.session_state.prompt:
            st.code(st.session_state.prompt, language="markdown")
        else:
            st.info("Nhấn **Generate Prompt** để tạo context gửi cho AI chatbot.")

    # ── Cột 2: Execution Engine ───────────────────────────────────────
    with col_executor:
        st.subheader("⚡ Execution Engine")
        st.caption("Paste JSON response từ AI chatbot, validate và thực thi lệnh.")

        raw_input = st.text_area(
            "Paste Chatbot JSON here",
            height=200,
            placeholder='{"ticker": "HPG", "action": "SELL", "volume": 500, "price": 30.1, "reason": "..."}',
        )

        if st.button("Execute Order", type="primary", use_container_width=True):
            if not raw_input.strip():
                st.warning("Vui lòng paste JSON response trước khi execute.")
            else:
                # Pipeline: extract → validate → execute
                try:
                    # Bước 1: Regex extract JSON
                    json_str = extract_json(raw_input)

                    # Bước 2: Pydantic validate (Trả về list các TradeOrder)
                    orders = validate_order(json_str)

                    # Bước 3: Load portfolio mới nhất, deep copy để tránh side-effect (Atomic)
                    current_portfolio = load_portfolio()
                    updated_portfolio = copy.deepcopy(current_portfolio)

                    for order in orders:
                        updated_portfolio = execute_order(order, updated_portfolio)

                    # Bước 4: Persist - ghi portfolio + audit log
                    save_portfolio(updated_portfolio)
                    for order in orders:
                        insert_log(order.model_dump())

                    # Bước 5: Feedback tổng hợp
                    msg_lines = []
                    for order in orders:
                        msg_lines.append(
                            f"- **{order.action} {order.volume} {order.ticker}** @ {order.price}  \n"
                            f"  *Lý do:* {order.reason}"
                        )
                    st.session_state.exec_message = (
                        "success",
                        "✅ **Đã thực thi thành công danh sách lệnh:**\n\n" + "\n\n".join(msg_lines),
                    )
                    st.rerun()

                except ValueError as e:
                    st.error(f"❌ **Lỗi dữ liệu:** {e}")

                except ValidationError as e:
                    st.error("❌ **Pydantic Validation Error** — AI đã hallucinate:")
                    for err in e.errors():
                        field = " → ".join(str(loc) for loc in err["loc"])
                        st.error(f"  • **{field}**: {err['msg']} (type: {err['type']})")

                except Exception as e:
                    st.error(f"❌ **Lỗi không xác định:** {type(e).__name__}: {e}")

        # Hiển thị kết quả execution từ session state (persist qua rerun)
        if st.session_state.exec_message:
            msg_type, msg_content = st.session_state.exec_message
            if msg_type == "success":
                st.success(msg_content)
            elif msg_type == "error":
                st.error(msg_content)

# ── Tab 2: Analyst Agent ──────────────────────────────────────────
with tab_analyst:
    st.subheader("🔬 Analyst Agent & Feedback Loop")
    st.caption("Định kỳ phân tích hiệu suất thực tế vs VNINDEX, tự động tinh chỉnh cấu hình System Prompt.")

    # Cấu hình hiện tại
    cfg = load_prompt_config()
    st.write("### ⚙️ Cấu hình Prompt hiện tại")
    
    col_cfg1, col_cfg2, col_cfg3 = st.columns(3)
    with col_cfg1:
        st.metric("RSI Oversold (BUY)", cfg.get("rsi_oversold_threshold", 30))
        st.metric("RSI Overbought (SELL)", cfg.get("rsi_overbought_threshold", 70))
    with col_cfg2:
        st.metric("Tỷ lệ tiền mặt tối đa", f"{cfg.get('max_cash_ratio', 0.4) * 100:.0f}%")
        st.metric("Tỷ trọng vị thế tối đa", f"{cfg.get('max_single_position_ratio', 0.3) * 100:.0f}%")
    with col_cfg3:
        st.metric("Chu kỳ Bias", cfg.get("cycle_bias", "neutral").upper())
        st.metric("Khung phân tích", cfg.get("analysis_framework", "vi_chu_ky").upper())

    if cfg.get("extra_instructions"):
        st.info("**Quy tắc bổ sung hiện tại:**\n" + "\n".join(f"- {inst}" for inst in cfg["extra_instructions"]))

    st.divider()

    st.write("### 🚀 Chạy phân tích & Tối ưu hóa")
    col_btn1, col_btn2 = st.columns([1, 3])
    with col_btn1:
        lookback = st.selectbox("Khoảng thời gian nhìn lại (ngày)", [7, 14, 30, 90], index=2)
    with col_btn2:
        st.write(" ") # Padding
        st.write(" ") # Padding
        run_analysis = st.button("Run Performance Analysis", type="primary", use_container_width=True)

    if run_analysis:
        with st.spinner("Analyst Agent đang tính toán hiệu suất danh mục, lấy dữ liệu VNINDEX và chạy chẩn đoán..."):
            try:
                agent = AnalystAgent(lookback_days=lookback)
                result = agent.run()
                st.session_state.analysis_result = result
                st.session_state.analysis_msg = ("success", "Phân tích hoàn tất thành công!")
                st.rerun()
            except Exception as e:
                st.session_state.analysis_msg = ("error", f"Lỗi khi chạy phân tích: {e}")
                st.session_state.analysis_result = None
                st.rerun()

    if st.session_state.analysis_msg:
        msg_type, msg_text = st.session_state.analysis_msg
        if msg_type == "success":
            st.toast(msg_text, icon="✅")
        else:
            st.error(msg_text)
        # Clear message state
        st.session_state.analysis_msg = None

    # Hiển thị kết quả nếu có
    if st.session_state.analysis_result:
        res = st.session_state.analysis_result
        report = res.report

        st.write("### 📊 Kết quả phân tích hiệu suất")
        
        # Metrics tổng quan
        col_m1, col_m2, col_m3, col_m4 = st.columns(4)
        with col_m1:
            st.metric("Lợi nhuận danh mục", f"{report.portfolio_return_pct:+.2f}%")
        with col_m2:
            st.metric("VNINDEX cùng kỳ", f"{report.vnindex_return_pct:+.2f}%")
        with col_m3:
            st.metric("Alpha (Portfolio vs VNINDEX)", f"{report.alpha_pct:+.2f}%")
        with col_m4:
            st.metric("Độ chính xác lệnh", f"{report.accuracy_pct:.1f}%", f"{report.correct_trades}/{report.total_trades} lệnh")

        # Chi tiết lệnh
        if report.ticker_performances:
            st.write("#### Chi tiết các giao dịch thực tế:")
            perf_rows = [
                {
                    "Mã": tp.ticker,
                    "Lệnh": tp.action,
                    "Giá khớp": f"{tp.exec_price:,.2f}",
                    "Giá hiện tại": f"{tp.current_price:,.2f}",
                    "P&L thực tế (%)": f"{tp.pnl_pct:+.2f}%",
                    "RSI lúc khớp": tp.rsi_at_exec,
                    "Kết quả": "✅ Đúng" if tp.decision_correct else "❌ Sai",
                }
                for tp in report.ticker_performances
            ]
            st.dataframe(perf_rows, use_container_width=True, hide_index=True)
        else:
            st.info("Không có giao dịch mua/bán nào trong khoảng thời gian này để tính hiệu suất.")

        # Diagnosis
        st.write("### 🧠 Chẩn đoán từ Analyst Agent (LLM)")
        st.info(res.diagnosis)

        # Suggestions & Apply
        st.write("### 📋 Đề xuất tinh chỉnh Prompt")
        st.markdown(res.diff_summary)

        if res.suggestions:
            if st.button("Apply Proposed Prompt Configuration", type="primary", use_container_width=True):
                try:
                    agent = AnalystAgent(lookback_days=lookback)
                    agent.apply_suggestions(res)
                    st.success("🎉 Đã cập nhật cấu hình Prompt thành công! Lần sinh prompt tiếp theo sẽ tự động áp dụng các tham số mới.")
                    st.session_state.analysis_result = None
                    st.session_state.analysis_msg = ("success", "Cập nhật cấu hình thành công!")
                    st.rerun()
                except Exception as e:
                    st.error(f"Lỗi khi apply config: {e}")
        else:
            st.success("Cấu hình prompt hiện tại đang tối ưu, không cần tinh chỉnh thêm.")
