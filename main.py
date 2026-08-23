import re
import uuid
import pandas as pd
from PIL import Image
import pytesseract
import streamlit as st
from st_aggrid import AgGrid, GridOptionsBuilder
from supabase import create_client

# Page configuration
st.set_page_config(page_title="Receipt Manager", layout="wide")

# Initialize Supabase client using secrets
@st.cache_resource
def init_supabase():
    url = st.secrets["SUPABASE_URL"]
    key = st.secrets["SUPABASE_KEY"]
    return create_client(url, key)

supabase = init_supabase()

# Session State Management for User Auth
if "user" not in st.session_state:
    st.session_state.user = None

# --- AUTHENTICATION SCREEN ---
if not st.session_state.user:
    st.title("Receipt Manager - Login")
    tab1, tab2 = st.tabs(["Sign In", "Sign Up"])
    
    with tab1:
        email = st.text_input("Email", key="login_email")
        password = st.text_input("Password", type="password", key="login_pass")
        if st.button("Log In"):
            try:
                res = supabase.auth.sign_in_with_password({"email": email, "password": password})
                st.session_state.user = res.user
                st.success("Logged in successfully!")
                st.rerun()
            except Exception as e:
                st.error(f"Login failed: {e}")

    with tab2:
        new_email = st.text_input("Email", key="signup_email")
        new_password = st.text_input("Password", type="password", key="signup_pass")
        if st.button("Sign Up"):
            try:
                res = supabase.auth.sign_up({"email": new_email, "password": new_password})
                st.success("Account created! Please log in.")
            except Exception as e:
                st.error(f"Sign up failed: {e}")

# --- MAIN APP INTERFACE ---
else:
    user_id = st.session_state.user.id
    
    # Sidebar Navigation
    st.sidebar.title("Navigation")
    st.sidebar.write(f"Logged in as: **{st.session_state.user.email}**")
    if st.sidebar.button("Logout"):
        supabase.auth.sign_out()
        st.session_state.user = None
        st.rerun()

    st.title("Receipt Manager & Ranking Dashboard")

    # Section 1: Receipt Upload & Processing
    st.subheader("1. Upload & Scan Receipt")
    uploaded_file = st.file_uploader("Choose a receipt photo", type=["jpg", "jpeg", "png"])

    if uploaded_file is not None:
        image = Image.open(uploaded_file)
        st.image(image, caption="Uploaded Receipt", width=300)

        # Run OCR
        with st.spinner("Extracting text from image..."):
            raw_text = pytesseract.image_to_string(image)
            
            # Simple Regex helpers to detect total dollar amounts
            numbers_with_currency = re.findall(r'(\d+[\.,]?\d*)\s*[\$]?', raw_text)
            extracted_amounts = [float(num.replace(',', '.')) for num in numbers_with_currency if num.replace('.', '').isdigit()]
            
            default_total = max(extracted_amounts) if extracted_amounts else 0.0
            default_subtotal = extracted_amounts[0] if len(extracted_amounts) > 1 else default_total

        # Editable form so user can review/verify handwritten OCR errors
        st.write("**Verify Extracted Data:**")
        with st.form("receipt_form"):
            rec_date = st.text_input("Transaction Date", value="August 8")
            subtotal = st.number_input("Subtotal ($)", value=float(default_subtotal), step=1.0)
            total_amount = st.number_input("Total Amount ($)", value=float(default_total), step=1.0)
            extra_amount = st.number_input("Extra Amount / Fee ($)", value=0.0, step=1.0)
            
            submit_button = st.form_submit_button("Save to Database")

        if submit_button:
            try:
                # 1. Save Image to Supabase Storage Bucket
                file_ext = uploaded_file.name.split('.')[-1]
                file_path = f"{user_id}/{uuid.uuid4()}.{file_ext}"
                bytes_data = uploaded_file.getvalue()
                
                supabase.storage.from_("receipts-storage").upload(file_path, bytes_data)
                public_image_url = supabase.storage.from_("receipts-storage").get_public_url(file_path)

                # 2. Insert into `receipts` table
                receipt_payload = {
                    "user_id": user_id,
                    "date": rec_date,
                    "subtotal": subtotal,
                    "total_amount": total_amount,
                    "extra_amount": extra_amount,
                    "image_url": public_image_url
                }
                res = supabase.table("receipts").insert(receipt_payload).execute()
                
                st.success("Receipt saved successfully!")
                st.rerun()
            except Exception as err:
                st.error(f"Error saving receipt: {err}")

    st.divider()

    # Section 2: Interactive Ranked Spreadsheet Viewer
    st.subheader("2. Ranked Receipts Spreadsheet")

    # Fetch user data from database
    response = supabase.table("receipts").select("*").eq("user_id", user_id).execute()
    records = response.data

    if records:
        df = pd.DataFrame(records)

        # Rank receipts automatically by Total Amount (Highest first)
        df["Rank"] = df["total_amount"].rank(ascending=False, method="min").astype(int)
        df = df.sort_values(by="Rank")

        # Organize column display order
        display_cols = ["Rank", "date", "subtotal", "extra_amount", "total_amount", "created_at", "image_url"]
        df_display = df[[c for c in display_cols if c in df.columns]]

        # Interactive AgGrid configuration
        gb = GridOptionsBuilder.from_dataframe(df_display)
        gb.configure_pagination(paginationAutoPageSize=True)
        gb.configure_side_bar()
        gb.configure_default_column(editable=True, groupable=True)
        
        gridOptions = gb.build()

        AgGrid(
            df_display,
            gridOptions=gridOptions,
            enable_enterprise_modules=False,
            height=350,
            theme="alpine"
        )
    else:
        st.info("No receipts found. Upload your first receipt above to populate the spreadsheet!")
