"""학교 정보업무 문의접수 (QR 진입 · 모바일 반응형 단일 화면).

입력 → ① 메일 발송(사진 첨부)  ② Google 시트에 1행 누적(헤더명 기준 매칭)
        + 접속 IP·기기·브라우저 등 자동 수집 항목 함께 기록 (없는 열은 자동 추가)
QR 링크에 ?school=학교명 을 붙이면 학교명이 미리 채워진다.

실행:  streamlit run app.py
배포:  GitHub → Streamlit Community Cloud

인증: 서비스계정(JSON 키) 방식 → refresh_token·로컬 설정 불필요.
      시트를 서비스계정 이메일에 '편집자'로 공유하면 끝.
Secrets:
  [smtp]                 user, password
  [gsheet]               sheet_id (URL 통째로 가능)
  [gcp_service_account]  서비스계정 JSON 키 내용
진단:  앱주소/?diag=1
"""

from __future__ import annotations

import base64
import collections
import datetime as dt
import io
import os
import re
import secrets as pysecrets
import smtplib
import time
from email.message import EmailMessage
from zoneinfo import ZoneInfo

import gspread
import streamlit as st
from google.oauth2.service_account import Credentials
from PIL import Image, ImageOps

try:  # 아이폰 HEIC 사진 지원
    from pillow_heif import register_heif_opener
    register_heif_opener()
except ImportError:
    pass

# =============================================================================
# 1. 설정
# =============================================================================
TITLE = "학교 정보업무 문의접수"
SUBTITLE = "장애 구분과 증상을 고르고, 연락처·사진을 남겨주세요"

KST = ZoneInfo("Asia/Seoul")

DEFAULT_TO = "1670-0570@skbroadband.com"
DEFAULT_HOST = "smtp.gmail.com"
DEFAULT_PORT = 587

MIN_MESSAGE_LEN = 8
MIN_SCHOOL_LEN = 2
MIN_PLACE_LEN = 2
PHONE_RE = re.compile(r"^010-\d{4}-\d{4}$")

MAX_EDGE = 1600
JPEG_QUALITY = 80

RETENTION = "처리 완료 후 3개월"
CONSENT = (
    "접수·회신 및 장애 원인 분석 목적으로 휴대전화번호·첨부 사진과 "
    "자동 수집 정보(접속 IP, 기기·브라우저 정보)를 수집하며, "
    f"{RETENTION}까지 보관 후 파기합니다."
)

SHEET_SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]
# 시트 헤더 정의. 기존 시트에 없는 항목은 오른쪽 끝에 자동 추가된다.
# 매칭 시 공백과 괄호 설명은 무시 → "문의 내용(8자 이상)" == "문의내용"
SHEET_HEADER = [
    # ── 사용자 입력 (기존 시트 열) ──
    "장애 구분", "증상 선택", "학교명", "장소", "휴대전화번호",
    "문의 내용(8자 이상)", "문제 화면 사진(1장 이상)",
    # ── 접수 관리 ──
    "접수번호", "접수일시", "첨부 방식", "메일 발송",
    # ── 자동 수집 (접속 환경) ──
    "접속 IP", "기기 종류", "운영체제", "브라우저", "기기 모델",
    "언어", "시간대", "진입 경로", "입력 소요(초)", "User-Agent",
]

# =============================================================================
# 2. 장애 분류 카탈로그  ← 증상 추가·삭제는 여기만 수정
# =============================================================================
WIRED, WIRELESS = "유선", "무선"
CATEGORIES = [WIRED, WIRELESS]

CATEGORY_DESC = {
    WIRED: "랜선으로 연결하는 업무망 · 에듀파인 등",
    WIRELESS: "와이파이 · 무선AP로 접속하는 인터넷",
}

SYMPTOMS: dict[str, list[dict]] = {
    WIRED: [
        {"label": "인터넷 끊어짐 (전혀 안 됨)", "note": "학교 전체인지, 특정 교실만인지 적어주세요."},
        {"label": "속도 느림", "note": "언제부터인지, 특정 시간대에만 그런지 적어주세요."},
        {"label": "간헐적으로 끊김", "note": "끊기는 주기와 지속 시간을 적어주세요."},
        {"label": "특정 사이트·서비스만 안 됨", "note": "해당 사이트 주소 또는 서비스명을 적어주세요."},
        {"label": "랜포트·랜선 불량 의심", "note": "교실 번호와 포트 번호를 적어주세요."},
        {"label": "기타 (내용에 직접 작성)", "note": "증상을 최대한 구체적으로 적어주세요."},
    ],
    WIRELESS: [
        {"label": "와이파이 신호가 안 잡힘", "note": "해당 교실·층과 SSID 이름을 적어주세요."},
        {"label": "연결은 되는데 인터넷이 안 됨", "note": "다른 기기도 동일한지 적어주세요."},
        {"label": "속도 느림", "note": "동시 접속 인원수를 함께 적어주세요."},
        {"label": "간헐적으로 끊김", "note": "특정 교실·시간대에 집중되는지 적어주세요."},
        {"label": "AP 램프 이상 (꺼짐·빨간불)", "note": "AP 위치와 램프 색을 적어주세요."},
        {"label": "특정 구역만 안 됨", "note": "되는 구역과 안 되는 구역을 구분해 적어주세요."},
        {"label": "기타 (내용에 직접 작성)", "note": "증상을 최대한 구체적으로 적어주세요."},
    ],
}


