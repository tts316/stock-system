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
st.set_page_config(page_title="股務管理系統 (抗429優化版)", layout="wide")

# Email 設定
SMTP_SERVER = "smtp.gmail.com"
SMTP_PORT = 587
SENDER_EMAIL = ""  
SENDER_PASSWORD = "" 

# --- 2. Google Sheets 資料庫核心邏輯 ---
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
            # 預先載入工作表物件
            self.ws_shareholders = self.sh.worksheet("shareholders")
            self.ws_transactions = self.sh.worksheet("transactions")
            self.ws_admin = self.sh.worksheet("system_admin")
        except Exception as e:
            st.error(f"連線 Google Sheets 失敗: {e}")
            st.stop()

    # --- [優化] 讀取資料 (加入自動重試機制) ---
    def get_df(self, table_name):
        retries = 3
        for i in range(retries):
            try:
                if table_name == "shareholders":
                    data = self.ws_shareholders.get_all_records()
                elif table_name == "transactions":
                    data = self.ws_transactions.get_all_records()
                return pd.DataFrame(data)
            except APIError as e:
                if "429" in str(e): # 若是配額額滿
                    time.sleep(2) # 等待 2 秒
                    if i == retries - 1: st.error("系統忙碌中，請稍後再試。")
                else:
                    st.error(f"讀取錯誤: {e}")
                    return pd.DataFrame()
        return pd.DataFrame()

    # --- 寫入操作 ---
    def upsert_shareholder(self, tax_id, name, holder_type, address, representative, email, hint):
        try:
            tax_id = str(tax_id).strip()
            if not hint: hint = "無提示"
            try:
                cell = self.ws_shareholders.find(tax_id)
            except APIError:
                time.sleep(1) # 避免讀取太快
                cell = self.ws_shareholders.find(tax_id)

            if cell:
                row = cell.row
                # 批次更新以節省額度
                self.ws_shareholders.batch_update([{
                    'range': f'B{row}:G{row}',
                    'values': [[name, holder_type, representative, address, email, hint]]
                }])
            else:
                self.ws_shareholders.append_row([tax_id, name, holder_type, representative, address, email, hint, 0, ""])
            return True, f"成功更新：{name}"
        except Exception as e:
            return False, str(e)

    def update_password(self, user_id, new_password, new_hint, is_admin=False):
        try:
            ws = self.ws_admin if is_admin else self.ws_shareholders
            cell = ws.find(user_id, in_column=1)
            if cell:
                row = cell.row
                if is_admin:
                    # Update Password(Col 2) & Hint(Col 4)
                    ws.update_cell(row, 2, new_password)
                    ws.update_cell(row, 4, new_hint)
                else:
                    # Update Password(Col 9) & Hint(Col 7)
                    ws.update_cell(row, 9, new_password)
                    ws.update_cell(row, 7, new_hint)
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
                    pwd = row_vals[8] if len(row_vals)>8 and row_vals[8] != "" else user_id
                return {"email": email, "hint": hint, "password": pwd}
            return None
        except: return None

    def verify_login(self, username, password, is_admin_attempt):
        try:
            ws = self.ws_admin if is_admin_attempt else self.ws_shareholders
            try:
                cell = ws.find(username, in_column=1)
            except APIError:
                time.sleep(1)
                cell = ws.find(username, in_column=1)
            
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

            if str(stored_pass) == str(password):
                return True, name, None
            else:
                return False, "密碼錯誤", stored_hint
        except Exception as e:
            return False, f"系統錯誤: {e}", None

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
                self.delete_shareholder(tid)
                time.sleep(0.5) # 稍微暫停避免刪除太快被擋
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

# --- [關鍵修改] 使用快取 Resource 避免重複連線 ---
@st.cache_resource
def get_db_system():
    return GoogleSheetDB()

try:
    sys = get_db_system()
except Exception as e:
    st.error("連線資料庫逾時，請重新整理頁面。")
    st.stop()

