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
st.set_page_config(page_title="股務管理系統 (全功能完整版)", layout="wide")

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

    # --- 核心功能: 批次匯入 (極速版) ---
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

            self.ws_shareholders.clear()
            self.ws_shareholders.append_row(headers)
            self.ws_shareholders.append_rows(final_data)
            return True, f"處理完成！共 {updated_count} 筆，資料庫總計 {len(final_data)} 人。"
        except Exception as e: return False, f"匯入失敗: {str(e)}"

    # --- 申請單相關 ---
    def add_request(self, applicant_id, target_id, amount):
        try:
            req_id = int(time.time())
            date_str = datetime.now().strftime("%Y-%m-%d")
            self.ws_requests.append_row([req_id, date_str, applicant_id, target_id, amount, "Pending"])
            return True, "申請已送出，待管理員審核。"
        except Exception as e: return False, str(e)

    def approve_request(self, req_id, date, seller_id, buyer_id, amount):
        try:
            success, msg = self.transfer_shares(date, seller_id, buyer_id, amount, "股東申請交易")
            if not success: return False, msg
            cell = self.ws_requests.find(str(req_id), in_column=1)
            if cell: self.ws_requests.update_cell(cell.row, 6, "Approved")
            return True, "審核通過，已過戶！"
        except Exception as e: return False, str(e)

    # --- 基本功能 ---
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

    def delete_shareholder(self, tax_id):
        try:
            cell = self.ws_shareholders.find(tax_id, in_column=1)
            if cell: self.ws_shareholders.delete_rows(cell.row)
        except: pass
        
    def delete_batch_shareholders(self, tax_id_list):
        try:
            # 為了安全且簡單，重整整個表
            current = self.ws_shareholders.get_all_records()
            new_recs = [r for r in current if str(r['tax_id']) not in tax_id_list]
            headers = ["tax_id", "name", "holder_type", "representative", "address", "email", "password_hint", "shares_held", "password"]
            final_data = []
            for item in new_recs:
                final_data.append([
                    item['tax_id'], item['name'], item['holder_type'], item['representative'],
                    item['address'], item['email'], item['password_hint'], item['shares_held'], item['password']
                ])
            self.ws_shareholders.clear()
            self.ws_shareholders.append_row(headers)
            self.ws_shareholders.append_rows(final_data)
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
        msg['Subject'] = '密碼找回'
        msg['From'] = SENDER_EMAIL
        msg['To'] = to_email
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

@st.dialog("✏️ 修改")
def show_edit_dialog(current_data):
    with st.form("edit_form"):
        new_tax_id = st.text_input("統編", value=str(current_data['tax_id']), disabled=True)
        new_name = st.text_input("姓名", value=current_data['name'])
        t_opts = ["Individual", "Corporate"]
        curr_type = current_data['holder_type']
        t_idx = t_opts.index(curr_type) if curr_type in t_opts else 0
        new_type = st.selectbox("類別", t_opts, index=t_idx)
        new_addr = st.text_input("地址", value=str(current_data['address']))
        new_rep = st.text_input("代表人", value=str(current_data['representative']))
        new_email = st.text_input("Email", value=str(current_data['email']))
        new_hint = st.text_input("提示", value=str(current_data['password_hint']))
        if st.form_submit_button("更新"):
            succ, msg = sys.upsert_shareholder(new_tax_id, new_name, new_type, new_addr, new_rep, new_email, new_hint)
            if succ: st.success(msg); time.sleep(1); st.rerun()

@st.dialog("🗑️ 刪除")
def show_delete_dialog(tax_id, name):
    st.warning(f"刪除 {name} ({tax_id})？")
    if st.button("確認", type="primary"):
        sys.delete_shareholder(tax_id); st.success("已刪除"); time.sleep(1); st.rerun()

@st.dialog("🗑️ 批次刪除")
def show_batch_delete_dialog(selected_list):
    st.warning(f"刪除 {len(selected_list)} 筆？")
    st.write(selected_list)
    if st.button("確認刪除", type="primary"):
        ids = [i.split(" | ")[0] for i in selected_list]
        succ, msg = sys.delete_batch_shareholders(ids)
        if succ:
            st.success(msg)
            for k in list(st.session_state.keys()):
                if k.startswith("sel_"): del st.session_state[k]
            time.sleep(1.5); st.rerun()
        else: st.error(msg)

