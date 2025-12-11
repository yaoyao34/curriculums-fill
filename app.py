import streamlit as st
import pandas as pd
import gspread
from google.oauth2.service_account import Credentials
import datetime
import json
import base64
import uuid
import math

# --- NEW: Import FPDF and Enums for PDF generation ---
from fpdf import FPDF
from fpdf.enums import XPos, YPos

# --- 0. 班級資料庫與設定 ---
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

SPREADSHEET_NAME = "教科書填報" 
SHEET_HISTORY = "DB_History"
SHEET_CURRICULUM = "DB_Curriculum"
SHEET_SUBMISSION = "Submission_Records"

# --- 輔助函式 ---
def safe_note(row):
    note_cols = [c for c in row.index if "備註" in str(c)]
    notes = []
    for col in note_cols:
        val = row[col]
        if isinstance(val, pd.Series):
            val = val.iloc[0] if not val.empty else ""
        if val is None or str(val).lower() == "nan":
            val = ""
        val = str(val).replace("備註1", "").replace("備註2", "")
        if "dtype" in val: val = val.split("Name:")[0]
        val = val.replace("\n", " ").strip()
        notes.append(val)
    r1 = notes[0] if len(notes) > 0 else ""
    r2 = notes[1] if len(notes) > 1 else ""
    if r1 and r2 and r1 == r2: r2 = ""
    return [r1, r2]

def parse_classes(class_str):
    if not class_str: return set()
    clean_str = str(class_str).replace('"', '').replace("'", "").replace('，', ',')
    return {c.strip() for c in clean_str.split(',') if c.strip()}

def check_class_match(def_s, sub_s):
    d_set, s_set = parse_classes(def_s), parse_classes(sub_s)
    if not d_set: return True
    if not s_set: return False
    return not d_set.isdisjoint(s_set)

def get_target_classes_for_dept(dept, grade, sys_name):
    prefix = {"1": "一", "2": "二", "3": "三"}.get(str(grade), "")
    suffixes = DEPT_SPECIFIC_CONFIG[dept].get(sys_name, []) if dept in DEPT_SPECIFIC_CONFIG else ALL_SUFFIXES.get(sys_name, [])
    return [f"{prefix}{s}" for s in suffixes] if not (str(grade)=="3" and sys_name=="建教班") else []

def get_all_possible_classes(grade):
    prefix = {"1": "一", "2": "二", "3": "三"}.get(str(grade), "")
    if not prefix: return []
    classes = []
    for sys_name, suffixes in ALL_SUFFIXES.items():
        if str(grade) == "3" and sys_name == "建教班": continue
        for s in suffixes: classes.append(f"{prefix}{s}")
    return sorted(list(set(classes)))

# --- 1. 連線設定 ---
@st.cache_resource
def get_connection():
    scope = ['https://www.googleapis.com/auth/spreadsheets', 'https://www.googleapis.com/auth/drive']
    if "GCP_CREDENTIALS" in st.secrets:
        try:
            creds_dict = json.loads(st.secrets["GCP_CREDENTIALS"])
            creds = Credentials.from_service_account_info(creds_dict, scopes=scope)
        except Exception: return None
    else:
        try:
            creds = Credentials.from_service_account_file('credentials.json', scopes=scope)
        except Exception: return None
    return gspread.authorize(creds)

# --- 讀取雲端密碼 ---
@st.cache_data(ttl=600)
def get_cloud_password():
    client = get_connection()
    if not client: return None, None
    try:
        sh = client.open(SPREADSHEET_NAME)
        ws = sh.worksheet("Dashboard")
        val_year = ws.cell(2, 1).value
        val_pwd = ws.cell(2, 2).value
        return str(val_pwd).strip(), str(val_year).strip()
    except Exception: return None, None

# --- 取得可用的歷史學年度 ---
@st.cache_data(ttl=300)
def get_history_years(current_year):
    client = get_connection()
    if not client: return []
    try:
        sh = client.open(SPREADSHEET_NAME)
        ws_hist = sh.worksheet(SHEET_HISTORY)
        data = ws_hist.get_all_values()
        if not data or len(data) < 2: return []
        headers = data[0]
        if "學年度" not in headers: return []
        year_idx = headers.index("學年度")
        unique_years = set()
        for row in data[1:]:
            if len(row) > year_idx:
                y = str(row[year_idx]).strip()
                if y and y != str(current_year): unique_years.add(y)
        return sorted(list(unique_years), reverse=True)
    except Exception: return []

# --- 登出 ---
def logout():
    st.session_state["logged_in"] = False
    st.session_state["current_school_year"] = None
    st.query_params.clear()
    st.rerun()
    
# --- 登入檢查 ---
def check_login():
    if st.session_state.get("logged_in"):
        with st.sidebar:
            st.divider()
            col_info, col_btn = st.columns([2, 1])
            with col_info:
                st.markdown(f"##### 📅 學年度：{st.session_state.get('current_school_year', '')}")
            with col_btn:
                if st.button("👋 登出", type="secondary", width="stretch"):
                    logout()
        return True

    cloud_pwd, cloud_year = get_cloud_password()
    params = st.query_params
    url_token = params.get("access_token", None)

    if url_token and url_token == cloud_pwd:
        st.session_state["logged_in"] = True
        st.session_state["current_school_year"] = cloud_year
        st.rerun()

    st.markdown("## 🔒 系統登入")
    with st.form("login_form"):
        st.caption("請輸入系統通行碼 (設定於 Dashboard)")
        input_pwd = st.text_input("通行碼", type="password", key="login_input")
        if st.form_submit_button("登入"):
            if cloud_pwd and input_pwd == cloud_pwd:
                st.session_state["logged_in"] = True
                st.session_state["current_school_year"] = cloud_year
                st.query_params["access_token"] = input_pwd
                st.success("登入成功！")
                st.rerun()
            else:
                st.error("❌ 通行碼錯誤。")
    return False
    
