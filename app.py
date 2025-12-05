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
st.set_page_config(page_title="股務管理系統 (旗艦完整版)", layout="wide")

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
            # 定義權限 Scope (包含 Sheet, Drive, Cloud Platform)
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
            
            # 嘗試連線 logs 分頁，若無則建立或忽略
            try: self.ws_log = self.sh.worksheet("change_logs")
            except: self.ws_log = None

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
                data = []
                if table_name == "shareholders": data = self.ws_sh.get_all_records()
                elif table_name == "transactions": data = self.ws_tx.get_all_records()
                elif table_name == "requests": data = self.ws_req.get_all_records()
                elif table_name == "logs" and self.ws_log: data = self.ws_log.get_all_records()
                
                df = pd.DataFrame(data)
                # 自動去除欄位空白
                if not df.empty: df.columns = df.columns.str.strip()
                return df
            except APIError: time.sleep(1)
        return pd.DataFrame()

    # --- 圖片上傳 Google Drive ---
    def upload_image_to_drive(self, file_obj, filename):
        try:
            query = "name='StockSystem_Images' and mimeType='application/vnd.google-apps.folder' and trashed=false"
            results = self.drive_service.files().list(q=query, fields="files(id)").execute()
            files = results.get('files', [])
            
            if not files:
                file_metadata = {'name': 'StockSystem_Images', 'mimeType': 'application/vnd.google-apps.folder'}
                folder = self.drive_service.files().create(body=file_metadata, fields='id').execute()
                folder_id = folder.get('id')
            else:
                folder_id = files[0]['id']

            file_metadata = {'name': filename, 'parents': [folder_id]}
            media = MediaIoBaseUpload(file_obj, mimetype=file_obj.type, resumable=True)
            file = self.drive_service.files().create(body=file_metadata, media_body=media, fields='id, webViewLink').execute()
            self.drive_service.permissions().create(fileId=file.get('id'), body={'role': 'reader', 'type': 'anyone'}).execute()
            return file.get('webViewLink')
        except Exception as e:
            return None

    # --- OCR 辨識 (優化版) ---
    def ocr_id_card(self, content):
        try:
            image = vision.Image(content=content)
            response = self.vision_client.text_detection(image=image)
            texts = response.text_annotations
            
            if not texts: return False, "❌ 無法辨識任何文字，請確認照片方向正確且未反光。"

            full_text = texts[0].description
            name, address = "", ""
            
            if "身分證" not in full_text and "中華民國" not in full_text:
                return False, "⚠️ 這看起來不像身分證，請重新拍攝。"

            name_match = re.search(r"姓名\s*([^\n]+)", full_text)
            if name_match: name = name_match.group(1).strip()
            
            lines = full_text.split('\n')
            for line in lines:
                if any(x in line for x in ['縣', '市', '區', '路', '街', '號']):
                    if "戶政事務所" not in line and len(line) > 8:
                        address = line.strip()
                        break
            
            if not name and not address:
                return False, "⚠️ 影像太模糊或被遮擋，無法讀取關鍵資料。"
                
            return True, {"name": name, "address": address}
        except Exception as e:
            return False, f"系統錯誤: {str(e)}"

    # --- 資料更新 (含 Log) ---
    def update_shareholder_profile(self, editor, tax_id, new_data):
        try:
            cell = self.ws_sh.find(tax_id, in_column=1)
            if not cell: return False, "找不到資料"
            headers = self.ws_sh.row_values(1)
            headers = [h.strip() for h in headers] # 清理 Header
            
            old_row = self.ws_sh.row_values(cell.row)
            while len(old_row) < len(headers): old_row.append("")
            current_data = dict(zip(headers, old_row))
            changes = []
            
            # 欄位對應: Sheet Header -> new_data Key
            for key, val in new_data.items():
                if key in headers:
                    new_val = str(val)
                    old_val = str(current_data.get(key, ""))
                    if new_val != old_val:
                        changes.append([datetime.now().strftime("%Y-%m-%d %H:%M:%S"), editor, tax_id, key, old_val, new_val])
                        col_idx = headers.index(key) + 1
                        self.ws_sh.update_cell(cell.row, col_idx, new_val)
            
            if changes:
                if self.ws_log: self.ws_log.append_rows(changes)
                return True, f"已更新 {len(changes)} 個欄位"
            else:
                return True, "資料無變更" # 即使無文字變更，圖片上傳成功也會視為成功
        except Exception as e: return False, str(e)

    # --- 批次匯入 (極速版) ---
    def batch_import_from_excel(self, df_excel, replace_shares=False):
        try:
            current = self.ws_sh.get_all_records()
            db_map = {str(item['tax_id']).strip(): item for item in current}
            cnt = 0
            for i, row in df_excel.iterrows():
                tid = str(row.get("身分證或統編", "")).strip()
                if not tid: continue
                new_info = {
                    'name': str(row.get("姓名", "")).strip(),
                    'holder_type': "Corporate" if "法人" in str(row.get("身分別", "")) else "Individual",
                    'representative': str(row.get("代表人", "")),
                    'household_address': str(row.get("戶籍地址", row.get("地址", ""))),
                    'mailing_address': str(row.get("通訊地址", row.get("地址", ""))),
                    'email': str(row.get("Email", "")),
                    'password_hint': str(row.get("密碼提示", ""))
                }
                shares = 0
                try: shares = int(row.get("持股數") or row.get("初始持股數") or 0)
                except: pass

                if tid in db_map:
                    db_map[tid].update(new_info)
                    if shares >= 0:
                        if replace_shares: db_map[tid]['shares_held'] = shares
                        else: db_map[tid]['shares_held'] = int(db_map[tid].get('shares_held') or 0) + shares
                else:
                    new_info.update({'tax_id': tid, 'shares_held': shares, 'password': "", 'phone': "", 'id_image_url': ""})
                    db_map[tid] = new_info
                cnt += 1
            
            final_data = []
            headers = ["tax_id", "name", "holder_type", "representative", "household_address", "mailing_address", "phone", "email", "password_hint", "shares_held", "password", "id_image_url"]
            for k, v in db_map.items():
                final_data.append([v.get(h, "") for h in headers])
            
            self.ws_sh.clear()
            self.ws_sh.append_row(headers)
            self.ws_sh.append_rows(final_data)
            return True, f"匯入成功，共處理 {cnt} 筆"
        except Exception as e: return False, str(e)

    # --- 申請與交易 ---
    def add_request(self, applicant_id, amount, reason):
        try:
            cell = self.ws_sh.find(applicant_id, in_column=1)
            # Col 10 is shares_held
            curr = int(self.ws_sh.cell(cell.row, 10).value or 0) 
            
            reqs = self.ws_req.get_all_records()
            pending = sum([int(r['amount']) for r in reqs if str(r['applicant'])==str(applicant_id) and r['status']=='Pending'])
            
            available = curr - pending
            if amount > available: return False, f"股數不足 (可用: {available})"
            
            rid = int(time.time())
            self.ws_req.append_row([rid, datetime.now().strftime("%Y-%m-%d"), applicant_id, "", amount, "Pending", reason, ""])
            return True, "已送出"
        except Exception as e: return False, str(e)

    def approve_request(self, req_id, date, s_id, b_id, amount):
        try:
            if not self.transfer_shares(date, s_id, b_id, amount, "交易申請"): return False, "過戶失敗"
            cell = self.ws_req.find(str(req_id), in_column=1)
            if cell:
                self.ws_req.update_cell(cell.row, 4, b_id) # Target
                self.ws_req.update_cell(cell.row, 6, "Approved")
            return True, "已核准"
        except Exception as e: return False, str(e)

    def reject_request(self, req_id, reason):
        try:
            cell = self.ws_req.find(str(req_id), in_column=1)
            if cell:
                self.ws_req.update_cell(cell.row, 6, "Rejected")
                self.ws_req.update_cell(cell.row, 8, reason)
            return True, "已退件"
        except Exception as e: return False, str(e)

    def delete_request(self, req_id):
        try:
            cell = self.ws_req.find(str(req_id), in_column=1)
            if cell and self.ws_req.cell(cell.row, 6).value == "Pending":
                self.ws_req.delete_rows(cell.row)
                return True, "已撤銷"
            return False, "無法撤銷"
        except: return False, "Error"

    def transfer_shares(self, date, s_id, b_id, amount, reason):
        try:
            s_cell = self.ws_sh.find(s_id, in_column=1)
            b_cell = self.ws_sh.find(b_id, in_column=1)
            if not s_cell or not b_cell: return False, "找不到買賣方"
            
            s_shares = int(self.ws_sh.cell(s_cell.row, 10).value or 0)
            b_shares = int(self.ws_sh.cell(b_cell.row, 10).value or 0)
            
            if s_shares < amount: return False, "股數不足"
            
            self.ws_sh.update_cell(s_cell.row, 10, s_shares - amount)
            self.ws_sh.update_cell(b_cell.row, 10, b_shares + amount)
            self.ws_tx.append_row([str(date), s_id, b_id, amount, reason])
            return True, "成功"
        except Exception as e: return False, str(e)

    # --- 基本功能 (Upsert, Issue, Delete, Verify) ---
    def upsert_shareholder(self, tax_id, name, holder_type, address, representative, email, hint):
        try:
            tax_id = str(tax_id).strip()
            if not hint: hint = "無提示"
            try: cell = self.ws_sh.find(tax_id)
            except: time.sleep(1); cell = self.ws_sh.find(tax_id)
            
            row_data = [tax_id, name, holder_type, representative, address, address, "", email, hint, 0, "", ""]
            
            if cell: return False, "股東已存在"
            else: self.ws_sh.append_row(row_data)
            return True, "新增成功"
        except Exception as e: return False, str(e)

    def issue_shares(self, tax_id, amount):
        try:
            cell = self.ws_sh.find(tax_id, in_column=1)
            curr = int(self.ws_sh.cell(cell.row, 10).value or 0)
            self.ws_sh.update_cell(cell.row, 10, curr + amount)
        except: pass

    def delete_shareholder(self, tax_id):
        try:
            cell = self.ws_sh.find(tax_id, in_column=1)
            self.ws_sh.delete_rows(cell.row)
        except: pass
        
    def delete_batch_shareholders(self, ids):
        for i in ids: self.delete_shareholder(i)
        return True, "已刪除"

    def get_shareholder_detail(self, tax_id):
        try:
            records = self.ws_sh.get_all_records()
            for r in records:
                if str(r['tax_id']) == str(tax_id): return r
            return None
        except: return None

    def verify_login(self, username, password, is_admin):
        try:
            ws = self.ws_adm if is_admin else self.ws_sh
            try: cell = ws.find(username, in_column=1)
            except: time.sleep(1); cell = ws.find(username, in_column=1)
            if not cell: return False, "無此帳號", None
            row = ws.row_values(cell.row)
            if is_admin:
                p = row[1]; h = row[3] if len(row)>3 else ""; n = "管理員"
            else:
                n = row[1]; h = row[8] if len(row)>8 else ""; p = row[10] if len(row)>10 else ""
                if p=="": p = username
            if str(p)==str(password): return True, n, None
            else: return False, "密碼錯誤", h
        except Exception as e: return False, str(e), None

    def get_user_recovery_info(self, user_id, is_admin=False):
        try:
            ws = self.ws_adm if is_admin else self.ws_sh
            cell = ws.find(user_id, in_column=1)
            if cell:
                row_vals = ws.row_values(cell.row)
                if is_admin:
                    email = row_vals[2] if len(row_vals)>2 else ""
                    hint = row_vals[3] if len(row_vals)>3 else ""
                    pwd = row_vals[1]
                else:
                    email = row_vals[7] if len(row_vals)>7 else ""
                    hint = row_vals[8] if len(row_vals)>8 else ""
                    pwd = row_vals[10] if len(row_vals)>10 and row_vals[10]!="" else user_id
                return {"email": email, "hint": hint, "password": pwd}
            return None
        except: return None

    def update_password(self, uid, pwd, hint, admin=False):
        try:
            ws = self.ws_adm if admin else self.ws_sh
            cell = ws.find(uid, in_column=1)
            if cell:
                r = cell.row
                if admin: ws.update_cell(r, 2, pwd); ws.update_cell(r, 4, hint)
                else: ws.update_cell(r, 11, pwd); ws.update_cell(r, 9, hint)
                return True
            return False
        except: return False

