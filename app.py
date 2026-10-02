"""목표 자금 달성 시뮬레이터 (Streamlit 버전).

index.html의 계산 로직(simulate)을 그대로 옮기고, 화면은 Streamlit 위젯으로 구성한다.
실행: streamlit run app.py
저장: localStorage 대신 같은 폴더의 goal_fund_state.json (구조는 PRD 8장과 동일).
"""

from __future__ import annotations

import copy
import csv
import io
import json
import math
import random
import string
import time
from datetime import date, datetime
from pathlib import Path

import altair as alt
import pandas as pd
import streamlit as st

# ==== CALC START ====
SCHEMA_VERSION = 1
MAX_PROJECTION_MONTHS = 600  # 예상 달성 시점 탐색 한도 (50년)
ACTUAL_FIELDS = ("saving", "income", "extra")


def is_num(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def month_to_index(ym: str) -> int:
    y, m = (int(x) for x in ym.split("-"))
    return y * 12 + (m - 1)


def index_to_month(idx: int) -> str:
    return f"{idx // 12}-{idx % 12 + 1:02d}"


def income_for_month(settings: dict, month_number: int) -> float:
    if settings["annualIncomeMode"] == "split":
        return settings["annualIncome"] / 12
    return settings["annualIncome"] if month_number == settings["annualIncomeMonth"] else 0


# PRD 5장: 매월 ① 이자 → ② 월 저축 → ③ 정기수입
def step_month(balance: float, idx: int, settings: dict, r: float, saving: float):
    interest = balance * r
    income = income_for_month(settings, idx % 12 + 1)
    return interest, income, balance + interest + saving + income


def required_extra(shortfall: float, r: float, n: int) -> float:
    return shortfall / n if r == 0 else shortfall * r / ((1 + r) ** n - 1)


def lump_sum_for(shortfall: float, r: float, n: int) -> float:
    return shortfall if r == 0 else shortfall / (1 + r) ** n


# 부족해지지 않게 올림. 부동소수 오차로 1원이 더 붙지 않도록 미세값을 뺀다
def round_up(x: float, to_manwon: bool) -> float:
    return math.ceil(x / 1e4 - 1e-9) * 1e4 if to_manwon else math.ceil(x - 1e-6)


# 그 달의 계획 저축: 월별 조정값이 있으면 조정값, 없으면 기본 월 저축액
def planned_saving(settings: dict, plan: dict, idx: int) -> float:
    v = plan.get(index_to_month(idx))
    return v if is_num(v) else settings["monthlySaving"]


# 목표 도달 월 인덱스. 시작 전에 이미 도달했으면 from_idx - 1, 한도 내 불가면 None
def find_reach(balance, from_idx, settings, r, goal, plan=None):
    plan = plan or {}
    if balance >= goal:
        return from_idx - 1
    for t in range(MAX_PROJECTION_MONTHS):
        balance = step_month(balance, from_idx + t, settings, r, planned_saving(settings, plan, from_idx + t))[2]
        if balance >= goal:
            return from_idx + t
    return None


def has_actual_value(entry) -> bool:
    return bool(entry) and any(is_num(entry.get(f)) for f in ACTUAL_FIELDS)


# plan: 월별 계획 저축 조정값 { 'YYYY-MM': 금액 }
def simulate(settings: dict, actuals: dict | None = None, plan: dict | None = None) -> dict:
    actuals = actuals or {}
    plan = plan or {}
    start_idx = month_to_index(settings["startMonth"])
    goal_idx = month_to_index(settings["goalMonth"])
    n = goal_idx - start_idx + 1  # 시작 월과 목표 월 모두 포함
    if n < 1:
        return {"error": "목표 시점이 시작 시점보다 앞입니다."}

    r = settings["annualReturnRate"] / 100 / 12
    goal = settings["goalAmount"]
    to_man = bool(settings["roundExtraToManwon"])

    # 계획
    rows = []
    balance = settings["currentFunds"]
    for t in range(n):
        idx = start_idx + t
        month = index_to_month(idx)
        saving = planned_saving(settings, plan, idx)
        interest, income, balance = step_month(balance, idx, settings, r, saving)
        rows.append({
            "month": month,
            "saving": saving,
            "adjusted": is_num(plan.get(month)),
            "income": income,
            "interest": interest,
            "balance": balance,
            "actualBalance": None,
            "diff": None,
            "hasActual": False,
            "memo": (actuals.get(month) or {}).get("memo", ""),
        })

    projected = balance
    shortfall = goal - projected
    extra_monthly = round_up(required_extra(shortfall, r, n), to_man) if shortfall > 0 else 0
    lump_sum = round_up(lump_sum_for(shortfall, r, n), to_man) if shortfall > 0 else 0
    reach_idx = find_reach(settings["currentFunds"], start_idx, settings, r, goal, plan)

    # 실적 (PRD 5.6)
    last_actual_idx = None
    for month, entry in actuals.items():
        if not has_actual_value(entry):
            continue
        idx = month_to_index(month)
        if idx < start_idx or idx > goal_idx:
            continue
        if last_actual_idx is None or idx > last_actual_idx:
            last_actual_idx = idx

    actual = None
    boost = None
    if last_actual_idx is not None:
        k = last_actual_idx - start_idx + 1
        ab = settings["currentFunds"]
        for t in range(k):
            idx = start_idx + t
            entry = actuals.get(rows[t]["month"]) or {}
            saving = entry["saving"] if is_num(entry.get("saving")) else rows[t]["saving"]
            income = entry["income"] if is_num(entry.get("income")) else income_for_month(settings, idx % 12 + 1)
            extra = entry["extra"] if is_num(entry.get("extra")) else 0
            ab += ab * r + saving + income + extra
            rows[t]["actualBalance"] = ab
            rows[t]["diff"] = ab - rows[t]["balance"]
            rows[t]["hasActual"] = has_actual_value(entry)

        remaining = goal_idx - last_actual_idx
        pb = ab
        for i in range(1, remaining + 1):
            pb = step_month(pb, last_actual_idx + i, settings, r, planned_saving(settings, plan, last_actual_idx + i))[2]
        a_shortfall = goal - pb
        a_extra = round_up(required_extra(a_shortfall, r, remaining), to_man) if a_shortfall > 0 and remaining > 0 else 0
        a_reach_idx = find_reach(ab, last_actual_idx + 1, settings, r, goal, plan)

        actual = {
            "lastMonth": index_to_month(last_actual_idx),
            "balance": ab,
            "diff": ab - rows[k - 1]["balance"],
            "remainingMonths": remaining,
            "projected": pb,
            "shortfall": a_shortfall,
            "achievable": a_shortfall <= 0,
            "extraMonthly": a_extra,
            "requiredMonthlyTotal": settings["monthlySaving"] + a_extra,
            "reachedMonth": None if a_reach_idx is None else index_to_month(a_reach_idx),
            "alreadyReached": ab >= goal,
        }

        if a_extra > 0:
            boost = [None] * n
            boost[k - 1] = ab
            b = ab
            for t in range(k, n):
                b = step_month(b, start_idx + t, settings, r, rows[t]["saving"] + a_extra)[2]
                boost[t] = b
    elif extra_monthly > 0:
        boost = []
        b = settings["currentFunds"]
        for t in range(n):
            b = step_month(b, start_idx + t, settings, r, rows[t]["saving"] + extra_monthly)[2]
            boost.append(b)

    for row in rows:
        b = row["actualBalance"] if row["actualBalance"] is not None else row["balance"]
        row["goalPct"] = b / goal if goal > 0 else 0

    latest_balance = actual["balance"] if actual else settings["currentFunds"]
    return {
        "rows": rows,
        "boost": boost,
        "actual": actual,
        "summary": {
            "months": n,
            "achievable": shortfall <= 0,
            "projected": projected,
            "shortfall": shortfall,
            "extraMonthly": extra_monthly,
            "requiredMonthlyTotal": settings["monthlySaving"] + extra_monthly,
            "lumpSum": lump_sum,
            "adjustedMonths": sum(1 for row in rows if row["adjusted"]),
            "reachedMonth": None if reach_idx is None else index_to_month(reach_idx),
            "alreadyReached": reach_idx == start_idx - 1,
            "latestBalance": latest_balance,
            "progress": latest_balance / goal if goal > 0 else 0,
            "goalReached": goal > 0 and latest_balance >= goal,
        },
    }
# ==== CALC END ====

STATE_FILE = Path(__file__).with_name("goal_fund_state.json")
MAX_SCENARIOS = 5
TABS = ("table", "actuals", "scenarios")
YEAR_OPTIONS = list(range(1900, 2201))


def is_month(v) -> bool:
    if not isinstance(v, str) or len(v) != 7 or v[4] != "-":
        return False
    y, m = v[:4], v[5:]
    return y.isdigit() and m.isdigit() and 1 <= int(m) <= 12


def current_month() -> str:
    d = date.today()
    return f"{d.year}-{d.month:02d}"


def default_settings() -> dict:
    return {
        "goalAmount": 200000000,
        "goalMonth": "2030-08",
        "startMonth": current_month(),
        "currentFunds": 0,
        "monthlySaving": 1800000,
        "annualIncome": 10000000,
        "annualIncomeMonth": 12,
        "annualIncomeMode": "lump",
        "annualReturnRate": 0,
        "displayUnit": "won",
        "roundExtraToManwon": False,
    }


def default_state() -> dict:
    return {
        "schemaVersion": SCHEMA_VERSION,
        "updatedAt": None,
        "settings": default_settings(),
        "actuals": {},
        "planOverrides": {},  # 월별 계획 저축 조정값 { 'YYYY-MM': 금액 }. 없는 달은 기본 월 저축액
        "scenarios": [],
        "ui": {"activeTab": "table", "collapsedYears": []},
    }


def new_id() -> str:
    return "s" + format(int(time.time() * 1000), "x") + "".join(random.choices(string.ascii_lowercase + string.digits, k=4))


# 저장된 값이나 가져온 파일을 신뢰하지 않고 항목별로 검증해서 기본값과 합친다
def normalize_settings(raw) -> dict:
    s = default_settings()
    if not isinstance(raw, dict):
        return s
    for k in ("goalAmount", "currentFunds", "monthlySaving", "annualIncome"):
        if is_num(raw.get(k)) and raw[k] >= 0:
            s[k] = raw[k]
    for k in ("goalMonth", "startMonth"):
        if is_month(raw.get(k)):
            s[k] = raw[k]
    m = raw.get("annualIncomeMonth")
    if isinstance(m, int) and not isinstance(m, bool) and 1 <= m <= 12:
        s["annualIncomeMonth"] = m
    if raw.get("annualIncomeMode") in ("lump", "split"):
        s["annualIncomeMode"] = raw["annualIncomeMode"]
    if is_num(raw.get("annualReturnRate")) and 0 <= raw["annualReturnRate"] <= 100:
        s["annualReturnRate"] = raw["annualReturnRate"]
    if raw.get("displayUnit") in ("won", "man"):
        s["displayUnit"] = raw["displayUnit"]
    if isinstance(raw.get("roundExtraToManwon"), bool):
        s["roundExtraToManwon"] = raw["roundExtraToManwon"]
    return s


def normalize_actuals(raw) -> dict:
    out = {}
    if not isinstance(raw, dict):
        return out
    for month, v in raw.items():
        if not is_month(month) or not isinstance(v, dict):
            continue
        entry = {}
        if is_num(v.get("saving")) and v["saving"] >= 0:
            entry["saving"] = v["saving"]
        if is_num(v.get("income")) and v["income"] >= 0:
            entry["income"] = v["income"]
        if is_num(v.get("extra")):
            entry["extra"] = v["extra"]
        if isinstance(v.get("memo"), str) and v["memo"]:
            entry["memo"] = v["memo"][:200]
        if entry:
            out[month] = entry
    return out


def normalize_plan(raw) -> dict:
    if not isinstance(raw, dict):
        return {}
    return {m: v for m, v in raw.items() if is_month(m) and is_num(v) and v >= 0}


# planOverrides는 schemaVersion 1에 나중에 더한 선택 항목이라, 없는 예전 데이터는 빈 값으로 읽는다
def normalize_state(raw) -> dict:
    s = default_state()
    if not isinstance(raw, dict):
        return s
    s["settings"] = normalize_settings(raw.get("settings"))
    s["actuals"] = normalize_actuals(raw.get("actuals"))
    s["planOverrides"] = normalize_plan(raw.get("planOverrides"))
    if isinstance(raw.get("scenarios"), list):
        items = [x for x in raw["scenarios"] if isinstance(x, dict)][:MAX_SCENARIOS]
        s["scenarios"] = [{
            "id": x["id"] if isinstance(x.get("id"), str) and x["id"] else new_id(),
            "name": x["name"].strip()[:30] if isinstance(x.get("name"), str) and x["name"].strip() else f"시나리오 {i + 1}",
            "settings": normalize_settings(x.get("settings")),
            "planOverrides": normalize_plan(x.get("planOverrides")),
            "savedAt": x["savedAt"] if isinstance(x.get("savedAt"), str) else None,
        } for i, x in enumerate(items)]
    ui = raw.get("ui") if isinstance(raw.get("ui"), dict) else {}
    if ui.get("activeTab") in TABS:
        s["ui"]["activeTab"] = ui["activeTab"]
    if isinstance(ui.get("collapsedYears"), list):
        s["ui"]["collapsedYears"] = [y for y in ui["collapsedYears"] if isinstance(y, int)]
    s["updatedAt"] = raw["updatedAt"] if isinstance(raw.get("updatedAt"), str) else None
    return s


def load_state() -> tuple[dict, bool]:
    try:
        if STATE_FILE.exists():
            return normalize_state(json.loads(STATE_FILE.read_text(encoding="utf-8"))), True
        return default_state(), True
    except (OSError, ValueError):
        return default_state(), False


def state_fingerprint(state: dict) -> str:
    return json.dumps({k: v for k, v in state.items() if k != "updatedAt"}, ensure_ascii=False, sort_keys=True)


def save_state_if_changed(state: dict) -> None:
    fp = state_fingerprint(state)
    if fp == st.session_state.get("saved_fp"):
        return
    state["updatedAt"] = datetime.now().astimezone().isoformat(timespec="seconds")
    try:
        STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
        st.session_state.storage_ok = True
    except OSError:
        st.session_state.storage_ok = False
    st.session_state.saved_fp = fp


# ---- 포맷 ----
def js_round(x: float) -> int:
    return math.floor(x + 0.5)


def format_won(n: float) -> str:
    return f"{js_round(n):,}원"


def format_man(n: float) -> str:
    v = round(n / 1e4, 1)
    text = f"{v:,.1f}"
    return (text[:-2] if text.endswith(".0") else text) + "만 원"


def format_korean(n: float) -> str:
    sign = "−" if n < 0 else ""
    abs_v = js_round(abs(n))
    # 만 원 단위로 먼저 반올림해야 "1억 10,000만"처럼 자리올림이 빠지지 않음
    total_man = js_round(abs_v / 1e4)
    if total_man == 0:
        return "0원" if abs_v == 0 else f"{sign}{abs_v:,}원"
    jo = total_man // 10**8
    eok = (total_man % 10**8) // 10**4
    man = total_man % 10**4
    parts = []
    if jo:
        parts.append(f"{jo:,}조")
    if eok:
        parts.append(f"{eok:,}억")
    if man:
        parts.append(f"{man:,}만")
    return sign + " ".join(parts) + " 원"


def S() -> dict:
    return st.session_state.app_state


# 선택한 표시 단위로 금액 표시
def fmt(n: float) -> str:
    return format_man(n) if S()["settings"]["displayUnit"] == "man" else format_won(n)


# 카드 보조 표기: 주 표기와 다른 형식
def fmt_sub(n: float) -> str:
    return format_won(n) if S()["settings"]["displayUnit"] == "man" else format_korean(n)


def fmt_signed(n: float) -> str:
    if js_round(n) == 0:
        return fmt(0)
    return ("+" if n > 0 else "−") + fmt(abs(n))


def format_month(ym: str) -> str:
    y, m = ym.split("-")
    return f"{y}년 {int(m)}월"


def format_period(n: int) -> str:
    y, m = divmod(n, 12)
    parts = []
    if y:
        parts.append(f"{y}년")
    if m or not y:
        parts.append(f"{m}개월")
    return " ".join(parts)


def today_stamp() -> str:
    return date.today().strftime("%Y%m%d")


def reach_text(month, already_reached) -> tuple[str, str]:
    if already_reached:
        return "이미 달성", ""
    if not month:
        return "50년 내 달성 불가", ""
    in_time = month_to_index(month) <= month_to_index(S()["settings"]["goalMonth"])
    return format_month(month), "기한 내" if in_time else "기한 이후"


def range_months() -> list[str]:
    s = S()["settings"]
    return [index_to_month(i) for i in range(month_to_index(s["startMonth"]), month_to_index(s["goalMonth"]) + 1)]


def adjusted_month_count() -> int:
    plan = S()["planOverrides"]
    return sum(1 for m in range_months() if is_num(plan.get(m)))


# ---- 위젯 ↔ 상태 ----
MONEY_KEYS = ("goalAmount", "currentFunds", "monthlySaving", "annualIncome")


def sync_widgets_from_state() -> None:
    """불러오기·가져오기·초기화처럼 상태를 통째로 바꿀 때 입력 위젯 값을 맞춘다."""
    ss = st.session_state
    s = S()["settings"]
    for k in MONEY_KEYS:
        ss[f"w_{k}"] = int(s[k])
    for k in ("startMonth", "goalMonth"):
        y, m = s[k].split("-")
        ss[f"w_{k}_y"] = int(y)
        ss[f"w_{k}_m"] = int(m)
    ss.w_annualIncomeMode = s["annualIncomeMode"]
    ss.w_annualIncomeMonth = s["annualIncomeMonth"]
    ss.w_annualReturnRate = float(s["annualReturnRate"])
    ss.w_displayUnit = s["displayUnit"]
    ss.w_roundExtraToManwon = s["roundExtraToManwon"]


def reset_editor() -> None:
    """월별 계획·실적 편집기를 상태에서 새로 만든다 (편집 중이던 변경은 먼저 반영)."""
    merge_editor_edits()
    ss = st.session_state
    base = ss.get("editor_base")
    if base and base["key"] in ss:
        del ss[base["key"]]
    ss.editor_base = None
    ss.editor_ver = ss.get("editor_ver", 0) + 1


def replace_state(new_state: dict) -> None:
    ss = st.session_state
    base = ss.get("editor_base")
    if base and base["key"] in ss:
        del ss[base["key"]]  # 이전 상태 기준의 편집 내용이 새 상태에 덮어쓰이지 않게 버림
    ss.editor_base = None
    ss.editor_ver = ss.get("editor_ver", 0) + 1
    ss.app_state = new_state
    sync_widgets_from_state()


# ---- 월별 계획·실적 편집기 ----
COL_PLAN, COL_SAVING, COL_INCOME, COL_EXTRA, COL_MEMO = "계획 저축", "실제 저축", "정기수입", "기타 수입/지출", "메모"
COL_FIELD = {COL_PLAN: "plan", COL_SAVING: "saving", COL_INCOME: "income", COL_EXTRA: "extra", COL_MEMO: "memo"}


def set_actual_field(month: str, field: str, value) -> None:
    actuals = S()["actuals"]
    entry = dict(actuals.get(month) or {})
    if value is None or value == "":
        entry.pop(field, None)
    else:
        entry[field] = value
    if entry:
        actuals[month] = entry
    else:
        actuals.pop(month, None)


# 빈 값(None)이면 조정을 지워 기본 월 저축액을 따르게 함
def set_plan_override(month: str, value) -> None:
    if value is None:
        S()["planOverrides"].pop(month, None)
    else:
        S()["planOverrides"][month] = value


def clean_money(v, signed=False):
    """편집기 값 → 원 단위 정수. 빈 값은 None. 소수점 이하는 버림."""
    if v is None or (isinstance(v, float) and math.isnan(v)):
        return None
    try:
        v = int(float(v))
    except (TypeError, ValueError):
        return None
    return v if signed else max(v, 0)


def merge_editor_edits() -> None:
    """편집기에서 고친 칸을 상태에 반영. 기준표 대비 누적 변경이라 여러 번 적용해도 같다."""
    ss = st.session_state
    base = ss.get("editor_base")
    if not base:
        return
    edits = ss.get(base["key"])
    if not edits:
        return
    for ri, changes in edits.get("edited_rows", {}).items():
        ri = int(ri)
        if ri >= len(base["months"]):
            continue
        month = base["months"][ri]
        for col, val in changes.items():
            field = COL_FIELD.get(col)
            if field == "plan":
                set_plan_override(month, clean_money(val))
            elif field == "memo":
                set_actual_field(month, "memo", (val or "")[:200] if isinstance(val, str) else None)
            elif field:
                set_actual_field(month, field, clean_money(val, signed=field == "extra"))


def editor_base() -> dict:
    ss = st.session_state
    s = S()["settings"]
    sig = f"{s['startMonth']}_{s['goalMonth']}_{ss.get('editor_ver', 0)}"
    base = ss.get("editor_base")
    if base and base["sig"] == sig:
        return base
    months = range_months()
    now = current_month()
    actuals, plan = S()["actuals"], S()["planOverrides"]
    records = []
    for m in months:
        e = actuals.get(m) or {}
        records.append({
            "연월": format_month(m) + (" (이번 달)" if m == now else ""),
            COL_PLAN: plan.get(m),
            COL_SAVING: e.get("saving"),
            COL_INCOME: e.get("income"),
            COL_EXTRA: e.get("extra"),
            COL_MEMO: e.get("memo", ""),
        })
    df = pd.DataFrame(records, columns=["연월", COL_PLAN, COL_SAVING, COL_INCOME, COL_EXTRA, COL_MEMO])
    for c in (COL_PLAN, COL_SAVING, COL_INCOME, COL_EXTRA):
        df[c] = df[c].astype("Float64")
    base = {"sig": sig, "key": f"actuals_editor_{sig}", "months": months, "df": df}
    ss.editor_base = base
    return base


# ---- 버튼 콜백 ----
def cb_plan_forward(month: str) -> None:
    reset_editor()
    value = S()["planOverrides"].get(month)
    for m in range_months():
        if m > month:
            set_plan_override(m, value)
    st.session_state.flash = f"{format_month(month)} 이후 모든 달의 계획 저축을 " + (
        format_won(value) if value is not None else f"기본값({format_won(S()['settings']['monthlySaving'])})") + "으로 맞췄습니다."


def cb_fill_plan(month: str) -> None:
    reset_editor()
    planned = planned_saving(S()["settings"], S()["planOverrides"], month_to_index(month))
    set_actual_field(month, "saving", planned)


def cb_clear_actual(month: str) -> None:
    reset_editor()
    S()["actuals"].pop(month, None)


def cb_save_scenario() -> None:
    state = S()
    if len(state["scenarios"]) >= MAX_SCENARIOS:
        return
    name = st.session_state.get("scn_name", "").strip()[:30] or f"시나리오 {len(state['scenarios']) + 1}"
    state["scenarios"].append({
        "id": new_id(),
        "name": name,
        "settings": dict(state["settings"]),
        "planOverrides": dict(state["planOverrides"]),
        "savedAt": datetime.now().astimezone().isoformat(timespec="seconds"),
    })
    st.session_state.scn_name = ""


def find_scenario(sid):
    return next((x for x in S()["scenarios"] if x["id"] == sid), None)


def cb_load_scenario(sid: str) -> None:
    sc = find_scenario(sid)
    if not sc:
        return
    merge_editor_edits()
    new_state = copy.deepcopy(S())
    # 금액 단위 표시는 화면 설정이므로 현재 값 유지
    new_state["settings"] = {**sc["settings"], "displayUnit": new_state["settings"]["displayUnit"]}
    new_state["planOverrides"] = dict(sc["planOverrides"])
    replace_state(new_state)
    st.session_state.flash = f'"{sc["name"]}" 시나리오를 불러왔습니다. 실적은 그대로 두었습니다.'


def cb_rename_scenario(sid: str) -> None:
    sc = find_scenario(sid)
    name = st.session_state.get("scn_rename", "").strip()
    if sc and name:
        sc["name"] = name[:30]
        st.session_state.scn_rename = ""


def cb_delete_scenario(sid: str) -> None:
    S()["scenarios"] = [x for x in S()["scenarios"] if x["id"] != sid]


def cb_import(raw_bytes: bytes) -> None:
    ss = st.session_state
    try:
        raw = json.loads(raw_bytes.decode("utf-8-sig"))
    except (ValueError, UnicodeDecodeError):
        ss.flash_error = "JSON 파일을 읽을 수 없습니다. 파일 내용을 확인하세요."
        return
    sv = raw.get("schemaVersion") if isinstance(raw, dict) else None
    if not isinstance(sv, int) or isinstance(sv, bool) or not isinstance(raw.get("settings"), dict):
        ss.flash_error = "이 시뮬레이터의 백업 파일이 아닙니다."
        return
    if sv > SCHEMA_VERSION:
        ss.flash_error = "더 새로운 버전에서 만든 백업 파일이라 가져올 수 없습니다."
        return
    replace_state(normalize_state(raw))
    ss.uploader_ver = ss.get("uploader_ver", 0) + 1
    ss.flash = "백업 파일을 가져왔습니다."


def cb_reset() -> None:
    replace_state(default_state())
    st.session_state.flash = "모든 데이터를 지우고 기본값으로 되돌렸습니다."


# ---- 내보내기 ----
def csv_text(v) -> str:
    """메모가 = + - @ 등으로 시작하면 엑셀이 수식으로 실행하므로 앞에 '를 붙여 글자로 고정."""
    s = "" if v is None else str(v)
    return "'" + s if s[:1] in ("=", "+", "-", "@", "\t", "\r") else s


def build_csv(result: dict) -> bytes:
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\r\n")
    w.writerow(["연월", "월 저축(계획)", "월별 조정", "정기수입", "이자", "계획 누적액", "실제 누적액", "계획 대비", "목표 대비(%)", "메모"])
    for r in result["rows"]:
        w.writerow([
            r["month"],
            js_round(r["saving"]),
            "O" if r["adjusted"] else "",
            js_round(r["income"]),
            js_round(r["interest"]),
            js_round(r["balance"]),
            "" if r["actualBalance"] is None else js_round(r["actualBalance"]),
            "" if r["diff"] is None else js_round(r["diff"]),
            f"{r['goalPct'] * 100:.1f}",
            csv_text(r["memo"]),
        ])
    return ("﻿" + buf.getvalue()).encode("utf-8")  # 엑셀 한글 깨짐 방지용 BOM


# ---- 화면 ----
CSS = """
<style>
  .gf-hero { display: grid; grid-template-columns: 1fr 1fr; gap: 12px; margin-bottom: 12px; }
  @media (max-width: 1000px) { .gf-hero { grid-template-columns: 1fr; } }
  .gf-hero-card { border-radius: 12px; padding: 18px; border: 1px solid rgba(128,128,128,.22); }
  .gf-hero-card.primary { background: rgba(70,120,230,.12); border-color: transparent; }
  .gf-hero-card.ok { background: rgba(34,163,90,.13); border-color: transparent; }
  .gf-hero-card.bad { background: rgba(224,72,72,.13); border-color: transparent; }
  .gf-label { font-size: 13px; font-weight: 600; opacity: .72; }
  .gf-hero-value { font-size: 30px; font-weight: 800; letter-spacing: -0.02em; line-height: 1.25; margin: 4px 0; color: #4a7ae8; }
  .gf-status { font-size: 22px; font-weight: 800; margin: 4px 0; }
  .gf-hero-card.ok .gf-status, .gf-ok { color: #22a35a; }
  .gf-hero-card.bad .gf-status, .gf-bad { color: #e04848; }
  .gf-sub { font-size: 13px; opacity: .72; }
  .gf-progress { position: relative; height: 10px; margin-top: 12px; border-radius: 999px; background: rgba(128,128,128,.18); overflow: hidden; }
  .gf-progress > div { position: absolute; left: 0; top: 0; bottom: 0; border-radius: 999px; }
  .gf-hero-card.ok .gf-progress { color: #22a35a; }
  .gf-hero-card.bad .gf-progress { color: #e04848; }
  .gf-progress .proj { background: currentColor; opacity: .3; }
  .gf-progress .now { background: currentColor; }
  .gf-progress-legend { display: flex; justify-content: space-between; flex-wrap: wrap; gap: 8px; margin-top: 6px; font-size: 12px; opacity: .72; }
  .gf-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(min(100%, 165px), 1fr)); gap: 10px; margin-bottom: 8px; }
  .gf-card { border: 1px solid rgba(128,128,128,.25); border-radius: 8px; padding: 12px 14px; }
  .gf-card .gf-value { font-size: 17px; font-weight: 700; overflow-wrap: anywhere; }
  .gf-card .gf-label, .gf-card .gf-sub { font-size: 12px; }
</style>
"""


def card(label, value, sub="", cls="") -> str:
    return (f'<div class="gf-card"><div class="gf-label">{label}</div>'
            f'<div class="gf-value {cls}">{value}</div>'
            + (f'<div class="gf-sub">{sub}</div>' if sub else "") + "</div>")


def render_sidebar() -> None:
    s = S()["settings"]
    with st.sidebar:
        st.header("조건 입력")

        st.subheader("목표")
        s["goalAmount"] = st.number_input("목표 금액 (원)", min_value=0, step=1_000_000, format="%d", key="w_goalAmount")
        st.caption(format_korean(s["goalAmount"]))
        for k, label in (("startMonth", "시작 시점"), ("goalMonth", "목표 시점")):
            c1, c2 = st.columns([3, 2])
            y = c1.selectbox(f"{label} (연)", YEAR_OPTIONS, key=f"w_{k}_y", format_func=lambda v: f"{v}년")
            m = c2.selectbox("월", list(range(1, 13)), key=f"w_{k}_m", format_func=lambda v: f"{v}월")
            s[k] = f"{y}-{m:02d}"
        st.caption("목표 시점은 해당 월 말일 기준이며, 시작 월과 목표 월을 모두 포함해 계산합니다.")

        st.subheader("자산 · 수입")
        s["currentFunds"] = st.number_input("현재 자금 (원)", min_value=0, step=1_000_000, format="%d",
                                            key="w_currentFunds", help="시작 시점에 이미 모아 둔 금액")
        st.caption(format_korean(s["currentFunds"]))
        s["monthlySaving"] = st.number_input("월 저축액 (원)", min_value=0, step=100_000, format="%d",
                                             key="w_monthlySaving", help="매달 기본으로 저축하는 금액")
        count = adjusted_month_count()
        st.caption(format_korean(s["monthlySaving"]) + " · " + (
            f"{count}개월은 월별로 따로 정했습니다." if count else '달마다 다르면 "월별 계획·실적" 탭에서 바꿀 수 있습니다.'))
        s["annualIncome"] = st.number_input("연 정기수입 (원)", min_value=0, step=1_000_000, format="%d",
                                            key="w_annualIncome", help="상여금·성과급 등 매년 들어오는 수입")
        st.caption(format_korean(s["annualIncome"]))
        c1, c2 = st.columns(2)
        s["annualIncomeMode"] = c1.selectbox("입금 방식", ["lump", "split"], key="w_annualIncomeMode",
                                             format_func=lambda v: "연 1회" if v == "lump" else "매월 균등 분할")
        s["annualIncomeMonth"] = c2.selectbox("입금 월", list(range(1, 13)), key="w_annualIncomeMonth",
                                              format_func=lambda v: f"{v}월", disabled=s["annualIncomeMode"] == "split")
        rate = st.number_input("예상 연 수익률 (%)", min_value=0.0, max_value=100.0, step=0.1, format="%.1f",
                               key="w_annualReturnRate")
        s["annualReturnRate"] = int(rate) if float(rate).is_integer() else rate
        st.caption("월복리 적용 (월 수익률 = 연 수익률 ÷ 12)")

        st.subheader("표시 옵션")
        s["displayUnit"] = st.radio("금액 단위", ["won", "man"], key="w_displayUnit", horizontal=True,
                                    format_func=lambda v: "원" if v == "won" else "만 원")
        s["roundExtraToManwon"] = st.checkbox("추가 필요액을 만 원 단위로 올림", key="w_roundExtraToManwon")



def render_backup(result: dict) -> None:
    ss = st.session_state
    state = S()
    with st.sidebar:
        st.divider()
        st.subheader("백업 · 내보내기")
        st.download_button("JSON 내보내기", data=json.dumps(state, ensure_ascii=False, indent=2).encode("utf-8"),
                           file_name=f"목표자금_백업_{today_stamp()}.json", mime="application/json", width="stretch")
        uploaded = st.file_uploader("JSON 가져오기", type=["json"], key=f"uploader_{ss.get('uploader_ver', 0)}")
        if uploaded is not None:
            st.warning("가져온 데이터로 현재 입력값, 실적, 시나리오를 모두 덮어씁니다.")
            st.button("가져오기 적용", on_click=cb_import, args=(uploaded.getvalue(),), type="primary", width="stretch")
        if not result.get("error"):
            st.download_button("CSV 내보내기", data=build_csv(result), file_name=f"목표자금_시뮬레이션_{today_stamp()}.csv",
                               mime="text/csv", width="stretch")
        with st.popover("초기화", width="stretch"):
            st.write("입력값, 실적, 시나리오를 모두 지우고 기본값으로 되돌립니다. 필요하면 먼저 JSON 내보내기로 백업하세요.")
            st.button("모두 지우고 초기화", on_click=cb_reset, type="primary")

        if not ss.get("storage_ok", True):
            st.error("데이터를 파일에 저장할 수 없습니다. JSON 내보내기로 백업하세요.")
        elif state.get("updatedAt"):
            st.caption("마지막 저장 " + datetime.fromisoformat(state["updatedAt"]).strftime("%m/%d %H:%M") + f" · {STATE_FILE.name}")
        else:
            st.caption("저장 기록 없음")


def render_summary(result: dict) -> None:
    s = result["summary"]
    settings = S()["settings"]
    goal = settings["goalAmount"]
    reach, reach_sub = reach_text(s["reachedMonth"], s["alreadyReached"])
    abs_v = abs(s["shortfall"])
    now_pct = min(s["progress"] * 100, 100)
    proj_pct = min(s["projected"] / goal * 100, 100) if goal > 0 else 0
    tone = "ok" if s["achievable"] else "bad"

    if s["goalReached"]:
        st.success(f"🎉 목표 금액 {format_korean(goal)}을 달성했습니다!")

    if s["extraMonthly"] <= 0:
        hero_sub = "지금 계획만으로 기한 내 달성할 수 있습니다"
    elif s["adjustedMonths"]:
        hero_sub = f"월별 계획 저축에 매달 <strong>{fmt(s['extraMonthly'])}</strong>씩 더 모으면 기한 내 달성"
    else:
        hero_sub = f"매달 총 <strong>{fmt(s['requiredMonthlyTotal'])}</strong>을 저축하면 기한 내 달성"
    hero = f"""<div class="gf-hero">
      <div class="gf-hero-card primary">
        <div class="gf-label">월 추가 저축 필요액</div>
        <div class="gf-hero-value">{fmt(s['extraMonthly'])}</div>
        <div class="gf-sub">{hero_sub}</div>
      </div>
      <div class="gf-hero-card {tone}">
        <div class="gf-label">달성 여부</div>
        <div class="gf-status">{'✔ 달성 가능' if s['achievable'] else '✖ 목표 미달 예상'}</div>
        <div class="gf-sub">{'초과' if s['achievable'] else '부족'} {format_korean(abs_v)} · {format_month(settings['goalMonth'])} 기준</div>
        <div class="gf-progress"><div class="proj" style="width:{proj_pct}%"></div><div class="now" style="width:{now_pct}%"></div></div>
        <div class="gf-progress-legend">
          <span>{'실제 잔액' if result['actual'] else '현재 자금'} {s['progress'] * 100:.1f}%</span>
          <span>기한 시점 예상 {(s['projected'] / goal * 100) if goal > 0 else 0:.1f}%</span>
        </div>
      </div>
    </div>"""
    cards = [
        card("남은 기간", f"{s['months']}개월", format_period(s["months"])),
        card("목표 시점 예상 누적액", fmt(s["projected"]), fmt_sub(s["projected"])),
        card("초과액" if s["achievable"] else "부족액", fmt(abs_v), fmt_sub(abs_v), f"gf-{tone}"),
        card("필요 월 총 저축액", fmt(s["requiredMonthlyTotal"]),
             "기본 월 저축액 기준" if s["adjustedMonths"] else fmt_sub(s["requiredMonthlyTotal"])),
        card("대안: 일시금 필요액", fmt(s["lumpSum"]), "지금 한 번에 넣을 경우"),
        card("예상 달성 시점", reach, reach_sub),
    ]
    st.subheader("결과 요약")
    st.caption("계획 기준")
    st.markdown(hero + '<div class="gf-grid">' + "".join(cards) + "</div>", unsafe_allow_html=True)

    a = result["actual"]
    if a:
        st.subheader(f"실적 기준 ({format_month(a['lastMonth'])}까지 반영)")
        a_reach, a_reach_sub = reach_text(a["reachedMonth"], a["alreadyReached"])
        a_abs = abs(a["shortfall"])
        extra_card = (card(f"남은 {a['remainingMonths']}개월 월 추가 필요액", fmt(a["extraMonthly"]), fmt_sub(a["extraMonthly"]))
                      if a["remainingMonths"] > 0 else card("월 추가 필요액", "남은 기간 없음", "목표 시점까지 실적 입력 완료"))
        cards = [
            card("실제 잔액", fmt(a["balance"]), fmt_sub(a["balance"])),
            card("계획 대비", fmt_signed(a["diff"]), "계획보다 앞섬" if a["diff"] >= 0 else "계획보다 뒤처짐",
                 "gf-ok" if a["diff"] >= 0 else "gf-bad"),
            card("목표 시점 예상 누적액", fmt(a["projected"]), fmt_sub(a["projected"])),
            card("초과액" if a["achievable"] else "부족액", fmt(a_abs), fmt_sub(a_abs), "gf-ok" if a["achievable"] else "gf-bad"),
            extra_card,
            card("필요 월 총 저축액", fmt(a["requiredMonthlyTotal"]),
                 "기본 월 저축액 기준" if s["adjustedMonths"] else fmt_sub(a["requiredMonthlyTotal"])),
            card("예상 달성 시점", a_reach, a_reach_sub),
        ]
        st.markdown('<div class="gf-grid">' + "".join(cards) + "</div>", unsafe_allow_html=True)


SERIES = [("계획 누적액", "#4a7ae8", [1, 0]), ("실제 누적액", "#22a35a", [1, 0]),
          ("목표 금액", "#e04848", [1, 0]), ("보완 계획", "#9a6cf0", [6, 4])]


def render_chart(result: dict) -> None:
    st.subheader("누적 추이")
    goal = S()["settings"]["goalAmount"]
    boost = result["boost"]
    recs = []
    for i, r in enumerate(result["rows"]):
        d = pd.Timestamp(r["month"] + "-01")
        label = format_month(r["month"])
        values = [("계획 누적액", r["balance"]), ("실제 누적액", r["actualBalance"]), ("목표 금액", goal)]
        if boost:
            values.append(("보완 계획", boost[i]))
        for name, v in values:
            if v is not None:
                recs.append({"date": d, "연월": label, "구분": name, "금액": v, "금액표시": format_won(v)})
    df = pd.DataFrame(recs)
    used = [x for x in SERIES if x[0] in set(df["구분"])]
    color = alt.Color("구분:N", scale=alt.Scale(domain=[x[0] for x in used], range=[x[1] for x in used]),
                      legend=alt.Legend(orient="top", title=None))
    dash = alt.StrokeDash("구분:N", scale=alt.Scale(domain=[x[0] for x in used], range=[x[2] for x in used]), legend=None)
    x = alt.X("date:T", title=None, axis=alt.Axis(
        labelExpr="timeFormat(datum.value, '%Y') + '년 ' + (month(datum.value) + 1) + '월'", tickCount=8, grid=False))
    y = alt.Y("금액:Q", title=None, scale=alt.Scale(zero=True), axis=alt.Axis(
        labelExpr="datum.value == 0 ? '0' : (abs(datum.value) >= 1e8 ? replace(format(datum.value / 1e8, '.1f'), '.0', '') + '억'"
                  " : format(datum.value / 1e4, ',.0f') + '만')"))
    tooltip = [alt.Tooltip("연월:N"), alt.Tooltip("구분:N"), alt.Tooltip("금액표시:N", title="금액")]
    plan_df = df[df["구분"] == "계획 누적액"]
    actual_df = df[df["구분"] == "실제 누적액"]
    layers = [
        alt.Chart(plan_df).mark_area(opacity=0.10, color="#4a7ae8").encode(x=x, y=y),
        alt.Chart(df).mark_line(strokeWidth=2.2).encode(x=x, y=y, color=color, strokeDash=dash, tooltip=tooltip),
    ]
    if not actual_df.empty:
        layers.append(alt.Chart(actual_df).mark_point(filled=True, size=30, color="#22a35a").encode(x=x, y=y, tooltip=tooltip))
    st.altair_chart(alt.layer(*layers).properties(height=340), width="stretch")
    if boost:
        extra = (result["actual"] or result["summary"])["extraMonthly"]
        st.caption(f"점선은 월 {format_won(extra)}을 더 저축할 때의 보완 계획입니다.")


def render_table(result: dict) -> None:
    lump_mode = S()["settings"]["annualIncomeMode"] == "lump"
    by_year: dict[int, list] = {}
    for row in result["rows"]:
        by_year.setdefault(int(row["month"][:4]), []).append(row)

    def dash_or(v):
        return fmt(v) if v else "-"

    st.caption("연도를 누르면 월별 행을 펼치거나 접습니다. 비고 열에 정기수입 입금 월·월별 조정·실적 입력 여부를 표시합니다.")
    for year, rows in by_year.items():
        last = rows[-1]
        last_actual = next((r for r in reversed(rows) if r["actualBalance"] is not None), None)
        title = (f"**{year}년 소계** · 저축 {fmt(sum(r['saving'] for r in rows))} · 정기수입 {dash_or(sum(r['income'] for r in rows))}"
                 f" · 이자 {dash_or(sum(r['interest'] for r in rows))} · 연말 계획 누적 {fmt(last['balance'])}"
                 + (f" · 실제 {fmt(last_actual['actualBalance'])} ({fmt_signed(last_actual['diff'])})" if last_actual else "")
                 + f" · 목표 대비 {last['goalPct'] * 100:.1f}%")
        with st.expander(title, expanded=True):
            data = []
            for r in rows:
                tags = []
                if lump_mode and r["income"] > 0:
                    tags.append("정기수입")
                if r["adjusted"]:
                    tags.append("조정")
                if r["hasActual"]:
                    tags.append("실적")
                data.append({
                    "연월": format_month(r["month"]),
                    "월 저축": fmt(r["saving"]),
                    "정기수입": dash_or(r["income"]),
                    "이자": dash_or(r["interest"]),
                    "계획 누적액": fmt(r["balance"]),
                    "실제 누적액": fmt(r["actualBalance"]) if r["actualBalance"] is not None else "-",
                    "계획 대비": fmt_signed(r["diff"]) if r["diff"] is not None else "-",
                    "목표 대비": f"{r['goalPct'] * 100:.1f}%",
                    "비고": " · ".join(tags),
                    "메모": r["memo"],
                })
            st.dataframe(pd.DataFrame(data), hide_index=True, width="stretch")


def render_actuals(result: dict) -> None:
    st.caption("**계획 저축**은 그 달에 저축할 예정인 금액입니다. 비워 두면 기본 월 저축액을 씁니다. "
               "**실제 저축** 칸은 실제로 저축한 금액이며, 빈 칸은 계획값으로 계산합니다. "
               "기타 수입/지출은 지출이면 앞에 −를 붙이세요. 실제 누적액은 \"월별 표\" 탭에서 확인합니다.")
    months = range_months()
    if not months:
        st.info("시작 시점과 목표 시점을 확인하세요.")
        return
    base = editor_base()
    money = dict(min_value=0, step=1, format="localized")
    st.data_editor(
        base["df"],
        key=base["key"],
        hide_index=True,
        width="stretch",
        height=min(38 + 35 * len(months), 600),
        disabled=["연월"],
        num_rows="fixed",
        placeholder="",
        column_config={
            "연월": st.column_config.TextColumn("연월"),
            COL_PLAN: st.column_config.NumberColumn(COL_PLAN, help=f"비우면 기본값 {format_won(S()['settings']['monthlySaving'])}", **money),
            COL_SAVING: st.column_config.NumberColumn(COL_SAVING, help="비우면 그 달의 계획 저축으로 계산", **money),
            COL_INCOME: st.column_config.NumberColumn(COL_INCOME, help="비우면 계획한 정기수입으로 계산", **money),
            COL_EXTRA: st.column_config.NumberColumn(COL_EXTRA, step=1, format="localized", help="지출은 음수"),
            COL_MEMO: st.column_config.TextColumn(COL_MEMO, max_chars=200),
        },
    )

    st.markdown("##### 월 단위 작업")
    labels = {m: format_month(m) for m in months}
    now = current_month()
    month = st.selectbox("대상 월", months, index=months.index(now) if now in months else 0,
                         format_func=lambda m: labels[m], key="act_month")
    c1, c2, c3 = st.columns(3)
    c1.button("실제 저축 = 계획대로", on_click=cb_fill_plan, args=(month,), width="stretch",
              help="실제 저축을 이 달의 계획 저축으로 채움")
    c2.button("계획 저축 이후 적용", on_click=cb_plan_forward, args=(month,), width="stretch",
              help="이 달의 계획 저축을 다음 달부터 끝까지 똑같이 맞춤")
    c3.button("실적 지우기", on_click=cb_clear_actual, args=(month,), width="stretch",
              help="이 달의 실적(실제 저축·정기수입·기타·메모)을 지움. 계획 저축은 그대로")


def scenario_record(name: str, settings: dict, plan: dict) -> dict:
    r = simulate(settings, {}, plan)
    rec = {
        "이름": name,
        "현재 자금": fmt(settings["currentFunds"]),
        "월 저축": fmt(settings["monthlySaving"]),
        "연 정기수입": fmt(settings["annualIncome"]),
        "수익률": f"{settings['annualReturnRate']}%",
    }
    if r.get("error"):
        rec.update({"기간": r["error"], "예상 누적액": "", "부족/초과": "", "월 추가 필요액": "", "예상 달성": ""})
        return rec
    s = r["summary"]
    if s["adjustedMonths"]:
        rec["월 저축"] += f" ({s['adjustedMonths']}개월 조정)"
    rec.update({
        "기간": f"{s['months']}개월",
        "예상 누적액": fmt(s["projected"]),
        "부족/초과": ("+" if s["achievable"] else "−") + fmt(abs(s["shortfall"])),
        "월 추가 필요액": fmt(s["extraMonthly"]),
        "예상 달성": reach_text(s["reachedMonth"], s["alreadyReached"])[0],
    })
    return rec


def render_scenarios() -> None:
    state = S()
    st.caption(f"현재 입력값을 시나리오로 저장해 비교합니다 (최대 {MAX_SCENARIOS}개). 시나리오 비교에는 실적을 반영하지 않습니다.")
    full = len(state["scenarios"]) >= MAX_SCENARIOS
    c1, c2 = st.columns([3, 1], vertical_alignment="bottom")
    c1.text_input("시나리오 이름", max_chars=30, placeholder="시나리오 이름 (예: 월 200만)", key="scn_name")
    c2.button(f"최대 {MAX_SCENARIOS}개까지 저장 가능" if full else "현재 입력값 저장", on_click=cb_save_scenario,
              disabled=full, type="primary", width="stretch")

    recs = [scenario_record("▶ 현재 입력값", state["settings"], state["planOverrides"])]
    recs += [scenario_record(sc["name"], sc["settings"], sc["planOverrides"]) for sc in state["scenarios"]]
    st.dataframe(pd.DataFrame(recs), hide_index=True, width="stretch")

    if state["scenarios"]:
        names = {sc["id"]: sc["name"] for sc in state["scenarios"]}
        sid = st.selectbox("시나리오 선택", list(names), format_func=lambda i: names[i], key="scn_pick")
        c1, c2, c3 = st.columns([2, 1, 1], vertical_alignment="bottom")
        c1.text_input("새 이름", max_chars=30, key="scn_rename")
        c2.button("이름 변경", on_click=cb_rename_scenario, args=(sid,), width="stretch")
        c3.button("삭제", on_click=cb_delete_scenario, args=(sid,), width="stretch")
        with st.popover("불러오기"):
            st.write(f'"{names[sid]}" 시나리오를 불러오면 현재 입력값과 월별 계획 저축을 덮어씁니다. 실적은 그대로 둡니다.')
            st.button("불러오기 확인", on_click=cb_load_scenario, args=(sid,), type="primary")


def main() -> None:
    st.set_page_config(page_title="목표 자금 시뮬레이터", page_icon="₩", layout="wide")
    ss = st.session_state
    if "app_state" not in ss:
        ss.app_state, ss.storage_ok = load_state()
        ss.saved_fp = state_fingerprint(ss.app_state)
        sync_widgets_from_state()

    st.markdown(CSS, unsafe_allow_html=True)
    render_sidebar()
    merge_editor_edits()

    state = S()
    result = simulate(state["settings"], state["actuals"], state["planOverrides"])
    save_state_if_changed(state)
    render_backup(result)

    st.title("₩ 목표 자금 달성 시뮬레이터")
    if msg := ss.pop("flash", None):
        st.toast(msg)
    if err := ss.pop("flash_error", None):
        st.error(err)

    if result.get("error"):
        st.error("⚠ " + result["error"])
    else:
        render_summary(result)
        render_chart(result)

    tab_table, tab_actuals, tab_scn = st.tabs(["월별 표", "월별 계획·실적", "시나리오 비교"])
    with tab_table:
        if not result.get("error"):
            render_table(result)
    with tab_actuals:
        render_actuals(result)
    with tab_scn:
        render_scenarios()


if __name__ == "__main__":
    main()