# --- 2. 資料讀取 ---
def load_data(dept, semester, grade, history_year=None):
    client = get_connection()
    if not client: return pd.DataFrame()
    try:
        sh = client.open(SPREADSHEET_NAME)
        ws_sub = sh.worksheet(SHEET_SUBMISSION)
        ws_curr = sh.worksheet(SHEET_CURRICULUM) 
        
        def get_df(ws):
            data = ws.get_all_values()
            if not data: return pd.DataFrame()
            headers = data[0]
            rows = data[1:]
            mapping = {
                '教科書(1)': '教科書(優先1)', '教科書': '教科書(優先1)',
                '字號(1)': '審定字號(1)', '字號': '審定字號(1)', '審定字號': '審定字號(1)',
                '教科書(2)': '教科書(優先2)', '字號(2)': '審定字號(2)', '備註': '備註1'
            }
            new_headers = []
            seen = {}
            for col in headers:
                c = str(col).strip()
                final_name = mapping.get(c, c)
                if final_name in seen:
                    seen[final_name] += 1
                    if final_name.startswith('備註'): unique_name = f"備註{seen[final_name]}"
                    else: unique_name = f"{final_name}({seen[final_name]})"
                    new_headers.append(unique_name)
                else:
                    seen[final_name] = 1
                    if final_name == '備註': new_headers.append('備註1')
                    else: new_headers.append(final_name)
            return pd.DataFrame(rows, columns=new_headers)

        df_sub = get_df(ws_sub)
        df_curr = get_df(ws_curr) 

        if not df_sub.empty:
            for col in ['年級', '學期', '科別']: df_sub[col] = df_sub[col].astype(str).str.strip()
            # 🔥 確保 UUID 清淨
            if 'uuid' in df_sub.columns: df_sub['uuid'] = df_sub['uuid'].astype(str).str.strip()
        
        category_map = {}
        curr_course_options = []

        if not df_curr.empty:
            for col in ['年級', '學期', '科別']: df_curr[col] = df_curr[col].astype(str).str.strip()
            target_dept_curr = df_curr[df_curr['科別'] == dept]
            
            for _, row in target_dept_curr.iterrows():
                k = (row['課程名稱'], str(row['年級']), str(row['學期']))
                category_map[k] = row['課程類別']
            
            mask_opts = (df_curr['科別'] == str(dept)) & (df_curr['學期'] == str(semester)) & (df_curr['年級'] == str(grade))
            curr_course_options = df_curr[mask_opts]['課程名稱'].unique().tolist()
        
        st.session_state['curr_course_options'] = curr_course_options

        display_rows = []
        displayed_uuids = set()

        # === 模式 A: 載入歷史資料 ===
        if history_year:
            ws_hist = sh.worksheet(SHEET_HISTORY)
            df_hist = get_df(ws_hist)
            if not df_hist.empty:
                for col in ['年級', '學期', '科別', '學年度', 'uuid']: 
                    if col in df_hist.columns: 
                        df_hist[col] = df_hist[col].astype(str).str.strip()
                
                if '科別' not in df_hist.columns:
                    st.error("歷史資料庫缺少'科別'欄位，無法載入。")
                    return pd.DataFrame()

                mask_hist = (df_hist['科別'] == str(dept)) & \
                            (df_hist['學期'] == str(semester)) & \
                            (df_hist['年級'] == str(grade))
                
                if '學年度' in df_hist.columns:
                    mask_hist = mask_hist & (df_hist['學年度'] == str(history_year))
                
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
                        row_data['勾選'] = False
                    else:
                        row_data = h_row.to_dict()
                        row_data['uuid'] = h_uuid
                        row_data['勾選'] = False
                        for k, alt in {'教科書(優先1)': '教科書(1)', '審定字號(1)': '字號(1)', '審定字號(2)': '字號(2)'}.items():
                            if alt in row_data and k not in row_data: row_data[k] = row_data[alt]

                    c_name = row_data.get('課程名稱', '')
                    map_key = (c_name, str(grade), str(semester))
                    row_data['課程類別'] = category_map.get(map_key, "") if not row_data.get('課程類別') else row_data['課程類別']

                    display_rows.append(row_data)
                    displayed_uuids.add(h_uuid)

        # === 模式 B: 預設課程表 ===
        else:
            if not df_curr.empty:
                mask_curr = (df_curr['科別'] == dept) & (df_curr['學期'] == str(semester)) & (df_curr['年級'] == str(grade))
                target_curr = df_curr[mask_curr]

                for _, c_row in target_curr.iterrows():
                    c_name = c_row['課程名稱']
                    c_type = c_row['課程類別']
                    default_class = c_row.get('預設適用班級') or c_row.get('適用班級', '')

                    sub_matches = pd.DataFrame()
                    found_match = False
                    if not df_sub.empty:
                        mask_sub = (df_sub['科別'] == dept) & (df_sub['學期'] == str(semester)) & (df_sub['年級'] == str(grade)) & (df_sub['課程名稱'] == c_name)
                        sub_matches = df_sub[mask_sub]
                    
                    if not sub_matches.empty:
                        for _, s_row in sub_matches.iterrows():
                            if check_class_match(default_class, str(s_row.get('適用班級', ''))):
                                s_uuid = str(s_row.get('uuid')).strip()
                                if s_uuid and s_uuid not in displayed_uuids:
                                    s_data = s_row.to_dict()
                                    s_data['勾選'] = False
                                    s_data['課程類別'] = c_type
                                    display_rows.append(s_data)
                                    displayed_uuids.add(s_uuid)
                                found_match = True
                    
                    if not found_match:
                        new_uuid = str(uuid.uuid4())
                        display_rows.append({
                            "勾選": False, "uuid": new_uuid,
                            "科別": dept, "年級": grade, "學期": semester,
                            "課程類別": c_type, "課程名稱": c_name, "適用班級": default_class,
                            "教科書(優先1)": "", "冊次(1)": "", "出版社(1)": "", "審定字號(1)": "",
                            "教科書(優先2)": "", "冊次(2)": "", "出版社(2)": "", "審定字號(2)": "",
                            "備註1": "", "備註2": ""
                        })

        if not df_sub.empty:
            mask_orphan = (df_sub['科別'] == dept) & (df_sub['學期'] == str(semester)) & (df_sub['年級'] == str(grade))
            orphan_subs = df_sub[mask_orphan]
            for _, s_row in orphan_subs.iterrows():
                s_uuid = str(s_row.get('uuid')).strip()
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
                if col not in df_final.columns: df_final[col] = ""
            if '課程類別' in df_final.columns and '課程名稱' in df_final.columns:
                 df_final = df_final.sort_values(by=['課程類別', '課程名稱'], ascending=[False, True]).reset_index(drop=True)
        return df_final

    except Exception as e: 
        st.error(f"讀取錯誤 (Detail): {e}")
        return pd.DataFrame()

