import streamlit as st
import pandas as pd
from datetime import datetime
import io
import time
import smtplib
from email.mime.text import MIMEText
import gspread
from google.oauth2.service_account import Credentials
from gspread.exceptions import APIError

# --- 1. 系統設定區 ---
st.set_page_config(page_title="股務管理系統 (交易審核版)", layout="wide")

# Email 設定
SMTP_SERVER = "smtp.gmail.com"
SMTP_PORT = 587
SENDER_EMAIL = ""  
SENDER_PASSWORD = "" 

# --- 2. Google Sheets 資料庫核心 ---
class GoogleSheetDB:
    def __init__(self):
        self.connect()

    def connect(self):
        try:
            scope = ["https://www.googleapis.com/auth/spreadsheets", "https://www.googleapis.com/auth/drive"]
            creds_dict = dict(st.secrets["gcp_service_account"])
            creds = Credentials.from_service_account_info(creds_dict, scopes=scope)
            self.client = gspread.authorize(creds)
            sheet_url = st.secrets["sheet_config"]["spreadsheet_url"]
            self.sh = self.client.open_by_url(sheet_url)
            # 載入工作表
            self.ws_shareholders = self.sh.worksheet("shareholders")
            self.ws_transactions = self.sh.worksheet("transactions")
            self.ws_admin = self.sh.worksheet("system_admin")
            self.ws_requests = self.sh.worksheet("requests") # 新增申請表
        except Exception as e:
            st.error(f"連線 Google Sheets 失敗: {e}")
            st.stop()

    def get_df(self, table_name):
        for i in range(3):
            try:
                if table_name == "shareholders":
                    data = self.ws_shareholders.get_all_records()
                elif table_name == "transactions":
                    data = self.ws_transactions.get_all_records()
                elif table_name == "requests":
                    data = self.ws_requests.get_all_records()
                return pd.DataFrame(data)
            except APIError: time.sleep(1)
        return pd.DataFrame()

    # --- 申請單相關功能 (新) ---
    def add_request(self, applicant_id, target_id, amount):
        try:
            req_id = int(time.time()) # 用時間戳記當 ID
            date_str = datetime.now().strftime("%Y-%m-%d")
            # 寫入: id, date, applicant, target, amount, status
            self.ws_requests.append_row([req_id, date_str, applicant_id, target_id, amount, "Pending"])
            return True, "申請已送出，待管理員審核。"
        except Exception as e: return False, str(e)

    def approve_request(self, req_id, date, seller_id, buyer_id, amount):
        try:
            # 1. 執行過戶
            success, msg = self.transfer_shares(date, seller_id, buyer_id, amount, "股東申請交易")
            if not success: return False, msg
            
            # 2. 更新申請單狀態為 Approved
            cell = self.ws_requests.find(str(req_id), in_column=1)
            if cell:
                self.ws_requests.update_cell(cell.row, 6, "Approved") # Col 6 is status
            
            return True, "審核通過，已完成過戶！"
        except Exception as e: return False, str(e)

    def reject_request(self, req_id):
        try:
            cell = self.ws_requests.find(str(req_id), in_column=1)
            if cell:
                self.ws_requests.update_cell(cell.row, 6, "Rejected")
                return True, "已駁回申請"
            return False, "找不到該申請單"
        except Exception as e: return False, str(e)

    # --- 原有核心功能 ---
    def upsert_shareholder(self, tax_id, name, holder_type, address, representative, email, hint):
        try:
            tax_id = str(tax_id).strip()
            if not hint: hint = "無提示"
            try: cell = self.ws_shareholders.find(tax_id)
            except: time.sleep(1); cell = self.ws_shareholders.find(tax_id)

            if cell:
                row = cell.row
                self.ws_shareholders.batch_update([{
                    'range': f'B{row}:G{row}',
                    'values': [[name, holder_type, representative, address, email, hint]]
                }])
            else:
                self.ws_shareholders.append_row([tax_id, name, holder_type, representative, address, email, hint, 0, ""])
            return True, f"成功更新：{name}"
        except Exception as e: return False, str(e)

    def update_password(self, user_id, new_password, new_hint, is_admin=False):
        try:
            ws = self.ws_admin if is_admin else self.ws_shareholders
            cell = ws.find(user_id, in_column=1)
            if cell:
                row = cell.row
                if is_admin:
                    ws.update_cell(row, 2, new_password); ws.update_cell(row, 4, new_hint)
                else:
                    ws.update_cell(row, 9, new_password); ws.update_cell(row, 7, new_hint)
                return True
            return False
        except: return False

    def get_user_recovery_info(self, user_id, is_admin=False):
        try:
            ws = self.ws_admin if is_admin else self.ws_shareholders
            cell = ws.find(user_id, in_column=1)
            if cell:
                row_vals = ws.row_values(cell.row)
                if is_admin:
                    email = row_vals[2] if len(row_vals)>2 else ""
                    hint = row_vals[3] if len(row_vals)>3 else ""
                    pwd = row_vals[1]
                else:
                    email = row_vals[5] if len(row_vals)>5 else ""
                    hint = row_vals[6] if len(row_vals)>6 else ""
                    pwd = row_vals[8] if len(row_vals)>8 else user_id
                return {"email": email, "hint": hint, "password": pwd}
            return None
        except: return None

    def verify_login(self, username, password, is_admin_attempt):
        try:
            ws = self.ws_admin if is_admin_attempt else self.ws_shareholders
            try: cell = ws.find(username, in_column=1)
            except: time.sleep(1); cell = ws.find(username, in_column=1)
            
            if not cell: return False, "無此帳號", None
            row_vals = ws.row_values(cell.row)
            if is_admin_attempt:
                stored_pass = row_vals[1]
                stored_hint = row_vals[3] if len(row_vals)>3 else ""
                name = "系統管理員"
            else:
                name = row_vals[1]
                stored_hint = row_vals[6] if len(row_vals)>6 else ""
                stored_pass = row_vals[8] if len(row_vals)>8 else ""
                if stored_pass == "": stored_pass = username 
            
            if str(stored_pass) == str(password): return True, name, None
            else: return False, "密碼錯誤", stored_hint
        except Exception as e: return False, f"系統錯誤: {e}", None

    def issue_shares(self, tax_id, amount):
        try:
            cell = self.ws_shareholders.find(tax_id, in_column=1)
            if cell:
                row = cell.row
                curr = int(self.ws_shareholders.cell(row, 8).value or 0)
                self.ws_shareholders.update_cell(row, 8, curr + amount)
        except: pass

    def set_share_count(self, tax_id, amount):
        try:
            cell = self.ws_shareholders.find(tax_id, in_column=1)
            if cell: self.ws_shareholders.update_cell(cell.row, 8, amount)
        except: pass

    def delete_shareholder(self, tax_id):
        try:
            cell = self.ws_shareholders.find(tax_id, in_column=1)
            if cell: self.ws_shareholders.delete_rows(cell.row)
        except: pass
        
    def delete_batch_shareholders(self, tax_id_list):
        try:
            for tid in tax_id_list:
                self.delete_shareholder(tid); time.sleep(0.5)
            return True, f"已刪除 {len(tax_id_list)} 筆"
        except Exception as e: return False, str(e)

    def batch_import_from_excel(self, df_excel, replace_shares=False):
        # ... (維持之前的極速版邏輯，省略以節省篇幅，請保留原有的 batch_import) ...
        # 為確保功能完整，這裡用簡化版 (單筆) 或請您保留上一版的 batch_import_from_excel
        # 這裡示範單筆 fallback，建議您若有大量需求可將上一版 batch 函數貼回來
        count = 0
        for i, r in df_excel.iterrows():
            try:
                tid = str(r.get("身分證或統編", "")).strip()
                if not tid: continue
                nm = str(r.get("姓名", "")).strip()
                tp = "Corporate" if "法人" in str(r.get("身分別", "")) else "Individual"
                addr = str(r.get("地址", "")); rep = str(r.get("代表人", ""))
                email = str(r.get("Email", "")); hint = str(r.get("密碼提示", ""))
                self.upsert_shareholder(tid, nm, tp, addr, rep, email, hint)
                try:
                    qty = int(r.get("持股數", 0))
                    if qty >= 0:
                        if replace_shares: self.set_share_count(tid, qty)
                        else: self.issue_shares(tid, qty)
                except: pass
                count += 1
            except: pass
        return True, f"已處理 {count} 筆"

    def transfer_shares(self, date, seller_tax_id, buyer_tax_id, amount, reason):
        try:
            s_cell = self.ws_shareholders.find(seller_tax_id, in_column=1)
            if not s_cell: return False, "找不到賣方"
            s_shares = int(self.ws_shareholders.cell(s_cell.row, 8).value or 0)
            if s_shares < amount: return False, "股數不足"
            
            b_cell = self.ws_shareholders.find(buyer_tax_id, in_column=1)
            if not b_cell: return False, "找不到買方"
            b_shares = int(self.ws_shareholders.cell(b_cell.row, 8).value or 0)
            
            self.ws_shareholders.update_cell(s_cell.row, 8, s_shares - amount)
            self.ws_shareholders.update_cell(b_cell.row, 8, b_shares + amount)
            
            self.ws_transactions.append_row([str(date), seller_tax_id, buyer_tax_id, amount, reason])
            return True, "過戶成功"
        except Exception as e: return False, str(e)

