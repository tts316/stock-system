import streamlit as st
import sqlite3
import pandas as pd
from datetime import datetime
import io
import time
import smtplib
from email.mime.text import MIMEText

# --- 1. 系統設定區 ---
st.set_page_config(page_title="股務管理系統 (批次管理版)", layout="wide")

DEFAULT_ADMIN_USER = "admin"
DEFAULT_ADMIN_PASS = "admin888"
DEFAULT_ADMIN_EMAIL = "admin@company.com"
DEFAULT_ADMIN_HINT = "公司預設密碼"

# Email 設定 (若無則使用模擬模式)
SMTP_SERVER = "smtp.gmail.com"
SMTP_PORT = 587
SENDER_EMAIL = ""
SENDER_PASSWORD = ""

# --- 2. 資料庫核心邏輯 ---
class StockSystem:
    def __init__(self, db_name="company_stock.db"):
        self.db_name = db_name
        self.create_tables()
        self.init_admin()

    def get_connection(self):
        return sqlite3.connect(self.db_name)

    def create_tables(self):
        conn = self.get_connection()
        cursor = conn.cursor()
        
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS shareholders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                tax_id TEXT UNIQUE NOT NULL,
                name TEXT NOT NULL,
                holder_type TEXT CHECK(holder_type IN ('Individual', 'Corporate')),
                representative TEXT,
                address TEXT,
                email TEXT,
                password_hint TEXT,
                shares_held INTEGER DEFAULT 0,
                password TEXT DEFAULT NULL
            )
        ''')
        
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS transactions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                date TEXT,
                seller_tax_id TEXT,
                buyer_tax_id TEXT,
                amount INTEGER,
                reason TEXT
            )
        ''')
        
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS system_admin (
                username TEXT PRIMARY KEY,
                password TEXT NOT NULL,
                email TEXT,
                password_hint TEXT
            )
        ''')
        conn.commit()
        conn.close()

    def init_admin(self):
        conn = self.get_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM system_admin WHERE username = ?", (DEFAULT_ADMIN_USER,))
        if not cursor.fetchone():
            cursor.execute("INSERT INTO system_admin (username, password, email, password_hint) VALUES (?, ?, ?, ?)", 
                           (DEFAULT_ADMIN_USER, DEFAULT_ADMIN_PASS, DEFAULT_ADMIN_EMAIL, DEFAULT_ADMIN_HINT))
            conn.commit()
        conn.close()

    # --- 資料操作 ---
    def upsert_shareholder(self, tax_id, name, holder_type, address, representative, email, hint):
        conn = self.get_connection()
        try:
            tax_id = str(tax_id).strip()
            if not hint: hint = "無提示"
            conn.execute('''
                INSERT INTO shareholders (tax_id, name, holder_type, address, representative, email, password_hint)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(tax_id) DO UPDATE SET
                name=excluded.name, address=excluded.address, 
                representative=excluded.representative, holder_type=excluded.holder_type,
                email=excluded.email, password_hint=excluded.password_hint
            ''', (tax_id, name, holder_type, address, representative, email, hint))
            conn.commit()
            return True, f"成功更新：{name}"
        except Exception as e:
            return False, str(e)
        finally:
            conn.close()

    def update_password(self, user_id, new_password, new_hint, is_admin=False):
        conn = self.get_connection()
        try:
            if is_admin:
                conn.execute("UPDATE system_admin SET password = ?, password_hint = ? WHERE username = ?", (new_password, new_hint, user_id))
            else:
                conn.execute("UPDATE shareholders SET password = ?, password_hint = ? WHERE tax_id = ?", (new_password, new_hint, user_id))
            conn.commit()
            return True
        except Exception as e: return False
        finally: conn.close()

    def get_user_recovery_info(self, user_id, is_admin=False):
        conn = self.get_connection()
        cursor = conn.cursor()
        if is_admin:
            cursor.execute("SELECT email, password_hint, password FROM system_admin WHERE username = ?", (user_id,))
        else:
            cursor.execute("SELECT email, password_hint, password FROM shareholders WHERE tax_id = ?", (user_id,))
        res = cursor.fetchone()
        conn.close()
        if res:
            pwd = res[2] if res[2] else user_id 
            return {"email": res[0], "hint": res[1], "password": pwd}
        return None

    def verify_login(self, username, password, is_admin_attempt):
        conn = self.get_connection()
        cursor = conn.cursor()
        if is_admin_attempt:
            cursor.execute("SELECT password, password_hint FROM system_admin WHERE username = ?", (username,))
            res = cursor.fetchone()
            conn.close()
            if not res: return False, "無此帳號", None
            stored_pass, stored_hint = res
            if stored_pass == password: return True, "系統管理員", None
            else: return False, "密碼錯誤", stored_hint
        else:
            cursor.execute("SELECT name, password, password_hint FROM shareholders WHERE tax_id = ?", (username,))
            res = cursor.fetchone()
            conn.close()
            if not res: return False, "無此帳號", None
            name, stored_pass, stored_hint = res
            actual_pass = stored_pass if stored_pass is not None else username
            if password == actual_pass: return True, name, None
            else:
                hint_msg = stored_hint if stored_hint else "未設定提示"
                return False, "密碼錯誤", hint_msg

    def get_df(self, table_name):
        conn = self.get_connection()
        df = pd.read_sql_query(f"SELECT * FROM {table_name}", conn)
        conn.close()
        return df

    def issue_shares(self, tax_id, amount):
        conn = self.get_connection()
        conn.execute("UPDATE shareholders SET shares_held = shares_held + ? WHERE tax_id = ?", (amount, tax_id))
        conn.commit()
        conn.close()

    def set_share_count(self, tax_id, amount):
        conn = self.get_connection()
        conn.execute("UPDATE shareholders SET shares_held = ? WHERE tax_id = ?", (amount, tax_id))
        conn.commit()
        conn.close()
        
    def delete_shareholder(self, tax_id):
        conn = self.get_connection()
        conn.execute("DELETE FROM shareholders WHERE tax_id = ?", (tax_id,))
        conn.commit()
        conn.close()

    def delete_batch_shareholders(self, tax_id_list):
        conn = self.get_connection()
        try:
            # 批次刪除
            placeholders = ','.join('?' for _ in tax_id_list)
            query = f"DELETE FROM shareholders WHERE tax_id IN ({placeholders})"
            conn.execute(query, tax_id_list)
            conn.commit()
            return True, f"已刪除 {len(tax_id_list)} 筆資料"
        except Exception as e:
            return False, str(e)
        finally:
            conn.close()

    def transfer_shares(self, date, seller_tax_id, buyer_tax_id, amount, reason):
        conn = self.get_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT shares_held FROM shareholders WHERE tax_id = ?", (seller_tax_id,))
        res = cursor.fetchone()
        seller_shares = res[0] if res else 0
        if seller_shares < amount:
            conn.close()
            return False, f"股數不足"
        try:
            cursor.execute("UPDATE shareholders SET shares_held = shares_held - ? WHERE tax_id = ?", (amount, seller_tax_id))
            cursor.execute("UPDATE shareholders SET shares_held = shares_held + ? WHERE tax_id = ?", (amount, buyer_tax_id))
            cursor.execute('''
                INSERT INTO transactions (date, seller_tax_id, buyer_tax_id, amount, reason)
                VALUES (?, ?, ?, ?, ?)
            ''', (date, seller_tax_id, buyer_tax_id, amount, reason))
            conn.commit()
            return True, "過戶成功"
        except Exception as e:
            conn.rollback()
            return False, str(e)
        finally:
            conn.close()

sys = StockSystem()

# --- 3. Email 發送 ---
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

# --- 4. 彈出視窗 ---
@st.dialog("🔑 忘記密碼救援")
def show_forgot_password_dialog():
    st.info("請輸入您的帳號 (管理員輸入 admin，股東輸入統編)")
    user_input = st.text_input("帳號")
    if st.button("查詢資料"):
        if user_input:
            is_admin = (user_input == DEFAULT_ADMIN_USER)
            info = sys.get_user_recovery_info(user_input, is_admin)
            if info:
                st.success("✅ 找到帳號")
                st.markdown(f"**密碼提示：** {info['hint'] if info['hint'] else '未設定'}")
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
        new_tax_id = st.text_input("統編/身分證", value=current_data['tax_id'], disabled=True)
        new_name = st.text_input("姓名", value=current_data['name'])
        t_idx = 0 if current_data['holder_type'] == "Individual" else 1
        new_type = st.selectbox("類別", ["Individual", "Corporate"], index=t_idx)
        new_addr = st.text_input("地址", value=current_data['address'] if current_data['address'] else "")
        new_rep = st.text_input("代表人", value=current_data['representative'] if current_data['representative'] else "")
        new_email = st.text_input("Email", value=current_data['email'] if current_data['email'] else "")
        new_hint = st.text_input("密碼提示", value=current_data['password_hint'] if current_data['password_hint'] else "")
        if st.form_submit_button("確認更新"):
            succ, msg = sys.upsert_shareholder(new_tax_id, new_name, new_type, new_addr, new_rep, new_email, new_hint)
            if succ:
                st.success(msg)
                time.sleep(1)
                st.rerun()

@st.dialog("🔑 修改密碼")
def show_password_dialog(user_role, user_id):
    st.info("設定新密碼與密碼提示詞 (皆為必填)")
    with st.form("pwd_form"):
        p1 = st.text_input("新密碼", type="password")
        p2 = st.text_input("確認新密碼", type="password")
        new_hint = st.text_input("密碼提示詞", placeholder="例如：生日、寵物名")
        if st.form_submit_button("修改"):
            if not p1 or not p2 or not new_hint:
                st.error("⚠️ 皆為必填")
            elif p1 != p2:
                st.error("⚠️ 密碼不一致")
            else:
                is_admin = (user_role == "admin")
                sys.update_password(user_id, p1, new_hint, is_admin)
                st.success("✅ 已更新，請重新登入。")
                time.sleep(1.5)
                st.session_state.logged_in = False
                st.rerun()

@st.dialog("🗑️ 確認刪除 (單筆)")
def show_delete_dialog(tax_id, name):
    st.warning(f"您確定要刪除「{name} ({tax_id})」嗎？\n此動作無法復原！")
    if st.button("確認刪除", type="primary"):
        sys.delete_shareholder(tax_id)
        st.success("刪除成功")
        time.sleep(1)
        st.rerun()

@st.dialog("🗑️ 批次刪除確認")
def show_batch_delete_dialog(selected_list):
    st.warning(f"您即將刪除以下 **{len(selected_list)} 位** 股東：")
    st.write(selected_list)
    st.error("⚠️ 此動作無法復原！請再次確認。")
    
    col1, col2 = st.columns(2)
    if col1.button("🔥 確定全部刪除", type="primary"):
        # 提取 ID
        ids_to_del = [item.split(" | ")[0] for item in selected_list]
        succ, msg = sys.delete_batch_shareholders(ids_to_del)
        if succ:
            st.success(msg)
            # 清除 session 選擇狀態
            for key in list(st.session_state.keys()):
                if key.startswith("sel_"):
                    del st.session_state[key]
            time.sleep(1.5)
            st.rerun()
        else:
            st.error(msg)
    if col2.button("取消"):
        st.rerun()

# --- 5. 主功能介面 ---
def run_main_app(role, user_name, user_id):
    with st.sidebar:
        st.markdown(f"### 👋 {user_name}")
        st.caption(f"身分：{role}")
        if st.button("🔑 修改密碼"): show_password_dialog(role, user_id)
        if st.button("登出"):
            st.session_state.logged_in = False
            st.rerun()
        st.divider()

        if role == "admin":
            menu_options = ["📊 股東名簿總覽", "📂 批次匯入 (Excel)", "➕ 新增/編輯股東", "💰 發行/增資", "🤝 股權過戶 (交易)", "📝 交易歷史紀錄"]
        else:
            menu_options = ["📝 我的持股資訊"]
        menu = st.radio("功能選單", menu_options)

    st.title("🏢 聯成電腦 - 股務管理系統")

    if role == "admin":
        # --- 頁面 1: 股東名簿 (含全選與批次刪除) ---
        if menu == "📊 股東名簿總覽":
            st.header("股東名簿管理")
            df = sys.get_df("shareholders")
            
            c1, c2 = st.columns(2)
            c1.metric("👥 人數", len(df))
            c2.metric("💰 總股數", f"{df['shares_held'].sum():,}")
            
            # 搜尋過濾
            search = st.text_input("🔍 搜尋", placeholder="姓名或統編...")
            if search:
                df = df[df['name'].str.contains(search) | df['tax_id'].str.contains(search)]

            st.divider()
            
            # 下載
            buf = io.BytesIO()
            with pd.ExcelWriter(buf, engine='xlsxwriter') as writer: df.to_excel(writer, index=False)
            st.download_button("📥 下載 Excel", buf, f"股東名簿_{datetime.now().strftime('%Y%m%d')}.xlsx")
            
            st.markdown("### 📋 詳細名單")

            # --- 全選功能邏輯 ---
            def toggle_select_all():
                is_checked = st.session_state.master_select
                for tid in df['tax_id']:
                    st.session_state[f"sel_{tid}"] = is_checked

            # --- 批次刪除檢查邏輯 ---
            # 統計目前被勾選的人
            selected_ids = []
            for tid in df['tax_id']:
                if st.session_state.get(f"sel_{tid}", False):
                    # 找出對應的名字
                    name = df[df['tax_id'] == tid].iloc[0]['name']
                    selected_ids.append(f"{tid} | {name}")
            
            # 工具列：顯示全選與批次刪除按鈕
            tool_col1, tool_col2 = st.columns([1, 4])
            with tool_col1:
                # 全選 Checkbox
                st.checkbox("✅ 全選", key="master_select", on_change=toggle_select_all)
            
            with tool_col2:
                if selected_ids:
                    if st.button(f"🗑️ 批次刪除 ({len(selected_ids)} 人)", type="primary"):
                        show_batch_delete_dialog(selected_ids)

            # --- 列表顯示 ---
            # 定義欄位寬度: [勾選, 統編, 姓名, Email, 股數, 操作]
            col_ratio = [0.5, 1.5, 1.5, 2, 1, 2] 
            h0, h1, h2, h3, h4, h5 = st.columns(col_ratio)
            h0.write("") # 勾選欄位標題留空
            h1.markdown("**統編**")
            h2.markdown("**姓名**")
            h3.markdown("**Email**")
            h4.markdown("**股數**")
            h5.markdown("**操作**")
            st.divider()

            for idx, row in df.iterrows():
                with st.container():
                    c0, c1, c2, c3, c4, c5 = st.columns(col_ratio, vertical_alignment="center")
                    
                    # 勾選框 (Key 綁定 tax_id)
                    c0.checkbox("", key=f"sel_{row['tax_id']}", label_visibility="collapsed")
                    
                    c1.write(row['tax_id'])
                    c2.write(row['name'])
                    c3.write(row['email'] if row['email'] else "-")
                    c4.write(f"{row['shares_held']:,}")
                    
                    with c5:
                        b1, b2 = st.columns(2)
                        if b1.button("✏️", key=f"e_{row['id']}"): show_edit_dialog(row)
                        if b2.button("🗑️", key=f"d_{row['id']}"): 
                             show_delete_dialog(row['tax_id'], row['name'])
                    
                    st.markdown("---")

        elif menu == "📂 批次匯入 (Excel)":
            st.header("批次匯入")
            st.warning("請選擇匯入模式：")
            replace_shares = st.checkbox("⚠️ 覆寫持股數 (勾選=取代舊股數；未勾選=累加)")

            sample = pd.DataFrame({
                "身分證或統編": ["A123456789", "12345678"], "姓名": ["測試A", "測試B公司"],
                "身分別": ["自然人", "法人"], "地址": ["台北", "新竹"], "代表人": ["", "董仔"], 
                "持股數": [1000, 5000], "Email": ["userA@test.com", "compB@test.com"], "密碼提示": ["生日", "統編後四碼"]
            })
            buf = io.BytesIO()
            with pd.ExcelWriter(buf, engine='xlsxwriter') as writer: sample.to_excel(writer, index=False)
            st.download_button("📥 下載範本", buf, "匯入範本.xlsx")
            
            up_file = st.file_uploader("上傳 Excel", type=["xlsx"])
            if up_file and st.button("確認匯入"):
                try:
                    df_up = pd.read_excel(up_file)
                    cnt = 0
                    for i, r in df_up.iterrows():
                        tid = str(r["身分證或統編"]).strip()
                        nm = str(r["姓名"]).strip()
                        tp = "Corporate" if "法人" in str(r["身分別"]) else "Individual"
                        addr = str(r["地址"]) if "地址" in r else ""
                        rep = str(r["代表人"]) if "代表人" in r and pd.notna(r["代表人"]) else None
                        email = str(r["Email"]) if "Email" in r and pd.notna(r["Email"]) else None
                        hint = str(r["密碼提示"]) if "密碼提示" in r and pd.notna(r["密碼提示"]) else None

                        sys.upsert_shareholder(tid, nm, tp, addr, rep, email, hint)
                        if "持股數" in r and pd.notna(r["持股數"]) or "初始持股數" in r:
                            qty_col = "持股數" if "持股數" in r else "初始持股數"
                            try:
                                qty = int(r[qty_col])
                                if qty >= 0:
                                    if replace_shares: sys.set_share_count(tid, qty)
                                    else: sys.issue_shares(tid, qty)
                            except: pass
                        cnt+=1
                    st.success(f"匯入 {cnt} 筆 (覆蓋模式: {'開啟' if replace_shares else '關閉'})")
                    time.sleep(2)
                    st.rerun()
                except Exception as e: st.error(f"錯誤: {e}")

        elif menu == "➕ 新增/編輯股東":
            st.header("手動新增")
            with st.form("add"):
                c1, c2 = st.columns(2)
                tid = c1.text_input("統編/身分證")
                nm = c2.text_input("姓名")
                tp = st.selectbox("類別", ["Individual", "Corporate"])
                rep = st.text_input("代表人")
                addr = st.text_input("地址")
                email = st.text_input("Email")
                hint = st.text_input("密碼提示")
                if st.form_submit_button("儲存"):
                    if tid and nm:
                        sys.upsert_shareholder(tid, nm, tp, addr, rep, email, hint)
                        st.success("成功")
                    else: st.error("缺資料")

        elif menu == "💰 發行/增資":
            st.header("發行")
            df = sys.get_df("shareholders")
            if not df.empty:
                ops = df.apply(lambda x: f"{x['name']} ({x['tax_id']})", axis=1)
                tgt = st.selectbox("對象", ops)
                amt = st.number_input("股數", min_value=1)
                if st.button("發行"):
                    sys.issue_shares(tgt.split("(")[-1].replace(")", ""), amt)
                    st.success("成功")
            else: st.warning("無資料")

        elif menu == "🤝 股權過戶 (交易)":
            st.header("過戶")
            df = sys.get_df("shareholders")
            if len(df)>=2:
                ops = df.apply(lambda x: f"{x['name']} ({x['tax_id']})", axis=1)
                c1, c2 = st.columns(2)
                s = c1.selectbox("賣方", ops)
                b = c2.selectbox("買方", ops)
                amt = st.number_input("股數", min_value=1)
                reason = st.text_input("原因", value="買賣")
                dt = st.date_input("日期", datetime.today())
                if st.button("過戶"):
                    if s==b: st.error("買賣方相同")
                    else:
                        sid = s.split("(")[-1].replace(")", "")
                        bid = b.split("(")[-1].replace(")", "")
                        succ, msg = sys.transfer_shares(dt, sid, bid, amt, reason)
                        if succ: st.success(msg)
                        else: st.error(msg)
            else: st.warning("人數不足")

        elif menu == "📝 交易歷史紀錄":
            st.header("歷史紀錄")
            st.dataframe(sys.get_df("transactions"), use_container_width=True)

    elif menu == "📝 我的持股資訊":
        st.header(f"持股資訊 - {user_name}")
        conn = sys.get_connection()
        df_self = pd.read_sql_query(f"SELECT * FROM shareholders WHERE tax_id = '{user_id}'", conn)
        conn.close()
        if not df_self.empty:
            r = df_self.iloc[0]
            c1, c2, c3 = st.columns(3)
            c1.metric("股數", f"{r['shares_held']:,}")
            c2.metric("Email", r['email'] if r['email'] else "未設定")
            c3.metric("提示詞", r['password_hint'] if r['password_hint'] else "未設定")
            st.info(f"統編/身分證：{r['tax_id']}")
            st.text_input("通訊地址", value=r['address'], disabled=True)
            st.divider()
        else: st.warning("無資料")

if __name__ == "__main__":
    if 'logged_in' not in st.session_state:
        st.session_state.logged_in = False
        st.session_state.user_role = None
        st.session_state.user_name = None
        st.session_state.user_id = None

    if not st.session_state.logged_in:
        c1, c2, c3 = st.columns([1, 2, 1])
        with c2:
            st.markdown("## 🔒 系統登入")
            acc = st.text_input("帳號 (admin 或 統編)")
            pwd = st.text_input("密碼", type="password")
            
            col_login, col_forgot = st.columns([1, 1])
            if col_login.button("登入", type="primary", use_container_width=True):
                if acc == DEFAULT_ADMIN_USER:
                    is_valid, msg, hint = sys.verify_login(acc, pwd, True)
                    if is_valid:
                        st.session_state.logged_in = True
                        st.session_state.user_role = "admin"
                        st.session_state.user_name = msg
                        st.session_state.user_id = acc
                        st.rerun()
                    else:
                        st.error(f"❌ {msg}")
                        if hint: st.info(f"💡 密碼提示：{hint}")
                else:
                    is_valid, msg, hint = sys.verify_login(acc, pwd, False)
                    if is_valid:
                        st.session_state.logged_in = True
                        st.session_state.user_role = "shareholder"
                        st.session_state.user_name = msg
                        st.session_state.user_id = acc
                        st.rerun()
                    else:
                        st.error(f"❌ {msg}")
                        if hint: st.info(f"💡 密碼提示：{hint}")

            if col_forgot.button("❓ 忘記密碼", use_container_width=True):
                show_forgot_password_dialog()
    else:
        run_main_app(st.session_state.user_role, st.session_state.user_name, st.session_state.user_id)