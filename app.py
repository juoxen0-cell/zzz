"""우리집 물건관리 | Python 3.11+ | streamlit run app.py
GitHub에 app.py와 requirements.txt 업로드 후 Streamlit Community Cloud 배포.
GitHub Pages는 지원하지 않습니다.
선택 Secrets: DATABASE_URL = "postgresql://USER:PASSWORD@HOST:5432/DB?sslmode=require"
외부 PostgreSQL 미설정 시 SQLite: Cloud 재시작/재배포 시 유실 가능.
로그인·가족 계정 기능 없음: 민감한 정보를 입력하지 말고 배포 접근 권한을 제한하세요.
목적별 세 메뉴: 공동구매 / 소진 예측 / 중복 구매 확인.
기존 버전과 별도 테이블을 사용하므로 이전 데이터가 자동 표시되지는 않습니다.
"""
from __future__ import annotations
import io
import re
import unicodedata
from urllib.parse import urlencode, urlsplit, urlunsplit
import os
import sqlite3
import statistics
import uuid
from contextlib import contextmanager
from datetime import datetime, date, timedelta
from zoneinfo import ZoneInfo

import streamlit as st
from PIL import Image, ImageOps

st.set_page_config(page_title="살림온 · 우리집 재고", page_icon="🏠", layout="centered")
TZ = ZoneInfo("Asia/Seoul")
CATEGORIES = ["생필품", "식품", "생활물품", "가전·필터", "기타"]


def today():
    return datetime.now(TZ).date()


def now():
    return datetime.now(TZ).isoformat(timespec="microseconds")


def config(key):
    try:
        return str(st.secrets.get(key, os.environ.get(key, "")))
    except (FileNotFoundError, st.errors.StreamlitSecretNotFoundError):
        return os.environ.get(key, "")


PG = bool(config("DATABASE_URL"))


@contextmanager
def db():
    if PG:
        import psycopg
        from psycopg.rows import dict_row
        conn = psycopg.connect(config("DATABASE_URL"), row_factory=dict_row, connect_timeout=12)
    else:
        conn = sqlite3.connect("home_inventory.sqlite3", timeout=20)
        conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def sql(conn, query, args=()):
    return conn.execute(query.replace("?", "%s") if PG else query, args)


def read(query, args=()):
    with db() as conn:
        return [dict(r) for r in sql(conn, query, args).fetchall()]


def initialize():
    blob = "BYTEA" if PG else "BLOB"
    with db() as conn:
        sql(conn, f"""CREATE TABLE IF NOT EXISTS simple_items(
            id TEXT PRIMARY KEY, name TEXT NOT NULL, aliases TEXT NOT NULL,
            category TEXT NOT NULL, location TEXT NOT NULL,
            qty INTEGER NOT NULL CHECK(qty>=0), unit TEXT NOT NULL,
            expiry TEXT, interval_days INTEGER NOT NULL,
            last_maintenance TEXT, memo TEXT NOT NULL, photo {blob})""")
        sql(conn, """CREATE TABLE IF NOT EXISTS purpose_orders(
            id TEXT PRIMARY KEY, item_id TEXT NOT NULL,
            qty INTEGER NOT NULL CHECK(qty>0), status TEXT NOT NULL,
            buyer TEXT NOT NULL, created TEXT NOT NULL)""")
        sql(conn, """CREATE TABLE IF NOT EXISTS item_thresholds(
            item_id TEXT PRIMARY KEY, threshold INTEGER NOT NULL CHECK(threshold>=0))""")
        sql(conn, """CREATE TABLE IF NOT EXISTS inventory_plans(
            item_id TEXT PRIMARY KEY, qty INTEGER NOT NULL CHECK(qty>=0))""")
        sql(conn, """CREATE TABLE IF NOT EXISTS simple_cycles(
            id TEXT PRIMARY KEY, item_id TEXT NOT NULL,
            started TEXT NOT NULL, finished TEXT)""")


def lock(conn, item_id):
    if not PG:
        sql(conn, "BEGIN IMMEDIATE")
    row = sql(conn, "SELECT * FROM simple_items WHERE id=?" + (" FOR UPDATE" if PG else ""), (item_id,)).fetchone()
    if not row:
        raise ValueError("물건을 찾을 수 없습니다. 다시 선택하세요.")
    return dict(row)


def change_stock(item_id, delta):
    with db() as conn:
        item = lock(conn, item_id)
        if sql(conn, "SELECT id FROM simple_cycles WHERE item_id=? AND finished IS NULL", (item_id,)).fetchone():
            raise ValueError("사용 중인 주기가 있어요. 소진 예측에서 완료하거나 취소한 뒤 수량을 변경하세요.")
        if item["qty"] + delta < 0:
            raise ValueError("현재 수량보다 많이 차감할 수 없습니다.")
        sql(conn, "UPDATE simple_items SET qty=qty+? WHERE id=?", (delta, item_id))


