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
st.set_page_config(page_title="股務管理系統 (交易審核嚴謹版)", layout="wide")

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
            self.ws_shareholders = self.sh.worksheet("shareholders")
            self.ws_transactions = self.sh.worksheet("transactions")
            self.ws_admin = self.sh.worksheet("system_admin")
            self.ws_requests = self.sh.worksheet("requests")
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

    # --- 申請單邏輯 (大幅修改) ---
    def add_request(self, applicant_id, amount, reason):
        try:
            # 1. 檢查持股數
            cell_sh = self.ws_shareholders.find(applicant_id, in_column=1)
            if not cell_sh: return False, "找不到股東資料"
            current_shares = int(self.ws_shareholders.cell(cell_sh.row, 8).value or 0)

            # 2. 檢查「申請中但未核准」的股數 (防呆機制)
            req_data = self.ws_requests.get_all_records()
            pending_shares = 0
            for r in req_data:
                # 確保欄位存在且狀態為 Pending 且申請人是自己
                if str(r.get('applicant')) == str(applicant_id) and r.get('status') == 'Pending':
                    pending_shares += int(r.get('amount') or 0)
            
            # 3. 計算可用餘額
            available_shares = current_shares - pending_shares
            
            if amount > available_shares:
                return False, f"股數不足！\n目前持股: {current_shares}\n申請中扣除: {pending_shares}\n可用於交易: {available_shares}"

            # 4. 寫入申請 (target 留空)
            req_id = int(time.time())
            date_str = datetime.now().strftime("%Y-%m-%d")
            # 順序: id, date, applicant, target(空), amount, status, reason, reject_reason(空)
            self.ws_requests.append_row([req_id, date_str, applicant_id, "", amount, "Pending", reason, ""])
            return True, "申請已送出，待管理員審核。"
        except Exception as e: return False, str(e)

    def delete_request(self, req_id):
        try:
            cell = self.ws_requests.find(str(req_id), in_column=1)
            if cell:
                # 雙重確認狀態是否為 Pending (避免剛好被核准又被刪除)
                status = self.ws_requests.cell(cell.row, 6).value
                if status == "Pending":
                    self.ws_requests.delete_rows(cell.row)
                    return True, "申請已撤銷刪除"
                else:
                    return False, "該申請已被處理，無法刪除"
            return False, "找不到該申請單"
        except Exception as e: return False, str(e)

    def approve_request(self, req_id, date, seller_id, buyer_id, amount):
        try:
            # 1. 執行過戶 (這會檢查賣方實際庫存)
            success, msg = self.transfer_shares(date, seller_id, buyer_id, amount, "股東申請交易")
            if not success: return False, msg
            
            # 2. 更新申請單: 填入買方(Col 4), 狀態(Col 6)
            cell = self.ws_requests.find(str(req_id), in_column=1)
            if cell:
                self.ws_requests.update_cell(cell.row, 4, buyer_id) # Target
                self.ws_requests.update_cell(cell.row, 6, "Approved") # Status
            
            return True, "審核通過，已完成過戶！"
        except Exception as e: return False, str(e)

    def reject_request(self, req_id, reject_reason):
        try:
            cell = self.ws_requests.find(str(req_id), in_column=1)
            if cell:
                self.ws_requests.update_cell(cell.row, 6, "Rejected") # Status
                self.ws_requests.update_cell(cell.row, 8, reject_reason) # Reject Reason
                return True, "已駁回申請"
            return False, "找不到該申請單"
        except Exception as e: return False, str(e)

    # --- (以下為維持不變的核心功能) ---
    def batch_import_from_excel(self, df_excel, replace_shares=False):
        try:
            current_records = self.ws_shareholders.get_all_records()
            db_map = {str(item['tax_id']).strip(): item for item in current_records}
            updated_count = 0
            for index, row in df_excel.iterrows():
                tid = str(row.get("身分證或統編", "")).strip()
                if not tid: continue
                nm = str(row.get("姓名", "")).strip()
                tp = "Corporate" if "法人" in str(row.get("身分別", "")) else "Individual"
                addr = str(row.get("地址", "")); rep = str(row.get("代表人", ""))
                email = str(row.get("Email", "")); hint = str(row.get("密碼提示", ""))
                excel_shares = 0
                try:
                    raw_shares = row.get("持股數") if "持股數" in row else row.get("初始持股數", 0)
                    excel_shares = int(raw_shares)
                except: excel_shares = 0

                if tid in db_map:
                    target = db_map[tid]
                    target.update({'name': nm, 'holder_type': tp, 'address': addr, 'representative': rep, 'email': email, 'password_hint': hint})
                    if excel_shares >= 0:
                        if replace_shares: target['shares_held'] = excel_shares
                        else: target['shares_held'] = int(target['shares_held'] or 0) + excel_shares
                else:
                    db_map[tid] = {
                        'tax_id': tid, 'name': nm, 'holder_type': tp, 'representative': rep, 
                        'address': addr, 'email': email, 'password_hint': hint, 
                        'shares_held': excel_shares, 'password': ""
                    }
                updated_count += 1
            final_data = []
            headers = ["tax_id", "name", "holder_type", "representative", "address", "email", "password_hint", "shares_held", "password"]
            for key, val in db_map.items():
                final_data.append([
                    val.get('tax_id'), val.get('name'), val.get('holder_type', 'Individual'), val.get('representative', ''),
                    val.get('address', ''), val.get('email', ''), val.get('password_hint', ''), val.get('shares_held', 0), val.get('password', '')
                ])
            self.ws_shareholders.clear(); self.ws_shareholders.append_row(headers); self.ws_shareholders.append_rows(final_data)
            return True, f"處理完成！共 {updated_count} 筆。"
        except Exception as e: return False, f"匯入失敗: {str(e)}"

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
                if is_admin: ws.update_cell(row, 2, new_password); ws.update_cell(row, 4, new_hint)
                else: ws.update_cell(row, 9, new_password); ws.update_cell(row, 7, new_hint)
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
                stored_pass = row_vals[1]; stored_hint = row_vals[3] if len(row_vals)>3 else ""; name = "系統管理員"
            else:
                name = row_vals[1]; stored_hint = row_vals[6] if len(row_vals)>6 else ""; stored_pass = row_vals[8] if len(row_vals)>8 else ""
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
            current = self.ws_shareholders.get_all_records()
            new_recs = [r for r in current if str(r['tax_id']) not in tax_id_list]
            headers = ["tax_id", "name", "holder_type", "representative", "address", "email", "password_hint", "shares_held", "password"]
            final_data = []
            for item in new_recs:
                final_data.append([
                    item['tax_id'], item['name'], item['holder_type'], item['representative'],
                    item['address'], item['email'], item['password_hint'], item['shares_held'], item['password']
                ])
            self.ws_shareholders.clear(); self.ws_shareholders.append_row(headers); self.ws_shareholders.append_rows(final_data)
            return True, f"已刪除 {len(tax_id_list)} 筆"
        except Exception as e: return False, str(e)

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
    if not SENDER_EMAIL or not SENDER_PASSWORD: return True, "模擬發送成功"
    try:
        msg = MIMEText(f"帳號：{user_id}\n密碼：{password}", 'plain', 'utf-8')
        msg['Subject'] = '密碼找回'; msg['From'] = SENDER_EMAIL; msg['To'] = to_email
        server = smtplib.SMTP(SMTP_SERVER, SMTP_PORT)
        server.starttls(); server.login(SENDER_EMAIL, SENDER_PASSWORD)
        server.send_message(msg); server.quit()
        return True, "已發送"
    except Exception as e: return False, str(e)

