import streamlit as st
import pandas as pd
import plotly.express as px
from datetime import datetime, timedelta
import time
import gspread
from google.oauth2.service_account import Credentials


# =========================================================
# 初期設定・デザイン
# =========================================================

st.set_page_config(
    page_title="生駒祭屋台シフト提出用",
    layout="centered"
)

# Google検索結果に表示させない
st.markdown(
    '<meta name="robots" content="noindex, nofollow">',
    unsafe_allow_html=True
)

st.markdown("""
    <style>
    .block-container {
        max-width: 700px;
        padding-top: 2rem;
    }

    .stButton>button {
        border-radius: 8px;
        font-weight: bold;
    }

    .stRadio > div {
        gap: 0.5rem;
    }

    .norma-box {
        background-color: #f0f8ff;
        padding: 15px;
        border-radius: 8px;
        border-left: 5px solid #4285f4;
        margin-bottom: 20px;
    }
    </style>
""", unsafe_allow_html=True)


# =========================================================
# スプレッドシート接続
# =========================================================

@st.cache_resource
def get_gspread_client():
    """
    Google Sheetsへ接続するクライアントを作成する。

    Streamlit Secrets の private_key に含まれる
    文字列 "\\n" を、実際の改行に変換してから
    GoogleのCredentialsへ渡す。
    """

    scope = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive"
    ]

    # Streamlit Secretsからサービスアカウント情報を取得
    creds_dict = dict(st.secrets["gcp_service_account"])

    # -----------------------------------------------------
    # private_key の改行を修正
    # -----------------------------------------------------
    private_key = creds_dict.get("private_key", "")

    if not private_key:
        raise ValueError(
            "Streamlit Secrets の [gcp_service_account] に "
            "private_key が設定されていません。"
        )

    # TOML内の \n を実際の改行へ変換
    private_key = private_key.replace("\\n", "\n")

    # Windows形式の改行が混ざっていても正常化
    private_key = private_key.replace("\r\n", "\n").replace("\r", "\n")

    # 前後の余計な空白だけ削除
    private_key = private_key.strip()

    # PEMヘッダー・フッターの確認
    if "-----BEGIN PRIVATE KEY-----" not in private_key:
        raise ValueError(
            "private_key に -----BEGIN PRIVATE KEY----- がありません。"
            "Streamlit Secrets の private_key を確認してください。"
        )

    if "-----END PRIVATE KEY-----" not in private_key:
        raise ValueError(
            "private_key に -----END PRIVATE KEY----- がありません。"
            "Streamlit Secrets の private_key を確認してください。"
        )

    # 修正した秘密鍵をCredentialsへ渡す
    creds_dict["private_key"] = private_key

    # Google認証情報を作成
    creds = Credentials.from_service_account_info(
        creds_dict,
        scopes=scope
    )

    # gspreadへ渡す
    return gspread.authorize(creds)


# =========================================================
# Google Sheetsからデータを読み込む
# =========================================================

@st.cache_data(ttl=10, show_spinner=False)
def load_data_from_gsheets():
    """
    Google Sheetsからシフトとアンケートを読み込む。

    Streamlitの再実行のたびにGoogle Sheetsへアクセスすると、
    利用人数が増えたときにGoogle Sheets APIのRead quotaへ
    到達しやすいため、10秒間キャッシュする。
    """

    client = get_gspread_client()

    spreadsheet_name = st.secrets.get(
        "spreadsheet_name",
        "ikomakai_db"
    )

    ss = client.open(spreadsheet_name)

    # -------------------------------------------------
    # shifts
    # -------------------------------------------------

    shift_sheet = ss.worksheet("shifts")
    shift_values = shift_sheet.get_all_values()

    shift_data = []

    if shift_values:
        headers = shift_values[0]

        for values in shift_values[1:]:
            row = {}

            for i, header in enumerate(headers):
                row[header] = values[i] if i < len(values) else ""

            # 完全な空行は無視
            if any(str(v).strip() for v in row.values()):
                shift_data.append(row)

    # -------------------------------------------------
    # prefs
    # -------------------------------------------------

    pref_sheet = ss.worksheet("prefs")
    pref_values = pref_sheet.get_all_values()

    prefs_dict = {}

    if pref_values:
        headers = pref_values[0]

        for values in pref_values[1:]:
            row = {}

            for i, header in enumerate(headers):
                row[header] = values[i] if i < len(values) else ""

            name = row.pop("名前", None)

            if name:
                prefs_dict[name] = row

    return shift_data, prefs_dict


# =========================================================
# Google Sheetsへシフト・アンケートを保存
# =========================================================