# --- 3. Email & Dialogs ---
def send_recovery_email(to_email, user_id, password):
    if not SENDER_EMAIL or not SENDER_PASSWORD:
        return True, f"【模擬發送】\n已發送密碼至 {to_email}。\n內容：您的帳號 {user_id} 密碼為 {password}"
    try:
        msg = MIMEText(f"親愛的用戶您好，\n\n您的帳號為：{user_id}\n您的密碼為：{password}\n\n請盡速登入並修改密碼。", 'plain', 'utf-8')
        msg['Subject'] = '【股務系統】密碼找回通知'
        msg['From'] = SENDER_EMAIL
        msg['To'] = to_email
        server = smtplib.SMTP(SMTP_SERVER, SMTP_PORT)
        server.starttls()
        server.login(SENDER_EMAIL, SENDER_PASSWORD)
        server.send_message(msg)
        server.quit()
        return True, f"已發送密碼信件至 {to_email}"
    except Exception as e:
        return False, f"發送失敗：{str(e)}"

@st.dialog("🔑 忘記密碼救援")
def show_forgot_password_dialog():
    st.info("請輸入您的帳號 (管理員輸入 admin，股東輸入統編)")
    user_input = st.text_input("帳號")
    if st.button("查詢資料"):
        if user_input:
            is_admin = (user_input == "admin")
            info = sys.get_user_recovery_info(user_input, is_admin)
            if info:
                st.success("✅ 找到帳號")
                st.markdown(f"**密碼提示：** {info['hint']}")
                st.divider()
                if info['email']:
                    st.write(f"Email: `{info['email']}`")
                    if st.button("📧 發送密碼到此 Email"):
                        succ, msg = send_recovery_email(info['email'], user_input, info['password'])
                        if succ: st.success(msg)
                        else: st.error(msg)
                else: st.warning("⚠️ 此帳號未設定 Email")
            else: st.error("❌ 找不到此帳號")

@st.dialog("✏️ 修改股東資料")
def show_edit_dialog(current_data):
    with st.form("edit_form"):
        new_tax_id = st.text_input("統編/身分證", value=str(current_data['tax_id']), disabled=True)
        new_name = st.text_input("姓名", value=current_data['name'])
        t_opts = ["Individual", "Corporate"]
        curr_type = current_data['holder_type']
        t_idx = t_opts.index(curr_type) if curr_type in t_opts else 0
        new_type = st.selectbox("類別", t_opts, index=t_idx)
        new_addr = st.text_input("地址", value=str(current_data['address']))
        new_rep = st.text_input("代表人", value=str(current_data['representative']))
        new_email = st.text_input("Email", value=str(current_data['email']))
        new_hint = st.text_input("密碼提示", value=str(current_data['password_hint']))
        if st.form_submit_button("確認更新"):
            succ, msg = sys.upsert_shareholder(new_tax_id, new_name, new_type, new_addr, new_rep, new_email, new_hint)
            if succ: st.success(msg); time.sleep(1); st.rerun()

@st.dialog("🔑 修改密碼")
def show_password_dialog(user_role, user_id):
    st.info("設定新密碼與密碼提示詞")
    with st.form("pwd_form"):
        p1 = st.text_input("新密碼", type="password")
        p2 = st.text_input("確認新密碼", type="password")
        new_hint = st.text_input("密碼提示詞", placeholder="例如：生日")
        if st.form_submit_button("修改"):
            if not p1 or not p2 or not new_hint: st.error("⚠️ 皆為必填")
            elif p1 != p2: st.error("⚠️ 密碼不一致")
            else:
                is_admin = (user_role == "admin")
                sys.update_password(user_id, p1, new_hint, is_admin)
                st.success("✅ 已更新"); time.sleep(1.5); st.session_state.logged_in = False; st.rerun()

@st.dialog("🗑️ 確認刪除")
def show_delete_dialog(tax_id, name):
    st.warning(f"確定刪除 {name} ({tax_id})？")
    if st.button("確認刪除", type="primary"):
        sys.delete_shareholder(tax_id)
        st.success("刪除成功"); time.sleep(1); st.rerun()

@st.dialog("🗑️ 批次刪除確認")
def show_batch_delete_dialog(selected_list):
    st.warning(f"即將刪除 {len(selected_list)} 位股東，確定嗎？")
    st.write(selected_list)
    if st.button("🔥 確定全部刪除", type="primary"):
        ids = [i.split(" | ")[0] for i in selected_list]
        succ, msg = sys.delete_batch_shareholders(ids)
        if succ:
            st.success(msg)
            for k in list(st.session_state.keys()):
                if k.startswith("sel_"): del st.session_state[k]
            time.sleep(1.5); st.rerun()
        else: st.error(msg)