@st.dialog("🔑 忘記密碼")
def show_forgot_password_dialog():
    user_input = st.text_input("帳號")
    if st.button("查詢"):
        info = sys.get_user_recovery_info(user_input, user_input=="admin")
        if info:
            st.success("找到帳號"); st.info(f"提示：{info['hint']}")
            if info['email'] and st.button("📧 寄送密碼"): send_recovery_email(info['email'], user_input, info['password'])
        else: st.error("無此帳號")

@st.dialog("🔑 修改密碼")
def show_password_dialog(user_role, user_id):
    with st.form("pwd_form"):
        p1 = st.text_input("新密碼", type="password"); p2 = st.text_input("確認", type="password"); hint = st.text_input("提示詞")
        if st.form_submit_button("修改"):
            if p1==p2 and hint:
                sys.update_password(user_id, p1, hint, user_role=="admin")
                st.success("成功"); time.sleep(1); st.session_state.logged_in=False; st.rerun()
            else: st.error("錯誤")

@st.dialog("✍️ 提出交易申請")
def show_request_dialog(applicant_id, current_holdings, pending_shares):
    st.info(f"目前持有: {current_shares:,} 股 | 申請中: {pending_shares:,} 股")
    available = current_shares - pending_shares
    st.success(f"可用交易股數: {available:,} 股")
    
    with st.form("req_form"):
        amount = st.number_input("欲交易股數", min_value=1, max_value=available if available > 0 else 1)
        reason = st.text_input("交易原因", placeholder="例如：個人資金需求、轉讓給親屬...")
        
        if st.form_submit_button("送出申請"):
            if available <= 0:
                st.error("可用股數不足，無法申請。")
            elif amount > available:
                st.error(f"輸入股數超過可用額度 ({available})")
            elif not reason:
                st.error("請填寫交易原因")
            else:
                succ, msg = sys.add_request(applicant_id, amount, reason)
                if succ: st.success(msg); time.sleep(1.5); st.rerun()
                else: st.error(msg)

