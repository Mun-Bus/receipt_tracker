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
    """Worker function to run receipt extraction via OpenRouter API."""
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
        
        # Clean markdown wrappers if present
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

if st.session_state.admin:
    st.info("⚡ **Admin Mode Active:** You can view master logs for all users.")

active_user_id = user_options.get(selected_name) if selected_name != "-- Select Your Name --" else None

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
                if "OPENROUTER_API_KEY" not in st.secrets or not st.secrets["OPENROUTER_API_KEY"]:
                    st.error("⚠️ OPENROUTER_API_KEY missing in Streamlit Secrets!")
                else:
                    with st.status("🤖 Running dual AI extraction & verification...", expanded=True) as status:
                        status.write("📷 Encoding image for Vision processing...")
                        base64_image = encode_image(image)
                        current_year = datetime.date.today().year

                        prompt = f"""
                        Analyze this receipt image.
                        1. Extract the transaction date printed at the top. Format as YYYY-MM-DD. If year is missing from receipt, infer using current year ({current_year}).
                        2. Extract all items/products with their names and individual prices.
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

                        status.write("⚡ Dispatching Model #1 and Model #2 concurrently...")
                        api_key = st.secrets["OPENROUTER_API_KEY"]
                        
                        # Use free-tier compatible vision endpoints on OpenRouter
                        model_1 = "openrouter/free"
                        model_2 = "google/gemini-2.0-flash-exp:free"

                        with ThreadPoolExecutor(max_workers=2) as executor:
                            future_1 = executor.submit(analyze_receipt, model_1, prompt, base64_image, api_key)
                            future_2 = executor.submit(analyze_receipt, model_2, prompt, base64_image, api_key)

                            status.write("🔍 Model #1 analyzing items and prices...")
                            res_1, err_1 = future_1.result()

                            status.write("🔍 Model #2 cross-checking values...")
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
                                status.update(label="⚠️ Dual AI complete — Discrepancies detected (using Model #1)", state="complete", expanded=False)
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

                    # 1. Insert into 'receipts' table
                    receipt_payload = {
                        "user_id": active_user_id,
                        "date": str(rec_date),
                        "subtotal": subtotal,
                        "total_amount": total_amount,
                        "image_url": public_url
                    }
                    res = supabase.table("receipts").insert(receipt_payload).execute()
                    
                    # Get newly created receipt ID
                    if res.data:
                        receipt_id = res.data[0]["id"]

                        # 2. Insert items into 'receipt_items' child table
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

                    # Reset state for next upload
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
        
        # Format item list from child table for easy viewing
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