def start_cycle(item_id):
    with db() as conn:
        item = lock(conn, item_id)
        if item["qty"] < 1:
            raise ValueError("재고가 있어야 사용을 시작할 수 있어요.")
        if sql(conn, "SELECT id FROM simple_cycles WHERE item_id=? AND finished IS NULL", (item_id,)).fetchone():
            raise ValueError("이미 사용 중입니다.")
        sql(conn, "INSERT INTO simple_cycles VALUES(?,?,?,?)", (uuid.uuid4().hex, item_id, now(), None))


def end_cycle(item_id, cancel=False):
    with db() as conn:
        item = lock(conn, item_id)
        cycle = sql(conn, "SELECT * FROM simple_cycles WHERE item_id=? AND finished IS NULL", (item_id,)).fetchone()
        if not cycle:
            raise ValueError("사용 중인 주기가 없어요.")
        if cancel:
            sql(conn, "DELETE FROM simple_cycles WHERE id=?", (cycle["id"],))
        else:
            if item["qty"] < 1:
                raise ValueError("차감할 재고가 없어요.")
            sql(conn, "UPDATE simple_cycles SET finished=? WHERE id=?", (now(), cycle["id"]))
            sql(conn, "UPDATE simple_items SET qty=qty-1 WHERE id=?", (item_id,))


def predict(cycles):
    completed = sorted([c for c in cycles if c["finished"]], key=lambda c: c["finished"])
    durations = [(datetime.fromisoformat(c["finished"]).date() - datetime.fromisoformat(c["started"]).date()).days for c in completed]
    durations = [d for d in durations if d > 0]
    current = next((c for c in cycles if not c["finished"]), None)
    median = statistics.median(durations[-6:]) if len(durations) >= 2 else None
    due = datetime.fromisoformat(current["started"]).date() + timedelta(days=round(median)) if current and median is not None else None
    return len(durations), median, current, due


def navigate(page):
    if page == "소진 예측" or st.session_state.get("page") == "소진 예측":
        for key in ("predict_chosen", "predict_query", "predict_matches", "predict_clear_query"):
            st.session_state.pop(key, None)
    st.session_state["page"] = page


def perform(fn, message):
    try:
        fn()
    except ValueError as exc:
        st.error(str(exc))
    except Exception:
        st.error("저장하지 못했습니다. DB 연결이나 사진 형식을 확인하고 다시 시도하세요.")
    else:
        st.session_state["action_done"] = message
        st.rerun()


def choose(items, key):
    if not items:
        st.info("등록된 물건이 없어요. 홈의 ‘물건 등록’에서 먼저 등록하세요.")
        return None
    lookup = {i["id"]: i for i in items}
    options = list(lookup)
    if st.session_state.get(key) not in options:
        st.session_state.pop(key, None)
    iid = st.selectbox("물건 선택", options, index=None, placeholder=("필요한 물건을 입력해주세요" if key == "purchase_item" else "물건을 선택하세요"), format_func=lambda x: f'{lookup[x]["name"]} · {lookup[x]["location"]}', key=key)
    return lookup.get(iid)


def as_date(value):
    return date.fromisoformat(value) if value else None


@st.dialog("등록 완료", dismissible=False)
def registration_confirmation():
    st.success("✅ 물건이 등록됐어요!")
    st.write(f"**{st.session_state.get('registered_name', '')}** 등록을 완료했습니다.")
    more, home = st.columns(2)
    if more.button("더 등록하기", use_container_width=True):
        st.session_state["registration_generation"] = st.session_state.get("registration_generation", 0) + 1
        st.session_state.pop("registered_name", None)
        st.rerun()
    if home.button("홈화면으로 돌아가기", type="primary", use_container_width=True):
        st.session_state.pop("registered_name", None)
        navigate("홈")
        st.rerun()


@st.dialog("입력 내용을 확인해 주세요")
def registration_error(message):
    st.error(message)
    if st.button("다시 입력하기", use_container_width=True):
        st.rerun()


