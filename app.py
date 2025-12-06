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
from PIL import Image, ImageEnhance

# --- 1. 系統設定區 ---
st.set_page_config(page_title="股務管理系統 (終極完整版)", layout="wide")

# Email 設定 (若無則使用模擬模式)
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
            # 讀取 Secrets
            creds_dict = dict(st.secrets["gcp_service_account"])
            self.creds = Credentials.from_service_account_info(creds_dict, scopes=scope)
            
            # 1. Sheet 連線
            self.gc = gspread.authorize(self.creds)
            sheet_url = st.secrets["sheet_config"]["spreadsheet_url"]
            self.sh = self.gc.open_by_url(sheet_url)
            
            # 載入所有工作表
            self.ws_sh = self.sh.worksheet("shareholders")
            self.ws_tx = self.sh.worksheet("transactions")
            self.ws_adm = self.sh.worksheet("system_admin")
            self.ws_req = self.sh.worksheet("requests")
            
            # 嘗試連線 logs 分頁，若無則設為 None
            try: self.ws_log = self.sh.worksheet("change_logs")
            except: self.ws_log = None

            # 2. Drive 連線 (存圖用)
            self.drive_service = build('drive', 'v3', credentials=self.creds)

            # 3. Vision 連線 (OCR用)
            self.vision_client = vision.ImageAnnotatorClient(credentials=self.creds)

        except Exception as e:
            st.error(f"連線失敗，請檢查網路或 Secrets 設定: {e}")
            st.stop()

    # --- 讀取資料 (含欄位清理) ---
    def get_df(self, table_name):
        for i in range(3): # 重試機制
            try:
                data = []
                if table_name == "shareholders": data = self.ws_sh.get_all_records()
                elif table_name == "transactions": data = self.ws_tx.get_all_records()
                elif table_name == "requests": data = self.ws_req.get_all_records()
                elif table_name == "logs" and self.ws_log: data = self.ws_log.get_all_records()
                
                df = pd.DataFrame(data)
                # 自動去除欄位名稱的前後空白，避免 KeyError
                if not df.empty: df.columns = df.columns.str.strip()
                return df
            except APIError: time.sleep(1)
        return pd.DataFrame()

    # --- 圖片上傳 Google Drive ---
    def upload_image_to_drive(self, file_obj, filename):
        try:
            # 檢查資料夾是否存在
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
            file_obj.seek(0) # 重置指標
            media = MediaIoBaseUpload(file_obj, mimetype=file_obj.type, resumable=True)
            file = self.drive_service.files().create(body=file_metadata, media_body=media, fields='id, webViewLink').execute()
            
            # 開啟公開讀取權限
            self.drive_service.permissions().create(fileId=file.get('id'), body={'role': 'reader', 'type': 'anyone'}).execute()
            return file.get('webViewLink')
        except Exception as e:
            return None

    # --- 影像前處理 (增強 OCR) ---
    def preprocess_image(self, image_bytes):
        try:
            img = Image.open(io.BytesIO(image_bytes))
            enhancer = ImageEnhance.Contrast(img)
            img = enhancer.enhance(1.5) 
            enhancer = ImageEnhance.Sharpness(img)
            img = enhancer.enhance(2.0)
            img_byte_arr = io.BytesIO()
            img.save(img_byte_arr, format='JPEG', quality=95) 
            return img_byte_arr.getvalue()
        except: return image_bytes

    # --- OCR 辨識 ---
    def ocr_id_card(self, content):
        try:
            enhanced_content = self.preprocess_image(content)
            image = vision.Image(content=enhanced_content)
            response = self.vision_client.text_detection(image=image)
            texts = response.text_annotations
            
            if not texts: return False, "❌ 無法辨識文字，請確認光線充足且未反光。"

            full_text = texts[0].description
            name, address = "", ""
            
            # 關鍵字檢核
            # if "身分證" not in full_text and "中華民國" not in full_text:
            #     return False, "⚠️ 這看起來不像身分證，請重新拍攝。"

            # 抓取姓名
            name_match = re.search(r"姓名\s*[:：]?\s*([\u4e00-\u9fa5]{2,4})", full_text)
            if name_match: name = name_match.group(1).strip()
            
            # 抓取地址
            lines = full_text.split('\n')
            for line in lines:
                clean_line = line.replace(" ", "")
                if any(x in clean_line for x in ['縣', '市', '區', '路', '街', '號']):
                    if "戶政" not in clean_line and len(clean_line) > 6:
                        address = clean_line.replace("住址", "").replace("地址", "").strip()
                        break
            
            if not name and not address:
                return False, "⚠️ 影像模糊，請嘗試重新對焦拍攝。"
                
            return True, {"name": name, "address": address}
        except Exception as e:
            return False, f"系統錯誤: {str(e)}"

    # --- 資料更新 (含 Log) ---
    def update_shareholder_profile(self, editor, tax_id, new_data):
        try:
            cell = self.ws_sh.find(tax_id, in_column=1)
            if not cell: return False, "找不到資料"
            
            headers = self.ws_sh.row_values(1)
            headers = [h.strip() for h in headers]
            
            old_row = self.ws_sh.row_values(cell.row)
            while len(old_row) < len(headers): old_row.append("")
            current_data = dict(zip(headers, old_row))
            changes = []
            
            for key, val in new_data.items():
                if key in headers:
                    new_val = str(val)
                    old_val = str(current_data.get(key, ""))
                    if new_val != old_val:
                        changes.append([datetime.now().strftime("%Y-%m-%d %H:%M:%S"), editor, tax_id, key, old_val, new_val])
                        col_idx = headers.index(key) + 1
                        self.ws_sh.update_cell(cell.row, col_idx, new_val)
            
            if changes and self.ws_log:
                self.ws_log.append_rows(changes)
                return True, f"已更新 {len(changes)} 個欄位"
            return True, "資料已儲存 (無欄位變更)"
        except Exception as e: return False, str(e)

    # --- 批次匯入 (全量處理) ---
    def batch_import_from_excel(self, df_excel, replace_shares=False):
        try:
            current = self.ws_sh.get_all_records()
            # 建立 Map
            db_map = {str(item['tax_id']).strip(): item for item in current}
            cnt = 0
            
            for index, row in df_excel.iterrows():
                tid = str(row.get("身分證或統編", "")).strip()
                if not tid: continue
                
                # 準備新資料
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
                    # 舊股東更新
                    db_map[tid].update(new_info)
                    if shares >= 0:
                        if replace_shares: db_map[tid]['shares_held'] = shares
                        else: db_map[tid]['shares_held'] = int(db_map[tid].get('shares_held') or 0) + shares
                else:
                    # 新股東建立
                    new_info.update({'tax_id': tid, 'shares_held': shares, 'password': "", 'phone': "", 'id_image_url': ""})
                    db_map[tid] = new_info
                cnt += 1
            
            # 轉回 List 準備寫入
            final_data = []
            headers = ["tax_id", "name", "holder_type", "representative", "household_address", "mailing_address", "phone", "email", "password_hint", "shares_held", "password", "id_image_url"]
            
            for k, v in db_map.items():
                row_data = [v.get(h, "") for h in headers]
                final_data.append(row_data)
            
            self.ws_sh.clear()
            self.ws_sh.append_row(headers)
            self.ws_sh.append_rows(final_data)
            return True, f"匯入成功，共處理 {cnt} 筆資料"
        except Exception as e: return False, str(e)

    # --- 申請單邏輯 ---
    def add_request(self, applicant_id, amount, reason):
        try:
            cell = self.ws_sh.find(applicant_id, in_column=1)
            # col 10 is shares_held
            curr = int(self.ws_sh.cell(cell.row, 10).value or 0) 
            
            reqs = self.ws_req.get_all_records()
            pending = sum([int(r['amount']) for r in reqs if str(r['applicant'])==str(applicant_id) and r['status']=='Pending'])
            
            available = curr - pending
            if amount > available: return False, f"額度不足 (持有:{curr}, 凍結:{pending})"
            
            rid = int(time.time())
            self.ws_req.append_row([rid, datetime.now().strftime("%Y-%m-%d"), applicant_id, "", amount, "Pending", reason, ""])
            return True, "已送出申請"
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
            return False, "無法撤銷 (可能已審核)"
        except: return False, "Error"

    # --- 股權轉讓核心 ---
    def transfer_shares(self, date, s_id, b_id, amount, reason):
        try:
            s_cell = self.ws_sh.find(s_id, in_column=1)
            b_cell = self.ws_sh.find(b_id, in_column=1)
            if not s_cell or not b_cell: return False, "找不到買賣方"
            
            s_shares = int(self.ws_sh.cell(s_cell.row, 10).value or 0)
            b_shares = int(self.ws_sh.cell(b_cell.row, 10).value or 0)
            
            if s_shares < amount: return False, "賣方股數不足"
            
            self.ws_sh.update_cell(s_cell.row, 10, s_shares - amount)
            self.ws_sh.update_cell(b_cell.row, 10, b_shares + amount)
            self.ws_tx.append_row([str(date), s_id, b_id, amount, reason])
            return True, "成功"
        except Exception as e: return False, str(e)

    # --- 單筆管理功能 ---
    def upsert_shareholder(self, tax_id, name, holder_type, address, representative, email, hint):
        try:
            tax_id = str(tax_id).strip()
            if not hint: hint = "無提示"
            try: cell = self.ws_sh.find(tax_id)
            except: time.sleep(1); cell = self.ws_sh.find(tax_id)
            
            # 若不存在，新增完整列 (確保長度正確)
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

    # --- 登入與密碼 ---
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

