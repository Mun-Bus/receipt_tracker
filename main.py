import base64
import io
import json
import re
import uuid
import datetime
import calendar
from concurrent.futures import ThreadPoolExecutor
import pandas as pd
from PIL import Image
import streamlit as st
from st_aggrid import AgGrid, GridOptionsBuilder
from supabase import create_client
from openai import OpenAI
import plotly.graph_objects as go

st.set_page_config(page_title="Receipt Manager", layout="wide")

# Fallback vision models list
FREE_VISION_CANDIDATES = [
    "qwen/qwen2.5-vl-72b-instruct:free",
    "meta-llama/llama-3.2-11b-vision-instruct:free",
    "openrouter/free"
]

# Initialize Supabase
@st.cache_resource
def init_supabase():
    return create_client(
        st.secrets["SUPABASE_URL"],
        st.secrets["SUPABASE_KEY"]
    )

supabase = init_supabase()

def encode_image(img):
    """Downscales the image to a max dimension of 1024px and compresses JPEG quality."""
    resized_img = img.copy()
    resized_img.thumbnail((1024, 1024))
    
    buffered = io.BytesIO()
    resized_img.save(buffered, format="JPEG", quality=75, optimize=True)
    return base64.b64encode(buffered.getvalue()).decode("utf-8")

def analyze_receipt(prompt_text, base64_img, api_key):
    """Iterates through free vision endpoints until one successfully returns parsed receipt JSON."""
    ai_client = OpenAI(
        base_url="https://openrouter.ai/api/v1",
        api_key=api_key
    )
    
    last_err = ""
    for model_id in FREE_VISION_CANDIDATES:
        try:
            response = ai_client.chat.completions.create(
                model=model_id,
                extra_headers={
                    "HTTP-Referer": "https://streamlit.io",
                    "X-Title": "Receipt Manager"
                },
                messages=[
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": prompt_text},
                            {
                                "type": "image_url",
                                "image_url": {
                                    "url": f"data:image/jpeg;base64,{base64_img}"
                                }
                            }
                        ]
                    }
                ]
            )
            ai_text = response.choices[0].message.content or ""
            
            cleaned = re.sub(r"```json\s*", "", ai_text)
            cleaned = re.sub(r"```\s*", "", cleaned)
            json_match = re.search(r"\{.*\}", cleaned, re.DOTALL)
            
            if json_match:
                data = json.loads(json_match.group(0))
                if any(k in data for k in ["total_amount", "items", "subtotal", "date"]):
                    return data, None, model_id
            
            last_err = f"Model {model_id} responded without valid receipt JSON"
        except Exception as e:
            last_err = f"Model {model_id} error: {str(e)}"
            continue

    return None, f"All endpoints failed. Last log: {last_err}", None

# Initialize session states
if "admin" not in st.session_state:
    st.session_state.admin = None
if "extracted_date" not in st.session_state:
    st.session_state.extracted_date = datetime.date.today()
if "extracted_subtotal" not in st.session_state:
    st.session_state.extracted_subtotal = 0.0
if "extracted_total" not in st.session_state:
    st.session_state.extracted_total = 0.0
if "extracted_items_df" not in st.session_state:
    st.session_state.extracted_items_df = pd.DataFrame(columns=["item_name", "price"])
if "processed_file_id" not in st.session_state:
    st.session_state.processed_file_id = None
if "verification_status" not in st.session_state:
    st.session_state.verification_status = None
if "uploader_key" not in st.session_state:
    st.session_state.uploader_key = 0
if "selected_calendar_date" not in st.session_state:
    st.session_state.selected_calendar_date = None

# --- SIDEBAR: ADMIN LOGIN ---
st.sidebar.title("🔒 System Access")

if st.session_state.admin:
    st.sidebar.success(f"Admin Logged In:\n{st.session_state.admin.email}")
    if st.sidebar.button("Admin Log Out"):
        supabase.auth.sign_out()
        st.session_state.admin = None
        st.rerun()
