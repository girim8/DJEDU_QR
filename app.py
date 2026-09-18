"""학교 정보업무 문의접수 (QR 진입 · 모바일 반응형 단일 화면).

유선/무선 장애 구분 → 증상 선택 → 연락처 → 문의내용 → 사진 첨부 → 메일 발송.

실행:  streamlit run app.py
배포:  GitHub → Streamlit Community Cloud

SMTP 계정 정보는 코드에도, 저장소에도 넣지 않는다.
Streamlit Cloud → App settings → Secrets 에만 입력한다.
"""

from __future__ import annotations

import base64
import datetime as dt
import io
import os
import re
import smtplib
from email.message import EmailMessage

import streamlit as st
from PIL import Image, ImageOps

# =============================================================================
# 1. 설정
# =============================================================================
TITLE = "학교 정보업무 문의접수"
SUBTITLE = "장애 구분과 증상을 고르고, 연락처·사진을 남겨주세요"

DEFAULT_TO = "1670-0570@skbroadband.com"

# Gmail SMTP 기본값. Secrets 에 user / password 만 넣으면 그대로 동작한다.
DEFAULT_HOST = "smtp.gmail.com"
DEFAULT_PORT = 587          # STARTTLS. 465 사용 시 use_ssl = true

MIN_MESSAGE_LEN = 8
MIN_SCHOOL_LEN = 2
MIN_PLACE_LEN = 2
PHONE_RE = re.compile(r"^010-\d{4}-\d{4}$")

MAX_EDGE = 1600      # 첨부 사진 긴 변 최대 픽셀
JPEG_QUALITY = 80

CONSENT = (
    "접수·회신 목적으로 휴대전화번호와 첨부 사진을 수집하며, "
    "처리 완료 후 지체 없이 파기합니다."
)

# =============================================================================
# 2. 장애 분류 카탈로그  ← 증상 추가·삭제는 여기만 수정
# =============================================================================
WIRED, WIRELESS = "유선", "무선"
CATEGORIES = [WIRED, WIRELESS]

CATEGORY_DESC = {
    WIRED: "랜선으로 연결하는 업무망 · 에듀파인 등",
    WIRELESS: "와이파이 · 무선AP로 접속하는 인터넷",
}

# note = 접수자가 함께 적어주면 좋은 정보. 증상 선택 시 힌트로 노출된다.
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
# 3. 검증
# =============================================================================
def normalize_phone(raw: str) -> str:
    """입력값에서 숫자만 뽑아 010-XXXX-XXXX 형태로 자동 정리."""
    digits = re.sub(r"\D", "", raw or "")[:11]
    if len(digits) <= 3:
        return digits
    if len(digits) <= 7:
        return f"{digits[:3]}-{digits[3:]}"
    return f"{digits[:3]}-{digits[3:7]}-{digits[7:]}"


def validate(
    category: str | None,
    symptom: str | None,
    school: str,
    place: str,
    phone: str,
    message: str,
    photo_count: int,
    agreed: bool,
) -> list[str]:
    """통과하면 빈 리스트, 아니면 사용자에게 보여줄 오류 문구 목록."""
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


# =============================================================================
# 4. 사진 압축
#    휴대폰 원본은 장당 3~8MB라 메일 서버 첨부 제한에 쉽게 걸린다.
#    긴 변 1600px / JPEG 80이면 판독에 충분하면서 장당 200~500KB로 떨어진다.
# =============================================================================
def compress(raw: bytes) -> bytes:
    try:
        img = Image.open(io.BytesIO(raw))
        img = ImageOps.exif_transpose(img)  # 세로 사진 회전 보정
        if img.mode not in ("RGB", "L"):
            img = img.convert("RGB")
        img.thumbnail((MAX_EDGE, MAX_EDGE), Image.LANCZOS)
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=JPEG_QUALITY, optimize=True)
        return buf.getvalue()
    except Exception:
        return raw


# =============================================================================
# 5. 메일 발송
#    설정 우선순위: Streamlit Cloud Secrets → 환경변수
# =============================================================================
def read_secrets() -> dict:
    """secrets 미설정 환경(로컬 등)에서도 예외 없이 빈 dict 를 돌려준다."""
    try:
        return dict(st.secrets.get("smtp", {}))
    except Exception:
        return {}


