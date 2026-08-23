import base64
import io
import json
import re
import uuid
import datetime
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

# Helper function to convert PIL Image to Base64
def encode_image(img):
    buffered = io.BytesIO()
    img.save(buffered, format="JPEG")
    return base64.b64encode(buffered.getvalue()).decode("utf-8")

# Initialize session states
if "admin" not in st.session_state:
    st.session_state.admin = None
if "extracted_subtotal" not in st.session_state:
    st.session_state.extracted_subtotal = 0.0
if "extracted_extra" not in st.session_state:
    st.session_state.extracted_extra = 0.0
if "extracted_total" not in st.session_state:
    st.session_state.extracted_total = 0.0
if "extracted_items" not in st.session_state:
    st.session_state.extracted_items = ""
if "processed_file_id" not in st.session_state:
    st.session_state.processed_file_id = None

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
                    st.error("⚠️ OPENROUTER_API_KEY is missing in Streamlit Cloud Secrets!")
                else:
                    with st.spinner("Analyzing receipt items & totals with Vision AI..."):
                        base64_image = encode_image(image)
                        
                        prompt = """
                        Analyze this receipt image carefully. Extract:
                        1. items: List all purchased items with prices (e.g. ["Milk - $3.50", "Bread - $2.00"])
                        2. subtotal: Pre-tax/fee total (numeric)
                        3. extra_amount: Tax, tip, or service fees (numeric, 0.0 if none)
                        4. total_amount: Final grand total (numeric)

                        Return ONLY valid JSON matching this exact structure:
                        {
                            "items": ["Item 1 - $5.00", "Item 2 - $3.00"],
                            "subtotal": 8.00,
                            "extra_amount": 0.80,
                            "total_amount": 8.80
                        }
                        """

                        try:
                            ai_client = OpenAI(
                                base_url="https://openrouter.ai/api/v1",
                                api_key=st.secrets["OPENROUTER_API_KEY"]
                            )

                            response = ai_client.chat.completions.create(
                                model="openrouter/free",
                                extra_headers={
                                    "HTTP-Referer": "https://streamlit.io",
                                    "X-Title": "Receipt Manager"
                                },
                                messages=[
                                    {
                                        "role": "user",
                                        "content": [
                                            {"type": "text", "text": prompt},
                                            {
                                                "type": "image_url",
                                                "image_url": {
                                                    "url": f"data:image/jpeg;base64,{base64_image}"
                                                }
                                            }
                                        ]
                                    }
                                ]
                            )

                            ai_text = response.choices[0].message.content
                            json_match = re.search(r"\{.*\}", ai_text, re.DOTALL)
                            
                            if json_match:
                                extracted = json.loads(json_match.group(0))
                                items_list = extracted.get("items", [])
                                items_formatted = "\n".join(items_list) if isinstance(items_list, list) else str(items_list)
                                
                                st.session_state.extracted_subtotal = float(extracted.get("subtotal", 0.0))
                                st.session_state.extracted_extra = float(extracted.get("extra_amount", 0.0))
                                st.session_state.extracted_total = float(extracted.get("total_amount", 0.0))
                                st.session_state.extracted_items = items_formatted
                                st.session_state.processed_file_id = file_id
                                st.rerun()

                        except Exception as e:
                            st.error(f"AI extraction error: {e}")

            st.write("**Verify Data:**")
            with st.form("receipt_form"):
                rec_date = st.date_input("Date", value=datetime.date.today())
                subtotal = st.number_input("Subtotal ($)", value=st.session_state.extracted_subtotal, step=0.01)
                extra_amount = st.number_input("Extra Amount / Tax / Tip ($)", value=st.session_state.extracted_extra, step=0.01)
                total_amount = st.number_input("Total Amount ($)", value=st.session_state.extracted_total, step=0.01)
                items_text = st.text_area("Products Bought", value=st.session_state.extracted_items, height=120)
                
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
                        "extra_amount": extra_amount,
                        "items": items_text,
                        "image_url": public_url
                    }
                    supabase.table("receipts").insert(receipt_payload).execute()
                    st.success("Receipt saved!")
                    
                    # Reset state
                    st.session_state.processed_file_id = None
                    st.session_state.extracted_subtotal = 0.0
                    st.session_state.extracted_extra = 0.0
                    st.session_state.extracted_total = 0.0
                    st.session_state.extracted_items = ""
                    st.rerun()
                except Exception as err:
                    st.error(f"Save error: {err}")

        st.divider()

    # 2. SPREADSHEET VIEWER
    st.subheader("2. Ranked Receipts Spreadsheet")

    if st.session_state.admin and not active_user_id:
        rec_resp = supabase.table("receipts").select("*").execute()
    elif active_user_id:
        rec_resp = supabase.table("receipts").select("*").eq("user_id", active_user_id).execute()
    else:
        rec_resp = None

    if rec_resp and rec_resp.data:
        df = pd.DataFrame(rec_resp.data)
        df["Rank"] = df["total_amount"].rank(ascending=False, method="min").astype(int)
        df = df.sort_values(by="Rank")

        display_cols = ["Rank", "user_id", "date", "items", "subtotal", "extra_amount", "total_amount", "created_at", "image_url"]
        df_display = df[[c for c in display_cols if c in df.columns]]

        gb = GridOptionsBuilder.from_dataframe(df_display)
        gb.configure_pagination(paginationAutoPageSize=True)
        gb.configure_default_column(editable=True, groupable=True)
        gridOptions = gb.build()

        AgGrid(df_display, gridOptions=gridOptions, height=350, theme="alpine")
    else:
        st.info("No receipts found for this view.")
else:
    st.info("Select or add a user profile above, or log in as Admin using the sidebar menu.")
