import csv
import hmac
import inspect
import io
import json
import random
import re
import threading
import time
import unicodedata
from pathlib import Path

import streamlit as st

st.set_page_config(page_title="熱中症ビンゴ", page_icon="🥵", layout="centered")

# Streamlitのバージョン差を吸収する（ボタンを横いっぱいに広げる指定）
STRETCH = (
    {"width": "stretch"}
    if "width" in inspect.signature(st.button).parameters
    else {"use_container_width": True}
)
STRETCH_DL = (
    {"width": "stretch"}
    if "width" in inspect.signature(st.download_button).parameters
    else {"use_container_width": True}
)

# ------------------------------------------------------------
# スマホ向けの見た目
#  - 上のバー（Streamlitのヘッダー）に隠れないよう、上の余白を広げる
#  - 3×3のマスを、スマホでも横3列のまま表示する
# ------------------------------------------------------------
st.markdown(
    """
<style>
.block-container{padding:4.5rem .6rem 3rem!important}
[class*="st-key-brow"] [data-testid="stHorizontalBlock"]{flex-direction:row!important;flex-wrap:nowrap!important;gap:.35rem!important}
[class*="st-key-brow"] [data-testid="stColumn"],[class*="st-key-brow"] [data-testid="column"]{min-width:0!important;width:33%!important;flex:1 1 0!important}
[class*="st-key-brow"] .stButton>button{min-height:5.6rem;padding:.25rem .3rem;line-height:1.3}
[class*="st-key-brow"] .stButton>button p{font-size:.88rem;word-break:break-all;white-space:normal}
</style>
""",
    unsafe_allow_html=True,
)

# ------------------------------------------------------------
# 定数
# ------------------------------------------------------------
N_CHILD = 6  # 子どものお題の数
N_ADULT = 7  # 大人のお題の数
MAX_LEN = 60  # お題1つの最大文字数
CHILD = "子ども"

# 見本のお題（コードの先頭が 0 のとき使われる）
DEFAULT_TOPICS = {
    "c": (
        "太陽の下、帽子をかぶらずに遊んだ人",
        "水筒が空になって困った人",
        "プールの後にフラフラした人",
        "運動会の練習で頭が痛くなった人",
        "車の中で待たされて暑かった人",
        "公園のすべり台が熱くてびっくりした人",
    ),
    "a": (
        "炎天下の行列に、日傘なしで30分以上並んだ人",
        "締め切った車内にいて、頭がボーッとなった人",
        "エアコンをつけずに寝て、夜中に頭が痛くなった人",
        "夏フェスで水を買い忘れてフラフラになった人",
        "水分を取らずに炎天下でBBQを楽しんだ人",
        "猛暑日に自転車通勤して、着いた時点でヘトヘトだった人",
        "サウナの後に水分補給を忘れて立ちくらみした人",
    ),
}
CORNERS = [0, 2, 6, 8]  # 四隅 = 子どものお題
CROSS = [1, 3, 4, 5, 7]  # 十字 = 大人のお題
LINES = [
    [0, 1, 2], [3, 4, 5], [6, 7, 8],
    [0, 3, 6], [1, 4, 7], [2, 5, 8],
    [0, 4, 8], [2, 4, 6],
]
# (キー, ラベル, 複数行かどうか)
FIELDS_CELL = [
    ("who", "👤 話した人（ニックネームでもOK）", False),
    ("issue", "⚠️ 課題", True),
    ("solution", "💡 解決策", True),
    ("thoughts", "💬 感想", True),
]
FIELDS_OWN = [
    ("issue", "⚠️ 課題", True),
    ("solution", "💡 解決策", True),
]


# ------------------------------------------------------------
# お題セットの保管庫
#   配布コード = 「お題セットの番号（1桁）」＋「カードの種（4桁）」
#   0 … 見本 / 1 … topics.json があればそのお題 / 2〜9 … 運営画面で保存したお題
#   保管するのは最大10セットだけなので、参加者が増えても、配り直しても、
#   古いコードが追い出されることはありません。
# ------------------------------------------------------------
def load_topics_file():
    """同じフォルダの topics.json があれば読む（再起動しても消えない固定お題）"""
    try:
        path = Path(__file__).with_name("topics.json")
        data = json.loads(path.read_text(encoding="utf-8"))
        c = [str(x).strip() for x in data["child"] if str(x).strip()]
        a = [str(x).strip() for x in data["adult"] if str(x).strip()]
        if len(c) == N_CHILD and len(a) == N_ADULT:
            return {"c": tuple(c), "a": tuple(a)}
    except Exception:
        pass
    return None


