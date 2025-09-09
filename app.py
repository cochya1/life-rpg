# app.py — Жизненная RPG (исправленный)

# ---------- ИМПОРТЫ ----------
import streamlit as st
import json
import os
import io
import pandas as pd

from datetime import datetime, date, timedelta
from zoneinfo import ZoneInfo
from collections import Counter
from datetime import time as dtime

def goal_due_datetime(goal) -> datetime:
    """
    Возвращает полноценный datetime дедлайна задачи.
    Если время не задано — считаем 23:59:59 этого дня.
    """
    dd: date = goal["due"]
    tstr = goal.get("time")
    if tstr:
        try:
            hh, mm = map(int, tstr.split(":"))
            return datetime.combine(dd, dtime(hh, mm))
        except Exception:
            pass
    return datetime.combine(dd, dtime(23, 59, 59))

# ---------- SUPABASE AUTH (инициализация клиента) ----------
from supabase import create_client, Client

@st.cache_resource
def get_supabase() -> Client:
    url = st.secrets.get("SUPABASE_URL", "").strip()
    key = st.secrets.get("SUPABASE_ANON_KEY", "").strip()  # <-- ВАЖНО: ANON
    if not (url.startswith("https://") and ".supabase.co" in url):
        st.error("❗ SUPABASE_URL не задан/неверный (Manage app → Settings → Secrets).")
        st.stop()
    if not key:
        st.error("❗ SUPABASE_ANON_KEY не задан (Settings → Secrets).")
        st.stop()
    return create_client(url, key)

supabase = get_supabase()

def auth_form():
    st.header("🔐 Вход в аккаунт")
    try:
        mode = st.segmented_control("Режим", ["Войти", "Регистрация"], key="auth_mode")
    except Exception:
        mode = st.radio("Режим", ["Войти", "Регистрация"], key="auth_mode_radio")

    email = st.text_input("Email", key="auth_email")
    password = st.text_input("Пароль", type="password", key="auth_password")

    col1, col2 = st.columns(2)
    with col1:
        disabled = (mode != "Войти") or (not email or not password)
        if st.button("Войти", use_container_width=True, disabled=disabled):
            try:
                res = supabase.auth.sign_in_with_password({"email": email, "password": password})
                st.session_state.auth_user = res.user.model_dump()
                st.success("Готово! Вошли.")
                st.rerun()
            except Exception as e:
                st.error(f"Не удалось войти: {e}")

    with col2:
        disabled = (mode != "Регистрация") or (not email or not password)
        if st.button("Зарегистрироваться", use_container_width=True, disabled=disabled):
            try:
                res = supabase.auth.sign_up({"email": email, "password": password})
                st.session_state.auth_user = res.user.model_dump()
                st.success("Аккаунт создан, вы вошли.")
                st.rerun()
            except Exception as e:
                st.error(f"Не удалось зарегистрироваться: {e}")

def current_user_id() -> str | None:
    """UUID пользователя из Supabase Auth (или None, если не залогинен)."""
    u = st.session_state.get("auth_user")
    if u and u.get("id"):
        return u["id"]
    # пробуем восстановить сессию
    try:
        res = supabase.auth.get_user()
        if res and res.user:
            st.session_state.auth_user = res.user.model_dump()
            return res.user.id
    except Exception:
        pass
    return None

def logout_button():
    if st.sidebar.button("Выйти", use_container_width=True):
        try:
            supabase.auth.sign_out()
        except Exception:
            pass
        st.session_state.pop("auth_user", None)
        st.rerun()

# === Проверка авторизации ===
user_id = current_user_id()
if not user_id:
    auth_form()   # показываем форму логина/регистрации
    st.stop()     # дальше код не идёт, пока не войдём

# Кнопка выхода в сайдбаре
logout_button()

# ---------- ХРАНИЛКА В SUPABASE ----------
def db_save_state(user_id: str, data: dict):
    supabase.table("rpg_state").upsert({"user_id": user_id, "data": data}).execute()

def db_load_state(user_id: str) -> dict | None:
    res = supabase.table("rpg_state").select("data").eq("user_id", user_id).execute()
    if res.data:
        return res.data[0]["data"]
    return None

def save_state():
    user_id = current_user_id()
    if not user_id:
        return  # не залогинен — не сохраняем
    try:
        db_save_state(user_id, serialize_state())
    except Exception as e:
        st.sidebar.warning(f"Не удалось сохранить в базу: {e}")

def load_state_if_exists() -> bool:
    user_id = current_user_id()
    if not user_id:
        return False
    try:
        data = db_load_state(user_id)
        if data:
            deserialize_state(data)
            return True
    except Exception as e:
        st.sidebar.warning(f"Не удалось загрузить из базы: {e}")
    return False

# Altair для пончиковых диаграмм
import altair as alt

# ========================= БАЗОВЫЕ НАСТРОЙКИ =========================
st.set_page_config(page_title="Жизненная RPG", page_icon="🎯", layout="wide")
st.title("🎯 Жизненная RPG")

STATE_FILE = "state.json"

GOAL_TYPES = {"Краткосрочная": 5, "Среднесрочная": 25, "Долгосрочная": 70}
XP_PER_LEVEL = 1000
WEEKDAY_LABELS = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]
CATEGORIES = ["Работа", "Личное", "Семья", "Прочее", "Проекты"]

HABIT_XP = 10

BIG_GOAL_XP = 250
BIG_GOAL_STAT_BONUS = 10

def award_big_goal_failure():
    """-250 XP и -10 ко всем характеристикам за провал глобальной цели."""
    add_xp(-BIG_GOAL_XP)
    for k in list(st.session_state.stats.keys()):
        update_stat(k, -BIG_GOAL_STAT_BONUS)

