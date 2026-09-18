"""학교 정보업무 문의접수 (QR 진입 · 모바일 전용 단일 화면).

유선/무선 장애 구분 → 증상 선택 → 연락처 → 문의내용 → 사진 첨부 → 메일 발송.

실행:  streamlit run app.py
배포:  GitHub → Streamlit Community Cloud (또는 사내 서버)

SMTP 계정 정보는 코드에 넣지 않는다. .streamlit/secrets.toml 또는 환경변수로만 주입한다.
"""

from __future__ import annotations

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
MIN_MESSAGE_LEN = 8
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
# =============================================================================
class MailConfig:
    """secrets 또는 환경변수에서 SMTP 설정을 읽는다."""

    def __init__(self, secrets: dict | None = None):
        s = dict(secrets or {})

        def pick(key: str, env: str, default=None):
            return s.get(key, os.environ.get(env, default))

        self.host = pick("host", "SMTP_HOST")
        self.port = int(pick("port", "SMTP_PORT", 587))
        self.user = pick("user", "SMTP_USER")
        self.password = pick("password", "SMTP_PASSWORD")
        self.sender = pick("sender", "SMTP_SENDER") or self.user
        self.to = pick("to", "SMTP_TO", DEFAULT_TO)
        self.use_ssl = str(pick("use_ssl", "SMTP_USE_SSL", "0")).lower() in ("1", "true")

    @property
    def ready(self) -> bool:
        return all([self.host, self.user, self.password, self.sender, self.to])


def build_subject(category: str, symptom: str, phone: str) -> str:
    return f"[문의접수][{category}] {symptom} / {phone}"


def build_body(category: str, symptom: str, phone: str, message: str, n: int) -> str:
    now = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    return (
        "학교 정보업무 문의가 접수되었습니다.\n"
        "--------------------------------------------\n"
        f"접수 일시   : {now}\n"
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
    phone: str,
    message: str,
    photos: list[tuple[str, bytes]],
    timeout: int = 30,
) -> None:
    """발송 성공 시 None, 실패 시 예외를 그대로 올린다."""
    mail = EmailMessage()
    mail["Subject"] = build_subject(category, symptom, phone)
    mail["From"] = cfg.sender
    mail["To"] = cfg.to
    mail["Reply-To"] = cfg.sender
    mail.set_content(build_body(category, symptom, phone, message, len(photos)))

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
# 6. 모바일 우선 스타일
# =============================================================================
CSS = """
<style>
  .block-container{max-width:520px;padding:1rem 1rem 4rem;}
  #MainMenu,footer,header{visibility:hidden;}

  .hero{background:#16324F;color:#fff;border-radius:16px;padding:1rem 1.1rem;margin-bottom:1.1rem;}
  .hero h1{font-size:1.2rem;margin:0 0 .3rem;color:#fff;font-weight:800;}
  .hero p{font-size:.85rem;margin:0;color:#B9CBE0;}

  .step{font-size:1.0rem;font-weight:800;color:#16202E;margin:.9rem 0 .35rem;}
  .step span{display:inline-block;background:#1F5AA6;color:#fff;border-radius:50%;
             width:22px;height:22px;line-height:22px;text-align:center;
             font-size:.78rem;margin-right:.45rem;}

  label,.stTextInput label,.stTextArea label{font-size:1rem!important;font-weight:700!important;color:#16202E!important;}
  .stTextInput input{font-size:1.15rem!important;height:56px;letter-spacing:.5px;}
  .stTextArea textarea{font-size:1.05rem!important;line-height:1.6;}
  .stRadio label p{font-size:1.02rem!important;font-weight:600;}

  div.stButton>button{width:100%;min-height:62px;font-size:1.1rem;font-weight:800;border-radius:14px;}
  div.stButton>button[kind="primary"]{background:#1F5AA6;border-color:#1F5AA6;}

  .hint{background:#FFFAF0;border-left:4px solid #F0B429;border-radius:0 10px 10px 0;
        padding:.7rem .85rem;font-size:.9rem;color:#5C4813;line-height:1.6;margin:.2rem 0 .6rem;}

  .ok{background:#EAF6EE;border:1.5px solid #9AD3AE;border-radius:16px;padding:1.3rem;text-align:center;}
  .ok .big{font-size:1.25rem;font-weight:800;color:#15603A;margin-bottom:.35rem;}
  .ok .sub{font-size:.92rem;color:#3F6B52;line-height:1.7;}
  .foot{font-size:.75rem;color:#94A0AE;text-align:center;margin-top:1.8rem;line-height:1.7;}
</style>
"""


# =============================================================================
# 7. 화면
# =============================================================================
def init_state() -> None:
    st.session_state.setdefault("phone", "")
    st.session_state.setdefault("message", "")
    st.session_state.setdefault("sent", False)
    st.session_state.setdefault("receipt", {})


def on_phone_change() -> None:
    st.session_state.phone = normalize_phone(st.session_state.phone)


def step(num: int, text: str) -> None:
    st.markdown(f'<div class="step"><span>{num}</span>{text}</div>', unsafe_allow_html=True)


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
        if photos:
            st.image([p[1] for p in photos], width=96)
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

    step(3, "휴대전화번호")
    st.text_input(
        "휴대전화번호",
        key="phone",
        max_chars=13,
        placeholder="010-0000-0000",
        on_change=on_phone_change,
        label_visibility="collapsed",
    )

    step(4, f"문의 내용 ({MIN_MESSAGE_LEN}자 이상)")
    st.text_area(
        "문의 내용",
        key="message",
        height=130,
        placeholder="예) OO초 3층 교무실. 오늘 오전부터 인터넷이 안 되고 랜선을 바꿔도 동일합니다.",
        label_visibility="collapsed",
    )

    step(5, "문제 화면 사진 (1장 이상)")
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
        category, symptom, st.session_state.phone, st.session_state.message,
        len(photos), agreed,
    )
    if errors:
        for msg in errors:
            st.error(msg)
        return

    cfg = MailConfig(st.secrets.get("smtp", {}))
    if not cfg.ready:
        st.error("메일 발송 설정이 없습니다. 관리자에게 문의해 주세요.")
        return

    with st.spinner("전송 중입니다..."):
        try:
            send_inquiry(
                cfg, category, symptom,
                st.session_state.phone, st.session_state.message, photos,
            )
        except Exception:
            st.error("전송에 실패했습니다. 잠시 후 다시 시도해 주세요.")
            return

    st.session_state.sent = True
    st.session_state.receipt = {
        "category": category, "symptom": symptom, "photos": len(photos),
    }
    st.rerun()


def render_success() -> None:
    r = st.session_state.receipt
    st.markdown(
        '<div class="ok"><div class="big">접수가 완료되었습니다</div>'
        f'<div class="sub">{r.get("category", "")} · {r.get("symptom", "")}<br>'
        f'사진 {r.get("photos", 0)}장과 함께 담당자에게 전달했습니다.<br>'
        "입력하신 번호로 회신드립니다.</div></div>",
        unsafe_allow_html=True,
    )
    st.write("")
    if st.button("새 문의 작성", type="primary"):
        for key in ("phone", "message", "sent", "receipt"):
            st.session_state.pop(key, None)
        st.rerun()


def main() -> None:
    st.set_page_config(page_title=TITLE, page_icon="🏫", layout="centered")
    st.markdown(CSS, unsafe_allow_html=True)
    init_state()

    st.markdown(
        f'<div class="hero"><h1>{TITLE}</h1><p>{SUBTITLE}</p></div>',
        unsafe_allow_html=True,
    )

    if st.session_state.sent:
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
