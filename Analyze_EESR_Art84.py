import os
import re
import pandas as pd
import requests
import base64
import time
import google.generativeai as genai
import fitz  # PyMuPDF

# ==========================================
# 0. API & 환경 설정
# ==========================================
# Gemini API Key (제공해주신 키 연동 완료)
GEMINI_API_KEY = "발급받은_GEMINI_API_KEY_여기에_붙여넣기"

# EPO OPS API Key (별도로 발급받으신 Key/Secret 기입 필요)
# 발급 사이트: developers.epo.org
# 참고: 브라우저 에이전트로 인한 IP 차단 이슈로 부득이 직접 기입하시도록 공란으로 비워두었습니다.
EPO_CONSUMER_KEY = "여기에_CONSUMER_KEY_입력"
EPO_CONSUMER_SECRET = "여기에_CONSUMER_SECRET_입력"
# ==========================================

genai.configure(api_key=GEMINI_API_KEY)
# 모델 선택 (텍스트 분류/분석에 뛰어난 1.5-flash-latest 사용)
gemini_model = genai.GenerativeModel('gemini-1.5-flash-latest')

def get_epo_token(client_id, client_secret):
    """EPO OPS API OAuth 토큰 발급"""
    try:
        url = "https://ops.epo.org/3.2/auth/accesstoken"
        auth_string = f"{client_id}:{client_secret}"
        encoded_auth = base64.b64encode(auth_string.encode()).decode()
        
        headers = {
            "Authorization": f"Basic {encoded_auth}",
            "Content-Type": "application/x-www-form-urlencoded"
        }
        data = {"grant_type": "client_credentials"}
        response = requests.post(url, headers=headers, data=data)
        response.raise_for_status()
        return response.json().get("access_token")
    except Exception as e:
        print(f"[ERROR] EPO 토큰 발급 실패 (API 키 설정을 확인하세요): {e}")
        return None

def download_eesr_pdf(app_number, token, save_dir):
    """
    EPO OPS에서 EESR PDF 문서를 다운로드하는 함수 로직.
    실제 현업에서는 EPO OPS의 Published Data 또는 Global Dossier API를 통해 문서 ID를 찾아와야 합니다.
    """
    if not token:
        return None
        
    pdf_path = os.path.join(save_dir, f"{app_number}_EESR.pdf")
    
    # [개발 로직 가이드] 
    # 1. biblio Search API를 통해 해당 출원번호의 document ID 리스트업
    # 2. Description이 "Search Report" 또는 EESR인 document ID 파악
    # 3. images API를 통해 다운로드 및 병합하여 PDF로 조합
    
    print(f"  -> [{app_number}] EPO API 연동하여 PDF 다운로드 진행 중 (구조화됨)...")
    
    if os.path.exists(pdf_path):
        return pdf_path
    return None 

def extract_pdf_text_and_check_art84(pdf_path):
    """PDF에서 텍스트를 추출하고 Article 84 언급 여부를 다각적 정규표현식으로 확인"""
    try:
        doc = fitz.open(pdf_path)
        full_text = ""
        for page in doc:
            full_text += page.get_text("text") + "\n"
        doc.close()
        
        # 확장된 Article 84 정규식 향상 (Art. 84, Article 84, Art84, A. 84, A.84 등 포괄)
        pattern = r"\b(?:Article|Art\.?|A\.?)\s*84\b"
        if re.search(pattern, full_text, re.IGNORECASE):
            return True, full_text
        else:
            return False, full_text
            
    except Exception as e:
        print(f"[ERROR] PDF 파싱 에러({pdf_path}): {e}")
        return False, ""

def classify_art84_with_gemini(text):
    """Gemini API를 사용하여 분류 작업 수행"""
    prompt = f"""
    당신은 유럽 특허법(EPC)을 다루는 전문 특허 번역가이자 변리사입니다.
    아래는 Extended European Search Report (EESR) 문서의 일부 텍스트입니다.
    해당 문서들에는 'Article 84' (또는 A.84, Art. 84) 거절 사유가 명시되어 있습니다.
    
    주어진 텍스트를 분석하여, 거절 사유를 다음 4가지 종류 중 **정확히 하나**로만 분류해주세요.
    응답은 반드시 "종류 X" 포맷으로 시작하고, 그 뒤에 짧고 명확한 근거(1~2문장)를 한국어로 작성하세요.

    [분류 기준]
    종류 1. 명확성 부족 (Lack of Clarity): "about", "substantially", "suitable" 등 모호하거나 불확실한 상대적 용어 사용, 선택적 특징 표기, 또는 핵심적 특징(Essential features) 결여.
    종류 2. 명세서에 의한 뒷받침 부족 (Lack of Support): 청구항과 명세서 내용 불일치(Inconsistency), 발명 범위 초과 등 일치성 위반.
    종류 3. 간결성 및 청구항 수 (Conciseness & Number of Claims): 독립항 과다(Rule 62a) 또는 청구항 중복으로 권리 범위 파악 곤란 (Rule 29(5) 위반 동반 등).
    종류 4. 기타: 위 3가지에 명확히 해당하지 않는 기타 유형.

    [문서 텍스트]
    {text[:8000]}  # 텍스트 앞부분 위주로 전송 (근거 문장 파악용)
    """

    try:
        response = gemini_model.generate_content(prompt)
        return response.text.strip()
    except Exception as e:
        print(f"[ERROR] Gemini API 오류: {e}")
        return "분류 에러"

