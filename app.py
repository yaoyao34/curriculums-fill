import streamlit as st
import pandas as pd
import gspread
from google.oauth2.service_account import Credentials
import datetime
import json
import base64
import uuid
import math

# --- NEW: Import FPDF and Enums for PDF generation (修正 FPDF 警告) ---
from fpdf import FPDF
from fpdf.enums import XPos, YPos

def safe_note(row):
    """
    最終穩定版 v2：
    - 自動抓所有「備註」欄位
    - 處理 Series
    - 用 replace 清掉 備註1/2
    - 移除 dtype 尾巴
    - ✅ 若 r1 == r2，自動清空 r2（避免雙重顯示）
    """
    note_cols = [c for c in row.index if "備註" in str(c)]
    notes = []

    for col in note_cols:
        val = row[col]
        if isinstance(val, pd.Series):
            if not val.empty:
                val = val.iloc[0]
            else:
                val = ""
        if val is None or str(val).lower() == "nan":
            val = ""

        val = str(val)
        # 強制移除 備註1 / 備註2
        val = val.replace("備註1", "").replace("備註2", "")

        # 強制移除 Name: 0, dtype: object
        if "dtype" in val:
            val = val.split("Name:")[0]

        val = val.replace("\n", " ").strip()
        notes.append(val)

    r1 = notes[0] if len(notes) > 0 else ""
    r2 = notes[1] if len(notes) > 1 else ""

    # ✅ ✅ ✅ 重點修正：如果 r1 == r2，視為只有一則備註
    if r1 and r2 and r1 == r2:
        r2 = ""

    return [r1, r2]

# --- 全域設定 ---
SPREADSHEET_NAME = "教科書填報" 
SHEET_HISTORY = "DB_History"
SHEET_CURRICULUM = "DB_Curriculum"
SHEET_SUBMISSION = "Submission_Records"

# --- 0. 班級資料庫 ---
ALL_SUFFIXES = {
    "普通科": ["機甲", "機乙", "電甲", "電乙", "建築", "室設", "製圖"],
    "建教班": ["機丙", "模丙"],
    "實用技能班": ["機加", "電修", "營造"]
}

DEPT_SPECIFIC_CONFIG = {
    "機械科": { "普通科": ["機甲", "機乙"], "建教班": ["機丙", "模丙"], "實用技能班": ["機加"] },
    "電機科": { "普通科": ["電甲", "電乙"], "建教班": [], "實用技能班": ["電修"] },
    "建築科": { "普通科": ["建築"], "建教班": [], "實用技能班": ["營造"] },
    "室設科": { "普通科": ["室設"], "建教班": [], "實用技能班": [] },
    "製圖科": { "普通科": ["製圖"], "建教班": [], "實用技能班": [] }
}

# --- 1. 連線設定 ---
@st.cache_resource
def get_connection():
    scope = ['https://www.googleapis.com/auth/spreadsheets', 'https://www.googleapis.com/auth/drive']
    if "GCP_CREDENTIALS" in st.secrets:
        try:
            creds_dict = json.loads(st.secrets["GCP_CREDENTIALS"])
            creds = Credentials.from_service_account_info(creds_dict, scopes=scope)
        except json.JSONDecodeError:
            st.error("Secrets 格式錯誤")
            return None
        except ValueError as e:
            try:
                creds_json_str = base64.b64decode(st.secrets["GCP_CREDENTIALS"]).decode('utf-8')
                creds_dict = json.loads(creds_json_str)
                creds = Credentials.from_service_account_info(creds_dict, scopes=scope)
            except Exception as e:
                st.error(f"Secrets 格式錯誤或 Base64 解碼失敗: {e}")
                return None
    else:
        try:
            creds = Credentials.from_service_account_file('credentials.json', scopes=scope)
            pass
        except Exception:
            st.error("找不到金鑰")
            return None
    return gspread.authorize(creds)

# --- 新增功能：從 Google Sheet 取得雲端密碼 ---
# 🔥 修正重點：加入 cache_data(ttl=600)，讓它每 10 分鐘才讀一次 API，解決 Quota Exceeded 問題
@st.cache_data(ttl=600)
def get_cloud_password():
    client = get_connection()
    if not client: return None, None
    
    try:
        sh = client.open(SPREADSHEET_NAME)
        # 嘗試開啟 Dashboard，如果沒有這個分頁會報錯
        ws = sh.worksheet("Dashboard")
        
        # 讀取第二列 (資料列)
        # 假設 A欄=學年度, B欄=密碼
        val_year = ws.cell(2, 1).value  # A2
        val_pwd = ws.cell(2, 2).value   # B2
        
        return str(val_pwd).strip(), str(val_year).strip()
    except Exception as e:
        # 為了避免 cache 住錯誤結果，這裡不回傳，讓它下次重試
        # 但在 Streamlit 中直接報錯顯示
        st.error(f"讀取 Dashboard 密碼失敗: {e}")
        return None, None

# --- 登出功能 ---
def logout():
    st.session_state["logged_in"] = False
    st.session_state["current_school_year"] = None
    # 清除網址上的 token
    st.query_params.clear()
    st.rerun()
    
# --- 登入檢查 (含 Session 保存與防瀏覽器雞婆) ---
def check_login():
    """
    回傳 True 表示已登入，False 表示未登入
    """
    # 🔥 修正重點：若已經登入，直接回傳 True，完全不要去呼叫 get_cloud_password()
    # 這能大幅減少不必要的 API 讀取
    if st.session_state.get("logged_in"):
        with st.sidebar:
            st.divider()
            # === 修改排版：將學年度與登出按鈕並排 ===
            col_info, col_btn = st.columns([2, 1])
            with col_info:
                st.write(f"📅 學年度：{st.session_state.get('current_school_year', '')}")
            with col_btn:
                if st.button("👋 登出", type="secondary", use_container_width=True):
                    logout()
            # ====================================
        return True

    # 只有未登入時，才去快取中讀取密碼
    cloud_pwd, cloud_year = get_cloud_password()
    
    # 2. 檢查網址是否有 token (用於 F5 重整後保持登入)
    # 使用 query_params 取得目前的參數
    params = st.query_params
    url_token = params.get("access_token", None)

    # 如果網址有正確的 token，視為已登入
    if url_token and url_token == cloud_pwd:
        st.session_state["logged_in"] = True
        st.session_state["current_school_year"] = cloud_year
        st.rerun() # 立即重整以刷新介面

    # --- 4. 顯示登入畫面 ---
    st.markdown("## 🔒 系統登入")
    
    # [技巧]：改用 st.form 可以讓輸入體驗更好 (按 Enter 即可送出)
    with st.form("login_form"):
        st.caption("請輸入系統通行碼 (設定於 Dashboard)")
        
        # [關鍵]：將 label 改為 "通行碼" 或 "Access Code"，避開 "密碼/Password" 關鍵字
        # 這樣 Chrome 比較不會跳出「建議高強度密碼」
        input_pwd = st.text_input(
            "通行碼", 
            type="password", 
            key="login_input",
            # 如果您的 Streamlit 版本夠新 (1.34+)，這行可以更強制關閉建議：
            # autocomplete="current-password" 
        )
        
        submitted = st.form_submit_button("登入")
        
        if submitted:
            if cloud_pwd and input_pwd == cloud_pwd:
                st.session_state["logged_in"] = True
                st.session_state["current_school_year"] = cloud_year
                
                # [關鍵]：將密碼寫入網址參數，達成「重整不登出」
                # 注意：這會讓密碼顯示在網址列末端 (?access_token=...)，
                # 但因為這是內部共用密碼，且為了方便性，通常是可以接受的折衷方案。
                st.query_params["access_token"] = input_pwd
                
                st.success("登入成功！")
                st.rerun()
            else:
                st.error("❌ 通行碼錯誤，請重試。")
                
    return False
    