# --- 新增功能：讀取整科的所有 Submission 資料 (供預覽用) ---
def load_preview_data(dept):
    client = get_connection()
    if not client: return pd.DataFrame()
    
    mapping = {
        '教科書(1)': '教科書(優先1)', '教科書': '教科書(優先1)',
        '字號(1)': '審定字號(1)', '字號': '審定字號(1)', '審定字號': '審定字號(1)',
        '教科書(2)': '教科書(優先2)', '字號(2)': '審定字號(2)', '備註': '備註1'
    }

    try:
        sh = client.open(SPREADSHEET_NAME)
        ws_sub = sh.worksheet(SHEET_SUBMISSION)
        data = ws_sub.get_all_values()
    except:
        return pd.DataFrame() 

    df_sub = pd.DataFrame()
    if data:
        headers = data[0]
        rows = data[1:]
        new_headers = []
        seen = {}
        for col in headers:
            c = str(col).strip()
            final_name = mapping.get(c, c)
            if final_name in seen:
                seen[final_name] += 1
                if final_name.startswith('備註'): unique_name = f"備註{seen[final_name]}"
                else: unique_name = f"{final_name}({seen[final_name]})"
                new_headers.append(unique_name)
            else:
                seen[final_name] = 1
                if final_name == '備註': new_headers.append('備註1')
                else: new_headers.append(final_name)
        
        df_sub = pd.DataFrame(rows, columns=new_headers)
        if '科別' in df_sub.columns:
            df_sub = df_sub[df_sub['科別'] == dept].copy()
    
    use_hist = st.session_state.get('use_history_checkbox', False)
    hist_year = st.session_state.get('history_year_val')
    
    if use_hist and not hist_year:
        curr = st.session_state.get('current_school_year', '')
        years = get_history_years(curr)
        if years: hist_year = years[0]
    
    df_final = df_sub
    
    if use_hist and hist_year:
        try:
            ws_hist = sh.worksheet(SHEET_HISTORY)
            data_hist = ws_hist.get_all_values()
            if data_hist:
                h_headers = data_hist[0]
                h_rows = data_hist[1:]
                
                df_hist = pd.DataFrame(h_rows, columns=h_headers)
                df_hist.rename(columns=mapping, inplace=True)
                
                if '科別' in df_hist.columns and '學年度' in df_hist.columns:
                     df_hist['科別'] = df_hist['科別'].astype(str).str.strip()
                     df_hist['學年度'] = df_hist['學年度'].astype(str).str.strip()
                     
                     target_hist = df_hist[
                        (df_hist['科別'] == str(dept).strip()) & 
                        (df_hist['學年度'] == str(hist_year).strip())
                     ].copy()
                     
                     if not target_hist.empty:
                         existing_uuids = set(df_sub['uuid'].astype(str).str.strip()) if not df_sub.empty and 'uuid' in df_sub.columns else set()
                         if 'uuid' in target_hist.columns:
                            target_hist['uuid'] = target_hist['uuid'].astype(str).str.strip()
                            target_hist = target_hist[~target_hist['uuid'].isin(existing_uuids)]
                         
                         df_final = pd.concat([df_sub, target_hist], ignore_index=True)
        except Exception:
            pass 

    if df_final.empty: return pd.DataFrame()

    if '勾選' not in df_final.columns:
        df_final.insert(0, "勾選", False)
        
    if '年級' in df_final.columns and '學期' in df_final.columns and '課程名稱' in df_final.columns:
         df_final = df_final.sort_values(by=['年級', '學期', '課程名稱'], ascending=[True, True, True]).reset_index(drop=True)
         
    return df_final

def get_course_list():
    courses = set()
    if 'data' in st.session_state and not st.session_state['data'].empty:
        if '課程名稱' in st.session_state['data'].columns:
            courses.update(st.session_state['data']['課程名稱'].unique().tolist())
    if 'curr_course_options' in st.session_state:
        courses.update(st.session_state['curr_course_options'])
    return sorted(list(courses))

# --- 4. 存檔 ---
def save_single_row(row_data, original_key=None):
    client = get_connection()
    if not client: return False
    
    sh = client.open(SPREADSHEET_NAME)
    try: ws_sub = sh.worksheet(SHEET_SUBMISSION)
    except:
        ws_sub = sh.add_worksheet(title=SHEET_SUBMISSION, rows=1000, cols=20)
        ws_sub.append_row(["uuid", "填報時間", "學年度", "科別", "學期", "年級", "課程名稱", "教科書(1)", "冊次(1)", "出版社(1)", "字號(1)", "教科書(2)", "冊次(2)", "出版社(2)", "字號(2)", "適用班級", "備註1", "備註2"])

    all_values = ws_sub.get_all_values()
    if not all_values:
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
    current_school_year = st.session_state.get("current_school_year", "")

    data_dict = {
        "uuid": target_uuid, "填報時間": timestamp, "學年度": current_school_year,
        "科別": row_data['科別'], "學期": row_data['學期'], "年級": row_data['年級'], "課程名稱": row_data['課程名稱'],
        "教科書(1)": row_data['教科書(優先1)'], "冊次(1)": row_data['冊次(1)'], "出版社(1)": row_data['出版社(1)'], "字號(1)": row_data['審定字號(1)'],
        "教科書(2)": row_data['教科書(優先2)'], "冊次(2)": row_data['冊次(2)'], "出版社(2)": row_data['出版社(2)'], "字號(2)": row_data['審定字號(2)'],
        "適用班級": row_data['適用班級'], "備註1": row_data.get('備註1', ''), "備註2": row_data.get('備註2', '')
    }
    
    row_to_write = []
    for h in headers:
        val = ""
        if h in data_dict: val = data_dict[h]
        elif h in ["字號(1)", "字號", "審定字號"]: val = data_dict.get("字號(1)", "")
        elif h == "字號(2)": val = data_dict.get("字號(2)", "")
        elif h == "備註": val = data_dict.get("備註1", "")
        row_to_write.append(val)

    target_row_index = -1
    if target_uuid and "uuid" in col_map:
        uuid_idx = col_map["uuid"]
        for i in range(1, len(all_values)):
            if all_values[i][uuid_idx] == target_uuid:
                target_row_index = i + 1
                break

    if target_row_index > 0:
        start, end = 'A', chr(ord('A') + len(headers) - 1)
        if len(headers) > 26: end = 'Z'
        ws_sub.update(range_name=f"{start}{target_row_index}:{end}{target_row_index}", values=[row_to_write])
    else:
        ws_sub.append_row(row_to_write)
    return True

def delete_row_from_db(target_uuid):
    if not target_uuid: return False
    client = get_connection()
    if not client: return False
    try: ws_sub = client.open(SPREADSHEET_NAME).worksheet(SHEET_SUBMISSION)
    except: return False
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

# --- 4.6 同步歷史資料到 Submission ---
def sync_history_to_db(dept, history_year):
    client = get_connection()
    if not client: return False
    try:
        sh = client.open(SPREADSHEET_NAME)
        ws_hist = sh.worksheet(SHEET_HISTORY)
        ws_sub = sh.worksheet(SHEET_SUBMISSION)
        
        timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        current_school_year = st.session_state.get("current_school_year", "")
        if not history_year: return True

        data_sub = ws_sub.get_all_records()
        df_sub = pd.DataFrame(data_sub)
        existing_uuids = set(df_sub['uuid'].astype(str).str.strip().tolist()) if not df_sub.empty else set()

        sub_headers = ws_sub.row_values(1)
        if not sub_headers:
            sub_headers = ["uuid", "填報時間", "學年度", "科別", "學期", "年級", "課程名稱", "教科書(1)", "冊次(1)", "出版社(1)", "字號(1)", "教科書(2)", "冊次(2)", "出版社(2)", "字號(2)", "適用班級", "備註1", "備註2"]
            ws_sub.append_row(sub_headers)

        data_hist = ws_hist.get_all_records()
        df_hist = pd.DataFrame(data_hist)
        if df_hist.empty: return True

        df_hist['學年度'] = df_hist['學年度'].astype(str)
        if '科別' not in df_hist.columns:
            st.error("History 缺少'科別'欄位")
            return False

        target_rows = df_hist[
            (df_hist['學年度'].str.strip() == str(history_year).strip()) & 
            (df_hist['科別'].str.strip() == dept.strip())
        ]

        if len(target_rows) == 0: return True

        rows_to_append = []
        for _, row in target_rows.iterrows():
            h_uuid = str(row.get('uuid', '')).strip()
            if h_uuid in existing_uuids: continue 

            def get_val(keys):
                for k in keys:
                    if k in row and str(row[k]).strip(): return str(row[k]).strip()
                return ""

            row_dict = {
                "uuid": h_uuid, "填報時間": timestamp, "學年度": current_school_year,
                "科別": row.get('科別', dept),
                "學期": str(row.get('學期', '')), "年級": str(row.get('年級', '')), "課程名稱": row.get('課程名稱', ''),
                "教科書(1)": get_val(['教科書(優先1)', '教科書(1)', '教科書']), "冊次(1)": get_val(['冊次(1)', '冊次']), "出版社(1)": get_val(['出版社(1)', '出版社']), "字號(1)": get_val(['審定字號(1)', '字號(1)']),
                "教科書(2)": get_val(['教科書(優先2)', '教科書(2)']), "冊次(2)": get_val(['冊次(2)']), "出版社(2)": get_val(['出版社(2)']), "字號(2)": get_val(['審定字號(2)', '字號(2)']),
                "適用班級": row.get('適用班級', ''), "備註1": get_val(['備註1', '備註']), "備註2": get_val(['備註2'])
            }
            new_row_list = []
            for header in sub_headers:
                val = row_dict.get(header, "")
                if not val:
                    if header == "教科書(1)": val = row_dict.get("教科書(1)")
                    elif header == "字號(1)": val = row_dict.get("字號(1)")
                new_row_list.append(val)
            rows_to_append.append(new_row_list)

        if rows_to_append: ws_sub.append_rows(rows_to_append)
        return True 
    except Exception as e:
        st.error(f"同步失敗: {e}")
        return False