def symptom_labels(category: str) -> list[str]:
    return [s["label"] for s in SYMPTOMS.get(category, [])]


def symptom_note(category: str, label: str) -> str:
    for s in SYMPTOMS.get(category, []):
        if s["label"] == label:
            return s["note"]
    return ""


# =============================================================================
# 3. 공통 유틸 / 검증
# =============================================================================
def now_kst() -> dt.datetime:
    return dt.datetime.now(KST)


def make_receipt_id() -> str:
    return f"Q{now_kst():%y%m%d-%H%M%S}-{pysecrets.token_hex(2).upper()}"


def normalize_phone(raw: str) -> str:
    digits = re.sub(r"\D", "", raw or "")[:11]
    if len(digits) <= 3:
        return digits
    if len(digits) <= 7:
        return f"{digits[:3]}-{digits[3:]}"
    return f"{digits[:3]}-{digits[3:7]}-{digits[7:]}"


def validate(category, symptom, school, place, phone, message, photo_count, agreed) -> list[str]:
    errors: list[str] = []
    if not category:
        errors.append("유선 / 무선 중 하나를 선택해 주세요.")
    if not symptom:
        errors.append("증상을 선택해 주세요.")
    if len((school or "").strip()) < MIN_SCHOOL_LEN:
        errors.append("학교명을 입력해 주세요.")
    if len((place or "").strip()) < MIN_PLACE_LEN:
        errors.append("장소를 입력해 주세요. (예: 3층 교무실)")
    if not PHONE_RE.match(phone or ""):
        errors.append("휴대전화번호를 010-0000-0000 형식으로 입력해 주세요.")
    if len((message or "").strip()) < MIN_MESSAGE_LEN:
        errors.append(f"문의 내용을 {MIN_MESSAGE_LEN}자 이상 입력해 주세요.")
    if photo_count < 1:
        errors.append("문제 화면 사진을 1장 이상 첨부해 주세요.")
    if not agreed:
        errors.append("개인정보 수집·이용에 동의해 주세요.")
    return errors


def compress(raw: bytes) -> bytes:
    try:
        img = Image.open(io.BytesIO(raw))
        img = ImageOps.exif_transpose(img)
        if img.mode not in ("RGB", "L"):
            img = img.convert("RGB")
        img.thumbnail((MAX_EDGE, MAX_EDGE), Image.LANCZOS)
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=JPEG_QUALITY, optimize=True)
        return buf.getvalue()
    except Exception:
        return raw


def read_secrets(section: str) -> dict:
    try:
        return dict(st.secrets.get(section, {}))
    except Exception:
        return {}


def _pick(s: dict, key: str, env: str, default=None):
    v = s.get(key, os.environ.get(env, default))
    return v.strip() if isinstance(v, str) else v


def _mask_mail(v):
    if not v or "@" not in v:
        return "(미설정)" if not v else "설정됨"
    name, dom = v.split("@", 1)
    return f"{name[:2]}***@{dom}"


@st.cache_resource(show_spinner=False)
def error_log() -> collections.deque:
    """최근 오류 20건. 사용자에게는 숨기고 ?diag=1 에서만 확인."""
    return collections.deque(maxlen=20)


def record_error(where: str, exc: Exception) -> None:
    error_log().appendleft(
        f"{now_kst():%m-%d %H:%M:%S} [{where}] {type(exc).__name__}: {str(exc)[:300]}"
    )


# =============================================================================
# 3-1. 접속 환경 자동 수집 (서버가 받는 HTTP 헤더 기반, 별도 스크립트 없음)
#      화면 해상도·배터리·GPS 등은 브라우저 스크립트가 필요해 수집하지 않는다.
# =============================================================================
def _ctx(name: str, default=None):
    try:
        return getattr(st.context, name, default)
    except Exception:
        return default


def _header(name: str) -> str:
    try:
        return (st.context.headers.get(name) or "").strip()
    except Exception:
        return ""


def client_ip() -> str:
    xff = _header("X-Forwarded-For")
    if xff:
        return xff.split(",")[0].strip()
    return _header("X-Real-IP") or (_ctx("ip_address") or "")