@st.cache_resource
def get_store():
    store = {"slots": {0: DEFAULT_TOPICS}, "order": [], "lock": threading.Lock()}
    fixed = load_topics_file()
    if fixed:
        store["slots"][1] = fixed
    return store


STORE = get_store()


def register_topics(topics):
    """運営が保存したお題を、空いている番号（2〜9）に入れる。満杯なら一番古いものと入れ替え"""
    with STORE["lock"]:
        free = [i for i in range(2, 10) if i not in STORE["slots"]]
        slot = free[0] if free else STORE["order"].pop(0)
        STORE["slots"][slot] = topics
        STORE["order"] = [s for s in STORE["order"] if s != slot] + [slot]
    return slot


def get_slot_topics(slot):
    with STORE["lock"]:
        return STORE["slots"].get(slot)


def secret(name):
    try:
        return st.secrets[name]
    except Exception:
        return None


# ------------------------------------------------------------
# 入力の整形
# ------------------------------------------------------------
def norm_code(s):
    """全角数字や前後の空白を直して、半角5桁の数字だけを通す"""
    s = unicodedata.normalize("NFKC", str(s or "")).strip()
    return s if re.fullmatch(r"[0-9]{5}", s) else None


def esc(s):
    """Markdownの記号（* _ ` ~ $ : など）が、表示を崩さないようにする"""
    return re.sub(r"([\\`*_~$\[\]<>:])", r"\\\1", str(s))


def make_code(slot):
    return f"{slot}{random.randint(0, 9999):04d}"


# ------------------------------------------------------------
# カードの作成
# ------------------------------------------------------------
def deal(code, role, topics):
    """5桁コードから、いつも同じカードとお題を作る（コード＝乱数の種）"""
    ch, ad = topics["c"], topics["a"]
    rng = random.Random(int(code))
    c = list(range(len(ch)))
    a = list(range(len(ad)))
    rng.shuffle(c)
    rng.shuffle(a)
    cells = {}
    for pos, i in zip(CORNERS, c[:4]):
        cells[pos] = ch[i]
    for pos, i in zip(CROSS, a[:5]):
        cells[pos] = ad[i]
    # 自分のお題は、カードに選ばれなかった残りのお題から選ぶ
    if role == CHILD:
        leftovers = [ch[i] for i in c[4:]]
    else:
        leftovers = [ad[i] for i in a[5:]]
    own = random.Random(f"{code}-{role}").choice(leftovers)
    return cells, own


# ------------------------------------------------------------
# 状態を変える関数（ボタンなどから呼ばれる）
# ------------------------------------------------------------
def sync_admin_fields(topics):
    st.session_state["in_adm_c"] = "\n".join(topics["c"])
    st.session_state["in_adm_a"] = "\n".join(topics["a"])


def apply_code(code):
    """正しい形のコードを受け取り、そのお題セットに切り替える。見つからなければ False"""
    slot = int(code[0])
    topics = get_slot_topics(slot)
    if topics is None:
        return False
    st.session_state.update(
        topics=topics, slot=slot, in_code=code, joined=True, missing=False, sel=None
    )
    sync_admin_fields(topics)
    return True


def join():
    code = norm_code(st.session_state.get("in_join"))
    if not code:
        st.session_state["join_msg"] = "5桁の数字で入力してください"
    elif not apply_code(code):
        st.session_state["join_msg"] = (
            "そのコードのお題が見つかりません。運営の人に確認してください"
            "（アプリが再起動した可能性もあります）"
        )
    else:
        st.session_state["join_msg"] = None


def play_sample():
    apply_code(make_code(0))


def new_code():
    """同じお題セットのまま、新しいカード（新しい種）にする"""
    st.session_state["in_code"] = make_code(st.session_state["slot"])
    st.session_state.update(missing=False, sel=None)