# --- 5. PDF 報表 ---
def create_pdf_report(dept):
    CHINESE_FONT = 'NotoSans' 
    current_year = st.session_state.get('current_school_year', '114')

    class PDF(FPDF):
        def header(self):
            self.set_auto_page_break(False)
            self.set_font(CHINESE_FONT, 'B', 18) 
            self.cell(0, 10, f'{dept} {current_year}學年度 教科書選用總表', new_x=XPos.LMARGIN, new_y=YPos.NEXT, align='C')
            self.set_font(CHINESE_FONT, '', 10)
            self.cell(0, 5, f"列印時間：{datetime.datetime.now().strftime('%Y-%m-%d %H:%M')}", new_x=XPos.LMARGIN, new_y=YPos.NEXT, align='R')
            self.ln(5)
            self.set_auto_page_break(True, margin=15)

        def footer(self):
            self.set_y(-15)
            self.set_font(CHINESE_FONT, 'I', 8)
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
                if c.startswith('教科書'): new_name = f"教科書(優先{seen[c]})"
                elif c.startswith('備註'): new_name = c
                new_headers.append(new_name)
            else:
                seen[c] = 1
                if c == '教科書(1)': new_headers.append('教科書(優先1)')
                elif c == '教科書': new_headers.append('教科書(優先1)')
                elif c.startswith('備註'): new_headers.append(c)
                else: new_headers.append(c)
        
        df_full = pd.DataFrame(rows, columns=new_headers)
        if df_full.empty: return None
        df = df_full[df_full['科別'] == dept].copy()
        if df.empty: return None
        if '學期' in df.columns: df['學期'] = df['學期'].astype(str)
        df = df.sort_values(by='填報時間').drop_duplicates(subset=['科別', '年級', '學期', '課程名稱', '適用班級'], keep='last')
    except Exception: return None
        
    pdf = PDF(orientation='L', unit='mm', format='A4') 
    pdf.set_auto_page_break(auto=True, margin=15)
    try:
        pdf.add_font(CHINESE_FONT, '', 'NotoSansCJKtc-Regular.ttf') 
        pdf.add_font(CHINESE_FONT, 'B', 'NotoSansCJKtc-Regular.ttf') 
        pdf.add_font(CHINESE_FONT, 'I', 'NotoSansCJKtc-Regular.ttf') 
    except Exception: CHINESE_FONT = 'Helvetica'
        
    pdf.add_page()
    col_widths = [28, 73, 53, 11, 29, 38, 33, 11 ]
    col_names = ["課程名稱", "適用班級", "教科書", "冊次", "出版社", "審定字號", "備註", "核定"]
    
    if dept == "室設科":
        col_widths[1] = 19   # 班級
        col_widths[2] = 107  # 教科書

    LINE_HEIGHT = 5.5 
    
    def render_table_header(pdf):
        auto_pb = pdf.auto_page_break
        pdf.set_auto_page_break(False)
        pdf.set_font(CHINESE_FONT, 'B', 12) 
        pdf.set_fill_color(220, 220, 220)
        start_x = pdf.get_x()
        start_y = pdf.get_y()
        for w, name in zip(col_widths, col_names):
            pdf.set_xy(start_x, start_y)
            pdf.multi_cell(w, 8, name, border=1, align='C', fill=True) 
            start_x += w
        pdf.set_xy(pdf.l_margin, start_y + 8) 
        pdf.set_font(CHINESE_FONT, '', 12) 
        if auto_pb: pdf.set_auto_page_break(True, margin=15)

    for sem in sorted(df['學期'].unique()):
        sem_df = df[df['學期'] == sem].copy()
        pdf.set_font(CHINESE_FONT, 'B', 14)
        pdf.set_fill_color(200, 220, 255)
        pdf.cell(sum(col_widths), 10, f"第 {sem} 學期", border=1, new_x=XPos.LMARGIN, new_y=YPos.NEXT, align='L', fill=True)
        if not sem_df.empty:
            sem_df = sem_df.sort_values(by=['年級', '課程名稱']) 
            render_table_header(pdf)
            for _, row in sem_df.iterrows():
                b1 = str(row.get('教科書(優先1)') or row.get('教科書(1)', '')).strip()
                v1, p1 = str(row.get('冊次(1)', '')).strip(), str(row.get('出版社(1)', '')).strip()
                c1 = str(row.get('審定字號(1)') or row.get('字號(1)', '')).strip()
                r1, r2 = safe_note(row)
                b2 = str(row.get('教科書(優先2)') or row.get('教科書(2)', '')).strip()
                v2, p2 = str(row.get('冊次(2)', '')).strip(), str(row.get('出版社(2)', '')).strip()
                c2 = str(row.get('審定字號(2)') or row.get('字號(2)', '')).strip()
                has_priority_2 = (b2 != "" or v2 != "")
                def clean(s): return s.replace('\r', '').replace('\n', ' ')
                p1_data = [str(row['課程名稱']), str(row['適用班級']), clean(b1), clean(v1), clean(p1), clean(c1), clean(r1), ""]
                p2_data = ["", "", clean(b2), clean(v2), clean(p2), clean(c2), clean(r2), ""]

                pdf.set_font(CHINESE_FONT, '', 12) 
                lines_p1 = []
                for i, text in enumerate(p1_data):
                    w = col_widths[i]
                    txt_w = pdf.get_string_width(text)
                    lines = math.ceil(txt_w / (w-2)) if txt_w > 0 else 1
                    if text == "": lines = 0
                    if i in [0, 1]: lines = 0
                    lines_p1.append(lines)
                
                lines_p2 = []
                for i, text in enumerate(p2_data):
                    w = col_widths[i]
                    txt_w = pdf.get_string_width(text)
                    lines = math.ceil(txt_w / (w-2)) if txt_w > 0 else 1
                    if text == "": lines = 0
                    lines_p2.append(lines)
                
                lines_common = []
                for i in [0, 1]:
                    w = col_widths[i]
                    text = p1_data[i]
                    txt_w = pdf.get_string_width(text)
                    lines = math.ceil(txt_w / (w-2)) if txt_w > 0 else 1
                    lines_common.append(lines)

                max_h_p1 = max(lines_p1) * LINE_HEIGHT + 2
                max_h_p2 = max(lines_p2) * LINE_HEIGHT + 2 if has_priority_2 else 0
                max_h_common = max(lines_common) * LINE_HEIGHT + 4
                if max_h_p1 < 6: max_h_p1 = 6
                if has_priority_2 and max_h_p2 < 6: max_h_p2 = 6
                row_h = max(max_h_common, max_h_p1 + max_h_p2)
                
                if pdf.get_y() + row_h > pdf.page_break_trigger:
                    pdf.add_page()
                    pdf.set_font(CHINESE_FONT, 'B', 14)
                    pdf.set_fill_color(200, 220, 255)
                    pdf.cell(sum(col_widths), 10, f"第 {sem} 學期 (續)", border=1, new_x=XPos.LMARGIN, new_y=YPos.NEXT, align='L', fill=True)
                    render_table_header(pdf)
                    
                start_x, start_y = pdf.get_x(), pdf.get_y()
                for i in range(8):
                    w = col_widths[i]
                    pdf.set_xy(start_x, start_y)
                    pdf.cell(w, row_h, "", border=1)
                    
                    if i in [0, 1]:
                        y_pos = start_y + (row_h - lines_common[i]*LINE_HEIGHT)/2
                        pdf.set_xy(start_x, y_pos)
                        pdf.multi_cell(w, LINE_HEIGHT, p1_data[i], border=0, align='C' if i==1 else 'L')
                    elif i == 7:
                        w_chk = w
                        box_sz = 4
                        box_x = start_x + (w_chk - box_sz)/2 - 2
                        y_box1 = start_y + (max_h_p1 - box_sz)/2
                        pdf.rect(box_x, y_box1, box_sz, box_sz)
                        pdf.set_xy(box_x + box_sz + 1, y_box1)
                        pdf.set_font(CHINESE_FONT, '', 8)
                        pdf.cell(5, box_sz, "1", border=0)
                        if has_priority_2:
                            y_box2 = start_y + max_h_p1 + (max_h_p2 - box_sz)/2
                            pdf.rect(box_x, y_box2, box_sz, box_sz)
                            pdf.set_xy(box_x + box_sz + 1, y_box2)
                            pdf.cell(5, box_sz, "2", border=0)
                        pdf.set_font(CHINESE_FONT, '', 12)
                    else:
                        y_pos1 = start_y + (max_h_p1 - lines_p1[i]*LINE_HEIGHT)/2
                        pdf.set_xy(start_x, y_pos1)
                        pdf.multi_cell(w, LINE_HEIGHT, p1_data[i], border=0, align='C' if i==3 else 'L')
                        if has_priority_2:
                            y_pos2 = start_y + max_h_p1 + (max_h_p2 - lines_p2[i]*LINE_HEIGHT)/2
                            pdf.set_xy(start_x, y_pos2)
                            pdf.multi_cell(w, LINE_HEIGHT, p2_data[i], border=0, align='C' if i==3 else 'L')
                    start_x += w
                pdf.set_y(start_y + row_h)
            pdf.ln(5) 
    
    pdf.set_font(CHINESE_FONT, '', 12) 
    pdf.ln(10)
    is_vocational = dept in DEPT_SPECIFIC_CONFIG
    footer_text = ["填表人：", "召集人：", "教務主任："]
    if is_vocational: footer_text.append("實習主任：")
    footer_text.append("校長：")
    cell_w = sum(col_widths) / len(footer_text)
    for text in footer_text:
        pdf.cell(cell_w, 12, text, border='B', new_x=XPos.RIGHT, new_y=YPos.TOP, align='L')
    pdf.ln()
    return pdf.output()