# --- 2. 資料讀取 (v10 最終修正版：精準欄位映射，修復資料不顯示問題) ---
def load_data(dept, semester, grade, use_history=False):
    client = get_connection()
    if not client: return pd.DataFrame()
    try:
        sh = client.open(SPREADSHEET_NAME)
        ws_sub = sh.worksheet(SHEET_SUBMISSION)
        ws_curr = sh.worksheet(SHEET_CURRICULUM) 
        
        # 讀取工作表通用函式 (修正欄位映射邏輯)
        def get_df(ws):
            data = ws.get_all_values()
            if not data: return pd.DataFrame()
            headers = data[0]
            rows = data[1:]
            
            # 定義標準化欄位名稱映射表
            # 左邊是 Google Sheet 可能出現的名稱，右邊是程式內部使用的標準名稱
            mapping = {
                '教科書(1)': '教科書(優先1)',
                '教科書': '教科書(優先1)',
                '字號(1)': '審定字號(1)',
                '字號': '審定字號(1)',
                '審定字號': '審定字號(1)',
                '教科書(2)': '教科書(優先2)',
                '字號(2)': '審定字號(2)',
                '備註': '備註1',
                # 備註1, 備註2, 冊次(1)... 等如果名稱一致就不用特別列
            }
            
            new_headers = []
            seen = {} # 用來處理真正的重複欄位 (例如有兩個 "備註")

            for col in headers:
                c = str(col).strip()
                
                # 1. 先進行標準化映射
                if c in mapping:
                    final_name = mapping[c]
                else:
                    final_name = c
                
                # 2. 處理重複欄位 (自動加上後綴)
                if final_name in seen:
                    seen[final_name] += 1
                    # 如果是重複的備註，嘗試自動給予編號 (例如 備註, 備註 -> 備註1, 備註2)
                    if final_name.startswith('備註'):
                         # 這裡為了對應舊資料結構，簡單處理
                         unique_name = f"備註{seen[final_name]}"
                    else:
                         unique_name = f"{final_name}({seen[final_name]})"
                    
                    # 特殊修正: 如果因為重複處理產生了像是 "教科書(優先1)(2)" 這種怪名，這裡可以微調
                    # 但基本上用 mapping 已經解決了大半
                    
                    new_headers.append(unique_name)
                else:
                    seen[final_name] = 1
                    # 如果是第一個遇到的 "備註"，且沒被 map 改名，我們統一叫 "備註1" 以配合後續邏輯
                    if final_name == '備註':
                        new_headers.append('備註1')
                    else:
                        new_headers.append(final_name)
                        
            return pd.DataFrame(rows, columns=new_headers)

        df_sub = get_df(ws_sub)
        df_curr = get_df(ws_curr) 

        # 統一轉字串
        if not df_sub.empty:
            df_sub['年級'] = df_sub['年級'].astype(str)
            df_sub['學期'] = df_sub['學期'].astype(str)
            df_sub['科別'] = df_sub['科別'].astype(str)
        
        # --- 建立課程類別對照表 (Map) ---
        category_map = {}
        if not df_curr.empty:
            df_curr['年級'] = df_curr['年級'].astype(str)
            df_curr['學期'] = df_curr['學期'].astype(str)
            df_curr['科別'] = df_curr['科別'].astype(str)
            
            target_dept_curr = df_curr[df_curr['科別'] == dept]
            for _, row in target_dept_curr.iterrows():
                k = (row['課程名稱'], str(row['年級']), str(row['學期']))
                category_map[k] = row['課程類別']

        display_rows = []
        displayed_uuids = set()

        # --- 輔助函式 ---
        def parse_classes(class_str):
            if not class_str: return set()
            clean_str = str(class_str).replace('"', '').replace("'", "").replace('，', ',')
            return {c.strip() for c in clean_str.split(',') if c.strip()}

        def check_class_match(default_class_str, submission_class_str):
            def_set = parse_classes(default_class_str)
            sub_set = parse_classes(submission_class_str)
            if not def_set: return True
            if not sub_set: return False
            return not def_set.isdisjoint(sub_set)

        # ==========================================
        # 模式 A: 載入歷史資料 (History Mode)
        # ==========================================
        if use_history:
            ws_hist = sh.worksheet(SHEET_HISTORY)
            df_hist = get_df(ws_hist)
            if not df_hist.empty:
                df_hist['年級'] = df_hist['年級'].astype(str)
                df_hist['學期'] = df_hist['學期'].astype(str)
                df_hist['科別'] = df_hist['科別'].astype(str)
                
                mask_hist = (df_hist['科別'] == dept) & (df_hist['學期'] == str(semester)) & (df_hist['年級'] == str(grade))
                target_hist = df_hist[mask_hist]

                for _, h_row in target_hist.iterrows():
                    h_uuid = str(h_row.get('uuid', '')).strip()
                    if not h_uuid: h_uuid = str(uuid.uuid4())

                    sub_match = pd.DataFrame()
                    if not df_sub.empty:
                        sub_match = df_sub[df_sub['uuid'] == h_uuid]
                    
                    row_data = {}
                    if not sub_match.empty:
                        s_row = sub_match.iloc[0]
                        row_data = s_row.to_dict()
                        row_data['uuid'] = h_uuid
                        row_data['勾選'] = False
                    else:
                        row_data = h_row.to_dict()
                        row_data['uuid'] = h_uuid
                        row_data['勾選'] = False
                        
                        # 補齊歷史資料中可能缺漏的標準欄位
                        if '教科書(1)' in row_data and '教科書(優先1)' not in row_data: row_data['教科書(優先1)'] = row_data['教科書(1)']
                        if '字號(1)' in row_data and '審定字號(1)' not in row_data: row_data['審定字號(1)'] = row_data['字號(1)']
                        if '字號(2)' in row_data and '審定字號(2)' not in row_data: row_data['審定字號(2)'] = row_data['字號(2)']

                    # 補上課程類別
                    c_name = row_data.get('課程名稱', '')
                    map_key = (c_name, str(grade), str(semester))
                    if map_key in category_map:
                        row_data['課程類別'] = category_map[map_key]
                    else:
                        if '課程類別' not in row_data or not row_data['課程類別']:
                             row_data['課程類別'] = "" 

                    display_rows.append(row_data)
                    displayed_uuids.add(h_uuid)

        # ==========================================
        # 模式 B: 不載入歷史 (Curriculum Mode - 預設)
        # ==========================================
        else:
            if not df_curr.empty:
                mask_curr = (df_curr['科別'] == dept) & (df_curr['學期'] == str(semester)) & (df_curr['年級'] == str(grade))
                target_curr = df_curr[mask_curr]

                for _, c_row in target_curr.iterrows():
                    c_name = c_row['課程名稱']
                    c_type = c_row['課程類別']
                    default_class = c_row.get('預設適用班級') or c_row.get('適用班級', '')

                    sub_matches = pd.DataFrame()
                    if not df_sub.empty:
                        mask_sub = (df_sub['科別'] == dept) & (df_sub['學期'] == str(semester)) & (df_sub['年級'] == str(grade)) & (df_sub['課程名稱'] == c_name)
                        sub_matches = df_sub[mask_sub]
                    
                    found_match = False
                    
                    if not sub_matches.empty:
                        for _, s_row in sub_matches.iterrows():
                            s_class_str = str(s_row.get('適用班級', ''))
                            if check_class_match(default_class, s_class_str):
                                s_data = s_row.to_dict()
                                s_data['勾選'] = False
                                s_data['課程類別'] = c_type
                                display_rows.append(s_data)
                                displayed_uuids.add(s_data.get('uuid'))
                                found_match = True
                    
                    if not found_match:
                        new_uuid = str(uuid.uuid4())
                        display_rows.append({
                            "勾選": False,
                            "uuid": new_uuid,
                            "科別": dept, "年級": grade, "學期": semester,
                            "課程類別": c_type, "課程名稱": c_name,
                            "適用班級": default_class,
                            "教科書(優先1)": "", "冊次(1)": "", "出版社(1)": "", "審定字號(1)": "",
                            "教科書(優先2)": "", "冊次(2)": "", "出版社(2)": "", "審定字號(2)": "",
                            "備註1": "", "備註2": ""
                        })

        # ==========================================
        # 共同階段：補上「自訂課程」(Orphans)
        # ==========================================
        if not df_sub.empty:
            mask_orphan = (df_sub['科別'] == dept) & (df_sub['學期'] == str(semester)) & (df_sub['年級'] == str(grade))
            orphan_subs = df_sub[mask_orphan]
            
            for _, s_row in orphan_subs.iterrows():
                s_uuid = s_row.get('uuid')
                if s_uuid and s_uuid not in displayed_uuids:
                    s_data = s_row.to_dict()
                    s_data['勾選'] = False
                    s_data['課程類別'] = "自訂/新增"
                    display_rows.append(s_data)
                    displayed_uuids.add(s_uuid)

        df_final = pd.DataFrame(display_rows)
        if not df_final.empty:
            required_cols = ["勾選", "課程類別", "課程名稱", "適用班級", "教科書(優先1)", "冊次(1)", "出版社(1)", "審定字號(1)", "備註1", "教科書(優先2)", "冊次(2)", "出版社(2)", "審定字號(2)", "備註2"]
            for col in required_cols:
                if col not in df_final.columns:
                    df_final[col] = ""
            
            if '課程類別' in df_final.columns and '課程名稱' in df_final.columns:
                 df_final = df_final.sort_values(by=['課程類別', '課程名稱'], ascending=[False, True]).reset_index(drop=True)

        return df_final

    except Exception as e:
        st.error(f"讀取錯誤 (Detail): {e}")
        import traceback
        traceback.print_exc()
        return pd.DataFrame()