def save_shift_to_gsheets(new_rows, user_name, user_pref):
    """
    シフトとアンケートをGoogle Sheetsへ保存する。

    以前の「シート全体をclearして全データを書き直す」方式では、
    複数人が同時に提出した際に他人のデータを上書きする可能性があった。
    ここでは、現在のユーザーに関係する行だけを変更する。

    また、同じシートを1回の保存中に何度も読み込まないようにし、
    429 (Read quota exceeded) が発生しにくい構成にしている。
    """

    client = get_gspread_client()

    spreadsheet_name = st.secrets.get(
        "spreadsheet_name",
        "ikomakai_db"
    )

    # 429が一時的に発生した場合だけ少し待って再試行する
    retry_delays = [0, 2, 5, 10]
    last_error = None

    for delay in retry_delays:

        if delay:
            time.sleep(delay)

        try:
            ss = client.open(spreadsheet_name)

            # =================================================
            # shifts 保存
            # =================================================

            shift_sheet = ss.worksheet("shifts")

            headers = [
                "名前",
                "開始",
                "終了",
                "希望順位",
                "表示区分"
            ]

            # 1回だけ読み込む
            existing_values = shift_sheet.get_all_values()

            # シートが空ならヘッダーを作る
            if not existing_values:
                shift_sheet.update(
                    "A1:E1",
                    [headers]
                )
                existing_values = [headers]

            # A列の名前だけを見て、現在のユーザーの行を特定
            user_rows = []

            for row_num, row_values in enumerate(
                existing_values[1:],
                start=2
            ):
                name = (
                    row_values[0]
                    if len(row_values) > 0
                    else ""
                )

                if str(name) == str(user_name):
                    user_rows.append(row_num)

            # -------------------------------------------------
            # 現在のユーザーの古い行だけ削除
            # -------------------------------------------------
            # 下の行から削除することで、上の行番号がずれないようにする。
            # Google Sheets APIへは1回のbatch_updateで送る。

            if user_rows:
                delete_requests = []

                for row_num in reversed(user_rows):
                    delete_requests.append({
                        "deleteDimension": {
                            "range": {
                                "sheetId": shift_sheet.id,
                                "dimension": "ROWS",
                                "startIndex": row_num - 1,
                                "endIndex": row_num
                            }
                        }
                    })

                ss.batch_update({
                    "requests": delete_requests
                })

            # -------------------------------------------------
            # 新しいシフトをまとめて追加
            # -------------------------------------------------

            shift_values_to_append = []

            for nr in new_rows:
                shift_values_to_append.append([
                    str(nr.get("名前", user_name)),
                    str(nr.get("開始", "")),
                    str(nr.get("終了", "")),
                    str(nr.get("希望順位", "")),
                    str(nr.get("表示区分", ""))
                ])

            # append_rowを何回も呼ばず、append_rowsで1回にまとめる
            if shift_values_to_append:
                shift_sheet.append_rows(
                    shift_values_to_append,
                    value_input_option="USER_ENTERED"
                )

            # =================================================
            # prefs 保存
            # =================================================

            pref_sheet = ss.worksheet("prefs")

            p_headers = [
                "名前",
                "9時間可能か",
                "理由",
                "入り方の希望",
                "一人暮らし"
            ]

            # prefsも1回だけ読み込む
            existing_pref_values = pref_sheet.get_all_values()

            if not existing_pref_values:
                pref_sheet.update(
                    "A1:E1",
                    [p_headers]
                )
                existing_pref_values = [p_headers]

            pref_row = None

            for row_num, row_values in enumerate(
                existing_pref_values[1:],
                start=2
            ):
                name = (
                    row_values[0]
                    if len(row_values) > 0
                    else ""
                )

                if str(name) == str(user_name):
                    pref_row = row_num
                    break

            pref_data = [
                str(user_name),
                str(user_pref.get("9時間可能か", "-")),
                str(user_pref.get("理由", "-")),
                str(user_pref.get("入り方の希望", "-")),
                str(user_pref.get("一人暮らし", "-"))
            ]

            # 既存ユーザーならその行だけ更新
            if pref_row is not None:
                pref_sheet.update(
                    f"A{pref_row}:E{pref_row}",
                    [pref_data],
                    value_input_option="USER_ENTERED"
                )

            # 初めてなら新しい行を追加
            else:
                pref_sheet.append_row(
                    pref_data,
                    value_input_option="USER_ENTERED"
                )

            # -------------------------------------------------
            # キャッシュを消す
            # -------------------------------------------------
            # 次回の読み込みで保存直後の最新データを取得できるようにする。
            st.cache_data.clear()

            return

        except Exception as e:
            last_error = e

            # 429以外はすぐにエラーにする
            if "429" not in str(e) and "Quota exceeded" not in str(e):
                break

    st.error(
        f"⚠️ スプレッドシート保存詳細エラー: {last_error}"
    )
    raise last_error

# =========================================================
# データ定義
# =========================================================

DAYS = [
    "11月2日(日)",
    "11月3日(月・祝)",
    "11月4日(火)"
]


DATE_MAP = {

    "11月2日(日)": "2026-11-02",

    "11月3日(月・祝)": "2026-11-03",

    "11月4日(火)": "2026-11-04"
}


MEMBERS = [

    "選択してください...",

    "宮本(責任者)",
    "村井(副責任者)",

    "岩田",
    "植野",
    "宇田",
    "大野",
    "尾濱",

    "加納",
    "川井",
    "川崎",
    "喜多川",
    "小池",

    "駒形",
    "佐藤",
    "高畑",
    "高村",
    "田中",

    "永田",
    "坂東",
    "東",
    "廣中",
    "堀川",

    "眞野",
    "宮崎",
    "八木(健)",
    "八木(椋)",
    "柳川"
]


# =========================================================
# 時間選択肢
# =========================================================