# --- UI Dialogs ---
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

@st.dialog("📝 編輯個人資料 (雙面辨識)")
def show_profile_edit_dialog(user_data):
    st.info("請依序拍攝證件正面與反面。")
    with st.expander("📸 拍攝技巧", expanded=False):
        st.markdown("1. 光線充足 2. 避免反光 3. 填滿畫面")

    if "temp_name" not in st.session_state: st.session_state.temp_name = user_data['name']
    if "temp_addr" not in st.session_state: st.session_state.temp_addr = str(user_data.get('household_address', ''))

    tab1, tab2, tab3 = st.tabs(["1️⃣ 正面", "2️⃣ 反面", "3️⃣ 確認"])

    with tab1:
        front_img = st.camera_input("正面", key="cam_front")
        if front_img:
            st.image(front_img, width=200)
            if st.button("🔍 辨識正面"):
                with st.spinner("分析中..."):
                    s, r = sys.ocr_id_card(front_img.getvalue())
                    if s: 
                        st.success("成功")
                        if r['name']: st.session_state.temp_name = r['name']
                        if r['address']: st.session_state.temp_addr = r['address']
                    else: st.error(r)

    with tab2:
        back_img = st.camera_input("反面", key="cam_back")
        if back_img:
            st.image(back_img, width=200)
            if st.button("🔍 辨識反面"):
                with st.spinner("分析中..."):
                    s, r = sys.ocr_id_card(back_img.getvalue())
                    if s and r['address']: st.info(f"偵測地址: {r['address']}")
                    else: st.warning("未偵測到地址")

    with tab3:
        with st.form("final"):
            n = st.text_input("姓名", value=st.session_state.temp_name)
            p = st.text_input("手機", value=str(user_data.get('phone', '')))
            ha = st.text_input("戶籍", value=st.session_state.temp_addr)
            ma = st.text_input("通訊", value=str(user_data.get('mailing_address', '')))
            e = st.text_input("Email", value=str(user_data.get('email', '')))
            
            if st.form_submit_button("💾 儲存", type="primary"):
                ud = {'name': n, 'phone': p, 'household_address': ha, 'mailing_address': ma, 'email': e}
                if front_img:
                    link = sys.upload_image_to_drive(front_img, f"{user_data['tax_id']}_f_{int(time.time())}.jpg")
                    if link: ud['id_image_url'] = link
                s, m = sys.update_shareholder_profile(st.session_state.user_name, user_data['tax_id'], ud)
                if s: st.success(m); time.sleep(1.5); st.rerun()
                else: st.error(m)