# --- Main App ---
def run_main_app(role, user_name, user_id):
    with st.sidebar:
        st.markdown(f"### 👋 {user_name}")
        st.caption(f"身分：{role}")
        if st.button("🔑 修改密碼"): show_password_dialog(role, user_id)
        if st.button("登出"): st.session_state.logged_in = False; st.rerun()
        st.divider()
        if role == "admin":
            menu_options = ["📊 股東名簿總覽", "📂 批次匯入 (Excel)", "➕ 新增/編輯股東", "💰 發行/增資", "🤝 股權過戶 (交易)", "📝 交易歷史紀錄"]
        else:
            menu_options = ["📝 我的持股資訊"]
        menu = st.radio("功能選單", menu_options)

    st.title("🏢 聯成電腦 - 股務系統")

    if role == "admin":
        if menu == "📊 股東名簿總覽":
            st.header("股東名簿")
            df = sys.get_df("shareholders")
            if df.empty: st.info("尚無資料")
            else:
                c1, c2 = st.columns(2)
                c1.metric("👥 人數", len(df))
                c2.metric("💰 總股數", f"{df['shares_held'].sum():,}")
                search = st.text_input("🔍 搜尋")
                if search: df = df[df['name'].astype(str).str.contains(search) | df['tax_id'].astype(str).str.contains(search)]
                st.divider()
                def toggle_all():
                    val = st.session_state.master_select
                    for t in df['tax_id']: st.session_state[f"sel_{t}"] = val
                sel_ids = []
                for t in df['tax_id']:
                    if st.session_state.get(f"sel_{t}", False):
                        n = df[df['tax_id']==t].iloc[0]['name']
                        sel_ids.append(f"{t} | {n}")
                tc1, tc2 = st.columns([1, 4])
                with tc1: st.checkbox("全選", key="master_select", on_change=toggle_all)
                with tc2:
                    if sel_ids:
                        if st.button(f"🗑️ 刪除 ({len(sel_ids)})", type="primary"): show_batch_delete_dialog(sel_ids)
                
                cols = [0.5, 1.5, 1.5, 2, 1, 2]
                h = st.columns(cols)
                h[1].write("**統編**"); h[2].write("**姓名**"); h[3].write("**Email**"); h[4].write("**股數**"); h[5].write("**操作**")
                st.divider()
                for idx, row in df.iterrows():
                    with st.container():
                        c = st.columns(cols, vertical_alignment="center")
                        c[0].checkbox("", key=f"sel_{row['tax_id']}", label_visibility="collapsed")
                        c[1].write(str(row['tax_id']))
                        c[2].write(row['name'])
                        c[3].write(row['email'])
                        c[4].write(f"{row['shares_held']:,}")
                        with c[5]:
                            b1, b2 = st.columns(2)
                            if b1.button("✏️", key=f"e_{row['tax_id']}"): show_edit_dialog(row)
                            if b2.button("🗑️", key=f"d_{row['tax_id']}"): show_delete_dialog(row['tax_id'], row['name'])
                        st.markdown("---")

        elif menu == "📂 批次匯入 (Excel)":
            st.header("批次匯入")
            replace_shares = st.checkbox("⚠️ 覆寫持股數")
            sample = pd.DataFrame(columns=["身分證或統編", "姓名", "身分別", "地址", "代表人", "持股數", "Email", "密碼提示"])
            buf = io.BytesIO()
            with pd.ExcelWriter(buf, engine='xlsxwriter') as writer: sample.to_excel(writer, index=False)
            st.download_button("📥 下載範本", buf, "範本.xlsx")
            up_file = st.file_uploader("上傳 Excel", type=["xlsx"])
            if up_file and st.button("確認匯入"):
                try:
                    df_up = pd.read_excel(up_file)
                    cnt = 0
                    for i, r in df_up.iterrows():
                        tid = str(r.get("身分證或統編", "")).strip()
                        if not tid: continue
                        nm = str(r.get("姓名", "")).strip()
                        tp = "Corporate" if "法人" in str(r.get("身分別", "")) else "Individual"
                        addr = str(r.get("地址", "")); rep = str(r.get("代表人", ""))
                        email = str(r.get("Email", "")); hint = str(r.get("密碼提示", ""))
                        sys.upsert_shareholder(tid, nm, tp, addr, rep, email, hint)
                        try:
                            qty = int(r.get("持股數", 0))
                            if qty >= 0:
                                if replace_shares: sys.set_share_count(tid, qty)
                                else: sys.issue_shares(tid, qty)
                        except: pass
                        cnt+=1
                    st.success(f"已處理 {cnt} 筆"); time.sleep(2); st.rerun()
                except Exception as e: st.error(f"Error: {e}")

        elif menu == "➕ 新增/編輯股東":
            st.header("手動新增")
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
            st.header("發行")
            df = sys.get_df("shareholders")
            if not df.empty:
                ops = [f"{r['tax_id']} | {r['name']}" for i,r in df.iterrows()]
                tgt = st.selectbox("對象", ops)
                amt = st.number_input("股數", min_value=1)
                if st.button("發行"):
                    tid = tgt.split(" | ")[0]
                    sys.issue_shares(tid, amt)
                    st.success("成功")
            else: st.warning("無資料")

        elif menu == "🤝 股權過戶 (交易)":
            st.header("過戶")
            df = sys.get_df("shareholders")
            if len(df)>=2:
                ops = [f"{r['tax_id']} | {r['name']}" for i,r in df.iterrows()]
                s = st.selectbox("賣方", ops)
                b = st.selectbox("買方", ops)
                amt = st.number_input("股數", min_value=1)
                reason = st.text_input("原因", value="買賣")
                dt = st.date_input("日期", datetime.today())
                if st.button("過戶"):
                    sid = s.split(" | ")[0]; bid = b.split(" | ")[0]
                    if sid==bid: st.error("相同")
                    else:
                        succ, msg = sys.transfer_shares(dt, sid, bid, amt, reason)
                        if succ: st.success(msg)
                        else: st.error(msg)
            else: st.warning("人數不足")

        elif menu == "📝 交易歷史紀錄":
            st.header("歷史紀錄")
            st.dataframe(sys.get_df("transactions"), use_container_width=True)

    else:
        menu == "📝 我的持股資訊"
        st.header(f"持股資訊 - {user_name}")
        df = sys.get_df("shareholders")
        r = df[df['tax_id'].astype(str) == str(user_id)]
        if not r.empty:
            row = r.iloc[0]
            c1, c2, c3 = st.columns(3)
            c1.metric("股數", f"{row['shares_held']:,}")
            c2.metric("Email", row['email'])
            c3.metric("提示", row['password_hint'])
            st.info(f"統編: {row['tax_id']}")
            st.text_input("地址", value=row['address'], disabled=True)
        else: st.warning("無資料")