# --- 3. 取得課程列表 ---
def get_course_list():
    if 'data' in st.session_state and not st.session_state['data'].empty:
        return st.session_state['data']['課程名稱'].unique().tolist()
    return []

# --- 4. 存檔 (單筆寫入) ---
def save_single_row(row_data, original_key=None):
    client = get_connection()
    if not client: return False
    
    sh = client.open(SPREADSHEET_NAME)
    try:
        ws_sub = sh.worksheet(SHEET_SUBMISSION)
    except:
        # 若無工作表，建立新表並寫入包含學年度的新標題
        ws_sub = sh.add_worksheet(title=SHEET_SUBMISSION, rows=1000, cols=20)
        ws_sub.append_row(["uuid", "填報時間", "學年度", "科別", "學期", "年級", "課程名稱", "教科書(1)", "冊次(1)", "出版社(1)", "字號(1)", "教科書(2)", "冊次(2)", "出版社(2)", "字號(2)", "適用班級", "備註1", "備註2"])

    all_values = ws_sub.get_all_values()
    if not all_values:
        # 若表是空的，寫入包含學年度的新標題
        headers = ["uuid", "填報時間", "學年度", "科別", "學期", "年級", "課程名稱", "教科書(1)", "冊次(1)", "出版社(1)", "字號(1)", "教科書(2)", "冊次(2)", "出版社(2)", "字號(2)", "適用班級", "備註1", "備註2"]
        ws_sub.append_row(headers)
        all_values = [headers] 
    
    headers = all_values[0]
    
    if "uuid" not in headers:
        ws_sub.clear() 
        headers = ["uuid", "填報時間", "學年度", "科別", "學期", "年級", "課程名稱", "教科書(1)", "冊次(1)", "出版社(1)", "字號(1)", "教科書(2)", "冊次(2)", "出版社(2)", "字號(2)", "適用班級", "備註1", "備註2"]
        ws_sub.append_row(headers)
        all_values = [headers]

    col_map = {h: i for i, h in enumerate(headers)}
    timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    target_uuid = row_data.get('uuid')
    
    # 取得當前 Session 的學年度
    current_school_year = st.session_state.get("current_school_year", "")

    # 準備資料字典，包含「學年度」
    data_dict = {
        "uuid": target_uuid,
        "填報時間": timestamp,
        "學年度": current_school_year,  # 新增欄位
        "科別": row_data['科別'], "學期": row_data['學期'], "年級": row_data['年級'], "課程名稱": row_data['課程名稱'],
        "教科書(1)": row_data['教科書(優先1)'], "冊次(1)": row_data['冊次(1)'], "出版社(1)": row_data['出版社(1)'], "字號(1)": row_data['審定字號(1)'],
        "教科書(2)": row_data['教科書(優先2)'], "冊次(2)": row_data['冊次(2)'], "出版社(2)": row_data['出版社(2)'], "字號(2)": row_data['審定字號(2)'],
        "適用班級": row_data['適用班級'], 
        "備註1": row_data.get('備註1', ''),
        "備註2": row_data.get('備註2', '')
    }
    
    row_to_write = []
    # 根據 Sheet 實際的 Headers 動態填入資料
    # 如果 Sheet 還沒有「學年度」欄位，這裡會自動略過，不會報錯
    for h in headers:
        val = ""
        if h in data_dict: val = data_dict[h]
        elif h == "字號(1)": val = data_dict.get("字號(1)") or data_dict.get('審定字號(1)', '')
        elif h == "字號(2)": val = data_dict.get("字號(2)") or data_dict.get('審定字號(2)', '')
        elif h == "字號" or h == "審定字號": val = data_dict.get("字號(1)", "") 
        elif h == "備註": val = data_dict.get("備註1", "") 
        row_to_write.append(val)

    target_row_index = -1

    if target_uuid:
        uuid_col_idx = col_map.get("uuid")
        if uuid_col_idx is not None:
            for i in range(1, len(all_values)):
                if all_values[i][uuid_col_idx] == target_uuid:
                    target_row_index = i + 1
                    break

    if target_row_index > 0:
        start_col_char = 'A'
        end_col_char = chr(ord('A') + len(headers) - 1) 
        if len(headers) > 26: end_col_char = 'Z' 

        range_name = f"{start_col_char}{target_row_index}:{end_col_char}{target_row_index}"
        ws_sub.update(range_name=range_name, values=[row_to_write])
    else:
        ws_sub.append_row(row_to_write)
        
    return True

# --- 4.5 刪除功能 ---
def delete_row_from_db(target_uuid):
    if not target_uuid: return False
    
    client = get_connection()
    if not client: return False
    sh = client.open(SPREADSHEET_NAME)
    try:
        ws_sub = sh.worksheet(SHEET_SUBMISSION)
    except:
        return False
        
    all_values = ws_sub.get_all_values()
    if not all_values: return False
    headers = all_values[0]
    
    if "uuid" not in headers: return False 
    uuid_idx = headers.index("uuid")
    
    target_row_index = -1
    for i in range(1, len(all_values)):
        if all_values[i][uuid_idx] == target_uuid:
            target_row_index = i + 1
            break
            
    if target_row_index > 0:
        ws_sub.delete_rows(target_row_index)
        return True
    return False