def get_time_options(day_index):

    start_hour = 11 if day_index == 0 else 9

    times = []

    for h in range(start_hour, 20):

        times.append(f"{h:02d}:00")
        times.append(f"{h:02d}:30")

    times.append("20:00")

    return times


# =========================================================
# 勤務時間計算
# =========================================================

def calculate_hours(start_str, end_str):

    fmt = "%H:%M"

    t_start = datetime.strptime(
        start_str,
        fmt
    )

    t_end = datetime.strptime(
        end_str,
        fmt
    )

    return (t_end - t_start).seconds / 3600


# =========================================================
# セッション状態の初期化
# =========================================================

if "shift_list" not in st.session_state:

    st.session_state.shift_list = []


# =========================================================
# Google Sheetsから安全にロード
# =========================================================

try:

    gs_shifts, gs_prefs = load_data_from_gsheets()

    st.session_state.submitted_shifts = (
        gs_shifts if gs_shifts else []
    )

    st.session_state.user_prefs = (
        gs_prefs if gs_prefs else {}
    )

except Exception as e:

    st.error(
        f"データ読み込み時エラー: {e}"
    )

    if "submitted_shifts" not in st.session_state:

        st.session_state.submitted_shifts = []

    if "user_prefs" not in st.session_state:

        st.session_state.user_prefs = []


if "auto_scheduled" not in st.session_state:

    st.session_state.auto_scheduled = []


# =========================================================
# メインUI
# =========================================================

st.title("🍟 〜生駒祭屋台シフト提出用〜")

st.markdown(
    "希望する日時を追加して、最後に提出してください。"
)


# =========================================================
# 注意事項
# =========================================================

st.markdown("""
<div class="norma-box">

<strong>⚠️ シフト提出に関すること</strong>

<ul style="margin-bottom: 0;">

<li>
<strong>【3日間合計で 1人あたり 最低9時間】</strong>
を目標として欲しいです
（選手権など都合がある場合は考慮します）
</li>

<li>
バランスよくシフトを組むために、
<strong>【第一希望は 3日間合計で 最大10時間まで】</strong>
しか提出できません。
（第二希望は無制限です）
</li>

<li>
<strong>【連続での勤務は最大5時間まで】</strong>
としてます。
1時間の休憩を挟み1日で9時間入ることは可能です。
</li>

<li>
<small>
※法律に基づき、1日の実労働時間が
<strong>6時間を超える場合は45分
（システム上は1時間）以上、
8時間を超える場合は1時間以上の休憩</strong>
を必ず挟むように自動で調整されます。
</small>
</li>

<li>
<small>
※責任者・副責任者はどっちかは常にいます
</small>
</li>

</ul>

</div>
""", unsafe_allow_html=True)


st.divider()


# =========================================================
# 1. ユーザー情報
# =========================================================

with st.expander(
    "👤 氏名を選択（ここをタップして名前を選んでください）",
    expanded=True
):

    user_name = st.radio(
        "自分の名前を選んでください",
        MEMBERS,
        key="user_select",
        label_visibility="collapsed"
    )

    if user_name != "選択してください...":

        has_submitted = any(
            str(s.get("名前")) == user_name
            for s in st.session_state.submitted_shifts
        )

        if has_submitted:

            st.warning(
                "📝 あなたは既にシフトを提出済みです。"
                "修正する場合は新しいシフトを追加して"
                "「提出（上書き）」するか、"
                "以下のボタンで白紙に戻せます。"
            )

            if st.button(
                "⚠️ 自分の提出済みシフトをすべてリセット（削除）する"
            ):

                updated_shifts = [
                    s
                    for s in st.session_state.submitted_shifts
                    if str(s.get("名前")) != user_name
                ]

                default_pref = (
                    st.session_state.user_prefs.get(
                        user_name,
                        {
                            "9時間可能か": "-",
                            "理由": "-",
                            "入り方の希望": "-",
                            "一人暮らし": "-"
                        }
                    )
                )

                save_shift_to_gsheets(
                    updated_shifts,
                    user_name,
                    default_pref
                )

                st.success(
                    "提出済みのシフトをリセットしました！"
                )

                st.rerun()


st.markdown("<br>", unsafe_allow_html=True)


# =========================================================
# 1.5 アンケート
# =========================================================

with st.container(border=True):

    st.markdown(
        "#### 📝 シフト調整アンケート（※必須）"
    )

    can_work = st.radio(
        "① 3日間で最低9時間は入れますか？",
        ["はい", "いいえ"],
        horizontal=True
    )

    reason = ""

    if can_work == "いいえ":

        reason = st.text_input(
            "入れない理由を教えてください",
            placeholder="例：3日は選手権があるため"
        )

    st.markdown("---")

    shift_style = st.radio(
        "② シフトの入り方の希望",
        [
            "できれば1日でたくさん入りたい、終わらせたい",
            "2、3日に分けても問題ない"
        ],
        horizontal=False
    )

    st.markdown("---")

    living_alone = st.radio(
        "③ 一人暮らしですか？",
        ["はい", "いいえ"],
        horizontal=True
    )

    if living_alone == "はい":

        st.info(
            "💡 一人暮らしの方（「はい」の方）には、"
            "できる限り朝のシフトに入ってもらうよう"
            "調整する予定です。ご協力お願いします！"
        )