def show_registration(items):
    if "registered_name" in st.session_state:
        registration_confirmation()
        return
    generation = st.session_state.get("registration_generation", 0)
    with st.form(f"register_{generation}", clear_on_submit=False):
        st.markdown('<div class="required-label">물건 이름<span>* 필수</span></div>', unsafe_allow_html=True)
        name = st.text_input("물건 이름", key=f"reg_name_{generation}", max_chars=80, label_visibility="collapsed")
        st.markdown('<div class="required-label">보관 장소<span>* 필수</span></div>', unsafe_allow_html=True)
        location = st.text_input("보관 장소", key=f"reg_location_{generation}", placeholder="예: 다용도실 선반 두 번째 칸", max_chars=120, label_visibility="collapsed")
        st.markdown('<div class="required-label">현재 수량<span>* 필수</span></div>', unsafe_allow_html=True)
        qty_text = st.text_input("현재 수량", key=f"reg_qty_{generation}", value="", placeholder="숫자를 입력하세요. 예: 3", max_chars=4, label_visibility="collapsed")
        photo = st.file_uploader("사진 (선택, 2MB 이하)", key=f"reg_photo_{generation}", type=["jpg", "jpeg", "png"])
        memo = ""
        submit = st.form_submit_button("등록", use_container_width=True)
    st.caption("보관 장소에는 보관함 이름을 입력하세요. 예: 주방 2번 서랍. 찾기 쉬운 장소 이름으로 등록하세요.")
    if submit:
        def create():
            missing = []
            if not name.strip(): missing.append("물건 이름")
            if not location.strip(): missing.append("보관 장소")
            if not qty_text.strip(): missing.append("현재 수량")
            if missing:
                raise ValueError("입력하지 않은 항목: " + ", ".join(missing) + " — 필수 항목을 입력해 주세요.")
            if not re.fullmatch(r"[0-9]{1,4}", qty_text.strip()):
                raise ValueError("현재 수량은 0~9999 사이의 정수로 입력하세요.")
            qty = int(qty_text.strip())
            data = None
            if photo:
                if photo.size > 2 * 1024 * 1024:
                    raise ValueError("사진은 2MB 이하로 올려 주세요.")
                image = ImageOps.exif_transpose(Image.open(photo))
                image.thumbnail((1000, 1000))
                buffer = io.BytesIO()
                image.convert("RGB").save(buffer, format="JPEG", quality=80)
                data = buffer.getvalue()
            with db() as conn:
                if sql(conn, "SELECT id FROM simple_items WHERE LOWER(name)=LOWER(?) AND LOWER(location)=LOWER(?)", (name.strip(), location.strip())).fetchone():
                    raise ValueError("같은 장소에 같은 이름이 있어요. 기존 물건을 수정하거나 이름을 구분하세요.")
                sql(conn, "INSERT INTO simple_items VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", (uuid.uuid4().hex, name.strip(), "", "기타", location.strip(), qty, "개", None, 0, None, memo, data))
        try:
            create()
        except ValueError as exc:
            registration_error(str(exc))
        except Exception:
            registration_error("저장하지 못했습니다. DB 연결이나 사진 형식을 확인해 주세요.")
        else:
            st.session_state["registered_name"] = name.strip()
            registration_confirmation()


def pick_prediction_item(item_id):
    st.session_state["predict_chosen"] = item_id
    st.session_state["predict_query"] = ""


def clear_prediction_choice():
    st.session_state.pop("predict_chosen", None)


def stock_status(quantity, threshold):
    return "없음" if quantity == 0 else ("부족" if quantity <= threshold else "충분")


def threshold_for(item_id):
    rows = read("SELECT threshold FROM item_thresholds WHERE item_id=?", (item_id,))
    return int(rows[0]["threshold"]) if rows else 1


def save_threshold(item_id, threshold):
    if not isinstance(threshold, int) or not 0 <= threshold <= 9999:
        raise ValueError("부족 기준은 0~9999 사이의 정수로 입력하세요.")
    with db() as conn:
        lock(conn, item_id)
        sql(conn, "INSERT INTO item_thresholds(item_id,threshold) VALUES(?,?) ON CONFLICT(item_id) DO UPDATE SET threshold=excluded.threshold", (item_id, threshold))