def parse_ua(ua: str, ch_platform: str = "", ch_mobile: str = "") -> dict:
    """User-Agent → 기기 종류 / OS / 브라우저 / 모델. 외부 라이브러리 없이 정규식."""
    ua = ua or ""
    os_name = model = ""

    if m := re.search(r"Android\s*([\d.]+)?", ua):
        os_name = f"Android {m.group(1) or ''}".strip()
        mm = re.search(r"Android[^;)]*;\s*(?:[a-z]{2}[-_][A-Za-z]{2};\s*)?([^;)]+?)(?:\s+Build/|\))", ua)
        if mm:
            model = mm.group(1).strip()
    elif m := re.search(r"(iPhone|iPad|iPod).*?OS\s([\d_]+)", ua):
        os_name = f"iOS {m.group(2).replace('_', '.')}"
        model = m.group(1)
    elif m := re.search(r"Windows NT\s([\d.]+)", ua):
        os_name = {"10.0": "Windows 10/11", "6.3": "Windows 8.1", "6.1": "Windows 7"}.get(
            m.group(1), f"Windows NT {m.group(1)}")
    elif m := re.search(r"Mac OS X\s([\d_]+)", ua):
        os_name = f"macOS {m.group(1).replace('_', '.')}"
    elif "CrOS" in ua:
        os_name = "ChromeOS"
    elif "Linux" in ua:
        os_name = "Linux"
    if not os_name and ch_platform:
        os_name = ch_platform.strip('"')
    if model in ("K", "Linux", "U", "wv"):   # 크롬 UA 축소 정책으로 모델 비공개
        model = "(비공개)"

    # (UA 식별자, 표시명, 버전 토큰) — 인앱·파생 브라우저를 먼저 검사
    browsers = [
        ("KAKAOTALK", "카카오톡 인앱", "KAKAOTALK"), ("NAVER(", "네이버앱", "NAVER\\(inapp; search; \\d+; "),
        ("Whale", "웨일", "Whale"), ("SamsungBrowser", "삼성 인터넷", "SamsungBrowser"),
        ("EdgA", "Edge", "EdgA"), ("Edg/", "Edge", "Edg"), ("FxiOS", "Firefox", "FxiOS"),
        ("Firefox", "Firefox", "Firefox"), ("CriOS", "Chrome", "CriOS"),
        ("Chrome", "Chrome", "Chrome"), ("Safari", "Safari", "Version"),
    ]
    browser = ""
    for key, name, token in browsers:
        if key in ua:
            m = re.search(rf"{token}[/ ]([\d]+(?:\.\d+)?)", ua)
            browser = f"{name} {m.group(1)}" if m else name
            break
    if "; wv)" in ua and "인앱" not in browser:
        browser = f"{browser} (앱 내 웹뷰)".strip()

    if "iPad" in ua or ("Android" in ua and "Mobile" not in ua):
        kind = "태블릿"
    elif "Mobi" in ua or "iPhone" in ua or ch_mobile == "?1":
        kind = "모바일"
    elif ua:
        kind = "PC"
    else:
        kind = ""
    return {"kind": kind, "os": os_name, "browser": browser, "model": model}


def collect_client_meta() -> dict:
    ua = _header("User-Agent")
    info = parse_ua(ua, _header("Sec-CH-UA-Platform"), _header("Sec-CH-UA-Mobile"))
    lang = _ctx("locale") or _header("Accept-Language").split(",")[0]
    tz = _ctx("timezone") or ""
    try:
        qp = {k: v for k, v in st.query_params.to_dict().items() if k != "diag"}
    except Exception:
        qp = {}
    entry = "&".join(f"{k}={v}" for k, v in qp.items()) or "직접 접속"
    t0 = st.session_state.get("t0")
    elapsed = int(time.time() - t0) if t0 else ""
    return {
        "접속 IP": client_ip(),
        "기기 종류": info["kind"],
        "운영체제": info["os"],
        "브라우저": info["browser"],
        "기기 모델": info["model"],
        "언어": lang,
        "시간대": tz,
        "진입 경로": entry,
        "입력 소요(초)": elapsed,
        "User-Agent": ua[:400],
    }