@st.dialog("✍️ 申請交易")
def show_request_dialog(uid, curr, pend):
    avail = curr - pend
    st.info(f"可用: {avail}")
    with st.form("req"):
        a = st.number_input("股數", 1, max_value=avail if avail>0 else 1)
        r = st.text_input("原因")
        if st.form_submit_button("送出"):
            if avail <= 0: st.error("不足")
            else:
                s, m = sys.add_request(uid, a, r)
                if s: st.success(m); time.sleep(1); st.rerun()
                else: st.error(m)

@st.dialog("📋 核定")
def show_approve_dialog(req, users):
    st.write(f"申請人: {req['applicant']}, 股數: {req['amount']}")
    with st.form("appr"):
        opts = [x for x in users if x.split(" | ")[0] != str(req['applicant'])]
        tgt = st.selectbox("買方", opts)
        if st.form_submit_button("確認"):
            s, m = sys.approve_request(req['id'], datetime.now().strftime("%Y-%m-%d"), req['applicant'], tgt.split(" | ")[0], req['amount'])
            if s: st.success(m); time.sleep(1); st.rerun()
            else: st.error(m)

@st.dialog("❌ 退件")
def show_reject_dialog(rid):
    with st.form("rej"):
        r = st.text_input("原因")
        if st.form_submit_button("確認"):
            sys.reject_request(rid, r); st.success("已退"); time.sleep(1); st.rerun()