class MailConfig:
    """Gmail 기준 기본값 + Secrets/환경변수 덮어쓰기."""

    REQUIRED = {"host": "호스트", "user": "계정", "password": "앱 비밀번호",
                "sender": "발신주소", "to": "수신주소"}

    def __init__(self, secrets: dict | None = None):
        s = dict(secrets or {})

        def pick(key: str, env: str, default=None):
            v = s.get(key, os.environ.get(env, default))
            return v.strip() if isinstance(v, str) else v

        self.host = pick("host", "SMTP_HOST", DEFAULT_HOST)
        self.port = int(pick("port", "SMTP_PORT", DEFAULT_PORT))
        self.user = pick("user", "SMTP_USER")
        # Gmail 앱 비밀번호는 4자씩 띄어 표시되므로 공백을 제거해 둔다.
        pw = pick("password", "SMTP_PASSWORD")
        self.password = pw.replace(" ", "") if isinstance(pw, str) else pw
        # Gmail 은 인증 계정과 다른 From 을 거부하므로 sender 는 user 로 고정된다.
        self.sender = pick("sender", "SMTP_SENDER") or self.user
        self.to = pick("to", "SMTP_TO", DEFAULT_TO)
        self.use_ssl = str(pick("use_ssl", "SMTP_USE_SSL", "0")).lower() in ("1", "true")

    @property
    def missing(self) -> list[str]:
        """비어 있는 필수 항목의 한글 이름 목록."""
        return [ko for key, ko in self.REQUIRED.items() if not getattr(self, key)]

    @property
    def ready(self) -> bool:
        return not self.missing

    def masked(self) -> dict[str, str]:
        """진단용. 값은 마스킹해서 노출한다."""
        def mask_mail(v):
            if not v or "@" not in v:
                return "(미설정)" if not v else "설정됨"
            name, dom = v.split("@", 1)
            return f"{name[:2]}***@{dom}"
        return {
            "호스트": f"{self.host}:{self.port}" + (" (SSL)" if self.use_ssl else " (STARTTLS)"),
            "계정": mask_mail(self.user),
            "앱 비밀번호": f"설정됨 ({len(self.password)}자)" if self.password else "(미설정)",
            "발신주소": mask_mail(self.sender),
            "수신주소": self.to or "(미설정)",
        }


def build_subject(category: str, symptom: str, school: str, place: str) -> str:
    return f"[문의접수][{category}] {school.strip()} {place.strip()} - {symptom}"


def build_body(
    category: str, symptom: str, school: str, place: str,
    phone: str, message: str, n: int,
) -> str:
    now = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    return (
        "학교 정보업무 문의가 접수되었습니다.\n"
        "--------------------------------------------\n"
        f"접수 일시   : {now}\n"
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
        "회신은 위 연락처로 부탁드립니다.\n"
    )


def send_inquiry(
    cfg: MailConfig,
    category: str,
    symptom: str,
    school: str,
    place: str,
    phone: str,
    message: str,
    photos: list[tuple[str, bytes]],
    timeout: int = 30,
) -> None:
    """발송 성공 시 None, 실패 시 예외를 그대로 올린다."""
    mail = EmailMessage()
    mail["Subject"] = build_subject(category, symptom, school, place)
    mail["From"] = cfg.sender
    mail["To"] = cfg.to
    mail["Reply-To"] = cfg.sender
    mail.set_content(
        build_body(category, symptom, school, place, phone, message, len(photos))
    )

    for idx, (name, data) in enumerate(photos, start=1):
        mail.add_attachment(
            data, maintype="image", subtype="jpeg", filename=name or f"photo_{idx}.jpg"
        )

    if cfg.use_ssl:
        with smtplib.SMTP_SSL(cfg.host, cfg.port, timeout=timeout) as smtp:
            smtp.login(cfg.user, cfg.password)
            smtp.send_message(mail)
    else:
        with smtplib.SMTP(cfg.host, cfg.port, timeout=timeout) as smtp:
            smtp.starttls()
            smtp.login(cfg.user, cfg.password)
            smtp.send_message(mail)