# =============================================================================
# 4. 메일
# =============================================================================
class MailConfig:
    REQUIRED = {"host": "호스트", "user": "계정", "password": "앱 비밀번호",
                "sender": "발신주소", "to": "수신주소"}

    def __init__(self, s: dict | None = None):
        s = dict(s or {})
        self.host = _pick(s, "host", "SMTP_HOST", DEFAULT_HOST)
        self.port = int(_pick(s, "port", "SMTP_PORT", DEFAULT_PORT))
        self.user = _pick(s, "user", "SMTP_USER")
        pw = _pick(s, "password", "SMTP_PASSWORD")
        self.password = pw.replace(" ", "") if isinstance(pw, str) else pw
        self.sender = _pick(s, "sender", "SMTP_SENDER") or self.user
        self.to = _pick(s, "to", "SMTP_TO", DEFAULT_TO)
        self.use_ssl = str(_pick(s, "use_ssl", "SMTP_USE_SSL", "0")).lower() in ("1", "true")

    @property
    def missing(self) -> list[str]:
        return [ko for k, ko in self.REQUIRED.items() if not getattr(self, k)]

    @property
    def ready(self) -> bool:
        return not self.missing

    def masked(self) -> dict[str, str]:
        return {
            "호스트": f"{self.host}:{self.port}" + (" (SSL)" if self.use_ssl else " (STARTTLS)"),
            "계정": _mask_mail(self.user),
            "앱 비밀번호": f"설정됨 ({len(self.password)}자)" if self.password else "(미설정)",
            "수신주소": self.to or "(미설정)",
        }


def build_subject(rid, category, symptom, school, place) -> str:
    return f"[문의접수][{category}] {school.strip()} {place.strip()} - {symptom} ({rid})"


def build_body(rid, category, symptom, school, place, phone, message, n) -> str:
    return (
        "학교 정보업무 문의가 접수되었습니다.\n"
        "--------------------------------------------\n"
        f"접수 번호   : {rid}\n"
        f"접수 일시   : {now_kst():%Y-%m-%d %H:%M:%S} (KST)\n"
        f"학교명      : {school.strip()}\n"
        f"장소        : {place.strip()}\n"
        f"장애 구분   : {category}\n"
        f"증상        : {symptom}\n"
        f"회신 연락처 : {phone}\n"
        f"첨부 사진   : {n}장\n"
        "--------------------------------------------\n\n"
        "[문의 내용]\n"
        f"{message.strip()}\n\n"
        "--------------------------------------------\n"
        "본 메일은 QR 접수 페이지에서 자동 발송되었습니다.\n"
    )


def send_inquiry(cfg: MailConfig, subject: str, body: str,
                 photos: list[tuple[str, bytes]], timeout: int = 30) -> None:
    mail = EmailMessage()
    mail["Subject"] = subject
    mail["From"] = cfg.sender
    mail["To"] = cfg.to
    mail["Reply-To"] = cfg.sender
    mail.set_content(body)
    for idx, (name, data) in enumerate(photos, start=1):
        mail.add_attachment(data, maintype="image", subtype="jpeg",
                            filename=name or f"photo_{idx}.jpg")
    smtp_cls = smtplib.SMTP_SSL if cfg.use_ssl else smtplib.SMTP
    with smtp_cls(cfg.host, cfg.port, timeout=timeout) as smtp:
        if not cfg.use_ssl:
            smtp.starttls()
        smtp.login(cfg.user, cfg.password)
        smtp.send_message(mail)


# =============================================================================
# 5. Google 시트 (서비스계정)
#    - 로그인·토큰 발급 없음. JSON 키 + 시트 공유만으로 동작
#    - 1행 헤더를 읽어 '헤더명 기준'으로 값을 꽂는다 → 열 순서가 달라도 매칭
# =============================================================================
def parse_sheet_id(v: str) -> str:
    m = re.search(r"/spreadsheets/d/([a-zA-Z0-9_-]+)", v or "")
    return m.group(1) if m else (v or "").strip()


class SheetConfig:
    def __init__(self, gsheet: dict | None = None, sa: dict | None = None):
        g = dict(gsheet or {})
        self.sheet_id = parse_sheet_id(_pick(g, "sheet_id", "G_SHEET_ID") or "")
        self.tab = (_pick(g, "tab", "G_SHEET_TAB") or "").strip()   # 비우면 첫 번째 탭
        self.sa = dict(sa or {})

    @property
    def sa_email(self) -> str:
        return self.sa.get("client_email", "")

    @property
    def missing(self) -> list[str]:
        miss = []
        if not self.sheet_id:
            miss.append("시트 ID")
        if not self.sa_email or not self.sa.get("private_key"):
            miss.append("서비스계정 키")
        return miss

    @property
    def ready(self) -> bool:
        return not self.missing

    @property
    def sheet_url(self) -> str:
        return f"https://docs.google.com/spreadsheets/d/{self.sheet_id}/edit" if self.sheet_id else ""

    def masked(self) -> dict[str, str]:
        return {
            "로그 시트": self.sheet_url or "(미설정)",
            "탭": self.tab or "(첫 번째 탭)",
            "서비스계정": self.sa_email or "(미설정)",
        }