def show_prediction(items):
    with st.container(key="prediction_entry"):
        st.markdown("### 먼저 물건 이름을 입력하세요")
        st.caption("이름을 입력한 뒤 검색 결과에서 확인할 물건을 눌러주세요.")
        query = st.text_input(
            "물건 이름", placeholder="예: 세제, 치약", key="predict_query",
            on_change=clear_prediction_choice)
    selected_id = st.session_state.get("predict_chosen")
    item = next((i for i in items if i["id"] == selected_id), None)
    candidates = [i for i in items if matches(i, query)] if query.strip() else []
    if query.strip():
        if not candidates:
            st.info("등록되지 않은 물건이에요. 먼저 물건을 등록해주세요.")
        else:
            st.caption("검색 결과에서 물건을 눌러주세요.")
            for candidate in candidates:
                st.button(candidate["name"] + " · " + candidate["location"],
                          key="predict_search_" + candidate["id"],
                          on_click=pick_prediction_item, args=(candidate["id"],),
                          use_container_width=True)
    if item is None:
        return
    st.subheader(item["name"])
    st.caption("선택한 물건 · " + item["location"])
    cycles = read("SELECT * FROM simple_cycles WHERE item_id=?", (item["id"],))
    count, median, current, due = predict(cycles)
    if not current and item["qty"] < 1:
        st.warning("이 물건은 현재 재고가 0개라 사용을 시작할 수 없어요.")
        st.write("실제로 가지고 있다면 아래에 추가할 수량을 입력하세요. 실제로 보유한 물건만 입력해주세요.")
        with st.form("prediction_restock"):
            amount = st.text_input("추가할 수량", placeholder="예: 1")
            submitted = st.form_submit_button("수량 반영하기", use_container_width=True)
        if submitted:
            if not amount.strip().isascii() or not amount.strip().isdigit() or int(amount.strip()) < 1:
                st.error("추가할 수량을 1 이상의 숫자로 입력해주세요.")
            else:
                perform(lambda: change_stock(item["id"], int(amount.strip())), "수량을 반영했어요. 이제 사용을 시작할 수 있어요.")
        return
    if median is not None:
        st.markdown("#### 우리 집 사용 속도")
        st.write(f"최근 유효한 사용 기록 {min(count, 6)}회를 보면 한 개를 보통 약 {median:g}일 사용했어요.")
        st.caption("튀는 기록의 영향을 줄이기 위해 평균 대신 최근 최대 6회의 중앙값을 사용해요. 사용 중인 1개 기준이에요.")
        if due:
            days_left = (due - today()).days
            if days_left > 0:
                st.info(f"현재 제품은 약 {days_left}일 더 사용할 것으로 예상돼요.")
            else:
                st.warning("예상 소진일에 도달했어요. 실제로 다 썼는지 확인해 주세요.")
        else:
            st.caption("사용을 시작하면 이번 제품의 예상 소진일까지 계산해요.")
    if due:
        st.success(f"다 쓸 것으로 예상되는 날: {due}")
    elif median is not None:
        st.info("사용 기록이 준비됐어요. 아래 버튼을 누르면 예상일을 알려드려요.")
    elif current:
        st.info("사용 기록을 모으고 있어요. 다 쓰면 아래 ‘다 썼어요’를 눌러주세요.")
    else:
        st.info("아직 소진을 예측할 데이터가 부족해요. 아래 버튼으로 사용 기록을 시작하세요.")
    if current:
        st.write(f'사용 시작일: {current["started"][:10]}')
        if st.button("다 썼어요", use_container_width=True):
            perform(lambda: end_cycle(item["id"]), "다 쓴 날을 기록했어요.")
        if st.button("사용 시작 취소", use_container_width=True):
            perform(lambda: end_cycle(item["id"], cancel=True), "사용 기록을 취소했어요.")
    elif st.button("예측 시작하기", use_container_width=True):
        perform(lambda: start_cycle(item["id"]), "사용 기록을 시작했어요. 다 쓰면 ‘다 썼어요’를 눌러주세요.")
    if median is None:
        st.caption("서로 다른 날짜에 사용을 시작하고 다 쓴 기록이 2번 쌓이면 예상일이 나와요. 같은 날 시작·완료한 기록은 계산에서 제외돼요.")


def normalize(text):
    return re.sub(r"[\W_]+", "", unicodedata.normalize("NFKC", text).casefold())


def matches(item, query):
    target = normalize(query)
    return bool(target) and any(target in normalize(v) for v in [item["name"]] + item["aliases"].replace("，", ",").split(","))


def add_order(item_id, quantity):
    with db() as conn:
        lock(conn, item_id)
        if sql(conn, "SELECT id FROM purpose_orders WHERE item_id=? AND status<>?", (item_id, "도착")).fetchone():
            raise ValueError("이미 구매 예정이거나 주문한 물건입니다. 기존 목록을 확인하세요.")
        sql(conn, "INSERT INTO purpose_orders VALUES(?,?,?,?,?,?)", (uuid.uuid4().hex, item_id, quantity, "구매 예정", "", now()))


def advance_order(order_id, buyer=""):
    with db() as conn:
        initial = sql(conn, "SELECT * FROM purpose_orders WHERE id=?", (order_id,)).fetchone()
        if not initial:
            raise ValueError("구매 항목이 없습니다.")
        lock(conn, initial["item_id"])
        order = dict(sql(conn, "SELECT * FROM purpose_orders WHERE id=?", (order_id,)).fetchone())
        if order["status"] == "구매 예정":
            if not buyer.strip():
                raise ValueError("구매 담당자의 별명을 입력하세요.")
            sql(conn, "UPDATE purpose_orders SET status=?,buyer=? WHERE id=?", ("주문 완료", buyer.strip(), order_id))
        elif order["status"] == "주문 완료":
            # This action is explicit; stale clicks never change a different step.
            raise ValueError("이미 주문됐어요. 최신 목록에서 도착 확인을 누르세요.")
        else:
            raise ValueError("이미 도착 처리된 항목입니다.")