# --- 4.6 同步歷史資料到 Submission (修正版：動態對應欄位) ---
def sync_history_to_db(dept):
    """
    當勾選「載入歷史資料」且按下轉 PDF 時觸發。
    功能：找出 DB_History 中該科別資料，寫入 Submission_Records。
    修正：支援動態欄位對應 (含學年度)。
    """
    client = get_connection()
    if not client: return False

    try:
        sh = client.open(SPREADSHEET_NAME)
        ws_hist = sh.worksheet(SHEET_HISTORY)
        ws_sub = sh.worksheet(SHEET_SUBMISSION)

        # 讀取 History
        data_hist = ws_hist.get_all_records()
        df_hist = pd.DataFrame(data_hist)
        
        # 讀取 Submission (為了比對 UUID)
        data_sub = ws_sub.get_all_records()
        df_sub = pd.DataFrame(data_sub)
        
        # 取得目前 Submission 的標題列，確保寫入順序正確
        sub_headers = ws_sub.row_values(1)
        if not sub_headers:
            # 如果是空的，定義預設標題
            sub_headers = ["uuid", "填報時間", "學年度", "科別", "學期", "年級", "課程名稱", "教科書(1)", "冊次(1)", "出版社(1)", "字號(1)", "教科書(2)", "冊次(2)", "出版社(2)", "字號(2)", "適用班級", "備註1", "備註2"]
            ws_sub.append_row(sub_headers)

        if not df_hist.empty:
            df_hist['年級'] = df_hist['年級'].astype(str)
            df_hist['學期'] = df_hist['學期'].astype(str)
            
            target_hist = df_hist[
                (df_hist['科別'] == dept) & 
                (df_hist['年級'].isin(['1', '2', '3'])) & 
                (df_hist['學期'].isin(['1', '2']))
            ]
        else:
            target_hist = pd.DataFrame()

        if target_hist.empty:
            return True 

        existing_uuids = set()
        if not df_sub.empty:
            existing_uuids = set(df_sub['uuid'].astype(str).tolist())

        rows_to_append = []
        timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        current_school_year = st.session_state.get("current_school_year", "")

        for _, row in target_hist.iterrows():
            h_uuid = str(row.get('uuid', '')).strip()
            
            # --- 穩健取值 (兼容舊欄位名) ---
            def get_val(keys):
                for k in keys:
                    if k in row and str(row[k]).strip():
                        return str(row[k]).strip()
                return ""

            if h_uuid and h_uuid not in existing_uuids:
                # 建立完整的資料字典，包含所有可能的欄位
                row_dict = {
                    "uuid": h_uuid,
                    "填報時間": timestamp,
                    "學年度": current_school_year,  # 帶入目前的學年度
                    "科別": row.get('科別', ''),
                    "學期": str(row.get('學期', '')),
                    "年級": str(row.get('年級', '')),
                    "課程名稱": row.get('課程名稱', ''),
                    "教科書(1)": get_val(['教科書(優先1)', '教科書(1)', '教科書']),
                    "教科書(優先1)": get_val(['教科書(優先1)', '教科書(1)', '教科書']), # 確保名稱對應
                    "冊次(1)": get_val(['冊次(1)', '冊次']),
                    "出版社(1)": get_val(['出版社(1)', '出版社']),
                    "字號(1)": get_val(['審定字號(1)', '字號(1)', '審定字號', '字號']),
                    "審定字號(1)": get_val(['審定字號(1)', '字號(1)', '審定字號', '字號']),
                    "教科書(2)": get_val(['教科書(優先2)', '教科書(2)']),
                    "教科書(優先2)": get_val(['教科書(優先2)', '教科書(2)']),
                    "冊次(2)": get_val(['冊次(2)']),
                    "出版社(2)": get_val(['出版社(2)']),
                    "字號(2)": get_val(['審定字號(2)', '字號(2)']),
                    "審定字號(2)": get_val(['審定字號(2)', '字號(2)']),
                    "適用班級": row.get('適用班級', ''),
                    "備註1": get_val(['備註1', '備註']),
                    "備註2": get_val(['備註2'])
                }

                # 根據 Google Sheet 目前的欄位順序產生 List
                new_row_list = []
                for header in sub_headers:
                    # 處理欄位名稱映射 (例如 Sheet 是 "教科書(1)" 但程式邏輯可能是 "教科書(優先1)")
                    val = row_dict.get(header, "")
                    # 特殊處理簡稱
                    if not val:
                        if header == "教科書(1)": val = row_dict.get("教科書(優先1)", "")
                        elif header == "教科書(2)": val = row_dict.get("教科書(優先2)", "")
                        elif header == "字號(1)": val = row_dict.get("審定字號(1)", "")
                        elif header == "字號(2)": val = row_dict.get("審定字號(2)", "")
                    new_row_list.append(val)
                
                rows_to_append.append(new_row_list)

        if rows_to_append:
            ws_sub.append_rows(rows_to_append)
            print(f"已同步 {len(rows_to_append)} 筆歷史資料")
            return True 
        
        return False 

    except Exception as e:
        st.error(f"同步歷史資料失敗: {e}")
        return False