@st.cache_resource
def get_db_system(): return GoogleServices()
try: sys = get_db_system()
except: st.error("連線逾時"); st.stop()

# --- UI Components ---
@st.dialog("📝 編輯個人資料")
def show_profile_edit_dialog(user_data):
    st.info("請更新您的資料，若有上傳身分證，系統將自動進行辨識。")
    with st.expander("📸 拍攝指南 (點擊展開)"):
        st.markdown("1. 光線充足 2. 避免反光 3. 填滿畫面 4. 水平拍攝")

    with st.form("profile_form"):
        col1, col2 = st.columns(2)
        if "ocr_name" not in st.session_state: st.session_state.ocr_name = user_data['name']
        if "ocr_addr" not in st.session_state: st.session_state.ocr_addr = str(user_data.get('household_address', ''))

        new_name = col1.text_input("姓名", value=st.session_state.ocr_name)
        new_phone = col2.text_input("手機", value=str(user_data.get('phone', '')))
        new_h_addr = st.text_input("戶籍地址", value=st.session_state.ocr_addr)
        new_m_addr = st.text_input("通訊地址", value=str(user_data.get('mailing_address', '')))
        new_email = st.text_input("Email", value=str(user_data.get('email', '')))
        
        st.markdown("---")
        st.write("🆔 身分證")
        img_method = st.radio("方式", ["上傳", "相機"], horizontal=True)
        img_file = st.file_uploader("檔案", type=['jpg','png']) if img_method=="上傳" else st.camera_input("拍照")
        
        if img_file: st.image(img_file, width=200)

        c_ocr, c_save = st.columns([1,1])
        do_ocr = c_ocr.form_submit_button("🔍 辨識")
        do_save = c_save.form_submit_button("💾 儲存", type="primary")

        if do_ocr:
            if not img_file: st.error("請先選擇圖片")
            else:
                with st.spinner("分析中..."):
                    succ, res = sys.ocr_id_card(img_file.getvalue())
                    if succ:
                        st.success("辨識成功，請手動確認資料")
                        st.info(f"姓名: {res['name']}"); st.info(f"地址: {res['address']}")
                    else: st.error(res)

        if do_save:
            ud = {'name': new_name, 'phone': new_phone, 'household_address': new_h_addr, 'mailing_address': new_m_addr, 'email': new_email}
            if img_file:
                with st.spinner("上傳中..."):
                    link = sys.upload_image_to_drive(img_file, f"{user_data['tax_id']}_{int(time.time())}.jpg")
                    if link: ud['id_image_url'] = link
            succ, msg = sys.update_shareholder_profile(st.session_state.user_name, user_data['tax_id'], ud)
            if succ:
                st.success(msg)
                if "ocr_name" in st.session_state: del st.session_state.ocr_name
                if "ocr_addr" in st.session_state: del st.session_state.ocr_addr
                time.sleep(1.5); st.rerun()
            else: st.error(msg)