st.markdown("<br>", unsafe_allow_html=True)


# =========================================================
# 2. シフト追加フォーム
# =========================================================

with st.container(border=True):

    st.markdown(
        "#### 🕒 シフトの追加"
    )

    st.info(
        "💡 提出し直す場合、"
        "同じ日の古いシフトは自動で上書き（削除）されます。"
    )

    priority = st.radio(
        "希望順位",
        ["第一希望", "第二希望"],
        horizontal=False
    )

    selected_day = st.selectbox(
        "日付を選択",
        DAYS
    )

    day_idx = DAYS.index(
        selected_day
    )

    time_options = get_time_options(
        day_idx
    )

    st.markdown(
        "##### 時間帯"
    )

    selected_time = st.select_slider(
        "開始時間と終了時間をスライドして選択",
        options=time_options,
        value=(
            time_options[0],
            time_options[4]
        )
    )

    start_time, end_time = selected_time

    if st.button(
        "➕ このシフトをリストに追加"
    ):

        current_name = st.session_state.user_select

        if current_name == "選択してください...":

            st.error(
                "上の「氏名を選択」を開いて、"
                "自分の名前を選んでから追加してください。"
            )

        elif start_time == end_time:

            st.error(
                "開始時間と終了時間が同じです。"
                "正しい時間帯を選択してください。"
            )

        else:

            new_shift_hours = calculate_hours(
                start_time,
                end_time
            )

            is_leader = (
                "(責任者)" in current_name
                or "(副責任者)" in current_name
            )

            total_priority_hours = 0

            # ---------------------------------------------
            # 既に提出済みのシフトを確認
            # ---------------------------------------------

            for old_shift in st.session_state.submitted_shifts:

                old_date_str = str(
                    old_shift.get("開始", "")
                ).split(" ")[0]

                if (
                    str(old_shift.get("名前"))
                    == current_name
                    and old_shift.get("希望順位")
                    == priority
                    and old_date_str
                    != DATE_MAP[selected_day]
                ):

                    parts = str(
                        old_shift.get("開始", "")
                    ).split(" ")

                    end_parts = str(
                        old_shift.get("終了", "")
                    ).split(" ")

                    if (
                        len(parts) > 1
                        and len(end_parts) > 1
                    ):

                        total_priority_hours += (
                            calculate_hours(
                                parts[1],
                                end_parts[1]
                            )
                        )

            # ---------------------------------------------
            # 今回追加するリスト内のシフト
            # ---------------------------------------------

            for list_shift in st.session_state.shift_list:

                if list_shift["希望順位"] == priority:

                    total_priority_hours += (
                        list_shift["時間数"]
                    )

            # ---------------------------------------------
            # 第一希望10時間制限
            # ---------------------------------------------

            if (
                not is_leader
                and priority == "第一希望"
                and total_priority_hours
                + new_shift_hours > 10
            ):

                st.error(
                    f"⚠️ 3日間合計の {priority} "
                    f"上限（10時間）を超えてしまいます。\n"
                    f"（すでに提出済みの分も含め、現在 "
                    f"{total_priority_hours}時間 分の "
                    f"{priority} があります）\n"
                    f"修正する場合は、下のリストから古いものを"
                    f"「❌ 削除」してから再度追加してください。"
                )

            else:

                if (
                    not is_leader
                    and new_shift_hours > 5
                ):

                    st.warning(
                        f"⚠️ 連続 {new_shift_hours} 時間の"
                        "シフトが追加されました。"
                        "（原則、連続勤務は最大5時間までとしています）"
                    )

                elif is_leader:

                    st.success(
                        f"{new_shift_hours}時間の"
                        "シフトを追加しました！（責任者枠）"
                    )

                else:

                    st.success(
                        f"{new_shift_hours}時間の"
                        "シフトを追加しました！"
                    )

                st.session_state.shift_list.append({

                    "日付": selected_day,

                    "開始": start_time,

                    "終了": end_time,

                    "時間数": new_shift_hours,

                    "希望順位": priority

                })


st.markdown("<br>", unsafe_allow_html=True)


# =========================================================
# 3. 追加されたシフト確認
# =========================================================

st.markdown(
    "#### 📋 提出予定のシフト一覧"
)

if len(st.session_state.shift_list) == 0:

    st.info(
        "まだシフトが追加されていません。"
        "（シフトに入れない場合は、アンケートに回答して"
        "下の「提出する」を押してください）"
    )

else:

    for i, shift in enumerate(
        st.session_state.shift_list
    ):

        col1, col2 = st.columns(
            [5, 2]
        )

        with col1:

            st.markdown(
                f"**{shift['日付']}** | "
                f"{shift['開始']}〜{shift['終了']} | "
                f"**{shift['希望順位']}** "
                f"({shift['時間数']}h)"
            )

        with col2:

            if st.button(
                "❌ 削除",
                key=f"del_{i}"
            ):

                st.session_state.shift_list.pop(i)

                st.rerun()


st.markdown("<br>", unsafe_allow_html=True)


# =========================================================
# 提出ボタン
# =========================================================

btn_col1, btn_col2 = st.columns(
    [1, 2]
)


with btn_col1:

    if st.button("🗑️ 全てクリア"):

        st.session_state.shift_list = []

        st.rerun()