# --- 7. Callbacks ---
def auto_load_data():
    dept = st.session_state.get('dept_val')
    sem = st.session_state.get('sem_val')
    grade = st.session_state.get('grade_val')
    
    if st.session_state.get('edit_index') is not None:
        if st.session_state.get('last_dept') != dept:
            st.session_state['edit_index'] = None
        elif st.session_state.get('last_grade') != grade:
            orig = st.session_state.get('original_key')
            if orig and str(orig.get('年級')) == str(grade):
                restored_classes = st.session_state.get('original_classes', [])
                st.session_state['active_classes'] = restored_classes
                st.session_state['class_multiselect'] = restored_classes
            else:
                st.session_state['active_classes'] = []
                st.session_state['class_multiselect'] = []
                st.session_state['cb_reg'] = False
                st.session_state['cb_prac'] = False
                st.session_state['cb_coop'] = False
                st.session_state['cb_all'] = False
            st.session_state['last_grade'] = grade
            update_class_list_from_checkboxes()
            return 
        else: return

    st.session_state['last_dept'] = dept
    st.session_state['last_grade'] = grade

    use_hist = st.session_state.get('use_history_checkbox', False)
    hist_year = None
    if use_hist:
        val_in_state = st.session_state.get('history_year_val')
        if val_in_state: hist_year = val_in_state
        else:
            curr = st.session_state.get('current_school_year', '')
            available_years = get_history_years(curr)
            if available_years: hist_year = available_years[0] 

    if dept and sem and grade:
        st.session_state['active_classes'] = []
        st.session_state['class_multiselect'] = []
        is_spec = dept in DEPT_SPECIFIC_CONFIG
        st.session_state['cb_reg'] = True
        st.session_state['cb_prac'] = not is_spec
        st.session_state['cb_coop'] = not is_spec
        st.session_state['cb_all'] = not is_spec
        update_class_list_from_checkboxes()

        df = load_data(dept, sem, grade, hist_year)
        st.session_state['data'] = df
        st.session_state['loaded'] = True
        st.session_state['edit_index'] = None
        st.session_state['original_key'] = None
        st.session_state['current_uuid'] = None
        
        st.session_state['form_data'] = {k: '' for k in ['course','book1','pub1','code1','book2','pub2','code2','note1','note2']}
        st.session_state['form_data'].update({'vol1':'全', 'vol2':'全'})
        st.session_state['editor_key_counter'] += 1