if __name__ == "__main__":
    if 'logged_in' not in st.session_state:
        st.session_state.logged_in = False
        st.session_state.user_role = None; st.session_state.user_name = None; st.session_state.user_id = None

    if not st.session_state.logged_in:
        c1, c2, c3 = st.columns([1, 2, 1])
        with c2:
            st.markdown("## 🔒 系統登入")
            acc = st.text_input("帳號 (admin 或 統編)")
            pwd = st.text_input("密碼", type="password")
            cb1, cb2 = st.columns(2)
            if cb1.button("登入", type="primary", use_container_width=True):
                if acc == "admin":
                    valid, msg, hint = sys.verify_login(acc, pwd, True)
                    if valid:
                        st.session_state.logged_in = True
                        st.session_state.user_role = "admin"
                        st.session_state.user_name = msg
                        st.session_state.user_id = acc
                        st.rerun()
                    else: st.error(msg)
                else:
                    valid, msg, hint = sys.verify_login(acc, pwd, False)
                    if valid:
                        st.session_state.logged_in = True
                        st.session_state.user_role = "shareholder"
                        st.session_state.user_name = msg
                        st.session_state.user_id = acc
                        st.rerun()
                    else:
                        st.error(msg)
                        if hint: st.info(f"提示: {hint}")
            if cb2.button("忘記密碼", use_container_width=True):
                show_forgot_password_dialog()
    else:
        run_main_app(st.session_state.user_role, st.session_state.user_name, st.session_state.user_id)