with btn_col2:

    if st.button(
        "🚀 以上の内容で提出する",
        type="primary",
        use_container_width=True
    ):

        current_name = (
            st.session_state.user_select
        )

        # ---------------------------------------------
        # 名前チェック
        # ---------------------------------------------

        if current_name == "選択してください...":

            st.error(
                "エラー：氏名が選択されていません！"
                "一番上で名前を選んでください。"
            )

        # ---------------------------------------------
        # 理由チェック
        # ---------------------------------------------

        elif (
            can_work == "いいえ"
            and not reason.strip()
        ):

            st.error(
                "エラー：アンケートの"
                "「入れない理由」を入力してください。"
            )

        else:

            # -----------------------------------------
            # アンケートデータ
            # -----------------------------------------

            new_pref_data = {

                "9時間可能か":
                    can_work,

                "理由":
                    reason.strip()
                    if can_work == "いいえ"
                    else "-",

                "入り方の希望":
                    shift_style,

                "一人暮らし":
                    living_alone

            }

            # -----------------------------------------
            # 現在のシフトを取得
            # -----------------------------------------

            current_shifts = st.session_state.get(
                "submitted_shifts",
                []
            )

            submitted_days = set(
                [
                    shift["日付"]
                    for shift
                    in st.session_state.shift_list
                ]
            )

            filtered_shifts = []

            # -----------------------------------------
            # 同じユーザー・同じ日の古いシフト削除
            # -----------------------------------------

            for old_shift in current_shifts:

                s_start = str(
                    old_shift.get("開始", "")
                )

                old_date_str = (
                    s_start.split(" ")[0]
                    if " " in s_start
                    else ""
                )

                should_remove = False

                if (
                    str(old_shift.get("名前"))
                    == current_name
                ):

                    for day in submitted_days:

                        if (
                            DATE_MAP[day]
                            == old_date_str
                        ):

                            should_remove = True

                            break

                if not should_remove:

                    filtered_shifts.append(
                        old_shift
                    )

            # -----------------------------------------
            # 新しいシフトを追加
            # -----------------------------------------

            for shift in (
                st.session_state.shift_list
            ):

                date_str = DATE_MAP[
                    shift["日付"]
                ]

                is_leader = (
                    "(責任者)" in current_name
                    or "(副責任者)" in current_name
                )

                display_category = (
                    "責任者・副責任者"
                    if is_leader
                    else shift["希望順位"]
                )

                filtered_shifts.append({

                    "名前":
                        current_name,

                    "開始":
                        f"{date_str} "
                        f"{shift['開始']}",

                    "終了":
                        f"{date_str} "
                        f"{shift['終了']}",

                    "希望順位":
                        shift["希望順位"],

                    "表示区分":
                        display_category

                })

            # -----------------------------------------
            # Google Sheetsへ保存
            # -----------------------------------------

            save_shift_to_gsheets(
                filtered_shifts,
                current_name,
                new_pref_data
            )

            # 今回保存した内容をセッションにも反映
            st.session_state.submitted_shifts = filtered_shifts
            st.session_state.user_prefs[current_name] = new_pref_data

            st.balloons()

            st.success(
                f"ありがとうございます！ "
                f"{current_name} さんのシフトと"
                "アンケート回答をスプレッドシートに同期しました。"
            )

            st.session_state.shift_list = []

            st.rerun()


st.divider()


# =========================================================
# 4. 全体の提出状況（ガントチャート）
# =========================================================

st.markdown(
    "#### 📊 全体の提出希望状況"
)