def load_code():
    """「配布コード」欄に入力されたとき"""
    st.session_state["sel"] = None
    code = norm_code(st.session_state.get("in_code"))
    if not code:
        return
    st.session_state["in_code"] = code  # 全角数字などを半角に直す
    if not apply_code(code):
        st.session_state["missing"] = True


def ask_clear():
    st.session_state["confirm_clear"] = True


def cancel_clear():
    st.session_state["confirm_clear"] = False


def clear_marks():
    for key in list(st.session_state.keys()):
        if key.startswith("in_m_") or key.startswith("in_c_"):
            del st.session_state[key]
    st.session_state.update(sel=None, bingo_count=0, confirm_clear=False)


def has_memo(mk):
    return any(
        st.session_state.get(f"in_m_{mk}_{f}", "").strip() for f, _, _ in FIELDS_CELL
    )


def is_on(code, pos):
    return bool(st.session_state.get(f"in_c_{code}_{pos}", False))


def select_cell(pos):
    """マスをタップ → そのマスのメモ欄を開く（もう一度タップすると閉じる）"""
    st.session_state["sel"] = None if st.session_state["sel"] == pos else pos


def close_panel():
    st.session_state["sel"] = None


def memo_changed(mk, chk_key):
    """メモを書いたら、自動でチェックを付ける"""
    if has_memo(mk):
        st.session_state[chk_key] = True


# ---- 運営用 ----
def try_unlock():
    now = time.time()
    if now < st.session_state["lock_until"]:
        wait = int(st.session_state["lock_until"] - now) + 1
        st.session_state["admin_msg"] = ("error", f"入力を間違えすぎました。{wait}秒後にもう一度どうぞ")
        return
    expected = str(secret("HOST_PASSWORD") or "")
    given = str(st.session_state.get("in_pw", ""))
    if expected and hmac.compare_digest(given.encode(), expected.encode()):
        st.session_state.update(admin_ok=True, admin_msg=None, fails=0, in_pw="")
    else:
        time.sleep(1)
        st.session_state["fails"] += 1
        if st.session_state["fails"] >= 5:
            st.session_state.update(lock_until=now + 60, fails=0)
        st.session_state["admin_msg"] = ("error", "パスワードが違います")


def lock_admin():
    st.session_state.update(admin_ok=False, admin_msg=None, in_pw="")


def parse_lines(text):
    return [s.strip() for s in (text or "").splitlines() if s.strip()]


def validate_topics(c, a):
    if len(c) != N_CHILD or len(a) != N_ADULT:
        return f"子どもは{N_CHILD}行（今{len(c)}行）、大人は{N_ADULT}行（今{len(a)}行）で入力してください"
    too_long = [t for t in c + a if len(t) > MAX_LEN]
    if too_long:
        return f"1つのお題は{MAX_LEN}文字以内にしてください：「{too_long[0][:15]}…」"
    if len(set(c + a)) != len(c + a):
        return "同じお題が2回入っています。重ならないようにしてください"
    return None


def save_topics():
    if not st.session_state.get("admin_ok"):
        st.session_state["admin_msg"] = ("error", "運営用のパスワードを入力してください")
        return
    c = parse_lines(st.session_state.get("in_adm_c"))
    a = parse_lines(st.session_state.get("in_adm_a"))
    err = validate_topics(c, a)
    if err:
        st.session_state["admin_msg"] = ("error", err)
        return
    topics = {"c": tuple(c), "a": tuple(a)}
    slot = register_topics(topics)
    code = make_code(slot)
    st.session_state.update(
        topics=topics, slot=slot, in_code=code, joined=True, missing=False, sel=None
    )
    st.session_state["admin_msg"] = (
        "success",
        f"お題を保存しました！配布コードは {code} です。"
        f"アプリのURLの最後に ?code={code} を付けると、参加者がコード入力なしで開けます",
    )


# ------------------------------------------------------------
# 表示用の部品
# ------------------------------------------------------------
def build_csv(code, cells, own, role):
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["マス", "お題", "チェック", "話した人", "課題", "解決策", "感想"])
    for pos in range(9):
        mk = f"{code}_{pos}"
        g = lambda f: st.session_state.get(f"in_m_{mk}_{f}", "")
        w.writerow(
            [pos + 1, cells[pos], "○" if is_on(code, pos) else "", g("who"), g("issue"), g("solution"), g("thoughts")]
        )
    g = lambda f: st.session_state.get(f"in_m_{code}_{role}_own_{f}", "")
    w.writerow(["自分のお題", own, "", "", g("issue"), g("solution"), ""])
    return buf.getvalue().encode("utf-8-sig")  # Excelでも文字化けしない