@st.cache_resource(show_spinner=False)
def sheet_client(sa_json: str) -> gspread.Client:
    import json
    info = json.loads(sa_json)
    # Secrets 에 \n 이 문자 그대로 들어간 경우 보정
    info["private_key"] = info["private_key"].replace("\\n", "\n")
    creds = Credentials.from_service_account_info(info, scopes=SHEET_SCOPES)
    return gspread.authorize(creds)


def open_worksheet(cfg: SheetConfig) -> gspread.Worksheet:
    import json
    gc = sheet_client(json.dumps(cfg.sa, sort_keys=True))
    sh = gc.open_by_key(cfg.sheet_id)
    return sh.worksheet(cfg.tab) if cfg.tab else sh.sheet1


def _norm(h: str) -> str:
    """공백·괄호 설명 제거 후 비교: '문의 내용(8자 이상)' → '문의내용'"""
    return re.sub(r"\(.*?\)|\s+", "", str(h or ""))


def append_record(cfg: SheetConfig, record: dict) -> list[str]:
    """record 를 시트 헤더명에 맞춰 1행 추가. 시트에 없는 키 목록을 반환."""
    ws = open_worksheet(cfg)
    header = ws.row_values(1)
    if not any(h.strip() for h in header):
        header = []

    # 시트에 없는 표준 항목은 오른쪽 끝에 헤더 자동 추가
    have = {_norm(h) for h in header}
    add = [h for h in SHEET_HEADER if _norm(h) not in have]
    if add:
        header = header + add
        if len(header) > ws.col_count:
            ws.add_cols(len(header) - ws.col_count)
        ws.update(range_name="A1", values=[header])

    index = {_norm(h): i for i, h in enumerate(header)}
    row = [""] * len(header)
    unmatched = []
    for key, val in record.items():
        i = index.get(_norm(key))
        if i is None:
            unmatched.append(key)
        else:
            row[i] = "" if val is None else str(val)

    # RAW: '=' 로 시작하는 입력도 수식으로 실행되지 않음 / table_range: 항상 A열부터 누적
    ws.append_row(row, value_input_option="RAW", table_range="A1")
    return unmatched


