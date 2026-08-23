import base64
import io
import json
import re
import uuid
import datetime
from concurrent.futures import ThreadPoolExecutor
import pandas as pd
from PIL import Image
import streamlit as st
from st_aggrid import AgGrid, GridOptionsBuilder
from supabase import create_client
from openai import OpenAI

st.set_page_config(page_title="Receipt Manager", layout="wide")

# Initialize Supabase
@st.cache_resource
def init_supabase():
    return create_client(
        st.secrets["SUPABASE_URL"],
        st.secrets["SUPABASE_KEY"]
    )

supabase = init_supabase()

def encode_image(img):
    buffered = io.BytesIO()
    img.save(buffered, format="JPEG")
    return base64.b64encode(buffered.getvalue()).decode("utf-8")

def analyze_receipt(model_id, prompt_text, base64_img, api_key):
    """Worker function to run receipt extraction via OpenRouter API using a dedicated API key."""
    try:
        ai_client = OpenAI(
            base_url="https://openrouter.ai/api/v1",
            api_key=api_key
        )
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
            return json.loads(json_match.group(0)), None
        else:
            return None, f"No valid JSON returned ({ai_text[:80]}...)"
    except Exception as e:
        return None, str(e)

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

# --- SIDEBAR: ADMIN LOGIN & SYSTEM ACTIONS ---
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
            except Exception as e:
                st.error("Invalid admin credentials.")

# --- MAIN SCREEN ---
st.title("Receipt Manager")

users_resp = supabase.table("users").select("*").execute()
users_data = users_resp.data or []

user_options = {
    f"{u.get('first_name', '')} {u.get('last_name', '')}".strip(): u["id"]
    for u in users_data
}

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
    
    admin_tab1, admin_tab2, admin_tab3 = st.tabs(["📊 Financial Dashboard", "⚠️ Audit & Discrepancies", "⚙️ Manage Data"])

    # Fetch master records for dashboard
    try:
        master_resp = supabase.table("receipts").select("*, receipt_items(*)").execute()
        master_data = master_resp.data or []
    except Exception:
        master_data = []

    # Process Master Data
    all_receipts_df = pd.DataFrame(master_data)
    all_items_list = []

    if not all_receipts_df.empty:
        all_receipts_df["date_dt"] = pd.to_datetime(all_receipts_df["date"], errors="coerce")
        all_receipts_df["month_year"] = all_receipts_df["date_dt"].dt.strftime("%Y-%m")

        for idx, row in all_receipts_df.iterrows():
            items = row.get("receipt_items") or []
            item_sum = sum([float(i.get("price", 0)) for i in items if isinstance(i, dict)])
            total_amt = float(row.get("total_amount", 0))
            
            # Check discrepancy between calculated items vs receipt total
            discrepancy = round(abs(total_amt - item_sum), 2)
            all_receipts_df.at[idx, "items_sum"] = item_sum
            all_receipts_df.at[idx, "discrepancy"] = discrepancy
            all_receipts_df.at[idx, "is_discrepant"] = discrepancy > 0.05

            for item in items:
                if isinstance(item, dict):
                    all_items_list.append({
                        "receipt_id": row.get("id"),
                        "user_id": row.get("user_id"),
                        "date": row.get("date"),
                        "month_year": row.get("month_year"),
                        "item_name": str(item.get("item_name", "")).strip().title(),
                        "price": float(item.get("price", 0.0))
                    })

    all_items_df = pd.DataFrame(all_items_list)

    # --- TAB 1: FINANCIAL DASHBOARD ---
    with admin_tab1:
        if not all_receipts_df.empty:
            months_available = sorted(all_receipts_df["month_year"].dropna().unique(), reverse=True)
            selected_month = st.selectbox("📅 Select Spending Period (Month)", options=["All Time"] + list(months_available))

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

    # --- TAB 2: AUDIT & DISCREPANCIES ---
    with admin_tab2:
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
                        "total_amount": st.column_config.NumberColumn("Receipt Total ($)", format="$%.2f"),
                        "items_sum": st.column_config.NumberColumn("Item Sum ($)", format="$%.2f"),
                        "discrepancy": st.column_config.NumberColumn("Difference ($)", format="$%.2f")
                    }
                )
            else:
                st.success("✅ All receipts pass sum audit! Item totals match final totals.")
        else:
            st.info("No records available to audit.")

    # --- TAB 3: MANAGE & DELETE DATA ---
    with admin_tab3:
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
                receipt_list = [f"ID: {r['id']} | Date: {r['date']} | Total: ${r['total_amount']}" for r in master_data]
                selected_rec = st.selectbox("Select Receipt to Remove", options=["-- Choose Receipt --"] + receipt_list)
                if st.button("Delete Selected Receipt", type="primary"):
                    if selected_rec != "-- Choose Receipt --":
                        rec_id = selected_rec.split("ID: ")[1].split(" |")[0]
                        supabase.table("receipts").delete().eq("id", rec_id).execute()
                        st.success("Receipt deleted successfully!")
                        st.rerun()