# =============================================================================
# 6. 반응형 스타일
#    - clamp() 유동 스케일: 320px ~ 1024px 구간을 브레이크포인트 없이 연속 대응
#    - 입력 글꼴 16px 하한: iOS Safari 포커스 시 자동 확대 방지
#    - dvh / safe-area-inset: 모바일 주소창·홈 인디케이터 영역 보정
#    - word-break: keep-all: 한글 어절 단위 줄바꿈
# =============================================================================
CSS = """
<style>
  :root{
    --pad:       clamp(.7rem, 4vw, 1.25rem);
    --gap:       clamp(.45rem, 2.2vw, .8rem);
    --radius:    clamp(10px, 3vw, 16px);
    --fs-h1:     clamp(1.02rem, 4.6vw, 1.32rem);
    --fs-sub:    clamp(.76rem, 3.1vw, .9rem);
    --fs-step:   clamp(.92rem, 3.9vw, 1.08rem);
    --fs-body:   clamp(.88rem, 3.5vw, 1rem);
    --fs-input:  max(16px, clamp(1rem, 4.4vw, 1.18rem));
    --fs-area:   max(16px, clamp(1rem, 4vw, 1.06rem));
    --h-input:   clamp(50px, 13vw, 60px);
    --h-btn:     clamp(56px, 14vw, 68px);
    --thumb:     clamp(70px, 21vw, 110px);
  }

  html{ -webkit-text-size-adjust:100%; }
  body{ overflow-x:hidden; }
  .block-container *{ word-break:keep-all; overflow-wrap:anywhere; }

  .block-container{
    max-width:min(560px, 100%);
    padding-left:var(--pad);
    padding-right:var(--pad);
    padding-top:clamp(.5rem, 3vw, 1.1rem);
    padding-bottom:calc(3rem + env(safe-area-inset-bottom, 0px));
  }
  #MainMenu, footer, header{ visibility:hidden; }

  /* ---- 헤더 ---- */
  .hero{
    background:#16324F; color:#fff; border-radius:var(--radius);
    padding:var(--pad); margin-bottom:clamp(.7rem, 3vw, 1.1rem);
  }
  .hero h1{ font-size:var(--fs-h1); margin:0 0 .28rem; color:#fff; font-weight:800; line-height:1.35; }
  .hero p { font-size:var(--fs-sub); margin:0; color:#B9CBE0; line-height:1.5; }

  /* ---- 단계 라벨 ---- */
  .step{ font-size:var(--fs-step); font-weight:800; color:#16202E;
         margin:clamp(.7rem,3vw,1rem) 0 .3rem; line-height:1.4; }
  .step span{
    display:inline-flex; align-items:center; justify-content:center;
    background:#1F5AA6; color:#fff; border-radius:50%;
    width:clamp(19px,5.2vw,23px); height:clamp(19px,5.2vw,23px);
    font-size:clamp(.66rem,2.6vw,.78rem); margin-right:.42rem; flex:0 0 auto;
  }

  /* ---- 입력 위젯 (구/신 셀렉터 병기) ---- */
  .stTextInput input, div[data-testid="stTextInput"] input{
    font-size:var(--fs-input)!important; height:var(--h-input);
    letter-spacing:.4px; border-radius:var(--radius);
  }
  .stTextArea textarea, div[data-testid="stTextArea"] textarea{
    font-size:var(--fs-area)!important; line-height:1.6; border-radius:var(--radius);
  }
  div[data-baseweb="select"] > div{
    font-size:var(--fs-input)!important; min-height:var(--h-input); border-radius:var(--radius);
  }
  div[data-testid="stRadio"] label p{ font-size:var(--fs-body)!important; font-weight:600; }
  div[data-testid="stRadio"] > div{ flex-wrap:wrap; gap:var(--gap); }
  div[data-testid="stCheckbox"] label p{ font-size:clamp(.8rem,3.2vw,.92rem)!important; line-height:1.55; }

  /* ---- 업로더 / 카메라 ---- */
  div[data-testid="stFileUploader"] section{ padding:var(--pad); border-radius:var(--radius); }
  div[data-testid="stFileUploader"] section small{ font-size:clamp(.68rem,2.8vw,.8rem); }
  div[data-testid="stCameraInput"] video,
  div[data-testid="stCameraInput"] img{ width:100%!important; height:auto!important; border-radius:var(--radius); }

  /* ---- 버튼 ---- */
  div.stButton>button{
    width:100%; min-height:var(--h-btn);
    font-size:clamp(1rem,4.2vw,1.14rem); font-weight:800;
    border-radius:var(--radius); line-height:1.35;
  }
  div.stButton>button[kind="primary"]{ background:#1F5AA6; border-color:#1F5AA6; }

  /* ---- 사진 미리보기 (유동 그리드) ---- */
  .thumbs{ display:grid; gap:var(--gap); margin:.55rem 0 .2rem;
           grid-template-columns:repeat(auto-fill, minmax(var(--thumb), 1fr)); }
  .thumbs img{ width:100%; aspect-ratio:1/1; object-fit:cover;
               border-radius:calc(var(--radius) - 4px); border:1px solid #DDE3EA; display:block; }

  /* ---- 힌트 / 완료 / 푸터 ---- */
  .hint{ background:#FFFAF0; border-left:4px solid #F0B429; border-radius:0 10px 10px 0;
         padding:.65rem .8rem; font-size:var(--fs-body); color:#5C4813;
         line-height:1.6; margin:.2rem 0 .5rem; }
  .ok{ background:#EAF6EE; border:1.5px solid #9AD3AE; border-radius:var(--radius);
       padding:clamp(1rem,5vw,1.4rem); text-align:center; }
  .ok .big{ font-size:clamp(1.05rem,4.6vw,1.28rem); font-weight:800; color:#15603A; margin-bottom:.3rem; }
  .ok .sub{ font-size:var(--fs-body); color:#3F6B52; line-height:1.75; }
  .foot{ font-size:clamp(.66rem,2.7vw,.78rem); color:#94A0AE; text-align:center;
         margin-top:clamp(1rem,5vw,1.8rem); line-height:1.75; }

  /* ---- 초소형 단말 (iPhone SE, 구형 안드로이드 ~359px) ---- */
  @media (max-width:359px){
    .hero{ padding:.7rem .8rem; }
    div[data-testid="stRadio"] > div{ flex-direction:column; align-items:stretch; }
    div[data-testid="stFileUploader"] section span{ display:none; }
  }

  /* ---- 가로 모드 / 낮은 화면 ---- */
  @media (orientation:landscape) and (max-height:520px){
    .block-container{ padding-top:.4rem; }
    .step{ margin:.5rem 0 .25rem; }
    .hero{ padding:.6rem .85rem; }
  }

  /* ---- 태블릿·데스크톱 ---- */
  @media (min-width:901px){
    .block-container{ padding-top:2rem; }
  }

  @media (prefers-reduced-motion:reduce){
    *{ animation:none!important; transition:none!important; }
  }
</style>
"""