@st.dialog("🗑️ 撤銷")
def show_cancel_request_dialog(rid):
    st.warning("撤銷？")
    if st.button("確認"):
        sys.delete_request(rid); st.success("已撤"); time.sleep(1); st.rerun()

@st.dialog("✏️ 修改(Admin)")
def show_edit_dialog(current_data):
    with st.form("edit_form"):
        new_tax_id = st.text_input("統編", value=str(current_data['tax_id']), disabled=True)
        new_name = st.text_input("姓名", value=current_data['name'])
        t_opts = ["Individual", "Corporate"]
        t_idx = t_opts.index(current_data['holder_type']) if current_data['holder_type'] in t_opts else 0
        new_type = st.selectbox("類別", t_opts, index=t_idx)
        new_h = st.text_input("戶籍", value=str(current_data['household_address']))
        new_m = st.text_input("通訊", value=str(current_data['mailing_address']))
        new_rep = st.text_input("代表人", value=str(current_data['representative']))
        new_email = st.text_input("Email", value=str(current_data['email']))
        new_hint = st.text_input("提示", value=str(current_data['password_hint']))
        
        if st.form_submit_button("更新"):
            ud = {'name': new_name, 'holder_type': new_type, 'representative': new_rep, 
                  'household_address': new_h, 'mailing_address': new_m, 'email': new_email, 'password_hint': new_hint}
            succ, msg = sys.update_shareholder_profile("Admin", new_tax_id, ud)
            if succ: st.success(msg); time.sleep(1); st.rerun()

@st.dialog("🗑️ 刪除")
def show_delete_dialog(tid, name):
    st.warning(f"刪除 {name}?")
    if st.button("確認"): sys.delete_shareholder(tid); st.success("OK"); time.sleep(1); st.rerun()

@st.dialog("🗑️ 批次刪除")
def show_batch_delete_dialog(selected_list):
    st.warning(f"刪除 {len(selected_list)} 筆?")
    if st.button("確認"):
        ids = [i.split(" | ")[0] for i in selected_list]
        sys.delete_batch_shareholders(ids)
        st.success("OK")
        for k in list(st.session_state.keys()):
            if k.startswith("sel_"): del st.session_state[k]
        time.sleep(1); st.rerun()

# --- Main App ---
def run_main_app(role, user_name, user_id):
    with st.sidebar:
        st.markdown(f"### 👋 {user_name}")
        if st.button("密碼"): show_password_dialog(role, user_id)
        if st.button("登出"): st.session_state.logged_in=False; st.rerun()
        
        if role == "admin":
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
                
                search = st.text_input("搜尋")
                if search: df = df[df['name'].astype(str).str.contains(search) | df['tax_id'].astype(str).str.contains(search)]
                
                # 批次刪除邏輯
                def toggle_all():
                    val = st.session_state.master_select
                    for t in df['tax_id']: st.session_state[f"sel_{t}"] = val
                
                sel_ids = []
                for t in df['tax_id']:
                    if st.session_state.get(f"sel_{t}", False):
                        n = df[df['tax_id']==t].iloc[0]['name']
                        sel_ids.append(f"{t} | {n}")
                
                c1, c2 = st.columns([1,4])
                c1.checkbox("全選", key="master_select", on_change=toggle_all)
                if sel_ids: 
                    if c2.button(f"刪除 ({len(sel_ids)})"): show_batch_delete_dialog(sel_ids)

                st.dataframe(df)
                
                # 操作按鈕列
                st.write("單筆操作:")
                for i, r in df.iterrows():
                    with st.expander(f"{r['name']} ({r['tax_id']})"):
                        c1, c2 = st.columns(2)
                        if c1.button("編輯", key=f"e_{r['tax_id']}"): show_edit_dialog(r)
                        if c2.button("刪除", key=f"d_{r['tax_id']}"): show_delete_dialog(r['tax_id'], r['name'])

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
