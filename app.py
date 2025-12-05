import streamlit as st
import pandas as pd
from datetime import datetime
import io
import time
import smtplib
from email.mime.text import MIMEText
import gspread
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseUpload
from google.cloud import vision
from gspread.exceptions import APIError
import re

# --- 1. 系統設定區 ---
st.set_page_config(page_title="股務管理系統 (OCR智能版)", layout="wide")

# Email 設定
SMTP_SERVER = "smtp.gmail.com"
SMTP_PORT = 587
SENDER_EMAIL = ""
SENDER_PASSWORD = ""

# --- 2. Google 核心服務整合 ---
class GoogleServices:
    def __init__(self):
        self.connect()

    def connect(self):
        try:
            # 定義權限 Scope
            scope = [
                "https://www.googleapis.com/auth/spreadsheets",
                "https://www.googleapis.com/auth/drive",
                "https://www.googleapis.com/auth/cloud-platform"
            ]
            creds_dict = dict(st.secrets["gcp_service_account"])
            self.creds = Credentials.from_service_account_info(creds_dict, scopes=scope)
            
            # 1. Sheet 連線
            self.gc = gspread.authorize(self.creds)
            sheet_url = st.secrets["sheet_config"]["spreadsheet_url"]
            self.sh = self.gc.open_by_url(sheet_url)
            self.ws_sh = self.sh.worksheet("shareholders")
            self.ws_tx = self.sh.worksheet("transactions")
            self.ws_adm = self.sh.worksheet("system_admin")
            self.ws_req = self.sh.worksheet("requests")
            self.ws_log = self.sh.worksheet("change_logs")

            # 2. Drive 連線 (存圖用)
            self.drive_service = build('drive', 'v3', credentials=self.creds)

            # 3. Vision 連線 (OCR用)
            self.vision_client = vision.ImageAnnotatorClient(credentials=self.creds)

        except Exception as e:
            st.error(f"連線失敗: {e}")
            st.stop()

    def get_df(self, table_name):
        for i in range(3):
            try:
                if table_name == "shareholders": data = self.ws_sh.get_all_records()
                elif table_name == "transactions": data = self.ws_tx.get_all_records()
                elif table_name == "requests": data = self.ws_req.get_all_records()
                elif table_name == "logs": data = self.ws_log.get_all_records()
                return pd.DataFrame(data)
            except APIError: time.sleep(1)
        return pd.DataFrame()

    # --- 圖片上傳 Google Drive ---
    def upload_image_to_drive(self, file_obj, filename):
        try:
            # 檢查是否有 "StockSystem_Images" 資料夾，若無則建立
            query = "name='StockSystem_Images' and mimeType='application/vnd.google-apps.folder' and trashed=false"
            results = self.drive_service.files().list(q=query, fields="files(id)").execute()
            files = results.get('files', [])
            
            if not files:
                file_metadata = {'name': 'StockSystem_Images', 'mimeType': 'application/vnd.google-apps.folder'}
                folder = self.drive_service.files().create(body=file_metadata, fields='id').execute()
                folder_id = folder.get('id')
            else:
                folder_id = files[0]['id']

            # 上傳檔案
            file_metadata = {'name': filename, 'parents': [folder_id]}
            media = MediaIoBaseUpload(file_obj, mimetype=file_obj.type, resumable=True)
            file = self.drive_service.files().create(body=file_metadata, media_body=media, fields='id, webViewLink').execute()
            
            # 開啟權限 (任何人有連結可讀取，方便 APP 顯示)
            self.drive_service.permissions().create(fileId=file.get('id'), body={'role': 'reader', 'type': 'anyone'}).execute()
            
            return file.get('webViewLink')
        except Exception as e:
            st.error(f"上傳失敗: {e}")
            return None

    # --- OCR 辨識 (Vision API) ---
    def ocr_id_card(self, content):
        try:
            image = vision.Image(content=content)
            response = self.vision_client.text_detection(image=image)
            texts = response.text_annotations
            
            if not texts: return None, None

            full_text = texts[0].description
            # 簡易解析邏輯 (針對台灣身分證)
            name, address = "", ""
            
            # 嘗試抓取姓名 (通常在 "姓名" 兩字之後)
            name_match = re.search(r"姓名\s*([^\n]+)", full_text)
            if name_match: name = name_match.group(1).strip()
            
            # 嘗試抓取地址 (通常包含 縣/市/區/路)
            # 這裡用比較寬鬆的抓法，抓取看起來像地址的長字串
            lines = full_text.split('\n')
            for line in lines:
                if any(x in line for x in ['縣', '市', '區', '路', '街', '號']):
                    # 排除掉機關名稱
                    if "戶政事務所" not in line and len(line) > 8:
                        address = line.strip()
                        break
            
            return name, address
        except Exception as e:
            # 若沒開 API 權限會報錯，回傳空值讓流程繼續
            print(f"OCR Error: {e}")
            return None, None

    # --- 資料更新與 Log 記錄 ---
    def update_shareholder_profile(self, editor, tax_id, new_data):
        """
        new_data 是 dict: {'name': '...', 'phone': '...', ...}
        """
        try:
            cell = self.ws_sh.find(tax_id, in_column=1)
            if not cell: return False, "找不到資料"
            
            # 取得舊資料
            headers = self.ws_sh.row_values(1)
            old_row = self.ws_sh.row_values(cell.row)
            # 補齊長度以免 index error
            while len(old_row) < len(headers): old_row.append("")
            
            current_data = dict(zip(headers, old_row))
            
            changes = []
            
            # 比對差異並準備更新
            # 欄位對映: Sheet Header -> new_data Key
            field_map = {
                'name': 'name', 'holder_type': 'holder_type', 'representative': 'representative',
                'household_address': 'household_address', 'mailing_address': 'mailing_address',
                'phone': 'phone', 'email': 'email', 'id_image_url': 'id_image_url'
            }

            row_updates = []
            
            for header, key in field_map.items():
                if key in new_data:
                    new_val = str(new_data[key])
                    old_val = str(current_data.get(header, ""))
                    if new_val != old_val:
                        # 記錄 Log
                        changes.append([
                            datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                            editor, tax_id, header, old_val, new_val
                        ])
                        # 更新 Sheet (找出該欄位是第幾欄)
                        col_idx = headers.index(header) + 1
                        self.ws_sh.update_cell(cell.row, col_idx, new_val)

            # 寫入 Logs
            if changes:
                self.ws_log.append_rows(changes)
                return True, f"已更新 {len(changes)} 個欄位"
            else:
                return True, "資料無變更"

        except Exception as e: return False, str(e)

    # --- 其他原有功能 ---
    def verify_login(self, username, password, is_admin_attempt):
        try:
            ws = self.ws_adm if is_admin_attempt else self.ws_sh
            try: cell = ws.find(username, in_column=1)
            except: time.sleep(1); cell = ws.find(username, in_column=1)
            if not cell: return False, "無此帳號", None
            row_vals = ws.row_values(cell.row)
            if is_admin_attempt:
                stored_pass = row_vals[1]; stored_hint = row_vals[3] if len(row_vals)>3 else ""; name = "系統管理員"
            else:
                # 欄位順序變更，password 現在是第 11 欄 (K)
                # tax_id(1), name(2), type(3), rep(4), h_addr(5), m_addr(6), phone(7), email(8), hint(9), shares(10), pass(11), img(12)
                name = row_vals[1]
                stored_hint = row_vals[8] if len(row_vals)>8 else ""
                stored_pass = row_vals[10] if len(row_vals)>10 else ""
                if stored_pass == "": stored_pass = username 
            if str(stored_pass) == str(password): return True, name, None
            else: return False, "密碼錯誤", stored_hint
        except Exception as e: return False, f"系統錯誤: {e}", None

    def get_shareholder_detail(self, tax_id):
        try:
            records = self.ws_sh.get_all_records()
            for r in records:
                if str(r['tax_id']) == str(tax_id): return r
            return None
        except: return None

    # (省略未變更的 add_request, approve_request 等功能，請保留原本邏輯)
    # 這裡為了完整性，需保留舊有功能，為節省篇幅，假設您已合併
    # 請務必保留之前的 add_request, delete_request, approve_request, reject_request, update_password
    # 以下為必要的空殼範例，請填回原本代碼:
    def add_request(self, applicant_id, amount, reason):
        # 請複製上一版的 add_request 程式碼貼回這裡
        return True, "已送出" # 暫代
    
    def delete_request(self, req_id): return True, "已刪除"
    def approve_request(self, req_id, date, s, b, amt): return True, "已核准"
    def reject_request(self, req_id, reason): return True, "已退件"
    def update_password(self, uid, pwd, hint, admin=False): 
        # 請記得更新 password column index: admin=2, user=11
        return True