# =============================================================================
# 7. 화면
# =============================================================================
def init_state() -> None:
    st.session_state.setdefault("school", "")
    st.session_state.setdefault("place", "")
    st.session_state.setdefault("phone", "")
    st.session_state.setdefault("message", "")
    st.session_state.setdefault("sent", False)
    st.session_state.setdefault("receipt", {})


def on_phone_change() -> None:
    st.session_state.phone = normalize_phone(st.session_state.phone)


def step(num: int, text: str) -> None:
    st.markdown(f'<div class="step"><span>{num}</span>{text}</div>', unsafe_allow_html=True)


def render_previews(photos: list[tuple[str, bytes]]) -> None:
    """st.columns 대신 CSS 그리드로 직접 렌더 (모바일에서 컬럼이 깨지는 문제 회피)."""
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
            "사진 선택",
            type=["jpg", "jpeg", "png", "heic", "webp"],
            accept_multiple_files=True,
            label_visibility="collapsed",
        )
        for idx, f in enumerate(files or [], start=1):
            photos.append((f"photo_{idx}.jpg", compress(f.getvalue())))
        render_previews(photos)
    return photos


def render_form() -> None:
    step(1, "장애 구분")
    category = st.radio(
        "장애 구분",
        CATEGORIES,
        horizontal=True,
        index=None,
        label_visibility="collapsed",
        captions=[CATEGORY_DESC[c] for c in CATEGORIES],
    )

    step(2, "증상 선택")
    symptom = None
    if category:
        symptom = st.selectbox(
            "증상",
            symptom_labels(category),
            index=None,
            placeholder="증상을 선택하세요",
            label_visibility="collapsed",
        )
        if symptom:
            note = symptom_note(category, symptom)
            if note:
                st.markdown(f'<div class="hint">{note}</div>', unsafe_allow_html=True)
    else:
        st.selectbox(
            "증상",
            ["먼저 유선 / 무선을 선택하세요"],
            disabled=True,
            label_visibility="collapsed",
        )

    step(3, "학교명")
    st.text_input(
        "학교명",
        key="school",
        max_chars=30,
        placeholder="예) 대전OO초등학교",
        label_visibility="collapsed",
    )

    step(4, "장소")
    st.text_input(
        "장소",
        key="place",
        max_chars=40,
        placeholder="예) 본관 3층 교무실 / 3층 3-1교실",
        label_visibility="collapsed",
    )

    step(5, "휴대전화번호")
    st.text_input(
        "휴대전화번호",
        key="phone",
        max_chars=13,
        placeholder="010-0000-0000",
        on_change=on_phone_change,
        label_visibility="collapsed",
    )

    step(6, f"문의 내용 ({MIN_MESSAGE_LEN}자 이상)")
    st.text_area(
        "문의 내용",
        key="message",
        height=130,
        placeholder="예) OO초 3층 교무실. 오늘 오전부터 인터넷이 안 되고 랜선을 바꿔도 동일합니다.",
        label_visibility="collapsed",
    )

    step(7, "문제 화면 사진 (1장 이상)")
    mode = st.radio(
        "첨부 방식",
        ["카메라 촬영", "사진함에서 선택"],
        horizontal=True,
        label_visibility="collapsed",
    )
    photos = collect_photos(mode)

    st.write("")
    agreed = st.checkbox(f"개인정보 수집·이용 동의 — {CONSENT}")

    st.write("")
    if st.button("보내기", type="primary"):
        submit(category, symptom, photos, agreed)