def receive_order(order_id):
    with db() as conn:
        initial = sql(conn, "SELECT * FROM purpose_orders WHERE id=?", (order_id,)).fetchone()
        if not initial:
            raise ValueError("구매 항목이 없습니다.")
        lock(conn, initial["item_id"])
        order = dict(sql(conn, "SELECT * FROM purpose_orders WHERE id=?", (order_id,)).fetchone())
        if order["status"] != "주문 완료":
            raise ValueError("이미 처리됐거나 아직 주문 전입니다. 새로고침하세요.")
        sql(conn, "UPDATE purpose_orders SET status=? WHERE id=?", ("도착", order_id))
        sql(conn, "UPDATE simple_items SET qty=qty+? WHERE id=?", (order["qty"], order["item_id"]))


def show_duplicates(items):
    st.write("구매 전에 물건을 검색해 재고와 진행 중인 주문을 확인하세요.")
    with st.form("duplicate_search", clear_on_submit=True):
        query = st.text_input("구매하려는 물건", placeholder="물건 이름 입력")
        submitted = st.form_submit_button("확인", use_container_width=True)
    if submitted:
        if not query.strip():
            st.error("물건 이름을 입력하세요.")
            return
        found = [i for i in items if matches(i, query)]
        results = []
        if not found:
            results.append("등록되지 않은 물건입니다. 집에 없다는 뜻은 아니므로 먼저 확인해 주세요.")
        for item in found:
            results.append(f'물건: {item["name"]} · 보관 장소: {item["location"]}')
            results.append(f'현재 재고: {item["qty"]}개')
            if item["qty"] == 0:
                results.append("재고가 없어요. 구매가 필요해요.")
            elif item["qty"] <= threshold_for(item["id"]):
                results.append(f"⚠️ 재고 부족: {item['qty']}개 남았어요. 설정한 부족 기준 이하예요.")
            else:
                results.append("재고에 여유가 있어요. 추가 구매 전 확인하세요.")
            orders = read("SELECT * FROM purpose_orders WHERE item_id=? AND status<>?", (item["id"], "도착"))
            results.append("진행 중인 구매가 있어요." if orders else "진행 중인 구매가 없어요.")
            for order in orders:
                results.append(f'{order["status"]}: {order["qty"]}개 · 담당 {order["buyer"] or "미정"}')
        st.session_state["duplicate_result"] = results
        st.session_state["action_done"] = "중복 구매 확인을 완료했습니다."
        st.rerun()


@st.dialog("확인 완료", dismissible=False)
def action_confirmation():
    if "duplicate_result" not in st.session_state:
        st.success(st.session_state.get("action_done", "완료했습니다."))
    if "duplicate_result" in st.session_state:
        for entry in st.session_state["duplicate_result"]:
            if entry.startswith("⚠️"):
                st.warning(entry)
            elif entry.startswith("현재 재고:"):
                st.metric("지금 집에 있는 수량", entry.split(":", 1)[1].strip())
            elif entry == "진행 중인 구매가 있어요.":
                st.warning("🛒 진행 중인 구매가 있어요")
            elif entry == "진행 중인 구매가 없어요.":
                st.info("🛒 진행 중인 구매가 없어요")
            elif entry.startswith("물건:"):
                st.subheader(entry)
            elif "등록되지 않은" in entry:
                st.warning(entry)
            elif "여유가" in entry:
                st.success(entry)
            elif "구매가 필요" in entry:
                st.error(entry)
            else:
                st.write(entry)
    left, right = st.columns(2)
    if left.button("더 하기", use_container_width=True):
        st.session_state.pop("action_done", None)
        st.session_state.pop("duplicate_result", None)
        st.rerun()
    if right.button("홈화면으로 돌아가기", type="primary", use_container_width=True):
        st.session_state.pop("action_done", None)
        st.session_state.pop("duplicate_result", None)
        navigate("홈")
        st.rerun()



def remaining_estimate(cycles, stock):
    if stock == 0:
        return "재고 없음"
    _, median, current, due = predict(cycles)
    if median is None:
        return "예측 준비 중"
    if not current:
        return "사용 시작 전"
    elapsed = max(0, (today() - datetime.fromisoformat(current["started"]).date()).days)
    percent = round(max(0, min(100, (1 - elapsed / median) * 100)))
    days = max(0, (due - today()).days)
    return f"사용 중 약 {percent}% · 약 {days}일"


def save_inventory_plan(item_id, quantity):
    if quantity < 0:
        raise ValueError("0 이상의 수량을 입력하세요.")
    with db() as conn:
        lock(conn, item_id)
        sql(conn, "INSERT INTO inventory_plans(item_id,qty) VALUES(?,?) ON CONFLICT(item_id) DO UPDATE SET qty=excluded.qty", (item_id, quantity))