def draw_gantt_chart(
    data,
    title_suffix=""
):

    tabs = st.tabs(
        DAYS
    )

    color_map = {

        "責任者・副責任者":
            "#FFD700",

        "第一希望":
            "#4285F4",

        "第二希望":
            "#34A853",

        "自動編成確定":
            "#9C27B0"

    }

    for idx, tab in enumerate(tabs):

        with tab:

            target_day = DAYS[idx]

            target_date_str = DATE_MAP[
                target_day
            ]

            day_shifts = [
                s
                for s in data
                if str(
                    s.get("開始", "")
                ).startswith(
                    target_date_str
                )
            ]

            if len(day_shifts) == 0:

                st.info(
                    f"{target_day} のデータは"
                    "まだありません。"
                )

            else:

                df_timeline = pd.DataFrame(
                    day_shifts
                )

                # 開始列の文字化け対策
                col_name = (
                    "开始"
                    if (
                        "開始"
                        not in df_timeline.columns
                        and
                        "开始"
                        in df_timeline.columns
                    )
                    else "開始"
                )

                df_timeline["開始_dt"] = (
                    pd.to_datetime(
                        df_timeline[col_name]
                    )
                )

                df_timeline["終了_dt"] = (
                    pd.to_datetime(
                        df_timeline["終了"]
                    )
                )

                # -------------------------------------
                # 並び順
                # -------------------------------------

                def get_sort_score(name):

                    if "(責任者)" in str(name):

                        return 1

                    elif "(副責任者)" in str(name):

                        return 2

                    else:

                        return 3

                df_timeline["sort_score"] = (
                    df_timeline["名前"]
                    .apply(get_sort_score)
                )

                df_timeline = (
                    df_timeline
                    .sort_values(
                        by=[
                            "sort_score",
                            "名前"
                        ],
                        ascending=[
                            True,
                            True
                        ]
                    )
                )

                ordered_names = (
                    df_timeline["名前"]
                    .unique()
                    .tolist()
                )

                ordered_names.reverse()

                # -------------------------------------
                # ガントチャート
                # -------------------------------------

                fig = px.timeline(

                    df_timeline,

                    x_start="開始_dt",

                    x_end="終了_dt",

                    y="名前",

                    color="表示区分",

                    color_discrete_map=color_map,

                    hover_data=[
                        "希望順位"
                    ],

                    height=max(
                        250,
                        45
                        * len(
                            df_timeline["名前"]
                            .unique()
                        )
                    )
                )

                fig.update_yaxes(
                    categoryorder="array",
                    categoryarray=ordered_names
                )

                if idx == 0:

                    range_start = (
                        f"{target_date_str} 10:30"
                    )

                else:

                    range_start = (
                        f"{target_date_str} 08:30"
                    )

                range_end = (
                    f"{target_date_str} 20:30"
                )

                fig.update_xaxes(

                    tickformat="%H:%M",

                    dtick=3600000,

                    title="時間",

                    showgrid=True,

                    gridwidth=1.5,

                    gridcolor="lightgray",

                    griddash="solid",

                    tickangle=0,

                    range=[
                        range_start,
                        range_end
                    ],

                    minor=dict(
                        ticklen=0,
                        dtick=1800000,
                        showgrid=True,
                        gridwidth=1,
                        gridcolor="lightgray",
                        griddash="dash"
                    )
                )

                fig.update_layout(

                    margin=dict(
                        l=0,
                        r=0,
                        t=30,
                        b=30
                    ),

                    legend_title="区分",

                    xaxis=dict(
                        side="bottom"
                    )
                )

                st.plotly_chart(
                    fig,
                    use_container_width=True,
                    key=f"chart_{idx}_{title_suffix}"
                )


# =========================================================
# ガントチャート表示
# =========================================================

if len(
    st.session_state.submitted_shifts
) == 0:

    st.info(
        "まだ誰のシフトも提出されていません。"
    )

else:

    draw_gantt_chart(
        st.session_state.submitted_shifts,
        "raw"
    )


st.divider()


# =========================================================
# 5. アンケート回答一覧
# =========================================================

with st.expander(
    "📝 メンバーのアンケート回答一覧を開く"
):

    if not st.session_state.user_prefs:

        st.info(
            "現在、アンケートを提出している"
            "メンバーはいません。"
        )

    else:

        prefs_list = []

        for name, prefs in (
            st.session_state.user_prefs.items()
        ):

            prefs_list.append({

                "名前":
                    name,

                "9時間可能か":
                    prefs.get(
                        "9時間可能か",
                        prefs.get(
                            "9時間クリア",
                            "-"
                        )
                    ),

                "理由":
                    prefs.get(
                        "理由",
                        "-"
                    ),

                "入り方の希望":
                    prefs.get(
                        "入り方の希望",
                        "-"
                    ),

                "一人暮らし":
                    prefs.get(
                        "一人暮らし",
                        "-"
                    )

            })

        prefs_df = pd.DataFrame(
            prefs_list
        )

        st.dataframe(
            prefs_df,
            use_container_width=True,
            hide_index=True
        )


st.divider()


# =========================================================
# 6. 自動シフト編成
# =========================================================

st.markdown(
    "#### 🤖 自動シフト編成ツール"
    "（仮なのであくまでも参考程度に）"
)

st.markdown(
    "皆が提出した希望から仮のシフト案を自動生成します。"
)