def submit(
    category: str | None,
    symptom: str | None,
    photos: list[tuple[str, bytes]],
    agreed: bool,
) -> None:
    errors = validate(
        category, symptom,
        st.session_state.school, st.session_state.place,
        st.session_state.phone, st.session_state.message,
        len(photos), agreed,
    )
    if errors:
        for msg in errors:
            st.error(msg)
        return

    cfg = MailConfig(read_secrets())
    if not cfg.ready:
        st.error(
            "메일 발송 설정이 완료되지 않았습니다. 관리자에게 문의해 주세요.\n\n"
            f"(누락 항목: {', '.join(cfg.missing)})"
        )
        return

    with st.spinner("전송 중입니다..."):
        try:
            send_inquiry(
                cfg, category, symptom,
                st.session_state.school, st.session_state.place,
                st.session_state.phone, st.session_state.message, photos,
            )
        except Exception:
            st.error("전송에 실패했습니다. 잠시 후 다시 시도해 주세요.")
            return

    st.session_state.sent = True
    st.session_state.receipt = {
        "category": category, "symptom": symptom,
        "school": st.session_state.school, "place": st.session_state.place,
        "photos": len(photos),
    }
    st.rerun()


def render_success() -> None:
    r = st.session_state.receipt
    st.markdown(
        '<div class="ok"><div class="big">접수가 완료되었습니다</div>'
        f'<div class="sub">{r.get("school", "")} {r.get("place", "")}<br>'
        f'{r.get("category", "")} · {r.get("symptom", "")}<br>'
        f'사진 {r.get("photos", 0)}장과 함께 담당자에게 전달했습니다.<br>'
        "입력하신 번호로 회신드립니다.</div></div>",
        unsafe_allow_html=True,
    )
    st.write("")
    if st.button("새 문의 작성", type="primary"):
        for key in ("school", "place", "phone", "message", "sent", "receipt"):
            st.session_state.pop(key, None)
        st.rerun()


def render_diag() -> None:
    """?diag=1 진단 화면. 값은 마스킹되며 비밀번호는 노출되지 않는다."""
    cfg = MailConfig(read_secrets())

    st.subheader("메일 발송 설정 진단")
    if cfg.ready:
        st.success("필수 항목이 모두 설정되었습니다.")
    else:
        st.error(f"누락 항목: {', '.join(cfg.missing)}")

    for k, v in cfg.masked().items():
        st.write(f"- **{k}** : {v}")

    st.divider()
    st.caption("아래 버튼은 수신주소로 테스트 메일 1통을 보냅니다.")
    if st.button("테스트 메일 보내기", type="primary", disabled=not cfg.ready):
        with st.spinner("전송 중..."):
            try:
                send_inquiry(
                    cfg, "유선", "테스트 발송", "테스트학교", "1층 전산실",
                    "010-0000-0000", "QR 접수 페이지 설정 확인용 테스트입니다.", [],
                )
                st.success(f"발송 완료. {cfg.to} 수신함을 확인하세요.")
            except Exception as exc:
                st.error(f"발송 실패: {type(exc).__name__}")
                st.code(str(exc)[:400])

    st.divider()
    if st.button("접수 화면으로"):
        st.query_params.clear()
        st.rerun()


def main() -> None:
    st.set_page_config(
        page_title=TITLE, page_icon="🏫",
        layout="centered", initial_sidebar_state="collapsed",
    )
    st.markdown(CSS, unsafe_allow_html=True)
    init_state()

    st.markdown(
        f'<div class="hero"><h1>{TITLE}</h1><p>{SUBTITLE}</p></div>',
        unsafe_allow_html=True,
    )

    if st.query_params.get("diag") == "1":
        render_diag()
    elif st.session_state.sent:
        render_success()
    else:
        render_form()

    st.markdown(
        '<div class="foot">수집 항목: 휴대전화번호, 문의내용, 첨부사진<br>'
        "보유기간: 처리 완료 후 즉시 파기</div>",
        unsafe_allow_html=True,
    )


if __name__ == "__main__":
    main()