# --- WORKFLOW SECTION ---
if active_user_id or st.session_state.admin:
    st.divider()

    # 1. RECEIPT UPLOAD
    if active_user_id:
        st.subheader(f"1. Upload Receipt for {selected_name}")
        uploaded_file = st.file_uploader("Upload receipt photo", type=["jpg", "jpeg", "png"])

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
                    with st.status("🤖 Running dual AI extraction & verification...", expanded=True) as status:
                        status.write("📷 Encoding image for Vision processing...")
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

                        status.write("⚡ Dispatching requests concurrently using API Key #1 and API Key #2...")
                        
                        model_1 = "openrouter/free"
                        model_2 = "google/gemini-2.0-flash-exp:free"

                        with ThreadPoolExecutor(max_workers=2) as executor:
                            future_1 = executor.submit(analyze_receipt, model_1, prompt, base64_image, key_1)
                            future_2 = executor.submit(analyze_receipt, model_2, prompt, base64_image, key_2)

                            status.write("🔍 Model #1 (Key 1) analyzing items...")
                            res_1, err_1 = future_1.result()

                            status.write("🔍 Model #2 (Key 2) cross-checking values...")
                            res_2, err_2 = future_2.result()

                        status.write("⚖️ Comparing outputs from both models...")

                        if err_1:
                            status.write(f"⚠️ Model #1 log: {err_1}")
                        if err_2:
                            status.write(f"⚠️ Model #2 log: {err_2}")

                        final_json = None
                        if res_1 and res_2:
                            d1, d2 = res_1.get("date"), res_2.get("date")
                            s1, s2 = float(res_1.get("subtotal", 0)), float(res_2.get("subtotal", 0))
                            t1, t2 = float(res_1.get("total_amount", 0)), float(res_2.get("total_amount", 0))
                            i1, i2 = len(res_1.get("items", [])), len(res_2.get("items", []))

                            if d1 == d2 and abs(s1 - s2) < 0.01 and abs(t1 - t2) < 0.01 and i1 == i2:
                                st.session_state.verification_status = "MATCH"
                                status.update(label="✅ Dual AI analysis complete — Perfect Match Verified!", state="complete", expanded=False)
                            else:
                                st.session_state.verification_status = "MISMATCH"
                                status.update(label="⚠️ Dual AI complete — Minor differences found (using Model #1)", state="complete", expanded=False)
                            final_json = res_1

                        elif res_1:
                            final_json = res_1
                            st.session_state.verification_status = "SINGLE"
                            status.update(label="✅ Analysis complete (Model #1 succeeded)", state="complete", expanded=False)

                        elif res_2:
                            final_json = res_2
                            st.session_state.verification_status = "SINGLE"
                            status.update(label="✅ Analysis complete (Model #2 succeeded)", state="complete", expanded=False)

                        else:
                            status.update(label="❌ AI extraction failed on both models", state="error")

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

            # Display verification banner
            if st.session_state.verification_status == "MATCH":
                st.success("✅ **Dual AI Verification Passed:** Both models returned identical results.")
            elif st.session_state.verification_status == "MISMATCH":
                st.warning("⚠️ **Dual AI Notice:** Minor discrepancies detected between models. Using primary model output.")

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
            rec_resp = supabase.table("receipts").select("*, receipt_items(*)").execute()
        elif active_user_id:
            rec_resp = supabase.table("receipts").select("*, receipt_items(*)").eq("user_id", active_user_id).execute()
        else:
            rec_resp = None
    except Exception:
        if st.session_state.admin and not active_user_id:
            rec_resp = supabase.table("receipts").select("*").execute()
        elif active_user_id:
            rec_resp = supabase.table("receipts").select("*").eq("user_id", active_user_id).execute()
        else:
            rec_resp = None

    if rec_resp and rec_resp.data:
        df = pd.DataFrame(rec_resp.data)
        
        if "receipt_items" in df.columns:
            df["items"] = df["receipt_items"].apply(
                lambda items: ", ".join([f"{i.get('item_name')} (${i.get('price')})" for i in items]) if isinstance(items, list) else ""
            )

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