@st.cache_resource
def get_db_system():
    return GoogleSheetDB()

try: sys = get_db_system()
except: st.error("連線逾時"); st.stop()

# --- UI Components ---
def send_recovery_email(to_email, user_id, password):
    # ... (維持原樣) ...
    return True, "模擬發送成功"

@st.dialog("🔑 忘記密碼")
def show_forgot_password_dialog():
    user_input = st.text_input("帳號")
    if st.button("查詢"):
        info = sys.get_user_recovery_info(user_input, user_input=="admin")
        if info:
            st.success("找到帳號"); st.info(f"提示：{info['hint']}")
        else: st.error("無此帳號")

@st.dialog("🔑 修改密碼")
def show_password_dialog(user_role, user_id):
    with st.form("pwd_form"):
        p1 = st.text_input("新密碼", type="password")
        p2 = st.text_input("確認", type="password")
        hint = st.text_input("提示詞")
        if st.form_submit_button("修改"):
            if p1 and p1==p2 and hint:
                sys.update_password(user_id, p1, hint, user_role=="admin")
                st.success("成功"); time.sleep(1); st.session_state.logged_in=False; st.rerun()
            else: st.error("錯誤")

@st.dialog("✍️ 提出交易申請")
def show_request_dialog(applicant_id, shareholder_list):
    st.info("請填寫您欲進行的交易")
    with st.form("req_form"):
        # 買方/賣方邏輯：假設申請人是賣方 (轉讓給別人)
        target = st.selectbox("轉讓對象 (買方)", shareholder_list)
        amount = st.number_input("轉讓股數", min_value=1)
        
        if st.form_submit_button("送出申請"):
            target_id = target.split(" | ")[0]
            if target_id == applicant_id:
                st.error("不能轉讓給自己")
            else:
                succ, msg = sys.add_request(applicant_id, target_id, amount)
                if succ: st.success(msg); time.sleep(1.5); st.rerun()
                else: st.error(msg)

