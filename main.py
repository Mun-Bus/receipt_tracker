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

# Initialize OpenRouter Client (OpenAI compatible)
ai_client = OpenAI(
    base_url="https://openrouter.ai/api/v1",
    api_key=st.secrets["OPENROUTER_API_KEY"]
)

# Helper function to convert PIL Image to Base64
def encode_image(img):
    buffered = io.BytesIO()
    img.save(buffered, format="JPEG")
    return base64.b64encode(buffered.getvalue()).decode("utf-8")

# Initialize session state for Admin Auth
if "admin" not in st.session_state:
    st.session_state.admin = None

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

            with st.spinner("Analyzing receipt with Vision AI..."):
                base64_image = encode_image(image)
                default_subtotal = 0.0
                default_total = 0.0

                try:
                    response = ai_client.chat.completions.create(
                        model="openrouter/free",
                        messages=[
                            {
                                "role": "user",
                                "content": [
                                    {
                                        "type": "text",
                                        "text": "Analyze this receipt. Extract subtotal and total_amount. Return ONLY valid JSON format like this: {\"subtotal\": 12.50, \"total_amount\": 15.00}"
                                    },
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
                        extracted_data = json.loads(json_match.group(0))
                        default_subtotal = float(extracted_data.get("subtotal", 0.0))
                        default_total = float(extracted_data.get("total_amount", 0.0))

                except Exception as e:
                    st.error(f"AI extraction error: {e}")

            st.write("**Verify Data:**")
            with st.form("receipt_form"):
                rec_date = st.date_input("Date", value=datetime.date.today())
                subtotal = st.number_input("Subtotal ($)", value=default_subtotal, step=0.01)
                total_amount = st.number_input("Total Amount ($)", value=default_total, step=0.01)
                extra_amount = st.number_input("Extra Amount ($)", value=0.0, step=0.01)
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
                        "image_url": public_url
                    }
                    supabase.table("receipts").insert(receipt_payload).execute()
                    st.success("Receipt saved!")
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

        display_cols = ["Rank", "user_id", "date", "subtotal", "extra_amount", "total_amount", "created_at", "image_url"]
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