@st.dialog("✍️ 申請交易")
def show_request_dialog(applicant_id, shareholder_list):
    st.info("填寫申請單")
    with st.form("req_form"):
        target = st.selectbox("轉讓對象", shareholder_list)
        amount = st.number_input("股數", min_value=1)
        if st.form_submit_button("送出"):
            tid = target.split(" | ")[0]
            if tid == applicant_id: st.error("不能轉給自己")
            else:
                succ, msg = sys.add_request(applicant_id, tid, amount)
                if succ: st.success(msg); time.sleep(1.5); st.rerun()
                else: st.error(msg)

@st.dialog("📋 審核確認")
def show_approve_dialog(req_data):
    st.warning("核准此交易？")
    st.write(f"賣方: {req_data['applicant']}"); st.write(f"買方: {req_data['target']}"); st.write(f"股數: {req_data['amount']}")
    if st.button("✅ 核准"):
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
            menu_options = ["📝 我的持股", "📜 交易紀錄查詢", "✍️ 申請交易"]
        menu = st.radio("選單", menu_options)

    st.title("🏢 股務管理系統")

    if role == "admin":
        if menu == "📊 股東名簿總覽":
            st.header("股東名簿")
            df = sys.get_df("shareholders")
            if not df.empty:
                c1, c2 = st.columns(2)
                c1.metric("人數", len(df)); c2.metric("股數", f"{df['shares_held'].sum():,}")
                search = st.text_input("搜尋")
                if search: df = df[df['name'].astype(str).str.contains(search) | df['tax_id'].astype(str).str.contains(search)]
                
                # Batch Delete
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
                    if c2.button(f"刪除選取 ({len(sel_ids)})"): show_batch_delete_dialog(sel_ids)

                cols = [0.5, 1.5, 1.5, 2, 1, 2]
                h = st.columns(cols)
                h[1].write("統編"); h[2].write("姓名"); h[3].write("Email"); h[4].write("股數"); h[5].write("操作")
                st.divider()
                for i, r in df.iterrows():
                    with st.container():
                        c = st.columns(cols, vertical_alignment="center")
                        c[0].checkbox("選取", key=f"sel_{r['tax_id']}", label_visibility="collapsed")
                        c[1].write(str(r['tax_id'])); c[2].write(r['name']); c[3].write(r['email']); c[4].write(f"{r['shares_held']:,}")
                        with c[5]:
                            b1, b2 = st.columns(2)
                            if b1.button("✏️", key=f"e_{r['tax_id']}"): show_edit_dialog(r)
                            if b2.button("🗑️", key=f"d_{r['tax_id']}"): show_delete_dialog(r['tax_id'], r['name'])
                    st.markdown("---")
            else: st.info("無資料")

        elif menu == "📂 批次匯入":
            st.header("批次匯入")
            replace = st.checkbox("⚠️ 覆寫持股數")
            sample = pd.DataFrame(columns=["身分證或統編", "姓名", "身分別", "地址", "代表人", "持股數", "Email", "密碼提示"])
            buf = io.BytesIO()
            with pd.ExcelWriter(buf, engine='xlsxwriter') as writer: sample.to_excel(writer, index=False)
            st.download_button("下載範本", buf, "template.xlsx")
            up = st.file_uploader("上傳 Excel", type=["xlsx"])
            if up and st.button("確認匯入"):
                try:
                    df_up = pd.read_excel(up)
                    succ, msg = sys.batch_import_from_excel(df_up, replace)
                    if succ: st.success(msg); time.sleep(2); st.rerun()
                    else: st.error(msg)
                except Exception as e: st.error(str(e))

        elif menu == "✅ 審核交易申請":
            st.header("審核交易")
            df = sys.get_df("requests")
            if not df.empty and "status" in df.columns:
                pending = df[df["status"] == "Pending"]
                if pending.empty: st.info("無待審核")
                else:
                    st.dataframe(pending); st.divider()
                    for i, r in pending.iterrows():
                        c1, c2, c3, c4 = st.columns(4)
                        c1.write(f"賣: {r['applicant']}"); c2.write(f"買: {r['target']}"); c3.write(f"股: {r['amount']}")
                        if c4.button("審核", key=f"ap_{r['id']}"): show_approve_dialog(r)
            else: st.info("無申請資料")

        elif menu == "➕ 新增股東":
            with st.form("add"):
                c1, c2 = st.columns(2)
                tid = c1.text_input("統編"); nm = c2.text_input("姓名")
                tp = st.selectbox("類別", ["Individual", "Corporate"])
                addr = st.text_input("地址"); rep = st.text_input("代表人")
                email = st.text_input("Email"); hint = st.text_input("提示")
                if st.form_submit_button("儲存"):
                    if tid and nm:
                        sys.upsert_shareholder(tid, nm, tp, addr, rep, email, hint)
                        st.success("成功"); time.sleep(1); st.rerun()
                    else: st.error("缺資料")

        elif menu == "💰 發行/增資":
            df = sys.get_df("shareholders")
            if not df.empty:
                ops = [f"{r['tax_id']} | {r['name']}" for i,r in df.iterrows()]
                tgt = st.selectbox("對象", ops); amt = st.number_input("股數", min_value=1)
                if st.button("發行"):
                    sys.issue_shares(tgt.split(" | ")[0], amt); st.success("成功")
            else: st.warning("無資料")

        elif menu == "🤝 股權過戶":
            df = sys.get_df("shareholders")
            if len(df)>=2:
                ops = [f"{r['tax_id']} | {r['name']}" for i,r in df.iterrows()]
                s = st.selectbox("賣方", ops); b = st.selectbox("買方", ops)
                amt = st.number_input("股數", min_value=1)
                if st.button("過戶"):
                    msg = sys.transfer_shares(datetime.today(), s.split(" | ")[0], b.split(" | ")[0], amt, "Admin手動")
                    st.success(msg) if "成功" in msg else st.error(msg)
            else: st.warning("人數不足")

        elif menu == "📝 交易歷史":
            st.dataframe(sys.get_df("transactions"), use_container_width=True)

    else:
        # 股東功能
        if menu == "📝 我的持股":
            st.header(f"持股 - {user_name}")
            df = sys.get_df("shareholders")
            r = df[df['tax_id'].astype(str)==str(user_id)]
            if not r.empty:
                row = r.iloc[0]
                c1, c2, c3 = st.columns(3)
                c1.metric("股數", f"{row['shares_held']:,}")
                c2.metric("Email", row['email'])
                c3.metric("提示", row['password_hint'])
                st.info(f"統編: {row['tax_id']}")
                st.text_input("地址", value=row['address'], disabled=True)
            else: st.warning("無資料")
        
        elif menu == "📜 交易紀錄查詢":
            st.header("交易紀錄")
            df = sys.get_df("transactions")
            if not df.empty:
                my = df[(df['seller_tax_id'].astype(str)==str(user_id)) | (df['buyer_tax_id'].astype(str)==str(user_id))]
                st.dataframe(my) if not my.empty else st.info("無紀錄")
            else: st.info("無紀錄")

        elif menu == "✍️ 申請交易":
            st.header("申請轉讓")
            df = sys.get_df("shareholders")
            others = df[df['tax_id'].astype(str)!=str(user_id)]
            if not others.empty:
                ops = [f"{r['tax_id']} | {r['name']}" for i,r in others.iterrows()]
                if st.button("填寫申請單"): show_request_dialog(user_id, ops)
                st.divider(); st.subheader("我的申請")
                reqs = sys.get_df("requests")
                if not reqs.empty and "applicant" in reqs.columns:
                    st.dataframe(reqs[reqs['applicant'].astype(str)==str(user_id)])
            else: st.warning("無對象")

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
            c1, c2 = st.columns(2)
            if c1.button("登入", type="primary", use_container_width=True):
                if acc=="admin":
                    v, m, h = sys.verify_login(acc, pwd, True)
                    if v: st.session_state.logged_in=True; st.session_state.user_role="admin"; st.session_state.user_name=m; st.session_state.user_id=acc; st.rerun()
                    else: st.error(m)
                else:
                    v, m, h = sys.verify_login(acc, pwd, False)
                    if v: st.session_state.logged_in=True; st.session_state.user_role="shareholder"; st.session_state.user_name=m; st.session_state.user_id=acc; st.rerun()
                    else: 
                        st.error(m)
                        if h: st.info(f"提示: {h}")
            if c2.button("忘記密碼", use_container_width=True): show_forgot_password_dialog()
    else:
        run_main_app(st.session_state.user_role, st.session_state.user_name, st.session_state.user_id)