# =============================================================================
# 6. 반응형 스타일
# =============================================================================
CSS = """
<style>
  :root{
    --pad:clamp(.7rem,4vw,1.25rem); --gap:clamp(.45rem,2.2vw,.8rem);
    --radius:clamp(10px,3vw,16px);
    --fs-h1:clamp(1.02rem,4.6vw,1.32rem); --fs-sub:clamp(.76rem,3.1vw,.9rem);
    --fs-step:clamp(.92rem,3.9vw,1.08rem); --fs-body:clamp(.88rem,3.5vw,1rem);
    --fs-input:max(16px,clamp(1rem,4.4vw,1.18rem)); --fs-area:max(16px,clamp(1rem,4vw,1.06rem));
    --h-input:clamp(50px,13vw,60px); --h-btn:clamp(56px,14vw,68px); --thumb:clamp(70px,21vw,110px);
  }
  html{ -webkit-text-size-adjust:100%; }
  body{ overflow-x:hidden; }
  .block-container *{ word-break:keep-all; overflow-wrap:anywhere; }
  .block-container{
    max-width:min(560px,100%); padding-left:var(--pad); padding-right:var(--pad);
    padding-top:clamp(.5rem,3vw,1.1rem);
    padding-bottom:calc(3rem + env(safe-area-inset-bottom,0px));
  }
  #MainMenu, footer, header{ visibility:hidden; }

  .hero{ background:#16324F; color:#fff; border-radius:var(--radius);
         padding:var(--pad); margin-bottom:clamp(.7rem,3vw,1.1rem); }
  .hero h1{ font-size:var(--fs-h1); margin:0 0 .28rem; color:#fff; font-weight:800; line-height:1.35; }
  .hero p { font-size:var(--fs-sub); margin:0; color:#B9CBE0; line-height:1.5; }

  .step{ font-size:var(--fs-step); font-weight:800; color:#16202E;
         margin:clamp(.7rem,3vw,1rem) 0 .3rem; line-height:1.4; }
  .step span{ display:inline-flex; align-items:center; justify-content:center;
    background:#1F5AA6; color:#fff; border-radius:50%;
    width:clamp(19px,5.2vw,23px); height:clamp(19px,5.2vw,23px);
    font-size:clamp(.66rem,2.6vw,.78rem); margin-right:.42rem; flex:0 0 auto; }

  .stTextInput input, div[data-testid="stTextInput"] input{
    font-size:var(--fs-input)!important; height:var(--h-input); letter-spacing:.4px; border-radius:var(--radius); }
  .stTextArea textarea, div[data-testid="stTextArea"] textarea{
    font-size:var(--fs-area)!important; line-height:1.6; border-radius:var(--radius); }
  div[data-baseweb="select"] > div{
    font-size:var(--fs-input)!important; min-height:var(--h-input); border-radius:var(--radius); }
  div[data-testid="stRadio"] label p{ font-size:var(--fs-body)!important; font-weight:600; }
  div[data-testid="stRadio"] > div{ flex-wrap:wrap; gap:var(--gap); }
  div[data-testid="stCheckbox"] label p{ font-size:clamp(.8rem,3.2vw,.92rem)!important; line-height:1.55; }

  div[data-testid="stFileUploader"] section{ padding:var(--pad); border-radius:var(--radius); }
  div[data-testid="stFileUploader"] section small{ font-size:clamp(.68rem,2.8vw,.8rem); }
  div[data-testid="stCameraInput"] video,
  div[data-testid="stCameraInput"] img{ width:100%!important; height:auto!important; border-radius:var(--radius); }

  div.stButton>button{ width:100%; min-height:var(--h-btn);
    font-size:clamp(1rem,4.2vw,1.14rem); font-weight:800; border-radius:var(--radius); line-height:1.35; }
  div.stButton>button[kind="primary"]{ background:#1F5AA6; border-color:#1F5AA6; }

  .thumbs{ display:grid; gap:var(--gap); margin:.55rem 0 .2rem;
           grid-template-columns:repeat(auto-fill,minmax(var(--thumb),1fr)); }
  .thumbs img{ width:100%; aspect-ratio:1/1; object-fit:cover;
               border-radius:calc(var(--radius) - 4px); border:1px solid #DDE3EA; display:block; }

  .hint{ background:#FFFAF0; border-left:4px solid #F0B429; border-radius:0 10px 10px 0;
         padding:.65rem .8rem; font-size:var(--fs-body); color:#5C4813; line-height:1.6; margin:.2rem 0 .5rem; }
  .ok{ background:#EAF6EE; border:1.5px solid #9AD3AE; border-radius:var(--radius);
       padding:clamp(1rem,5vw,1.4rem); text-align:center; }
  .ok .big{ font-size:clamp(1.05rem,4.6vw,1.28rem); font-weight:800; color:#15603A; margin-bottom:.3rem; }
  .ok .rid{ display:inline-block; font-family:ui-monospace,monospace; background:#fff;
            border:1px solid #9AD3AE; border-radius:8px; padding:.2rem .6rem; margin:.3rem 0 .5rem;
            font-size:var(--fs-body); color:#15603A; font-weight:700; }
  .ok .sub{ font-size:var(--fs-body); color:#3F6B52; line-height:1.75; }
  .foot{ font-size:clamp(.66rem,2.7vw,.78rem); color:#94A0AE; text-align:center;
         margin-top:clamp(1rem,5vw,1.8rem); line-height:1.75; }

  @media (max-width:359px){
    .hero{ padding:.7rem .8rem; }
    div[data-testid="stRadio"] > div{ flex-direction:column; align-items:stretch; }
    div[data-testid="stFileUploader"] section span{ display:none; }
  }
  @media (orientation:landscape) and (max-height:520px){
    .block-container{ padding-top:.4rem; } .step{ margin:.5rem 0 .25rem; } .hero{ padding:.6rem .85rem; }
  }
  @media (min-width:901px){ .block-container{ padding-top:2rem; } }
  @media (prefers-reduced-motion:reduce){ *{ animation:none!important; transition:none!important; } }
</style>
"""


# =============================================================================
# 7. 화면
# =============================================================================
def init_state() -> None:
    try:
        qp_school = (st.query_params.get("school") or "").strip()[:30]
    except Exception:
        qp_school = ""
    for k, v in {"school": qp_school, "place": "", "phone": "", "message": "",
                 "sent": False, "receipt": {}, "t0": time.time()}.items():
        st.session_state.setdefault(k, v)


def on_phone_change() -> None:
    st.session_state.phone = normalize_phone(st.session_state.phone)


def step(num: int, text: str) -> None:
    st.markdown(f'<div class="step"><span>{num}</span>{text}</div>', unsafe_allow_html=True)


def render_previews(photos: list[tuple[str, bytes]]) -> None:
    if not photos:
        return
    cells = "".join(
        f'<img src="data:image/jpeg;base64,{base64.b64encode(d).decode()}" alt="">'
        for _, d in photos
    )
    st.markdown(f'<div class="thumbs">{cells}</div>', unsafe_allow_html=True)


def collect_photos(mode: str) -> list[tuple[str, bytes]]:
    photos: list[tuple[str, bytes]] = []
    if mode == "카메라 촬영":
        shot = st.camera_input("촬영", label_visibility="collapsed")
        if shot:
            photos.append(("capture.jpg", compress(shot.getvalue())))
    else:
        files = st.file_uploader(
            "사진 선택", type=["jpg", "jpeg", "png", "heic", "heif", "webp"],
            accept_multiple_files=True, label_visibility="collapsed",
        )
        for idx, f in enumerate(files or [], start=1):
            photos.append((f"photo_{idx}.jpg", compress(f.getvalue())))
        render_previews(photos)
    return photos