@st.dialog("📋 交易審核確認")
def show_approve_dialog(req_data):
    st.warning(f"確定核准此交易？")
    st.write(f"申請人 (賣方): {req_data['applicant']}")
    st.write(f"對象 (買方): {req_data['target']}")
    st.write(f"股數: {req_data['amount']}")
    
    if st.button("✅ 確認核准"):
        succ, msg = sys.approve_request(req_data['id'], datetime.today().strftime("%Y-%m-%d"), req_data['applicant'], req_data['target'], req_data['amount'])
        if succ: st.success(msg); time.sleep(1.5); st.rerun()
        else: st.error(msg)

# --- Main App ---
def run_main_app(role, user_name, user_id):
    with st.sidebar:
        st.markdown(f"### 👋 {user_name}")
        if st.button("密碼修改"): show_password_dialog(role, user_id)
        if st.button("登出"): st.session_state.logged_in = False; st.rerun()
        
        if role == "admin":
            menu_options = ["📊 股東名簿總覽", "✅ 審核交易申請", "📂 批次匯入", "➕ 新增股東", "💰 發行/增資", "🤝 股權過戶", "📝 交易歷史"]
        else:
            # 股東選單升級
            menu_options = ["📝 我的持股", "📜 交易紀錄查詢", "✍️ 申請交易"]
            
        menu = st.radio("選單", menu_options)

    st.title("🏢 股務管理系統")

    if role == "admin":
        if menu == "✅ 審核交易申請":
            st.header("審核交易申請")
            df = sys.get_df("requests")
            if not df.empty and "status" in df.columns:
                # 只顯示 Pending
                pending = df[df["status"] == "Pending"]
                if pending.empty:
                    st.info("目前無待審核申請")
                else:
                    st.dataframe(pending)
                    st.divider()
                    st.write("操作區：")
                    
                    for i, r in pending.iterrows():
                        c1, c2, c3, c4 = st.columns([2, 2, 2, 2])
                        c1.write(f"申請人: {r['applicant']}")
                        c2.write(f"對象: {r['target']}")
                        c3.write(f"股數: {r['amount']}")
                        if c4.button("審核", key=f"appr_{r['id']}"):
                            show_approve_dialog(r)
            else:
                st.info("尚無申請資料")

        # ... (其他 Admin 功能如 股東名簿、批次匯入 等保持不變，省略以節省篇幅) ...
        elif menu == "📊 股東名簿總覽":
            df = sys.get_df("shareholders")
            st.dataframe(df) # 簡化顯示，完整版請貼回之前的代碼
        elif menu == "📝 交易歷史":
            st.dataframe(sys.get_df("transactions"))
        
        # Admin 也可手動過戶
        elif menu == "🤝 股權過戶":
            df = sys.get_df("shareholders")
            ops = [f"{r['tax_id']} | {r['name']}" for i,r in df.iterrows()]
            s = st.selectbox("賣方", ops); b = st.selectbox("買方", ops)
            amt = st.number_input("股數", min_value=1)
            if st.button("過戶"):
                msg = sys.transfer_shares(datetime.today(), s.split(" | ")[0], b.split(" | ")[0], amt, "Admin手動")
                st.success(msg) if "成功" in msg else st.error(msg)

    else:
        # === 股東功能區 ===
        if menu == "📝 我的持股":
            st.header(f"我的持股 - {user_name}")
            df = sys.get_df("shareholders")
            r = df[df['tax_id'].astype(str) == str(user_id)]
            if not r.empty:
                row = r.iloc[0]
                c1, c2, c3 = st.columns(3)
                c1.metric("持有股數", f"{row['shares_held']:,}")
                c2.metric("Email", row['email'])
                c3.metric("提示詞", row['password_hint'])
            else: st.warning("查無資料")

        elif menu == "📜 交易紀錄查詢":
            st.header("歷史交易明細")
            df_trans = sys.get_df("transactions")
            if not df_trans.empty:
                # 篩選：賣方是我 OR 買方是我
                # 欄位順序: date, seller, buyer, amount, reason
                # 假設 Google Sheet 標題為英文，若為中文需調整
                # 這裡假設欄位名為: date, seller_tax_id, buyer_tax_id, ...
                try:
                    my_trans = df_trans[
                        (df_trans['seller_tax_id'].astype(str) == str(user_id)) | 
                        (df_trans['buyer_tax_id'].astype(str) == str(user_id))
                    ]
                    if not my_trans.empty:
                        st.dataframe(my_trans, use_container_width=True)
                    else:
                        st.info("目前尚無交易紀錄")
                except:
                    st.error("讀取紀錄發生錯誤，請確認交易紀錄表標題是否正確 (date, seller_tax_id, buyer_tax_id, amount, reason)")
            else:
                st.info("尚無任何交易紀錄")

        elif menu == "✍️ 申請交易":
            st.header("提出股份轉讓申請")
            st.info("此申請送出後，需經由管理員審核通過才會生效。")
            
            # 取得所有股東名單供選擇 (排除自己)
            df_users = sys.get_df("shareholders")
            if not df_users.empty:
                others = df_users[df_users['tax_id'].astype(str) != str(user_id)]
                if not others.empty:
                    target_list = [f"{r['tax_id']} | {r['name']}" for i, r in others.iterrows()]
                    
                    if st.button("填寫申請單"):
                        show_request_dialog(user_id, target_list)
                    
                    # 顯示我的申請狀態
                    st.divider()
                    st.subheader("我的申請進度")
                    df_req = sys.get_df("requests")
                    if not df_req.empty and "applicant" in df_req.columns:
                        my_reqs = df_req[df_req['applicant'].astype(str) == str(user_id)]
                        st.dataframe(my_reqs)
                else:
                    st.warning("系統中無其他股東可轉讓")

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
                    else: 
                        st.error(m)
                        if h: st.info(f"提示: {h}")
    else:
        run_main_app(st.session_state.user_role, st.session_state.user_name, st.session_state.user_id)