# --- 5. 產生 PDF 報表 (修正 DeprecationWarning) ---
def create_pdf_report(dept):
    CHINESE_FONT = 'NotoSans' 
    
    # 取得當前學年度，若無則預設
    current_year = st.session_state.get('current_school_year', '114')

    class PDF(FPDF):
        def header(self):
            # 修正: uni=True 已棄用，移除
            self.set_font(CHINESE_FONT, 'B', 18) 
            # 修正: ln=1 -> new_x=XPos.LMARGIN, new_y=YPos.NEXT
            # 使用變數 current_year
            self.cell(0, 10, f'{dept} {current_year}學年度 教科書選用總表', new_x=XPos.LMARGIN, new_y=YPos.NEXT, align='C')
            self.set_font(CHINESE_FONT, '', 10)
            self.cell(0, 5, f"列印時間：{datetime.datetime.now().strftime('%Y-%m-%d %H:%M')}", new_x=XPos.LMARGIN, new_y=YPos.NEXT, align='R')
            self.ln(5)

        def footer(self):
            self.set_y(-15)
            self.set_font(CHINESE_FONT, 'I', 8)
            # 修正: ln=0 -> new_x=XPos.RIGHT, new_y=YPos.TOP
            self.cell(0, 10, f'Page {self.page_no()}/{{nb}}', new_x=XPos.RIGHT, new_y=YPos.TOP, align='C')
            
    client = get_connection()
    if not client: return None
    
    try:
        sh = client.open(SPREADSHEET_NAME)
        ws_sub = sh.worksheet(SHEET_SUBMISSION)
        data = ws_sub.get_all_values()
        if not data: return None
        
        headers = data[0]
        rows = data[1:]
        
        seen = {}
        new_headers = []
        for col in headers:
            c = str(col).strip()
            if c in seen:
                seen[c] += 1
                new_name = f"{c}({seen[c]})"
                if c == '冊次': new_name = f"冊次({seen[c]})"
                elif c == '出版社': new_name = f"出版社({seen[c]})"
                elif c == '字號' or c == '審定字號': new_name = f"審定字號({seen[c]})"
                elif c == '教科書': new_name = f"教科書(優先{seen[c]})"
                elif c.startswith('備註'): new_name = c
                new_headers.append(new_name)
            else:
                seen[c] = 1
                if c == '教科書(1)': new_headers.append('教科書(優先1)')
                elif c == '教科書': new_headers.append('教科書(優先1)')
                elif c == '冊次': new_headers.append('冊次(1)')
                elif c == '出版社': new_headers.append('出版社(1)')
                elif c == '字號' or c == '審定字號': new_headers.append('審定字號(1)')
                elif c.startswith('備註'): new_headers.append(c)
                else: new_headers.append(c)
        
        df_full = pd.DataFrame(rows, columns=new_headers)

        if df_full.empty: return None

        df = df_full[df_full['科別'] == dept].copy()
        if df.empty: return None

        if '年級' in df.columns: df['年級'] = df['年級'].astype(str)
        if '學期' in df.columns: df['學期'] = df['學期'].astype(str)
        df = df.sort_values(by='填報時間')
        df = df.drop_duplicates(subset=['科別', '年級', '學期', '課程名稱', '適用班級'], keep='last')
        
    except Exception:
        return None
        
    pdf = PDF(orientation='L', unit='mm', format='A4') 
    pdf.set_auto_page_break(auto=True, margin=15)
    
    try:
        # 修正: 移除 uni=True
        pdf.add_font(CHINESE_FONT, '', 'NotoSansCJKtc-Regular.ttf') 
        pdf.add_font(CHINESE_FONT, 'B', 'NotoSansCJKtc-Regular.ttf') 
        pdf.add_font(CHINESE_FONT, 'I', 'NotoSansCJKtc-Regular.ttf') 
    except Exception as e:
        st.warning(f"🚨 警告: 無法載入中文字體 ({e})。")
        CHINESE_FONT = 'Helvetica'
        
    pdf.add_page()
    
    # 總和: 30+65+45+12+22+28+55+18 = 275mm
    col_widths = [28, 73, 53, 11, 29, 38, 33, 11 ]
    col_names = ["課程名稱", "適用班級", "教科書", "冊次", "出版社", "審定字號", "備註", "核定"]
    TOTAL_TABLE_WIDTH = sum(col_widths)
    
    def render_table_header(pdf):
        pdf.set_font(CHINESE_FONT, 'B', 12) 
        pdf.set_fill_color(220, 220, 220)
        start_x = pdf.get_x()
        start_y = pdf.get_y()
        for w, name in zip(col_widths, col_names):
            pdf.set_xy(start_x, start_y)
            # 修正: ln=1 -> align='C' inside cell
            pdf.multi_cell(w, 8, name, border=1, align='C', fill=True) 
            start_x += w
        pdf.set_xy(pdf.l_margin, start_y + 8) 
        pdf.set_font(CHINESE_FONT, '', 12) 
        
    pdf.set_font(CHINESE_FONT, '', 12) 
    LINE_HEIGHT = 5.5 
    
    for sem in sorted(df['學期'].unique()):
        sem_df = df[df['學期'] == sem].copy()
        
        pdf.set_font(CHINESE_FONT, 'B', 14)
        pdf.set_fill_color(200, 220, 255)
        # 修正: ln=1
        pdf.cell(TOTAL_TABLE_WIDTH, 10, f"第 {sem} 學期", border=1, new_x=XPos.LMARGIN, new_y=YPos.NEXT, align='L', fill=True)
        
        if not sem_df.empty:
            sem_df = sem_df.sort_values(by=['年級', '課程名稱']) 
            render_table_header(pdf)

            for _, row in sem_df.iterrows():
                b1 = str(row.get('教科書(優先1)') or row.get('教科書(1)', '')).strip()
                v1 = str(row.get('冊次(1)', '')).strip()
                p1 = str(row.get('出版社(1)', '')).strip()
                c1 = str(row.get('審定字號(1)') or row.get('字號(1)', '')).strip()
                r1, r2 = safe_note(row)
                
                b2 = str(row.get('教科書(優先2)') or row.get('教科書(2)', '')).strip()
                v2 = str(row.get('冊次(2)', '')).strip()
                p2 = str(row.get('出版社(2)', '')).strip()
                c2 = str(row.get('審定字號(2)') or row.get('字號(2)', '')).strip()
                
                has_priority_2 = (b2 != "" or v2 != "")
                
                def format_combined_cell(val1, val2):
                    val1 = val1 if val1 else ""
                    val2 = val2 if val2 else ""
                    if not val1 and not val2: return ""
                    elif not val2: return val1
                    elif not val1: return val2
                    else: return f"{val1}\n{val2}"
                
                data_row_to_write = [
                    str(row['課程名稱']),
                    str(row['適用班級']),
                    format_combined_cell(b1, b2), 
                    format_combined_cell(v1, v2), 
                    format_combined_cell(p1, p2), 
                    format_combined_cell(c1, c2), 
                    format_combined_cell(r1, r2)
                ]
                
                # 計算高度
                pdf.set_font(CHINESE_FONT, '', 12) 
                cell_line_counts = [] 
                
                for i, text in enumerate(data_row_to_write):
                    w = col_widths[i] 
                    segments = str(text).split('\n')
                    total_lines_for_cell = 0
                    for seg in segments:
                        safe_width = w - 2
                        if safe_width < 1: safe_width = 1
                        txt_width = pdf.get_string_width(seg)
                        if txt_width > 0:
                            lines_needed = math.ceil(txt_width / safe_width)
                        else:
                            lines_needed = 1 
                            if not seg and len(segments) == 1 and text == "": lines_needed = 0
                        total_lines_for_cell += lines_needed
                    if total_lines_for_cell < 1: total_lines_for_cell = 1
                    cell_line_counts.append(total_lines_for_cell)
                
                max_lines_in_row = max(cell_line_counts)
                min_lines = 2 if has_priority_2 else 1
                if max_lines_in_row < min_lines: max_lines_in_row = min_lines

                calculated_height = max_lines_in_row * LINE_HEIGHT + 4 
                row_height = max(calculated_height, 10.0) 
                
                if pdf.get_y() + row_height > pdf.page_break_trigger:
                    pdf.add_page()
                    pdf.set_font(CHINESE_FONT, 'B', 14)
                    pdf.set_fill_color(200, 220, 255)
                    pdf.cell(TOTAL_TABLE_WIDTH, 10, f"第 {sem} 學期 (續)", border=1, new_x=XPos.LMARGIN, new_y=YPos.NEXT, align='L', fill=True)
                    render_table_header(pdf)
                    
                start_x = pdf.get_x()
                start_y = pdf.get_y()
                
                for i, text in enumerate(data_row_to_write):
                    w = col_widths[i] 
                    # 修正: ln=0 -> new_x=XPos.RIGHT, new_y=YPos.TOP
                    pdf.set_xy(start_x, start_y)
                    pdf.cell(w, row_height, "", border=1, new_x=XPos.RIGHT, new_y=YPos.TOP) 
                    
                    this_cell_content_height = cell_line_counts[i] * LINE_HEIGHT
                    y_pos = start_y + (row_height - this_cell_content_height) / 2
                    
                    pdf.set_xy(start_x, y_pos)
                    pdf.set_font(CHINESE_FONT, '', 12)
                    
                    align = 'C' if i == 3 else 'L' 
                    pdf.multi_cell(w, LINE_HEIGHT, str(text), border=0, align=align)
                        
                    start_x += w 
                
                w_check = col_widths[7]
                pdf.set_xy(start_x, start_y)
                pdf.cell(w_check, row_height, "", border=1, new_x=XPos.RIGHT, new_y=YPos.TOP) 
                
                box_size = 4
                box_x = start_x + (w_check - box_size) / 2 - 2 
                
                y_p1 = start_y + (row_height * 0.25) - (box_size / 2)
                pdf.rect(box_x, y_p1, box_size, box_size)
                pdf.set_xy(box_x + box_size + 1, y_p1)
                pdf.set_font(CHINESE_FONT, '', 8)
                pdf.cell(5, box_size, "1", border=0, new_x=XPos.RIGHT, new_y=YPos.TOP)
                
                if has_priority_2:
                    y_p2 = start_y + (row_height * 0.75) - (box_size / 2)
                    pdf.rect(box_x, y_p2, box_size, box_size)
                    pdf.set_xy(box_x + box_size + 1, y_p2)
                    pdf.cell(5, box_size, "2", border=0, new_x=XPos.RIGHT, new_y=YPos.TOP)

                pdf.set_y(start_y + row_height)
                    
            pdf.ln(5) 
    
    pdf.set_font(CHINESE_FONT, '', 12) 
    pdf.ln(10)
    
    is_vocational = dept in DEPT_SPECIFIC_CONFIG
    footer_text = ["填表人：", "召集人：", "教務主任："]
    if is_vocational:
        footer_text.append("實習主任：")
    footer_text.append("校長：")
    
    cell_width = TOTAL_TABLE_WIDTH / len(footer_text)
    
    for text in footer_text:
        # 修正: ln=0 -> new_x=XPos.RIGHT, new_y=YPos.TOP
        pdf.cell(cell_width, 12, text, border='B', new_x=XPos.RIGHT, new_y=YPos.TOP, align='L')
    pdf.ln()

    # 修正: dest='S' 已棄用，預設回傳 bytearray
    return pdf.output()