def render_form() -> None:
    step(1, "장애 구분")
    category = st.radio("장애 구분", CATEGORIES, horizontal=True, index=None,
                        label_visibility="collapsed",
                        captions=[CATEGORY_DESC[c] for c in CATEGORIES])

    step(2, "증상 선택")
    symptom = None
    if category:
        symptom = st.selectbox("증상", symptom_labels(category), index=None,
                               placeholder="증상을 선택하세요", label_visibility="collapsed")
        if symptom and (note := symptom_note(category, symptom)):
            st.markdown(f'<div class="hint">{note}</div>', unsafe_allow_html=True)
    else:
        st.selectbox("증상", ["먼저 유선 / 무선을 선택하세요"], disabled=True,
                     label_visibility="collapsed")

    step(3, "학교명")
    st.text_input("학교명", key="school", max_chars=30,
                  placeholder="예) 대전OO초등학교", label_visibility="collapsed")

    step(4, "장소")
    st.text_input("장소", key="place", max_chars=40,
                  placeholder="예) 본관 3층 교무실 / 3층 3-1교실", label_visibility="collapsed")

    step(5, "휴대전화번호")
    st.text_input("휴대전화번호", key="phone", max_chars=13, placeholder="010-0000-0000",
                  on_change=on_phone_change, label_visibility="collapsed")

    step(6, f"문의 내용 ({MIN_MESSAGE_LEN}자 이상)")
    st.text_area("문의 내용", key="message", height=130, label_visibility="collapsed",
                 placeholder="예) 오늘 오전부터 인터넷이 안 되고 랜선을 바꿔도 동일합니다.")

    step(7, "문제 화면 사진 (1장 이상)")
    mode = st.radio("첨부 방식", ["카메라 촬영", "사진함에서 선택"],
                    horizontal=True, label_visibility="collapsed")
    photos = collect_photos(mode)
    st.session_state.attach_mode = mode

    st.write("")
    agreed = st.checkbox(f"개인정보 수집·이용 동의 — {CONSENT}")

    st.write("")
    if st.button("보내기", type="primary"):
        submit(category, symptom, photos, agreed)


def submit(category, symptom, photos, agreed) -> None:
    ss = st.session_state
    errors = validate(category, symptom, ss.school, ss.place, ss.phone,
                      ss.message, len(photos), agreed)
    if errors:
        for msg in errors:
            st.error(msg)
        return

    mcfg = MailConfig(read_secrets("smtp"))
    scfg = SheetConfig(read_secrets("gsheet"), read_secrets("gcp_service_account"))
    if not mcfg.ready and not scfg.ready:
        st.error("접수 설정이 완료되지 않았습니다. 관리자에게 문의해 주세요.")
        return

    rid = make_receipt_id()
    meta = collect_client_meta()
    ts = f"{now_kst():%Y-%m-%d %H:%M:%S}"
    mailed = logged = False

    with st.spinner("전송 중입니다..."):
        # ① 메일
        if mcfg.ready:
            try:
                send_inquiry(
                    mcfg,
                    build_subject(rid, category, symptom, ss.school, ss.place),
                    build_body(rid, category, symptom, ss.school, ss.place,
                               ss.phone, ss.message, len(photos)),
                    photos,
                )
                mailed = True
            except Exception as exc:
                record_error("메일", exc)

        # ② 시트 누적 (메일 실패 건도 기록)
        if scfg.ready:
            try:
                append_record(scfg, {
                    "장애 구분": category,
                    "증상 선택": symptom,
                    "학교명": ss.school.strip(),
                    "장소": ss.place.strip(),
                    "휴대전화번호": ss.phone,
                    "문의 내용": ss.message.strip(),
                    "문제 화면 사진": f"{len(photos)}장 (메일 첨부)",
                    "접수번호": rid,
                    "접수일시": ts,
                    "첨부 방식": ss.get("attach_mode", ""),
                    "메일 발송": "성공" if mailed else "실패",
                    **meta,
                })
                logged = True
            except Exception as exc:
                record_error("시트", exc)

    if not mailed and not logged:
        st.error("전송에 실패했습니다. 잠시 후 다시 시도해 주세요.")
        return

    ss.sent = True
    ss.receipt = {"rid": rid, "category": category, "symptom": symptom,
                  "school": ss.school, "place": ss.place, "photos": len(photos)}
    st.rerun()