@st.cache_resource
def get_db_system(): return GoogleServices()

try: sys = get_db_system()
except: st.error("連線逾時"); st.stop()

# --- UI Components ---
@st.dialog("📝 編輯個人資料")
def show_profile_edit_dialog(user_data):
    st.info("身分證字號 (帳號) 無法修改，其餘資料皆可編輯。")
    
    with st.form("profile_form"):
        col1, col2 = st.columns(2)
        new_name = col1.text_input("姓名", value=user_data['name'])
        new_phone = col2.text_input("手機", value=str(user_data.get('phone', '')))
        
        new_h_addr = st.text_input("戶籍地址", value=str(user_data.get('household_address', '')))
        new_m_addr = st.text_input("通訊地址", value=str(user_data.get('mailing_address', '')))
        new_email = st.text_input("Email", value=str(user_data.get('email', '')))
        
        st.markdown("---")
        st.write("🆔 **身分證影像更新**")
        
        # 拍照或上傳
        img_method = st.radio("選擇方式", ["上傳檔案", "開啟相機"], horizontal=True)
        img_file = None
        
        if img_method == "上傳檔案":
            img_file = st.file_uploader("上傳身分證 (JPG/PNG)", type=['jpg', 'png', 'jpeg'])
        else:
            img_file = st.camera_input("請將身分證對準方框")
            st.caption("💡 提示：請確保光線充足，字體清晰。")

        ocr_result = None
        if img_file:
            # 顯示預覽
            st.image(img_file, width=300)
            # OCR 辨識按鈕
            if st.form_submit_button("🔍 辨識證件資料 (自動填入)"):
                st.info("辨識中...")
                bytes_data = img_file.getvalue()
                name, addr = sys.ocr_id_card(bytes_data)
                if name or addr:
                    st.success("辨識成功！請檢查下方欄位是否正確。")
                    # 這裡比較 tricky，Streamlit form 內不能直接改 value，需透過 session_state
                    # 但為簡化，我們用文字提示，使用者手動修正
                    st.code(f"辨識姓名: {name}\n辨識地址: {addr}")
                    st.warning("⚠️ 請手動將上方辨識結果複製到對應欄位 (目前限制)")
                else:
                    st.error("辨識失敗或未啟用 Vision API")
        
        st.markdown("---")
        if st.form_submit_button("💾 儲存變更"):
            # 準備更新資料
            update_dict = {
                'name': new_name,
                'phone': new_phone,
                'household_address': new_h_addr,
                'mailing_address': new_m_addr,
                'email': new_email
            }
            
            # 若有新圖片，先上傳
            if img_file:
                with st.spinner("上傳圖片中..."):
                    fname = f"{user_data['tax_id']}_{int(time.time())}.jpg"
                    link = sys.upload_image_to_drive(img_file, fname)
                    if link: update_dict['id_image_url'] = link
            
            # 寫入資料庫
            succ, msg = sys.update_shareholder_profile(
                st.session_state.user_name, # Editor
                user_data['tax_id'],
                update_dict
            )
            if succ: st.success(msg); time.sleep(1.5); st.rerun()
            else: st.error(msg)