def memo_panel(code, pos, title):
    mk = f"{code}_{pos}"
    ck = f"in_c_{code}_{pos}"
    with st.container(border=True):
        st.markdown(f"**📝 {esc(title)}**")
        st.checkbox("✅ このお題に当てはまる人と話した（チェック）", key=ck)
        for f, label, multi in FIELDS_CELL:
            key = f"in_m_{mk}_{f}"
            if multi:
                st.text_area(label, key=key, height=90, on_change=memo_changed, args=(mk, ck))
            else:
                st.text_input(label, key=key, on_change=memo_changed, args=(mk, ck))
        st.button("✕ 閉じる", on_click=close_panel, **STRETCH)


def render_settings(code=None, cells=None, own=None, role=None):
    with st.expander("⚙️ コード・設定"):
        st.text_input(
            "配布コード（5桁の数字）", key="in_code", max_chars=5, on_change=load_code
        )
        st.button("🎲 配り直す（新しいカード）", on_click=new_code, **STRETCH)
        st.radio("あなたは", ["大人", CHILD], horizontal=True, key="in_role")
        if code:
            st.download_button(
                "💾 チェックとメモを保存（CSV）",
                data=build_csv(code, cells, own, role),
                file_name=f"熱中症ビンゴ_{code}.csv",
                mime="text/csv",
                **STRETCH_DL,
            )
        if st.session_state["confirm_clear"]:
            st.warning("チェックとメモを、すべて消します。よろしいですか？")
            c1, c2 = st.columns(2)
            c1.button("はい、消す", on_click=clear_marks, type="primary", **STRETCH)
            c2.button("やめる", on_click=cancel_clear, **STRETCH)
        else:
            st.button("🧹 チェックとメモを消す", on_click=ask_clear, **STRETCH)
        if st.session_state["missing"]:
            st.warning(
                "このコードのお題が見つかりません（アプリが再起動した可能性があります）。"
                "今のお題で表示しています。運営の人に確認してください。"
            )


def render_admin():
    with st.expander("🔒 運営用（お題の入力）"):
        if not secret("HOST_PASSWORD"):
            st.warning(
                "運営画面は無効です。Secretsに HOST_PASSWORD を設定すると使えます。"
                "（お題をファイルで固定したいときは topics.json を使います）"
            )
            return
        if not st.session_state["admin_ok"]:
            st.text_input("運営パスワード", type="password", key="in_pw")
            st.button("🔓 開く", on_click=try_unlock, **STRETCH)
        else:
            st.caption(
                f"お題を1行に1つずつ入力します。子どもは{N_CHILD}行、大人は{N_ADULT}行です。"
                f"1つは{MAX_LEN}文字以内、同じお題は重ねないでください。"
            )
            st.text_area(f"👦 子どものお題（{N_CHILD}個）", key="in_adm_c", height=180)
            st.text_area(f"🧑 大人のお題（{N_ADULT}個）", key="in_adm_a", height=210)
            st.button("💾 このお題でカードを作る", on_click=save_topics, type="primary", **STRETCH)
            st.button("🔒 運営画面を閉じる", on_click=lock_admin, **STRETCH)
        msg = st.session_state["admin_msg"]
        if msg:
            (st.error if msg[0] == "error" else st.success)(msg[1])


def render_join():
    st.markdown("#### 🎟 参加コードを入力してください")
    st.caption("運営の人から配られた5桁の数字を入れます。")
    st.text_input("参加コード（5桁の数字）", key="in_join", max_chars=5, placeholder="例：38274")
    st.button("▶ 参加する", on_click=join, type="primary", **STRETCH)
    if st.session_state.get("join_msg"):
        st.error(st.session_state["join_msg"])
    st.button("見本のお題で遊ぶ", on_click=play_sample, **STRETCH)


def sync_query(code, role):
    """コード・役割・チェックをURLに残す（リロードや画面ロックの後も復帰できる）"""
    try:
        bits = "".join("1" if is_on(code, p) else "0" for p in range(9))
        want = {"code": code, "role": "c" if role == CHILD else "a", "k": bits}
        for k, v in want.items():
            if st.query_params.get(k) != v:
                st.query_params[k] = v
    except Exception:
        pass