# --- 6. 班級計算邏輯 ---
def get_all_possible_classes(grade):
    prefix = {"1": "一", "2": "二", "3": "三"}.get(str(grade), "")
    if not prefix: return []
    classes = []
    for sys_name, suffixes in ALL_SUFFIXES.items():
        if str(grade) == "3" and sys_name == "建教班": continue
        for s in suffixes: classes.append(f"{prefix}{s}")
    return sorted(list(set(classes)))

def get_target_classes_for_dept(dept, grade, sys_name):
    prefix = {"1": "一", "2": "二", "3": "三"}.get(str(grade), "")
    if not prefix: return []
    
    suffixes = []
    if dept in DEPT_SPECIFIC_CONFIG:
        suffixes = DEPT_SPECIFIC_CONFIG[dept].get(sys_name, [])
    else:
        suffixes = ALL_SUFFIXES.get(sys_name, [])
        
    if str(grade) == "3" and sys_name == "建教班": return []
    return [f"{prefix}{s}" for s in suffixes]

# --- 7. Callbacks ---
def update_class_list_from_checkboxes():
    dept = st.session_state.get('dept_val')
    grade = st.session_state.get('grade_val')
    
    current_list = list(st.session_state.get('class_multiselect', []))
    current_set = set(current_list)

    for sys_key, sys_name in [('cb_reg', '普通科'), ('cb_prac', '實用技能班'), ('cb_coop', '建教班')]:
        is_checked = st.session_state[sys_key]
        target_classes = get_target_classes_for_dept(dept, grade, sys_name)
        
        if is_checked:
            current_set.update(target_classes)
        else:
            current_set.difference_update(target_classes)
    
    final_list = sorted(list(current_set))
    st.session_state['active_classes'] = final_list
    st.session_state['class_multiselect'] = final_list 

    if st.session_state['cb_reg'] and st.session_state['cb_prac'] and st.session_state['cb_coop']:
        st.session_state['cb_all'] = True
    else:
        st.session_state['cb_all'] = False

def toggle_all_checkboxes():
    new_state = st.session_state['cb_all']
    st.session_state['cb_reg'] = new_state
    st.session_state['cb_prac'] = new_state
    st.session_state['cb_coop'] = new_state
    update_class_list_from_checkboxes()

def on_multiselect_change():
    st.session_state['active_classes'] = st.session_state['class_multiselect']

def on_editor_change():
    key = f"main_editor_{st.session_state['editor_key_counter']}"
    if key not in st.session_state: return

    edits = st.session_state[key]["edited_rows"]
    
    target_idx = None
    for idx, changes in edits.items():
        if "勾選" in changes and changes["勾選"] is True:
            target_idx = int(idx)
            break
            
    if target_idx is not None:
        st.session_state['data']["勾選"] = False
        st.session_state['data'].at[target_idx, "勾選"] = True
        st.session_state['edit_index'] = target_idx
        
        row_data = st.session_state['data'].iloc[target_idx]
        
        st.session_state['original_key'] = {
            '科別': row_data['科別'],
            '年級': str(row_data['年級']),
            '學期': str(row_data['學期']),
            '課程名稱': row_data['課程名稱'],
            '適用班級': str(row_data.get('適用班級', ''))
        }
        st.session_state['current_uuid'] = row_data.get('uuid')
        
        st.session_state['form_data'] = {
            'course': row_data["課程名稱"],
            'book1': row_data.get("教科書(優先1)", ""), 'vol1': row_data.get("冊次(1)", ""), 'pub1': row_data.get("出版社(1)", ""), 'code1': row_data.get("審定字號(1)", ""),
            'book2': row_data.get("教科書(優先2)", ""), 'vol2': row_data.get("冊次(2)", ""), 'pub2': row_data.get("出版社(2)", ""), 'code2': row_data.get("審定字號(2)", ""),
            'note1': row_data.get("備註1", ""), 
            'note2': row_data.get("備註2", "")
        }
        
        class_str = str(row_data.get("適用班級", ""))
        class_list = [c.strip() for c in class_str.replace("，", ",").split(",") if c.strip()]
        
        grade = st.session_state.get('grade_val')
        dept = st.session_state.get('dept_val')
        valid_classes = get_all_possible_classes(grade) if grade else []
        final_list = [c for c in class_list if c in valid_classes]
        
        st.session_state['active_classes'] = final_list
        st.session_state['class_multiselect'] = final_list

        st.session_state['cb_reg'] = False
        st.session_state['cb_prac'] = False
        st.session_state['cb_coop'] = False
        
        reg_targets = get_target_classes_for_dept(dept, grade, "普通科")
        prac_targets = get_target_classes_for_dept(dept, grade, "實用技能班")
        coop_targets = get_target_classes_for_dept(dept, grade, "建教班")
        
        if reg_targets and any(c in final_list for c in reg_targets): st.session_state['cb_reg'] = True
        if prac_targets and any(c in final_list for c in prac_targets): st.session_state['cb_prac'] = True
        if coop_targets and any(c in final_list for c in coop_targets): st.session_state['cb_coop'] = True
        
        st.session_state['cb_all'] = (st.session_state['cb_reg'] and st.session_state['cb_prac'] and st.session_state['cb_coop'])
    
    else:
        current_idx = st.session_state.get('edit_index')
        if current_idx is not None and str(current_idx) in edits:
            if edits[str(current_idx)].get("勾選") is False:
                st.session_state['data'].at[current_idx, "勾選"] = False
                st.session_state['edit_index'] = None
                st.session_state['original_key'] = None
                st.session_state['current_uuid'] = None