else:
    with st.sidebar.expander("🔑 Admin Login", expanded=False):
        admin_email = st.text_input("Admin Email")
        admin_password = st.text_input("Password", type="password")
        if st.button("Authenticate Admin"):
            try:
                res = supabase.auth.sign_in_with_password({
                    "email": admin_email,
                    "password": admin_password
                })
                st.session_state.admin = res.user
                st.success("Admin authenticated!")
                st.rerun()
            except Exception:
                st.error("Invalid admin credentials.")

# --- MAIN SCREEN ---
st.title("Receipt Manager")

users_resp = supabase.table("users").select("*").execute()
users_data = users_resp.data or []

user_options = {
    f"{u.get('first_name', '')} {u.get('last_name', '')}".strip(): u["id"]
    for u in users_data
}
user_lookup = {v: k for k, v in user_options.items()}

col_select, col_add = st.columns([2, 1])

with col_select:
    selected_name = st.selectbox(
        "Select User Profile",
        options=["-- Select Your Name --"] + list(user_options.keys())
    )

with col_add:
    with st.expander("➕ Add New User"):
        new_first = st.text_input("First Name")
        new_last = st.text_input("Last Name")
        if st.button("Create Profile"):
            if new_first:
                supabase.table("users").insert({
                    "first_name": new_first,
                    "last_name": new_last
                }).execute()
                st.success(f"Added {new_first}!")
                st.rerun()
            else:
                st.warning("First name required.")

active_user_id = user_options.get(selected_name) if selected_name != "-- Select Your Name --" else None