@st.dialog("✍️ 提出交易申請")
def show_request_dialog(applicant_id, current_shares, pending_shares):
    st.info(f"持有: {current_shares} | 凍結: {pending_shares}")
    available = current_shares - pending_shares
    with st.form("req"):
        amt = st.number_input("股數", min_value=1, max_value=available if available>0 else 1)
        rsn = st.text_input("原因")
        if st.form_submit_button("送出"):
            if available <= 0 or amt > available: st.error("額度不足")
            elif not rsn: st.error("請填寫原因")
            else:
                s, m = sys.add_request(applicant_id, amt, rsn)
                if s: st.success(m); time.sleep(1); st.rerun()
                else: st.error(m)

@st.dialog("📋 核定")
def show_approve_dialog(req_data, user_list):
    st.write(f"申請人: {req_data['applicant']}, 股數: {req_data['amount']}")
    with st.form("appr"):
        opts = [x for x in user_list if x.split(" | ")[0] != str(req_data['applicant'])]
        target = st.selectbox("買方", opts)
        if st.form_submit_button("✅ 確認"):
            s, m = sys.approve_request(req_data['id'], datetime.today().strftime("%Y-%m-%d"), req_data['applicant'], target.split(" | ")[0], req_data['amount'])
            if s: st.success(m); time.sleep(1); st.rerun()
            else: st.error(m)