@st.dialog("📋 核定交易 (審核通過)")
def show_approve_dialog(req_data, shareholder_list):
    st.info("請指定此筆交易的買方 (受讓人)")
    st.write(f"申請人 (賣方): {req_data['applicant']}")
    st.write(f"申請股數: {req_data['amount']:,}")
    st.write(f"申請原因: {req_data['reason']}")
    
    with st.form("approve_form"):
        # 排除賣方自己
        options = [x for x in shareholder_list if x.split(" | ")[0] != str(req_data['applicant'])]
        target = st.selectbox("選擇買方 (受讓人)", options)
        
        if st.form_submit_button("✅ 確認過戶"):
            target_id = target.split(" | ")[0]
            succ, msg = sys.approve_request(req_data['id'], datetime.today().strftime("%Y-%m-%d"), req_data['applicant'], target_id, req_data['amount'])
            if succ: st.success(msg); time.sleep(1.5); st.rerun()
            else: st.error(msg)

@st.dialog("❌ 退件 (審核不通過)")
def show_reject_dialog(req_id):
    st.warning("您即將退回此申請")
    with st.form("reject_form"):
        reason = st.text_input("退件原因 (必填)", placeholder="例如：資料不符、暫停交易...")
        if st.form_submit_button("確認退件"):
            if not reason: st.error("請填寫原因")
            else:
                succ, msg = sys.reject_request(req_id, reason)
                if succ: st.success(msg); time.sleep(1.5); st.rerun()
                else: st.error(msg)