# ========================= УТИЛИТЫ =========================
def days_left_text(due: date, time_str: str | None = None) -> str:
    """
    Если задано время — показываем точнее (сегодня/через X часов/просрочена на ...).
    Без времени — старая логика по дням.
    """
    if time_str:
        try:
            hh, mm = map(int, time_str.split(":"))
            dt_due = datetime.combine(due, dtime(hh, mm))
        except Exception:
            dt_due = datetime.combine(due, dtime(23, 59, 59))
        delta = dt_due - datetime.now()
        s = int(delta.total_seconds())
        if s > 0:
            days = s // 86400
            if days > 0:
                return f"осталось {days} дн."
            hours = s // 3600
            if hours > 0:
                return f"через {hours} ч."
            mins = max(1, (s % 3600) // 60)
            return f"через {mins} мин."
        else:
            s = -s
            days = s // 86400
            if days > 0:
                return f"просрочена на {days} дн."
            hours = s // 3600
            if hours > 0:
                return f"просрочена на {hours} ч."
            mins = max(1, (s % 3600) // 60)
            return f"просрочена на {mins} мин."
    else:
        d = (due - date.today()).days
        if d > 0:
            return f"осталось {d} дн."
        if d == 0:
            return "сегодня дедлайн"
        return f"просрочена на {-d} дн."

def classify_by_due(due: date) -> str:
    left = (due - date.today()).days
    if left <= 7:
        return "Краткосрочная"
    elif left <= 92:
        return "Среднесрочная"
    else:
        return "Долгосрочная"


def next_from_days(d: date, days: list[int]) -> date:
    if not days:
        return d + timedelta(days=7)
    for step in range(1, 8):
        cand = d + timedelta(days=step)
        if cand.weekday() in days:
            return cand
    return d + timedelta(days=7)


def compute_next_due(goal) -> date:
    mode = goal.get("recur_mode", "none")
    if mode == "daily":
        return goal["due"] + timedelta(days=1)
    elif mode == "weekly":
        return goal["due"] + timedelta(days=7)            
    elif mode == "by_days":
        return next_from_days(goal["due"], goal.get("recur_days", []))
    return goal["due"]

def _moscow_now() -> datetime:
    """Текущее время в часовом поясе МСК."""
    return datetime.now(ZoneInfo("Europe/Moscow"))

def _default_stats_dict():
    return {
        "Здоровье ❤️": 0,
        "Интеллект 🧠": 0,
        "Радость 🙂": 0,
        "Отношения 🤝": 0,
        "Успех ⭐": 0,
        "Дисциплина 🎯": 0.0,
    }

def export_year_report_xlsx(archive: dict, year: int) -> bytes:
    """
    Делает Excel со статистикой за год на основе snapshot'а archive (serialize_state()).
    Возвращает bytes xlsx — их удобно отдавать через st.download_button.
    """
    # Подготовим таблицы
    # --- XP по дням
    xp_items = sorted([(k, int(v)) for k, v in archive.get("xp_log", {}).items()
                       if k.startswith(str(year) + "-")])
    df_xp = pd.DataFrame(xp_items, columns=["Дата (ISO)", "ΔXP"]) if xp_items else pd.DataFrame(columns=["Дата (ISO)", "ΔXP"])

    # --- Обычные задачи
    goals = archive.get("goals", [])
    df_goals = pd.DataFrame(goals) if goals else pd.DataFrame(columns=[
        "title","due","type","category","done","failed","overdue","stat","recur_mode","recur_days"
    ])

    # --- Глобальные цели
    bgoals = archive.get("big_goals", [])
    df_big = pd.DataFrame(bgoals) if bgoals else pd.DataFrame(columns=["title","due","done","failed","note"])

    # --- Привычки
    habits = archive.get("habits", [])
    # развернём в удобный вид
    rows_h = []
    for h in habits:
        rows_h.append({
            "title": h.get("title",""),
            "days": ",".join(map(str, h.get("days",[]))),
            "stat": h.get("stat",""),
            "completions_count": len(h.get("completions",[])),
            "failures_count": len(h.get("failures",[])),
            "completions": ",".join(h.get("completions",[])),
            "failures": ",".join(h.get("failures",[])),
        })
    df_habits = pd.DataFrame(rows_h) if rows_h else pd.DataFrame(columns=[
        "title","days","stat","completions_count","failures_count","completions","failures"
    ])

    # --- Сводка
    total_goals = len(goals)
    done_goals = sum(1 for g in goals if g.get("done"))
    failed_goals = sum(1 for g in goals if g.get("failed"))
    overdue_goals = sum(1 for g in goals if g.get("overdue"))

    total_big = len(bgoals)
    done_big = sum(1 for g in bgoals if g.get("done"))
    failed_big = sum(1 for g in bgoals if g.get("failed"))

    total_habits = len(habits)
    total_h_done = sum(len(h.get("completions",[])) for h in habits)
    total_h_fail = sum(len(h.get("failures",[])) for h in habits)

    df_summary = pd.DataFrame([
        {"Показатель":"Год", "Значение": year},
        {"Показатель":"Всего задач", "Значение": total_goals},
        {"Показатель":"Выполнено задач", "Значение": done_goals},
        {"Показатель":"Провалено задач", "Значение": failed_goals},
        {"Показатель":"Просрочено задач", "Значение": overdue_goals},
        {"Показатель":"Глобальных целей всего", "Значение": total_big},
        {"Показатель":"Глобальных целей выполнено", "Значение": done_big},
        {"Показатель":"Глобальных целей провалено", "Значение": failed_big},
        {"Показатель":"Привычек всего", "Значение": total_habits},
        {"Показатель":"Выполнений привычек", "Значение": total_h_done},
        {"Показатель":"Провалов привычек", "Значение": total_h_fail},
        {"Показатель":"Итоговый XP", "Значение": int(archive.get("xp",0))},
        {"Показатель":"Итоговый уровень", "Значение": int(archive.get("level",1))},
    ])

    # Пишем в xlsx (в память)
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="xlsxwriter") as writer:
        df_summary.to_excel(writer, sheet_name="Сводка", index=False)
        df_xp.to_excel(writer, sheet_name="XP по дням", index=False)
        df_goals.to_excel(writer, sheet_name="Задачи", index=False)
        df_big.to_excel(writer, sheet_name="Глобальные цели", index=False)
        df_habits.to_excel(writer, sheet_name="Привычки", index=False)
    buf.seek(0)
    return buf.read()

def reset_all_stats_after_export():
    """Обнуляет статистику и рабочие списки после экспорта отчёта."""
    st.session_state.goals = []
    st.session_state.big_goals = []
    st.session_state.habits = []
    st.session_state.xp_log = {}
    st.session_state.discipline_awarded_dates = []
    st.session_state.xp = 0
    st.session_state.level = 1
    st.session_state.stats = _default_stats_dict()
    save_state()

def auto_check_yearly_reset():
    """
    Каждый запуск проверяет: если сегодня 31 декабря >= 12:00 (МСК) и за этот год
    ещё не сбрасывали — сформировать отчёт, поднять флаг модалки и обнулить статистику.
    """
    now_msk = _moscow_now()
    year = now_msk.year
    # 31 декабря, 12:00 или позже
    if (now_msk.month, now_msk.day) != (12, 31) or now_msk.hour < 12:
        return
    # уже сбрасывали этот год?
    if st.session_state.get("last_reset_year") == year:
        return

    # --- формируем snapshot для отчёта
    snapshot = serialize_state()  # ТЕКУЩЕЕ состояние до обнуления
    report_bytes = export_year_report_xlsx(snapshot, year)
    # сохраним в session_state для download_button
    st.session_state.yearly_report_bytes = report_bytes
    st.session_state.yearly_report_year = year

    # --- обнуляем всё
    reset_all_stats_after_export()

    # запомним, что в этом году уже сброшено
    st.session_state.last_reset_year = year
    st.session_state.year_reset_pending = True
    save_state()

def render_year_reset_modal():
    """Поздравление с прошедшим годом + кнопка скачать отчёт и закрыть модалку."""
    if not st.session_state.get("year_reset_pending"):
        return

    title = "🎆 С Новым годом!"
    body_md = (
        "Поздравляю с прошедшим годом! Ты проделал(а) огромную работу — гордись собой. "
        "Пусть новый год будет ещё сильнее, радостнее и продуктивнее. 🚀\n\n"
        "Здесь можно скачать **полный отчёт за год** в Excel."
    )

    # 1) Если есть st.dialog (новые версии)
    if hasattr(st, "dialog"):
        @st.dialog(title)
        def _year_dialog():
            st.markdown(body_md)
            year = st.session_state.get("yearly_report_year", _moscow_now().year)
            data = st.session_state.get("yearly_report_bytes", b"")
            file_name = f"year_report_{year}.xlsx"
            if data:
                st.download_button("⬇️ Скачать отчёт", data=data, file_name=file_name, mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", use_container_width=True)
            if st.button("Только вперёд 💪", use_container_width=True, key="close_newyear"):
                st.session_state.year_reset_pending = False
                # чистим байты, чтобы не висели в памяти
                st.session_state.yearly_report_bytes = b""
                save_state()
                st.rerun()
        _year_dialog()
        return

    # 2) Fallback: «псевдо-модалка»
    st.markdown("""
    <style>
      .ny-overlay { position: fixed; inset: 0; background: rgba(0,0,0,.55);
        display:flex; align-items:center; justify-content:center; z-index: 9999; }
      .ny-modal { width:min(560px,95vw); background:#111; color:#fff; border:1px solid #333;
        border-radius: 16px; padding: 24px; box-shadow: 0 20px 60px rgba(0,0,0,.6); }
      .ny-title { font-size:24px; margin:0 0 8px 0; }
      .ny-sub { opacity:.9; margin-bottom: 14px; white-space: pre-wrap; }
    </style>
    """, unsafe_allow_html=True)
    st.markdown(f"""
    <div class="ny-overlay">
      <div class="ny-modal">
        <div class="ny-title">{title}</div>
        <div class="ny-sub">{body_md}</div>
      </div>
    </div>
    """, unsafe_allow_html=True)

    # Кнопки рядом (в обычном потоке)
    year = st.session_state.get("yearly_report_year", _moscow_now().year)
    data = st.session_state.get("yearly_report_bytes", b"")
    file_name = f"year_report_{year}.xlsx"
    if data:
        st.download_button("⬇️ Скачать отчёт", data=data, file_name=file_name, mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", use_container_width=True, key="dl_year_report_fb")
    if st.button("Только вперёд 💪", use_container_width=True, key="close_newyear_fb"):
        st.session_state.year_reset_pending = False
        st.session_state.yearly_report_bytes = b""
        save_state()
        st.rerun()

# ========================= СОХРАНЕНИЕ/ЗАГРУЗКА =========================
def serialize_state():
    return {
        "xp": st.session_state.xp,
        "level": st.session_state.level,
        "stats": st.session_state.stats,
        "goals": [
    {
        "title": g["title"],
        "due": g["due"].isoformat(),
        "type": g["type"],
        "category": g.get("category", "Прочее"),
        "done": g["done"],
        "failed": g["failed"],
        "overdue": g.get("overdue", False),
        "stat": g["stat"],
        "recur_mode": g.get("recur_mode", "none"),
        "recur_days": g.get("recur_days", []),
        "time": g.get("time"),  # <— НОВОЕ
    }
    for g in st.session_state.goals
    ],
        "xp_log": st.session_state.xp_log,
        "discipline_awarded_dates": st.session_state.discipline_awarded_dates,

        # глобальные цели
        "big_goals": [
            {
                "title": g["title"],
                "due": g["due"].isoformat(),
                "done": g["done"],
                "failed": g["failed"],
                "note": g.get("note", ""),
            }
            for g in st.session_state.get("big_goals", [])
        ],

        # привычки
        "habits": [
            {
                "title": h["title"],
                "days": h.get("days", []),
                "stat": h.get("stat", "Дисциплина 🎯"),
                "completions": h.get("completions", []),
                "failures": h.get("failures", []),
            }
            for h in st.session_state.get("habits", [])
        ],
    }


def deserialize_state(data: dict):
    st.session_state.xp = int(data.get("xp", 0))
    st.session_state.level = int(data.get("level", 1))
    st.session_state.stats = data.get(
        "stats",
        {
            "Здоровье ❤️": 0,
            "Интеллект 🧠": 0,
            "Радость 🙂": 0,
            "Отношения 🤝": 0,
            "Успех ⭐": 0,
            "Дисциплина 🎯": 0.0,
        },
    )

    # обычные задачи
    st.session_state.goals.append(
    {
        "title": g["title"],
        "due": date.fromisoformat(g["due"]),
        "type": g["type"],
        "category": g.get("category", "Прочее"),
        "done": g.get("done", False),
        "failed": g.get("failed", False),
        "overdue": g.get("overdue", False),
        "stat": g.get("stat", "Успех ⭐"),
        "recur_mode": g.get("recur_mode", "none"),
        "recur_days": g.get("recur_days", []),
        "time": g.get("time"),  # <— НОВОЕ
    }
    )


    # xp_log (ВЫРОВНЯН по уровню функции)
    xp_src = data.get("xp_log", {})
    if isinstance(xp_src, dict):
        st.session_state.xp_log = {str(k): int(v) for k, v in xp_src.items()}
    else:
        try:
            st.session_state.xp_log = {str(k): int(v) for k, v in xp_src}
        except Exception:
            st.session_state.xp_log = {}

    # дисциплина
    st.session_state.discipline_awarded_dates = data.get("discipline_awarded_dates", [])

    # глобальные цели
    st.session_state.big_goals = []
    for g in data.get("big_goals", []):
        st.session_state.big_goals.append({
            "title": g["title"],
            "due": date.fromisoformat(g["due"]),
            "done": g.get("done", False),
            "failed": g.get("failed", False),
            "note": g.get("note", ""),
        })

        # привычки
    st.session_state.habits = []
    for h in data.get("habits", []):
        st.session_state.habits.append({
            "title": h["title"],
            "days": h.get("days", []),
            "stat": h.get("stat", "Дисциплина 🎯"),
            "completions": list(h.get("completions", [])),
            "failures": list(h.get("failures", [])),
        })

# ========================= XP / СТАТЫ =========================
def ensure_xp_log_dict():
    log = st.session_state.get("xp_log")
    if log is None:
        st.session_state.xp_log = {}
        return
    if isinstance(log, list):
        # конвертируем список пар в словарь
        try:
            st.session_state.xp_log = {str(k): int(v) for k, v in log}
        except Exception:
            st.session_state.xp_log = {}
    elif not isinstance(log, dict):
        st.session_state.xp_log = {}


def add_xp(delta: int):
    """Начисляет/списывает опыт, логирует его и проверяет повышение уровня."""
    ensure_xp_log_dict()

    xp_old = int(st.session_state.get("xp", 0))
    lvl_old = int(st.session_state.get("level", 1))

    xp_new = xp_old + int(delta)
    st.session_state.xp = xp_new

    # лог по дням
    d = date.today().isoformat()
    st.session_state.xp_log[d] = int(st.session_state.xp_log.get(d, 0)) + int(delta)

    # новый уровень каждые 1000 XP (базовый 1)
    lvl_new = max(1, (xp_new // 1000) + 1)
    if lvl_new > lvl_old:
        st.session_state.level = lvl_new
        st.session_state.levelup_pending = True
        st.session_state.levelup_to = lvl_new

    save_state()

WEEKDAY_LABELS = ["Пн","Вт","Ср","Чт","Пт","Сб","Вс"]  # если уже есть — второй раз не вставляйте

def today_str() -> str:
    return date.today().isoformat()

def is_habit_scheduled_today(h: dict, on_date: date | None = None) -> bool:
    d = on_date or date.today()
    return d.weekday() in h.get("days", [])

def habit_done_on_date(h: dict, on_date: date | None = None) -> bool:
    d = on_date or date.today()
    return (d.isoformat() in h.get("completions", []))

def habit_failed_on_date(h: dict, on_date: date | None = None) -> bool:
    d = on_date or date.today()
    return (d.isoformat() in h.get("failures", []))

def habit_mark_done(h: dict, on_date: date | None = None):
    d = (on_date or date.today()).isoformat()
    if d not in h["completions"]:
        h["completions"].append(d)
        add_xp(HABIT_XP)
        # прокачиваем выбранную характеристику на +1 (как и у задач)
        update_stat(h.get("stat", "Дисциплина 🎯"), +1)
        # маленький бонус дисциплины за факт выполнения любой единицы в день у нас не даётся,
        # общий +1 к дисциплине начисляется другим авто-правилом, когда выполнено всё за день
        save_state()

def habit_mark_failed(h: dict, on_date: date | None = None):
    d = (on_date or date.today()).isoformat()
    if d not in h["failures"]:
        h["failures"].append(d)
        add_xp(-HABIT_XP)
        update_stat(h.get("stat", "Дисциплина 🎯"), -1)
        save_state()

def habit_uid(h: dict) -> str:
    return f"{h['title']}|{','.join(map(str, h.get('days', [])))}|{h.get('stat','')}"


def update_stat(stat_name: str, delta: float):
    st.session_state.stats[stat_name] = max(
        0, round(st.session_state.stats.get(stat_name, 0) + float(delta), 2)
    )
    save_state()


# ========================= АВТО-ЛОГИКА (просрочки/дисциплина) =========================
def _ensure_discipline_list():
    if "discipline_awarded_dates" not in st.session_state or st.session_state.discipline_awarded_dates is None:
        st.session_state.discipline_awarded_dates = []


def _day_done_ok(the_day: date) -> bool:
    """True, если все задачи И все привычки, запланированные на день, выполнены; и нет провалов."""
    # Задачи (как было)
    todays_goals = [g for g in st.session_state.goals if g["due"] == the_day]
    if todays_goals:
        if any(g["failed"] for g in todays_goals):
            return False
        if not all(g["done"] for g in todays_goals if g.get("recur_mode","none") == "none"):
            # одноразовые должны быть все done
            if any((not g["done"]) and g.get("recur_mode","none") == "none" for g in todays_goals):
                return False

    # Повторяющиеся задачи на день тоже должны быть закрыты в этот день,
    # но у нас их мы переносим по нажатию. Для простоты — если есть повторяющиеся на этот день и они не закрыты, считаем не ок.
    if any((g.get("recur_mode","none")!="none") and (g["due"]==the_day) for g in todays_goals):
        # если есть хоть одна повторяющаяся задача с due==the_day, требуем, чтобы юзер её нажал "выполнить" (т.е. не оставил на этот день)
        if any((g.get("recur_mode","none")!="none") and (g["due"]==the_day) for g in todays_goals):
            return False

    # Привычки
    todays_habits = [h for h in st.session_state.get("habits", []) if is_habit_scheduled_today(h, the_day)]
    if todays_habits:
        # если какая-то привычка провалена в этот день — сразу не ок
        if any(habit_failed_on_date(h, the_day) for h in todays_habits):
            return False
        # все запланированные привычки на день должны быть выполнены
        if not all(habit_done_on_date(h, the_day) for h in todays_habits):
            return False

    # если ни задач, ни привычек — не даём авто-бонус (возвращаем False)
    if not todays_goals and not todays_habits:
        return False

    return True

def auto_process_overdues():
    """Штрафуем и переносим просроченные задачи; одноразовые — помечаем проваленными."""
    changed = False
    today = date.today()

    for g in st.session_state.goals:
        if g["done"] or g["failed"]:
            continue
        now_dt = datetime.now()
            due_dt = goal_due_datetime(g)
            if due_dt.date() < today or (due_dt.date() == today and due_dt < now_dt):
    # т.е. просрочено, если день в прошлом, или сегодня, но время уже прошло

                # повторяемые — за каждый пропуск
                while g["due"] < today:
                    add_xp(-reward)
                    update_stat(g["stat"], -1)
                    update_stat("Дисциплина 🎯", -0.1)
                    g["due"] = compute_next_due(g)
                    g["type"] = classify_by_due(g["due"])
                    changed = True
                g["overdue"] = False
            else:
                # одноразовые
                g["overdue"] = True
                add_xp(-reward)
                update_stat(g["stat"], -1)
                update_stat("Дисциплина 🎯", -0.1)
                g["failed"] = True
                changed = True

    if changed:
        save_state()

def auto_process_big_goal_overdues():
    """Если глобальная цель просрочена и не закрыта — провалить и применить штраф один раз."""
    today = date.today()
    changed = False

    for g in st.session_state.get("big_goals", []):
        if g.get("done") or g.get("failed"):
            continue
        if g["due"] < today:
            # помечаем как проваленную и штрафуем
            g["failed"] = True
            award_big_goal_failure()
            changed = True

    if changed:
        save_state()

def auto_award_yesterday_if_ok():
    """Если вчера все задачи выполнены и бонус ещё не выдавался — +1 к дисциплине."""
    _ensure_discipline_list()
    y = date.today() - timedelta(days=1)
    y_str = y.isoformat()
    if y_str in st.session_state.discipline_awarded_dates:
        return
    if _day_done_ok(y):
        update_stat("Дисциплина 🎯", +1.0)
        st.session_state.discipline_awarded_dates.append(y_str)
        save_state()
        st.sidebar.success("Вчера всё выполнено: Дисциплина +1.0 🎯")

# ========================= UI: ЗАДАЧИ =========================
def goal_uid(g) -> str:
    base = (
        f"{g['title']}|{g['due'].isoformat()}|{g.get('recur_mode','none')}|"
        f"{','.join(map(str, g.get('recur_days', [])))}|{g.get('category','')}"
    )
    return f"{base}|{id(g)}"

def big_goal_uid(g) -> str:
    return f"{g['title']}|{g['due'].isoformat()}|{id(g)}"

def row(goal, scope: str, idx: int):
    reward = GOAL_TYPES[goal["type"]]
    status = "✅" if goal["done"] else ("❌" if goal["failed"] else ("⏰" if goal.get("overdue") else "⬜"))

    left, mid, b1, b2, b3 = st.columns([6, 3, 1, 1, 1])

    with left:
        if goal["due"] == date.today() and not goal["done"] and not goal["failed"]:
            st.markdown(
                '<div style="display:inline-block;padding:2px 8px;border-radius:12px;'
                'background:#ffdd57;color:#000;font-size:12px;margin-right:6px;">Сегодня</div>',
                unsafe_allow_html=True,
            )
        st.write(
            f"{status} **{goal['title']}** · {goal['type']} (±{reward} XP) · {goal['stat']} · "
            f"🏷️ {goal.get('category','')}"
        )

    time_str = goal.get("time")
    time_part = f" • ⏰ {time_str}" if time_str else ""
    mid.caption(f"📅 {goal['due'].strftime('%d-%m-%Y')}{time_part} • {days_left_text(goal['due'], time_str)}")


    uid = goal_uid(goal)

    if not goal["done"] and not goal["failed"]:
        if b1.button("✅", key=f"{scope}_done_{uid}_{idx}", use_container_width=True, help="Выполнить"):
            if goal.get("recur_mode", "none") != "none":
                award_xp_for_goal(goal, True)
                goal["due"] = compute_next_due(goal)
                goal["type"] = classify_by_due(goal["due"])
            else:
                goal["done"] = True
                award_xp_for_goal(goal, True)
            save_state()
            st.rerun()

        if b2.button("❌", key=f"{scope}_fail_{uid}_{idx}", use_container_width=True, help="Провалить"):
            if goal.get("recur_mode", "none") != "none":
                award_xp_for_goal(goal, False)
                goal["due"] = compute_next_due(goal)
                goal["type"] = classify_by_due(goal["due"])
            else:
                goal["failed"] = True
                award_xp_for_goal(goal, False)
            save_state()
            st.rerun()

    if b3.button("🗑️", key=f"{scope}_del_{uid}_{idx}", use_container_width=True, help="Удалить задачу"):
        st.session_state.goals = [g for g in st.session_state.goals if g is not goal]
        save_state()
        st.rerun()

def render_list(goals, scope: str):
    if not goals:
        st.caption("Нет задач в этом списке.")
        return
    goals = sorted(goals, key=lambda g: goal_due_datetime(g))
    for i, g in enumerate(goals):
        row(g, scope, i)


# ========================= ФОРМА ДОБАВЛЕНИЯ =========================
def render_add_task_form(suffix: str = ""):
    """Форма для добавления новой задачи (ключи уникальны за счёт suffix)"""

    with st.form(f"add_goal_form{suffix}", clear_on_submit=True):
        st.subheader("➕ Добавить задачу")

        # Поля формы
        title = st.text_input("Название задачи", key=f"title{suffix}")
        due_input = st.date_input("Дедлайн", value=date.today(), key=f"due{suffix}")
        characteristic = st.selectbox(
            "Какая характеристика качается:",
            ["Здоровье ❤️", "Интеллект 🧠", "Радость 🙂", "Отношения 🤝", "Успех ⭐", "Дисциплина 🎯"],
            key=f"char{suffix}"
        )
        category = st.selectbox(
            "Категория:",
            ["Работа", "Учёба", "Дом", "Здоровье", "Хобби", "Другое"],
            key=f"cat{suffix}"
        )

        # Режим повторения
        RECUR_OPTIONS = {
            "Не повторять": "none",
            "Ежедневно": "daily",
            "Еженедельно": "weekly",
            "По дням недели": "by_days"
        }
        recur_mode_label = st.selectbox(
            "Повторение:",
            list(RECUR_OPTIONS.keys()),
            index=0,
            key=f"recur_mode{suffix}"
        )
        mode_key = RECUR_OPTIONS[recur_mode_label]

        recur_days = []
        if mode_key == "by_days":
            st.markdown("**Выберите дни недели:**")
            checks = []
            cols = st.columns(7)
            for i, day in enumerate(["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]):
                with cols[i]:
                    checked = st.checkbox(day, key=f"day_{i}{suffix}")
                    if checked:
                        checks.append(i)
            recur_days = checks
        
        use_time = st.checkbox("Указать время", key=f"use_time{suffix}")
        tvalue = None
        if use_time:
            t = st.time_input("Время дедлайна", key=f"time{suffix}")
            # st.time_input возвращает datetime.time — сохраним строкой HH:MM
            tvalue = f"{t.hour:02d}:{t.minute:02d}"

        submitted = st.form_submit_button("Добавить задачу", use_container_width=True, key=f"submit{suffix}")

        if submitted:
            if not title.strip():
                st.error("❌ Нужно ввести название задачи")
            else:
                new_goal = {
                    "title": title.strip(),
                    "due": due_input if isinstance(due_input, date) else date.fromisoformat(str(due_input)),
                    "type": classify_by_due(due_input),
                    "category": category,
                    "done": False,
                    "failed": False,
                    "overdue": False,
                    "stat": characteristic,
                    "recur_mode": mode_key,
                    "recur_days": recur_days,
                    "time": tvalue,  # <— НОВОЕ (может быть None)
}


                st.session_state.goals.append(new_goal)
                save_state()
                st.success(f"✅ Задача '{title}' добавлена!")
                st.rerun()

# ========================= ВИЗУАЛИЗАЦИЯ =========================
def pie_from_counter(counter_like, title="Диаграмма"):
    """Пончиковая диаграмма через Altair с защитой от пустых данных."""
    alt.data_transformers.disable_max_rows()
    try:
        items = list(counter_like.items())
    except AttributeError:
        items = list(counter_like)

    df = pd.DataFrame(items, columns=["label", "value"])
    if df.empty or (df["value"].fillna(0).astype(float).sum() == 0):
        st.caption(f"ℹ️ Нет данных для: {title}.")
        return

    df = df[df["value"].fillna(0).astype(float) > 0]
    chart = (
        alt.Chart(df)
        .mark_arc(innerRadius=60)
        .encode(
            theta=alt.Theta("value:Q", stack=True),
            color=alt.Color("label:N", legend=alt.Legend(title=None)),
            tooltip=[alt.Tooltip("label:N", title="Категория"), alt.Tooltip("value:Q", title="Значение")],
        )
        .properties(title=title)
    )
    st.altair_chart(chart, use_container_width=True)


def _xp_last_7_days_df():
    """Возвращает DataFrame (date, XP) за последние 7 дней.
    Поддерживает dict {'DD-MM-YYYY': delta} или список пар [(date, delta), ...].
    """
    xp_src = st.session_state.get("xp_log")
    if xp_src is None:
        return None

    if isinstance(xp_src, dict):
        df = pd.DataFrame({"date": list(xp_src.keys()), "XP": list(xp_src.values())})
    else:
        try:
            df = pd.DataFrame(xp_src, columns=["date", "XP"])
        except Exception:
            return None

    df["date"] = pd.to_datetime(df["date"]).dt.date
    df = df.groupby("date", as_index=False)["XP"].sum()

    idx = pd.date_range(end=pd.Timestamp.today().normalize(), periods=7, freq="D").date
    s = pd.Series(0.0, index=idx)
    s.update(pd.Series(df["XP"].values, index=df["date"]))
    return pd.DataFrame({"date": pd.to_datetime(s.index), "XP": s.values})


def render_progress_section():
    """Секция визуализации: 2 пончика + линия XP."""
    st.markdown("## 📈 Визуализация прогресса")

    goals = st.session_state.get("goals", [])
    done_tasks = [g for g in goals if g.get("done")]

    col1, col2 = st.columns(2)
    with col1:
        by_type = Counter(g.get("type", "Неизв.") for g in done_tasks)
        pie_from_counter(by_type, "Выполненные по длительности")
    with col2:
        by_cat = Counter(g.get("category", "Прочее") for g in done_tasks)
        pie_from_counter(by_cat, "Выполненные по категориям")

    st.divider()
    xp_df = _xp_last_7_days_df()
    if xp_df is not None and not xp_df.empty:
        st.markdown("#### XP за последние 7 дней")
        st.line_chart(xp_df, x="date", y="XP", use_container_width=True)
    else:
        st.caption("ℹ️ Нет данных для графика XP за 7 дней.")


# ========================= СТРАНИЦЫ =========================
def award_xp_for_goal(goal: dict, success: bool):
    """
    Вычисляет награду/штраф для goal и применяет её:
      - add_xp(+reward) или add_xp(-reward)
      - обновляет соответствующую характеристику и дисциплину
    """
    reward = GOAL_TYPES.get(goal.get("type", "Краткосрочная"), 5)
    stat_name = goal.get("stat", "Успех ⭐")

    if success:
        add_xp(reward)
        update_stat(stat_name, +1)
        update_stat("Дисциплина 🎯", +0.1)
    else:
        add_xp(-reward)
        update_stat(stat_name, -1)
        update_stat("Дисциплина 🎯", -0.1)

def award_big_goal_completion():
    """+250 XP и +10 ко всем характеристикам за выполнение глобальной цели."""
    add_xp(BIG_GOAL_XP)
    for k in list(st.session_state.stats.keys()):
        update_stat(k, +BIG_GOAL_STAT_BONUS)

def render_home_page():
    """Главная страница"""

    render_levelup_modal()
    st.header("🏠 Главная")
    render_year_reset_modal()

        # --- Счётчики ---
    goals = st.session_state.get("goals", [])
    today = date.today()

    today_count = sum(1 for g in goals if g["due"] == today and not g["done"] and not g["failed"])
    active_total = sum(1 for g in goals if not g["done"] and not g["failed"])

    # стиль «пилюлек»
    st.markdown("""
    <style>
    .counters { display:flex; gap:10px; flex-wrap:wrap; margin-bottom: 8px; }
    .pill {
      display:inline-block; padding:6px 12px; border-radius:999px;
      background: rgba(255,255,255,0.07); border:1px solid rgba(255,255,255,0.12);
      font-size:14px; color:#eaeaea;
    }
    .pill-today { background:#ffdd57; color:#111; border-color:#e6c84f; }
    </style>
    """, unsafe_allow_html=True)

    st.markdown(
        f"<div class='counters'>"
        f"<span class='pill pill-today'>Сегодня: <b>{today_count}</b></span>"
        f"<span class='pill'>Активных всего: <b>{active_total}</b></span>"
        f"</div>",
        unsafe_allow_html=True
    )

    # --- Характеристики и опыт ---
    st.subheader("📊 Характеристики")
    cols = st.columns(6)
    stats = st.session_state.stats
    keys = ["Здоровье ❤️", "Интеллект 🧠", "Радость 🙂", "Отношения 🤝", "Успех ⭐", "Дисциплина 🎯"]
    for i, k in enumerate(keys):
        with cols[i]:
            st.metric(k, f"{stats.get(k, 0):.1f}")

    xp = st.session_state.xp
    level = st.session_state.level
    st.markdown(f"**Опыт (XP):** {xp} / 1000 &nbsp;&nbsp;|&nbsp;&nbsp; **Уровень:** {level}")
    st.progress(min(1.0, (xp % 1000) / 1000))

    st.divider()

    # --- Задачи на сегодня (карточки) ---
    render_today_tasks_section()
    st.divider()

    # --- Добавить задачу (раскрывающаяся форма) ---
    st.subheader("➕ Добавить задачу")

    if "show_add_form" not in st.session_state:
        st.session_state.show_add_form = False

    if not st.session_state.show_add_form:
        if st.button("➕ Открыть форму", key="open_add_form_home"):
            st.session_state.show_add_form = True
            st.rerun()
    else:
        render_add_task_form(suffix="_home")
        if st.button("🔽 Скрыть форму", key="hide_add_form_home"):
            st.session_state.show_add_form = False
            st.rerun()

    st.divider()

    # --- Активные задачи (по типам) ---
    st.subheader("🟢 Активные задачи")

    active = [g for g in st.session_state.goals if not g["done"] and not g["failed"]]
    # теперь НЕ исключаем сегодняшние — они тоже попадают в «Активные»
    active_rest = active

    short = [g for g in active_rest if g["type"] == "Краткосрочная"]
    mid   = [g for g in active_rest if g["type"] == "Среднесрочная"]
    long  = [g for g in active_rest if g["type"] == "Долгосрочная"]

    st.markdown(f"#### ⏱️ Краткосрочные ({len(short)})")
    render_list(short, "active_short")

    st.markdown(f"#### 📆 Среднесрочные ({len(mid)})")
    render_list(mid, "active_mid")

    st.markdown(f"#### 🗓️ Долгосрочные ({len(long)})")
    render_list(long, "active_long")

def render_levelup_modal():
    """Кросс-версия модалки «Новый уровень»: st.dialog если есть, иначе — псевдо-модалка."""
    if not st.session_state.get("levelup_pending"):
        return

    # Вариант 1: если Streamlit поддерживает st.dialog
    if hasattr(st, "dialog"):
        @st.dialog("🎉 Новый уровень!")
        def _levelup_dialog():
            to_lvl = st.session_state.get("levelup_to", st.session_state.get("level", 1))
            st.markdown(f"## Поздравляю! Достигнут уровень **{to_lvl}** 🚀")
            st.write("Ты стал(а) сильнее. Продолжаем путь!")
            if st.button("Только вперёд 💪", use_container_width=True, key="close_levelup_dialog"):
                st.session_state.levelup_pending = False
                save_state()
                st.rerun()
        _levelup_dialog()
        return

    # Вариант 2: fallback — «псевдо-модалка» (если st.dialog недоступен)
    st.markdown("""
    <style>
      .lvlup-overlay {
        position: fixed; inset: 0; background: rgba(0,0,0,0.55);
        display: flex; align-items: center; justify-content: center;
        z-index: 9999;
      }
      .lvlup-modal {
        width: min(520px, 92vw);
        background: #111; color: #fff; border: 1px solid #333;
        border-radius: 16px; padding: 24px;
        box-shadow: 0 20px 60px rgba(0,0,0,0.6);
      }
      .lvlup-title { font-size: 24px; margin: 0 0 8px 0; }
      .lvlup-sub { opacity: .85; margin-bottom: 18px; }
    </style>
    """, unsafe_allow_html=True)

    to_lvl = st.session_state.get("levelup_to", st.session_state.get("level", 1))
    st.markdown(
        f"""
        <div class="lvlup-overlay">
          <div class="lvlup-modal">
            <div class="lvlup-title">🎉 Новый уровень!</div>
            <div class="lvlup-sub">Поздравляю! Достигнут уровень <b>{to_lvl}</b> 🚀<br/>Ты стал(а) сильнее. Продолжаем путь!</div>
          </div>
        </div>
        """,
        unsafe_allow_html=True
    )

    # Кнопку рендерим «поверх» — рядом, но логически относится к модалке
    if st.button("Только вперёд 💪", key="close_levelup_fallback", use_container_width=True):
        st.session_state.levelup_pending = False
        save_state()
        st.rerun()

def render_today_tasks_section():
    """Красивые карточки задач на сегодня с кнопками ✔ / ✖."""
    st.subheader("📅 Задачи на сегодня")

    today = date.today()
    today_tasks = [
        g for g in st.session_state.goals
        if g["due"] == today and not g["done"] and not g["failed"]
    ]

    st.markdown("""
    <style>
    .task-card {
        padding: 12px 14px; border: 1px solid rgba(255,255,255,0.08);
        border-radius: 12px; margin-bottom: 10px; background: rgba(255,255,255,0.03);
    }
    .task-row { display:flex; align-items:center; gap:10px; }
    .task-left { flex: 1; }
    .badge {
        display:inline-block; padding: 2px 8px; border-radius: 999px; font-size: 12px;
        background: #2a2a2a; color: #eaeaea; margin-right: 6px;
        border: 1px solid rgba(255,255,255,0.1);
    }
    .badge.today { background: #ffdd57; color:#000; border-color:#e6c84f; }
    .title { font-weight: 600; }
    .meta { color:#bbb; font-size:13px; margin-top:2px; }
    </style>
    """, unsafe_allow_html=True)

    if not today_tasks:
        st.info("Сегодня задач нет! 🎉")
        return

    for g in sorted(today_tasks, key=lambda x: x.get("category", "")):
        uid = goal_uid(g)
        reward = GOAL_TYPES.get(g["type"], 5)
        due_str = g["due"].strftime("%d-%m-%Y")
        time_str = g.get("time")
        time_part = f" ⏰ {time_str}" if time_str else ""
        status_tail = days_left_text(g["due"], time_str)
        ...
        f'    <div class="meta">{g.get("stat","")} • дедлайн: {due_str}{time_part} • {status_tail}</div>'


        with st.container():
            st.markdown('<div class="task-card">', unsafe_allow_html=True)

            c_left, c_done, c_fail = st.columns([8, 1, 1])

            with c_left:
                st.markdown(
                    f'<div class="task-row">'
                    f'  <div class="task-left">'
                    f'    <span class="badge today">Сегодня</span>'
                    f'    <span class="badge">{g["type"]}</span>'
                    f'    <span class="badge">🏷️ {g.get("category","")}</span>'
                    f'    <span class="badge">±{reward} XP</span>'
                    f'    <div class="title">{g["title"]}</div>'
                    f'    <div class="meta">{g.get("stat","")} • дедлайн: {due_str}</div>'
                    f'  </div>'
                    f'</div>',
                    unsafe_allow_html=True
                )

            with c_done:
                if st.button("✅", key=f"today_done_{uid}", use_container_width=True, help="Выполнить"):
                    if g.get("recur_mode", "none") != "none":
                        award_xp_for_goal(g, True)
                        g["due"] = compute_next_due(g)
                        g["type"] = classify_by_due(g["due"])
                    else:
                        g["done"] = True
                        award_xp_for_goal(g, True)
                    save_state()
                    st.rerun()

            with c_fail:
                if st.button("❌", key=f"today_fail_{uid}", use_container_width=True, help="Провалить"):
                    if g.get("recur_mode", "none") != "none":
                        award_xp_for_goal(g, False)
                        g["due"] = compute_next_due(g)
                        g["type"] = classify_by_due(g["due"])
                    else:
                        g["failed"] = True
                        award_xp_for_goal(g, False)
                    save_state()
                    st.rerun()

            st.markdown('</div>', unsafe_allow_html=True)

# ====== ПОЛНАЯ СТАТИСТИКА ======

def _current_and_best_streak() -> tuple[int, int]:
    """
    Считаем 'серии без пропусков' на основе discipline_awarded_dates:
    день считается успешным, если вчера (или дата из списка) был выполнен весь план (задачи+привычки).
    current — текущая серия до вчера включительно.
    best — лучшая серия за всё время.
    """
    dates = sorted(set(st.session_state.get("discipline_awarded_dates", [])))
    if not dates:
        return 0, 0

    # Преобразуем в объекты date
    from datetime import datetime, timedelta
    ds = [datetime.fromisoformat(d).date() for d in dates]

    # Лучшая серия
    best = 0
    cur = 1
    for i in range(1, len(ds)):
        if (ds[i] - ds[i-1]).days == 1:
            cur += 1
        else:
            best = max(best, cur)
            cur = 1
    best = max(best, cur)

    # Текущая серия до вчера
    yesterday = date.today() - timedelta(days=1)
    # найдём хвост последовательности, оканчивающейся на yesterday
    if ds[-1] != yesterday:
        current = 0
    else:
        current = 1
        i = len(ds) - 1
        while i > 0 and (ds[i] - ds[i-1]).days == 1:
            current += 1
            i -= 1

    return current, best


def _goals_stats():
    goals = st.session_state.get("goals", [])
    # по типам
    by_type = {"Краткосрочная": 0, "Среднесрочная": 0, "Долгосрочная": 0}
    # по категориям
    by_cat = {}
    done_cnt = 0
    fail_cnt = 0
    overdue_cnt = 0
    active_cnt = 0

    for g in goals:
        by_type[g["type"]] = by_type.get(g["type"], 0) + 1
        cat = g.get("category", "Прочее")
        by_cat[cat] = by_cat.get(cat, 0) + 1
        if g["done"]:
            done_cnt += 1
        elif g["failed"]:
            fail_cnt += 1
            if g.get("overdue"):
                overdue_cnt += 1
        else:
            active_cnt += 1

    return {
        "by_type": by_type,
        "by_cat": by_cat,
        "done": done_cnt,
        "failed": fail_cnt,
        "overdue": overdue_cnt,
        "active": active_cnt,
        "total": len(goals),
    }


def _big_goals_stats():
    bgs = st.session_state.get("big_goals", [])
    total = len(bgs)
    done = sum(1 for x in bgs if x.get("done"))
    failed = sum(1 for x in bgs if x.get("failed"))
    active = total - done - failed
    # по дедлайнам ближайшее/прошедшее
    past_due = sum(1 for x in bgs if (not x.get("done") and not x.get("failed") and x["due"] < date.today()))
    return {
        "total": total, "done": done, "failed": failed, "active": active, "past_due": past_due
    }


def _habits_stats():
    habits = st.session_state.get("habits", [])
    total = len(habits)
    # суммарные выполнения/провалы
    total_done = sum(len(h.get("completions", [])) for h in habits)
    total_fail = sum(len(h.get("failures", [])) for h in habits)
    # средняя «успешность» по привычкам
    per_habit = []
    for h in habits:
        d = len(h.get("completions", []))
        f = len(h.get("failures", []))
        attempts = d + f
        rate = (d / attempts * 100) if attempts > 0 else 0.0
        per_habit.append((h["title"], d, f, rate))
    return {
        "total": total,
        "total_done": total_done,
        "total_fail": total_fail,
        "per_habit": per_habit,
    }


def _xp_last_7_days():
    # возвращает список (date_str, delta_xp) за последние 7 дней
    logs = st.session_state.get("xp_log", {})
    from datetime import timedelta
    out = []
    for i in range(6, -1, -1):
        d = (date.today() - timedelta(days=i))
        k = d.isoformat()  # в логах ключи ISO
        out.append((d.strftime("%d-%m-%Y"), int(logs.get(k, 0))))
    return out


def _week_distribution_from_dates(date_strs: list[str]) -> dict:
    """Распределение по дням недели (0..6) из списка 'YYYY-MM-DD'."""
    counts = {i: 0 for i in range(7)}
    for s in date_strs:
        try:
            d = date.fromisoformat(s)
            counts[d.weekday()] += 1
        except Exception:
            pass
    return counts

def _habits_week_success() -> dict:
    """
    Возвращает словарь {weekday_index: success_rate_percent}
    где success_rate = completions / (completions + failures) * 100.
    Считается по ВСЕМ привычкам суммарно.
    """
    habits = st.session_state.get("habits", [])
    comp = {i: 0 for i in range(7)}
    fail = {i: 0 for i in range(7)}
    for h in habits:
        for s in h.get("completions", []):
            try:
                w = date.fromisoformat(s).weekday()
                comp[w] += 1
            except Exception:
                pass
        for s in h.get("failures", []):
            try:
                w = date.fromisoformat(s).weekday()
                fail[w] += 1
            except Exception:
                pass
    rate = {}
    for i in range(7):
        total = comp[i] + fail[i]
        rate[i] = round((comp[i] / total * 100.0), 1) if total > 0 else 0.0
    return rate

def _goals_category_success() -> list[tuple[str, int, int, float]]:
    """
    Считает успешность по категориям задач:
    возвращает список [(категория, done, failed, success%)].
    """
    goals = st.session_state.get("goals", [])
    by_cat_done = {}
    by_cat_fail = {}
    for g in goals:
        cat = g.get("category", "Прочее")
        if g.get("done"):
            by_cat_done[cat] = by_cat_done.get(cat, 0) + 1
        elif g.get("failed"):
            by_cat_fail[cat] = by_cat_fail.get(cat, 0) + 1
    cats = sorted(set(list(by_cat_done.keys()) + list(by_cat_fail.keys())))
    out = []
    for c in cats:
        d = by_cat_done.get(c, 0)
        f = by_cat_fail.get(c, 0)
        total = d + f
        rate = round((d / total * 100.0), 1) if total > 0 else 0.0
        out.append((c, d, f, rate))
    # сортируем по успешности убыв.
    out.sort(key=lambda x: x[3], reverse=True)
    return out

def _xp_last_30_days_summary():
    """Средний XP за 30 дней и топ-3 дня по XP."""
    logs = st.session_state.get("xp_log", {})
    from datetime import timedelta
    vals = []
    for i in range(29, -1, -1):
        d = (date.today() - timedelta(days=i))
        k = d.isoformat()
        vals.append((d, int(logs.get(k, 0))))
    avg = round(sum(v for _, v in vals) / len(vals), 1) if vals else 0.0
    top3 = sorted(vals, key=lambda t: t[1], reverse=True)[:3]
    # форматируем
    top3_fmt = [(d.strftime("%d-%m-%Y"), v) for d, v in top3]
    return avg, top3_fmt

def render_full_stats():
    """Большой блок 'Полная статистика' в профиле."""
    st.markdown("### 📈 Полная статистика")

    # 1) Стрики дисциплины
    cur_streak, best_streak = _current_and_best_streak()
    c1, c2, c3 = st.columns(3)
    c1.metric("Текущая серия без пропусков (дней)", cur_streak)
    c2.metric("Лучшая серия (дней)", best_streak)
    c3.metric("Дней с +1 к дисциплине всего", len(set(st.session_state.get("discipline_awarded_dates", []))))

    st.divider()

    # 2) Задачи (обычные)
    gstats = _goals_stats()
    st.markdown("#### ✅ Задачи")
    cA, cB, cC, cD, cE = st.columns(5)
    cA.metric("Всего", gstats["total"])
    cB.metric("Активных", gstats["active"])
    cC.metric("Выполнено", gstats["done"])
    cD.metric("Провалено", gstats["failed"])
    cE.metric("Просрочено", gstats["overdue"])

    col1, col2 = st.columns(2)
    with col1:
        st.caption("По типам")
        st.bar_chart(gstats["by_type"])
    with col2:
        st.caption("По категориям (число задач)")
        if gstats["by_cat"]:
            st.bar_chart(gstats["by_cat"])
        else:
            st.info("Категорий пока нет.")

    # ➕ Новое: успешность по категориям
    cat_succ = _goals_category_success()
    if cat_succ:
        st.caption("Успешность по категориям (выполнено/провалено, % успеха)")
        st.dataframe(
            [{"Категория": c, "Выполнено": d, "Провалено": f, "Успех, %": r} for (c,d,f,r) in cat_succ],
            use_container_width=True,
            hide_index=True
        )

    st.divider()

    # 3) Глобальные цели
    bg = _big_goals_stats()
    st.markdown("#### 🎯 Глобальные цели")
    c1, c2, c3, c4, c5 = st.columns(5)
    c1.metric("Всего", bg["total"])
    c2.metric("Активных", bg["active"])
    c3.metric("Выполнено", bg["done"])
    c4.metric("Провалено", bg["failed"])
    c5.metric("С дедлайном в прошлом", bg["past_due"])

    st.divider()

    # 4) Привычки
    hst = _habits_stats()
    st.markdown("#### 📆 Привычки")
    c1, c2, c3 = st.columns(3)
    c1.metric("Всего привычек", hst["total"])
    c2.metric("Выполнений всего", hst["total_done"])
    c3.metric("Провалов всего", hst["total_fail"])

    # ➕ Новое: успешность по дням недели
    week_rate = _habits_week_success()
    if any(v > 0 for v in week_rate.values()):
        st.caption("Успешность привычек по дням недели, %")
        week_series = {WEEKDAY_LABELS[i]: week_rate[i] for i in range(7)}
        st.bar_chart(week_series)
    else:
        st.info("Пока нет выполнений/провалов привычек для расчёта успешности по дням недели.")

    # Табличка по каждой привычке (успех % уже был)
    if hst["per_habit"]:
        rows = [{"Привычка": n, "Выполнено": d, "Провалов": f, "Успех, %": round(r, 1)} for (n, d, f, r) in hst["per_habit"]]
        st.dataframe(rows, use_container_width=True, hide_index=True)
    else:
        st.info("Пока нет данных по привычкам.")

    st.divider()

    # 5) XP за 7 дней (как было)
    st.markdown("#### ⭐ XP за последние 7 дней")
    xp7 = _xp_last_7_days()
    st.bar_chart({d: v for (d, v) in xp7})

    # ➕ Новое: Средний XP за 30 дней и Топ-3 дня
    avg30, top3 = _xp_last_30_days_summary()
    c1, c2 = st.columns(2)
    c1.metric("Средний XP за 30 дней", avg30)
    if top3:
        c2.markdown("**Топ-3 дня по XP**")
        c2.table([{"Дата": d, "XP": v} for d, v in top3])

def render_profile_page():
    """Страница профиля"""

    render_levelup_modal()

    render_year_reset_modal()

    st.header("👤 Профиль")

    # --- Характеристики ---
    st.subheader("📊 Характеристики")

    cols = st.columns(6)
    stats = st.session_state.stats
    keys = ["Здоровье ❤️", "Интеллект 🧠", "Радость 🙂", "Отношения 🤝", "Успех ⭐", "Дисциплина 🎯"]
    for i, k in enumerate(keys):
        with cols[i]:
            st.metric(k, f"{stats.get(k, 0):.1f}")

    st.divider()

    # --- Опыт и уровень ---
    st.subheader("⭐ Прогресс")

    xp = st.session_state.xp
    level = st.session_state.level
    st.markdown(f"**Опыт (XP):** {xp} / 1000 &nbsp;&nbsp;|&nbsp;&nbsp; **Уровень:** {level}")
    st.progress(xp % 1000 / 1000)

    st.divider()

    # --- Визуализация (по кнопке) ---
    if "show_visual" not in st.session_state:
        st.session_state.show_visual = False

    if not st.session_state.show_visual:
        if st.button("📊 Показать визуализацию", key="show_visual_btn"):
            st.session_state.show_visual = True
            st.rerun()
    else:
        render_progress_section()
        if st.button("🔽 Скрыть визуализацию", key="hide_visual_btn"):
            st.session_state.show_visual = False
            st.rerun()

# Кнопка показать/скрыть полную статистику
    if "show_full_stats" not in st.session_state:
        st.session_state.show_full_stats = False

    btn_lbl = "📊 Показать полную статистику" if not st.session_state.show_full_stats else "🔽 Скрыть статистику"
    if st.button(btn_lbl, key="toggle_full_stats"):
        st.session_state.show_full_stats = not st.session_state.show_full_stats
        st.rerun()

    if st.session_state.show_full_stats:
        render_full_stats()
        st.divider()
        
def render_goals_page():
    """🎯 Глобальные цели (годовые)"""
    st.header("🎯 Глобальные цели")
    render_levelup_modal()

    render_year_reset_modal()

    st.caption("Это большие цели, например, на год. За выполнение: **+250 XP** и **+10 ко всем показателям**.")

    # --- показать/скрыть форму добавления ---
    if "show_big_goal_form" not in st.session_state:
        st.session_state.show_big_goal_form = False

    if st.button("➕ Добавить цель", key="btn_show_big_goal_form"):
        st.session_state.show_big_goal_form = not st.session_state.show_big_goal_form
        st.rerun()

    if st.session_state.show_big_goal_form:
        with st.form("add_big_goal_form", clear_on_submit=True):
            title = st.text_input("Название глобальной цели", placeholder="Например: Выучить английский до B2")
            due = st.date_input("Дедлайн", value=date.today().replace(month=12, day=31))
            note = st.text_area("Описание / критерии успеха (по желанию)")
            submitted = st.form_submit_button("Сохранить")

            if submitted:
                if not title.strip():
                    st.warning("Введите название цели.")
                else:
                    st.session_state.big_goals.append({
                        "title": title.strip(),
                        "due": due if isinstance(due, date) else date.fromisoformat(str(due)),
                        "done": False,
                        "failed": False,
                        "note": note.strip(),
                    })
                    save_state()
                    st.success("Глобальная цель добавлена!")
                    st.session_state.show_big_goal_form = False
                    st.rerun()

    st.divider()

    # --- список глобальных целей ---
    goals = st.session_state.get("big_goals", [])
    if not goals:
        st.info("Пока нет глобальных целей. Добавьте первую выше 👆")
        return

    goals = sorted(goals, key=lambda g: g["due"])

    for g in goals:
        uid = big_goal_uid(g)
        status = "✅ Выполнена" if g["done"] else ("❌ Провалена" if g["failed"] else "🟡 В процессе")
        due_str = g["due"].strftime("%d-%m-%Y")

        with st.container():
            c1, c2, c3, c4 = st.columns([6, 2, 1, 1])

            with c1:
                st.markdown(f"**{g['title']}**")
                meta = f"📅 Дедлайн: {due_str} • Статус: {status}"
                if g.get("note"):
                    meta += f" • 📝 {g['note']}"
                st.caption(meta)

            with c2:
                if not g["done"] and not g["failed"]:
                    done_btn = st.button("✅ Выполнить", key=f"big_done_{uid}")
                    fail_btn = st.button("❌ Провалить", key=f"big_fail_{uid}")
                else:
                    done_btn = fail_btn = False

            with c3:
                if st.button("🗑️", key=f"big_del_{uid}", help="Удалить цель"):
                    st.session_state.big_goals = [x for x in st.session_state.big_goals if x is not g]
                    save_state()
                    st.rerun()

            with c4:
                new_due = st.date_input("Новый дедлайн", value=g["due"], key=f"big_due_{uid}")
                if new_due != g["due"]:
                    g["due"] = new_due
                    save_state()

            # обработка выполнения/провала
            if done_btn:
                g["done"] = True
                award_big_goal_completion()
                save_state()
                st.success("Поздравляю! Большая цель достигнута 🎉")
                st.rerun()

            if fail_btn:
                g["failed"] = True
                award_big_goal_failure()
                save_state()
                st.warning("Цель помечена как проваленная. Штраф применён.")
                st.rerun()

def render_habits_page():
    st.header("📆 Трекер привычек")

    render_levelup_modal()

    render_year_reset_modal()

    if "show_habit_form" not in st.session_state:
        st.session_state.show_habit_form = False

    if st.button("➕ Добавить привычку", key="btn_show_habit_form"):
        st.session_state.show_habit_form = not st.session_state.show_habit_form
        st.rerun()

    if st.session_state.show_habit_form:
        with st.form("add_habit_form", clear_on_submit=True):
            title = st.text_input("Название привычки", placeholder="Например: Утренняя зарядка")
            days = st.multiselect(
                "Дни недели",
                options=list(range(7)),
                default=[0, 1, 2, 3, 4],
                format_func=lambda i: WEEKDAY_LABELS[i]
            )
            stat = st.selectbox(
                "Какая характеристика качается:",
                ["Здоровье ❤️","Интеллект 🧠","Радость 🙂","Отношения 🤝","Успех ⭐","Дисциплина 🎯"]
            )
            submitted = st.form_submit_button("Сохранить")

            if submitted:
                if not title.strip():
                    st.warning("Введите название привычки.")
                elif not days:
                    st.warning("Выберите дни недели.")
                else:
                    st.session_state.habits.append({
                        "title": title.strip(),
                        "days": days[:],
                        "stat": stat,
                        "completions": [],
                        "failures": [],
                    })
                    save_state()
                    st.success("Привычка добавлена!")
                    st.session_state.show_habit_form = False
                    st.rerun()

    st.divider()

    # --- список привычек ---
    habits = st.session_state.get("habits", [])
    if not habits:
        st.info("Пока нет привычек. Добавьте первую выше 👆")
        return

    # Сегодняшний день
    today = date.today()
    today_label = WEEKDAY_LABELS[today.weekday()]

    for h in habits:
        uid = habit_uid(h)
        scheduled_today = is_habit_scheduled_today(h, today)
        done_today = habit_done_on_date(h, today)
        failed_today = habit_failed_on_date(h, today)

        # текстовый статус
        if not scheduled_today:
            status_label = "— сегодня не запланирована"
            status_style = "⚪"
        elif done_today:
            status_label = "выполнена ✅"
            status_style = "🟢"
        elif failed_today:
            status_label = "провалена ❌"
            status_style = "🔴"
        else:
            status_label = "ожидает выполнения"
            status_style = "🟡"

        # аккуратная строка
        c1, c2, c3, c4, c5 = st.columns([5, 3, 1, 1, 1])

        with c1:
            days_str = ", ".join(WEEKDAY_LABELS[i] for i in h.get("days", []))
            st.markdown(
                f"**{h['title']}** {status_style}  \n"
                f"Сегодня: {status_label}  \n"
                f"Дни: {days_str}  \n"
                f"Стат: {h.get('stat','')}"
            )

        with c2:
            if scheduled_today and not done_today and not failed_today:
                st.info(f"Запланирована на {today_label}")

        with c3:
            if st.button("✅", key=f"h_done_{uid}", help="Отметить выполненной сегодня", use_container_width=True):
                habit_mark_done(h, today)
                d = today.isoformat()
                if d in h["failures"]:
                    h["failures"].remove(d)
                save_state()
                st.rerun()

        with c4:
            if st.button("❌", key=f"h_fail_{uid}", help="Отметить проваленной сегодня", use_container_width=True):
                habit_mark_failed(h, today)
                d = today.isoformat()
                if d in h["completions"]:
                    h["completions"].remove(d)
                save_state()
                st.rerun()

        with c5:
            if st.button("🗑️", key=f"h_del_{uid}", help="Удалить привычку", use_container_width=True):
                st.session_state.habits = [x for x in st.session_state.habits if x is not h]
                save_state()
                st.rerun()

# ---------- ИНИЦИАЛИЗАЦИЯ СЕССИИ И АВТО-ПРОЦЕССОВ ----------
if "initialized" not in st.session_state:
    loaded = load_state_if_exists()
    if not loaded:
        
        st.session_state.goals = []
        st.session_state.big_goals = []
        st.session_state.habits = []
        st.session_state.xp = 0
        st.session_state.level = 1
        st.session_state.setdefault("levelup_pending", False)
        st.session_state.setdefault("levelup_to", st.session_state.get("level", 1))
        st.session_state.setdefault("last_reset_year", None)
        st.session_state.setdefault("year_reset_pending", False)       # показать модалку
        st.session_state.setdefault("yearly_report_path", None)        # путь к xlsx, если сохраним на диск (не обяз.)
        st.session_state.setdefault("yearly_report_year", None)
        st.session_state.stats = {
            "Здоровье ❤️": 0,
            "Интеллект 🧠": 0,
            "Радость 🙂": 0,
            "Отношения 🤝": 0,
            "Успех ⭐": 0,
            "Дисциплина 🎯": 0.0,
        }
        st.session_state.xp_log = {}
        st.session_state.discipline_awarded_dates = []
    else:
        # подстраховки для старых сохранений
        if "goals" not in st.session_state:
            st.session_state.goals = []
        if "big_goals" not in st.session_state or st.session_state.big_goals is None:
            st.session_state.big_goals = []
        if "habits" not in st.session_state or st.session_state.habits is None:
            st.session_state.habits = []
        if "xp" not in st.session_state:
            st.session_state.xp = 0
        if "level" not in st.session_state:
            st.session_state.level = 1
        if "stats" not in st.session_state:
            st.session_state.stats = {
                "Здоровье ❤️": 0,
                "Интеллект 🧠": 0,
                "Радость 🙂": 0,
                "Отношения 🤝": 0,
                "Успех ⭐": 0,
                "Дисциплина 🎯": 0.0,
            }
        if "xp_log" not in st.session_state or st.session_state.xp_log is None:
            st.session_state.xp_log = {}
        if "discipline_awarded_dates" not in st.session_state:
            st.session_state.discipline_awarded_dates = []

    # общие служебные вещи
    ensure_xp_log_dict()
    st.session_state.setdefault("page", "home")
    st.session_state.setdefault("show_add_form", False)
    st.session_state.setdefault("show_visual", False)
    st.session_state.initialized = True

    # служебные приведения типов/структур
    ensure_xp_log_dict()

    # UI-флаги по умолчанию
    st.session_state.setdefault("page", "home")          # <— ВАЖНО: текущая страница
    st.session_state.setdefault("show_add_form", False)  # форма добавления задачи на Главной
    st.session_state.setdefault("show_visual", False)    # показ визуализации на Профиле

    st.session_state.initialized = True

# авто-процессы (каждый запуск)
_ensure_discipline_list()
auto_process_overdues()              # штрафы/переносы по обычным задачам
auto_award_yesterday_if_ok()         # +1 дисциплина, если вчера всё выполнено
auto_process_big_goal_overdues()     # штраф/провал для просроченных глобальных целей
auto_check_yearly_reset()            # ⬅️ запуск годового сброса + отчёт

# страховка: всегда есть "page"
st.session_state.setdefault("page", "home")

# --- РОУТЕР ---
st.session_state.setdefault("page", "home")
page = st.session_state.page

if page == "home":
    render_home_page()
elif page == "profile":
    render_profile_page()
elif page == "goals":
    render_goals_page()
elif page == "habits":
    render_habits_page()


# ========================= НИЖНЯЯ НАВИГАЦИЯ =========================
st.markdown(
    """
<style>
.navbar {
  position: fixed; left: 0; right: 0; bottom: 0;
  padding: 10px 16px;
  background: rgba(32,32,32,0.9);
  border-top: 1px solid rgba(255,255,255,0.1);
  backdrop-filter: blur(6px);
  z-index: 9999;
}
.navbar .label { font-size: 14px; text-align: center; margin-top: 4px; color: #ddd; }
.navbar .active { color: #16c60c; font-weight: 700; }
</style>
""",
    unsafe_allow_html=True,
)
st.markdown('<div class="navbar">', unsafe_allow_html=True)
c1, c2, c3, c4 = st.columns(4)


def nav_button(col, label, icon, target, key):
    with col:
        pressed = st.button(f"{icon} {label}", key=key, use_container_width=True)
        cls = "label active" if st.session_state.page == target else "label"
        st.markdown(f'<div class="{cls}">{label}</div>', unsafe_allow_html=True)
    if pressed:
        st.session_state.page = target
        st.rerun()


nav_button(c1, "Главная", "🏠", "home", "nav_home")
nav_button(c2, "Профиль", "👤", "profile", "nav_profile")
nav_button(c3, "Цели", "🎯", "goals", "nav_goals")
nav_button(c4, "Привычки", "📆", "habits", "nav_habits")
st.markdown("</div>", unsafe_allow_html=True)


