# ------------------------------------------------------------
# 状態の準備
# ------------------------------------------------------------
# 画面を再描画しても入力欄の中身が消えないようにする（"in_"で始まるキー）
for _k in list(st.session_state.keys()):
    if _k.startswith("in_"):
        st.session_state[_k] = st.session_state[_k]

_defaults = {
    "topics": DEFAULT_TOPICS,
    "slot": 0,
    "joined": False,
    "sel": None,
    "bingo_count": 0,
    "missing": False,
    "confirm_clear": False,
    "admin_ok": False,
    "admin_msg": None,
    "fails": 0,
    "lock_until": 0.0,
    "join_msg": None,
    "in_role": "大人",
    "in_code": make_code(0),
    "in_adm_c": "\n".join(DEFAULT_TOPICS["c"]),
    "in_adm_a": "\n".join(DEFAULT_TOPICS["a"]),
}
for _k, _v in _defaults.items():
    st.session_state.setdefault(_k, _v)

# 最初の1回だけ、URLの ?code=…&role=…&k=… から復帰する
if "init_done" not in st.session_state:
    st.session_state["init_done"] = True
    _qp = st.query_params
    if _qp.get("role") == "c":
        st.session_state["in_role"] = CHILD
    _c = norm_code(_qp.get("code"))
    if _c and apply_code(_c):
        _bits = str(_qp.get("k", ""))
        if re.fullmatch(r"[01]{9}", _bits):
            for _p, _b in enumerate(_bits):
                if _b == "1":
                    st.session_state[f"in_c_{_c}_{_p}"] = True

# ------------------------------------------------------------
# 画面
# ------------------------------------------------------------
st.markdown("### 🥵 熱中症ビンゴ")

# --- まだ参加していない：コード入力画面 ---
if not st.session_state["joined"]:
    render_join()
    render_admin()
    st.stop()

code = norm_code(st.session_state["in_code"])
role = st.session_state["in_role"]

# --- コードの形が正しくない ---
if code is None:
    st.warning("配布コードは5桁の数字で入力してください。")
    render_settings()
    render_admin()
    st.stop()

cells, own = deal(code, role, st.session_state["topics"])

# --- 上：自分のお題とメモ ---
own_filled = any(
    st.session_state.get(f"in_m_{code}_{role}_own_{f}", "").strip() for f, _, _ in FIELDS_OWN
)
st.info(f"⭐ あなたのお題：{esc(own)}")
if own_filled:
    st.caption("📝 自分のお題のメモを記入済みです")
with st.expander("📝 自分のお題のメモ（課題・解決策）"):
    for f, label, multi in FIELDS_OWN:
        st.text_area(label, key=f"in_m_{code}_{role}_own_{f}", height=90)

# --- ビンゴカード（マスをタップすると、その行の下にメモ欄が出る） ---
sel = st.session_state["sel"]
st.caption(f"配布コード：**{code}**　マスをタップするとメモ欄が開きます")
for row in range(3):
    with st.container(key=f"brow{row}"):
        cols = st.columns(3)
        for k in range(3):
            pos = row * 3 + k
            on = is_on(code, pos)
            label = ("✅ " if on else "") + esc(cells[pos])
            if has_memo(f"{code}_{pos}"):
                label += " 📝"
            if sel == pos:
                label = "👉 " + label
            cols[k].button(
                label,
                key=f"cell_{pos}",
                type="primary" if on else "secondary",
                on_click=select_cell,
                args=(pos,),
                **STRETCH,
            )
    if sel is not None and sel // 3 == row:
        memo_panel(code, sel, cells[sel])

# ---- ビンゴ判定 ----
done = [l for l in LINES if all(is_on(code, p) for p in l)]
if len(done) > st.session_state["bingo_count"]:
    st.balloons()
st.session_state["bingo_count"] = len(done)
if done:
    st.success(f"🎉 {len(done)}ビンゴ！！")
st.caption("💧 ゲーム中もこまめに水分補給＆休憩を忘れずに！")

# ---- 設定・運営用 ----
render_settings(code, cells, own, role)
render_admin()
sync_query(code, role)