# --- ADMIN CONTROL & ANALYTICS DASHBOARD ---
if st.session_state.admin:
    st.divider()
    st.subheader("👑 Admin Command Center")
    
    admin_tab1, admin_tab2, admin_tab3, admin_tab4 = st.tabs([
        "📅 Interactive Calendar", 
        "📊 Financial Dashboard", 
        "⚠️ Audit & Discrepancies", 
        "⚙️ Manage Data"
    ])

    try:
        receipts_resp = supabase.table("receipts").select("*").execute()
        receipts_data = receipts_resp.data or []
    except Exception:
        receipts_data = []

    try:
        items_resp = supabase.table("receipt_items").select("*").execute()
        items_data = items_resp.data or []
    except Exception:
        items_data = []

    all_receipts_df = pd.DataFrame(receipts_data)
    raw_items_df = pd.DataFrame(items_data)

    if not all_receipts_df.empty:
        all_receipts_df["date_dt"] = pd.to_datetime(all_receipts_df["date"], errors="coerce")
        all_receipts_df["month_year"] = all_receipts_df["date_dt"].dt.strftime("%Y-%m")

        item_sums = {}
        if not raw_items_df.empty:
            raw_items_df["price"] = pd.to_numeric(raw_items_df["price"], errors="coerce").fillna(0.0)
            item_sums = raw_items_df.groupby("receipt_id")["price"].sum().to_dict()

        for idx, row in all_receipts_df.iterrows():
            r_id = row.get("id")
            item_sum = float(item_sums.get(r_id, 0.0))
            total_amt = float(row.get("total_amount", 0.0))
            
            discrepancy = round(abs(total_amt - item_sum), 2)
            all_receipts_df.at[idx, "items_sum"] = item_sum
            all_receipts_df.at[idx, "discrepancy"] = discrepancy
            all_receipts_df.at[idx, "is_discrepant"] = discrepancy > 0.05

        if not raw_items_df.empty:
            merged_items = raw_items_df.merge(
                all_receipts_df[["id", "user_id", "date", "month_year"]],
                left_on="receipt_id",
                right_on="id",
                how="inner"
            )
            merged_items["item_name"] = merged_items["item_name"].astype(str).str.strip().str.title()
            all_items_df = merged_items
        else:
            all_items_df = pd.DataFrame(columns=["receipt_id", "user_id", "date", "month_year", "item_name", "price"])
    else:
        all_items_df = pd.DataFrame(columns=["receipt_id", "user_id", "date", "month_year", "item_name", "price"])

    # --- TAB 1: INTERACTIVE CALENDAR ---
    with admin_tab1:
        st.write("**🗓️ Spending Calendar (Hover for summary, Click a cell to view details)**")
        if not all_receipts_df.empty:
            valid_dates = all_receipts_df["date_dt"].dropna()
            
            if not valid_dates.empty:
                available_months = sorted(all_receipts_df["month_year"].dropna().unique(), reverse=True)
                cal_month_str = st.selectbox("Select Month View", options=available_months, index=0)
                year, month = map(int, cal_month_str.split("-"))
            else:
                today = datetime.date.today()
                year, month = today.year, today.month

            num_days = calendar.monthrange(year, month)[1]
            month_days = [datetime.date(year, month, day) for day in range(1, num_days + 1)]
            
            date_stats = {}
            for d in month_days:
                d_str = str(d)
                day_receipts = all_receipts_df[all_receipts_df["date"] == d_str]
                day_items = all_items_df[all_items_df["date"] == d_str]
                
                total_spent = day_receipts["total_amount"].sum() if not day_receipts.empty else 0.0
                rec_count = len(day_receipts)
                
                items_summary = ""
                if not day_items.empty:
                    top_items = day_items.head(4)
                    item_lines = [f"• {r['item_name']}: ${r['price']:.2f}" for _, r in top_items.iterrows()]
                    items_summary = "<br>".join(item_lines)
                    if len(day_items) > 4:
                        items_summary += f"<br>...and {len(day_items) - 4} more"
                
                date_stats[d_str] = {
                    "total_spent": total_spent,
                    "count": rec_count,
                    "items_summary": items_summary if items_summary else "No item details"
                }

            grid_x, grid_y, z_vals, text_labels, hover_texts, custom_dates = [], [], [], [], [], []
            week_days = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
            
            week_idx = 0
            for day in range(1, num_days + 1):
                cur_date = datetime.date(year, month, day)
                d_str = str(cur_date)
                w_day = cur_date.weekday()
                
                if day > 1 and w_day == 0:
                    week_idx += 1

                stats = date_stats[d_str]
                spent = stats["total_spent"]
                count = stats["count"]
                
                grid_x.append(week_days[w_day])
                grid_y.append(f"Week {week_idx + 1}")
                z_vals.append(spent)
                text_labels.append(f"<b>{day}</b><br>${spent:.2f}" if spent > 0 else f"{day}")
                
                hover_content = f"<b>Date:</b> {d_str}<br><b>Total Spent:</b> ${spent:,.2f}<br><b>Receipts:</b> {count}<br><br><b>Items Preview:</b><br>{stats['items_summary']}"
                hover_texts.append(hover_content)
                custom_dates.append(d_str)

            fig = go.Figure(data=go.Heatmap(
                x=grid_x,
                y=grid_y,
                z=z_vals,
                text=text_labels,
                texttemplate="%{text}",
                hoverinfo="text",
                hovertext=hover_texts,
                customdata=custom_dates,
                colorscale="YlGnBu",
                showscale=True,
                colorbar=dict(title="Spent ($)")
            ))

            fig.update_layout(
                title=f"Spending Heatmap - {calendar.month_name[month]} {year}",
                xaxis=dict(title="Day of Week", categoryorder="array", categoryarray=week_days),
                yaxis=dict(autorange="reversed", title=""),
                height=420,
                margin=dict(l=40, r=40, t=50, b=40)
            )

            event_data = st.plotly_chart(fig, use_container_width=True, on_select="rerun", selection_mode="points")
            
            selected_date = None
            if event_data and "selection" in event_data and event_data["selection"].get("points"):
                point = event_data["selection"]["points"][0]
                if "customdata" in point:
                    selected_date = point["customdata"]

            if selected_date:
                st.session_state.selected_calendar_date = selected_date

            st.divider()
            active_date = st.session_state.selected_calendar_date
            if active_date:
                st.subheader(f"📌 Detailed Breakdown for {active_date}")
                
                day_recs = all_receipts_df[all_receipts_df["date"] == active_date]
                day_its = all_items_df[all_items_df["date"] == active_date]

                if not day_recs.empty:
                    c1, c2 = st.columns(2)
                    c1.metric("Day Total Spending", f"${day_recs['total_amount'].sum():,.2f}")
                    c2.metric("Receipts Logged", len(day_recs))

                    st.write("---")
                    st.write("**🛒 Items Purchased on this Day:**")
                    if not day_its.empty:
                        st.dataframe(
                            day_its[["item_name", "price", "receipt_id"]],
                            use_container_width=True,
                            hide_index=True,
                            column_config={
                                "item_name": st.column_config.TextColumn("Product / Item"),
                                "price": st.column_config.NumberColumn("Price ($)", format="$%.2f"),
                                "receipt_id": st.column_config.TextColumn("Receipt Ref ID")
                            }
                        )
                    else:
                        st.info("No itemized breakdown saved for this date.")

                    st.write("**🧾 Receipts List:**")
                    for _, r_row in day_recs.iterrows():
                        u_name = user_lookup.get(r_row["user_id"], "Unknown User")
                        with st.expander(f"Receipt ID: {r_row['id']} | User: {u_name} | Total: ${r_row['total_amount']:.2f}"):
                            st.write(f"Subtotal: ${r_row.get('subtotal', 0.0):.2f}")
                            if r_row.get("image_url"):
                                st.markdown(f"[📷 View Original Receipt Image]({r_row['image_url']})")
                else:
                    st.info(f"No receipts recorded on {active_date}.")
            else:
                st.info("💡 Click on any date box in the calendar chart above to view full itemized receipts.")
        else:
            st.info("No receipts recorded yet to generate calendar.")

    # --- TAB 2: FINANCIAL DASHBOARD ---
    with admin_tab2:
        if not all_receipts_df.empty:
            months_available = sorted(all_receipts_df["month_year"].dropna().unique(), reverse=True)
            selected_month = st.selectbox("📅 Select Spending Period (Month)", options=["All Time"] + list(months_available), key="fin_dash_month")

            filtered_df = all_receipts_df if selected_month == "All Time" else all_receipts_df[all_receipts_df["month_year"] == selected_month]
            filtered_items = all_items_df if selected_month == "All Time" or all_items_df.empty else all_items_df[all_items_df["month_year"] == selected_month]

            m1, m2, m3 = st.columns(3)
            m1.metric("Total Spending", f"${filtered_df['total_amount'].sum():,.2f}")
            m2.metric("Receipt Count", len(filtered_df))
            m3.metric("Flagged Discrepancies", int(filtered_df['is_discrepant'].sum()) if 'is_discrepant' in filtered_df else 0)

            st.write("---")
            st.write("**🛒 Itemized Breakdown (e.g., Apple, Bread Spending)**")
            if not filtered_items.empty:
                item_group = filtered_items.groupby("item_name").agg(
                    Total_Spent=("price", "sum"),
                    Times_Purchased=("price", "count"),
                    Avg_Price=("price", "mean")
                ).reset_index().sort_values(by="Total_Spent", ascending=False)

                st.dataframe(
                    item_group,
                    use_container_width=True,
                    hide_index=True,
                    column_config={
                        "item_name": st.column_config.TextColumn("Product / Item"),
                        "Total_Spent": st.column_config.NumberColumn("Total Spent ($)", format="$%.2f"),
                        "Times_Purchased": st.column_config.NumberColumn("Quantity Count"),
                        "Avg_Price": st.column_config.NumberColumn("Average Price ($)", format="$%.2f")
                    }
                )
            else:
                st.info("No item details found for this period.")
        else:
            st.info("No receipts recorded yet to build analytics.")

    # --- TAB 3: AUDIT & DISCREPANCIES ---
    with admin_tab3:
        st.write("**⚠️ Receipts Flagged for Admin Review**")
        if not all_receipts_df.empty and "is_discrepant" in all_receipts_df:
            flagged = all_receipts_df[all_receipts_df["is_discrepant"] == True]
            if not flagged.empty:
                st.warning(f"Found {len(flagged)} receipt(s) where the total amount does not match the sum of individual line items.")
                st.dataframe(
                    flagged[["id", "user_id", "date", "subtotal", "total_amount", "items_sum", "discrepancy"]],
                    use_container_width=True,
                    hide_index=True,
                    column_config={
                        "subtotal": st.column_config.NumberColumn("Subtotal ($)", format="$%.2f"),
                        "total_amount": st.column_config.NumberColumn("Receipt Total ($)", format="$%.2f"),
                        "items_sum": st.column_config.NumberColumn("Item Sum ($)", format="$%.2f"),
                        "discrepancy": st.column_config.NumberColumn("Difference ($)", format="$%.2f")
                    }
                )
            else:
                st.success("✅ All receipts pass sum audit! Item totals match final totals.")
        else:
            st.info("No records available to audit.")

    # --- TAB 4: MANAGE & DELETE DATA ---
    with admin_tab4:
        col_del_user, col_del_rec = st.columns(2)

        with col_del_user:
            st.write("🗑️ **Delete User Profile**")
            user_to_delete = st.selectbox("Select User to Remove", options=["-- Choose User --"] + list(user_options.keys()))
            if st.button("Delete User Profile", type="primary"):
                if user_to_delete != "-- Choose User --":
                    u_id = user_options[user_to_delete]
                    supabase.table("users").delete().eq("id", u_id).execute()
                    st.success(f"User '{user_to_delete}' removed successfully!")
                    st.rerun()

        with col_del_rec:
            st.write("🗑️ **Delete Specific Receipt**")
            if not all_receipts_df.empty:
                receipt_list = [f"ID: {r['id']} | Date: {r['date']} | Total: ${r['total_amount']}" for r in receipts_data]
                selected_rec = st.selectbox("Select Receipt to Remove", options=["-- Choose Receipt --"] + receipt_list)
                if st.button("Delete Selected Receipt", type="primary"):
                    if selected_rec != "-- Choose Receipt --":
                        rec_id = selected_rec.split("ID: ")[1].split(" |")[0]
                        supabase.table("receipt_items").delete().eq("receipt_id", rec_id).execute()
                        supabase.table("receipts").delete().eq("id", rec_id).execute()
                        st.success("Receipt deleted successfully!")
                        st.rerun()