def update_class_list_from_checkboxes():
    dept, grade = st.session_state.get('dept_val'), st.session_state.get('grade_val')
    cur_set = set(st.session_state.get('class_multiselect', []))
    def get_classes(sys_name):
        prefix = {"1": "一", "2": "二", "3": "三"}.get(str(grade), "")
        suffixes = DEPT_SPECIFIC_CONFIG[dept].get(sys_name, []) if dept in DEPT_SPECIFIC_CONFIG else ALL_SUFFIXES.get(sys_name, [])
        return [f"{prefix}{s}" for s in suffixes] if not (str(grade)=="3" and sys_name=="建教班") else []

    for k, name in [('cb_reg','普通科'), ('cb_prac','實用技能班'), ('cb_coop','建教班')]:
        if st.session_state[k]: cur_set.update(get_classes(name))
        else: cur_set.difference_update(get_classes(name))
    
    final = sorted(list(cur_set))
    st.session_state['active_classes'] = final
    st.session_state['class_multiselect'] = final 
    st.session_state['cb_all'] = all([st.session_state['cb_reg'], st.session_state['cb_prac'], st.session_state['cb_coop']])

def toggle_all_checkboxes():
    v = st.session_state['cb_all']
    for k in ['cb_reg', 'cb_prac', 'cb_coop']: st.session_state[k] = v
    update_class_list_from_checkboxes()

def on_multiselect_change():
    st.session_state['active_classes'] = st.session_state['class_multiselect']

def on_editor_change():
    key = f"main_editor_{st.session_state['editor_key_counter']}"
    if key not in st.session_state: return
    edits = st.session_state[key]["edited_rows"]
    
    new_checked_idx = next((int(i) for i, c in edits.items() if c.get("勾選") is True), None)
    
    current_idx = st.session_state.get('edit_index')
    unchecked_current = False
    if current_idx is not None:
        if str(current_idx) in edits and edits[str(current_idx)].get("勾選") is False:
            unchecked_current = True

    # 狀況 A: 使用者取消了目前的勾選 -> 退出編輯模式
    if unchecked_current:
        st.session_state['data'].at[current_idx, "勾選"] = False
        st.session_state['edit_index'] = None
        st.session_state['current_uuid'] = None
        st.session_state['original_key'] = None
        st.session_state['form_data'] = {k: '' for k in ['course','book1','pub1','code1','book2','pub2','code2','note1','note2']}
        st.session_state['form_data'].update({'vol1':'全', 'vol2':'全'})
        st.session_state['active_classes'] = []
        st.session_state['class_multiselect'] = []
        # 🔥 強制刷新介面，防止狀態殘留
        st.session_state['editor_key_counter'] += 1
        return

    # 狀況 B: 使用者勾選了新的一列
    if new_checked_idx is not None:
        if current_idx is not None and current_idx != new_checked_idx:
            st.session_state['data'].at[current_idx, "勾選"] = False
            
        st.session_state['data'].at[new_checked_idx, "勾選"] = True
        st.session_state['edit_index'] = new_checked_idx
        
        row = st.session_state['data'].iloc[new_checked_idx]
        st.session_state['original_key'] = {
            '科別': row['科別'], '年級': str(row['年級']), '學期': str(row['學期']), 
            '課程名稱': row['課程名稱'], '適用班級': str(row.get('適用班級', ''))
        }
        st.session_state['current_uuid'] = str(row.get('uuid')).strip()
        
        st.session_state['form_data'] = {
            'course': row["課程名稱"],
            'book1': row.get("教科書(優先1)", ""), 'vol1': row.get("冊次(1)", ""), 'pub1': row.get("出版社(1)", ""), 'code1': row.get("審定字號(1)", ""),
            'book2': row.get("教科書(優先2)", ""), 'vol2': row.get("冊次(2)", ""), 'pub2': row.get("出版社(2)", ""), 'code2': row.get("審定字號(2)", ""),
            'note1': row.get("備註1", ""), 'note2': row.get("備註2", "")
        }
        cls_list = [c.strip() for c in str(row.get("適用班級", "")).replace("，", ",").split(",") if c.strip()]
        st.session_state['original_classes'] = cls_list 
        st.session_state['active_classes'] = cls_list
        st.session_state['class_multiselect'] = cls_list
        
        dept, grade = st.session_state.get('dept_val'), st.session_state.get('grade_val')
        cls_set = set(cls_list)
        for k, sys in [('cb_reg','普通科'), ('cb_prac','實用技能班'), ('cb_coop','建教班')]:
            tgts = get_target_classes_for_dept(dept, grade, sys)
            st.session_state[k] = bool(tgts and set(tgts).intersection(cls_set))
        st.session_state['cb_all'] = all([st.session_state['cb_reg'], st.session_state['cb_prac'], st.session_state['cb_coop']])

def on_preview_change():
    key = "preview_editor"
    if key not in st.session_state: return
    edits = st.session_state[key]["edited_rows"]
    target_idx = next((int(i) for i, c in edits.items() if c.get("勾選")), None)
    
    if target_idx is not None:
        if st.session_state.get('edit_index') is not None:
            if 'data' in st.session_state and not st.session_state['data'].empty:
                 st.session_state['data'].at[st.session_state['edit_index'], "勾選"] = False
            st.session_state['edit_index'] = None
            st.session_state['current_uuid'] = None

        df_preview = st.session_state['preview_df']
        row = df_preview.iloc[target_idx]
        target_grade = str(row['年級'])
        target_sem = str(row['學期'])
        target_uuid = str(row.get('uuid', '')).strip() 
        
        st.session_state['grade_val'] = target_grade
        st.session_state['sem_val'] = target_sem
        st.session_state['last_grade'] = target_grade 
        
        auto_load_data()
        
        current_df = st.session_state['data']
        matching_indices = []
        if target_uuid:
            matching_indices = current_df.index[current_df['uuid'] == target_uuid].tolist()
        
        if not matching_indices:
            target_course = row['課程名稱']
            matching_indices = current_df.index[current_df['課程名稱'] == target_course].tolist()
        
        if matching_indices:
            new_idx = matching_indices[0]
            st.session_state['data'].at[new_idx, "勾選"] = True
            st.session_state['edit_index'] = new_idx
            
            row_data = current_df.iloc[new_idx]
            st.session_state['original_key'] = {
                '科別': row_data['科別'], '年級': str(row_data['年級']), '學期': str(row_data['學期']), 
                '課程名稱': row_data['課程名稱'], '適用班級': str(row_data.get('適用班級', ''))
            }
            st.session_state['current_uuid'] = str(row_data.get('uuid')).strip()
            st.session_state['form_data'] = {
                'course': row_data["課程名稱"],
                'book1': row_data.get("教科書(優先1)", ""), 'vol1': row_data.get("冊次(1)", ""), 'pub1': row_data.get("出版社(1)", ""), 'code1': row_data.get("審定字號(1)", ""),
                'book2': row_data.get("教科書(優先2)", ""), 'vol2': row_data.get("冊次(2)", ""), 'pub2': row_data.get("出版社(2)", ""), 'code2': row_data.get("審定字號(2)", ""),
                'note1': row_data.get("備註1", ""), 'note2': row_data.get("備註2", "")
            }
            
            cls_list = [c.strip() for c in str(row_data.get("適用班級", "")).replace("，", ",").split(",") if c.strip()]
            
            # 🔥 關鍵修正：這裡手動設定班級與 Checkbox 狀態，絕不能呼叫 update_class_list_from_checkboxes
            st.session_state['original_classes'] = cls_list
            st.session_state['active_classes'] = cls_list
            st.session_state['class_multiselect'] = cls_list
            
            dept, grade = st.session_state.get('dept_val'), st.session_state.get('grade_val')
            cls_set = set(cls_list)
            
            # 手動反推 Checkbox 狀態
            for k, sys in [('cb_reg','普通科'), ('cb_prac','實用技能班'), ('cb_coop','建教班')]:
                tgts = get_target_classes_for_dept(dept, grade, sys)
                st.session_state[k] = bool(tgts and set(tgts).intersection(cls_set))
            st.session_state['cb_all'] = all([st.session_state['cb_reg'], st.session_state['cb_prac'], st.session_state['cb_coop']])
            
            st.session_state['show_preview'] = False
            st.session_state['editor_key_counter'] += 1