def render_success() -> None:
    r = st.session_state.receipt
    st.markdown(
        '<div class="ok"><div class="big">접수가 완료되었습니다</div>'
        f'<div class="rid">접수번호 {r.get("rid", "")}</div>'
        f'<div class="sub">{r.get("school", "")} {r.get("place", "")}<br>'
        f'{r.get("category", "")} · {r.get("symptom", "")}<br>'
        f'사진 {r.get("photos", 0)}장과 함께 담당자에게 전달했습니다.<br>'
        "입력하신 번호로 회신드립니다.</div></div>",
        unsafe_allow_html=True,
    )
    st.write("")
    if st.button("새 문의 작성", type="primary"):
        for key in ("school", "place", "phone", "message", "sent", "receipt", "t0"):
            st.session_state.pop(key, None)
        st.rerun()


def render_diag() -> None:
    mcfg = MailConfig(read_secrets("smtp"))
    scfg = SheetConfig(read_secrets("gsheet"), read_secrets("gcp_service_account"))

    st.subheader("① 메일")
    (st.success if mcfg.ready else st.error)(
        "설정 정상" if mcfg.ready else f"누락: {', '.join(mcfg.missing)}")
    for k, v in mcfg.masked().items():
        st.write(f"- **{k}** : {v}")
    if st.button("테스트 메일 보내기", disabled=not mcfg.ready):
        try:
            send_inquiry(mcfg, "[문의접수][테스트] 설정 확인",
                         "QR 접수 페이지 설정 확인용 테스트입니다.", [])
            st.success(f"발송 완료 → {mcfg.to}")
        except Exception as exc:
            st.error(f"실패: {type(exc).__name__}")
            st.code(str(exc)[:400])

    st.divider()
    st.subheader("② 구글 시트")
    (st.success if scfg.ready else st.error)(
        "설정 정상" if scfg.ready else f"누락: {', '.join(scfg.missing)}")
    for k, v in scfg.masked().items():
        st.write(f"- **{k}** : {v}")
    if scfg.sa_email:
        st.info("시트 → 공유 → 아래 주소를 **편집자**로 추가해야 기록됩니다.")
        st.code(scfg.sa_email)

    c1, c2 = st.columns(2)
    if c1.button("헤더 읽기", disabled=not scfg.ready):
        try:
            ws = open_worksheet(scfg)
            header = ws.row_values(1)
            st.write(f"탭 '{ws.title}' 헤더:", header or "(비어 있음)")
            miss = [h for h in SHEET_HEADER if _norm(h) not in {_norm(x) for x in header}]
            if miss:
                st.info(f"첫 기록 시 오른쪽에 자동 추가될 열: {', '.join(miss)}")
            else:
                st.success("헤더가 앱 항목과 모두 일치합니다.")
        except Exception as exc:
            st.error(f"실패: {type(exc).__name__}")
            st.code(str(exc)[:400])
    if c2.button("테스트 행 기록", disabled=not scfg.ready):
        try:
            unmatched = append_record(scfg, {
                "접수번호": "TEST", "접수일시": f"{now_kst():%Y-%m-%d %H:%M:%S}",
                "학교명": "테스트학교", "장소": "진단화면", "문의 내용": "시트 기록 테스트",
                **collect_client_meta(),
            })
            st.success("기록 완료. 시트 맨 아래 TEST 행을 확인 후 삭제하세요.")
            if unmatched:
                st.warning(f"헤더 불일치: {', '.join(unmatched)}")
        except Exception as exc:
            st.error(f"실패: {type(exc).__name__}")
            st.code(str(exc)[:400])

    st.divider()
    st.subheader("③ 내 접속정보 수집 확인")
    for k, v in collect_client_meta().items():
        st.write(f"- **{k}** : {v if v != '' else '(수집 안 됨)'}")

    st.divider()
    st.subheader("④ 최근 오류")
    logs = list(error_log())
    st.code("\n".join(logs) if logs else "(없음)")

    if st.button("접수 화면으로"):
        st.query_params.clear()
        st.rerun()


def main() -> None:
    st.set_page_config(page_title=TITLE, page_icon="🏫",
                       layout="centered", initial_sidebar_state="collapsed")
    st.markdown(CSS, unsafe_allow_html=True)
    init_state()
    st.markdown(f'<div class="hero"><h1>{TITLE}</h1><p>{SUBTITLE}</p></div>',
                unsafe_allow_html=True)

    if st.query_params.get("diag") == "1":
        render_diag()
    elif st.session_state.sent:
        render_success()
    else:
        render_form()

    st.markdown(
        '<div class="foot">수집 항목: 휴대전화번호, 문의내용, 첨부사진<br>'
        "자동 수집: 접속 IP, 기기·브라우저 정보<br>"
        f"보유기간: {RETENTION}</div>",
        unsafe_allow_html=True,
    )


if __name__ == "__main__":
    main()