def auto_load_data():
    dept = st.session_state.get('dept_val')
    sem = st.session_state.get('sem_val')
    grade = st.session_state.get('grade_val')
    use_history = st.session_state.get('use_history', False)
    
    if dept and sem and grade:
        df = load_data(dept, sem, grade, use_history)
        st.session_state['data'] = df
        st.session_state['loaded'] = True
        st.session_state['edit_index'] = None
        st.session_state['original_key'] = None
        st.session_state['current_uuid'] = None
        st.session_state['active_classes'] = []
        
        st.session_state['form_data'] = {
            'course': '', 'book1': '', 'vol1': '全', 'pub1': '', 'code1': '',
            'book2': '', 'vol2': '全', 'pub2': '', 'code2': '', 'note1': '', 'note2': ''
        }
        
        if dept not in DEPT_SPECIFIC_CONFIG:
            st.session_state['cb_reg'] = True
            st.session_state['cb_prac'] = True
            st.session_state['cb_coop'] = True
            st.session_state['cb_all'] = True
        else:
            st.session_state['cb_reg'] = True
            st.session_state['cb_prac'] = False
            st.session_state['cb_coop'] = False
            st.session_state['cb_all'] = False
            
        update_class_list_from_checkboxes()
        st.session_state['editor_key_counter'] += 1