@st.dialog("❌ 退件")
def show_reject_dialog(req_id):
    with st.form("rej"):
        r = st.text_input("原因")
        if st.form_submit_button("確認"):
            s, m = sys.reject_request(req_id, r)
            if s: st.success(m); time.sleep(1); st.rerun()
            else: st.error(m)

@st.dialog("🗑️ 刪除申請")
def show_cancel_request_dialog(req_id):
    st.warning("撤銷申請？")
    if st.button("確認"):
        s, m = sys.delete_request(req_id)
        if s: st.success(m); time.sleep(1); st.rerun()
        else: st.error(m)

def send_recovery_email(to, uid, pwd):
    if not SENDER_EMAIL: return True, "模擬發送"
    try:
        msg=MIMEText(f"Pw:{pwd}"); msg['To']=to; s=smtplib.SMTP(SMTP_SERVER,SMTP_PORT); s.starttls(); s.login(SENDER_EMAIL,SENDER_PASSWORD); s.send_message(msg); s.quit(); return True,"OK"
    except: return False,"Err"

@st.dialog("🔑 忘記密碼")
def show_forgot_password_dialog():
    u = st.text_input("帳號")
    if st.button("查詢"):
        i = sys.get_user_recovery_info(u, u=="admin")
        if i:
            st.success(f"提示: {i['hint']}")
            if i['email'] and st.button("寄送"): send_recovery_email(i['email'],u,i['password'])
        else: st.error("無")