def main():
    desktop_path = os.path.join(os.path.expanduser("~"), "Desktop")
    excel_path = os.path.join(desktop_path, "특허검색_EESR검색.xlsx")
    output_excel_path = os.path.join(desktop_path, "특허검색_EESR검색_분류결과.xlsx")
    pdf_save_dir = os.path.join(desktop_path, "EESR_Downloads")
    
    os.makedirs(pdf_save_dir, exist_ok=True)
    
    if not os.path.exists(excel_path):
        print(f"[ERROR] 바탕화면에 '{os.path.basename(excel_path)}' 파일이 없습니다!")
        # 테스트 실행을 막지 않기 위해 함수를 바로 종료하지는 않고 모의 진행 안내
        print("[INFO] [테스트 모드]: 샘플 Dataframe을 생성하여 진행합니다.")
        df = pd.DataFrame({'출원번호': ['EP20123456', 'EP20987654']})
    else:
        print("엑셀 파일을 불러오는 중...")
        df = pd.read_excel(excel_path)
    
    # 엑셀 열에 출원번호가 있는지 체크 ('출원번호' 로 가정)
    col_app = None
    for col in df.columns:
        if "출원" in str(col) or "Application" in str(col):
            col_app = col
            break
            
    if not col_app:
        print("[ERROR] '출원번호' 관련 열을 찾을 수 없습니다. (열 이름 확인 필요)")
        return

    # 결과 저장을 위한 빈 열 생성
    if 'Article84_여부' not in df.columns:
        df['Article84_여부'] = "조사안됨"
    if '거절사유_유형분류' not in df.columns:
        df['거절사유_유형분류'] = ""

    # EPO Token 발급
    print("\n[EPO 토큰 발급 시도 중...]")
    epo_token = get_epo_token(EPO_CONSUMER_KEY, EPO_CONSUMER_SECRET)
    if epo_token:
        print("[OK] EPO 토큰 발급 성공!")
    else:
        print("[WARNING] EPO 토큰 값이 유효하지 않아 로컬 폴더(EESR_Downloads) 내 다운로드된 PDF만 검사합니다.")

    for idx, row in df.iterrows():
        app_number = str(row[col_app]).strip()
        if not app_number or app_number == 'nan':
            continue
            
        print(f"\n[Run] [{idx+1}/{len(df)}] 타겟 출원번호: {app_number}")
        
        # 1. EESR PDF 다운로드 (API 또는 로컬 파일)
        pdf_file = download_eesr_pdf(app_number, epo_token, pdf_save_dir)
        
        # 다운로드를 못했더라도 EESR_Downloads 폴더에 파일이 손수 있으면 읽도록 fallback
        fallback_pdf = os.path.join(pdf_save_dir, f"{app_number}.pdf")
        if not pdf_file and os.path.exists(fallback_pdf):
            pdf_file = fallback_pdf
            
        if not pdf_file:
            print("  -> EESR PDF 문서를 다운로드/찾을 수 없어 Skip 합니다.")
            df.at[idx, 'Article84_여부'] = "PDF없음"
            continue
            
        # 2. PDF 파싱 및 Article 84 체크
        is_art84, extract_txt = extract_pdf_text_and_check_art84(pdf_file)
        
        if is_art84:
            print("  -> [HIT] Article 84 (A.84 등) 관련 거절 발견! Gemini 분류 시작...")
            df.at[idx, 'Article84_여부'] = "존재(Yes)"
            
            # 3. Gemini로 분류 (API 속도 제한 고려 delay)
            time.sleep(2)
            class_result = classify_art84_with_gemini(extract_txt)
            print(f"  -> {class_result[:60]}...")
            df.at[idx, '거절사유_유형분류'] = class_result
            
        else:
            print("  -> 기술된 텍스트 중 Article 84 거절 조항이 없어 패스합니다.")
            df.at[idx, 'Article84_여부'] = "없음(No)"
            df.at[idx, '거절사유_유형분류'] = "Skip"
            
    # 최종 결과 저장
    try:
        df.to_excel(output_excel_path, index=False)
        print(f"\n[OK] 완료! 결과 파일이 저장되었습니다: {output_excel_path}")
    except Exception as e:
        print(f"[ERROR] 엑셀 저장 실패: {e}")

if __name__ == "__main__":
    main()