# --- 8. 主程式 ---
def main():
    st.set_page_config(page_title="教科書填報系統", layout="wide")
    if not check_login(): st.stop()
    
    st.markdown("""<style>div[data-testid="stDataEditor"] {background-color: #ffffff !important;} div[data-testid="column"] button {margin-top: 1.5rem;}</style>""", unsafe_allow_html=True)

    for k in ['edit_index', 'current_uuid', 'last_selected_row']: 
        if k not in st.session_state: st.session_state[k] = None
    if 'active_classes' not in st.session_state: st.session_state['active_classes'] = []
    if 'form_data' not in st.session_state: st.session_state['form_data'] = {k: '' for k in ['course','book1','pub1','code1','book2','pub2','code2','note1','note2']}
    if 'editor_key_counter' not in st.session_state: st.session_state['editor_key_counter'] = 0
    if 'use_history_checkbox' not in st.session_state: st.session_state['use_history_checkbox'] = False
    if 'show_preview' not in st.session_state: st.session_state['show_preview'] = False
    if 'last_dept' not in st.session_state: st.session_state['last_dept'] = None
    if 'last_grade' not in st.session_state: st.session_state['last_grade'] = None

    with st.sidebar:
        st.header("1. 填報設定")
        depts = ["建築科", "機械科", "電機科", "製圖科", "室設科", "國文科", "英文科", "數學科", "自然科", "社會科", "資訊科技", "體育科", "國防科", "藝術科", "健護科", "輔導科", "閩南語"]
        dept = st.selectbox("科別", depts, key='dept_val', on_change=auto_load_data)
        c1, c2 = st.columns(2)
        sem = c1.selectbox("學期", ["1", "2", "寒", "暑"], key='sem_val', on_change=auto_load_data)
        grade = c2.selectbox("年級", ["1", "2", "3"], key='grade_val', on_change=auto_load_data)
        
        use_hist = st.checkbox("載入歷史資料", key='use_history_checkbox', on_change=auto_load_data)
        if use_hist:
            years = get_history_years(st.session_state.get('current_school_year', ''))
            if years: st.selectbox("選擇歷史學年度", years, key='history_year_val', on_change=auto_load_data)
            else: st.warning("⚠️ 無可用的歷史學年度")

    col1, col2 = st.columns([4, 1])
    with col1: st.title("📚 教科書填報系統")
    with col2:
        c_prev, c_pdf = st.columns(2)
        with c_prev:
            if st.button("👁️ 預覽 PDF 資料", width="stretch"):
                st.session_state['show_preview'] = not st.session_state['show_preview']
                # 🔥 切換預覽時，強制取消當前的編輯狀態
                if st.session_state.get('edit_index') is not None:
                    if 'data' in st.session_state and not st.session_state['data'].empty:
                         st.session_state['data'].at[st.session_state['edit_index'], "勾選"] = False
                    st.session_state['edit_index'] = None
                    st.session_state['current_uuid'] = None
                    st.session_state['form_data'] = {k: '' for k in ['course','book1','pub1','code1','book2','pub2','code2','note1','note2']}
                    st.session_state['editor_key_counter'] += 1
        
        with c_pdf:
            if st.button("📄 轉 PDF (下載)", type="primary", width="stretch"):
                if dept:
                    with st.spinner(f"正在處理 {dept} PDF..."):
                        if st.session_state.get('use_history_checkbox'):
                            hist_year = st.session_state.get('history_year_val')
                            if hist_year:
                                st.info(f"同步 {hist_year} 年資料中...")
                                if sync_history_to_db(dept, hist_year): st.success("✅ 資料同步完成")
                                else: st.error("❌ 同步失敗")
                        
                        pdf_bytes = create_pdf_report(dept)
                        if pdf_bytes:
                            b64 = base64.b64encode(pdf_bytes).decode('latin-1')
                            st.markdown(f'<a href="data:application/pdf;base64,{b64}" download="{dept}_教科書總表.pdf" style="text-decoration:none; color:white; background-color:#b31412; padding:8px 12px; border-radius:5px; font-weight:bold; font-size:14px; display:block; text-align:center;">⬇️ 點此下載 PDF</a>', unsafe_allow_html=True)
                        else: st.error("生成失敗，Submission 無資料。")
                else: st.warning("請先選擇科別")

    if st.session_state['show_preview']:
        st.info("💡 勾選任一列可跳轉至該課程進行編輯。")
        df_prev = load_preview_data(dept)
        st.session_state['preview_df'] = df_prev
        
        if not df_prev.empty:
            st.data_editor(
                df_prev,
                key="preview_editor",
                on_change=on_preview_change,
                width='stretch',
                column_config={
                    "勾選": st.column_config.CheckboxColumn("編輯", width="small"),
                    "uuid": None, "填報時間": None, "學年度": None,
                    "學期": st.column_config.TextColumn("學期", width="small"),
                    "年級": st.column_config.TextColumn("年級", width="small"),
                    "課程名稱": st.column_config.TextColumn("課程名稱", width="medium"),
                    "教科書(優先1)": st.column_config.TextColumn("教科書", width="medium"),
                    "出版社(1)": st.column_config.TextColumn("出版社", width="small"),
                    "適用班級": st.column_config.TextColumn("適用班級", width="medium"),
                    "備註1": st.column_config.TextColumn("備註", width="small"),
                },
                disabled=["科別", "學期", "年級", "課程名稱", "教科書(優先1)", "冊次(1)", "出版社(1)", "審定字號(1)", "教科書(優先2)", "冊次(2)", "出版社(2)", "審定字號(2)", "適用班級", "備註1", "備註2"],
                column_order=["勾選", "學期", "年級", "課程名稱", "教科書(優先1)", "出版社(1)", "適用班級", "備註1"]
            )
        else:
            st.warning("⚠️ 目前沒有任何資料。")
        st.divider()

    if 'loaded' not in st.session_state and dept and sem and grade: auto_load_data()

    if st.session_state.get('loaded'):
        with st.sidebar:
            st.divider()
            is_edit = st.session_state['edit_index'] is not None
            st.subheader(f"2. 修改第 {st.session_state['edit_index'] + 1} 列" if is_edit else "2. 新增/插入課程")
            
            if is_edit:
                c_can, c_del = st.columns([1, 1])
                if c_can.button("❌ 取消", type="secondary"):
                    st.session_state['edit_index'] = None
                    st.session_state['data']["勾選"] = False
                    st.session_state['editor_key_counter'] += 1
                    st.rerun()
                if c_del.button("🗑️ 刪除此列", type="primary"):
                    if delete_row_from_db(st.session_state.get('current_uuid')):
                        st.session_state['data'] = st.session_state['data'].drop(st.session_state['edit_index']).reset_index(drop=True)
                        st.session_state['edit_index'] = None
                        st.session_state['editor_key_counter'] += 1
                        st.success("已刪除！")
                        st.rerun()

            frm = st.session_state['form_data']
            courses = get_course_list()
            if courses: inp_course = st.selectbox("選擇課程", courses, index=courses.index(frm['course']) if is_edit and frm['course'] in courses else 0)
            else: inp_course = st.text_input("課程名稱", value=frm['course'])
            
            st.markdown("##### 適用班級")
            ca, c1, c2, c3 = st.columns([1,1,1,1])
            ca.checkbox("全部", key="cb_all", on_change=toggle_all_checkboxes)
            c1.checkbox("普通", key="cb_reg", on_change=update_class_list_from_checkboxes)
            c2.checkbox("實技", key="cb_prac", on_change=update_class_list_from_checkboxes)
            c3.checkbox("建教", key="cb_coop", on_change=update_class_list_from_checkboxes)
            
            poss = get_all_possible_classes(grade)
            sel_cls = st.multiselect("最終班級列表:", options=sorted(list(set(poss + st.session_state['active_classes']))), default=st.session_state['active_classes'], key="class_multiselect", on_change=on_multiselect_change)
            inp_cls_str = ",".join(sel_cls)

            st.markdown("**第一優先**")
            inp_bk1 = st.text_input("書名", value=frm['book1'])
            b1, b2 = st.columns([1, 2])
            inp_vol1 = b1.selectbox("冊次", ["全", "上", "下", "I", "II", "III", "IV", "V", "VI"], index=["全", "上", "下", "I", "II", "III", "IV", "V", "VI"].index(frm.get('vol1','全')) if frm.get('vol1') in ["全", "上", "下", "I", "II", "III", "IV", "V", "VI"] else 0)
            inp_pub1 = b2.text_input("出版社", value=frm['pub1'])
            c1, n1 = st.columns(2)
            inp_cod1 = c1.text_input("審定字號", value=frm['code1'])
            inp_nt1 = n1.text_input("備註1(作者/單價)", value=frm['note1'])

            st.markdown("**第二優先**")
            inp_bk2 = st.text_input("備選書名", value=frm['book2'])
            b3, b4 = st.columns([1, 2])
            inp_vol2 = b3.selectbox("冊次(2)", ["全", "上", "下", "I", "II", "III", "IV", "V", "VI"], index=["全", "上", "下", "I", "II", "III", "IV", "V", "VI"].index(frm.get('vol2','全')) if frm.get('vol2') in ["全", "上", "下", "I", "II", "III", "IV", "V", "VI"] else 0)
            inp_pub2 = b4.text_input("出版社(2)", value=frm['pub2'])
            c2, n2 = st.columns(2)
            inp_cod2 = c2.text_input("審定字號(2)", value=frm['code2'])
            inp_nt2 = n2.text_input("備註2(作者/單價)", value=frm['note2'])

            if st.button("🔄 更新 (存檔)" if is_edit else "➕ 加入 (存檔)", type="primary", width="stretch"):
                if not inp_cls_str or not inp_bk1 or not inp_pub1 or not inp_vol1: st.error("⚠️ 班級、書名、冊次、出版社必填")
                else:
                    uid = st.session_state.get('current_uuid') if is_edit else str(uuid.uuid4())
                    row = {
                        "uuid": uid, "科別": dept, "年級": grade, "學期": sem, "課程類別": "部定必修", "課程名稱": inp_course,
                        "教科書(優先1)": inp_bk1, "冊次(1)": inp_vol1, "出版社(1)": inp_pub1, "審定字號(1)": inp_cod1,
                        "教科書(優先2)": inp_bk2, "冊次(2)": inp_vol2, "出版社(2)": inp_pub2, "審定字號(2)": inp_cod2,
                        "適用班級": inp_cls_str, "備註1": inp_nt1, "備註2": inp_nt2
                    }
                    if is_edit: save_single_row(row, st.session_state.get('original_key'))
                    else: save_single_row(row, None)
                    
                    if is_edit:
                        for k, v in row.items():
                            if k in st.session_state['data'].columns: st.session_state['data'].at[st.session_state['edit_index'], k] = v
                        st.session_state['data'].at[st.session_state['edit_index'], "勾選"] = False
                    else:
                        row['勾選'] = False
                        st.session_state['data'] = pd.concat([st.session_state['data'], pd.DataFrame([row])], ignore_index=True)
                    
                    st.session_state['edit_index'] = None
                    st.session_state['editor_key_counter'] += 1
                    st.success("已存檔！")
                    st.rerun()

        st.success(f"目前編輯：**{dept}** / **{grade}年級** / **第{sem}學期**")
        st.data_editor(
            st.session_state['data'], num_rows="dynamic", width='stretch', height=600,
            key=f"main_editor_{st.session_state['editor_key_counter']}", on_change=on_editor_change,
            column_config={
                "勾選": st.column_config.CheckboxColumn("勾選", width="small"),
                "uuid": None, "科別": None, "年級": None, "學期": None,
                "課程類別": st.column_config.TextColumn("類別", width="small", disabled=True),
                "課程名稱": st.column_config.TextColumn("課程名稱", width="medium", disabled=True),
                "適用班級": st.column_config.TextColumn("適用班級", width="medium", disabled=True),
                "教科書(優先1)": st.column_config.TextColumn("教科書(1)", width="medium", disabled=True),
                "冊次(1)": st.column_config.TextColumn("冊次(1)", width="small", disabled=True),
                "出版社(1)": st.column_config.TextColumn("出版社(1)", width="small", disabled=True),
                "備註1": st.column_config.TextColumn("備註", width="small", disabled=True),
                "教科書(優先2)": st.column_config.TextColumn("教科書(2)", width="medium", disabled=True),
                "冊次(2)": st.column_config.TextColumn("冊次(2)", width="small", disabled=True),
                "出版社(2)": st.column_config.TextColumn("出版社(2)", width="small", disabled=True),
                "備註2": st.column_config.TextColumn("備註2", width="small", disabled=True),
                "審定字號(1)": st.column_config.TextColumn("字號(1)", width="small", disabled=True),
                "審定字號(2)": st.column_config.TextColumn("字號(2)", width="small", disabled=True),
            },
            column_order=["勾選", "課程類別", "課程名稱", "適用班級", "教科書(優先1)", "冊次(1)", "出版社(1)", "審定字號(1)", "備註1", "教科書(優先2)", "冊次(2)", "出版社(2)", "審定字號(2)", "備註2"]
        )
    else: st.info("👈 請先在左側選擇科別")

if __name__ == "__main__": main()