@st.dialog("🔑 修改密碼")
def show_password_dialog(role, uid):
    with st.form("p"):
        p1=st.text_input("新密碼",type="password"); p2=st.text_input("確認",type="password"); h=st.text_input("提示")
        if st.form_submit_button("修改"):
            if p1==p2 and h: sys.update_password(uid, p1, h, role=="admin"); st.success("OK"); time.sleep(1); st.session_state.logged_in=False; st.rerun()

# --- Main App ---
def run_main_app(role, user_name, user_id):
    with st.sidebar:
        st.markdown(f"### 👋 {user_name}")
        if st.button("密碼"): show_password_dialog(role, user_id)
        if st.button("登出"): st.session_state.logged_in=False; st.rerun()
        
        if role == "admin":
            # 確保這裡列出了所有功能
            menu = st.radio("選單", ["📊 股東名簿總覽", "✅ 審核交易申請", "📂 批次匯入", "➕ 新增股東", "💰 發行/增資", "🤝 股權過戶", "📝 交易歷史", "📝 修改紀錄查詢"])
        else:
            menu = st.radio("選單", ["👤 個人資料維護", "📝 我的持股", "📜 交易紀錄查詢", "✍️ 申請交易"])

    st.title("🏢 股務管理系統")

    if role == "admin":
        if menu == "📊 股東名簿總覽":
            df = sys.get_df("shareholders")
            if not df.empty and 'shares_held' in df.columns:
                total = pd.to_numeric(df['shares_held'], errors='coerce').fillna(0).sum()
                st.metric("總股數", f"{total:,}")
                st.dataframe(df)
            else: st.info("無資料")
            
        elif menu == "✅ 審核交易申請":
            df = sys.get_df("requests")
            if not df.empty and "status" in df.columns:
                pending = df[df["status"]=="Pending"]
                st.dataframe(pending)
                if not pending.empty:
                    st.divider()
                    users = sys.get_df("shareholders")
                    ulist = [f"{r['tax_id']} | {r['name']}" for i,r in users.iterrows()]
                    for i, r in pending.iterrows():
                        c1, c2, c3 = st.columns([3, 1, 1])
                        c1.write(f"申請人: {r['applicant']}, 股數: {r['amount']}")
                        if c2.button("核准", key=f"ok_{r['id']}"): show_approve_dialog(r, ulist)
                        if c3.button("退件", key=f"no_{r['id']}"): show_reject_dialog(r['id'])
            else: st.info("無申請")
        
        elif menu == "📂 批次匯入":
            st.header("批次匯入")
            replace = st.checkbox("覆寫股數")
            up = st.file_uploader("Excel", type=["xlsx"])
            if up and st.button("匯入"):
                s, m = sys.batch_import_from_excel(pd.read_excel(up), replace)
                if s: st.success(m)
                else: st.error(m)
        
        elif menu == "➕ 新增股東":
            with st.form("add"):
                t = st.text_input("統編"); n = st.text_input("姓名")
                if st.form_submit_button("新增"):
                    sys.upsert_shareholder(t, n, "Individual", "", "", "", "")
                    st.success("成功")
        
        elif menu == "💰 發行/增資":
            df = sys.get_df("shareholders")
            ops = [f"{r['tax_id']} | {r['name']}" for i,r in df.iterrows()]
            t = st.selectbox("對象", ops); a = st.number_input("股數", min_value=1)
            if st.button("發行"): sys.issue_shares(t.split(" | ")[0], a); st.success("OK")
        
        elif menu == "🤝 股權過戶":
            df = sys.get_df("shareholders")
            ops = [f"{r['tax_id']} | {r['name']}" for i,r in df.iterrows()]
            s = st.selectbox("賣", ops); b = st.selectbox("買", ops); a = st.number_input("股數", min_value=1)
            if st.button("過戶"): 
                msg = sys.transfer_shares(datetime.today(), s.split(" | ")[0], b.split(" | ")[0], a, "Admin")
                if "成功" in msg: st.success(msg)
                else: st.error(msg)
        
        elif menu == "📝 交易歷史":
            st.dataframe(sys.get_df("transactions"))
        
        elif menu == "📝 修改紀錄查詢":
            df = sys.get_df("logs")
            if not df.empty:
                u = st.selectbox("篩選", ["全部"] + list(set(df['target_user'])))
                if u != "全部": df = df[df['target_user']==u]
                st.dataframe(df)
            else: st.info("無紀錄")

    else:
        if menu == "👤 個人資料維護":
            my = sys.get_shareholder_detail(user_id)
            if my:
                if my.get('id_image_url'): st.image(my['id_image_url'], width=300)
                st.write(f"姓名: {my['name']}, 統編: {my['tax_id']}")
                if st.button("編輯"): show_profile_edit_dialog(my)
        elif menu == "📝 我的持股":
            df = sys.get_df("shareholders")
            r = df[df['tax_id'].astype(str)==str(user_id)]
            if not r.empty:
                row = r.iloc[0]
                st.metric("股數", f"{row['shares_held']:,}")
                st.write(f"Email: {row['email']}")
        elif menu == "📜 交易紀錄查詢":
            df = sys.get_df("transactions")
            if not df.empty:
                my = df[(df['seller_tax_id'].astype(str)==str(user_id)) | (df['buyer_tax_id'].astype(str)==str(user_id))]
                st.dataframe(my)
        elif menu == "✍️ 申請交易":
            st.header("申請轉讓")
            df_sh = sys.get_df("shareholders")
            me = df_sh[df_sh['tax_id'].astype(str) == str(user_id)]
            if not me.empty:
                my_shares = int(me.iloc[0]['shares_held'] or 0)
                df_req = sys.get_df("requests")
                pending = 0
                if not df_req.empty and "applicant" in df_req.columns:
                    reqs = df_req[df_req['applicant'].astype(str)==str(user_id)]
                    pending = reqs[reqs['status']=="Pending"]['amount'].sum()
                
                if st.button("填寫申請"): show_request_dialog(user_id, my_shares, pending)
                st.divider()
                st.write("申請紀錄")
                if not df_req.empty:
                    my_h = df_req[df_req['applicant'].astype(str)==str(user_id)]
                    st.dataframe(my_h)

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
                if acc == "admin":
                    v, m, h = sys.verify_login(acc, pwd, True)
                    if v: st.session_state.logged_in=True; st.session_state.user_role="admin"; st.session_state.user_name=m; st.session_state.user_id=acc; st.rerun()
                    else: st.error(m)
                else:
                    v, m, h = sys.verify_login(acc, pwd, False)
                    if v: st.session_state.logged_in=True; st.session_state.user_role="shareholder"; st.session_state.user_name=m; st.session_state.user_id=acc; st.rerun()
                    else: st.error(m); st.info(f"提示: {h}") if h else None
            if st.button("忘記密碼"): show_forgot_password_dialog()
    else:
        run_main_app(st.session_state.user_role, st.session_state.user_name, st.session_state.user_id)