@st.dialog("물건별 부족 기준 설정")
def threshold_settings(items):
    st.caption("이 수량 이하가 되면 ‘부족’으로 표시해요. 설정하지 않은 물건은 1개 기준이에요.")
    lookup = {i["id"]: i for i in items}
    selected = st.selectbox("기준을 바꿀 물건", list(lookup), index=None,
                            format_func=lambda x: lookup[x]["name"] + " · " + lookup[x]["location"],
                            placeholder="물건을 선택하세요", key="threshold_item")
    if selected:
        with st.form("threshold_" + selected):
            raw = st.text_input("부족 기준 수량", value=str(threshold_for(selected)), help="예: 생수 6, 치약 1. 0으로 설정하면 재고가 없을 때만 알립니다.")
            submitted = st.form_submit_button("부족 기준 저장", use_container_width=True)
        if submitted:
            if not re.fullmatch(r"[0-9]{1,4}", raw.strip()):
                st.error("0~9999 사이의 정수를 입력해 주세요.")
            else:
                try:
                    save_threshold(selected, int(raw.strip()))
                except ValueError as exc:
                    st.error(str(exc))
                except Exception:
                    st.error("저장하지 못했어요. 연결을 확인하고 다시 시도해 주세요.")
                else:
                    st.session_state["threshold_saved_notice"] = True
                    st.rerun()


def show_overview(items):
    import html
    with st.container(key="inventory_tools"):
        refresh_col, settings_col = st.columns([1, 1])
        with refresh_col:
            st.button("새로고침", icon=":material/refresh:", key="inventory_refresh", on_click=lambda: None, use_container_width=True)
        with settings_col:
            if st.button("부족 기준", icon=":material/tune:", key="inventory_settings", disabled=not bool(items), use_container_width=True):
                threshold_settings(items)
    if st.session_state.pop("threshold_saved_notice", False):
        st.success("부족 기준을 저장했어요.")
    if not items:
        st.info("아직 등록된 물건이 없어요. 먼저 물건을 등록해 주세요.")
        return
    cycles = read("SELECT * FROM simple_cycles")
    plans = {r["item_id"]: r["qty"] for r in read("SELECT * FROM inventory_plans")}
    orders = read("SELECT item_id, SUM(qty) AS total FROM purpose_orders WHERE status<>? GROUP BY item_id", ("도착",))
    ordered = {r["item_id"]: int(r["total"]) for r in orders}
    for item in items:
        iid = item["id"]
        estimate = remaining_estimate([c for c in cycles if c["item_id"] == iid], item["qty"])
        threshold = threshold_for(iid)
        status = stock_status(item["qty"], threshold)
        color = "#b42318" if status != "충분" else "#16724c"
        planned = plans.get(iid, 0)
        active = ordered.get(iid, 0)
        purchase = f"{planned}개 구매 예정" if planned else "구매 예정 없음"
        if active:
            purchase += f" · 기존 주문 {active}개"
        st.markdown(f"""<div style="background:#fff;border:1px solid #e3e8f2;border-radius:18px;padding:17px;margin-bottom:12px;color:#17243b">
        <div style="font-size:18px;font-weight:750;overflow-wrap:anywhere">{html.escape(item['name'])}</div>
        <div style="display:flex;gap:12px;justify-content:space-between;flex-wrap:wrap;margin-top:10px">
        <span style="color:{color};font-size:19px;font-weight:750">재고 {item['qty']}개 · {status}</span>
        <span style="color:#7040ac;font-weight:650">{html.escape(estimate)}</span></div>
        <div style="color:#2356c8;background:#edf3ff;border-radius:9px;padding:8px;margin-top:10px;font-weight:650">{purchase}</div></div>""", unsafe_allow_html=True)


def bottom_navigation(page):
    links = [("홈", "홈", ":material/home:"), ("전체 재고", "재고", ":material/list_alt:"), ("소진 예측", "예측", ":material/query_stats:"), ("중복 구매 확인", "확인", ":material/inventory_2:")]
    with st.container(key="bottom_nav"):
        cols = st.columns(len(links))
        for col, (target, label, icon) in zip(cols, links):
            with col:
                st.button(label, key="nav_" + target, icon=icon, type="primary" if page == target else "secondary", on_click=navigate, args=(target,), use_container_width=True)