# --- WORKFLOW SECTION ---
if active_user_id or st.session_state.admin:
    st.divider()

    # 1. RECEIPT UPLOAD
    if active_user_id:
        st.subheader(f"1. Upload Receipt for {selected_name}")
        
        uploaded_file = st.file_uploader(
            "Upload receipt photo", 
            type=["jpg", "jpeg", "png"], 
            key=f"receipt_uploader_{st.session_state.uploader_key}"
        )

        if uploaded_file is not None:
            image = Image.open(uploaded_file).convert("RGB")
            st.image(image, caption="Uploaded Receipt", width=300)

            file_id = f"{uploaded_file.name}_{uploaded_file.size}"

            if st.session_state.processed_file_id != file_id:
                key_1 = st.secrets.get("OPENROUTER_API_KEY_1", st.secrets.get("OPENROUTER_API_KEY"))
                key_2 = st.secrets.get("OPENROUTER_API_KEY_2", st.secrets.get("OPENROUTER_API_KEY"))

                if not key_1 or not key_2:
                    st.error("⚠️ Missing API keys! Please define OPENROUTER_API_KEY_1 and OPENROUTER_API_KEY_2 in Streamlit Secrets.")
                else:
                    with st.status("🤖 Running dual AI extraction & endpoint auto-retry...", expanded=True) as status:
                        status.write("📷 Downscaling & compressing image payload...")
                        base64_image = encode_image(image)
                        current_year = datetime.date.today().year

                        prompt = f"""
                        Analyze this receipt image.
                        1. Extract transaction date. Format as YYYY-MM-DD. If year is missing, infer using current year ({current_year}).
                        2. Extract all items/products with names and individual prices.
                        3. Extract subtotal and final total_amount.

                        Return ONLY valid JSON matching this exact structure:
                        {{
                            "date": "YYYY-MM-DD",
                            "items": [
                                {{"item_name": "Item A", "price": 5.00}},
                                {{"item_name": "Item B", "price": 2.50}}
                            ],
                            "subtotal": 7.50,
                            "total_amount": 7.50
                        }}
                        """

                        status.write("⚡ Dispatching to free vision candidates with fallback retries...")

                        with ThreadPoolExecutor(max_workers=2) as executor:
                            future_1 = executor.submit(analyze_receipt, prompt, base64_image, key_1)
                            future_2 = executor.submit(analyze_receipt, prompt, base64_image, key_2)

                            res_1, err_1, used_model_1 = future_1.result()
                            res_2, err_2, used_model_2 = future_2.result()

                        status.write("⚖️ Validating outputs from responsive endpoints...")

                        final_json = None
                        if res_1 and res_2:
                            d1, d2 = res_1.get("date"), res_2.get("date")
                            s1, s2 = float(res_1.get("subtotal", 0)), float(res_2.get("subtotal", 0))
                            t1, t2 = float(res_1.get("total_amount", 0)), float(res_2.get("total_amount", 0))

                            if d1 == d2 and abs(s1 - s2) < 0.01 and abs(t1 - t2) < 0.01:
                                st.session_state.verification_status = "MATCH"
                                status.update(label=f"✅ Verified Match! ({used_model_1} & {used_model_2})", state="complete", expanded=False)
                            else:
                                st.session_state.verification_status = "MISMATCH"
                                status.update(label=f"⚠️ Extracted successfully using {used_model_1} (minor mismatch with worker #2)", state="complete", expanded=False)
                            final_json = res_1

                        elif res_1:
                            final_json = res_1
                            st.session_state.verification_status = "SINGLE"
                            status.update(label=f"✅ Extracted successfully via {used_model_1}", state="complete", expanded=False)

                        elif res_2:
                            final_json = res_2
                            st.session_state.verification_status = "SINGLE"
                            status.update(label=f"✅ Extracted successfully via {used_model_2}", state="complete", expanded=False)

                        else:
                            status.update(label="❌ Extraction failed across all free vision candidates.", state="error")
                            if err_1:
                                st.error(f"Worker 1 Log: {err_1}")
                            if err_2:
                                st.error(f"Worker 2 Log: {err_2}")

                        if final_json:
                            extracted_date_str = final_json.get("date", "")
                            try:
                                parsed_date = datetime.datetime.strptime(extracted_date_str, "%Y-%m-%d").date()
                            except ValueError:
                                parsed_date = datetime.date.today()

                            raw_items = final_json.get("items", [])
                            if isinstance(raw_items, list) and len(raw_items) > 0:
                                items_df = pd.DataFrame(raw_items)
                            else:
                                items_df = pd.DataFrame(columns=["item_name", "price"])

                            st.session_state.extracted_date = parsed_date
                            st.session_state.extracted_subtotal = float(final_json.get("subtotal", 0.0))
                            st.session_state.extracted_total = float(final_json.get("total_amount", 0.0))
                            st.session_state.extracted_items_df = items_df
                            st.session_state.processed_file_id = file_id
                            st.rerun()

            if st.session_state.verification_status == "MATCH":
                st.success("✅ **Dual Verification Passed:** Both vision workers successfully extracted matching data.")
            elif st.session_state.verification_status == "MISMATCH":
                st.warning("⚠️ **Notice:** Minor differences detected between models. Using primary output.")

            st.write("**Verify Data (Read-Only Review):**")
            
            st.write("Products Purchased:")
            st.dataframe(
                st.session_state.extracted_items_df,
                use_container_width=True,
                hide_index=True,
                column_config={
                    "item_name": st.column_config.TextColumn("Product Name"),
                    "price": st.column_config.NumberColumn("Price ($)", format="$%.2f")
                }
            )

            with st.form("receipt_form"):
                rec_date = st.date_input("Date", value=st.session_state.extracted_date, disabled=True)
                subtotal = st.number_input("Subtotal ($)", value=st.session_state.extracted_subtotal, step=0.01, disabled=True)
                total_amount = st.number_input("Total Amount ($)", value=st.session_state.extracted_total, step=0.01, disabled=True)

                submit_btn = st.form_submit_button("Save Receipt")

            if submit_btn:
                try:
                    file_ext = uploaded_file.name.split('.')[-1]
                    file_path = f"{active_user_id}/{uuid.uuid4()}.{file_ext}"
                    bytes_data = uploaded_file.getvalue()

                    supabase.storage.from_("receipts-storage").upload(file_path, bytes_data)
                    public_url = supabase.storage.from_("receipts-storage").get_public_url(file_path)

                    receipt_payload = {
                        "user_id": active_user_id,
                        "date": str(rec_date),
                        "subtotal": subtotal,
                        "total_amount": total_amount,
                        "image_url": public_url
                    }
                    res = supabase.table("receipts").insert(receipt_payload).execute()
                    
                    if res.data:
                        receipt_id = res.data[0]["id"]

                        items_payload = []
                        for _, row in st.session_state.extracted_items_df.iterrows():
                            if str(row.get("item_name", "")).strip():
                                items_payload.append({
                                    "receipt_id": receipt_id,
                                    "item_name": str(row.get("item_name", "")),
                                    "price": float(row.get("price", 0.0))
                                })

                        if items_payload:
                            supabase.table("receipt_items").insert(items_payload).execute()

                    st.success("Receipt and items saved successfully!")

                    st.session_state.uploader_key += 1
                    st.session_state.processed_file_id = None
                    st.session_state.verification_status = None
                    st.session_state.extracted_date = datetime.date.today()
                    st.session_state.extracted_subtotal = 0.0
                    st.session_state.extracted_total = 0.0
                    st.session_state.extracted_items_df = pd.DataFrame(columns=["item_name", "price"])
                    st.rerun()

                except Exception as err:
                    st.error(f"Save error: {err}")

        st.divider()

    # 2. SPREADSHEET VIEWER
    st.subheader("2. Ranked Receipts Spreadsheet")

    try:
        if st.session_state.admin and not active_user_id:
            rec_resp = supabase.table("receipts").select("*").execute()
            all_items_resp = supabase.table("receipt_items").select("*").execute()
        elif active_user_id:
            rec_resp = supabase.table("receipts").select("*").eq("user_id", active_user_id).execute()
            all_items_resp = supabase.table("receipt_items").select("*").execute()
        else:
            rec_resp = None
            all_items_resp = None
    except Exception:
        rec_resp = None
        all_items_resp = None

    if rec_resp and rec_resp.data:
        df = pd.DataFrame(rec_resp.data)
        
        items_dict = {}
        if all_items_resp and all_items_resp.data:
            items_df_tmp = pd.DataFrame(all_items_resp.data)
            if not items_df_tmp.empty and "receipt_id" in items_df_tmp.columns:
                for r_id, group in items_df_tmp.groupby("receipt_id"):
                    items_dict[r_id] = ", ".join([f"{row['item_name']} (${row['price']})" for _, row in group.iterrows()])

        df["items"] = df["id"].apply(lambda r_id: items_dict.get(r_id, ""))

        df["Rank"] = df["total_amount"].rank(ascending=False, method="min").astype(int)
        df = df.sort_values(by="Rank")

        display_cols = ["Rank", "user_id", "date", "items", "subtotal", "total_amount", "created_at", "image_url"]
        df_display = df[[c for c in display_cols if c in df.columns]]

        gb = GridOptionsBuilder.from_dataframe(df_display)
        gb.configure_pagination(paginationAutoPageSize=True)
        gb.configure_default_column(editable=False, groupable=True)
        gridOptions = gb.build()

        AgGrid(df_display, gridOptions=gridOptions, height=350, theme="alpine")
    else:
        st.info("No receipts found for this view.")
else:
    st.info("Select or add a user profile above, or log in as Admin using the sidebar menu.")