if st.button(
    "シフト案を自動作成する",
    type="primary",
    use_container_width=True
):

    if len(
        st.session_state.submitted_shifts
    ) == 0:

        st.error(
            "希望が一つも提出されていないため、"
            "シフトを作成できません。"
        )

    else:

        with st.spinner(
            "計算中... ルールと希望を調整しています..."
        ):

            final_schedule = []

            work_counts = {
                m: 0
                for m in MEMBERS
                if m != "選択してください..."
            }

            continuous_work = {
                m: 0
                for m in MEMBERS
                if m != "選択してください..."
            }

            forced_break_left = {
                m: 0
                for m in MEMBERS
                if m != "選択してください..."
            }

            daily_worked = {
                m: 0
                for m in MEMBERS
                if m != "選択してください..."
            }

            daily_break_taken = {
                m: 0
                for m in MEMBERS
                if m != "選択してください..."
            }

            has_started_today = {
                m: False
                for m in MEMBERS
                if m != "選択してください..."
            }

            # =============================================
            # 空き時間表作成
            # =============================================

            availability = {}

            for day_idx, day_name in enumerate(
                DAYS
            ):

                date_str = DATE_MAP[
                    day_name
                ]

                options = get_time_options(
                    day_idx
                )

                for time_str in options[:-1]:

                    slot_key = (
                        f"{date_str} {time_str}"
                    )

                    availability[
                        slot_key
                    ] = {}

            # =============================================
            # 提出された希望を30分単位へ変換
            # =============================================

            for shift in (
                st.session_state.submitted_shifts
            ):

                name = shift.get(
                    "名前"
                )

                s_start = str(
                    shift.get(
                        "開始",
                        ""
                    )
                )

                s_end = str(
                    shift.get(
                        "終了",
                        ""
                    )
                )

                if (
                    not name
                    or " " not in s_start
                    or " " not in s_end
                ):

                    continue

                start_dt = datetime.strptime(
                    s_start,
                    "%Y-%m-%d %H:%M"
                )

                end_dt = datetime.strptime(
                    s_end,
                    "%Y-%m-%d %H:%M"
                )

                priority = shift.get(
                    "希望順位",
                    ""
                )

                current_dt = start_dt

                while current_dt < end_dt:

                    slot_key = (
                        current_dt.strftime(
                            "%Y-%m-%d %H:%M"
                        )
                    )

                    if slot_key in availability:

                        availability[
                            slot_key
                        ][name] = priority

                    current_dt += timedelta(
                        minutes=30
                    )

            current_date_str = ""

            # =============================================
            # 30分ごとに自動編成
            # =============================================

            for (
                slot_key,
                available_people
            ) in sorted(
                availability.items()
            ):

                slot_time = datetime.strptime(
                    slot_key,
                    "%Y-%m-%d %H:%M"
                )

                slot_date = (
                    slot_key.split(" ")[0]
                )

                is_morning = (
                    slot_time.hour < 14
                )

                # -----------------------------------------
                # 日付が変わったらリセット
                # -----------------------------------------

                if (
                    slot_date
                    != current_date_str
                ):

                    current_date_str = (
                        slot_date
                    )

                    for m in MEMBERS:

                        if (
                            m
                            != "選択してください..."
                        ):

                            continuous_work[m] = 0

                            forced_break_left[m] = 0

                            daily_worked[m] = 0

                            daily_break_taken[m] = 0

                            has_started_today[m] = False

                selected_for_this_slot = []

                candidates = []

                # -----------------------------------------
                # 候補者を作る
                # -----------------------------------------

                for (
                    name,
                    priority
                ) in available_people.items():

                    is_leader = (
                        "(責任者)" in name
                        or "(副責任者)" in name
                    )

                    # 強制休憩中
                    if (
                        not is_leader
                        and forced_break_left.get(
                            name,
                            0
                        ) > 0
                    ):

                        forced_break_left[name] -= 1

                        if has_started_today.get(
                            name
                        ):

                            daily_break_taken[
                                name
                            ] += 1

                        continuous_work[
                            name
                        ] = 0

                        continue

                    # -------------------------------------
                    # 一定時間勤務後の休憩
                    # -------------------------------------

                    if (
                        not is_leader
                        and daily_worked.get(
                            name,
                            0
                        ) >= 11
                        and daily_break_taken.get(
                            name,
                            0
                        ) < 2
                    ):

                        forced_break_left[
                            name
                        ] = 2

                        forced_break_left[
                            name
                        ] -= 1

                        if has_started_today.get(
                            name
                        ):

                            daily_break_taken[
                                name
                            ] += 1

                        continuous_work[
                            name
                        ] = 0

                        continue

                    # -------------------------------------
                    # 5時間連続勤務後の休憩
                    # -------------------------------------

                    if (
                        not is_leader
                        and continuous_work.get(
                            name,
                            0
                        ) >= 10
                    ):

                        forced_break_left[
                            name
                        ] = 2

                        forced_break_left[
                            name
                        ] -= 1

                        if has_started_today.get(
                            name
                        ):

                            daily_break_taken[
                                name
                            ] += 1

                        continuous_work[
                            name
                        ] = 0

                        continue

                    candidates.append(
                        name
                    )

                # =========================================
                # 候補者をスコアリング
                # =========================================

                scored_candidates = []

                for name in candidates:

                    score = 0

                    is_leader = (
                        "(責任者)" in name
                        or "(副責任者)" in name
                    )

                    priority = (
                        available_people[name]
                    )

                    prefs = (
                        st.session_state
                        .user_prefs
                        .get(
                            name,
                            {}
                        )
                    )

                    # 責任者優先
                    if is_leader:

                        score += 10000

                    # 第一希望優先
                    if priority == "第一希望":

                        score += 1000

                    # 一人暮らしの朝シフト
                    if (
                        is_morning
                        and str(
                            prefs.get(
                                "一人暮らし",
                                ""
                            )
                        ) == "はい"
                    ):

                        score += 500

                    # 1日にまとめたい人
                    if (
                        str(
                            prefs.get(
                                "入り方の希望",
                                ""
                            )
                        )
                        ==
                        "できれば1日でたくさん入りたい、終わらせたい"
                        and
                        continuous_work.get(
                            name,
                            0
                        ) > 0
                    ):

                        score += 300

                    # 総勤務時間を少し均等化
                    score -= (
                        work_counts.get(
                            name,
                            0
                        ) * 10
                    )

                    scored_candidates.append(
                        (
                            score,
                            name
                        )
                    )

                # -----------------------------------------
                # スコア順
                # -----------------------------------------

                scored_candidates.sort(
                    reverse=True,
                    key=lambda x: x[0]
                )

                leaders_assigned = 0

                # =========================================
                # 最大8人
                # =========================================

                for (
                    score,
                    name
                ) in scored_candidates:

                    if (
                        len(
                            selected_for_this_slot
                        ) >= 8
                    ):

                        break

                    is_leader = (
                        "(責任者)" in name
                        or "(副責任者)" in name
                    )

                    # リーダーの人数調整
                    if (
                        is_leader
                        and leaders_assigned >= 1
                    ):

                        if (
                            len(
                                selected_for_this_slot
                            ) >= 5
                        ):

                            continue

                    selected_for_this_slot.append(
                        name
                    )

                    if is_leader:

                        leaders_assigned += 1

                # =========================================
                # 勤務状況を更新
                # =========================================

                for name in MEMBERS:

                    if (
                        name
                        == "選択してください..."
                    ):

                        continue

                    if (
                        name
                        in selected_for_this_slot
                    ):

                        has_started_today[
                            name
                        ] = True

                        work_counts[
                            name
                        ] = (
                            work_counts.get(
                                name,
                                0
                            ) + 1
                        )

                        continuous_work[
                            name
                        ] = (
                            continuous_work.get(
                                name,
                                0
                            ) + 1
                        )

                        daily_worked[
                            name
                        ] = (
                            daily_worked.get(
                                name,
                                0
                            ) + 1
                        )

                        is_leader = (
                            "(責任者)" in name
                            or "(副責任者)" in name
                        )

                        end_slot_time = (
                            slot_time
                            + timedelta(
                                minutes=30
                            )
                        )

                        final_schedule.append({

                            "名前":
                                name,

                            "開始":
                                slot_time.strftime(
                                    "%Y-%m-%d %H:%M"
                                ),

                            "終了":
                                end_slot_time.strftime(
                                    "%Y-%m-%d %H:%M"
                                ),

                            "希望順位":
                                "自動割当",

                            "表示区分":
                                (
                                    "責任者・副責任者"
                                    if is_leader
                                    else "自動編成確定"
                                )

                        })

                    else:

                        continuous_work[
                            name
                        ] = 0

                        if (
                            has_started_today.get(
                                name
                            )
                            and
                            forced_break_left.get(
                                name,
                                0
                            ) == 0
                        ):

                            daily_break_taken[
                                name
                            ] += 1

            # =============================================
            # DataFrame化
            # =============================================

            df_final = pd.DataFrame(
                final_schedule
            )

            if not df_final.empty:

                df_final = (
                    df_final
                    .sort_values(
                        by=[
                            "名前",
                            "開始"
                        ]
                    )
                )

                merged_schedule = []

                current_shift = None

                # =========================================
                # 連続する30分シフトを結合
                # =========================================

                for _, row in (
                    df_final.iterrows()
                ):

                    col_start = (
                        "开始"
                        if (
                            "開始" not in row
                            and
                            "开始" in row
                        )
                        else "開始"
                    )

                    if current_shift is None:

                        current_shift = {

                            "名前":
                                row["名前"],

                            "開始":
                                row[col_start],

                            "終了":
                                row["終了"],

                            "希望順位":
                                row.get(
                                    "希望順位",
                                    "自動割当"
                                ),

                            "表示区分":
                                row.get(
                                    "表示区分",
                                    "自動編成確定"
                                )

                        }

                    else:

                        if (
                            current_shift["名前"]
                            == row["名前"]
                            and
                            str(
                                current_shift["終了"]
                            )
                            ==
                            str(
                                row[col_start]
                            )
                        ):

                            current_shift[
                                "終了"
                            ] = row["終了"]

                        else:

                            merged_schedule.append(
                                current_shift
                            )

                            current_shift = {

                                "名前":
                                    row["名前"],

                                "開始":
                                    row[col_start],

                                "終了":
                                    row["終了"],

                                "希望順位":
                                    row.get(
                                        "希望順位",
                                        "自動割当"
                                    ),

                                "表示区分":
                                    row.get(
                                        "表示区分",
                                        "自動編成確定"
                                    )

                            }

                if current_shift is not None:

                    merged_schedule.append(
                        current_shift
                    )

                st.session_state.auto_scheduled = (
                    merged_schedule
                )

        st.success(
            "✨ シフト案の作成が完了しました！"
        )


# =========================================================
# 自動編成結果
# =========================================================

if len(
    st.session_state.auto_scheduled
) > 0:

    st.markdown(
        "##### 🎉 生成されたシフト案"
    )

    draw_gantt_chart(
        st.session_state.auto_scheduled,
        "auto"
    )

    st.markdown(
        "##### ⏱️ 各メンバーの割り当て時間合計"
    )

    summary = {}

    for shift in (
        st.session_state.auto_scheduled
    ):

        name = shift["名前"]

        s_start = (
            str(
                shift["開始"]
            ).split(" ")[1]
        )

        s_end = (
            str(
                shift["終了"]
            ).split(" ")[1]
        )

        hours = calculate_hours(
            s_start,
            s_end
        )

        summary[name] = (
            summary.get(
                name,
                0
            )
            + hours
        )

    summary_df = pd.DataFrame(
        list(
            summary.items()
        ),
        columns=[
            "名前",
            "合計時間(h)"
        ]
    )

    summary_df = (
        summary_df
        .sort_values(
            by="合計時間(h)",
            ascending=False
        )
    )

    st.dataframe(
        summary_df,
        hide_index=True
    )