def main():
    st.markdown('<style>\n:root{color-scheme:light;--ink:#172c2b;--green:#087e72;}\n.stApp,[data-testid="stAppViewContainer"],[data-testid="stHeader"]{background:#f5f7f6;color:#172c2b;}\n.block-container{max-width:480px!important;padding:76px 22px 36px!important;}\nh1,h2,h3{color:#172c2b;letter-spacing:-1px;}h1{font-size:28px!important;}\np,label{letter-spacing:-.3px;}input,textarea{font-size:16px!important;}\n.brandline{display:flex;gap:10px;align-items:center;font-size:21px;font-weight:850;margin:0 0 28px;}\n.brandmark{display:grid;place-items:center;background:#087e72;color:white;border-radius:13px;width:38px;height:38px;font-size:26px;}\n.hero-title{font-size:29px;font-weight:800;line-height:1.4;letter-spacing:-1px;margin:8px 0 26px;}.eyebrow{color:#087e72;font-size:11px;letter-spacing:2px;font-weight:750;margin-bottom:10px;}\n.section-label{font-size:14px;color:#657a77;margin:26px 0 8px;font-weight:700;}\n[data-testid="stButton"] button,[data-testid="stFormSubmitButton"] button{min-height:52px;border-radius:15px;transition:none!important;touch-action:manipulation;}\n[data-testid="stButton"] button:not([kind="primary"]){background:#fff;color:#172c2b;border:1px solid #e0e8e5;}\nbutton[kind="primary"],[data-testid="stFormSubmitButton"] button,[data-testid="stFormSubmitButton"] button:hover,[data-testid="stFormSubmitButton"] button:focus{background:#087e72!important;color:white!important;border-color:#087e72!important;}\n[data-testid="stFormSubmitButton"] button p{color:white!important;}\n.stApp div.st-key-register_banner button,.stApp div.st-key-register_banner button:hover,.stApp div.st-key-register_banner button:focus{background:#c92f45!important;color:white!important;border:0!important;min-height:72px;border-radius:19px;width:100%;justify-content:flex-start;padding:18px;}\n.stApp div.st-key-register_banner button *{color:white!important;}\ndiv[class*="st-key-tile_"] button{width:100%;min-height:90px!important;justify-content:flex-start;padding:20px!important;border-radius:20px!important;gap:17px;box-shadow:0 3px 12px #183b3105;}\ndiv[class*="st-key-tile_"] button p{font-size:18px;font-weight:750;}\ndiv[class*="st-key-tile_"] [data-testid="stIconMaterial"]{font-size:28px;color:#087e72;background:#e5f3ee;padding:12px;border-radius:15px;box-sizing:content-box;}\n[data-testid="stForm"]{border:1px solid #e0e8e5;border-radius:20px;background:white;padding:18px;}\n[data-baseweb="input"],[data-baseweb="select"]>div{background:#fff;border-radius:12px;}[data-testid="stAlert"]{border-radius:15px;}\n[role="dialog"]{border-radius:24px!important;width:min(440px,calc(100vw - 24px))!important;max-height:85dvh;overflow-y:auto;background:white;}\n.welcome{padding:52px 0 30px;text-align:center;}.welcome-logo{width:100px;height:100px;display:grid;place-items:center;border-radius:30px;background:#087e72;color:white;font-size:67px;font-weight:800;margin:0 auto 28px;box-shadow:0 15px 35px #087e7220;}.welcome h1{font-size:43px!important;letter-spacing:-2px;margin:0;}.welcome p{color:#657a77;font-size:16px;line-height:1.9;margin:18px 0 28px;}.welcome-tag{font-size:11px;letter-spacing:3px;color:#087e72;font-weight:750;}\ndiv.st-key-start_app button{width:100%;min-height:60px;font-size:18px;border-radius:19px;}\n@media(max-width:520px){.block-container{padding-left:20px!important;padding-right:20px!important;}}\n.house-symbol{display:block;width:44px;height:49px;background:#fff;clip-path:polygon(50% 0,100% 40%,100% 100%,0 100%,0 40%);position:relative;flex:none;}.house-symbol::after{content:"";position:absolute;inset:11px 10px 9px;background:#087e72;clip-path:polygon(50% 0,100% 37%,100% 100%,0 100%,0 37%);}.welcome-logo{padding:0!important;line-height:1!important;}.required-label{font-size:14px;font-weight:600;margin:4px 0 6px;color:#172c2b;}.required-label span{color:#c6283d;margin-left:6px;font-size:12px;}\n/* Center the geometric mark on both axes without font-baseline effects. */\n.welcome-logo{position:relative!important;display:block!important;width:100px!important;height:100px!important;margin:0 auto 28px!important;padding:0!important;transform:none!important;}\n.welcome-logo .house-symbol{position:absolute!important;left:50%!important;top:50%!important;transform:translate(-50%,-50%)!important;margin:0!important;width:44px!important;height:49px!important;}\n\n.welcome{display:flex!important;flex-direction:column!important;align-items:center!important;text-align:center!important;width:100%!important;}\n.welcome h1,.welcome p,.welcome-tag{width:100%!important;text-align:center!important;margin-left:auto!important;margin-right:auto!important;}\n.welcome h1{padding:0!important;text-indent:0!important;letter-spacing:0!important;}\n.welcome-logo .house-symbol{clip-path:none!important;background:none!important;width:48px!important;height:48px!important;}\n.welcome-logo .house-symbol::after{inset:0!important;background:white!important;clip-path:polygon(50% 5%,95% 43%,85% 55%,80% 51%,80% 94%,58% 94%,58% 68%,42% 68%,42% 94%,20% 94%,20% 51%,15% 55%,5% 43%)!important;}\ndiv.st-key-predict_query [data-baseweb="input"]{background:#e9f6ff!important;border:2px solid #2675d8!important;}\n[data-testid="stExpander"]{background:#f0f1f2;border:1px solid #e0e3e4;border-radius:14px;}\n[data-testid="stExpander"] summary{color:#63716e!important;font-size:13px!important;}\n\ndiv.st-key-prediction_entry{background:#e8f1ff!important;border:2px solid #2563eb!important;border-radius:20px;padding:20px!important;margin:4px 0 22px!important;}\ndiv.st-key-prediction_entry h3{color:#1746aa!important;font-size:22px!important;letter-spacing:-.5px;}\ndiv.st-key-prediction_entry [data-baseweb="input"],div.st-key-prediction_entry [data-baseweb="input"]:focus-within{background:#fff!important;border:2px solid #2563eb!important;box-shadow:0 0 0 3px #2563eb16!important;border-radius:12px!important;}\ndiv.st-key-prediction_entry input{background:#fff!important;color:#152c56!important;caret-color:#2563eb;min-height:48px;}\ndiv.st-key-prediction_entry input::placeholder{color:#617999!important;opacity:1;}\n[data-testid="stExpander"]{background:#f0f1f2!important;border:1px solid #dfe2e4!important;box-shadow:none!important;}\n\n/* Optical alignment requested: shift logo only, leave title and layout intact. */\n.welcome .welcome-logo{left:-6px!important;position:relative!important;}\n\ndiv.st-key-inventory_tools [data-testid="stHorizontalBlock"]{flex-wrap:nowrap!important;gap:10px!important;}\ndiv.st-key-inventory_tools [data-testid="stColumn"]{min-width:0!important;flex:1 1 0!important;}\ndiv.st-key-inventory_tools button{min-height:40px!important;padding:6px 10px!important;border-radius:12px!important;}\ndiv.st-key-inventory_tools button p{font-size:13px!important;}\n</style>', unsafe_allow_html=True)
    if not st.session_state.get("entered_app", False):
        st.markdown('<div class="welcome"><div class="welcome-logo"><span class="house-symbol"></span></div><div class="welcome-tag">SALLIM ON</div><h1>살림온</h1><p>우리집 물건을 한눈에.<br>기억 대신, 살림온.</p></div>', unsafe_allow_html=True)
        with st.container(key="start_app"):
            if st.button("시작하기", type="primary", icon=":material/arrow_forward:", use_container_width=True):
                st.session_state["entered_app"] = True
                st.session_state["page"] = "홈"
                st.rerun()
        return
    try:
        initialize()
        items = read("SELECT * FROM simple_items ORDER BY name,location")
    except Exception:
        st.error("DB에 연결할 수 없습니다. DATABASE_URL과 접속 설정을 확인하세요.")
        return
    page = st.session_state.get("page", "홈")
    if page == "공동구매":
        page = "홈"
        st.session_state["page"] = "홈"
    st.markdown('<div class="brandline"><span class="brandmark">⌂</span> 살림온 <span style="font-weight:500;color:#718681;font-size:11px;margin-left:auto;letter-spacing:1px">SALLIM ON</span></div>', unsafe_allow_html=True)
    if page == "홈":
        st.markdown('<div class="eyebrow">MY HOME INVENTORY</div><div class="hero-title">있는 건 챙기고,<br>없는 건 채우고.</div>', unsafe_allow_html=True)
        with st.container(key="register_banner"):
            st.button("＋ 물건 등록하기", key="home_register", on_click=navigate, args=("물건 등록",), icon=":material/add_circle:", use_container_width=True)
        st.caption("새 물건은 먼저 등록하세요 · 이름 / 사진 / 보관 장소 / 수량")
        st.markdown('<div class="section-label">무엇을 도와드릴까요?</div>', unsafe_allow_html=True)
        tiles = [("duplicate_overview","전체 재고","전체 재고",":material/list_alt:"),("predict","소진 예측","소진 예측",":material/query_stats:"),("duplicate","중복 구매 확인","중복 구매 확인",":material/inventory_2:")]
        for key, text, target, icon in tiles:
            with st.container(key="tile_" + key):
                st.button(text, key="go_" + key, on_click=navigate, args=(target,), icon=icon, use_container_width=True)



        return
    st.button("홈으로", icon=":material/arrow_back:", on_click=navigate, args=("홈",))
    st.title(page)
    if st.button("새로고침", icon=":material/refresh:"):
        st.rerun()
    if "notice" in st.session_state:
        st.success(st.session_state.pop("notice"))
    handlers = {"전체 재고":show_overview,"소진 예측":show_prediction,"중복 구매 확인":show_duplicates,"물건 등록":show_registration}
    if page in handlers:
        handlers[page](items)
        if "action_done" in st.session_state:
            action_confirmation()
    else:
        navigate("홈")
        st.rerun()


if __name__ == "__main__":
    main()