@st.dialog("🗑️ 刪除申請")
def show_cancel_request_dialog(req_id):
    st.warning("確定要撤銷此筆申請嗎？")
    if st.button("確認撤銷", type="primary"):
        succ, msg = sys.delete_request(req_id)
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
            menu_options = ["📝 我的持股", "📜 交易紀錄查詢", "✍️ 申請交易"]
        menu = st.radio("選單", menu_options)

    st.title("🏢 股務管理系統")

    if role == "admin":
        if menu == "✅ 審核交易申請":
            st.header("審核交易")
            df = sys.get_df("requests")
            if not df.empty and "status" in df.columns:
                pending = df[df["status"] == "Pending"]
                if pending.empty: st.info("無待審核案件")
                else:
                    st.dataframe(pending)
                    st.divider()
                    
                    # 取得所有股東名單供核定使用
                    df_users = sys.get_df("shareholders")
                    user_list = [f"{r['tax_id']} | {r['name']}" for i,r in df_users.iterrows()]
                    
                    for i, r in pending.iterrows():
                        with st.container():
                            c1, c2, c3, c4 = st.columns([2, 1, 2, 2])
                            c1.write(f"申請人: {r['applicant']}")
                            c2.write(f"股數: {r['amount']}")
                            c3.write(f"原因: {r.get('reason', '')}")
                            
                            b_col1, b_col2 = c4.columns(2)
                            if b_col1.button("核准", key=f"ok_{r['id']}"):
                                show_approve_dialog(r, user_list)
                            if b_col2.button("退件", key=f"no_{r['id']}"):
                                show_reject_dialog(r['id'])
                        st.markdown("---")
            else: st.info("無申請資料")

        # ... (其他 Admin 功能維持原樣，篇幅省略) ...
        elif menu == "📊 股東名簿總覽":
            df = sys.get_df("shareholders")
            st.dataframe(df) # 簡化顯示，完整版請保留原本邏輯
        elif menu == "📂 批次匯入":
            st.header("批次匯入")
            replace = st.checkbox("⚠️ 覆寫持股數")
            sample = pd.DataFrame(columns=["身分證或統編", "姓名", "身分別", "地址", "代表人", "持股數", "Email", "密碼提示"])
            buf = io.BytesIO(); sample.to_excel(buf, index=False); st.download_button("下載範本", buf, "template.xlsx")
            up = st.file_uploader("上傳 Excel", type=["xlsx"])
            if up and st.button("確認匯入"):
                try:
                    succ, msg = sys.batch_import_from_excel(pd.read_excel(up), replace)
                    st.success(msg) if succ else st.error(msg)
                except Exception as e: st.error(str(e))
        elif menu == "➕ 新增股東":
            with st.form("add"):
                tid = st.text_input("統編"); nm = st.text_input("姓名")
                tp = st.selectbox("類別", ["Individual", "Corporate"]); addr = st.text_input("地址")
                rep = st.text_input("代表人"); email = st.text_input("Email"); hint = st.text_input("提示")
                if st.form_submit_button("儲存"):
                    sys.upsert_shareholder(tid, nm, tp, addr, rep, email, hint)
                    st.success("成功")
        elif menu == "💰 發行/增資":
            df = sys.get_df("shareholders")
            ops = [f"{r['tax_id']} | {r['name']}" for i,r in df.iterrows()]
            tgt = st.selectbox("對象", ops); amt = st.number_input("股數", min_value=1)
            if st.button("發行"): sys.issue_shares(tgt.split(" | ")[0], amt); st.success("成功")
        elif menu == "🤝 股權過戶":
            df = sys.get_df("shareholders")
            ops = [f"{r['tax_id']} | {r['name']}" for i,r in df.iterrows()]
            s = st.selectbox("賣方", ops); b = st.selectbox("買方", ops); amt = st.number_input("股數", min_value=1)
            if st.button("過戶"): sys.transfer_shares(datetime.today(), s.split(" | ")[0], b.split(" | ")[0], amt, "Admin手動"); st.success("成功")
        elif menu == "📝 交易歷史":
            st.dataframe(sys.get_df("transactions"))

    else:
        # === 股東功能 ===
        if menu == "📝 我的持股":
            st.header(f"持股 - {user_name}")
            df = sys.get_df("shareholders")
            r = df[df['tax_id'].astype(str)==str(user_id)]
            if not r.empty:
                row = r.iloc[0]
                c1, c2, c3 = st.columns(3)
                c1.metric("持有股數", f"{row['shares_held']:,}")
                c2.metric("Email", row['email'])
                c3.metric("提示詞", row['password_hint'])
            else: st.warning("查無資料")

        elif menu == "📜 交易紀錄查詢":
            st.header("歷史交易明細")
            df = sys.get_df("transactions")
            if not df.empty:
                my = df[(df['seller_tax_id'].astype(str)==str(user_id)) | (df['buyer_tax_id'].astype(str)==str(user_id))]
                st.dataframe(my) if not my.empty else st.info("無紀錄")
            else: st.info("無紀錄")

        elif menu == "✍️ 申請交易":
            st.header("提出交易申請")
            
            # 1. 取得基本資料
            df_sh = sys.get_df("shareholders")
            me = df_sh[df_sh['tax_id'].astype(str) == str(user_id)]
            
            if not me.empty:
                my_shares = int(me.iloc[0]['shares_held'] or 0)
                
                # 2. 計算已申請但未核准的股數 (防呆)
                df_req = sys.get_df("requests")
                pending_sum = 0
                my_pending_reqs = pd.DataFrame()
                
                if not df_req.empty and "applicant" in df_req.columns:
                    # 篩選我的申請
                    my_reqs = df_req[df_req['applicant'].astype(str) == str(user_id)]
                    # 篩選 Pending 狀態
                    my_pending_reqs = my_reqs[my_reqs['status'] == "Pending"]
                    # 計算總和
                    if not my_pending_reqs.empty:
                        pending_sum = my_pending_reqs['amount'].sum()

                # 3. 顯示按鈕與對話框
                if st.button("📝 填寫申請單"):
                    show_request_dialog(user_id, my_shares, pending_sum)
                
                st.divider()
                st.subheader("申請進度 (待審核)")
                
                if not my_pending_reqs.empty:
                    # 顯示列表並提供刪除功能
                    for i, r in my_pending_reqs.iterrows():
                        c1, c2, c3, c4 = st.columns([2, 2, 3, 2])
                        c1.write(f"日期: {r['date']}")
                        c2.write(f"股數: {r['amount']}")
                        c3.write(f"原因: {r.get('reason', '')}")
                        if c4.button("撤銷", key=f"del_{r['id']}"):
                            show_cancel_request_dialog(r['id'])
                        st.markdown("---")
                    
                    st.info(f"目前凍結股數: {pending_sum:,} (待審核中，不可再次交易)")
                else:
                    st.info("目前無待審核的申請")
                
                # 顯示被退件或已完成的紀錄
                st.subheader("歷史申請紀錄")
                if not df_req.empty:
                     history = df_req[(df_req['applicant'].astype(str) == str(user_id)) & (df_req['status'] != "Pending")]
                     st.dataframe(history)

            else: st.error("無法讀取您的持股資料")

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