# --- 8. 主程式 ---
def main():
    st.set_page_config(page_title="教科書填報系統", layout="wide")
    # === 🛡️ 安全檢查區塊開始 ===
    # 呼叫檢查
    if not check_login():
        st.stop() # 未登入則停止執行下方內容
    
    st.markdown("""
        <style>
        html, body, [class*="css"] { font-family: 'Segoe UI', sans-serif; }
        div[data-testid="stDataEditor"] { background-color: #ffffff !important; }
        div[data-testid="column"] button { margin-top: 1.5rem; }
        </style>
    """, unsafe_allow_html=True)

    if 'edit_index' not in st.session_state: st.session_state['edit_index'] = None
    if 'current_uuid' not in st.session_state: st.session_state['current_uuid'] = None
    if 'active_classes' not in st.session_state: st.session_state['active_classes'] = []
    if 'form_data' not in st.session_state:
        st.session_state['form_data'] = {
            'course': '', 'book1': '', 'vol1': '全', 'pub1': '', 'code1': '',
            'book2': '', 'vol2': '全', 'pub2': '', 'code2': '', 'note1': '', 'note2': ''
        }
    if 'cb_all' not in st.session_state: st.session_state['cb_all'] = False
    if 'cb_reg' not in st.session_state: st.session_state['cb_reg'] = False
    if 'cb_prac' not in st.session_state: st.session_state['cb_prac'] = False
    if 'cb_coop' not in st.session_state: st.session_state['cb_coop'] = False
    if 'last_selected_row' not in st.session_state: st.session_state['last_selected_row'] = None
    if 'editor_key_counter' not in st.session_state: st.session_state['editor_key_counter'] = 0
    if 'use_history' not in st.session_state: st.session_state['use_history'] = False

    with st.sidebar:
        st.header("1. 填報設定")
        dept_options = [
            "建築科", "機械科", "電機科", "製圖科", "室設科", 
            "國文科", "英文科", "數學科", "自然科", "社會科", 
            "資訊科技", "體育科", "國防科", "藝術科", "健護科", "輔導科", "閩南語"
        ]
        
        dept = st.selectbox("科別", dept_options, key='dept_val', on_change=auto_load_data)
        col1, col2 = st.columns(2)
        with col1: sem = st.selectbox("學期", ["1", "2", "寒", "暑"], key='sem_val', on_change=auto_load_data)
        with col2: grade = st.selectbox("年級", ["1", "2", "3"], key='grade_val', on_change=auto_load_data)
        
        st.checkbox("載入歷史資料 (113學年)", key='use_history', on_change=auto_load_data)
        st.caption("勾選後將載入去年資料。若未勾選，則載入預設課程表。")

    top_col1, top_col2 = st.columns([4, 1])
    
    with top_col1:
        st.title("📚 教科書填報系統")
        
    with top_col2:
        if st.button("📄 轉 PDF 報表 (下載)", type="primary", use_container_width=True):
            if dept:
                with st.spinner(f"正在處理 {dept} PDF..."):
                    if st.session_state.get('use_history'):
                        st.info("正在同步歷史資料到填報紀錄...")
                        sync_success = sync_history_to_db(dept)
                        if sync_success:
                            st.success("✅ 歷史資料已同步寫入！")
                    
                    pdf_report_bytes = create_pdf_report(dept)
                    
                    if pdf_report_bytes is not None:
                        b64_bytes = base64.b64encode(pdf_report_bytes)
                        b64 = b64_bytes.decode('latin-1') 
                        href = f'<a href="data:application/pdf;base64,{b64}" download="{dept}_教科書總表.pdf" style="text-decoration:none; color:white; background-color:#b31412; padding:8px 12px; border-radius:5px; font-weight:bold; font-size:14px; display:block; text-align:center;">⬇️ 點此下載 PDF</a>'
                        st.markdown(href, unsafe_allow_html=True)
                    else:
                        st.error("生成失敗，請檢查資料。")
            else:
                st.warning("請先選擇科別")

    if 'loaded' not in st.session_state and dept and sem and grade:
        auto_load_data()

    if st.session_state.get('loaded'):
        with st.sidebar:
            st.divider()
            is_edit_mode = st.session_state['edit_index'] is not None
            header_text = f"2. 修改第 {st.session_state['edit_index'] + 1} 列" if is_edit_mode else "2. 新增/插入課程"
            st.subheader(header_text)
            
            if is_edit_mode:
                c_cancel, c_del = st.columns([1, 1])
                with c_cancel:
                    if st.button("❌ 取消", type="secondary"):
                        st.session_state['edit_index'] = None
                        st.session_state['current_uuid'] = None
                        st.session_state['data']["勾選"] = False
                        st.session_state['editor_key_counter'] += 1
                        st.rerun()
                with c_del:
                    if st.button("🗑️ 刪除此列", type="primary"):
                        idx = st.session_state['edit_index']
                        uuid_to_del = st.session_state.get('current_uuid')
                        
                        with st.spinner("同步資料庫..."):
                            if uuid_to_del:
                                delete_row_from_db(uuid_to_del)
                        
                            st.session_state['data'] = st.session_state['data'].drop(idx).reset_index(drop=True)
                            st.session_state['edit_index'] = None
                            st.session_state['current_uuid'] = None
                            st.session_state['active_classes'] = []
                            st.session_state['form_data'] = {k: '' for k in st.session_state['form_data']}
                            st.session_state['form_data']['vol1'] = '全'
                            st.session_state['form_data']['vol2'] = '全'
                            st.session_state['editor_key_counter'] += 1
                            
                            st.success("已刪除！")
                            st.rerun()

            current_form = st.session_state['form_data']

            course_list = get_course_list()
            course_index = 0
            if is_edit_mode and current_form['course'] in course_list:
                course_index = course_list.index(current_form['course'])
            
            if course_list:
                input_course = st.selectbox("選擇課程", course_list, index=course_index)
            else:
                input_course = st.text_input("課程名稱", value=current_form['course'])
            
            st.markdown("##### 適用班級")
            st.caption("👇 勾選學制 (勾'全部'選全校)")
            
            c_all, c1, c2, c3 = st.columns([1, 1, 1, 1])
            with c_all: st.checkbox("全部", key="cb_all", on_change=toggle_all_checkboxes)
            with c1: st.checkbox("普通", key="cb_reg", on_change=update_class_list_from_checkboxes)
            with c2: st.checkbox("實技", key="cb_prac", on_change=update_class_list_from_checkboxes)
            with c3: st.checkbox("建教", key="cb_coop", on_change=update_class_list_from_checkboxes)
            
            st.caption("👇 點選加入其他班級")
            all_possible = get_all_possible_classes(grade)
            final_options = sorted(list(set(all_possible + st.session_state['active_classes'])))
            selected_classes = st.multiselect(
                "最終班級列表:",
                options=final_options,
                default=st.session_state['active_classes'],
                key="class_multiselect",
                on_change=on_multiselect_change
            )
            input_class_str = ",".join(selected_classes)

            st.markdown("**第一優先**")
            input_book1 = st.text_input("書名", value=current_form['book1'])
            bc1, bc2 = st.columns([1, 2])
            vol_opts = ["全", "上", "下", "I", "II", "III", "IV", "V", "VI"]
            vol1_idx = vol_opts.index(current_form['vol1']) if current_form['vol1'] in vol_opts else 0
            with bc1: input_vol1 = st.selectbox("冊次", vol_opts, index=vol1_idx)
            with bc2: input_pub1 = st.text_input("出版社", value=current_form['pub1'])
            
            c_code1, c_note1 = st.columns(2)
            with c_code1: input_code1 = st.text_input("審定字號", value=current_form['code1']) 
            with c_note1: input_note1 = st.text_input("備註1(作者/單價)", value=current_form['note1']) 

            st.markdown("**第二優先**")
            input_book2 = st.text_input("備選書名", value=current_form['book2'])
            bc3, bc4 = st.columns([1, 2])
            vol2_idx = vol_opts.index(current_form['vol2']) if current_form['vol2'] in vol_opts else 0
            with bc3: input_vol2 = st.selectbox("冊次(2)", vol_opts, index=vol2_idx)
            with bc4: input_pub2 = st.text_input("出版社(2)", value=current_form['pub2'])

            c_code2, c_note2 = st.columns(2)
            with c_code2: input_code2 = st.text_input("審定字號(2)", value=current_form['code2']) 
            with c_note2: input_note2 = st.text_input("備註2(作者/單價)", value=current_form['note2'])

            if is_edit_mode:
                if st.button("🔄 更新表格 (存檔)", type="primary", use_container_width=True):
                    if not input_class_str or not input_book1 or not input_pub1 or not input_vol1:
                        st.error("⚠️ 適用班級、第一優先書名、冊次、出版社為必填！")
                    else:
                        idx = st.session_state['edit_index']
                        current_uuid = st.session_state.get('current_uuid')
                        if not current_uuid: current_uuid = str(uuid.uuid4())
                            
                        new_row = {
                            "uuid": current_uuid,
                            "科別": dept, "年級": grade, "學期": sem,
                            "課程類別": "部定必修", 
                            "課程名稱": input_course,
                            "教科書(優先1)": input_book1, "冊次(1)": input_vol1, "出版社(1)": input_pub1, "審定字號(1)": input_code1,
                            "教科書(優先2)": input_book2, "冊次(2)": input_vol2, "出版社(2)": input_pub2, "審定字號(2)": input_code2,
                            "適用班級": input_class_str,
                            "備註1": input_note1, "備註2": input_note2 
                        }
                        with st.spinner("正在寫入資料庫..."):
                            save_single_row(new_row, st.session_state.get('original_key'))

                        for k, v in new_row.items():
                            if k in st.session_state['data'].columns:
                                st.session_state['data'].at[idx, k] = v
                        st.session_state['data'].at[idx, "勾選"] = False
                        st.session_state['form_data'] = {k: '' for k in st.session_state['form_data']}
                        st.session_state['form_data']['vol1'] = '全'
                        st.session_state['form_data']['vol2'] = '全'
                        st.session_state['active_classes'] = []
                        st.session_state['edit_index'] = None
                        st.session_state['original_key'] = None
                        st.session_state['current_uuid'] = None
                        st.session_state['editor_key_counter'] += 1 
                        st.success("✅ 更新並存檔成功！")
                        st.rerun()
            else:
                if st.button("➕ 加入表格 (存檔)", type="primary", use_container_width=True):
                    if not input_class_str or not input_book1 or not input_pub1 or not input_vol1:
                        st.error("⚠️ 適用班級、第一優先書名、冊次、出版社為必填！")
                    else:
                        new_uuid = str(uuid.uuid4())
                        new_row = {
                            "勾選": False, "uuid": new_uuid,
                            "科別": dept, "年級": grade, "學期": sem,
                            "課程類別": "部定必修", "課程名稱": input_course,
                            "教科書(優先1)": input_book1, "冊次(1)": input_vol1, "出版社(1)": input_pub1, "審定字號(1)": input_code1,
                            "教科書(優先2)": input_book2, "冊次(2)": input_vol2, "出版社(2)": input_pub2, "審定字號(2)": input_code2,
                            "適用班級": input_class_str, "備註1": input_note1, "備註2": input_note2 
                        }
                        with st.spinner("正在寫入資料庫..."):
                            save_single_row(new_row, None)
                        st.session_state['data'] = pd.concat([st.session_state['data'], pd.DataFrame([new_row])], ignore_index=True)
                        st.session_state['editor_key_counter'] += 1
                        st.session_state['form_data'] = {k: '' for k in st.session_state['form_data']}
                        st.session_state['form_data']['vol1'] = '全'
                        st.session_state['form_data']['vol2'] = '全'
                        st.session_state['active_classes'] = []
                        st.success(f"✅ 已存檔：{input_course}")
                        st.rerun()

        st.success(f"目前編輯：**{dept}** / **{grade}年級** / **第{sem}學期**")
        
        # 修正: use_container_width -> width='stretch'
        edited_df = st.data_editor(
            st.session_state['data'],
            num_rows="dynamic",
            use_container_width=True, 
            height=600,
            key=f"main_editor_{st.session_state['editor_key_counter']}",
            on_change=on_editor_change,
            column_config={
                "勾選": st.column_config.CheckboxColumn("勾選", width="small", disabled=False),
                "uuid": None, "科別": None, "年級": None, "學期": None,
                "課程類別": st.column_config.TextColumn("類別", width="small", disabled=True),
                "課程名稱": st.column_config.TextColumn("課程名稱", width="medium", disabled=True),
                "適用班級": st.column_config.TextColumn("適用班級", width="medium", disabled=True), 
                "教科書(優先1)": st.column_config.TextColumn("教科書(1)", width="medium", disabled=True), 
                "冊次(1)": st.column_config.TextColumn("冊次(1)", width="small", disabled=True), 
                "出版社(1)": st.column_config.TextColumn("出版社(1)", width="small", disabled=True),
                "審定字號(1)": st.column_config.TextColumn("字號(1)", width="small", disabled=True),
                "備註1": st.column_config.TextColumn("備註(1)", width="small", disabled=True), 
                "教科書(優先2)": st.column_config.TextColumn("教科書(2)", width="medium", disabled=True),
                "冊次(2)": st.column_config.TextColumn("冊次(2)", width="small", disabled=True), 
                "出版社(2)": st.column_config.TextColumn("出版社(2)", width="small", disabled=True),
                "審定字號(2)": st.column_config.TextColumn("字號(2)", width="small", disabled=True),
                "備註2": st.column_config.TextColumn("備註(2)", width="small", disabled=True), 
            },
            column_order=[
                "勾選", "課程類別", "課程名稱", "適用班級",
                "教科書(優先1)", "冊次(1)", "審定字號(1)", "出版社(1)", "備註1", 
                "教科書(優先2)", "冊次(2)", "審定字號(2)", "出版社(2)", "備註2" 
            ]
        )

    else:
        st.info("👈 請先在左側選擇科別")

if __name__ == "__main__":
    main()