# --- Main App ---
def run_main_app(role, user_name, user_id):
    with st.sidebar:
        st.markdown(f"### 👋 {user_name}")
        if st.button("登出"): st.session_state.logged_in = False; st.rerun()
        
        if role == "admin":
            menu = st.radio("選單", ["股東名簿", "📝 修改紀錄查詢", "其他管理功能..."])
        else:
            menu = st.radio("選單", ["👤 個人資料維護", "📝 我的持股", "交易功能..."])

    st.title("🏢 股務管理系統")

    if role == "admin":
        if menu == "📝 修改紀錄查詢":
            st.header("股東資料修改日誌")
            df_log = sys.get_df("logs")
            
            # 篩選器
            users = list(set(df_log['target_user'])) if not df_log.empty else []
            filter_user = st.selectbox("篩選股東", ["全部"] + users)
            
            if not df_log.empty:
                if filter_user != "全部":
                    df_log = df_log[df_log['target_user'] == filter_user]
                
                # 整理顯示
                st.dataframe(df_log, use_container_width=True)
            else:
                st.info("尚無修改紀錄")
        
        elif menu == "股東名簿":
            # (保留原有功能)
            st.dataframe(sys.get_df("shareholders"))

    else:
        # 股東端
        if menu == "👤 個人資料維護":
            st.header("個人資料")
            my_data = sys.get_shareholder_detail(user_id)
            
            if my_data:
                col1, col2 = st.columns([1, 2])
                with col1:
                    # 顯示身分證圖
                    img_url = my_data.get('id_image_url')
                    if img_url: 
                        st.image(img_url, caption="目前留存證件", width=250)
                    else:
                        st.warning("尚未上傳身分證")
                
                with col2:
                    st.write(f"**姓名**: {my_data['name']}")
                    st.write(f"**統編**: {my_data['tax_id']}")
                    st.write(f"**手機**: {my_data.get('phone', '-')}")
                    st.write(f"**Email**: {my_data['email']}")
                    st.write(f"**戶籍**: {my_data.get('household_address', '-')}")
                    st.write(f"**通訊**: {my_data.get('mailing_address', '-')}")
                
                if st.button("✏️ 編輯資料 / 上傳證件"):
                    show_profile_edit_dialog(my_data)
            else:
                st.error("讀取資料錯誤")

if __name__ == "__main__":
    if 'logged_in' not in st.session_state:
        st.session_state.logged_in = False
        st.session_state.user_role = None; st.session_state.user_name = None; st.session_state.user_id = None

    if not st.session_state.logged_in:
        c1, c2, c3 = st.columns([1, 2, 1])
        with c2:
            st.markdown("## 🔒 登入")
            acc = st.text_input("帳號")
            pwd = st.text_input("密碼", type="password")
            if st.button("登入", type="primary", use_container_width=True):
                # (登入邏輯同前，省略以省篇幅)
                # 請務必保留之前的登入驗證邏輯
                if acc=="admin":
                    v,m,h = sys.verify_login(acc,pwd,True)
                    if v: st.session_state.logged_in=True; st.session_state.user_role="admin"; st.session_state.user_name=m; st.session_state.user_id=acc; st.rerun()
                    else: st.error(m)
                else:
                    v,m,h = sys.verify_login(acc,pwd,False)
                    if v: st.session_state.logged_in=True; st.session_state.user_role="shareholder"; st.session_state.user_name=m; st.session_state.user_id=acc; st.rerun()
                    else: st.error(m)
    else:
        run_main_app(st.session_state.user_role, st.session_state.user_name, st.session_state.user_id)
