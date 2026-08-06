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
from executor import extract_json, validate_order, execute_order, is_sell_order
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

if "pending_sell_orders" not in st.session_state:
    st.session_state.pending_sell_orders = []

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

tab_trading, tab_analyst, tab_portfolio = st.tabs([
    "💼 Giao dịch (Trading Desk)",
    "🔬 Analyst Agent (Feedback Loop)",
    "⚙️ Quản lý Danh mục",
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
                # Pipeline: extract → validate → split (auto execute BUY/HOLD vs pending approval for SELL/CUT_LOSS)
                try:
                    # Bước 1: Regex extract JSON
                    json_str = extract_json(raw_input)

                    # Bước 2: Pydantic validate (Trả về list các TradeOrder)
                    orders = validate_order(json_str)

                    # Bước 3: Phân loại lệnh
                    auto_exec_orders = [o for o in orders if not is_sell_order(o)]
                    pending_sells = [o for o in orders if is_sell_order(o)]

                    # Bước 4: Thực thi tự động cho các lệnh MUA/GIỮ
                    if auto_exec_orders:
                        current_portfolio = load_portfolio()
                        updated_portfolio = copy.deepcopy(current_portfolio)

                        for order in auto_exec_orders:
                            updated_portfolio = execute_order(order, updated_portfolio)

                        save_portfolio(updated_portfolio)
                        for order in auto_exec_orders:
                            insert_log(order.model_dump())

                    # Bước 5: Chuyển các lệnh BÁN vào danh sách chờ phê duyệt
                    if pending_sells:
                        st.session_state.pending_sell_orders.extend(pending_sells)

                    # Feedback tổng hợp
                    msg_lines = []
                    if auto_exec_orders:
                        msg_lines.append("✅ **Đã thực thi thành công các lệnh mua/nắm giữ:**")
                        for order in auto_exec_orders:
                            msg_lines.append(
                                f"- **{order.action} {order.volume} {order.ticker}** @ {order.price}  \n"
                                f"  *Lý do:* {order.reason}"
                            )
                    if pending_sells:
                        msg_lines.append(
                            f"⏳ **Có {len(pending_sells)} lệnh bán đã được chuyển vào hàng chờ Chờ Phê Duyệt bên dưới.**"
                        )

                    st.session_state.exec_message = (
                        "success" if auto_exec_orders else "info",
                        "\n\n".join(msg_lines),
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
            elif msg_type == "info":
                st.info(msg_content)
            elif msg_type == "error":
                st.error(msg_content)

        # ── Hàng đợi Lệnh bán Chờ Phê duyệt ────────────────────────────
        if st.session_state.pending_sell_orders:
            st.divider()
            st.subheader(f"⏳ Lệnh bán chờ phê duyệt ({len(st.session_state.pending_sell_orders)})")
            st.caption("Các lệnh bán bên dưới chưa thực thi. Chỉ cập nhật danh mục & audit log khi bạn phê duyệt.")

            # Thao tác hàng loạt nếu có nhiều hơn 1 lệnh
            if len(st.session_state.pending_sell_orders) > 1:
                btn_c1, btn_c2 = st.columns(2)
                with btn_c1:
                    if st.button("✅ Duyệt tất cả lệnh bán", type="primary", use_container_width=True, key="approve_all"):
                        try:
                            cur_p = load_portfolio()
                            upd_p = copy.deepcopy(cur_p)
                            approved_orders = list(st.session_state.pending_sell_orders)
                            for o in approved_orders:
                                upd_p = execute_order(o, upd_p)
                                insert_log(o.model_dump())
                            save_portfolio(upd_p)
                            st.session_state.pending_sell_orders = []
                            st.session_state.exec_message = (
                                "success",
                                f"✅ Đã phê duyệt và thực thi thành công tất cả {len(approved_orders)} lệnh bán!",
                            )
                            st.rerun()
                        except Exception as e:
                            st.error(f"❌ Lỗi khi duyệt tất cả: {e}")
                with btn_c2:
                    if st.button("❌ Hủy tất cả lệnh bán", use_container_width=True, key="reject_all"):
                        count = len(st.session_state.pending_sell_orders)
                        st.session_state.pending_sell_orders = []
                        st.session_state.exec_message = (
                            "info",
                            f"ℹ️ Đã từ chối và hủy {count} lệnh bán chờ duyệt.",
                        )
                        st.rerun()

            # Render thẻ từng lệnh
            for idx, order in enumerate(list(st.session_state.pending_sell_orders)):
                with st.expander(
                    f"📌 Lệnh #{idx+1}: {order.action} {order.volume} {order.ticker} @ {order.price}",
                    expanded=True,
                ):
                    col_info, col_act = st.columns([3, 2])
                    with col_info:
                        st.markdown(f"- **Mã Cổ Phiếu:** `{order.ticker}`")
                        st.markdown(f"- **Hành Động:** `{order.action}`")
                        st.markdown(f"- **Số Lượng:** `{order.volume}`")
                        st.markdown(f"- **Giá Đặt:** `{order.price:,.2f}` (nghìn VND)")
                        if isinstance(order.volume, (int, float)):
                            est_val = order.volume * order.price * 1000
                            st.markdown(f"- **Thu Về Dự Kiến:** `{est_val:,.0f} VND`")
                        st.markdown(f"- **Lý Do (AI):** {order.reason}")

                    with col_act:
                        st.write(" ")
                        if st.button(
                            "✅ Phê duyệt Bán",
                            key=f"approve_sell_{idx}_{order.ticker}",
                            type="primary",
                            use_container_width=True,
                        ):
                            try:
                                cur_p = load_portfolio()
                                upd_p = copy.deepcopy(cur_p)
                                upd_p = execute_order(order, upd_p)
                                save_portfolio(upd_p)
                                insert_log(order.model_dump())
                                st.session_state.pending_sell_orders.pop(idx)
                                st.session_state.exec_message = (
                                    "success",
                                    f"✅ **Đã phê duyệt và thực thi bán thành công {order.volume} {order.ticker}** @ {order.price}!",
                                )
                                st.rerun()
                            except Exception as e:
                                st.error(f"❌ Lỗi thực thi lệnh bán: {e}")

                        if st.button(
                            "❌ Từ chối",
                            key=f"reject_sell_{idx}_{order.ticker}",
                            use_container_width=True,
                        ):
                            st.session_state.pending_sell_orders.pop(idx)
                            st.session_state.exec_message = (
                                "info",
                                f"ℹ️ Đã từ chối lệnh bán {order.volume} {order.ticker}.",
                            )
                            st.rerun()

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

# ── Tab 3: Quản lý Danh mục (CRUD) ───────────────────────────────
with tab_portfolio:
    st.subheader("⚙️ Quản lý Danh mục")
    st.caption("Chỉnh sửa trực tiếp danh mục: tiền mặt, cổ phiếu đang nắm. Mọi thay đổi được lưu ngay vào portfolio.json.")

    pf = load_portfolio()

    # ────────────────────────────────────────────────────────────────
    # SECTION 1 — Sửa tiền mặt (UPDATE cash)
    # ────────────────────────────────────────────────────────────────
    st.write("### 💰 Tiền mặt")
    col_cash, col_cash_btn = st.columns([3, 1])
    with col_cash:
        new_cash = st.number_input(
            "Số dư tiền mặt (VND)",
            min_value=0.0,
            value=float(pf.get("cash", 0)),
            step=1_000_000.0,
            format="%.0f",
            key="crud_cash_input",
        )
    with col_cash_btn:
        st.write(" ")
        st.write(" ")
        if st.button("💾 Lưu tiền mặt", use_container_width=True, key="crud_save_cash"):
            pf["cash"] = float(new_cash)
            save_portfolio(pf)
            st.success(f"✅ Đã cập nhật tiền mặt: {new_cash:,.0f} VND")
            st.rerun()

    st.divider()

    # ────────────────────────────────────────────────────────────────
    # SECTION 2 — Thêm cổ phiếu mới (CREATE holding)
    # ────────────────────────────────────────────────────────────────
    st.write("### ➕ Thêm cổ phiếu mới")
    with st.form(key="crud_add_holding_form", clear_on_submit=True):
        col_add1, col_add2, col_add3 = st.columns(3)
        with col_add1:
            new_ticker = st.text_input(
                "Mã cổ phiếu",
                placeholder="VD: HPG",
                key="crud_new_ticker",
            ).strip().upper()
        with col_add2:
            new_qty = st.number_input(
                "Số lượng (cổ phiếu)",
                min_value=1,
                value=100,
                step=10,
                key="crud_new_qty",
            )
        with col_add3:
            new_avg = st.number_input(
                "Giá trung bình (nghìn VND)",
                min_value=0.0,
                value=0.0,
                step=0.1,
                format="%.2f",
                key="crud_new_avg",
            )
        submitted_add = st.form_submit_button("➕ Thêm vào danh mục", type="primary", use_container_width=True)

    if submitted_add:
        if not new_ticker or len(new_ticker) < 3:
            st.error("❌ Mã cổ phiếu không hợp lệ — phải từ 3 ký tự trở lên.")
        elif new_ticker in pf.get("holdings", {}):
            st.warning(f"⚠️ Mã **{new_ticker}** đã tồn tại trong danh mục. Hãy sửa số lượng trong bảng bên dưới.")
        else:
            pf.setdefault("holdings", {})[new_ticker] = {
                "quantity": int(new_qty),
                "avg_price": float(new_avg),
            }
            save_portfolio(pf)
            st.success(f"✅ Đã thêm **{new_ticker}** — {int(new_qty)} cổ phiếu @ {new_avg:.2f}")
            st.rerun()

    st.divider()

    # ────────────────────────────────────────────────────────────────
    # SECTION 3 — Sửa holdings (UPDATE via data_editor)
    # ────────────────────────────────────────────────────────────────
    st.write("### ✏️ Sửa danh sách nắm giữ")
    holdings_now = pf.get("holdings", {})

    if not holdings_now:
        st.info("Danh mục trống. Thêm cổ phiếu ở phần bên trên.")
    else:
        # Chuyển holdings dict → list of rows để data_editor edit được
        editor_rows = [
            {"Mã": ticker, "Số lượng": info["quantity"], "Giá TB (nghìn VND)": info["avg_price"]}
            for ticker, info in holdings_now.items()
        ]

        edited_df = st.data_editor(
            editor_rows,
            use_container_width=True,
            hide_index=True,
            num_rows="fixed",
            column_config={
                "Mã": st.column_config.TextColumn(disabled=True),
                "Số lượng": st.column_config.NumberColumn(min_value=0, step=10, format="%d"),
                "Giá TB (nghìn VND)": st.column_config.NumberColumn(min_value=0.0, step=0.01, format="%.2f"),
            },
            key="crud_holdings_editor",
        )

        if st.button("💾 Lưu thay đổi", type="primary", use_container_width=True, key="crud_save_holdings"):
            updated_holdings = {}
            has_error = False
            for row in edited_df:
                ticker_key = row["Mã"]
                qty = int(row["Số lượng"])
                avg = float(row["Giá TB (nghìn VND)"])
                if qty < 0:
                    st.error(f"❌ {ticker_key}: Số lượng không được âm.")
                    has_error = True
                    break
                if qty > 0:  # bỏ qua nếu đã giảm về 0
                    updated_holdings[ticker_key] = {"quantity": qty, "avg_price": avg}

            if not has_error:
                pf["holdings"] = updated_holdings
                save_portfolio(pf)
                st.success("✅ Đã lưu danh sách nắm giữ!")
                st.rerun()

    st.divider()

    # ────────────────────────────────────────────────────────────────
    # SECTION 4 — Xóa một mã (DELETE single holding)
    # ────────────────────────────────────────────────────────────────
    st.write("### 🗑️ Xóa cổ phiếu khỏi danh mục")
    holdings_for_delete = pf.get("holdings", {})

    if not holdings_for_delete:
        st.info("Danh mục trống, không có gì để xóa.")
    else:
        col_del1, col_del2 = st.columns([3, 1])
        with col_del1:
            ticker_to_delete = st.selectbox(
                "Chọn mã cần xóa",
                options=list(holdings_for_delete.keys()),
                key="crud_ticker_to_delete",
            )
        with col_del2:
            st.write(" ")
            st.write(" ")
            if st.button(f"🗑️ Xóa {ticker_to_delete}", type="primary", use_container_width=True, key="crud_delete_btn"):
                del pf["holdings"][ticker_to_delete]
                save_portfolio(pf)
                st.success(f"✅ Đã xóa **{ticker_to_delete}** khỏi danh mục.")
                st.rerun()

    st.divider()

    # ────────────────────────────────────────────────────────────────
    # SECTION 5 — Reset toàn bộ danh mục (DELETE all)
    # ────────────────────────────────────────────────────────────────
    st.write("### ⚠️ Reset danh mục")
    st.warning("Thao tác này sẽ xóa toàn bộ cổ phiếu đang nắm và đặt lại tiền mặt về mức mặc định. Không thể hoàn tác.")

    col_reset1, col_reset2 = st.columns([2, 1])
    with col_reset1:
        reset_cash_val = st.number_input(
            "Tiền mặt sau khi reset (VND)",
            min_value=0.0,
            value=100_000_000.0,
            step=10_000_000.0,
            format="%.0f",
            key="crud_reset_cash",
        )
    with col_reset2:
        confirm_reset = st.checkbox("Tôi xác nhận muốn reset", key="crud_confirm_reset")

    if st.button("🔄 Reset toàn bộ danh mục", disabled=not confirm_reset, use_container_width=True, key="crud_reset_btn"):
        save_portfolio({"cash": float(reset_cash_val), "holdings": {}})
        st.success(f"✅ Đã reset danh mục. Tiền mặt: {reset_cash_val:,.0f} VND, holdings: trống.")
        st.rerun()
